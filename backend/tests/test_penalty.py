"""分级处罚测试模块（文件级说明）。

覆盖 Task 4.3 分级处罚（C-04）全链路：
- apply_violation_penalty：违规累计 1-2 次置 restricted、≥3 次置 banned，
  每次处罚落 system 类型站内通知；
- banned 用户提交生成任务 → 403「账号已被禁用」；
- restricted 用户当日任务数达上限（10）→ 429「今日生成次数已达上限」；
- 机审 violate（违规词提示词）任务失败后自动处罚（violation_count=1）；
- 管理端解禁 /unban：状态回 active、计数清零、解禁后可正常提交生成。
"""

import uuid

import pytest
from sqlalchemy import select

from app.models import Notification, OperationLog, Task, User
from app.services.review_service import apply_violation_penalty
from tests.conftest import (
    create_project,
    get_wallet_model,
    insert_storyboard,
    unique_suffix,
    wait_task_terminal,
)


async def _project_with_storyboard(client, auth, db, tag: str):
    """辅助：建项目并直插一条分镜（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        tag: 标题防冲突后缀。

    Returns:
        Tuple[int, Storyboard]: (项目 ID, 分镜对象)。

    Raises:
        AssertionError: 项目创建返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"处罚项目_{tag}")
    sb = await insert_storyboard(db, project["id"], sequence=1)
    return project["id"], sb


async def test_fenji_chufa_leiji_zhuangtai(client, auth, db):
    """apply_violation_penalty：累计 1-2 次受限、≥3 次禁用，每次落 system 通知。

    Args:
        client: 内存 HTTP 客户端（本用例仅用于夹具依赖一致性）。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    uid = auth["user_id"]
    assert await apply_violation_penalty(db, uid) == "restricted"
    assert await apply_violation_penalty(db, uid) == "restricted"
    assert await apply_violation_penalty(db, uid) == "banned"

    user = await db.get(User, uid)
    assert user.account_status == "banned"
    assert user.violation_count == 3

    notifications = (
        await db.execute(
            select(Notification).where(
                Notification.user_id == uid, Notification.type == "system"
            )
        )
    ).scalars().all()
    assert len(notifications) == 3


async def test_banned_yonghu_shengcheng_403(client, auth, db):
    """banned 用户提交关键帧生成 → 403「账号已被禁用」。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _project_with_storyboard(client, auth, db, unique_suffix())
    user = await db.get(User, auth["user_id"])
    user.account_status = "banned"
    await db.commit()

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"], json={"phase": "trial"},
    )
    assert resp.status_code == 403, resp.text
    assert "禁用" in resp.json()["detail"]


async def test_restricted_xianliu_429(client, auth, db):
    """restricted 用户当日任务数达上限（10）→ 429 限流。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _project_with_storyboard(client, auth, db, unique_suffix())
    user = await db.get(User, auth["user_id"])
    user.account_status = "restricted"
    await db.commit()

    # 直插 10 条当日已完成任务（不计费、不进队列，仅构造日计数）
    for i in range(10):
        db.add(Task(
            project_id=pid, storyboard_id=None, type="image", phase="trial",
            status="done", cost_points=10, progress=100,
            idempotency_key=f"limit_{auth['user_id']}_{uuid.uuid4().hex[:12]}",
            meta={},
        ))
    await db.commit()

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"], json={"phase": "trial"},
    )
    assert resp.status_code == 429, resp.text
    assert "上限" in resp.json()["detail"]

    # 未达上限（9 条）时正常放行
    await db.execute(
        Task.__table__.delete().where(Task.project_id == pid, Task.type == "image")
    )
    await db.commit()
    ok = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"], json={"phase": "trial"},
    )
    assert ok.status_code == 200, ok.text
    await wait_task_terminal(client, auth["headers"], ok.json()["tasks"][0]["id"], timeout=30.0)


async def test_weigui_renwu_zidong_chufa(client, auth, db):
    """机审 violate：违规词提示词任务失败后自动处罚（计数 1、状态受限、退款）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _project_with_storyboard(client, auth, db, unique_suffix())

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial", "prompt_editable": "血腥屠杀的冲突画面"},
    )
    assert resp.status_code == 200, resp.text
    final = await wait_task_terminal(
        client, auth["headers"], resp.json()["tasks"][0]["id"], timeout=30.0
    )
    assert final["status"] == "failed", final
    assert final["error_code"] == "content_violation"

    user = await db.get(User, auth["user_id"])
    assert user.violation_count == 1
    assert user.account_status == "restricted"

    wallet = await get_wallet_model(db, auth["user_id"])
    assert wallet.points_balance == 5000
    assert wallet.frozen_points == 0


async def test_jiejin_hou_huifu_shengcheng(client, auth, db, admin_auth):
    """管理端解禁：/unban 置回 active、计数清零、留痕，解禁后可正常提交生成。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        admin_auth: 运营方上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _project_with_storyboard(client, auth, db, unique_suffix())
    user = await db.get(User, auth["user_id"])
    user.account_status = "banned"
    user.violation_count = 3
    await db.commit()

    banned = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"], json={"phase": "trial"},
    )
    assert banned.status_code == 403

    unban = await client.post(
        f"/api/admin/users/{auth['user_id']}/unban", headers=admin_auth["headers"]
    )
    assert unban.status_code == 200, unban.text
    assert "解禁" in unban.json()["message"]

    await db.refresh(user)
    assert user.account_status == "active"
    assert user.violation_count == 0

    # 留痕：operation_logs 存在 admin_unban_user 高危记录
    logs = (
        await db.execute(
            select(OperationLog).where(
                OperationLog.user_id == admin_auth["user_id"],
                OperationLog.action == "admin_unban_user",
            )
        )
    ).scalars().all()
    assert len(logs) == 1
    assert logs[0].risk_level == "high"
    assert logs[0].detail["target_user_id"] == auth["user_id"]

    # 解禁后生成接口恢复可用
    ok = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"], json={"phase": "trial"},
    )
    assert ok.status_code == 200, ok.text
    await wait_task_terminal(client, auth["headers"], ok.json()["tasks"][0]["id"], timeout=30.0)
