"""视频参数配置与尾帧链路测试模块（文件级说明，P-12 / P-03 / P-08）。

覆盖：
- VideoCreateRequest 校验：默认值（720p / 24 / medium）与非法取值 422；
- create_video_task：meta.video_params 落库（resolution/fps/motion_strength）、
  meta.first_frame 记录关键帧地址；
- use_end_frame=true 但分镜无尾帧时 400（提示先生成尾帧）；
- 尾帧生成任务：无关键帧 400、创建成功（meta.end_frame 标记）、执行器完成后
  回写 storyboards.end_frame_url（真实 mock 供应商链路）。
"""

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models import Storyboard, Task
from app.schemas.generation import VideoCreateRequest
from tests.conftest import (
    create_project,
    insert_storyboard,
    unique_suffix,
    wait_task_terminal,
)


def test_video_schema_defaults():
    """VideoCreateRequest 默认值：720p / 24 / medium / 不使用尾帧。

    Returns:
        None。

    Raises:
        AssertionError: 任一字段默认值与预期不符时中断。
    """
    req = VideoCreateRequest()
    assert req.resolution == "720p"
    assert req.fps == 24
    assert req.motion_strength == "medium"
    assert req.use_end_frame is False


@pytest.mark.parametrize(
    "field, value",
    [
        ("resolution", "360p"),
        ("resolution", "4k"),
        ("fps", 60),
        ("fps", 12),
        ("motion_strength", "extreme"),
    ],
)
def test_video_schema_invalid_values_422(field, value):
    """VideoCreateRequest 非法取值触发 pydantic 校验失败（API 层映射 422）。

    Args:
        field: 被注入非法值的字段名。
        value: 非法值。

    Returns:
        None。

    Raises:
        AssertionError: 非法值未触发 ValidationError 时中断。
    """
    with pytest.raises(ValidationError):
        VideoCreateRequest(**{field: value})


async def _make_storyboard_with_keyframe(db, project_id: int, sequence: int = 1):
    """建分镜并直填关键帧地址（绕开关键帧生成链路，聚焦视频/尾帧校验）。

    Args:
        db: 测试库会话。
        project_id: 归属项目 ID。
        sequence: 镜头序号。

    Returns:
        Storyboard: 已落库且 keyframe_url 已回填的分镜 ORM 对象。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    sb = await insert_storyboard(db, project_id, sequence=sequence)
    sb.keyframe_url = "/storage/generated/keyframe_test.png"
    sb.status = "done"
    await db.commit()
    await db.refresh(sb)
    return sb


@pytest.mark.asyncio
async def test_create_video_task_meta_video_params(client, auth, db):
    """创建视频任务：meta.video_params 落库 + meta.first_frame 记录关键帧地址。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: meta 字段与请求参数不一致时中断。
    """
    project = await create_project(client, auth["headers"], title=f"视频参数_{unique_suffix()}")
    sb = await _make_storyboard_with_keyframe(db, project["id"])
    resp = await client.post(
        f"/api/projects/{project['id']}/storyboards/{sb.id}/video",
        headers=auth["headers"],
        json={"resolution": "1080p", "fps": 30, "motion_strength": "high"},
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]

    # 直查任务行校验 meta 落库（video_params 在执行器合并 meta 后仍保留）
    row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
    vp = (row.meta or {}).get("video_params")
    assert vp == {"resolution": "1080p", "fps": 30, "motion_strength": "high"}, row.meta
    assert (row.meta or {}).get("first_frame") == "/storage/generated/keyframe_test.png"
    assert (row.meta or {}).get("use_end_frame") is False


@pytest.mark.asyncio
async def test_use_end_frame_weishengcheng_weijia_400(client, auth, db):
    """use_end_frame=true 但分镜尾帧未生成：创建视频任务返回 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码或错误信息与预期不符时中断。
    """
    project = await create_project(client, auth["headers"], title=f"尾帧校验_{unique_suffix()}")
    sb = await _make_storyboard_with_keyframe(db, project["id"])
    resp = await client.post(
        f"/api/projects/{project['id']}/storyboards/{sb.id}/video",
        headers=auth["headers"],
        json={"use_end_frame": True},
    )
    assert resp.status_code == 400, resp.text
    assert "尾帧" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_endframe_wu_guanjianzhen_400(client, auth, db):
    """分镜尚无关键帧时创建尾帧任务返回 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码或错误信息与预期不符时中断。
    """
    project = await create_project(client, auth["headers"], title=f"尾帧无帧_{unique_suffix()}")
    sb = await insert_storyboard(db, project["id"], sequence=1)
    resp = await client.post(
        f"/api/projects/{project['id']}/storyboards/{sb.id}/end-frame",
        headers=auth["headers"],
    )
    assert resp.status_code == 400, resp.text
    assert "关键帧" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_endframe_chuangjian_yu_huixie(client, auth, db):
    """尾帧任务创建与回写：创建（meta.end_frame 标记）→ 执行器完成后
    回写 storyboards.end_frame_url。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任务创建/回写任一环节失败时中断。
    """
    project = await create_project(client, auth["headers"], title=f"尾帧回写_{unique_suffix()}")
    # 直填关键帧（绕开关键帧生成链路，聚焦尾帧）
    sb = await _make_storyboard_with_keyframe(db, project["id"])
    resp = await client.post(
        f"/api/projects/{project['id']}/storyboards/{sb.id}/end-frame",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    task = resp.json()
    assert task["type"] == "image"
    assert task["storyboard_id"] == sb.id

    # meta 标记：end_frame=True + target_type=storyboard_end_frame
    row = (await db.execute(select(Task).where(Task.id == task["id"]))).scalar_one()
    assert (row.meta or {}).get("end_frame") is True, row.meta
    assert (row.meta or {}).get("target_type") == "storyboard_end_frame", row.meta

    # 执行器（mock 图像供应商）完成后回写分镜尾帧地址
    final = await wait_task_terminal(client, auth["headers"], task["id"], timeout=30.0)
    assert final["status"] == "done", final
    sb2 = (await db.execute(select(Storyboard).where(Storyboard.id == sb.id))).scalar_one()
    await db.refresh(sb2)
    assert sb2.end_frame_url == final["result_url"], sb2.end_frame_url
    assert sb2.end_frame_url, "尾帧任务完成后 end_frame_url 不应为空"
