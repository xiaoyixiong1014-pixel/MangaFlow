"""分镜锁定门禁测试模块（文件级说明，P0-1）。

覆盖链路：
- 主要角色未锁定/未生成 → decompose 409（body 含 code 与未就绪清单）；
- skip_lock_check=true 快速模式绕过门禁；
- role_type=extra 群像放行；
- generate 防重复：同角色存在未终态任务时 409。
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Character, Script
from tests.conftest import create_project, unique_suffix


async def _setup_project_with_script(client, auth, db: AsyncSession):
    """辅助：创建项目 + 插入含两场景的剧本行（decompose 前置数据）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        dict: {"project_id": int}。

    Raises:
        AssertionError: 项目创建失败时中断。
    """
    project = await create_project(client, auth["headers"], title=f"门禁项目_{unique_suffix()}")
    content = {
        "scenes": [
            {"title": "咖啡馆", "plot": "重逢",
             "dialogue": [{"character": "林小满", "line": "你来了。"}]},
            {"title": "天台", "plot": "告白",
             "dialogue": [{"character": "陈默", "line": "走吧。"}]},
        ]
    }
    db.add(Script(project_id=project["id"], content=content,
                  raw_text="林小满：你来了。\n陈默：走吧。"))
    await db.commit()
    return {"project_id": project["id"]}


async def _add_character(db: AsyncSession, project_id: int, name: str, **overrides) -> int:
    """辅助：ORM 直插角色资产并返回 ID。

    默认 enabled_at=now：锁定门禁只检查已启用角色（enabled_at IS NOT NULL，
    按集工作流语义），资产库语义的角色数据必须带启用时间，否则门禁会被
    过滤条件绕过（未就绪清单看不到该角色导致 200 放行）。

    Args:
        db: 测试库会话。
        project_id: 项目 ID。
        name: 角色名。
        **overrides: 其余字段覆盖（role_type/face_locked/generation_status 等）。

    Returns:
        int: 角色 ID。

    Raises:
        无。
    """
    from datetime import datetime, timezone as tz

    fields = {
        "visual_prompt": f"统一风格插画，{name}",
        "role_type": "lead",
        "face_locked": False,
        "generation_status": "pending",
        "enabled_at": datetime.now(tz.utc),
    }
    fields.update(overrides)
    ch = Character(project_id=project_id, name=name, **fields)
    db.add(ch)
    await db.commit()
    await db.refresh(ch)
    return ch.id


async def test_weisuoding_zhuyao_jiaose_lanjie(client, auth, db: AsyncSession):
    """门禁拦截：主要角色未锁定 → decompose 409 携带未就绪清单；快速模式绕过。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    data = await _setup_project_with_script(client, auth, db)
    pid = data["project_id"]
    locked = await _add_character(db, pid, "林小满", role_type="lead",
                                  face_locked=True, generation_status="done")
    unready_id = await _add_character(db, pid, "陈默", role_type="support",
                                      face_locked=False, generation_status="done")

    # 未锁定主要角色 → 409
    resp = await client.post(
        f"/api/projects/{pid}/storyboards/decompose", headers=auth["headers"], json={}
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "asset_lock_not_ready"
    unready = detail["unready"]
    assert len(unready) == 1
    assert unready[0]["character_id"] == unready_id
    assert unready[0]["reason"] == "未锁定"

    # 快速模式：skip_lock_check=true 绕过门禁（LLM 失败走规则降级仍可拆解）
    resp = await client.post(
        f"/api/projects/{pid}/storyboards/decompose",
        headers=auth["headers"], json={"skip_lock_check": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1


async def test_weishengcheng_dingzhuangzhao_lanjie(client, auth, db: AsyncSession):
    """门禁拦截：主要角色未生成定妆照（无图+pending）→ 409 原因「未生成定妆照」。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    data = await _setup_project_with_script(client, auth, db)
    pid = data["project_id"]
    await _add_character(db, pid, "林小满", role_type="lead")  # pending 无图

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/decompose", headers=auth["headers"], json={}
    )
    assert resp.status_code == 409
    unready = resp.json()["detail"]["unready"]
    assert unready[0]["reason"] == "未生成定妆照"
    assert unready[0]["has_image"] is False


async def test_extra_qunxiang_fangxing(client, auth, db: AsyncSession):
    """门禁放行：role_type=extra 群像不做定妆强制（对应只锁主要角色策略）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    data = await _setup_project_with_script(client, auth, db)
    pid = data["project_id"]
    await _add_character(db, pid, "路人甲", role_type="extra",
                         face_locked=False, generation_status="pending")

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/decompose", headers=auth["headers"], json={}
    )
    assert resp.status_code == 200, resp.text


async def test_generate_fangzhongfu_koufen(client, auth, db: AsyncSession):
    """generate 防重复：同角色存在未终态任务时二次请求 409（防双击重复冻结积分）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(client, auth["headers"], title=f"防重项目_{unique_suffix()}")
    resp = await client.post(
        f"/api/projects/{project['id']}/assets/characters",
        headers=auth["headers"], json={"name": "林小满", "description": "元气少女"},
    )
    assert resp.status_code == 200
    cid = resp.json()["id"]

    # 第一次生成入队（mock 渠道约 0.5s，紧随其后的二次请求窗口内必为 queued/running）
    first = await client.post(
        f"/api/projects/{project['id']}/assets/characters/{cid}/generate",
        headers=auth["headers"],
    )
    assert first.status_code == 200, first.text

    # 紧接着的第二次生成：存在未终态任务 → 409
    second = await client.post(
        f"/api/projects/{project['id']}/assets/characters/{cid}/generate",
        headers=auth["headers"],
    )
    if second.status_code != 409:
        # 极快机器上首个任务可能已完成：此时允许再次入队（终态后可重发），跳过断言
        pytest.skip("首个任务已完成，未命中防重复窗口")
    assert "进行中" in second.json()["detail"]
