"""连续性检查与目标时长自动调整测试模块（文件级说明，SB-06 / SB-07）。

覆盖链路：
- 连续性检查（POST /storyboards/continuity-check）：
  * 相邻两镜同角色不同服装变体 → outfit_conflict suspect 命中；
  * 无冲突序列 → items 为空（不误报：同变体 / 未绑变体 / 场景缺省）；
  * 场景切换（不同场景资产、无连续词）→ scene_jump info；
  * 提示词缺失（镜 N 提示词含角色名 → 镜 N+1 对白点名但提示词缺席）→
    prompt_missing info；
  * 他人项目 404 / 剧集不属于本项目 404。
- 目标时长自动调整（POST /storyboards/auto-fit-duration）：
  * 等比缩放落库断言（10s/30s → 目标 20s ⇒ 5s/15s，合计 ≈ 目标）；
  * 非法 target_seconds（0 / 3601）→ 400；
  * 他人项目 404。

数据构造：项目用 API 创建；分镜/角色/变体/关联/剧集直接 ORM 插入
（绕开 LLM 拆解，与 conftest.insert_storyboard 同模式）。
"""

from sqlalchemy import select

from app.models import (
    AssetVariant,
    Character,
    Episode,
    Storyboard,
    StoryboardCharacter,
)
from tests.conftest import create_project, insert_storyboard, register_user, unique_suffix


async def _make_character(db, project_id: int, name: str = "林小满") -> Character:
    """辅助：直插一个角色资产（内部函数）。

    Args:
        db: 测试库会话。
        project_id: 归属项目 ID。
        name: 角色名。

    Returns:
        Character: 已落库的角色 ORM 对象。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    ch = Character(project_id=project_id, name=name, visual_prompt=f"{name} 标准视觉提示词")
    db.add(ch)
    await db.commit()
    await db.refresh(ch)
    return ch


async def _make_outfit_variant(db, character_id: int, name: str) -> AssetVariant:
    """辅助：直插一个角色服装变体（内部函数）。

    Args:
        db: 测试库会话。
        character_id: 归属角色 ID（多态 owner，应用层完整性）。
        name: 变体名（如「校服」）。

    Returns:
        AssetVariant: 已落库的变体 ORM 对象。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    variant = AssetVariant(
        owner_type="character", owner_id=character_id, variant_type="outfit",
        name=name, inherit_mask={},
    )
    db.add(variant)
    await db.commit()
    await db.refresh(variant)
    return variant


async def _link_character(db, storyboard_id: int, character_id: int, variant_id) -> None:
    """辅助：直插分镜-角色关联（含服装变体绑定，内部函数）。

    Args:
        db: 测试库会话。
        storyboard_id: 分镜 ID。
        character_id: 角色 ID。
        variant_id: 服装变体 ID（可 None）。

    Returns:
        None。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    db.add(StoryboardCharacter(
        storyboard_id=storyboard_id, character_id=character_id,
        variant_id=variant_id, link_confidence=0.9,
    ))
    await db.commit()


async def test_fuzhuang_bianti_chongtu_suspect(client, auth, db):
    """相邻两镜同角色绑定不同服装变体 → outfit_conflict suspect 命中。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 未命中 suspect 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"连续项目_{unique_suffix()}")
    pid = project["id"]
    sb1 = await insert_storyboard(db, pid, sequence=1, dialogue="林小满：开始吧")
    sb2 = await insert_storyboard(db, pid, sequence=2, dialogue="林小满：走吧")

    ch = await _make_character(db, pid)
    v1 = await _make_outfit_variant(db, ch.id, "校服")
    v2 = await _make_outfit_variant(db, ch.id, "战斗装")
    await _link_character(db, sb1.id, ch.id, v1.id)
    await _link_character(db, sb2.id, ch.id, v2.id)

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/continuity-check", headers=auth["headers"], json={}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["checked_pairs"] == 1
    suspects = [i for i in body["items"]
                if i["type"] == "outfit_conflict" and i["confidence"] == "suspect"]
    assert suspects, body
    # 问题归属到后一镜，文案包含角色名与两个变体名
    assert suspects[0]["storyboard_id"] == sb2.id
    assert "林小满" in suspects[0]["message"]
    assert "校服" in suspects[0]["message"] and "战斗装" in suspects[0]["message"]


async def test_wu_chongtu_bu_wubao(client, auth, db):
    """无冲突序列 → items 为空（不误报）。

    场景：同角色相邻两镜绑定同一变体；另一镜同角色未绑变体（None 不判）；
    分镜均未强绑场景、默认提示词不含角色名（规则 a/c 前置不满足）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: items 非空时中断。
    """
    project = await create_project(client, auth["headers"], title=f"无冲突项目_{unique_suffix()}")
    pid = project["id"]
    sb1 = await insert_storyboard(db, pid, sequence=1)
    sb2 = await insert_storyboard(db, pid, sequence=2)
    sb3 = await insert_storyboard(db, pid, sequence=3)

    ch = await _make_character(db, pid)
    v1 = await _make_outfit_variant(db, ch.id, "校服")
    await _link_character(db, sb1.id, ch.id, v1.id)
    await _link_character(db, sb2.id, ch.id, v1.id)  # 同变体：不判冲突
    await _link_character(db, sb3.id, ch.id, None)   # 未绑变体：不判冲突

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/continuity-check", headers=auth["headers"], json={}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == [], body
    assert body["checked_pairs"] == 2


async def test_changjing_qiehuanyu_tishici_queshi_info(client, auth, db):
    """场景切换（无连续词）→ scene_jump info；对白点名但提示词缺席 → prompt_missing info。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 未命中 info 项时中断。
    """
    from app.models import Scene

    project = await create_project(client, auth["headers"], title=f"场景项目_{unique_suffix()}")
    pid = project["id"]

    # 两个真实场景资产（scene_id 有外键约束，不能插假值）
    scene_a = Scene(project_id=pid, name="天台", visual_prompt="天台场景")
    scene_b = Scene(project_id=pid, name="街道", visual_prompt="街道场景")
    db.add_all([scene_a, scene_b])
    await db.commit()
    await db.refresh(scene_a)
    await db.refresh(scene_b)

    # 镜 1：强绑天台、提示词包含角色名；镜 2：强绑街道、对白点名角色但提示词缺席
    ch = await _make_character(db, pid)
    sb1 = await insert_storyboard(
        db, pid, sequence=1,
        visual_description="天台远眺", dialogue=None,
        prompt_locked="统一风格；场景：天台；角色：林小满；画面：天台远眺",
    )
    sb2 = await insert_storyboard(
        db, pid, sequence=2,
        visual_description="街道独行（无连续词）", dialogue="林小满：接着走吧",
        prompt_locked="统一风格；场景：街道；画面：街道独行",
    )
    # 回填 scene_id（insert_storyboard 不暴露该字段）
    sb1.scene_id = scene_a.id
    sb2.scene_id = scene_b.id
    await db.commit()

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/continuity-check", headers=auth["headers"], json={}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    jumps = [i for i in body["items"] if i["type"] == "scene_jump"]
    assert len(jumps) == 1, body
    assert jumps[0]["confidence"] == "info"  # 无连续词 → info 而非 suspect
    assert jumps[0]["storyboard_id"] == sb2.id
    assert "天台" in jumps[0]["message"] and "街道" in jumps[0]["message"]

    missing = [i for i in body["items"] if i["type"] == "prompt_missing"]
    assert len(missing) == 1, body
    assert missing[0]["confidence"] == "info"
    assert missing[0]["storyboard_id"] == sb2.id
    assert "林小满" in missing[0]["message"]


async def test_continuity_yuequan_404(client, auth, db):
    """他人项目连续性检查 404；剧集不属于本项目 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 404 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"连续越权_{unique_suffix()}")
    pid = project["id"]
    await insert_storyboard(db, pid, sequence=1)

    # 他人项目：404
    other = await register_user(client, prefix="connoer")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    resp = await client.post(
        f"/api/projects/{pid}/storyboards/continuity-check", headers=other_headers, json={}
    )
    assert resp.status_code == 404

    # 剧集不属于本项目（挂在他人项目下）：404
    other_project = await create_project(client, other_headers, title=f"他 proj_{unique_suffix()}")
    ep = Episode(project_id=other_project["id"], title="别家剧集", sequence=1)
    db.add(ep)
    await db.commit()
    await db.refresh(ep)
    resp = await client.post(
        f"/api/projects/{pid}/storyboards/continuity-check",
        headers=auth["headers"],
        json={"episode_id": ep.id},
    )
    assert resp.status_code == 404


async def test_auto_fit_dengbi_suofang_luoku(client, auth, db):
    """目标时长自动调整：10s/30s → 目标 20s ⇒ 5s/15s（落库断言，合计 = 目标）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 摘要或落库值与预期不符时中断。
    """
    project = await create_project(client, auth["headers"], title=f"调时长_{unique_suffix()}")
    pid = project["id"]
    sb1 = await insert_storyboard(db, pid, sequence=1, duration=10)
    sb2 = await insert_storyboard(db, pid, sequence=2, duration=30)

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/auto-fit-duration",
        headers=auth["headers"],
        json={"target_seconds": 20},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["before_total"] == 40
    assert body["target_seconds"] == 20
    assert body["after_total"] == 20  # 5 + 15，恰为目标值
    assert body["changed"] == 2
    per_shot = {d["id"]: (d["before"], d["after"]) for d in body["per_shot"]}
    assert per_shot[sb1.id] == (10, 5)
    assert per_shot[sb2.id] == (30, 15)

    # 落库断言：本 session 缓存了造数对象（API 在独立 session 提交），
    # 先过期全部对象强制从数据库重载，再断言落库终态
    db.expire_all()
    rows = (
        await db.execute(select(Storyboard).where(Storyboard.project_id == pid))
    ).scalars().all()
    durations = {sb.id: sb.duration for sb in rows}
    assert durations[sb1.id] == 5
    assert durations[sb2.id] == 15


async def test_auto_fit_feifa_target_400(client, auth, db):
    """非法 target_seconds（0 / 3601）→ 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 400 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"调时非法_{unique_suffix()}")
    pid = project["id"]
    await insert_storyboard(db, pid, sequence=1, duration=10)

    for bad in (0, 3601):
        resp = await client.post(
            f"/api/projects/{pid}/storyboards/auto-fit-duration",
            headers=auth["headers"],
            json={"target_seconds": bad},
        )
        assert resp.status_code == 400, (bad, resp.text)


async def test_auto_fit_yuequan_404(client, auth, db):
    """他人项目自动调整 404（不泄露存在性）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文（项目归属者）。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 404 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"调时越权_{unique_suffix()}")
    await insert_storyboard(db, project["id"], sequence=1, duration=10)

    other = await register_user(client, prefix="fitnoer")
    resp = await client.post(
        f"/api/projects/{project['id']}/storyboards/auto-fit-duration",
        headers={"Authorization": f"Bearer {other['access_token']}"},
        json={"target_seconds": 30},
    )
    assert resp.status_code == 404
