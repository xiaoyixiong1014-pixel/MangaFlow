"""生成任务接口测试模块（文件级说明）。

覆盖链路（依赖进程内 mock 供应商执行器）：
- 关键帧 trial：任务入队 → 轮询至 done（最长 30s）→ 钱包扣 10、
  storyboard.keyframe_url 回写、media_assets 有记录
- 同分镜同阶段重复提交 409 防重
- 违规词提示词：任务 failed + error_code=content_violation + 审核记录
  violate + 冻结退款
- 取消 queued 任务：canceled + 冻结退款
"""

import pytest
from sqlalchemy import select

from app.models import MediaAsset, ReviewRecord, Storyboard
from tests.conftest import (
    create_project,
    get_wallet_model,
    insert_storyboard,
    unique_suffix,
    wait_task_terminal,
)


async def _make_project_with_storyboard(client, auth, db, tag: str):
    """辅助：建项目并直插一条分镜，返回 (project_id, storyboard)（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        tag: 标题防冲突后缀。

    Returns:
        Tuple[int, Storyboard]: (项目 ID, 分镜 ORM 对象)。

    Raises:
        AssertionError: 接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"生成项目_{tag}")
    sb = await insert_storyboard(db, project["id"], sequence=1)
    return project["id"], sb


async def test_guanjianzhen_shiyan_wancheng_koufei(client, auth, db):
    """关键帧 trial：任务 done 后扣 10 积分、关键帧回写、媒体素材入库。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _make_project_with_storyboard(client, auth, db, unique_suffix())

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial", "prompt_editable": "夕阳下的天台，少女远眺城市"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_cost"] == 10
    assert len(body["tasks"]) == 1
    task = body["tasks"][0]
    assert task["type"] == "image"
    assert task["phase"] == "trial"

    # 轮询至终态（mock 供应商本地生成很快，最长等 30s）
    final = await wait_task_terminal(client, auth["headers"], task["id"], timeout=30.0)
    assert final["status"] == "done", final
    assert final["progress"] == 100

    # 计费闭环：5000 - 10 = 4990，冻结清零
    wallet = await get_wallet_model(db, auth["user_id"])
    assert wallet.points_balance == 4990
    assert wallet.frozen_points == 0

    # 分镜关键帧回写
    sb2 = (await db.execute(select(Storyboard).where(Storyboard.id == sb.id))).scalar_one()
    await db.refresh(sb2)
    assert sb2.keyframe_url == final["result_url"]
    assert sb2.status == "done"

    # 媒体素材有记录
    media = (
        await db.execute(
            select(MediaAsset).where(
                MediaAsset.project_id == pid, MediaAsset.media_type == "image"
            )
        )
    ).scalars().all()
    assert len(media) >= 1


async def test_tong_fenjing_tong_jieduan_fangchong_409(client, auth, db):
    """同分镜同阶段已有进行中任务时重复提交返回 409。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 409 时中断。
    """
    pid, sb = await _make_project_with_storyboard(client, auth, db, unique_suffix())

    first = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial"},
    )
    assert first.status_code == 200

    # 紧随其后的第二次提交：首个任务仍在排队/进行中 → 409
    second = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial"},
    )
    assert second.status_code == 409, second.text
    assert "进行中" in second.json()["detail"] or "取消" in second.json()["detail"]

    # 清理：等首个任务结束（避免影响其他用例的数据窗口）
    await wait_task_terminal(client, auth["headers"], first.json()["tasks"][0]["id"])


async def test_weigui_cihui_renwu_shibai_tuikuan(client, auth, db):
    """违规词提示词：任务 failed（content_violation）+ 审核记录 violate + 退款。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _make_project_with_storyboard(client, auth, db, unique_suffix())

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial", "prompt_editable": "爆发枪击冲突的紧张画面"},
    )
    assert resp.status_code == 200, resp.text
    task = resp.json()["tasks"][0]

    final = await wait_task_terminal(client, auth["headers"], task["id"], timeout=30.0)
    assert final["status"] == "failed", final
    assert final["error_code"] == "content_violation"

    # 审核记录：gate=generate、result=violate
    records = (
        await db.execute(
            select(ReviewRecord).where(
                ReviewRecord.project_id == pid, ReviewRecord.gate == "generate"
            )
        )
    ).scalars().all()
    assert any(r.result == "violate" for r in records)

    # 退款闭环：冻结清零、余额恢复 5000
    wallet = await get_wallet_model(db, auth["user_id"])
    assert wallet.frozen_points == 0
    assert wallet.points_balance == 5000


async def test_quxiao_renwu_tuikuan(client, auth, db):
    """取消排队中的任务：最终 canceled 且冻结积分退回。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _make_project_with_storyboard(client, auth, db, unique_suffix())

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial"},
    )
    assert resp.status_code == 200
    task = resp.json()["tasks"][0]

    cancel = await client.post(f"/api/tasks/{task['id']}/cancel", headers=auth["headers"])
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] in ("canceled", "canceling")

    # 无论走了 queued 直取还是 running 协作取消，最终都应 canceled + 退款
    final = await wait_task_terminal(client, auth["headers"], task["id"], timeout=30.0)
    assert final["status"] == "canceled", final

    wallet = await get_wallet_model(db, auth["user_id"])
    assert wallet.frozen_points == 0
    assert wallet.points_balance == 5000
