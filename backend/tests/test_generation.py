"""生成任务接口测试模块（文件级说明）。

覆盖链路（依赖进程内 mock 供应商执行器）：
- 关键帧 trial：任务入队 → 轮询至 done（最长 30s）→ 钱包扣 10、
  storyboard.keyframe_url 回写、media_assets 有记录
- 同分镜同阶段重复提交 409 防重
- 违规词提示词：任务 failed + error_code=content_violation + 审核记录
  violate + 冻结退款
- 取消 queued 任务：canceled + 冻结退款
- A-04 扩展：关键帧任务 meta 透传绑定角色定妆照/造型变体图参考图
"""

from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import select

from app.models import (
    AssetVariant,
    Character,
    MediaAsset,
    Project,
    Prop,
    ReviewRecord,
    Scene,
    Storyboard,
    StoryboardCharacter,
    StoryboardProp,
    Task,
)
from tests.conftest import (
    create_project,
    get_wallet_model,
    insert_storyboard,
    unique_suffix,
    wait_task_terminal,
)


async def _make_project_with_storyboard(client, auth, db, tag: str):
    """辅助：建项目并直插一条分镜，返回 (project_id, storyboard)（内部函数）。

    项目状态同步置为 producing（生成阶段门禁：关键帧任务仅允许
    已确认分镜的项目发起，与真实用户「确认分镜」后的状态一致）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        tag: 标题防冲突后缀。

    Returns:
        Tuple[int, Storyboard]: (项目 ID, 分镜 ORM 对象)。

    Raises:
        AssertionError: 接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"生成项目_{tag}")
    sb = await insert_storyboard(db, project["id"], sequence=1)
    # 直改状态至 storyboard（模拟拆解完成）后走真实 confirm 链路推进 producing
    proj = (
        await db.execute(select(Project).where(Project.id == project["id"]))
    ).scalar_one()
    proj.status = "storyboard"
    await db.commit()
    confirm = await client.post(
        f"/api/projects/{project['id']}/storyboards/confirm", headers=auth["headers"]
    )
    assert confirm.status_code == 200, confirm.text
    return project["id"], sb


async def test_guanjianzhen_shiyan_wancheng_koufei(client, auth, db):
    """关键帧 trial：任务 done 后扣 10 积分、关键帧回写、媒体素材入库。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _make_project_with_storyboard(client, auth, db, unique_suffix())

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial", "prompt_editable": "夕阳下的天台，少女远眺城市"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_cost"] == 10
    assert len(body["tasks"]) == 1
    task = body["tasks"][0]
    assert task["type"] == "image"
    assert task["phase"] == "trial"

    # 轮询至终态（mock 供应商本地生成很快，最长等 30s）
    final = await wait_task_terminal(client, auth["headers"], task["id"], timeout=30.0)
    assert final["status"] == "done", final
    assert final["progress"] == 100

    # 计费闭环（D3 豁免语义）：mock 占位产物冻结全额退回，余额保持 5000
    assert final.get("placeholder") is True
    wallet = await get_wallet_model(db, auth["user_id"])
    assert wallet.points_balance == 5000
    assert wallet.frozen_points == 0

    # 分镜关键帧回写
    sb2 = (await db.execute(select(Storyboard).where(Storyboard.id == sb.id))).scalar_one()
    await db.refresh(sb2)
    assert sb2.keyframe_url == final["result_url"]
    assert sb2.status == "done"

    # 媒体素材有记录
    media = (
        await db.execute(
            select(MediaAsset).where(
                MediaAsset.project_id == pid, MediaAsset.media_type == "image"
            )
        )
    ).scalars().all()
    assert len(media) >= 1


async def test_tong_fenjing_tong_jieduan_fangchong_409(client, auth, db):
    """同分镜同阶段已有进行中任务时重复提交返回 409。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 409 时中断。
    """
    pid, sb = await _make_project_with_storyboard(client, auth, db, unique_suffix())

    first = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial"},
    )
    assert first.status_code == 200

    # 紧随其后的第二次提交：首个任务仍在排队/进行中 → 409
    second = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial"},
    )
    assert second.status_code == 409, second.text
    assert "进行中" in second.json()["detail"] or "取消" in second.json()["detail"]

    # 清理：等首个任务结束（避免影响其他用例的数据窗口）
    await wait_task_terminal(client, auth["headers"], first.json()["tasks"][0]["id"])


async def test_weigui_cihui_renwu_shibai_tuikuan(client, auth, db):
    """违规词提示词：任务 failed（content_violation）+ 审核记录 violate + 退款。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _make_project_with_storyboard(client, auth, db, unique_suffix())

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial", "prompt_editable": "爆发枪击冲突的紧张画面"},
    )
    assert resp.status_code == 200, resp.text
    task = resp.json()["tasks"][0]

    final = await wait_task_terminal(client, auth["headers"], task["id"], timeout=30.0)
    assert final["status"] == "failed", final
    assert final["error_code"] == "content_violation"

    # 审核记录：gate=generate、result=violate
    records = (
        await db.execute(
            select(ReviewRecord).where(
                ReviewRecord.project_id == pid, ReviewRecord.gate == "generate"
            )
        )
    ).scalars().all()
    assert any(r.result == "violate" for r in records)

    # 退款闭环：冻结清零、余额恢复 5000
    wallet = await get_wallet_model(db, auth["user_id"])
    assert wallet.frozen_points == 0
    assert wallet.points_balance == 5000


async def test_quxiao_renwu_tuikuan(client, auth, db):
    """取消排队中的任务：最终 canceled 且冻结积分退回。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _make_project_with_storyboard(client, auth, db, unique_suffix())

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial"},
    )
    assert resp.status_code == 200
    task = resp.json()["tasks"][0]

    cancel = await client.post(f"/api/tasks/{task['id']}/cancel", headers=auth["headers"])
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] in ("canceled", "canceling")

    # 无论走了 queued 直取还是 running 协作取消，最终都应 canceled + 退款
    final = await wait_task_terminal(client, auth["headers"], task["id"], timeout=30.0)
    assert final["status"] == "canceled", final

    wallet = await get_wallet_model(db, auth["user_id"])
    assert wallet.frozen_points == 0
    assert wallet.points_balance == 5000


async def _bind_character_with_image(client, auth, db, pid: int, sb, *, use_variant: bool):
    """辅助：为项目建一个带定妆照的角色并绑定到分镜（内部函数）。

    Args:
        client: 内存 HTTP 客户端（保留参数与既有辅助签名一致）。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        pid: 项目 ID。
        sb: 分镜 ORM 对象。
        use_variant: 为 True 时额外创建造型变体图并让绑定记录指向变体。

    Returns:
        Tuple[Character, Optional[AssetVariant]]: （角色，变体或 None）。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    char = Character(
        project_id=pid,
        name=f"林小满_{unique_suffix()}",
        visual_prompt="测试视觉提示词",
        asset_url="/storage/generated/char_face_ref.png",
        enabled_at=datetime.now(timezone.utc),
    )
    db.add(char)
    await db.flush()
    variant: Optional[AssetVariant] = None
    variant_id: Optional[int] = None
    if use_variant:
        variant = AssetVariant(
            owner_type="character",
            owner_id=char.id,
            variant_type="outfit",
            name=f"战斗装_{unique_suffix()}",
            inherit_mask={},
            asset_url="/storage/generated/char_variant_ref.png",
        )
        db.add(variant)
        await db.flush()
        variant_id = variant.id
    db.add(StoryboardCharacter(
        storyboard_id=sb.id, character_id=char.id,
        variant_id=variant_id, link_confidence=0.8,
    ))
    await db.commit()
    return char, variant


async def test_guanjianzhen_meta_tou_chuan_dingzhuangzhao(client, auth, db):
    """A-04 扩展：关键帧任务 meta 透传绑定角色的定妆照参考图。

    分镜绑定带定妆照的角色后发起 trial：任务 meta.reference_image_urls
    应为 [该角色定妆照 URL]，并携带项目画幅与一致性生成指令
    （TaskOut 不透出 meta，直接查库断言）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _make_project_with_storyboard(client, auth, db, unique_suffix())
    char, _ = await _bind_character_with_image(client, auth, db, pid, sb, use_variant=False)

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial"},
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["tasks"][0]["id"]
    row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
    meta = row.meta or {}
    assert meta.get("reference_image_urls") == [char.asset_url]
    # 输出画幅跟随项目配置（新建项目时选择的输出比例）
    assert meta.get("aspect_ratio")
    # 一致性生成指令动态追加进正向提示词（存量分镜免重拆即生效）
    assert "生成要求" in (meta.get("prompt") or "")

    # 收尾：等任务终态，避免遗留排队任务
    await wait_task_terminal(client, auth["headers"], task_id)


async def test_guanjianzhen_cankao_youxian_bianzhitu(client, auth, db):
    """A-04 扩展：绑定记录指向造型变体时优先取变体图作为参考图。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _make_project_with_storyboard(client, auth, db, unique_suffix())
    await _bind_character_with_image(client, auth, db, pid, sb, use_variant=True)

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial"},
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["tasks"][0]["id"]
    row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert (row.meta or {}).get("reference_image_urls") == ["/storage/generated/char_variant_ref.png"]

    # 收尾：等任务终态，避免遗留排队任务
    await wait_task_terminal(client, auth["headers"], task_id)


async def test_guanjianzhen_cankao_shouji_changjing_daoju(client, auth, db):
    """A-04 扩展：参考图组按「角色 → 场景 → 道具」顺序收集绑定资产图。

    分镜同时绑定角色（含定妆照）、场景（概念图）与道具（标准图）时，
    任务 meta.reference_image_urls 应为三者依序组成的列表。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _make_project_with_storyboard(client, auth, db, unique_suffix())
    char, _ = await _bind_character_with_image(client, auth, db, pid, sb, use_variant=False)
    # 直插场景与道具资产并绑定到分镜
    scene = Scene(
        project_id=pid, name=f"天台_{unique_suffix()}", visual_prompt="场景视觉提示词",
        asset_url="/storage/generated/scene_ref.png",
    )
    prop = Prop(
        project_id=pid, name=f"手机_{unique_suffix()}", visual_prompt="道具视觉提示词",
        asset_url="/storage/generated/prop_ref.png",
    )
    db.add_all([scene, prop])
    await db.flush()
    sb.scene_id = scene.id
    db.add(StoryboardProp(storyboard_id=sb.id, prop_id=prop.id, prop_version=1))
    await db.commit()

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"],
        json={"phase": "trial"},
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["tasks"][0]["id"]
    row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert (row.meta or {}).get("reference_image_urls") == [
        char.asset_url,
        "/storage/generated/scene_ref.png",
        "/storage/generated/prop_ref.png",
    ]

    # 收尾：等任务终态，避免遗留排队任务
    await wait_task_terminal(client, auth["headers"], task_id)


async def _bind_locked_speaker(db, pid: int, sb, name: str) -> Character:
    """辅助：建一个音色已锁定的角色并绑定到分镜（段级配音门禁前置数据）。

    Args:
        db: 测试库会话。
        pid: 项目 ID。
        sb: 分镜 ORM 对象。
        name: 角色名（与对白行「名字：」前缀精确匹配）。

    Returns:
        Character: 已落库并绑定的角色 ORM 对象。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    char = Character(
        project_id=pid,
        name=name,
        visual_prompt="测试角色视觉提示词",
        gender="female",
        voice_note="温柔清亮的女声，语速平缓",
        voice_selected="female_01",
        voice_locked=True,
        enabled_at=datetime.now(timezone.utc),
    )
    db.add(char)
    await db.flush()
    db.add(StoryboardCharacter(storyboard_id=sb.id, character_id=char.id))
    await db.commit()
    return char


async def test_duanji_peiyin_yiju_yisheng(client, auth, db):
    """段级配音（一句一行）：segment_index 提交仅合成该段并独立落产物。

    - meta 携带 segment_index 与全量 segments，计费按该段字数；
    - 段下标越界 400；同镜同段 active 任务 409 防重；
    - 任务 done 后 storyboard.audio.dialogue[idx] 回写段产物，
      未全段完成时 audio_url 保持空；
    - 补齐另一段后全段齐 → 后端自动拼接整镜音轨回写 audio_url。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _make_project_with_storyboard(client, auth, db, unique_suffix())
    sb.dialogue = "陈默：测试第一句台词。\n周姨：测试第二句台词。"
    await db.commit()
    await _bind_locked_speaker(db, pid, sb, "陈默")
    await _bind_locked_speaker(db, pid, sb, "周姨")
    tts_url = f"/api/projects/{pid}/storyboards/{sb.id}/tts"

    # 段下标越界 400
    bad = await client.post(tts_url, headers=auth["headers"], json={"segment_index": 5})
    assert bad.status_code == 400, bad.text

    # 提交段 1（周姨）：meta 携带 segment_index 与全量 segments，按段计费
    resp = await client.post(tts_url, headers=auth["headers"], json={"segment_index": 1})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]
    row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert (row.meta or {}).get("segment_index") == 1
    assert len((row.meta or {}).get("segments") or []) == 2
    assert row.cost_points == 5  # 段文本 9 字 → ceil(9/100)×5

    # 同镜同段防重：直插一条 running 段任务再提交 → 409
    db.add(Task(
        project_id=pid, type="tts", status="running", cost_points=5,
        storyboard_id=sb.id, idempotency_key=f"test-dup-guard-{unique_suffix()}",
        meta={"segment_index": 1, "segments": [], "text": "占位"},
    ))
    await db.commit()
    dup = await client.post(tts_url, headers=auth["headers"], json={"segment_index": 1})
    assert dup.status_code == 409, dup.text

    # 段 1 完成：audio.dialogue[1] 有产物、dialogue[0] 为占位 None，audio_url 保持空
    await wait_task_terminal(client, auth["headers"], task_id)
    await db.refresh(sb)
    dlg = (sb.audio or {}).get("dialogue") or []
    assert len(dlg) == 2 and dlg[1] and dlg[1]["audio_url"]
    assert dlg[1]["speaker"] == "周姨" and dlg[0] is None
    assert not sb.audio_url

    # 提交段 0（陈默）：全段齐 → 自动拼接整镜音轨
    resp0 = await client.post(tts_url, headers=auth["headers"], json={"segment_index": 0})
    assert resp0.status_code == 200, resp0.text
    await wait_task_terminal(client, auth["headers"], resp0.json()["id"])
    await db.refresh(sb)
    assert sb.audio_url and sb.audio_url.startswith("/storage/generated/tts_")
    assert (sb.actual_duration or 0) > 0
    assert len((sb.audio or {}).get("dialogue") or []) == 2


async def test_shipin_tishi_cidan_cijingshuiping(client, auth, db):
    """视频提示词强化：meta.prompt 以分镜画面描述为核心并携带镜头要素。

    2026-10-20 用户反馈视频不按分镜内容生成——新构造应直接携带
    visual_description/机位角度/环境光影/镜头情绪/角色对白与一致性指令，
    不再仅依赖拆解锁定层快照。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, sb = await _make_project_with_storyboard(client, auth, db, unique_suffix())
    sb.visual_description = "雨夜巷口，陈默撑伞回头看向追来的周姨"
    sb.camera_angle = "平视"
    sb.lighting = "冷色路灯，湿地面反光"
    sb.audio = {"sfx": [], "emotion": "压抑"}
    sb.dialogue = "周姨：这么晚你去哪？\n陈默：找人。"
    await db.commit()

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/{sb.id}/video",
        headers=auth["headers"],
        json={"resolution": "720p", "fps": 24, "motion_strength": "medium", "use_end_frame": False},
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]
    row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
    prompt = (row.meta or {}).get("prompt") or ""
    assert "分镜画面描述：雨夜巷口" in prompt
    assert "机位角度：平视" in prompt
    assert "环境光影：冷色路灯" in prompt
    assert "镜头情绪：压抑" in prompt
    assert "角色对白（说话角色口部自然开合）" in prompt
    assert "周姨：这么晚你去哪？" in prompt
    assert "必须与首帧严格一致" in prompt

    # 收尾：等任务终态，避免遗留排队任务
    await wait_task_terminal(client, auth["headers"], task_id)
