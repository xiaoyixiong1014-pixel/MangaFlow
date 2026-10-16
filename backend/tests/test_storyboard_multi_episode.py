# -*- coding: utf-8 -*-
"""下游分镜接收剧本 · 多剧集 Script 行回归测试。

背景（2026-10-15 剧本创作模块全链路验证，方案A修复回归）：
分阶段流水线（故事设定 → 分集大纲 → 剧本正文）按剧集落库 Script 行
（scripts 表唯一约束 uk_scripts_project = (project_id, episode_id)，
正文保存路径 api/script_pipeline.py 每集新建一行）。

修复前 storyboard_service.decompose 以
``select(Script).where(Script.project_id == project.id)`` +
``scalar_one_or_none()`` 取剧本 —— 项目内 Script 行数 > 1 时抛
MultipleResultsFound（500）。修复后按「默认行排最前 + 剧集 sequence 升序」
合并全部剧本行逐场景拆解，每镜 episode_id 随来源剧本行落库。

本文件两个用例分别覆盖：
- 用例 A：默认剧本行（episode_id NULL）+ 1 个剧集剧本行（小说输入 + 正文生成的典型状态）
- 用例 B：仅 2 个剧集剧本行、无默认行（纯流水线多集状态），并断言
  每镜 script_id / episode_id 正确随来源行落库、sequence 全局连续

LLM 打桩：monkeypatch storyboard_service._llm_shots 返回固定 1 镜，
保证完全确定性、不触达外部渠道。
"""

import pytest
from sqlalchemy import select

from app.models import Episode, Script
from app.services import storyboard_service
from tests.conftest import create_project, register_user, unique_suffix

# 与生产 wrap 后 content 同构的场景数据（unwrap_scenes 可解包）
_SCENES = [
    {
        "scene_no": 1,
        "title": "天台立约",
        "plot": "两人立下共同追梦的约定",
        "dialogue": [{"character": "林小满", "line": "一起拿金奖吧。"}],
        "duration_sec": 30,
    },
]


def _whole_wrapped_content() -> dict:
    """构造整体包装结构（PUT body 手动保存的落库形态，2026-10-15 修复点之一）。"""
    return {
        "value": {
            "title": "第一集",
            "scenes": _SCENES,
        },
        "source": "user",
    }


def _field_wrapped_content() -> dict:
    """构造字段级包装结构（正文 SSE 生成 wrap_content_fields 的落库形态）。"""
    return {
        "title": {"value": "第一集", "source": "ai"},
        "scenes": {"value": [dict(s, source="ai") for s in _SCENES], "source": "ai"},
    }


async def _seed_episode_script(db, project_id: int, sequence: int, content: dict) -> Episode:
    """ORM 直插 1 个剧集 + 1 个该集 Script 行（内部函数）。"""
    ep = Episode(project_id=project_id, title=f"第{sequence}集", sequence=sequence)
    db.add(ep)
    await db.flush()
    db.add(Script(
        project_id=project_id,
        episode_id=ep.id,
        content=content,
        raw_text="天台立约。",
        version=1,
    ))
    await db.commit()
    return ep


async def _count_project_scripts(db, project_id: int) -> int:
    """统计项目 Script 行数（内部函数）。"""
    rows = (
        await db.execute(select(Script).where(Script.project_id == project_id))
    ).scalars().all()
    return len(rows)


@pytest.fixture(autouse=True)
def _stub_llm_shots(monkeypatch):
    """打桩 _llm_shots：每场景固定返回 1 镜，不触达真实 LLM。"""

    async def _fake_llm_shots(scene):
        return [{
            "shot_type": "中景",
            "camera_movement": "固定",
            "duration": 5,
            "visual_description": str(scene.get("plot") or "（无）"),
            "dialogue": [],
        }]

    monkeypatch.setattr(storyboard_service, "_llm_shots", _fake_llm_shots)


async def test_decompose_default_row_plus_one_episode_row(client, auth, db):
    """用例 A：默认剧本行 + 1 个剧集剧本行 → 拆解是否可用。"""
    tag = unique_suffix()
    project = await create_project(client, auth["headers"], title=f"下游复现A_{tag}")
    pid = project["id"]

    # 小说输入建立默认剧本行（episode_id NULL）
    resp = await client.put(
        f"/api/projects/{pid}/script/input",
        headers=auth["headers"],
        json={"mode": "novel", "raw_text": "示例原文。"},
    )
    assert resp.status_code == 200, resp.text

    # 1 个剧集正文 Script 行（PUT body 手动保存同构：整体包装形态）
    ep1 = await _seed_episode_script(db, pid, sequence=1, content=_whole_wrapped_content())

    assert await _count_project_scripts(db, pid) == 2, "前置：项目应恰有 2 行 Script"

    decompose = await client.post(
        f"/api/projects/{pid}/storyboards/decompose", headers=auth["headers"]
    )
    assert decompose.status_code == 200, (
        f"下游拆解失败：HTTP {decompose.status_code} body={decompose.text[:300]}"
    )
    assert decompose.json()["total"] >= 1


async def test_decompose_two_episode_rows_only(client, auth, db):
    """用例 B：仅 2 个剧集剧本行（纯多集流水线状态）→ 拆解是否可用。"""
    tag = unique_suffix()
    project = await create_project(client, auth["headers"], title=f"下游复现B_{tag}")
    pid = project["id"]

    ep1 = await _seed_episode_script(db, pid, sequence=1, content=_field_wrapped_content())
    ep2 = await _seed_episode_script(db, pid, sequence=2, content=_field_wrapped_content())

    assert await _count_project_scripts(db, pid) == 2, "前置：项目应恰有 2 行 Script"

    decompose = await client.post(
        f"/api/projects/{pid}/storyboards/decompose", headers=auth["headers"]
    )
    assert decompose.status_code == 200, (
        f"下游拆解失败：HTTP {decompose.status_code} body={decompose.text[:300]}"
    )
    assert decompose.json()["total"] >= 1

    # 每镜来源落库正确：script_id / episode_id 随来源剧本行，sequence 全局连续
    listing = await client.get(f"/api/projects/{pid}/storyboards", headers=auth["headers"])
    assert listing.status_code == 200
    items = listing.json()
    assert len(items) == decompose.json()["total"]
    assert [i["sequence"] for i in items] == list(range(1, len(items) + 1))
    ep_ids = {i["episode_id"] for i in items}
    assert ep_ids == {ep1.id, ep2.id}, f"分镜应分属两集，实际 episode_id 集合={ep_ids}"
    # 排序：第 1 集的镜在前（episode sequence 升序合并）
    first_ep_items = [i for i in items if i["episode_id"] == ep1.id]
    last_ep_items = [i for i in items if i["episode_id"] == ep2.id]
    assert first_ep_items and last_ep_items
    assert max(i["sequence"] for i in first_ep_items) < min(
        i["sequence"] for i in last_ep_items
    )
