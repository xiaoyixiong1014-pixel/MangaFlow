"""生成结果上传替换测试模块（文件级说明，P-06）。

覆盖 POST /api/tasks/{task_id}/replace 链路（依赖进程内 mock 供应商执行器）：
- 归属校验：他人任务 403（沿用既有 _get_owned_task 归属模式：存在但不归你
  返回 403，任务不存在返回 404）；
- 非法替换：非 image 任务 400 / 任务未完成（queued）400 / 文件扩展名非法 400；
- 正常替换：done 关键帧任务上传 png 后 result_url 更新为 /storage/replaced/
  新地址、meta.replaced_from 留痕原地址、storyboard.keyframe_url 联动回写。
"""

import uuid

import pytest
from sqlalchemy import select

from app.models import Storyboard, Task
from tests.conftest import (
    create_project,
    insert_storyboard,
    register_user,
    unique_suffix,
    wait_task_terminal,
)

# 最小 PNG 字节（仅用于扩展名/大小校验，接口不校验图片内容）
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64


async def _insert_failed_image_task(db, project_id: int, storyboard_id: int) -> Task:
    """直插一条 failed 状态的图像任务（非法状态用例，绕开执行器竞态）。

    Args:
        db: 测试库会话。
        project_id: 归属项目 ID。
        storyboard_id: 关联分镜 ID。

    Returns:
        Task: 已落库的 failed 图像任务。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    task = Task(
        project_id=project_id,
        storyboard_id=storyboard_id,
        type="image",
        phase="trial",
        status="failed",
        cost_points=10,
        idempotency_key=f"replace_test_{uuid.uuid4().hex[:12]}",
        meta={},
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def _make_done_keyframe_task(client, auth, db):
    """辅助：建项目 + 分镜 + 打样关键帧，等待任务完成（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        tuple[int, Storyboard, dict]: (项目 ID, 分镜 ORM 对象, 终态任务 JSON)。

    Raises:
        AssertionError: 关键帧任务未在超时内到达 done 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"替换项目_{unique_suffix()}")
    sb = await insert_storyboard(db, project["id"], sequence=1)
    resp = await client.post(
        f"/api/projects/{project['id']}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial"},
    )
    assert resp.status_code == 200, resp.text
    task = resp.json()["tasks"][0]
    final = await wait_task_terminal(client, auth["headers"], task["id"], timeout=30.0)
    assert final["status"] == "done", final
    return project["id"], sb, final


@pytest.mark.asyncio
async def test_replace_bu_cunzai_404_yuequan_403(client, auth, db):
    """归属校验：不存在的任务 404；他人任务（归属越权）403。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文（任务归属者）。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码与预期不符时中断。
    """
    _pid, _sb, final = await _make_done_keyframe_task(client, auth, db)

    # 任务不存在：404
    resp = await client.post(
        "/api/tasks/999999/replace",
        headers=auth["headers"],
        files={"file": ("new.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 404, resp.text

    # 他人任务：403（既有归属模式：任务存在但不属于当前用户）
    other = await register_user(client, prefix="intruder")
    resp = await client.post(
        f"/api/tasks/{final['id']}/replace",
        headers={"Authorization": f"Bearer {other['access_token']}"},
        files={"file": ("new.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_replace_feifa_leixing_zhuangtai_400(client, auth, db):
    """非法替换：非 image 任务 / 任务未完成 / 文件扩展名非法，均返回 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码与预期不符时中断。
    """
    project = await create_project(client, auth["headers"], title=f"替换非法_{unique_suffix()}")
    sb = await insert_storyboard(db, project["id"], sequence=1)

    # 非法类型：TTS 任务不支持替换（分镜有默认对白，任务入队即校验类型）
    tts_resp = await client.post(
        f"/api/projects/{project['id']}/storyboards/{sb.id}/tts",
        headers=auth["headers"],
        json={"voice": "female_01"},
    )
    assert tts_resp.status_code == 200, tts_resp.text
    resp = await client.post(
        f"/api/tasks/{tts_resp.json()['id']}/replace",
        headers=auth["headers"],
        files={"file": ("new.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 400, resp.text
    assert "图像" in resp.json()["detail"]

    # 非法状态：failed（非 done）的图像任务不可替换（直插行绕开执行器竞态）
    failed_task = await _insert_failed_image_task(db, project["id"], sb.id)
    resp = await client.post(
        f"/api/tasks/{failed_task.id}/replace",
        headers=auth["headers"],
        files={"file": ("new.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 400, resp.text
    assert "尚未完成" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_replace_wenjian_geshi_feifa_400(client, auth, db):
    """文件校验：done 关键帧任务上传非法扩展名（gif）返回 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码与预期不符时中断。
    """
    _pid, _sb, final = await _make_done_keyframe_task(client, auth, db)
    resp = await client.post(
        f"/api/tasks/{final['id']}/replace",
        headers=auth["headers"],
        files={"file": ("anim.gif", PNG_BYTES, "image/gif")},
    )
    assert resp.status_code == 400, resp.text
    assert "png" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_replace_chenggong_huixie_liudong(client, auth, db):
    """正常替换：result_url 更新 + meta.replaced_from 留痕 + 分镜关键帧联动。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    _pid, sb, final = await _make_done_keyframe_task(client, auth, db)
    old_url = final["result_url"]
    assert old_url, "关键帧任务完成后应有结果地址"

    resp = await client.post(
        f"/api/tasks/{final['id']}/replace",
        headers=auth["headers"],
        files={"file": (f"replaced_{uuid.uuid4().hex[:6]}.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 任务结果地址更新为 replaced 目录新文件
    assert body["result_url"], body
    assert body["result_url"].startswith("/storage/replaced/"), body
    assert body["result_url"] != old_url

    # meta 留痕：直查任务行校验 replaced_from / replaced_at / replaced_by_upload
    task_row = (
        await db.execute(select(Task).where(Task.id == final["id"]))
    ).scalar_one()
    await db.refresh(task_row)
    assert task_row.meta.get("replaced_from") == old_url, task_row.meta
    assert task_row.meta.get("replaced_at"), task_row.meta
    assert task_row.meta.get("replaced_by_upload") is True, task_row.meta

    # 分镜关键帧联动：keyframe_url 同步为新地址（前端缩略图取同字段）
    sb2 = (await db.execute(select(Storyboard).where(Storyboard.id == sb.id))).scalar_one()
    await db.refresh(sb2)
    assert sb2.keyframe_url == body["result_url"], sb2.keyframe_url
    assert sb2.status == "done"
