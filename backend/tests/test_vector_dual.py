"""pgvector 双模向量检索测试（Task 6，文件级说明）。

本机开发环境 PostgreSQL 无 pgvector 扩展，测试以「降级路径为主、模拟路径
为辅」两条腿覆盖：
- probe 降级：无扩展时 probe_pgvector 返回 False，ensure_pgvector_schema
  跳过建列（inspect 断言两表无 embedding_vec 影子列）；
- JSONB 降级检索：资产 / 记忆检索走 Python 余弦路径，造两条不同 embedding
  断言排序与返回结构同既有行为一致；
- 影子列缺失自愈回退：monkeypatch 模拟「探测可用」（available=True、dim=256，
  测试库真实无影子列，SQL 会真实执行并失败）→ 检索自动降级回 Python 路径
  不报错、upsert 双写失败不影响 JSONB 主存储落行。

说明：真实 pgvector 路径需真扩展环境（SQL 真跑，monkeypatch 造不出列），
开发机无扩展，以降级路径测试 + 运行期自愈回退测试覆盖，README 记录该取舍。
"""

import logging
from typing import Any, Dict, List

import pytest
from sqlalchemy import inspect as sa_inspect, select

from app.models import AssetEmbedding
from app.services import vector_store
from app.services.asset_service import semantic_search_assets, upsert_asset_embedding
from app.services.memory_service import search_memories, upsert_memory
from tests.conftest import create_project, unique_suffix


@pytest.fixture(autouse=True)
async def _reset_vector_cache():
    """autouse 夹具：每个用例前后重置 vector_store 进程级探测/维度缓存。

    防止本文件的 monkeypatch 或探测结果污染同进程内其他测试文件
    （pytest-asyncio session 循环下全部用例共享同一进程）。

    Yields:
        None。

    Raises:
        无。
    """
    vector_store.reset_pgvector_cache()
    yield
    vector_store.reset_pgvector_cache()


async def _get_columns(engine, table: str) -> List[str]:
    """读取指定表的列名清单（inspect 经 run_sync 驱动，内部辅助函数）。

    Args:
        engine: 测试库异步引擎。
        table: 表名。

    Returns:
        List[str]: 该表全部列名。

    Raises:
        Exception: 连接或反射失败向上抛出。
    """
    async with engine.connect() as conn:
        return await conn.run_sync(
            lambda c: [col["name"] for col in sa_inspect(c).get_columns(table)]
        )


async def test_probe_wu_kuo_zhan_fanhui_false_jianlie(test_engine):
    """无 pgvector 扩展时 probe 返回 False，ensure 跳过建列（降级零副作用）。

    Args:
        test_engine: 测试库异步引擎（会话级夹具，本机无 vector 扩展）。

    Returns:
        None。

    Raises:
        AssertionError: probe 结果非 False，或 ensure 后影子列意外出现时中断。
    """
    available = await vector_store.probe_pgvector(test_engine)
    assert available is False, "本机 PostgreSQL 未安装 vector 扩展，probe 应返回 False"
    # ensure 在不可用时应直接跳过，不产生任何 DDL 副作用
    await vector_store.ensure_pgvector_schema(test_engine)
    for table in ("asset_embeddings", "langgraph_store"):
        cols = await _get_columns(test_engine, table)
        assert "embedding_vec" not in cols, f"{table} 不应出现影子列（ensure 应跳过建列）"


async def test_jsonb_lujian_zichan_jiansuo_pai(client, auth, db):
    """资产语义搜索降级路径：Python 余弦排序与返回结构同既有行为一致。

    造两个不同文本的角色（创建 API 自动向量落行），搜索查询应命中
    文本相近者且分数更高、结果字段结构不变。

    Args:
        client: 内存 HTTP 客户端（造项目/角色走真实 API 链路）。
        auth: 随机注册用户上下文。
        db: 测试库异步会话（直调 semantic_search_assets 断言）。

    Returns:
        None。

    Raises:
        AssertionError: 排序或结构断言失败时中断。
    """
    tag = unique_suffix()
    project = await create_project(client, auth["headers"], title=f"双模项目_{tag}")
    created: Dict[str, int] = {}
    # 角色文本对：cyber 与查询文本共享大量 2-gram，pastoral 几乎不共享
    characters = [
        ("cyber", f"赛博朋克少女{tag}", "霓虹都市机械义体夜行者"),
        ("pastoral", f"田园牧场少女{tag}", "水彩宁静乡村麦田微风"),
    ]
    for key, name, desc in characters:
        resp = await client.post(
            f"/api/projects/{project['id']}/assets/characters",
            headers=auth["headers"],
            json={"name": name, "description": desc},
        )
        assert resp.status_code == 200, resp.text
        created[key] = resp.json()["id"]

    # 降级路径检索（缓存未探测 → available=False → Python 余弦）
    results = await semantic_search_assets(
        db, auth["user_id"], "赛博朋克 霓虹都市 机械义体", limit=5)
    assert len(results) == 2
    assert set(results[0].keys()) == {"asset_type", "asset_id", "name", "score", "preview"}
    assert all(r["asset_type"] == "character" for r in results)
    # 文本相近者分数更高且排第一（第二条与查询几乎无共享 2-gram，允许 0 分）
    assert results[0]["asset_id"] == created["cyber"]
    assert results[0]["score"] > 0
    assert results[0]["score"] > results[1]["score"]


async def test_jsonb_lujian_jiyi_jiansuo_pai(db, auth):
    """记忆语义检索降级路径：Python 余弦排序与返回结构同既有行为一致。

    Args:
        db: 测试库异步会话（直调 upsert_memory 造数）。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 排序或结构断言失败时中断。
    """
    await upsert_memory(db, auth["user_id"], "chat", "k-cyber",
                        "用户偏好：赛博朋克霓虹都市画面", commit=True)
    await upsert_memory(db, auth["user_id"], "chat", "k-pastoral",
                        "用户喜欢水彩田园牧场风景", commit=True)

    results = await search_memories(db, auth["user_id"], "赛博朋克霓虹都市", top_k=5)
    assert len(results) == 2
    assert set(results[0].keys()) == {"namespace", "key", "text", "score", "updated_at"}
    assert results[0]["key"] == "k-cyber"
    assert results[0]["namespace"] == "chat"
    # 文本相近者排第一（第二条与查询几乎无共享 2-gram，余弦 0 分为正常行为）
    assert results[0]["score"] > 0
    assert results[0]["score"] >= results[1]["score"]
    # 用户隔离不受双模改造影响
    assert await search_memories(db, auth["user_id"] + 999, "赛博朋克", top_k=5) == []


async def test_yingzi_lie_queshi_jiansuo_huitui(client, auth, db, test_engine, monkeypatch):
    """影子列缺失自愈回退：模拟「探测可用」但测试库真实无影子列。

    monkeypatch 置 available=True、dim=256（SQL 会真实执行并因列缺失失败）
    → 检索自动降级回 Python 路径不报错且结果结构一致；upsert 双写经
    SAVEPOINT 失败后 JSONB 主存储照常落行（双写失败不影响主流程）。

    Args:
        client: 内存 HTTP 客户端（造项目/角色）。
        auth: 随机注册用户上下文。
        db: 测试库异步会话。
        test_engine: 测试库引擎（inspect 断言列缺失）。
        monkeypatch: pytest monkeypatch（用例结束自动还原）。

    Returns:
        None。

    Raises:
        AssertionError: 影子列意外存在、检索报错或主存储未落行时中断。
    """
    # 前置断言：测试库确实没有影子列
    for table in ("asset_embeddings", "langgraph_store"):
        assert "embedding_vec" not in await _get_columns(test_engine, table)

    # 模拟「探测可用」（下一用例 autouse 夹具会重置缓存，不会跨用例污染）
    monkeypatch.setattr(vector_store, "_AVAILABLE", True)
    monkeypatch.setattr(vector_store, "_DIM", 256)
    assert vector_store.can_use_pgvector(256) is True

    # ---------- 资产检索：SQL 路径真实失败 → 自愈回退 Python，不报错 ----------
    tag = unique_suffix()
    project = await create_project(client, auth["headers"], title=f"回退项目_{tag}")
    resp = await client.post(
        f"/api/projects/{project['id']}/assets/characters",
        headers=auth["headers"],
        json={"name": f"赛博朋克少女{tag}", "description": "霓虹都市机械义体"},
    )
    assert resp.status_code == 200, resp.text
    cid = resp.json()["id"]
    results = await semantic_search_assets(db, auth["user_id"], "赛博朋克霓虹", limit=5)
    assert any(r["asset_id"] == cid for r in results), f"回退后应命中角色：{results}"
    assert set(results[0].keys()) == {"asset_type", "asset_id", "name", "score", "preview"}
    # 自愈降级已关闭 pgvector 开关，后续检索不再尝试 SQL
    assert vector_store.pgvector_available() is False

    # ---------- 记忆检索：同样回退不报错 ----------
    await upsert_memory(db, auth["user_id"], "chat", "k-fallback",
                        "用户偏好：赛博朋克霓虹都市", commit=True)
    memories = await search_memories(db, auth["user_id"], "赛博朋克霓虹", top_k=5)
    assert any(m["key"] == "k-fallback" for m in memories), f"回退后应命中记忆：{memories}"
    assert set(memories[0].keys()) == {"namespace", "key", "text", "score", "updated_at"}

    # ---------- upsert 双写失败不影响 JSONB 主存储 ----------
    # 重新模拟可用（上一步检索失败已自愈置 False）
    monkeypatch.setattr(vector_store, "_AVAILABLE", True)
    await upsert_asset_embedding(db, "character", cid, "更新后的赛博朋克描述文本")
    row = (
        await db.execute(
            select(AssetEmbedding).where(
                AssetEmbedding.asset_type == "character", AssetEmbedding.asset_id == cid)
        )
    ).scalar_one_or_none()
    assert row is not None, "双写失败时 JSONB 主存储必须照常落行"
    assert isinstance(row.embedding, dict) and isinstance(row.embedding.get("vector"), list)
    assert row.model in ("remote", "hash-bag")

    # ---------- 缓存重置夹具语义：重置后可用性回归「未探测」 ----------
    vector_store.reset_pgvector_cache()
    assert vector_store.pgvector_available() is False
    assert vector_store.pgvector_dim() is None
