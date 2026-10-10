"""合成增强测试模块（文件级说明）。

覆盖 Task 5 合成增强（ffmpeg 相关验证以参数/查询逻辑为主，真实转码产物
不在断言范围——测试片段为占位地址，合成走降级路径）：
- build_srt：字幕时间轴（按实际时长累计）与文本（对白优先/画面描述回退/
  空分镜跳过）正确性；
- drawtext 水印滤镜构造（有字体时含水印文本；无字体环境降级 None）；
- 增量合成：storyboard_ids 过滤进任务 meta（保持镜头序）与非法清单 400；
- 合成增强参数（subtitles/subtitle_style/bgm/transition）透传任务 meta，
  且闸门二通过记录落库。
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import ReviewRecord, Task
from app.services.compose_service import build_drawtext_filter, build_srt
from tests.conftest import (
    create_project,
    insert_storyboard,
    unique_suffix,
    wait_task_terminal,
)


def test_build_srt_shijianzhou_yu_wenben():
    """build_srt：时间轴按 actual_duration/duration 累计；文本对白优先、描述回退、空跳过。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    storyboards = [
        # 第 1 镜：预估 5s，有对白 → 00:00:00.000 ~ 00:00:05.000
        SimpleNamespace(actual_duration=None, duration=5, dialogue="林小满：你好", visual_description="画面A"),
        # 第 2 镜：实际 2.5s，无对白回退画面描述 → 00:00:05.000 ~ 00:00:07.500
        SimpleNamespace(actual_duration=2.5, duration=3, dialogue="", visual_description="旁白画面"),
        # 第 3 镜：无任何文本 → 不产生字幕条目
        SimpleNamespace(actual_duration=None, duration=4, dialogue=None, visual_description=""),
    ]
    srt = build_srt(storyboards)
    assert srt is not None
    assert "00:00:00,000 --> 00:00:05,000" in srt
    assert "00:00:05,000 --> 00:00:07,500" in srt
    assert "林小满：你好" in srt
    assert "旁白画面" in srt
    # 仅 2 条字幕条目（第 3 镜跳过），SRT 序号正确
    assert srt.count("-->") == 2
    assert "\n2\n00:00:05,000" in srt


def test_build_srt_quanbu_weikong_fanhui_none():
    """build_srt：全部分镜无文本时返回 None（不产出空字幕文件）。

    Returns:
        None。

    Raises:
        AssertionError: 返回值非 None 时中断。
    """
    storyboards = [SimpleNamespace(actual_duration=None, duration=5, dialogue="", visual_description="")]
    assert build_srt(storyboards) is None


def test_drawtext_shuiyin_gouzao():
    """drawtext 水印滤镜：有字体时含水印文本与低透明度参数；无字体降级 None。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from pathlib import Path

    from app.services.compose_service import _find_font_file

    filter_expr = build_drawtext_filter("abcd1234")
    if _find_font_file() is None:
        assert filter_expr is None
    else:
        assert filter_expr is not None
        assert "drawtext=" in filter_expr
        assert "abcd1234" in filter_expr
        assert "white@0.15" in filter_expr


async def _project_with_storyboards(client, auth, db, tag: str, count: int = 3):
    """辅助：建项目并直插 count 条带 video_url 的分镜（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        tag: 标题防冲突后缀。
        count: 分镜数量。

    Returns:
        Tuple[int, List[Storyboard]]: (项目 ID, 分镜列表)。

    Raises:
        AssertionError: 项目创建返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"合成项目_{tag}")
    sbs = []
    for seq in range(1, count + 1):
        sbs.append(await insert_storyboard(
            db, project["id"], sequence=seq, duration=5 + seq,
            video_url=f"/storage/generated/fake_compose_{seq}.mp4",
        ))
    return project["id"], sbs


async def test_zengliang_hecheng_storyboard_ids_guolv(client, auth, db):
    """增量合成：storyboard_ids 过滤进任务 meta（保持镜头序），任务正常完成。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sbs = await _project_with_storyboards(client, auth, db, unique_suffix())

    resp = await client.post(
        f"/api/projects/{pid}/compose",
        headers=auth["headers"],
        json={
            "resolution": "720P", "mode": "incremental",
            "storyboard_ids": [sbs[0].id, sbs[2].id],
        },
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]
    final = await wait_task_terminal(client, auth["headers"], task_id, timeout=30.0)
    assert final["status"] == "done", final

    task = await db.get(Task, task_id)
    assert task.meta["mode"] == "incremental"
    # 仅保留指定分镜且按镜头序排列（跳过第 2 镜）
    assert task.meta["storyboard_ids"] == [sbs[0].id, sbs[2].id]


async def test_zengliang_feifa_qingdan_400(client, auth, db):
    """增量合成：storyboard_ids 含不属于项目的分镜 → 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 400 时中断。
    """
    pid, sbs = await _project_with_storyboards(client, auth, db, unique_suffix())
    resp = await client.post(
        f"/api/projects/{pid}/compose",
        headers=auth["headers"],
        json={"mode": "incremental", "storyboard_ids": [sbs[0].id, 999999]},
    )
    assert resp.status_code == 400, resp.text
    assert "不属于" in resp.json()["detail"]


async def test_hecheng_canshu_touchuan_yu_zhamen_jilu(client, auth, db):
    """合成增强参数透传任务 meta；闸门二通过记录（method=gate）落库。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sbs = await _project_with_storyboards(client, auth, db, unique_suffix(), count=2)

    resp = await client.post(
        f"/api/projects/{pid}/compose",
        headers=auth["headers"],
        json={
            "resolution": "1080P", "mode": "full",
            "subtitles": True,
            "subtitle_style": {"font_size": 32, "color": "&H00201080", "position": "top"},
            "bgm": {"url": "/storage/bgm/theme.mp3", "volume": 0.4},
            "transition": {"type": "smoothleft", "duration": 0.8},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cost_points"] == 200
    task_id = resp.json()["id"]

    task = await db.get(Task, task_id)
    assert task.meta["subtitles"] is True
    assert task.meta["subtitle_style"] == {"font_size": 32, "color": "&H00201080", "position": "top"}
    assert task.meta["bgm"] == {"url": "/storage/bgm/theme.mp3", "volume": 0.4}
    assert task.meta["transition"] == {"type": "smoothleft", "duration": 0.8}
    # 全量模式：meta 携带项目全部分镜（2 条）
    assert task.meta["storyboard_ids"] == [sbs[0].id, sbs[1].id]

    final = await wait_task_terminal(client, auth["headers"], task_id, timeout=30.0)
    assert final["status"] == "done", final

    # 闸门二通过记录（占位片段走降级合成，不产生转码断言）
    records = (
        await db.execute(
            select(ReviewRecord).where(
                ReviewRecord.project_id == pid, ReviewRecord.gate == "compose"
            )
        )
    ).scalars().all()
    assert any(r.result == "pass" and r.method == "gate" for r in records)
