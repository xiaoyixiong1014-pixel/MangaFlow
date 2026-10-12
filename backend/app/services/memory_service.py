"""用户长期记忆服务模块（文件级说明）。

基于 langgraph_store 表实现 MA-07 用户记忆语义检索：
- upsert_memory：把记忆文本向量化后以 KV 形式写入 langgraph_store
  （value JSONB 存 {"text", "embedding", "updated_at"}），并在 pgvector
  可用时同步双写影子列 embedding_vec（SAVEPOINT 隔离，失败不影响主存储）；
- search_memories：pgvector 可用且查询向量维度与影子列一致时走 SQL 余弦
  TopK（ivfflat 索引），否则遍历该用户的记忆行按 value.embedding 与查询
  向量的余弦相似度 TopK 返回；两种模式返回结构完全一致。

命名空间约定：langgraph_store 无 user_id 列，用户隔离通过
namespace = "user_memory:{user_id}:{namespace}" 前缀实现，且检索时
只命中该前缀行，不会污染兼作站点设置 KV 的 namespace='site_settings'。

存储方案说明：JSONB 为主存储（迁移期安全）；pgvector 影子列由
services/vector_store 幂等维护，本模块仅做双写与检索分流。
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy import select, text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LanggraphStore
from app.providers.embedding import cosine_similarity, embed_text
from app.services import vector_store

logger = logging.getLogger("mangaflow.services.memory_service")

# 用户记忆的 namespace 前缀（用户隔离约定，见文件级说明）
MEMORY_NS_PREFIX = "user_memory:"


async def _search_memories_pgvector(
    db: AsyncSession, user_id: int, qvec: List[float], top_k: int
) -> List[Dict[str, Any]]:
    """pgvector SQL 余弦 TopK 记忆检索（内部函数，影子列路径）。

    仅命中 namespace 前缀匹配且影子列非空的行，按 `1 - cosine_distance`
    （ivfflat 的 <=> 距离语义）排序取 TopK；text/updated_at 仍从 value
    JSONB 读取，返回结构与 Python 路径完全一致。

    Args:
        db: 异步数据库会话。
        user_id: 当前用户 ID。
        qvec: 查询向量（维度已与影子列一致，由 can_use_pgvector 保证）。
        top_k: 返回条数上限。

    Returns:
        List[Dict[str, Any]]: [{"namespace", "key", "text", "score", "updated_at"}]，
            按 score 降序。

    Raises:
        Exception: SQL 执行失败向上抛出，由调用方捕获后自愈降级回 Python 路径。
    """
    prefix = f"{MEMORY_NS_PREFIX}{user_id}:"
    sql = sa_text(
        "SELECT namespace AS namespace, key AS key, value AS value, "
        "1 - (embedding_vec <=> CAST(:qv AS vector)) AS score "
        "FROM langgraph_store "
        "WHERE namespace LIKE :prefix AND embedding_vec IS NOT NULL "
        "ORDER BY embedding_vec <=> CAST(:qv AS vector) "
        "LIMIT :k"
    )
    rows = (
        await db.execute(
            sql, {"qv": vector_store.vector_literal(qvec), "prefix": prefix + "%", "k": top_k}
        )
    ).all()
    scored: List[Dict[str, Any]] = []
    for r in rows:
        value = r.value if isinstance(r.value, dict) else {}
        mem_text = value.get("text")
        # 非记忆结构行（无 text）跳过，与 Python 路径的兼容语义一致
        if not isinstance(mem_text, str):
            continue
        scored.append({
            "namespace": r.namespace[len(prefix):],
            "key": r.key,
            "text": mem_text,
            "score": round(float(r.score), 4),
            "updated_at": value.get("updated_at"),
        })
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored


async def upsert_memory(
    db: AsyncSession,
    user_id: int,
    namespace: str,
    key: str,
    text: str,
    commit: bool = True,
) -> None:
    """把一条记忆文本向量化后 upsert 到 langgraph_store（MA-07）。

    写入结构：namespace = "user_memory:{user_id}:{namespace}"，
    value JSONB = {"text": 原文, "embedding": [float...], "updated_at": ISO 时间}，
    embedding 列同步存 {"vector": [...], "model": 来源标识}（dict 形态）。
    pgvector 可用时同步双写影子列 embedding_vec（SAVEPOINT 隔离，失败仅
    告警，不影响 JSONB 主存储）。依赖 uk_store_ns_key 唯一索引做 ON CONFLICT
    更新。

    Args:
        db: 异步数据库会话。
        user_id: 归属用户 ID。
        namespace: 记忆命名空间（如 "chat" / "preference"）。
        key: 记忆键（如 thread_id）。
        text: 记忆文本（会话摘要 / 用户偏好等）。
        commit: 是否由本函数提交事务；嵌入调用方事务（如 agent_chat
            会话摘要同事务写入）时传 False。

    Returns:
        None。

    Raises:
        无：向量失败由 embed_text 降级 hash-bag；写库异常向上抛出由
            调用方决定是否吞掉（commit=False 的调用方自带兜底）。
    """
    text = (text or "").strip()
    if not text:
        return  # 空文本无需写入
    vector = await embed_text(text)
    payload = {
        "text": text,
        "embedding": vector,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    ns_full = f"{MEMORY_NS_PREFIX}{user_id}:{namespace or 'default'}"
    stmt = pg_insert(LanggraphStore).values(
        namespace=ns_full,
        key=key,
        value=payload,
        embedding={"vector": vector},
    ).on_conflict_do_update(
        index_elements=["namespace", "key"],
        set_={"value": payload, "embedding": {"vector": vector}},
    )
    await db.execute(stmt)
    # pgvector 双写：影子列与 JSONB 同事务（commit 由调用方控制时随外层提交）
    await vector_store.sync_memory_embedding_vec(db, ns_full, key, vector)
    if commit:
        await db.commit()


async def search_memories(
    db: AsyncSession, user_id: int, query: str, top_k: int = 5
) -> List[Dict[str, Any]]:
    """在该用户的记忆中按查询文本做语义 TopK 检索（MA-07，pgvector 双模分流）。

    - pgvector 可用且查询向量维度与影子列一致 → SQL 余弦 TopK（ivfflat 索引，
      见 _search_memories_pgvector）；SQL 失败自愈降级并回退 Python 路径；
    - 否则遍历 namespace 以 "user_memory:{user_id}:" 开头的 langgraph_store
      行，按 value.embedding 计算余弦相似度排序（降级路径，行为不变）；
    - 两种模式返回结构完全一致，前端无感知。

    Args:
        db: 异步数据库会话。
        user_id: 当前用户 ID。
        query: 查询文本。
        top_k: 返回条数上限。

    Returns:
        List[Dict[str, Any]]: [{"namespace", "key", "text", "score", "updated_at"}]，
            按 score 降序；非记忆结构行（如站点设置 KV）自动跳过。

    Raises:
        无：查询失败场景不存在（该用户无记忆时返回空列表）。
    """
    qvec = await embed_text(query)
    # ---------- pgvector 双模分流：影子列可用且维度一致时走 SQL 余弦 TopK ----------
    if vector_store.can_use_pgvector(len(qvec)):
        try:
            return await _search_memories_pgvector(db, user_id, qvec, top_k)
        except Exception as exc:  # noqa: BLE001 —— SQL 路径失败自愈降级
            vector_store.mark_pgvector_unavailable(exc)
            await db.rollback()  # 释放 aborted 事务，保证后续只读查询可用

    # ---------- Python 降级路径：全量遍历 + 余弦（行为与既有实现一致） ----------
    prefix = f"{MEMORY_NS_PREFIX}{user_id}:"
    rows = (
        await db.execute(
            select(LanggraphStore).where(LanggraphStore.namespace.like(prefix + "%"))
        )
    ).scalars().all()
    scored: List[Dict[str, Any]] = []
    for row in rows:
        value = row.value if isinstance(row.value, dict) else {}
        mem_text = value.get("text")
        vec = value.get("embedding")
        # 非记忆结构（无 text/embedding）的行直接跳过，兼容同表其他 KV 用途
        if not isinstance(mem_text, str) or not isinstance(vec, list):
            continue
        scored.append({
            "namespace": row.namespace[len(prefix):],
            "key": row.key,
            "text": mem_text,
            "score": round(cosine_similarity(qvec, vec), 4),
            "updated_at": value.get("updated_at"),
        })
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:top_k]
