"""分镜域业务服务模块（文件级说明）。

承载分镜工作流的业务逻辑：
- AI 拆解（读剧本 content.scenes，逐场景 LLM 生成 1~3 镜，失败规则降级固定 2 镜）
- 分镜列表 / 编辑（角色关联整体替换）/ 重排 / 拆分 / 合并
- 告警检查（总时长偏差、单镜超长、实体未强绑资产）
- 重生成计数（RD-25 达阈值引导进画布）与分镜投递画布（CV-01 studio_linked）

幂等语义：拆解为「全量重建」——每次拆解先删除项目既有分镜再按剧本全量重建。
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm_adapter import llm
from app.models import (
    AssetVariant,
    Canvas,
    CanvasEdge,
    CanvasNode,
    Character,
    Episode,
    Project,
    Scene,
    Script,
    Storyboard,
    StoryboardCharacter,
    User,
)
from app.services.script_service import (
    build_character_bible_prompt,
    extract_appearance_from_description,
    strip_json_fences,
    unwrap_scenes,
)

# 场景名匹配命中时的应用层置信度（storyboards 表无独立字段，仅作匹配策略语义记录）
SCENE_MATCH_CONFIDENCE = 0.9
# 分镜-角色关联的实体链接置信度（storyboard_characters.link_confidence）
LINK_CONFIDENCE = 0.8
# 单镜告警时长阈值（秒）
SHOT_DURATION_WARN = 10
# 总时长偏差告警阈值（相对项目 target_duration）
TOTAL_DURATION_TOLERANCE = 0.15
# 重生成次数达到该值时建议进入画布精修（RD-25）
REGEN_SUGGEST_THRESHOLD = 3
# 连续性检查（SB-06）场景跳变 suspect 判定的强连续词白名单：
# 两镜分属不同场景资产、但画面描述仍含连续词时，提示「疑似误拆场景」
_SCENE_CONTINUITY_WORDS = ("接着", "随后", "同一", "继续", "紧接", "仍然", "依旧")
# 目标时长自动调整（SB-07）的合法目标范围上限（秒）
AUTO_FIT_TARGET_MAX = 3600

# LLM 单场景拆解系统提示词（要求仅输出严格 JSON）；
# visual_description 参照 AI 漫剧行业分镜公式：[景别]+[角色+外貌特征]+[动作]+
# [场景]+[情绪关键词]，保证下游关键帧生图可直接消费；camera_movement 白名单
# 覆盖运镜 8 术语（固定/推/拉/摇/移/跟随/升降/旋转）
_SHOTS_SYSTEM_PROMPT = (
    "你是专业分镜师。请把给定剧本场景拆解为 1~3 个分镜镜头。\n"
    "仅输出严格 JSON（禁止 markdown 围栏与任何解释文字），格式：\n"
    '{"shots":[{"shot_type":"远景|全景|中景|近景|特写",'
    '"camera_movement":"固定|推|拉|摇|移|跟随|升降|旋转","duration":5,'
    '"visual_description":"画面描述","dialogue":[{"character":"角色名","line":"台词"}]}]}\n'
    "要求：\n"
    "- visual_description 按分镜公式组织：景别 + 角色（含与设定一致的外貌特征）"
    "+ 动作 + 场景要素 + 情绪关键词，一句话可直接转生图提示词；\n"
    "- 画面中角色的外貌/服装必须与剧本角色设定保持一致，禁止换装换发型；\n"
    "- camera_movement 从白名单中按镜头意图选择（推=强调主体、拉=揭示环境、"
    "摇=跟随扫视、移=建立空间、跟随=角色跟拍、升降=氛围强调、旋转=眩晕/冲突）；\n"
    "- duration 按节奏取值：快卡点镜头 2~3 秒、常规叙事镜头 4~6 秒、"
    "单个镜头不超过 10 秒；\n"
    "- duration 为整数秒；无对白的镜头 dialogue 为空数组；全部使用中文。"
)


async def _get_sb_or_404(db: AsyncSession, project_id: int, storyboard_id: int) -> Storyboard:
    """获取属于指定项目的分镜，不存在时抛 404。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。
        storyboard_id: 分镜 ID。

    Returns:
        Storyboard: 分镜 ORM 对象。

    Raises:
        HTTPException: 404 —— 分镜不存在或不属于该项目。
    """
    sb = (
        await db.execute(
            select(Storyboard).where(
                Storyboard.id == storyboard_id, Storyboard.project_id == project_id)
        )
    ).scalar_one_or_none()
    if sb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分镜不存在")
    return sb


def _dialogue_to_text(dialogue: Any) -> str:
    """把对白数组规整为多行文本（每行 "角色名：台词"）。

    Args:
        dialogue: [{"character","line"}] 数组，或字符串，或 None。

    Returns:
        str: 多行对白文本；无有效对白时返回空串。
    """
    if isinstance(dialogue, str):
        return dialogue.strip()
    lines = []
    for d in (dialogue or []):
        if isinstance(d, dict) and str(d.get("line") or "").strip():
            speaker = str(d.get("character") or "旁白").strip() or "旁白"
            lines.append(f"{speaker}：{str(d['line']).strip()}")
        elif isinstance(d, str) and d.strip():
            lines.append(d.strip())
    return "\n".join(lines)


def _fallback_shots(scene: Dict[str, Any]) -> List[Dict[str, Any]]:
    """规则降级拆镜（离线降级）：LLM 失败时每场景固定拆 2 镜、每镜 5 秒。

    Args:
        scene: 剧本场景 dict（title/plot/dialogue/duration_sec）。

    Returns:
        List[Dict[str, Any]]: 2 个镜头 dict。
    """
    dialogue = scene.get("dialogue") or []
    if isinstance(dialogue, str):
        dialogue = [{"character": "旁白", "line": ln.strip()} for ln in dialogue.splitlines() if ln.strip()]
    half = (len(dialogue) + 1) // 2
    title = str(scene.get("title") or "场景")
    plot = str(scene.get("plot") or "")[:60]
    return [
        {
            "shot_type": "中景", "camera_movement": "固定", "duration": 5,
            "visual_description": f"{title}：{plot}（规则降级拆镜·前段）",
            "dialogue": dialogue[:half],
        },
        {
            "shot_type": "近景", "camera_movement": "推", "duration": 5,
            "visual_description": f"{title}：情绪特写推进（规则降级拆镜·后段）",
            "dialogue": dialogue[half:],
        },
    ]


async def _llm_shots(scene: Dict[str, Any]) -> List[Dict[str, Any]]:
    """LLM 单场景拆解：生成 1~3 个分镜；异常或输出非法时规则降级。

    Args:
        scene: 剧本场景 dict。

    Returns:
        List[Dict[str, Any]]: 规整后的镜头列表（每项含 shot_type/camera_movement/
            duration/visual_description/dialogue）。

    Raises:
        无：内部捕获一切异常（含网络/解析错误），保证降级路径可达。
    """
    user_content = (
        f"场景标题：{scene.get('title', '')}\n"
        f"场景剧情：{scene.get('plot', '')}\n"
        f"场景对白：{json.dumps(scene.get('dialogue') or [], ensure_ascii=False)}\n"
        f"场景时长：{scene.get('duration_sec', 30)} 秒"
    )
    try:
        text = await llm.chat(
            None,
            [
                {"role": "system", "content": _SHOTS_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            agent_name="storyboard_decompose",
            json_mode=True,
            temperature=0.3,
        )
        data = json.loads(strip_json_fences(text))
        raw_shots = data.get("shots") if isinstance(data, dict) else None
        if not isinstance(raw_shots, list) or not raw_shots:
            return _fallback_shots(scene)
        shots: List[Dict[str, Any]] = []
        for sh in raw_shots[:3]:  # 最多取 3 镜
            if not isinstance(sh, dict):
                continue
            try:
                duration = max(1, int(sh.get("duration") or 5))
            except (TypeError, ValueError):
                duration = 5
            shots.append({
                "shot_type": str(sh.get("shot_type") or "中景")[:20],
                "camera_movement": str(sh.get("camera_movement") or "固定")[:20],
                "duration": duration,
                "visual_description": str(sh.get("visual_description") or scene.get("plot") or ""),
                "dialogue": sh.get("dialogue") or [],
            })
        return shots or _fallback_shots(scene)
    except Exception:  # noqa: BLE001 —— LLM 不可用时必须走规则降级
        return _fallback_shots(scene)


def _match_scene(scene: Dict[str, Any], scene_rows: List[Scene]) -> Tuple[Optional[int], str]:
    """按场景名匹配场景资产（精确相等或互相包含）。

    匹配命中置信度 0.9（应用层匹配策略语义，storyboards 表无独立字段存储；
    未匹配时 scene_id 落 NULL，由告警接口标记「实体未强绑资产」）。

    Args:
        scene: 剧本场景 dict（取 title）。
        scene_rows: 项目内场景资产行列表。

    Returns:
        Tuple[Optional[int], str]: (scene_id 或 None, 参与提示词的场景名)。
    """
    title = str(scene.get("title") or "").strip()
    if title:
        for row in scene_rows:
            if row.name == title or row.name in title or title in row.name:
                return row.id, row.name
    return None, title or "未匹配场景"


def _match_characters(dialogue_text: str, char_rows: List[Character]) -> List[Character]:
    """匹配对白中出现的角色（说话人名匹配角色 name 或 aliases，RD-24 实体链接）。

    Args:
        dialogue_text: 多行对白文本（每行 "角色名：台词"）。
        char_rows: 项目内未删角色资产列表。

    Returns:
        List[Character]: 命中的角色列表（按对白出现顺序去重）。
    """
    hits: List[Character] = []
    if not dialogue_text:
        return hits
    for line in dialogue_text.splitlines():
        m = re.match(r"^([^：:]{1,20})[：:]", line.strip())
        if not m:
            continue
        speaker = m.group(1).strip()
        for ch in char_rows:
            aliases = ch.aliases if isinstance(ch.aliases, list) else []
            if ch.name == speaker or speaker in aliases:
                if ch not in hits:
                    hits.append(ch)
    return hits


def _build_prompt_locked(
    project: Project, scene_name: str, characters: List[Character],
    visual_description: str, shot_type: str, camera_movement: str,
) -> str:
    """构造五要素提示词锁定层模板（RD-04：风格/场景/角色/画面/景别运镜）。

    角色要素注入「角色圣经固定前缀」（AI 漫剧行业惯例：每条人物提示词以
    角色圣经开头，只改动作/场景）——外貌来源为 characters.description 的
    「外观：」段（build_profile_description 拼接格式），无该段时回退姓名，
    每角色截 60 字，保证跨镜头生图一致性。

    Args:
        project: 项目对象（取全局风格锚点）。
        scene_name: 场景名。
        characters: 绑定的角色 ORM 列表（外貌圣经来源）。
        visual_description: 画面描述。
        shot_type: 景别。
        camera_movement: 运镜。

    Returns:
        str: 五要素锁定层提示词。

    Raises:
        无：纯字符串拼装，角色字段缺失按姓名兜底。
    """
    char_parts: List[str] = []
    for ch in characters:
        appearance = extract_appearance_from_description(ch.description)
        bible = build_character_bible_prompt(ch.name, ch.age, appearance)
        char_parts.append(bible[:60])
    names_text = "；".join(char_parts) if char_parts else "无"
    return (
        f"统一风格：{project.style or '默认风格'}；场景：{scene_name}；"
        f"角色（外貌圣经，跨镜一致）：{names_text}；画面：{visual_description or '无'}；"
        f"景别运镜：{shot_type}/{camera_movement}"
    )


async def _build_out_list(db: AsyncSession, sbs: List[Storyboard]) -> List[Dict[str, Any]]:
    """把分镜 ORM 列表转换为输出 dict（批量联查角色关联与场景名，避免 N+1）。

    Args:
        db: 异步数据库会话。
        sbs: 分镜 ORM 列表。

    Returns:
        List[Dict[str, Any]]: 输出列表，每项含 characters 与 scene_name。
    """
    if not sbs:
        return []
    ids = [sb.id for sb in sbs]
    # 批量取分镜-角色关联（联角色名）
    link_rows = (
        await db.execute(
            select(StoryboardCharacter, Character.name)
            .join(Character, Character.id == StoryboardCharacter.character_id)
            .where(StoryboardCharacter.storyboard_id.in_(ids))
            .order_by(StoryboardCharacter.id.asc())
        )
    ).all()
    links_by_sb: Dict[int, List[Dict[str, Any]]] = {}
    for link, cname in link_rows:
        links_by_sb.setdefault(link.storyboard_id, []).append({
            "character_id": link.character_id,
            "name": cname,
            "variant_id": link.variant_id,
            "link_confidence": float(link.link_confidence) if link.link_confidence is not None else None,
        })
    # 批量取场景名
    scene_ids = {sb.scene_id for sb in sbs if sb.scene_id}
    scene_names: Dict[int, str] = {}
    if scene_ids:
        rows = (
            await db.execute(select(Scene.id, Scene.name).where(Scene.id.in_(scene_ids)))
        ).all()
        scene_names = {sid: name for sid, name in rows}

    out: List[Dict[str, Any]] = []
    for sb in sbs:
        out.append({
            "id": sb.id,
            "sequence": sb.sequence,
            "episode_id": sb.episode_id,
            "script_id": sb.script_id,
            "shot_type": sb.shot_type,
            "camera_movement": sb.camera_movement,
            "duration": sb.duration,
            "visual_description": sb.visual_description,
            "dialogue": sb.dialogue,
            "scene_id": sb.scene_id,
            "scene_name": scene_names.get(sb.scene_id),
            "prompt_locked": sb.prompt_locked,
            "prompt_editable": sb.prompt_editable,
            "keyframe_url": sb.keyframe_url,
            "end_frame_url": sb.end_frame_url,
            "video_url": sb.video_url,
            "audio_url": sb.audio_url,
            "status": sb.status,
            "regen_count": sb.regen_count,
            "characters": links_by_sb.get(sb.id, []),
        })
    return out


async def _resequence(db: AsyncSession, project_id: int) -> None:
    """按当前 sequence 顺序把项目分镜重写为连续 1..n（拆分/合并/重排后统一调用）。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。
    """
    sbs = (
        await db.execute(
            select(Storyboard)
            .where(Storyboard.project_id == project_id)
            .order_by(Storyboard.sequence.asc(), Storyboard.id.asc())
        )
    ).scalars().all()
    for idx, sb in enumerate(sbs, start=1):
        sb.sequence = idx


async def decompose_storyboards(db: AsyncSession, project: Project) -> Dict[str, Any]:
    """AI 拆解：读剧本 content.scenes 逐场景生成 1~3 镜并建 storyboards 行。

    - LLM 失败走规则降级（每场景固定 2 镜、每镜 5s，离线降级路径）；
    - 剧本按项目全部 Script 行合并读取（默认行 episode_id NULL 排最前，其余按剧集
      sequence 升序），兼容分阶段流水线每集一行的落库形态；
    - 每镜 episode_id 随来源剧本行落库（前端可按剧集过滤分镜）；
    - scene_id 按场景名匹配场景资产（未匹配落 NULL）；
    - storyboard_characters 关联对白中出现的角色（link_confidence=0.8）；
    - prompt_locked 写入五要素模板；status='pending'；project.status='storyboard'；
    - 幂等为全量重建语义：先删除项目既有分镜再重建。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。

    Returns:
        Dict[str, Any]: {"total": 新建分镜数, "degraded": 是否发生过规则降级}。

    Raises:
        HTTPException: 400 —— 项目无剧本或剧本无场景数据。
    """
    # 读取项目全部剧本行（下游接收修复 2026-10-15）：分阶段流水线按剧集落库
    # （每集一行，uk_scripts_project = (project_id, episode_id)），旧快速路径
    # 为单行默认行（episode_id NULL）。此前 scalar_one_or_none 在行数 >1 时
    # 抛 MultipleResultsFound（500），导致流水线产出无法被下游分镜接收。
    if (
        await db.execute(
            select(Script).where(Script.project_id == project.id).limit(1)
        )
    ).scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="项目尚无剧本，请先生成剧本")

    # 剧集序号映射：默认行（episode_id NULL）视为第 0 集排最前，
    # 其余按所属剧集 sequence 升序、同序按 Script.id 稳定排序
    episode_order: Dict[int, int] = {
        ep.id: (ep.sequence if ep.sequence is not None else 0)
        for ep in (
            await db.execute(select(Episode).where(Episode.project_id == project.id))
        ).scalars().all()
    }
    script_rows = (
        await db.execute(
            select(Script).where(Script.project_id == project.id).order_by(Script.id.asc())
        )
    ).scalars().all()
    script_rows.sort(
        key=lambda s: (
            0 if s.episode_id is None else episode_order.get(s.episode_id, 10**9),
            s.id,
        )
    )

    # 逐行解包场景并携带来源行（跨剧集按序合并为一整份可拆解序列）
    scene_entries: List[Tuple[Script, Dict[str, Any]]] = []
    for row in script_rows:
        for sc in unwrap_scenes(row.content):
            scene_entries.append((row, sc))
    if not scene_entries:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="剧本中无场景数据，无法拆解")

    # 项目内场景 / 角色资产（用于强绑与实体链接）
    scene_rows = (
        await db.execute(select(Scene).where(Scene.project_id == project.id))
    ).scalars().all()
    char_rows = (
        await db.execute(
            select(Character).where(
                Character.project_id == project.id, Character.deleted_at.is_(None))
        )
    ).scalars().all()

    # 幂等：全量重建语义——先删项目既有分镜（storyboard_characters 由外键级联删除）
    await db.execute(delete(Storyboard).where(Storyboard.project_id == project.id))

    seq = 0
    degraded = False
    for src_script, scene in scene_entries:
        shots = await _llm_shots(scene)
        if any("规则降级" in str(s.get("visual_description") or "") for s in shots):
            degraded = True
        scene_id, scene_name = _match_scene(scene, scene_rows)
        for shot in shots:
            seq += 1
            shot_type = shot["shot_type"]
            camera_movement = shot["camera_movement"]
            visual_description = shot["visual_description"]
            dialogue_text = _dialogue_to_text(shot.get("dialogue"))
            sb = Storyboard(
                project_id=project.id,
                script_id=src_script.id,
                episode_id=src_script.episode_id,
                sequence=seq,
                shot_type=shot_type,
                camera_movement=camera_movement,
                duration=shot["duration"],
                visual_description=visual_description or None,
                dialogue=dialogue_text or None,
                scene_id=scene_id,
                prompt_locked="",   # 关联角色确定后回填
                prompt_editable="",
                status="pending",
            )
            db.add(sb)
            await db.flush()  # 取 sb.id 供角色关联使用

            # 分镜-角色关联：对白中出现的角色（RD-24 强绑，link_confidence=0.8）
            linked = _match_characters(dialogue_text, char_rows)
            for ch in linked:
                db.add(StoryboardCharacter(
                    storyboard_id=sb.id,
                    character_id=ch.id,
                    variant_id=None,
                    link_confidence=LINK_CONFIDENCE,
                ))
            # 五要素锁定层提示词（RD-04）：角色要素注入外貌圣经固定前缀
            sb.prompt_locked = _build_prompt_locked(
                project, scene_name, linked,
                visual_description, shot_type, camera_movement,
            )

    project.status = "storyboard"
    await db.commit()
    return {"total": seq, "degraded": degraded}


async def list_storyboards(
    db: AsyncSession, project: Project, episode_id: Optional[int]
) -> List[Dict[str, Any]]:
    """查询项目分镜列表（按 sequence 升序，含角色关联与场景名）。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        episode_id: 剧集过滤（可空）。

    Returns:
        List[Dict[str, Any]]: 分镜输出列表。
    """
    conds = [Storyboard.project_id == project.id]
    if episode_id is not None:
        conds.append(Storyboard.episode_id == episode_id)
    sbs = (
        await db.execute(
            select(Storyboard).where(*conds).order_by(Storyboard.sequence.asc())
        )
    ).scalars().all()
    return await _build_out_list(db, list(sbs))


async def update_storyboard(
    db: AsyncSession, project: Project, storyboard_id: int, req: Any
) -> Dict[str, Any]:
    """更新分镜字段；character_links 提供时整体替换角色关联。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        storyboard_id: 分镜 ID。
        req: StoryboardUpdateRequest。

    Returns:
        Dict[str, Any]: 更新后的分镜输出（含 characters / scene_name）。

    Raises:
        HTTPException: 400 —— scene_id 不属于本项目 / character_links 引用非法 /
            角色重复；404 —— 分镜不存在。
    """
    sb = await _get_sb_or_404(db, project.id, storyboard_id)

    # 场景强绑校验：scene_id 须为本项目场景
    if req.scene_id is not None:
        scene = await db.get(Scene, req.scene_id)
        if scene is None or scene.project_id != project.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="场景不存在或不属于本项目")
        sb.scene_id = req.scene_id

    for field in ("shot_type", "camera_movement", "duration",
                  "visual_description", "dialogue", "prompt_editable"):
        value = getattr(req, field)
        if value is not None:
            setattr(sb, field, value)

    # character_links 提供则整体替换关联（先删后插）
    if req.character_links is not None:
        await db.execute(
            delete(StoryboardCharacter).where(StoryboardCharacter.storyboard_id == sb.id)
        )
        seen: set = set()
        for link in req.character_links:
            if link.character_id in seen:
                # (storyboard_id, character_id) 唯一索引：请求内去重
                continue
            seen.add(link.character_id)
            ch = await db.get(Character, link.character_id)
            if ch is None or ch.project_id != project.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"角色 {link.character_id} 不属于本项目",
                )
            if link.variant_id is not None:
                variant = await db.get(AssetVariant, link.variant_id)
                if variant is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"造型变体 {link.variant_id} 不存在",
                    )
            db.add(StoryboardCharacter(
                storyboard_id=sb.id,
                character_id=link.character_id,
                variant_id=link.variant_id,
                link_confidence=link.link_confidence,
            ))

    await db.commit()
    return (await _build_out_list(db, [sb]))[0]


async def reorder_storyboards(db: AsyncSession, project: Project, ordered_ids: List[int]) -> int:
    """按给定 ID 顺序重写 sequence（1..n）。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        ordered_ids: 目标顺序的分镜 ID 列表。

    Returns:
        int: 重排的分镜总数。

    Raises:
        HTTPException: 400 —— ordered_ids 未恰好覆盖项目全部分镜（缺漏/多余/重复）。
    """
    sbs = (
        await db.execute(select(Storyboard).where(Storyboard.project_id == project.id))
    ).scalars().all()
    current_ids = [sb.id for sb in sbs]
    # 严格校验：恰好是项目全部分镜的一个排列（避免静默丢失镜头）
    if len(ordered_ids) != len(current_ids) or set(ordered_ids) != set(current_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ordered_ids 必须恰好包含项目全部分镜 ID 且不重复",
        )
    id_to_sb = {sb.id: sb for sb in sbs}
    for idx, sid in enumerate(ordered_ids, start=1):
        id_to_sb[sid].sequence = idx
    await db.commit()
    return len(current_ids)


async def split_storyboard(
    db: AsyncSession, project: Project, storyboard_id: int, at_line: int
) -> List[Dict[str, Any]]:
    """按对白行拆分为两镜：前 at_line 行留本镜，其余行进新镜（插在其后，sequence 重排）。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        storyboard_id: 分镜 ID。
        at_line: 拆分行号（1 起）。

    Returns:
        List[Dict[str, Any]]: [拆分后的本镜, 新镜]。

    Raises:
        HTTPException: 400 —— 对白不足两行或行号越界；404 —— 分镜不存在。
    """
    sb = await _get_sb_or_404(db, project.id, storyboard_id)
    lines = [ln for ln in (sb.dialogue or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该分镜对白不足两行，无法拆分")
    if at_line < 1 or at_line >= len(lines):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"at_line 须介于 1 与 {len(lines) - 1} 之间",
        )

    first_text = "\n".join(lines[:at_line])
    second_text = "\n".join(lines[at_line:])
    # 按行数比例分配时长，两侧各保底 1s
    d1 = max(1, round(sb.duration * at_line / len(lines)))
    d2 = max(1, sb.duration - d1)

    # 平移后续镜头 sequence（避开 (project, episode, sequence) 唯一索引冲突），
    # 新镜插到本镜之后，最后统一重排 1..n
    await db.execute(
        update(Storyboard)
        .where(Storyboard.project_id == project.id, Storyboard.sequence > sb.sequence)
        .values(sequence=Storyboard.sequence + 1000000)
    )

    new_sb = Storyboard(
        project_id=project.id,
        episode_id=sb.episode_id,
        script_id=sb.script_id,
        sequence=sb.sequence + 1,
        shot_type=sb.shot_type,
        camera_movement=sb.camera_movement,
        duration=d2,
        visual_description=sb.visual_description,
        dialogue=second_text or None,
        scene_id=sb.scene_id,
        prompt_locked=sb.prompt_locked,
        prompt_editable="",
        status="pending",
        regen_count=0,
    )
    db.add(new_sb)
    await db.flush()  # 取新镜 ID 以复制角色关联

    # 角色关联复制到新镜（两镜沿用同一组绑定角色）
    old_links = (
        await db.execute(
            select(StoryboardCharacter).where(StoryboardCharacter.storyboard_id == sb.id)
        )
    ).scalars().all()
    for link in old_links:
        db.add(StoryboardCharacter(
            storyboard_id=new_sb.id,
            character_id=link.character_id,
            variant_id=link.variant_id,
            link_confidence=link.link_confidence,
        ))

    sb.dialogue = first_text or None
    sb.duration = d1

    await _resequence(db, project.id)
    await db.commit()
    return await _build_out_list(db, [sb, new_sb])


async def merge_storyboards(
    db: AsyncSession, project: Project, storyboard_id: int, other_id: int
) -> Dict[str, Any]:
    """合并两镜：对白拼接、时长相加、并入角色关联，删除 other 后重排 sequence。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        storyboard_id: 保留的分镜 ID。
        other_id: 被合并删除的分镜 ID。

    Returns:
        Dict[str, Any]: 合并后的分镜输出。

    Raises:
        HTTPException: 400 —— 不能与自身合并；404 —— 分镜不存在。
    """
    if storyboard_id == other_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能与自身合并")
    sb = await _get_sb_or_404(db, project.id, storyboard_id)
    other = await _get_sb_or_404(db, project.id, other_id)

    # 对白拼接（换行连接）、时长相加
    merged_dialogue = "\n".join(
        t for t in [(sb.dialogue or "").strip(), (other.dialogue or "").strip()] if t
    )
    sb.dialogue = merged_dialogue or None
    sb.duration = sb.duration + other.duration

    # other 的角色关联并入本镜（按 character_id 去重，撞唯一索引）
    existing_char_ids = {
        row for row in (
            await db.execute(
                select(StoryboardCharacter.character_id).where(
                    StoryboardCharacter.storyboard_id == sb.id)
            )
        ).scalars().all()
    }
    other_links = (
        await db.execute(
            select(StoryboardCharacter).where(StoryboardCharacter.storyboard_id == other.id)
        )
    ).scalars().all()
    for link in other_links:
        if link.character_id in existing_char_ids:
            continue
        db.add(StoryboardCharacter(
            storyboard_id=sb.id,
            character_id=link.character_id,
            variant_id=link.variant_id,
            link_confidence=link.link_confidence,
        ))

    # 删除 other（storyboard_characters 由外键级联删除）
    await db.delete(other)
    await db.flush()  # 先落 DELETE（session autoflush=False），避免重排查询读到待删行
    await _resequence(db, project.id)
    await db.commit()
    return (await _build_out_list(db, [sb]))[0]


async def storyboard_warnings(db: AsyncSession, project: Project) -> Dict[str, Any]:
    """分镜告警检查。

    告警规则（中文注释）：
    1. 全部分镜时长合计与项目 target_duration 偏差 > ±15% → warning；
    2. 单镜时长 > 10s → warning；
    3. 分镜 scene_id 为 NULL（实体未强绑资产）→ error。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。

    Returns:
        Dict[str, Any]: {"total_duration","target_duration","over_ratio","warnings"}。
    """
    sbs = (
        await db.execute(
            select(Storyboard)
            .where(Storyboard.project_id == project.id)
            .order_by(Storyboard.sequence.asc())
        )
    ).scalars().all()
    total = sum(sb.duration for sb in sbs)
    target = project.target_duration
    over_ratio: Optional[float] = None
    warnings: List[Dict[str, Any]] = []

    if target:
        over_ratio = round((total - target) / target, 4)
        if abs(over_ratio) > TOTAL_DURATION_TOLERANCE:
            warnings.append({
                "storyboard_id": None,
                "level": "warning",
                "message": f"分镜总时长 {total}s 与目标时长 {target}s 偏差 {over_ratio:.0%}，超过 ±15%",
            })
    for sb in sbs:
        if sb.duration > SHOT_DURATION_WARN:
            warnings.append({
                "storyboard_id": sb.id,
                "level": "warning",
                "message": f"镜头#{sb.sequence} 时长 {sb.duration}s 超过 {SHOT_DURATION_WARN} 秒",
            })
        if sb.scene_id is None:
            warnings.append({
                "storyboard_id": sb.id,
                "level": "error",
                "message": f"镜头#{sb.sequence} 实体未强绑资产",
            })
    return {
        "total_duration": total,
        "target_duration": target,
        "over_ratio": over_ratio,
        "warnings": warnings,
    }


async def continuity_check(
    db: AsyncSession, project: Project, episode_id: Optional[int] = None
) -> Dict[str, Any]:
    """分镜连续性检查（SB-06，RD-23「宁可漏报不误报」的窄口径规则）。

    检查规则（全部基于 sequence 相邻镜头对）：
    a) 场景切换提示：相邻两镜 scene_id 均非空且不同 → info 提示；仅当两镜
       画面描述含强连续词（_SCENE_CONTINUITY_WORDS 白名单）时升级 suspect
       （疑似同一地点被误拆到不同场景资产）；
    b) 服装变体冲突：同一角色在相邻出现的镜头（中间无该角色出现间隔）
       绑定的服装变体不同（两侧均非空才判定，未绑变体不判）→ suspect；
    c) 提示词关键词缺失：镜 N 提示词包含某角色名、镜 N+1 对白明确点名
       该角色（行首 "角色名：" 格式）但镜 N+1 提示词不包含 → info 提示
       （仅做「出现/缺席」二元判断）。

    故意不做的规则（误报风险高）：道具连续性、时间线/光照冲突、跨场景
    服装变更（换场换装合理）、别名扩展匹配等。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        episode_id: 剧集过滤（可空，None = 项目全部分镜；口径与列表接口一致）。

    Returns:
        Dict[str, Any]: {"items": [{storyboard_id, type, message, confidence}],
            "checked_pairs": 检查的相邻镜头对数量}；无问题时 items 为空数组。

    Raises:
        无：查询失败等异常向上抛出（由全局异常处理兜底）。
    """
    conds = [Storyboard.project_id == project.id]
    if episode_id is not None:
        conds.append(Storyboard.episode_id == episode_id)
    sbs = (
        await db.execute(
            select(Storyboard).where(*conds).order_by(Storyboard.sequence.asc())
        )
    ).scalars().all()
    items: List[Dict[str, Any]] = []
    if not sbs:
        return {"items": items, "checked_pairs": 0}

    # 场景名映射（提示文案用；场景行已删时回退显示 ID）
    scene_ids = {sb.scene_id for sb in sbs if sb.scene_id}
    scene_names: Dict[int, str] = {}
    if scene_ids:
        rows = (
            await db.execute(select(Scene.id, Scene.name).where(Scene.id.in_(scene_ids)))
        ).all()
        scene_names = {sid: name for sid, name in rows}

    # 项目未删除角色资产（规则 b 文案 / 规则 c 名字白名单，避免对白误匹配非资产名）
    char_rows = (
        await db.execute(
            select(Character).where(
                Character.project_id == project.id, Character.deleted_at.is_(None))
        )
    ).scalars().all()
    char_names = {ch.id: ch.name for ch in char_rows}

    # ---- 规则 a / c：相邻镜头对循环 ----
    for prev, cur in zip(sbs, sbs[1:]):
        # a) 场景切换（两镜均强绑场景且不同）
        if prev.scene_id and cur.scene_id and prev.scene_id != cur.scene_id:
            text = (prev.visual_description or "") + (cur.visual_description or "")
            suspect = any(w in text for w in _SCENE_CONTINUITY_WORDS)
            items.append({
                "storyboard_id": cur.id,
                "type": "scene_jump",
                "message": (
                    f"镜 #{prev.sequence}→#{cur.sequence} 场景切换："
                    f"{scene_names.get(prev.scene_id, prev.scene_id)} → "
                    f"{scene_names.get(cur.scene_id, cur.scene_id)}"
                    + ("，但画面描述含连续词，请确认是否误拆场景" if suspect else "")
                ),
                "confidence": "suspect" if suspect else "info",
            })
        # c) 提示词缺失（镜 N 提示词含角色名 → 镜 N+1 对白点名但提示词缺席）
        prev_prompt = (prev.prompt_locked or "") + "\n" + (prev.prompt_editable or "")
        cur_prompt = (cur.prompt_locked or "") + "\n" + (cur.prompt_editable or "")
        if cur.dialogue:
            for ch in char_rows:
                name = ch.name
                if not name or name in cur_prompt or name not in prev_prompt:
                    continue
                named = any(
                    ln.strip().startswith(f"{name}：") or ln.strip().startswith(f"{name}:")
                    for ln in cur.dialogue.splitlines()
                )
                if named:
                    items.append({
                        "storyboard_id": cur.id,
                        "type": "prompt_missing",
                        "message": (
                            f"镜 #{cur.sequence} 对白明确点名角色「{name}」，"
                            f"但其提示词未包含该角色（镜 #{prev.sequence} 提示词包含）"
                        ),
                        "confidence": "info",
                    })

    # ---- 规则 b：同一角色相邻出现镜头的服装变体冲突 ----
    link_rows = (
        await db.execute(
            select(StoryboardCharacter).where(
                StoryboardCharacter.storyboard_id.in_([sb.id for sb in sbs]))
        )
    ).scalars().all()
    seq_by_id = {sb.id: sb.sequence for sb in sbs}
    # character_id -> [(sequence, storyboard_id, variant_id)]（按出现顺序整理）
    appear: Dict[int, List[Tuple[int, int, Optional[int]]]] = {}
    for link in link_rows:
        seq = seq_by_id.get(link.storyboard_id)
        if seq is not None:
            appear.setdefault(link.character_id, []).append(
                (seq, link.storyboard_id, link.variant_id)
            )
    # 变体名映射（文案可读性；变体已删时回退显示 ID）
    variant_ids = {v for lst in appear.values() for (_, _, v) in lst if v}
    variant_names: Dict[int, str] = {}
    if variant_ids:
        vrows = (
            await db.execute(
                select(AssetVariant.id, AssetVariant.name).where(AssetVariant.id.in_(variant_ids))
            )
        ).all()
        variant_names = {vid: name for vid, name in vrows}
    for char_id, lst in appear.items():
        lst.sort(key=lambda t: t[0])
        for (seq_a, sb_a, var_a), (seq_b, sb_b, var_b) in zip(lst, lst[1:]):
            if var_a and var_b and var_a != var_b:
                items.append({
                    "storyboard_id": sb_b,
                    "type": "outfit_conflict",
                    "message": (
                        f"角色「{char_names.get(char_id, char_id)}」在镜 #{seq_a} 与 "
                        f"#{seq_b} 绑定的服装变体不同（{variant_names.get(var_a, var_a)} → "
                        f"{variant_names.get(var_b, var_b)}），请确认服装连续性"
                    ),
                    "confidence": "suspect",
                })

    return {"items": items, "checked_pairs": max(0, len(sbs) - 1)}


async def auto_fit_duration(
    db: AsyncSession, project: Project, target_seconds: int,
    episode_id: Optional[int] = None,
) -> Dict[str, Any]:
    """目标时长自动调整（SB-07）：分镜时长按比例等比缩放至目标总时长。

    - 每镜新时长 = max(1, round(原时长 × target / 当前合计))（单镜保底 1 秒，
      因此缩放后合计 ≈ 目标值而非严格相等）；
    - 直接改写 storyboards.duration 落库（actual_duration 不动）；
    - 目标值须在 1~3600 秒，否则 400。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        target_seconds: 目标总时长（秒）。
        episode_id: 剧集过滤（可空，None = 项目全部分镜）。

    Returns:
        Dict[str, Any]: {"before_total", "after_total", "target_seconds",
            "changed", "per_shot": [{id, before, after}]}。

    Raises:
        HTTPException: 400 —— target_seconds 越界（1~3600 之外）或项目/剧集
            下无分镜可调整。
    """
    if not (1 <= target_seconds <= AUTO_FIT_TARGET_MAX):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"target_seconds 须在 1~{AUTO_FIT_TARGET_MAX} 秒之间",
        )
    conds = [Storyboard.project_id == project.id]
    if episode_id is not None:
        conds.append(Storyboard.episode_id == episode_id)
    sbs = (
        await db.execute(
            select(Storyboard).where(*conds).order_by(Storyboard.sequence.asc())
        )
    ).scalars().all()
    if not sbs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="暂无分镜，无法调整时长"
        )

    before_total = sum(sb.duration for sb in sbs)
    scale = target_seconds / before_total
    per_shot: List[Dict[str, Any]] = []
    changed = 0
    for sb in sbs:
        after = max(1, round(sb.duration * scale))
        if after != sb.duration:
            changed += 1
        per_shot.append({"id": sb.id, "before": sb.duration, "after": after})
        sb.duration = after
    await db.commit()
    return {
        "before_total": before_total,
        "after_total": sum(p["after"] for p in per_shot),
        "target_seconds": target_seconds,
        "changed": changed,
        "per_shot": per_shot,
    }


async def regen_storyboard(db: AsyncSession, project: Project, storyboard_id: int) -> Dict[str, Any]:
    """分镜重生成计数：regen_count += 1；达到阈值时建议进入画布精修（RD-25）。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        storyboard_id: 分镜 ID。

    Returns:
        Dict[str, Any]: {"regen_count", "suggest_canvas"}。

    Raises:
        HTTPException: 404 —— 分镜不存在。
    """
    sb = await _get_sb_or_404(db, project.id, storyboard_id)
    sb.regen_count += 1
    await db.commit()
    return {"regen_count": sb.regen_count, "suggest_canvas": sb.regen_count >= REGEN_SUGGEST_THRESHOLD}


async def deliver_to_canvas(
    db: AsyncSession, user: User, project: Project, storyboard_id: Optional[int]
) -> int:
    """分镜投递画布：建 studio_linked 画布 + text/asset_ref/generate 三节点 + 两条连线。

    节点结构：
    - text：data={"text": 分镜画面描述}；
    - asset_ref：config={"asset_type":"character","asset_id":第一个绑定角色 ID 或 null}；
    - generate：config={"task_type":"image"}；
    - 连线：text→generate、asset_ref→generate。

    Args:
        db: 异步数据库会话。
        user: 当前用户（画布拥有者）。
        project: 已校验归属的项目。
        storyboard_id: 来源分镜 ID（缺省取项目第一镜）。

    Returns:
        int: 新建画布 ID。

    Raises:
        HTTPException: 400 —— 项目尚无分镜；404 —— 指定分镜不存在。
    """
    if storyboard_id is not None:
        sb = await _get_sb_or_404(db, project.id, storyboard_id)
    else:
        sb = (
            await db.execute(
                select(Storyboard)
                .where(Storyboard.project_id == project.id)
                .order_by(Storyboard.sequence.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if sb is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="项目尚无分镜，无法投递画布")

    # 第一个绑定的角色资产 ID（asset_ref 节点引用；无绑定则落 null）
    first_char_id = (
        await db.execute(
            select(StoryboardCharacter.character_id)
            .where(StoryboardCharacter.storyboard_id == sb.id)
            .order_by(StoryboardCharacter.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    canvas = Canvas(
        user_id=user.id,
        project_id=project.id,
        title=f"{project.title}·分镜精修",
        type="studio_linked",
        source_storyboard_id=sb.id,
    )
    db.add(canvas)
    await db.flush()  # 取 canvas.id 供节点外键使用

    node_text = CanvasNode(
        canvas_id=canvas.id, node_type="text",
        position_x=100.0, position_y=100.0,
        data={"text": sb.visual_description or ""},
    )
    node_asset = CanvasNode(
        canvas_id=canvas.id, node_type="asset_ref",
        position_x=100.0, position_y=320.0,
        config={"asset_type": "character", "asset_id": first_char_id},
    )
    node_gen = CanvasNode(
        canvas_id=canvas.id, node_type="generate",
        position_x=420.0, position_y=210.0,
        config={"task_type": "image"},
    )
    db.add_all([node_text, node_asset, node_gen])
    await db.flush()  # 取节点 ID 供连线使用

    db.add_all([
        CanvasEdge(canvas_id=canvas.id, source_node_id=node_text.id, target_node_id=node_gen.id),
        CanvasEdge(canvas_id=canvas.id, source_node_id=node_asset.id, target_node_id=node_gen.id),
    ])
    await db.commit()
    return canvas.id
