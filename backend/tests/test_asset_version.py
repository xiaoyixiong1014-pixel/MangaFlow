"""资产版本透传测试模块（文件级说明，P0-3）。

覆盖链路：
- 首次生成 asset_version 保持 1；替换/重生成后递增为 2；
- decompose 快照写入（storyboard_characters.character_version / storyboards.scene_version）；
- 资产图更新后分镜列表 stale=True（附原因文案）。
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Character, Scene, Script
from tests.conftest import (
    create_project,
    insert_storyboard,
    unique_suffix,
    wait_task_terminal,
)


async def _make_project_with_character(client, auth, tag: str):
    """辅助：创建项目与角色资产，返回 (project_id, character_id)。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        tag: 标题防冲突后缀。

    Returns:
        Tuple[int, int]: (项目 ID, 角色 ID)。

    Raises:
        AssertionError: 接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"版本项目_{tag}")
    resp = await client.post(
        f"/api/projects/{project['id']}/assets/characters",
        headers=auth["headers"], json={"name": "林小满", "description": "元气少女漫画家"},
    )
    assert resp.status_code == 200, resp.text
    return project["id"], resp.json()["id"]


async def test_shouci_shengcheng_banben_1(client, auth, db: AsyncSession):
    """版本基线：首次定妆照生成完成后 asset_version 保持 1。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, cid = await _make_project_with_character(client, auth, unique_suffix())
    resp = await client.post(
        f"/api/projects/{pid}/assets/characters/{cid}/generate", headers=auth["headers"]
    )
    assert resp.status_code == 200, resp.text
    task = await wait_task_terminal(client, auth["headers"], resp.json()["id"])
    assert task["status"] == "done", task

    obj = await db.get(Character, cid)
    await db.refresh(obj)
    assert obj.asset_version == 1
    assert obj.asset_url is not None


async def test_chongxin_shengcheng_banben_zengliang(client, auth, db: AsyncSession):
    """版本递增：已有定妆照再次生成（替换）后 asset_version 递增为 2。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, cid = await _make_project_with_character(client, auth, unique_suffix())
    # 首次生成
    first = await client.post(
        f"/api/projects/{pid}/assets/characters/{cid}/generate", headers=auth["headers"]
    )
    assert first.status_code == 200, first.text
    await wait_task_terminal(client, auth["headers"], first.json()["id"])

    # 替换（重生成）
    second = await client.post(
        f"/api/projects/{pid}/assets/characters/{cid}/generate", headers=auth["headers"]
    )
    assert second.status_code == 200, second.text
    final = await wait_task_terminal(client, auth["headers"], second.json()["id"])
    assert final["status"] == "done", final

    obj = await db.get(Character, cid)
    await db.refresh(obj)
    assert obj.asset_version == 2


async def test_kuaizhao_xiaru_yu_stale_panduan(client, auth, db: AsyncSession):
    """快照与过期判定：分镜引用写入版本快照；资产版本变化后该镜 stale=True。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from sqlalchemy import select

    from app.models import Storyboard, StoryboardCharacter

    project = await create_project(client, auth["headers"], title=f"快照项目_{unique_suffix()}")
    pid = project["id"]

    # 角色与场景资产
    resp = await client.post(
        f"/api/projects/{pid}/assets/characters",
        headers=auth["headers"], json={"name": "林小满", "description": "元气少女"},
    )
    cid = resp.json()["id"]
    resp = await client.post(
        f"/api/projects/{pid}/assets/scenes",
        headers=auth["headers"], json={"name": "咖啡馆", "description": "街角咖啡馆"},
    )
    sid = resp.json()["id"]

    # ORM 直插一条分镜 + 角色关联（写入引用时的版本快照）
    # 注：insert_storyboard 不支持 scene_id 覆盖，返回后补绑场景
    sb = await insert_storyboard(db, pid, sequence=1)
    sb.scene_id = sid
    sb.scene_version = 1  # 模拟引用时快照
    db.add(StoryboardCharacter(
        storyboard_id=sb.id, character_id=cid, variant_id=None,
        character_version=1, link_confidence=0.8,
    ))
    await db.commit()

    # 初始：版本一致 → stale=False
    resp = await client.get(f"/api/projects/{pid}/storyboards", headers=auth["headers"])
    assert resp.status_code == 200
    boards = resp.json()
    target = next(b for b in boards if b["id"] == sb.id)
    assert target["stale"] is False

    # 资产更新：角色 asset_version +1（模拟定妆照替换回写）
    ch = await db.get(Character, cid)
    ch.asset_version = 2
    await db.commit()

    resp = await client.get(f"/api/projects/{pid}/storyboards", headers=auth["headers"])
    boards = resp.json()
    target = next(b for b in boards if b["id"] == sb.id)
    assert target["stale"] is True
    assert any("定妆照已更新" in r for r in target["stale_reasons"])

    # 场景版本变化同样触发 stale
    scene = await db.get(Scene, sid)
    scene.asset_version = 3
    await db.commit()
    resp = await client.get(f"/api/projects/{pid}/storyboards", headers=auth["headers"])
    target = next(b for b in resp.json() if b["id"] == sb.id)
    reasons = target["stale_reasons"]
    assert any("定妆照已更新" in r for r in reasons)
    assert any("场景概念图已更新" in r for r in reasons)
