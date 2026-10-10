"""审核闸门真实化测试模块（文件级说明）。

覆盖 Task 4.2 闸门二/三判定矩阵（审核记录以 ORM 直插，绕过生成链路）：
- 闸门二（compose）：存在未放行 violate 记录 → 400 拦截；人工 approve 后放行；
- 闸门二：存在待人工 suspect 记录（manual_status 置空）→ 400 拦截；approve 后放行；
- 闸门三（publish）：存在未放行 violate 记录 → 400 拦截；approve 后发布成功；
- 闸门通过时落 compose/publish 通过记录（method=gate）。
"""

import pytest
from sqlalchemy import select

from app.models import FilmOutput, ReviewRecord
from tests.conftest import (
    create_project,
    insert_storyboard,
    unique_suffix,
    wait_task_terminal,
)


async def _project_with_storyboard(client, auth, db, tag: str):
    """辅助：建项目并直插一条带 video_url 的分镜（内部函数）。

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
    project = await create_project(client, auth["headers"], title=f"闸门项目_{tag}")
    sb = await insert_storyboard(
        db, project["id"], sequence=1,
        video_url="/storage/generated/fake_gate_clip.mp4",
    )
    return project["id"], sb


async def _insert_record(db, project_id: int, sb_id: int, **overrides):
    """辅助：直插一条审核记录（内部函数）。

    Args:
        db: 测试库会话。
        project_id: 项目 ID。
        sb_id: 关联分镜 ID（target_id）。
        **overrides: gate/target_type/result/manual_status 覆盖项。

    Returns:
        ReviewRecord: 已落库的审核记录。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    record = ReviewRecord(
        project_id=project_id,
        gate=overrides.get("gate", "generate"),
        target_type=overrides.get("target_type", "image"),
        target_id=sb_id,
        result=overrides.get("result", "violate"),
        violation_type="banned_word",
        suggestion=overrides.get("suggestion", "提示词命中严禁词，请修改后重新提交"),
        manual_status=overrides.get("manual_status"),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def test_zhamen2_weigui_lanjie_fangxing(client, auth, db, admin_auth):
    """闸门二：未放行 violate 记录拦截合成；人工 approve 后放行。

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
    record = await _insert_record(db, pid, sb.id, result="violate")

    resp = await client.post(
        f"/api/projects/{pid}/compose",
        headers=auth["headers"], json={"resolution": "720P", "mode": "full"},
    )
    assert resp.status_code == 400, resp.text
    assert "违规" in resp.json()["detail"]

    # 管理端人工复核放行 → 合成放行，且落 compose 通过记录
    handle = await client.post(
        f"/api/admin/review/{record.id}/handle",
        headers=admin_auth["headers"], json={"action": "approve"},
    )
    assert handle.status_code == 200, handle.text

    ok = await client.post(
        f"/api/projects/{pid}/compose",
        headers=auth["headers"], json={"resolution": "720P", "mode": "full"},
    )
    assert ok.status_code == 200, ok.text
    records = (
        await db.execute(
            select(ReviewRecord).where(
                ReviewRecord.project_id == pid, ReviewRecord.gate == "compose"
            )
        )
    ).scalars().all()
    assert any(r.result == "pass" and r.method == "gate" for r in records)


async def test_zhamen2_kexi_dairen_shenhe(client, auth, db, admin_auth):
    """闸门二：待人工 suspect 记录（manual_status 置空）拦截合成；approve 后放行。

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
    record = await _insert_record(db, pid, sb.id, result="suspect", manual_status=None)

    resp = await client.post(
        f"/api/projects/{pid}/compose",
        headers=auth["headers"], json={"resolution": "720P", "mode": "full"},
    )
    assert resp.status_code == 400, resp.text
    assert "人工" in resp.json()["detail"]

    handle = await client.post(
        f"/api/admin/review/{record.id}/handle",
        headers=admin_auth["headers"], json={"action": "approve"},
    )
    assert handle.status_code == 200, handle.text

    ok = await client.post(
        f"/api/projects/{pid}/compose",
        headers=auth["headers"], json={"resolution": "720P", "mode": "full"},
    )
    assert ok.status_code == 200, ok.text


async def test_zhamen3_fabu_lanjie_fangxing(client, auth, db, admin_auth):
    """闸门三：未放行 violate 记录拦截发布；人工 approve 后发布成功。

    前置：合成先通过（无 generate 违规记录）产生成片版本，再插入一条
    compose 闸门违规记录验证闸门三汇总"全部闸门"的口径。

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

    # 合成通过（闸门二无违规记录）→ 成片版本产生
    compose = await client.post(
        f"/api/projects/{pid}/compose",
        headers=auth["headers"], json={"resolution": "720P", "mode": "full"},
    )
    assert compose.status_code == 200, compose.text
    final = await wait_task_terminal(client, auth["headers"], compose.json()["id"], timeout=30.0)
    assert final["status"] == "done", final
    film = (
        await db.execute(select(FilmOutput).where(FilmOutput.project_id == pid))
    ).scalar_one()

    # 插入一条未放行的 compose 闸门违规记录 → 发布被闸门三拦截
    record = await _insert_record(db, pid, sb.id, gate="compose", target_type="film", result="violate")
    publish = await client.post(
        f"/api/projects/{pid}/publish",
        headers=auth["headers"], json={"output_id": film.id, "share_expires_days": 7},
    )
    assert publish.status_code == 400, publish.text
    assert "违规" in publish.json()["detail"]

    # 人工放行 → 发布成功并返回分享路径
    handle = await client.post(
        f"/api/admin/review/{record.id}/handle",
        headers=admin_auth["headers"], json={"action": "approve"},
    )
    assert handle.status_code == 200, handle.text

    ok = await client.post(
        f"/api/projects/{pid}/publish",
        headers=auth["headers"], json={"output_id": film.id, "share_expires_days": 7},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["share_url"].startswith("/share/")

    # 闸门三通过记录已落库
    gate_records = (
        await db.execute(
            select(ReviewRecord).where(
                ReviewRecord.project_id == pid, ReviewRecord.gate == "publish"
            )
        )
    ).scalars().all()
    assert any(r.result == "pass" and r.method == "gate" for r in gate_records)
