"""发布链路接口测试模块（文件级说明）。

覆盖链路（分镜以 ORM 直插，绕过 LLM 拆解）：
- 成片合成（compose）：任务 done 后 film_outputs 产生 is_current=True 版本
- 成片版本列表可查
- 发布（publish）：share_token 落库、返回 /share/{token} 路径
- GET /api/share/{token} 免认证访问 200（含项目标题/视频地址）
- 伪造 token 404
"""

import pytest
from sqlalchemy import select

from app.models import FilmOutput
from tests.conftest import (
    create_project,
    insert_storyboard,
    unique_suffix,
    wait_task_terminal,
)


async def _make_project_with_video_clip(client, auth, db, tag: str) -> int:
    """辅助：建项目并直插一条带 video_url 的分镜（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        tag: 标题防冲突后缀。

    Returns:
        int: 项目 ID。

    Raises:
        AssertionError: 接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"发布项目_{tag}")
    await insert_storyboard(
        db,
        project["id"],
        sequence=1,
        duration=6,
        visual_description="夕阳下的天台",
        dialogue="林小满：再见啦！",
        video_url="/storage/generated/fake_clip_test.mp4",  # 占位片段（文件可不存在，合成走降级）
    )
    return project["id"]


async def test_hecheng_wancheng_chansheng_dangqian_banben(client, auth, db):
    """成片合成：任务 done 后 film_outputs 有 is_current=True 版本记录。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid = await _make_project_with_video_clip(client, auth, db, unique_suffix())

    resp = await client.post(
        f"/api/projects/{pid}/compose",
        headers=auth["headers"],
        json={"resolution": "1080P", "mode": "full"},
    )
    assert resp.status_code == 200, resp.text
    task = resp.json()
    assert task["type"] == "compose"
    assert task["cost_points"] == 200

    final = await wait_task_terminal(client, auth["headers"], task["id"], timeout=30.0)
    assert final["status"] == "done", final
    assert final["result_url"]

    films = (
        await db.execute(select(FilmOutput).where(FilmOutput.project_id == pid))
    ).scalars().all()
    assert len(films) == 1
    assert films[0].is_current is True
    assert films[0].version_no == 1
    assert films[0].watermark_id

    # 成片版本列表可查（含当前版本标记）
    outputs = await client.get(f"/api/projects/{pid}/outputs", headers=auth["headers"])
    assert outputs.status_code == 200
    assert outputs.json()["total"] == 1
    assert outputs.json()["items"][0]["is_current"] is True

    # 计费：5000 - 200 = 4800
    from tests.conftest import get_wallet_model

    wallet = await get_wallet_model(db, auth["user_id"])
    assert wallet.points_balance == 4800
    assert wallet.frozen_points == 0


async def test_fabu_shengcheng_fenxiang_token(client, auth, db):
    """发布成片：share_token 落库且返回 /share/{token} 分享路径。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid = await _make_project_with_video_clip(client, auth, db, unique_suffix())

    compose = await client.post(
        f"/api/projects/{pid}/compose",
        headers=auth["headers"],
        json={"resolution": "720P", "mode": "full"},
    )
    assert compose.status_code == 200
    task_id = compose.json()["id"]
    final = await wait_task_terminal(client, auth["headers"], task_id, timeout=30.0)
    assert final["status"] == "done"

    film = (
        await db.execute(select(FilmOutput).where(FilmOutput.project_id == pid))
    ).scalar_one()

    publish = await client.post(
        f"/api/projects/{pid}/publish",
        headers=auth["headers"],
        json={"output_id": film.id, "share_expires_days": 7},
    )
    assert publish.status_code == 200, publish.text
    body = publish.json()
    assert body["share_url"].startswith("/share/")
    token = body["share_url"].rsplit("/", 1)[-1]

    await db.refresh(film)
    assert film.share_token == token
    assert film.share_revoked is False


async def test_fenxiang_ye_mian_renzhi_fangwen(client, auth, db):
    """分享页免认证访问：GET /api/share/{token} 无令牌 200 且数据正确。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid = await _make_project_with_video_clip(client, auth, db, unique_suffix())
    compose = await client.post(
        f"/api/projects/{pid}/compose",
        headers=auth["headers"],
        json={"resolution": "720P", "mode": "full"},
    )
    task_id = compose.json()["id"]
    await wait_task_terminal(client, auth["headers"], task_id, timeout=30.0)
    film = (
        await db.execute(select(FilmOutput).where(FilmOutput.project_id == pid))
    ).scalar_one()

    publish = await client.post(
        f"/api/projects/{pid}/publish",
        headers=auth["headers"],
        json={"output_id": film.id},
    )
    share_url = publish.json()["share_url"]
    # share_url 为前端页面路径 /share/{token}；后端数据接口为 /api/share/{token}
    token = share_url.rsplit("/", 1)[-1]

    # 不带任何认证头访问公开分享数据接口
    resp = await client.get(f"/api/share/{token}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"].startswith("发布项目_")
    assert body["video_url"] == film.url
    assert body["resolution"] == "720P"

    # 伪造 token → 404
    fake = await client.get("/api/share/not-a-real-share-token")
    assert fake.status_code == 404
