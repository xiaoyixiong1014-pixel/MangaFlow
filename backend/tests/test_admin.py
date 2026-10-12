"""管理端接口测试模块（文件级说明）。

覆盖链路：
- 普通用户（end_user）访问 /api/admin/* 返回 403
- 运营方查询平台总览 overview 200 与结构
- 积分调整：余额变更 + operation_logs 留痕（admin_adjust_points）
- 禁用用户后：登录被拒（实现为 403）、旧令牌访问受保护接口 401
- 审核人工复核：reject 缺 suggestion 400；approve 置 approved
"""

import pytest
from sqlalchemy import select

from app.models import OperationLog, ReviewRecord, Wallet
from tests.conftest import create_project, register_user


async def test_putong_yonghu_fangwen_admin_403(client, auth):
    """普通用户访问管理端总览返回 403。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册普通用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 403 时中断。
    """
    resp = await client.get("/api/admin/dashboard/overview", headers=auth["headers"])
    assert resp.status_code == 403

    resp2 = await client.get("/api/admin/users", headers=auth["headers"])
    assert resp2.status_code == 403


async def test_admin_overview_200_jiegou(client, admin_auth):
    """运营方查询平台总览：200 且包含全部统计键与逐日数组。

    Args:
        client: 内存 HTTP 客户端。
        admin_auth: 运营方账号上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    resp = await client.get(
        "/api/admin/dashboard/overview", headers=admin_auth["headers"], params={"range": "7d"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in (
        "users_total", "users_new", "revenue_total", "orders_paid",
        "points_liability", "tasks_total", "tasks_failed",
        "revenue_by_day", "generation_by_day",
    ):
        assert key in body
    assert body["users_total"] >= 1
    assert len(body["revenue_by_day"]) == 7
    assert len(body["generation_by_day"]) == 7

    # 非法 range 返回 400
    bad = await client.get(
        "/api/admin/dashboard/overview", headers=admin_auth["headers"], params={"range": "1h"}
    )
    assert bad.status_code == 400


async def test_jifen_tiaozheng_liuhen(client, auth, admin_auth, db):
    """管理员调整积分：余额 +123，operation_logs 出现 admin_adjust_points 留痕。

    Args:
        client: 内存 HTTP 客户端。
        auth: 目标普通用户上下文。
        admin_auth: 运营方账号上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    target_id = auth["user_id"]
    resp = await client.post(
        f"/api/admin/users/{target_id}/points",
        headers=admin_auth["headers"],
        json={"amount": 123, "reason": "测试活动补偿"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["points_balance"] == 5000 + 123
    assert body["frozen_points"] == 0

    # 审计留痕：action + detail.target_user_id
    logs = (
        await db.execute(
            select(OperationLog).where(OperationLog.action == "admin_adjust_points")
        )
    ).scalars().all()
    assert len(logs) >= 1
    log = logs[-1]
    assert log.user_id == admin_auth["user_id"]  # 操作人 = 管理员
    assert log.detail["target_user_id"] == target_id
    assert log.detail["amount"] == 123
    assert log.risk_level == "high"

    # 非法调整：金额 0 → 400
    bad = await client.post(
        f"/api/admin/users/{target_id}/points",
        headers=admin_auth["headers"],
        json={"amount": 0, "reason": "零调整"},
    )
    assert bad.status_code == 400


async def test_jinyong_yonghu_hou_denglu_jujue(client, auth, admin_auth):
    """禁用用户后：登录被拒（实现为 403），旧令牌访问受保护接口 401。

    Args:
        client: 内存 HTTP 客户端。
        auth: 被禁用的普通用户上下文。
        admin_auth: 运营方账号上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    target_id = auth["user_id"]
    resp = await client.post(
        f"/api/admin/users/{target_id}/status",
        headers=admin_auth["headers"],
        json={"status": 0, "reason": "违规行为封禁"},
    )
    assert resp.status_code == 200, resp.text
    assert "禁用" in resp.json()["message"]

    # 登录被拒（账号禁用语义为 403；与「凭证错误 401」区分）
    login = await client.post(
        "/api/auth/login",
        json={"username": auth["payload"]["username"], "password": auth["payload"]["password"]},
    )
    assert login.status_code == 403

    # 旧 access 令牌立即失效：401
    me = await client.get("/api/auth/me", headers=auth["headers"])
    assert me.status_code == 401


async def test_shenhe_bohui_que_suggestion_400(client, admin_auth, db):
    """人工复核驳回缺少 suggestion 返回 400；补齐后通过。

    Args:
        client: 内存 HTTP 客户端。
        admin_auth: 运营方账号上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    # 造一个待人工复核的机审违规记录
    project = await create_project(client, admin_auth["headers"], title="审核项目")
    record = ReviewRecord(
        project_id=project["id"],
        gate="generate",
        target_type="image",
        target_id=None,
        result="violate",
        violation_type="banned_word",
    )
    db.add(record)
    await db.commit()

    # 待复核队列可见
    listing = await client.get(
        "/api/admin/review", headers=admin_auth["headers"], params={"tab": "pending"}
    )
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1

    # 驳回缺 suggestion → 400
    reject = await client.post(
        f"/api/admin/review/{record.id}/handle",
        headers=admin_auth["headers"],
        json={"action": "reject"},
    )
    assert reject.status_code == 400
    assert "建议" in reject.json()["detail"]

    # 通过（approve）→ manual_status=approved、handler 记录为管理员
    approve = await client.post(
        f"/api/admin/review/{record.id}/handle",
        headers=admin_auth["headers"],
        json={"action": "approve", "suggestion": None},
    )
    assert approve.status_code == 200, approve.text
    assert approve.json()["manual_status"] == "approved"
    assert approve.json()["handler_id"] == admin_auth["user_id"]


async def test_admin_yonghu_liebiao(client, auth, admin_auth):
    """管理端用户列表：普通用户出现在列表中且带钱包余额。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册普通用户上下文。
        admin_auth: 运营方账号上下文。

    Returns:
        None.

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    resp = await client.get(
        "/api/admin/users",
        headers=admin_auth["headers"],
        params={"search": auth["payload"]["username"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == auth["user_id"]
    assert item["points_balance"] == 5000
    assert item["role"] == "user"


async def test_admin_yonghu_liebiao_jifen_yu_db_yizhi(client, auth, admin_auth, db):
    """用户列表积分与 DB 钱包严格一致且非零（A-BUG-1 回归）。

    UI 报告缺陷：列表积分恒 0 而实际钱包余额 52380+。后端需保证：
    1. 列表接口聚合 Wallet.points_balance（含管理端调整后的最新值）；
    2. 响应为扁平结构（points_balance 直接位于 item 顶层、不存在 wallet
       嵌套键）—— 前端列表按 row.points_balance 绑定，若后端改为 wallet
       嵌套将复现恒 0。

    Args:
        client: 内存 HTTP 客户端。
        auth: 目标普通用户上下文。
        admin_auth: 运营方账号上下文。
        db: 测试库会话。

    Returns:
        None.

    Raises:
        AssertionError: 接口余额与 DB 不一致、或结构契约被破坏时中断。
    """
    from tests.conftest import get_wallet_model

    target_id = auth["user_id"]

    # 1. 直改钱包为报告同款非零值（52430），冻结 100
    wallet = (
        await db.execute(select(Wallet).where(Wallet.user_id == target_id))
    ).scalar_one()
    wallet.points_balance = 52430
    wallet.frozen_points = 100
    wallet.version += 1
    await db.commit()

    resp = await client.get(
        "/api/admin/users",
        headers=admin_auth["headers"],
        params={"search": auth["payload"]["username"]},
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]

    # 2. 接口余额与 DB 快照一致（非 0）
    snapshot = await get_wallet_model(db, target_id)
    assert item["points_balance"] == snapshot.points_balance == 52430
    assert item["frozen_points"] == snapshot.frozen_points == 100

    # 3. 扁平结构契约：item 顶层直接携带余额字段，不存在 wallet 嵌套键
    assert "points_balance" in item
    assert "frozen_points" in item
    assert "wallet" not in item

    # 4. 管理端扣减 50 后，列表同步反映新余额（对应 UI 报告"扣减后仍显示 0"）
    adjust = await client.post(
        f"/api/admin/users/{target_id}/points",
        headers=admin_auth["headers"],
        json={"amount": -50, "reason": "A-BUG-1 回归：扣减后核对列表"},
    )
    assert adjust.status_code == 200, adjust.text
    after = await client.get(
        "/api/admin/users",
        headers=admin_auth["headers"],
        params={"search": auth["payload"]["username"]},
    )
    assert after.status_code == 200, after.text
    assert after.json()["items"][0]["points_balance"] == 52430 - 50
