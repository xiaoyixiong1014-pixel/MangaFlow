"""资产管理接口测试模块（文件级说明）。

覆盖链路：
- 创建角色（缺省视觉提示词模板生效）
- 生成定妆照：任务入队 + 冻结 10 积分 → 轮询至 done → 扣费、资产回写、
  媒体素材入库
- 面部锁定 / 解锁（face_locked + 版本快照）
- 变体：缺 inherit_mask 400；合法请求 200 并可列出
- 一致性校验返回 similarity
"""

import pytest
from sqlalchemy import select

from app.models import Character, MediaAsset, VersionSnapshot
from tests.conftest import (
    create_project,
    get_wallet_model,
    unique_suffix,
    wait_task_terminal,
)


async def _make_project_with_character(client, auth, tag: str):
    """辅助：创建项目与角色资产，返回 (project_id, character_id)（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        tag: 标题防冲突后缀。

    Returns:
        Tuple[int, int]: (项目 ID, 角色 ID)。

    Raises:
        AssertionError: 接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"资产项目_{tag}")
    resp = await client.post(
        f"/api/projects/{project['id']}/assets/characters",
        headers=auth["headers"],
        json={"name": "林小满", "description": "元气少女漫画家"},
    )
    assert resp.status_code == 200, resp.text
    character = resp.json()
    return project["id"], character["id"]


async def test_chuangjian_jiaose_moren_muban(client, auth):
    """创建角色：返回 id 且缺省视觉提示词走中文模板。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, cid = await _make_project_with_character(client, auth, unique_suffix())
    resp = await client.get(
        f"/api/projects/{pid}/assets", headers=auth["headers"]
    )
    assert resp.status_code == 200
    chars = resp.json()["characters"]
    assert len(chars) == 1
    assert chars[0]["id"] == cid
    assert chars[0]["name"] == "林小满"
    # 缺省模板：统一风格插画，{name}，{description}，全身立绘，纯色背景，高细节
    assert "统一风格插画" in chars[0]["visual_prompt"]
    assert chars[0]["face_locked"] is False


async def test_shengcheng_dingzhuangzhao_renwu_yu_jijin(client, auth, db):
    """定妆照生成：任务入队冻结 10 积分 → done 后扣费并回写资产/素材。

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
    task = resp.json()
    assert task["type"] == "image"
    assert task["cost_points"] == 10
    assert task["status"] in ("queued", "running", "done")

    # 冻结校验：执行器尚未完成任务时冻结应为 10（生成任务最短耗时 ~0.5s，
    # 紧随创建后的读取窗口内不会出现竞态；若已 done 则直接跳过冻结断言）
    wallet = await get_wallet_model(db, auth["user_id"])
    if task["status"] in ("queued", "running"):
        assert wallet.frozen_points == 10
        assert wallet.points_balance == 4990

    # 轮询至终态
    final = await wait_task_terminal(client, auth["headers"], task["id"], timeout=30.0)
    assert final["status"] == "done", final
    assert final["result_url"]

    # 计费闭环：扣费 10，冻结清零
    wallet2 = await get_wallet_model(db, auth["user_id"])
    assert wallet2.points_balance == 4990
    assert wallet2.frozen_points == 0

    # 资产回写：定妆照 URL 与生成状态
    char = (
        await db.execute(select(Character).where(Character.id == cid))
    ).scalar_one()
    assert char.asset_url == final["result_url"]
    assert char.generation_status == "done"

    # 媒体素材入库（studio 来源 image）
    media = (
        await db.execute(
            select(MediaAsset).where(
                MediaAsset.project_id == pid, MediaAsset.media_type == "image"
            )
        )
    ).scalars().all()
    assert len(media) >= 1
    assert any(m.url == final["result_url"] for m in media)


async def test_suoding_jiese_mianbu(client, auth, db):
    """锁定角色面部层：face_locked=True 并写 asset_locked 版本快照；解锁后复原。

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

    lock = await client.post(
        f"/api/projects/{pid}/assets/characters/{cid}/lock", headers=auth["headers"]
    )
    assert lock.status_code == 200
    assert lock.json()["face_locked"] is True

    # 版本快照落库（stage='asset_locked'）
    snaps = (
        await db.execute(
            select(VersionSnapshot).where(VersionSnapshot.project_id == pid)
        )
    ).scalars().all()
    assert any(s.stage == "asset_locked" for s in snaps)

    unlock = await client.post(
        f"/api/projects/{pid}/assets/characters/{cid}/unlock", headers=auth["headers"]
    )
    assert unlock.status_code == 200
    assert unlock.json()["face_locked"] is False


async def test_bianti_que_shao_inherit_mask_400(client, auth):
    """创建变体缺少 inherit_mask 返回 400（RD-14 显式继承声明）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 400 时中断。
    """
    pid, cid = await _make_project_with_character(client, auth, unique_suffix())
    resp = await client.post(
        f"/api/projects/{pid}/assets/characters/{cid}/variants",
        headers=auth["headers"],
        json={"variant_type": "outfit", "name": "便装"},  # 缺 inherit_mask
    )
    assert resp.status_code == 400
    assert "inherit_mask" in resp.json()["detail"]


async def test_bianti_chuangjian_yu_liebiao(client, auth):
    """合法变体创建成功并可从变体列表查到。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, cid = await _make_project_with_character(client, auth, unique_suffix())
    resp = await client.post(
        f"/api/projects/{pid}/assets/characters/{cid}/variants",
        headers=auth["headers"],
        json={
            "variant_type": "outfit",
            "name": "冬季校服",
            "inherit_mask": {"face": True, "hair": True, "outfit": False},
        },
    )
    assert resp.status_code == 200, resp.text
    variant = resp.json()
    assert variant["variant_type"] == "outfit"
    assert variant["inherit_mask"]["face"] is True

    listing = await client.get(
        f"/api/projects/{pid}/assets/characters/{cid}/variants", headers=auth["headers"]
    )
    assert listing.status_code == 200
    assert any(v["id"] == variant["id"] for v in listing.json())


async def test_yizhixing_jiaoyan_fanhui_similarity(client, auth):
    """一致性校验：返回 similarity（0.88~0.99）、passed 与报告文本。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, cid = await _make_project_with_character(client, auth, unique_suffix())
    resp = await client.post(
        f"/api/projects/{pid}/assets/consistency-check",
        headers=auth["headers"],
        json={"character_id": cid},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert 0.88 <= body["similarity"] <= 0.99
    assert isinstance(body["passed"], bool)
    assert "一致性" in body["report"]
