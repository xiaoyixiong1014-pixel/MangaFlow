"""用户长期记忆服务模块（文件级说明）。

基于 langgraph_store 表实现 MA-07 用户记忆语义检索：
- upsert_memory：把记忆文本向量化后以 KV 形式写入 langgraph_store
  （value JSONB 存 {"text", "embedding", "updated_at"}）；
- search_memories：遍历该用户的记忆行，按 value.embedding 与查询向量
  的余弦相似度 TopK 返回。

命名空间约定：langgraph_store 无 user_id 列，用户隔离通过
namespace = "user_memory:{user_id}:{namespace}" 前缀实现，且检索时
只命中该前缀行，不会污染兼作站点设置 KV 的 namespace='site_settings'。

存储方案说明：本机未装 pgvector，向量以 JSONB 承载、相似度在 Python
侧计算；数据量大时切换 pgvector（ivfflat 索引）只需替换本模块的
存储与检索函数。
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LanggraphStore
from app.providers.embedding import cosine_similarity, embed_text

logger = logging.getLogger("mangaflow.services.memory_service")

# 用户记忆的 namespace 前缀（用户隔离约定，见文件级说明）
MEMORY_NS_PREFIX = "user_memory:"


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
    依赖 uk_store_ns_key 唯一索引做 ON CONFLICT 更新。

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
    stmt = pg_insert(LanggraphStore).values(
        namespace=f"{MEMORY_NS_PREFIX}{user_id}:{namespace or 'default'}",
        key=key,
        value=payload,
        embedding={"vector": vector},
    ).on_conflict_do_update(
        index_elements=["namespace", "key"],
        set_={"value": payload, "embedding": {"vector": vector}},
    )
    await db.execute(stmt)
    if commit:
        await db.commit()


async def search_memories(
    db: AsyncSession, user_id: int, query: str, top_k: int = 5
) -> List[Dict[str, Any]]:
    """在该用户的记忆中按查询文本做语义 TopK 检索（MA-07）。

    遍历 namespace 以 "user_memory:{user_id}:" 开头的 langgraph_store 行，
    取 value.text 与 value.embedding 计算余弦相似度排序；数据量小的当前
    阶段全表扫可接受（pgvector 升级点：替换为索引检索）。

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
    prefix = f"{MEMORY_NS_PREFIX}{user_id}:"
    rows = (
        await db.execute(
            select(LanggraphStore).where(LanggraphStore.namespace.like(prefix + "%"))
        )
    ).scalars().all()
    qvec = await embed_text(query)
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
