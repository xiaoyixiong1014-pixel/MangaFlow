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
    """定妆照生成：任务入队冻结 10 积分 → done 后按产物性质结算并回写资产/素材。

    测试环境无真实渠道、产物来自 mock 兜底（D3 治理）：占位产物执行扣费
    豁免——冻结全额退回，余额不变；task.meta.placeholder=True 透出。

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

    # 计费闭环（D3 豁免语义）：mock 占位产物冻结全额退回，余额不变
    assert final.get("placeholder") is True
    wallet2 = await get_wallet_model(db, auth["user_id"])
    assert wallet2.points_balance == 5000
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
    """一致性校验：返回 embedding 余弦真实分数（0~1）、details 分项与算法标识。

    Task 6.2 真实化后返回 {"similarity","passed","report","score","threshold",
    "details","algorithm"}；similarity 与 score 同值，passed = score >= 0.85。

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
    # 新结构断言：算法标识、阈值、分项明细、公式一致性
    assert body["algorithm"] == "embedding-cosine-v1"
    assert body["threshold"] == 0.85
    assert 0 <= body["similarity"] <= 1
    assert body["score"] == body["similarity"]
    assert isinstance(body["passed"], bool)
    assert body["passed"] == (body["score"] >= body["threshold"])
    assert "一致性" in body["report"]
    assert {"visual_prompt_sim", "description_sim", "locked_bonus"} <= set(body["details"])


async def test_suggest_prompt_liuyaosu_shibie(client, auth, db):
    """AI 提示词六要素识别：场景/道具从剧本正文提取六要素（用户确认后生成图片）。

    测试环境 LLM 可达（deepseek 200）：断言接口返回六键齐全（场景六键
    + anchors 数组），不硬断言具体值（LLM 输出内容随剧本变化）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from app.models import Scene, Script

    project = await create_project(client, auth["headers"], title=f"识别项目_{unique_suffix()}")
    pid = project["id"]
    content = {
        "scenes": [
            {"scene_no": 1, "title": "加班", "location": "深夜写字楼格子间",
             "time_of_day": "夜", "int_ext": "内景",
             "plot": "王小满在工位摸鱼，屏幕光照亮他的脸。",
             "dialogue": [{"character": "王小满", "line": "摸鱼才能活命。"}]},
        ]
    }
    db.add(Script(project_id=pid, content=content, raw_text=""))
    db.add(Scene(project_id=pid, name="深夜写字楼格子间", visual_prompt="旧提示词"))
    await db.commit()

    scene = (await db.execute(
        select(Scene).where(Scene.project_id == pid, Scene.name == "深夜写字楼格子间")
    )).scalar_one()
    resp = await client.post(
        f"/api/projects/{pid}/assets/scene/{scene.id}/suggest-prompt",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "scene"
    elements = body["elements"]
    # 场景六要素键齐全（LLM 实际输出值不硬断言）
    for key in ("style_base", "space_type", "anchors", "lighting", "composition", "negative"):
        assert key in elements
    assert isinstance(elements["anchors"], list)

    # 非法类型 400
    resp2 = await client.post(
        f"/api/projects/{pid}/assets/character/{scene.id}/suggest-prompt",
        headers=auth["headers"],
    )
    assert resp2.status_code == 400


async def test_shengcheng_changjing_daotu_tu(client, auth, db):
    """场景/道具图生成：任务入队冻结积分 → done 后回写 asset_url（与角色定妆照同构）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from app.models import Prop, Scene

    project = await create_project(client, auth["headers"], title=f"场景道具生成_{unique_suffix()}")
    pid = project["id"]
    db.add(Scene(project_id=pid, name="咖啡馆", visual_prompt="场景提示词"))
    db.add(Prop(project_id=pid, name="怀表", visual_prompt="道具提示词"))
    await db.commit()
    scene = (await db.execute(
        select(Scene).where(Scene.project_id == pid, Scene.name == "咖啡馆")
    )).scalar_one()
    prop = (await db.execute(
        select(Prop).where(Prop.project_id == pid, Prop.name == "怀表")
    )).scalar_one()

    # 场景图生成入队 → 轮询终态 → asset_url 回写
    resp = await client.post(
        f"/api/projects/{pid}/assets/scenes/{scene.id}/generate", headers=auth["headers"]
    )
    assert resp.status_code == 200, resp.text
    task = resp.json()
    assert task["cost_points"] == 10
    assert task["meta"]["target_type"] == "scene"
    final = await wait_task_terminal(client, auth["headers"], task["id"], timeout=30.0)
    assert final["status"] == "done", final
    await db.refresh(scene)
    assert scene.asset_url

    # 道具图生成入队 → 轮询终态 → asset_url 回写
    resp2 = await client.post(
        f"/api/projects/{pid}/assets/props/{prop.id}/generate", headers=auth["headers"]
    )
    assert resp2.status_code == 200, resp2.text
    task2 = resp2.json()
    assert task2["meta"]["target_type"] == "prop"
    final2 = await wait_task_terminal(client, auth["headers"], task2["id"], timeout=30.0)
    assert final2["status"] == "done", final2
    await db.refresh(prop)
    assert prop.asset_url

    # 未终态防重复：首个任务已完成可再次发起（幂等闸门按未终态判断）
    resp3 = await client.post(
        f"/api/projects/{pid}/assets/scenes/{scene.id}/generate", headers=auth["headers"]
    )
    assert resp3.status_code == 200, resp3.text
