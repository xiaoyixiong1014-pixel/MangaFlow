"""管理端任务死信队列测试模块（文件级说明）。

覆盖链路：
- 死信列表分页与归属用户联查回填；
- 普通用户访问 403；
- requeue 404（任务不存在）/ 400（非死信任务、归属项目缺失、余额不足）路径；
- requeue 成功：重新冻结积分（freeze 流水）、状态重置、operation_logs 留痕。
"""

import uuid

from sqlalchemy import select

from app.models import OperationLog, Task, Wallet, WalletTransaction
from tests.conftest import create_project


async def _insert_dead_task(db, project_id: int, **overrides) -> Task:
    """直插一条死信任务（内部辅助函数）。

    Args:
        db: 测试库会话。
        project_id: 归属项目 ID（传 None 模拟项目已删除）。
        **overrides: 可覆盖的任务字段（cost_points/type/retry_count 等）。

    Returns:
        Task: 已落库的死信任务 ORM 对象。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    task = Task(
        project_id=project_id,
        type=overrides.get("type", "image"),
        phase=overrides.get("phase", "full"),
        status=overrides.get("status", "failed"),
        error_code=overrides.get("error_code", "platform_error"),
        error_message=overrides.get("error_message", "重试耗尽仍失败"),
        retry_count=overrides.get("retry_count", 3),
        cost_points=overrides.get("cost_points", 10),
        idempotency_key=f"dlq_{uuid.uuid4().hex[:16]}",
        dead_letter=overrides.get("dead_letter", True),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def test_sixin_liebiao_requires_operator(client, auth):
    """普通用户访问死信列表返回 403。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文（普通用户）。

    Returns:
        None。

    Raises:
        AssertionError: 响应码非 403 时中断。
    """
    resp = await client.get("/api/admin/tasks/dead-letters", headers=auth["headers"])
    assert resp.status_code == 403, resp.text


async def test_sixin_liebiao_fenye_yu_user_backfill(client, auth, admin_auth, db):
    """死信列表：分页返回死信任务，联项目回填归属用户 ID。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文（项目归属者）。
        admin_auth: 运营方上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 列表为空或字段回填缺失时中断。
    """
    project = await create_project(client, auth["headers"])
    await _insert_dead_task(db, project["id"])
    await _insert_dead_task(db, project["id"])

    resp = await client.get(
        "/api/admin/tasks/dead-letters", headers=admin_auth["headers"], params={"page": 1, "page_size": 10}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    item = body["items"][0]
    assert item["type"] == "image"
    assert item["status"] == "failed"
    assert item["retry_count"] == 3
    assert item["cost_points"] == 10
    assert item["error_code"] == "platform_error"
    assert item["project_id"] == project["id"]
    assert item["user_id"] == auth["user_id"]


async def test_requeue_404_renwu_bucunzai(client, admin_auth):
    """requeue 任务不存在返回 404。

    Args:
        client: 内存 HTTP 客户端。
        admin_auth: 运营方上下文。

    Returns:
        None。

    Raises:
        AssertionError: 响应码非 404 时中断。
    """
    resp = await client.post(
        "/api/admin/tasks/999999/requeue",
        headers=admin_auth["headers"],
        json={"reason": "运营复核重试"},
    )
    assert resp.status_code == 404, resp.text


async def test_requeue_requires_operator(client, auth, db):
    """普通用户 requeue 返回 403。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 响应码非 403 时中断。
    """
    project = await create_project(client, auth["headers"])
    task = await _insert_dead_task(db, project["id"])
    resp = await client.post(
        f"/api/admin/tasks/{task.id}/requeue",
        headers=auth["headers"],
        json={"reason": "越权尝试"},
    )
    assert resp.status_code == 403, resp.text


async def test_requeue_reason_missing_422(client, admin_auth, auth, db):
    """requeue 缺少 reason 返回 422（Pydantic 必填校验）。

    Args:
        client: 内存 HTTP 客户端。
        admin_auth: 运营方上下文。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 响应码非 422 时中断。
    """
    project = await create_project(client, auth["headers"])
    task = await _insert_dead_task(db, project["id"])
    resp = await client.post(
        f"/api/admin/tasks/{task.id}/requeue", headers=admin_auth["headers"], json={}
    )
    assert resp.status_code == 422, resp.text


async def test_requeue_fei_sixin_400(client, admin_auth, auth, db):
    """requeue 非死信任务返回 400。

    Args:
        client: 内存 HTTP 客户端。
        admin_auth: 运营方上下文。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 响应码非 400 时中断。
    """
    project = await create_project(client, auth["headers"])
    task = await _insert_dead_task(db, project["id"], dead_letter=False)
    resp = await client.post(
        f"/api/admin/tasks/{task.id}/requeue",
        headers=admin_auth["headers"],
        json={"reason": "不应成功的重入队"},
    )
    assert resp.status_code == 400, resp.text
    assert "死信" in resp.json()["detail"]


async def test_requeue_xiangmu_missing_400(client, admin_auth, db):
    """requeue 归属项目缺失（project_id=NULL）返回 400。

    Args:
        client: 内存 HTTP 客户端。
        admin_auth: 运营方上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 响应码非 400 时中断。
    """
    task = await _insert_dead_task(db, None)
    resp = await client.post(
        f"/api/admin/tasks/{task.id}/requeue",
        headers=admin_auth["headers"],
        json={"reason": "项目已删除"},
    )
    assert resp.status_code == 400, resp.text
    assert "项目" in resp.json()["detail"]


async def test_requeue_yue_buzu_400(client, admin_auth, auth, db):
    """requeue 用户余额不足返回 400（原 409 转义）。

    Args:
        client: 内存 HTTP 客户端。
        admin_auth: 运营方上下文。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 响应码非 400 时中断。
    """
    project = await create_project(client, auth["headers"])
    task = await _insert_dead_task(db, project["id"], cost_points=10)
    # 清空钱包可用余额，模拟余额不足以重新冻结
    await db.execute(
        Wallet.__table__.update().where(Wallet.user_id == auth["user_id"]).values(points_balance=0)
    )
    await db.commit()

    resp = await client.post(
        f"/api/admin/tasks/{task.id}/requeue",
        headers=admin_auth["headers"],
        json={"reason": "余额不足场景"},
    )
    assert resp.status_code == 400, resp.text
    assert "余额不足" in resp.json()["detail"]


async def test_requeue_chenggong_jidong_liushen(client, admin_auth, auth, db):
    """requeue 成功：重新冻结积分、状态重置、操作日志留痕。

    Args:
        client: 内存 HTTP 客户端。
        admin_auth: 运营方上下文。
        auth: 随机注册用户上下文（任务归属者）。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(client, auth["headers"])
    task = await _insert_dead_task(db, project["id"], cost_points=10)

    resp = await client.post(
        f"/api/admin/tasks/{task.id}/requeue",
        headers=admin_auth["headers"],
        json={"reason": "供应商已恢复，运营复核后重试"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == task.id
    assert body["status"] == "queued"

    # 重新冻结积分：存在本次 requeue 产生的 freeze 流水
    # （worker 可能已并发领取并消费冻结，故断言流水存在而非余额数值）
    tx = (
        await db.execute(
            select(WalletTransaction).where(
                WalletTransaction.ref_task_id == task.id,
                WalletTransaction.type == "freeze",
                WalletTransaction.idempotency_key.like(f"task_{task.id}_requeue_%"),
            )
        )
    ).scalars().first()
    assert tx is not None, "requeue 未产生重新冻结的 freeze 流水"

    # 审计留痕：operation_logs 存在 admin_requeue_task 记录且带 reason
    log = (
        await db.execute(
            select(OperationLog).where(
                OperationLog.action == "admin_requeue_task",
                OperationLog.detail["task_id"].as_integer() == task.id,
            )
        )
    ).scalars().first()
    assert log is not None, "requeue 未写入操作日志"
    assert log.detail["reason"] == "供应商已恢复，运营复核后重试"
    assert log.user_id == admin_auth["user_id"]

    # 任务行状态重置（worker 可能已改写 status，但死信/重试字段不会被成功路径触碰）
    row = (
        await db.execute(
            select(Task.dead_letter, Task.retry_count).where(Task.id == task.id)
        )
    ).one()
    assert row.dead_letter is False
    assert row.retry_count == 0
