"""模型效果对比 / 渠道健康摘要 / 创作统计测试模块（文件级说明）。

覆盖链路（Task 10.1 / 10.3）：
- GET  /api/models/health-summary：平台渠道健康摘要（渠道列表 + down/degraded 计数）
- POST /api/models/compare：逐渠道效果对比（渠道隔离、异常收敛、越权渠道报错、
  请求体校验 422）
- GET  /api/users/me/creation-stats：今日/本月创作统计聚合（D-04）

说明：对比测试使用指向本机关闭端口的渠道（连接立即被拒绝），真实网络调用
快速失败并收敛进结果，不需要真实外部服务；因此不标注 real 标记。
"""

import uuid
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelChannel, Task, WalletTransaction
from tests.conftest import create_project, register_user


async def _seed_platform_channel(
    db: AsyncSession,
    base_url: str = "http://127.0.0.1:9/v1",
    health_status: str = "healthy",
    models: Optional[Dict[str, Any]] = None,
) -> ModelChannel:
    """直插一条平台渠道（测试辅助函数）。

    Args:
        db: 测试库会话。
        base_url: 渠道地址（默认指向本机保留端口，连接立即失败）。
        health_status: 初始健康状态。
        models: 渠道模型目录（缺省 {"chat": ["mock-model"]}）。

    Returns:
        ModelChannel: 已落库的渠道 ORM 对象。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    channel = ModelChannel(
        protocol="openai",
        base_url=base_url,
        api_key_encrypted=f"sk-test-{uuid.uuid4().hex[:8]}",
        owner_user_id=None,
        models=models or {"chat": ["mock-model"]},
        health_status=health_status,
        enabled=True,
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)
    return channel


async def test_health_summary_counts(client, auth, db):
    """渠道健康摘要：返回平台渠道列表与 down/degraded 计数（D-05）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    await _seed_platform_channel(db, health_status="healthy")
    await _seed_platform_channel(db, health_status="down")
    await _seed_platform_channel(db, health_status="degraded")

    resp = await client.get("/api/models/health-summary", headers=auth["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["down_count"] == 1
    assert body["degraded_count"] == 1
    assert len(body["channels"]) == 3
    assert all(c["health_status"] in ("healthy", "down", "degraded") for c in body["channels"])
    # 不泄露 Key 相关字段
    assert all("api_key" not in c and "api_key_encrypted" not in c for c in body["channels"])


async def test_compare_llm_channels_isolated(client, auth, db):
    """LLM 效果对比：逐渠道返回结果，失败渠道收敛为 ok=False（PL-11）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    ch1 = await _seed_platform_channel(db)
    ch2 = await _seed_platform_channel(db, base_url="http://127.0.0.1:9/v2")

    resp = await client.post(
        "/api/models/compare",
        headers=auth["headers"],
        json={"capability": "llm", "prompt": "用一句话介绍漫剧", "channel_ids": [ch1.id, ch2.id]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    results = body["results"]
    # 结果与请求顺序一致、数量一致
    assert [r["channel_id"] for r in results] == [ch1.id, ch2.id]
    # 本机关闭端口：全部渠道快速失败但结构完整（异常收敛，不抛 500）
    for r in results:
        assert r["ok"] is False
        assert r["error"]
        assert r["text"] is None
        assert r["elapsed_ms"] >= 0
    # 不存在的渠道同样收敛为 ok=False
    resp2 = await client.post(
        "/api/models/compare",
        headers=auth["headers"],
        json={"capability": "llm", "prompt": "hi", "channel_ids": [99999]},
    )
    assert resp2.status_code == 200, resp2.text
    r0 = resp2.json()["results"][0]
    assert r0["ok"] is False and "渠道不存在" in r0["error"]


async def test_compare_validation_and_owner_scope(client, auth, db):
    """效果对比校验：channel_ids 超限 422；他人自有渠道视为不可访问（PL-11）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    # 超过 4 个渠道 → 422
    resp = await client.post(
        "/api/models/compare",
        headers=auth["headers"],
        json={"capability": "llm", "prompt": "hi", "channel_ids": [1, 2, 3, 4, 5]},
    )
    assert resp.status_code == 422

    # 他人自有渠道（owner_user_id 为另一位真实用户）→ ok=False 渠道不存在
    other = await register_user(client, prefix="foreign")
    foreign = ModelChannel(
        protocol="openai",
        base_url="http://127.0.0.1:9/v1",
        api_key_encrypted="sk-foreign",
        owner_user_id=other["user"]["id"],
        models={"chat": ["m"]},
        health_status="unknown",
        enabled=True,
    )
    db.add(foreign)
    await db.commit()
    await db.refresh(foreign)

    resp2 = await client.post(
        "/api/models/compare",
        headers=auth["headers"],
        json={"capability": "llm", "prompt": "hi", "channel_ids": [foreign.id]},
    )
    assert resp2.status_code == 200, resp2.text
    r0 = resp2.json()["results"][0]
    assert r0["channel_id"] == foreign.id
    assert r0["ok"] is False and "渠道不存在" in r0["error"]


async def test_creation_stats_aggregation(client, auth, db):
    """创作统计：项目/图片任务/扣费聚合进入 today 与 month（D-04）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(client, auth["headers"])

    # 一条完成的 image 任务 + 一条 consume 扣费流水
    db.add(Task(
        project_id=project["id"], type="image", phase="full", status="done",
        progress=100, cost_points=10, idempotency_key=f"t_{uuid.uuid4().hex[:10]}",
    ))
    db.add(WalletTransaction(
        user_id=auth["user_id"], type="consume", amount=-50, bear="user",
        idempotency_key=f"c_{uuid.uuid4().hex[:10]}",
    ))
    await db.commit()

    resp = await client.get("/api/users/me/creation-stats", headers=auth["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["today"]["projects"] >= 1
    assert body["month"]["projects"] >= 1
    assert body["today"]["images"] >= 1
    assert body["today"]["points_spent"] >= 50
    # today 与 month 同层级字段一致
    assert set(body["today"].keys()) == {"projects", "images", "video_seconds", "points_spent"}
