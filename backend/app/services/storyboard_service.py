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
from sqlalchemy import delete, func, select, update
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
    Prop,
    Scene,
    Script,
    Storyboard,
    StoryboardCharacter,
    StoryboardProp,
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
# 分镜-道具关联的实体链接置信度（storyboard_props.link_confidence，
# 文本包含匹配弱于角色说话人匹配，取 0.6）
LINK_CONFIDENCE_PROP = 0.6
# 机位角度白名单（方案 B 扩展：分镜「镜头语言」三要素补齐）
CAMERA_ANGLES = ("平视", "俯拍", "仰拍", "斜角", "过肩", "顶摄")
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

# LLM 单场景拆解系统提示词（要求仅输出严格 JSON，方案 B 结构化输出）；
# visual_description 参照 AI 漫剧行业分镜公式：[景别]+[角色+外貌特征]+[动作]+
# [场景]+[情绪关键词]，环境与光影拆至独立 lighting 字段（分层提示词素材），
# 另输出机位角度/负面提示词/音效/情绪标签，保证下游分层生图与配音可直接消费；
# camera_movement 白名单覆盖运镜 8 术语（固定/推/拉/摇/移/跟随/升降/旋转）
_SHOTS_SYSTEM_PROMPT = (
    "你是专业分镜师。请把给定剧本场景拆解为 1~3 个分镜镜头。\n"
    "仅输出严格 JSON（禁止 markdown 围栏与任何解释文字），格式：\n"
    '{"shots":[{"shot_type":"远景|全景|中景|近景|特写",'
    '"camera_angle":"平视|俯拍|仰拍|斜角|过肩|顶摄",'
    '"camera_movement":"固定|推|拉|摇|移|跟随|升降|旋转","duration":5,'
    '"visual_description":"主体动作描述","lighting":"环境与光影",'
    '"negative_prompt":"负面提示词","sfx":["音效"],"emotion":"情绪标签",'
    '"dialogue":[{"character":"角色名","line":"台词"}]}]}\n'
    "要求：\n"
    "- visual_description 按分镜公式组织：景别 + 角色（含与设定一致的外貌特征）"
    "+ 动作 + 场景要素 + 情绪关键词，一句话可直接转生图提示词；"
    "环境与光影不要混入，统一写到 lighting；\n"
    "- 若用户消息提供「场景动作/画面描述」，visual_description 必须以该动作描述为"
    "画面主体切分镜头（动作已含角色动作与环境细节，剧情仅作背景补充）；\n"
    "- lighting 为本镜环境与光影（时段光线/光源方向/明暗氛围），按画面需要撰写；\n"
    "- negative_prompt 为本镜生图需排除的元素（如：多余手指、肢体畸变、文字水印、"
    "模糊变形、画面杂乱元素），按画面内容选取 2~4 项；\n"
    "- camera_angle 按镜头情绪选择：平视=客观叙事、俯拍=渺小/压迫、仰拍=威严/崇高、"
    "斜角=不安/冲突、过肩=对话机位、顶摄=宿命感；\n"
    "- 画面中角色的外貌/服装必须与剧本角色设定（外观描述）严格一致，禁止换装换发型；"
    "「场景动作/画面描述」文本与角色设定冲突时（如动作文本写了不同的服装/发型/配色），"
    "以角色设定为准改写该段描述——仅可保留动作与状态变化（淋湿/破损/凌乱/沾污等），"
    "服装类型与颜色、发色发型、年龄性别一律不可改变；\n"
    "- camera_movement 从白名单中按镜头意图选择（推=强调主体、拉=揭示环境、"
    "摇=跟随扫视、移=建立空间、跟随=角色跟拍、升降=氛围强调、旋转=眩晕/冲突）；\n"
    "- duration 按节奏取值：快卡点镜头 2~3 秒、常规叙事镜头 4~6 秒、"
    "单个镜头不超过 10 秒；\n"
    "- sfx 为本镜音效关键词列表（环境音/动作音/氛围音），无则空数组；\n"
    "- emotion 为本镜整体情绪标签（如：压抑/爽感/紧张/释然/温馨）；\n"
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
        List[Dict[str, Any]]: 2 个镜头 dict（含方案 B 新字段的中性降级值）。
    """
    dialogue = scene.get("dialogue") or []
    if isinstance(dialogue, str):
        dialogue = [{"character": "旁白", "line": ln.strip()} for ln in dialogue.splitlines() if ln.strip()]
    half = (len(dialogue) + 1) // 2
    title = str(scene.get("title") or "场景")
    # 画面素材回退链：动作（画面描述）优先于剧情（剧情偏梗概，动作更贴近可生图画面）
    plot = str(scene.get("action") or scene.get("plot") or "")[:60]
    # 声音降级：透传剧情节点自带的音效/情绪（无则空）
    scene_sfx = [str(s).strip() for s in (scene.get("sfx") or []) if str(s or "").strip()]
    scene_emotion = str(scene.get("emotion") or "").strip() or None
    return [
        {
            "shot_type": "中景", "camera_angle": "平视", "camera_movement": "固定", "duration": 5,
            "visual_description": f"{title}：{plot}（规则降级拆镜·前段）",
            "lighting": None, "negative_prompt": None,
            "sfx": scene_sfx, "emotion": scene_emotion,
            "dialogue": dialogue[:half],
        },
        {
            "shot_type": "近景", "camera_angle": "平视", "camera_movement": "推", "duration": 5,
            "visual_description": f"{title}：情绪特写推进（规则降级拆镜·后段）",
            "lighting": None, "negative_prompt": None,
            "sfx": scene_sfx, "emotion": scene_emotion,
            "dialogue": dialogue[half:],
        },
    ]


async def _llm_shots(
    scene: Dict[str, Any], character_profiles: Optional[str] = None
) -> List[Dict[str, Any]]:
    """LLM 单场景拆解：生成 1~3 个分镜；异常或输出非法时规则降级。

    Args:
        scene: 剧本场景 dict。
        character_profiles: 项目主要角色外观设定清单（「姓名：外观段」串联，
            可空——注入后 LLM 拆镜的画面描述与角色定妆照形象对齐）。

    Returns:
        List[Dict[str, Any]]: 规整后的镜头列表（每项含 shot_type/camera_movement/
            duration/visual_description/dialogue）。

    Raises:
        无：内部捕获一切异常（含网络/解析错误），保证降级路径可达。
    """
    # 场景动作（剧情节点「动作」栏，AI 生成为必填）为画面主体素材；
    # 缺失时（旧数据/手动未填）该行为空串，提示词由剧情兜底
    user_content = (
        f"场景标题：{scene.get('title', '')}\n"
        f"场景剧情：{scene.get('plot', '')}\n"
        f"场景动作/画面描述：{scene.get('action') or ''}\n"
        f"场景对白：{json.dumps(scene.get('dialogue') or [], ensure_ascii=False)}\n"
        f"场景时长：{scene.get('duration_sec', 30)} 秒\n"
        f"出场角色外观设定：{character_profiles or '无'}"
    )
    try:
        # 声音兜底（循环外算一次）：LLM 未给音效/情绪时透传剧情节点自带值
        scene_sfx_fallback = [str(s).strip()[:50] for s in (scene.get("sfx") or []) if str(s or "").strip()][:5]
        scene_emotion_fallback = str(scene.get("emotion") or "").strip()[:50] or None
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
            # LLM 的 negative_prompt 偶尔返回数组（提示词格式不稳定），数组转顿号串
            raw_negative = sh.get("negative_prompt")
            if isinstance(raw_negative, list):
                negative_text = "、".join(
                    str(x).strip() for x in raw_negative if str(x or "").strip())
            else:
                negative_text = str(raw_negative or "").strip()
            shots.append({
                "shot_type": str(sh.get("shot_type") or "中景")[:20],
                # 方案 B：机位角度白名单校验（LLM 输出越界时回退平视）
                "camera_angle": str(sh.get("camera_angle") or "平视")[:20]
                if str(sh.get("camera_angle") or "") in CAMERA_ANGLES else "平视",
                "camera_movement": str(sh.get("camera_movement") or "固定")[:20],
                "duration": duration,
                # visual_description 回退链：LLM 输出 → 动作（画面描述）→ 剧情
                "visual_description": str(
                    sh.get("visual_description") or scene.get("action") or scene.get("plot") or ""),
                # 方案 B：环境光影 / 负面提示词（可空，LLM 未给时不造数据）
                "lighting": str(sh.get("lighting") or "").strip()[:200] or None,
                "negative_prompt": negative_text[:300] or None,
                # 方案 B：声音结构化（音效列表 + 情绪标签；LLM 未给时透传剧情节点自带值）
                "sfx": ([str(s).strip()[:50] for s in (sh.get("sfx") or [])
                         if str(s or "").strip()][:5]) or scene_sfx_fallback,
                "emotion": str(sh.get("emotion") or "").strip()[:50] or scene_emotion_fallback,
                "dialogue": sh.get("dialogue") or [],
            })
        return shots or _fallback_shots(scene)
    except Exception:  # noqa: BLE001 —— LLM 不可用时必须走规则降级
        return _fallback_shots(scene)


def _match_scene(scene: Dict[str, Any], scene_rows: List[Scene]) -> Tuple[Optional[Scene], str]:
    """按地点名匹配场景资产（精确相等或互相包含）。

    剧情节点结构（2026-10-17）下 location 才是地点（如「写字楼工位区」），
    title 是剧情概括标题（如「末位之夜的窒息」，与场景资产名无对应关系），
    故匹配文本按 location → title 顺序两轮尝试；旧数据（title 即地点）由
    title 轮兜底兼容。

    匹配命中置信度 0.9（应用层匹配策略语义，storyboards 表无独立字段存储；
    未匹配时 scene_id 落 NULL，由告警接口标记「实体未强绑资产」）。

    Args:
        scene: 剧本场景 dict（取 location/title）。
        scene_rows: 项目内场景资产行列表。

    Returns:
        Tuple[Optional[Scene], str]: (命中的场景 ORM 对象或 None, 参与提示词的场景名)。
    """
    for key in ("location", "title"):
        text = str(scene.get(key) or "").strip()
        if not text:
            continue
        for row in scene_rows:
            if row.name == text or row.name in text or text in row.name:
                return row, row.name
    fallback = str(scene.get("location") or scene.get("title") or "").strip()
    return None, fallback or "未匹配场景"


def _match_characters(
    dialogue_text: str, char_rows: List[Character], extra_text: str = "",
) -> List[Character]:
    """匹配镜头涉及角色（RD-24 实体链接，两级匹配）。

    一级：对白说话人（行首「角色名：」）精确匹配角色 name 或 aliases；
    二级兜底：角色名以子串出现在画面描述文本中（覆盖无对白的特写/空镜，
    如「特写，王小满（…）」）。命中按发现顺序去重。

    Args:
        dialogue_text: 多行对白文本（每行 "角色名：台词"）。
        char_rows: 项目内未删且已启用的角色资产列表。
        extra_text: 画面描述等兜底匹配文本（可空；空时不做二级匹配）。

    Returns:
        List[Character]: 命中的角色列表。
    """
    hits: List[Character] = []
    if dialogue_text:
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
    if extra_text:
        for ch in char_rows:
            if ch not in hits and ch.name and ch.name in extra_text:
                hits.append(ch)
    return hits


def _match_props(link_text: str, prop_rows: List[Prop]) -> List[Prop]:
    """按画面/对白文本匹配项目内道具资产（RD-24 强绑口径，方案 B 扩展）。

    匹配规则：道具名（≥2 字符）以子串形式出现在镜头文本中即命中；
    应用层置信度 0.6（LINK_CONFIDENCE_PROP，弱于角色说话人匹配）。
    短名误报风险由「名称 ≥2 字符 + 项目内道具池有限」约束兜底。

    Args:
        link_text: 参与匹配的镜头文本（画面描述 + 对白拼接）。
        prop_rows: 项目内未删道具资产行列表。

    Returns:
        List[Prop]: 命中的道具列表（按道具池顺序去重）。
    """
    hits: List[Prop] = []
    if not link_text:
        return hits
    for prop in prop_rows:
        name = str(prop.name or "").strip()
        if len(name) >= 2 and name in link_text and prop not in hits:
            hits.append(prop)
    return hits


def _build_prompt_locked(
    project: Project, scene_name: str, characters: List[Character],
    visual_description: str, shot_type: str, camera_movement: str,
    camera_angle: Optional[str] = None, lighting: Optional[str] = None,
    negative_prompt: Optional[str] = None,
) -> str:
    """构造分层提示词锁定层模板（RD-04 分层 + 方案 B 扩展八要素）。

    要素序：统一风格 / 场景 / 角色（外貌圣经）/ 画面（主体动作）/
    机位角度 / 景别运镜 / 环境光影 / 负面排除——对应 AI 漫剧行业
    「固定角色卡+动作+环境光影+风格质量+负面排除」分层公式。

    角色要素注入「角色圣经固定前缀」（AI 漫剧行业惯例：每条人物提示词以
    角色圣经开头，只改动作/场景）——外貌来源为 characters.description 的
    「外观：」段（build_profile_description 拼接格式），无该段时回退姓名，
    每角色截 60 字，保证跨镜头生图一致性。

    Args:
        project: 项目对象（取全局风格锚点）。
        scene_name: 场景名。
        characters: 绑定的角色 ORM 列表（外貌圣经来源）。
        visual_description: 画面描述（主体动作）。
        shot_type: 景别。
        camera_movement: 运镜。
        camera_angle: 机位与角度（None 时该要素省略）。
        lighting: 环境与光影（None 时该要素省略）。
        negative_prompt: 负面提示词（None 时该要素省略）。

    Returns:
        str: 分层锁定层提示词（缺省要素自动省略，存量调用兼容）。

    Raises:
        无：纯字符串拼装，角色字段缺失按姓名兜底。
    """
    char_parts: List[str] = []
    for ch in characters:
        appearance = extract_appearance_from_description(ch.description)
        bible = build_character_bible_prompt(ch.name, ch.age, appearance)
        char_parts.append(bible[:60])
    names_text = "；".join(char_parts) if char_parts else "无"
    # 方案 B 扩展要素（缺省省略，保持存量调用输出形态不变）
    extra_parts: List[str] = []
    if camera_angle:
        extra_parts.append(f"机位角度：{camera_angle}")
    if lighting:
        extra_parts.append(f"环境光影：{lighting}")
    if negative_prompt:
        extra_parts.append(f"负面排除：{negative_prompt}")
    extra_text = ("；" + "；".join(extra_parts)) if extra_parts else ""
    return (
        f"统一风格：{project.style or '默认风格'}；场景：{scene_name}；"
        f"角色（外貌圣经，跨镜一致）：{names_text}；画面：{visual_description or '无'}；"
        f"景别运镜：{shot_type}/{camera_movement}{extra_text}"
    )


def compute_stale_reasons(
    sb: Storyboard,
    links: List[Dict[str, Any]],
    char_version_map: Dict[int, int],
    char_name_map: Dict[int, str],
    scene_version_map: Dict[int, int],
    prop_links: Optional[List[Dict[str, Any]]] = None,
    prop_version_map: Optional[Dict[int, int]] = None,
    prop_name_map: Optional[Dict[int, str]] = None,
) -> Tuple[bool, List[str]]:
    """计算单镜过期状态（P0-3 版本透传，模块级纯函数供列表与批量重渲染共用）。

    判定规则（快照 NULL = 旧数据视为一致，防存量分镜全量误报）：
    - 任一角色关联的 character_version 与该角色当前 asset_version 不一致 → 过期；
    - scene_version 与绑定场景当前 asset_version 不一致 → 过期；
    - 任一道具关联的 prop_version 与该道具当前 asset_version 不一致 → 过期（方案 B）。

    Args:
        sb: 分镜 ORM 对象（取 scene_id/scene_version）。
        links: 该镜的角色关联输出列表（含 character_id）。
        char_version_map: 角色 ID → 当前 asset_version。
        char_name_map: 角色 ID → 角色名（过期原因文案用）。
        scene_version_map: 场景 ID → 当前 asset_version。
        prop_links: 该镜的道具关联输出列表（含 prop_id，方案 B，可空兼容存量调用）。
        prop_version_map: 道具 ID → 当前 asset_version（方案 B，可空）。
        prop_name_map: 道具 ID → 道具名（过期原因文案用，方案 B，可空）。

    Returns:
        Tuple[bool, List[str]]: (是否过期, 过期原因文案列表)。
    """
    reasons: List[str] = []
    for link in links:
        cur = char_version_map.get(link["character_id"])
        snap = link.get("character_version")
        if snap is not None and cur is not None and snap != cur:
            reasons.append(f"角色「{char_name_map.get(link['character_id'], link['character_id'])}」定妆照已更新")
    if sb.scene_id and sb.scene_version is not None:
        cur_scene = scene_version_map.get(sb.scene_id)
        if cur_scene is not None and sb.scene_version != cur_scene:
            reasons.append("绑定场景概念图已更新")
    # 方案 B：道具版本快照判定（NULL 快照视为一致，与角色/场景口径一致）
    for plink in (prop_links or []):
        cur_prop = (prop_version_map or {}).get(plink["prop_id"])
        snap_prop = plink.get("prop_version")
        if snap_prop is not None and cur_prop is not None and snap_prop != cur_prop:
            reasons.append(
                f"道具「{(prop_name_map or {}).get(plink['prop_id'], plink['prop_id'])}」标准图已更新")
    return bool(reasons), reasons


async def _build_out_list(db: AsyncSession, sbs: List[Storyboard]) -> List[Dict[str, Any]]:
    """把分镜 ORM 列表转换为输出 dict（批量联查角色/道具关联与场景名，避免 N+1）。

    P0-3：输出追加 stale / stale_reasons（引用版本快照 vs 资产当前版本）。
    方案 B：输出追加 camera_angle / lighting / negative_prompt / audio 与
    props（道具关联列表，含版本快照参与 stale 判定）。

    Args:
        db: 异步数据库会话。
        sbs: 分镜 ORM 列表。

    Returns:
        List[Dict[str, Any]]: 输出列表，每项含 characters / props 与 scene_name。
    """
    if not sbs:
        return []
    ids = [sb.id for sb in sbs]
    # 批量取分镜-角色关联（联角色名与当前版本，供 stale 计算）
    link_rows = (
        await db.execute(
            select(StoryboardCharacter, Character.name, Character.asset_version)
            .join(Character, Character.id == StoryboardCharacter.character_id)
            .where(StoryboardCharacter.storyboard_id.in_(ids))
            .order_by(StoryboardCharacter.id.asc())
        )
    ).all()
    links_by_sb: Dict[int, List[Dict[str, Any]]] = {}
    char_version_map: Dict[int, int] = {}
    char_name_map: Dict[int, str] = {}
    for link, cname, cversion in link_rows:
        links_by_sb.setdefault(link.storyboard_id, []).append({
            "character_id": link.character_id,
            "name": cname,
            "variant_id": link.variant_id,
            "link_confidence": float(link.link_confidence) if link.link_confidence is not None else None,
            "character_version": link.character_version,
        })
        char_version_map[link.character_id] = int(cversion or 1)
        char_name_map[link.character_id] = cname
    # 批量取分镜-道具关联（联道具名与当前版本，供 stale 计算；方案 B）
    prop_link_rows = (
        await db.execute(
            select(StoryboardProp, Prop.name, Prop.asset_version)
            .join(Prop, Prop.id == StoryboardProp.prop_id)
            .where(StoryboardProp.storyboard_id.in_(ids))
            .order_by(StoryboardProp.id.asc())
        )
    ).all()
    props_by_sb: Dict[int, List[Dict[str, Any]]] = {}
    prop_version_map: Dict[int, int] = {}
    prop_name_map: Dict[int, str] = {}
    for plink, pname, pversion in prop_link_rows:
        props_by_sb.setdefault(plink.storyboard_id, []).append({
            "prop_id": plink.prop_id,
            "name": pname,
            "link_confidence": float(plink.link_confidence) if plink.link_confidence is not None else None,
            "prop_version": plink.prop_version,
        })
        prop_version_map[plink.prop_id] = int(pversion or 1)
        prop_name_map[plink.prop_id] = pname
    # 批量取场景名与当前版本
    scene_ids = {sb.scene_id for sb in sbs if sb.scene_id}
    scene_names: Dict[int, str] = {}
    scene_version_map: Dict[int, int] = {}
    if scene_ids:
        rows = (
            await db.execute(select(Scene.id, Scene.name, Scene.asset_version)
                             .where(Scene.id.in_(scene_ids)))
        ).all()
        scene_names = {sid: name for sid, name, _ in rows}
        scene_version_map = {sid: int(ver or 1) for sid, _, ver in rows}

    out: List[Dict[str, Any]] = []
    for sb in sbs:
        links = links_by_sb.get(sb.id, [])
        prop_links = props_by_sb.get(sb.id, [])
        stale, stale_reasons = compute_stale_reasons(
            sb, links, char_version_map, char_name_map, scene_version_map,
            prop_links=prop_links, prop_version_map=prop_version_map,
            prop_name_map=prop_name_map)
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
            # P0-3 版本透传：引用过期标记（前端标黄 + 批量重渲染）
            "stale": stale,
            "stale_reasons": stale_reasons,
            "characters": links,
            # 方案 B：镜头语言扩展 / 画面分层 / 声音结构化 / 道具关联
            "camera_angle": sb.camera_angle,
            "lighting": sb.lighting,
            "negative_prompt": sb.negative_prompt,
            "audio": sb.audio,
            "props": prop_links,
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
    # 两阶段重排（同 _resequence_episodes）：避开 uk_sb_seq 唯一索引下
    # 重排/合并造成的同集内 sequence 互换平移冲突
    for sb in sbs:
        sb.sequence = sb.sequence + 1000000
    await db.flush()
    for idx, sb in enumerate(sbs, start=1):
        sb.sequence = idx


async def check_lock_readiness(
    db: AsyncSession, project: Project, scope_episode_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """校验主要角色定妆照锁定就绪度（P0-1 锁定门禁，按集工作流口径）。

    主要角色判定（复用 P-1 角色定位字段 role_type 分级）：
    lead 主角 / support 配角 / villain 反派 视为主要；extra 群像放行；
    role_type 为 NULL（旧数据/快速模式产物）保守视为主要，靠快速模式兜底。

    未就绪按序判定（取首个命中原因）：
    - 无图（asset_url IS NULL）且 generation_status=pending → "未生成定妆照"；
    - generation_status=failed → "生成失败"；
    - generation_status=generating → "生成中"（严格拦截防引用半成品）；
    - face_locked=False → "未锁定"。

    按集门禁（S-06）：scope_episode_id 传值时只检查「本集涉及的角色」——
    该集剧本剧情节点的对白说话人（精确）+ 剧情/动作/标题文本中的角色名
    （包含兜底），与 decompose 的实体绑定口径一致；其他集的角色（即使
    未定妆）不拦截本集拆解。scope 为 None（全量重建）时仍检查全部角色。

    Args:
        db: 请求级异步数据库会话。
        project: 已校验归属的项目。
        scope_episode_id: 按集拆解范围剧集 ID（None = 检查项目全部角色）。

    Returns:
        List[Dict[str, Any]]: 未就绪角色清单
            [{"character_id","name","role_type","face_locked","generation_status",
              "has_image","reason"}]，空列表 = 全部就绪。
    """
    rows = (
        await db.execute(
            select(Character).where(
                Character.project_id == project.id, Character.deleted_at.is_(None),
                # 按集工作流：门禁只看已启用角色（角色设定确认未启用的不拦截）
                Character.enabled_at.is_not(None))
        )
    ).scalars().all()
    # 按集门禁：先算「本集涉及角色」集合（无剧本/无节点时集合为空 → 全放行）
    involved: Optional[set] = None
    if scope_episode_id is not None:
        involved = await _episode_involved_character_ids(
            db, project.id, scope_episode_id, rows)
    unready: List[Dict[str, Any]] = []
    for ch in rows:
        if involved is not None and ch.id not in involved:
            continue  # 非本集涉及的角色不拦截本集拆解
        if ch.role_type == "extra":
            continue  # 群像不做定妆强制（对应小云雀只锁 3~5 主角的策略）
        if ch.asset_url is None and ch.generation_status == "pending":
            reason = "未生成定妆照"
        elif ch.generation_status == "failed":
            reason = "生成失败"
        elif ch.generation_status == "generating":
            reason = "生成中"
        elif not ch.face_locked:
            reason = "未锁定"
        else:
            continue  # 就绪
        unready.append({
            "character_id": ch.id,
            "name": ch.name,
            "role_type": ch.role_type,
            "face_locked": ch.face_locked,
            "generation_status": ch.generation_status,
            "has_image": ch.asset_url is not None,
            "reason": reason,
        })
    return unready


async def _episode_involved_character_ids(
    db: AsyncSession, project_id: int, episode_id: int,
    char_rows: List[Character],
) -> set:
    """提取「某集涉及的角色 ID 集合」（按集锁定门禁内部函数）。

    取该集绑定的剧本行（uk_scripts_project 每集一行），解包剧情节点后：
    对白说话人经 _match_characters 精确匹配（aliases 支持），剧情/动作/标题
    全文经名字包含兜底匹配——与 decompose 的实体绑定口径完全一致，
    保证「拆解会绑到的角色 ⊆ 门禁检查的角色」。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。
        episode_id: 剧集 ID。
        char_rows: 项目内已启用的角色 ORM 列表（匹配池）。

    Returns:
        set: 本集涉及的角色 ID 集合（无剧本或无涉及角色时为空集）。
    """
    involved: set = set()
    script_rows = (
        await db.execute(
            select(Script).where(
                Script.project_id == project_id, Script.episode_id == episode_id)
        )
    ).scalars().all()
    for row in script_rows:
        for scene in unwrap_scenes(row.content):
            if not isinstance(scene, dict):
                continue
            dialogue_text = _dialogue_to_text(scene.get("dialogue"))
            extra_text = "；".join(filter(None, (
                str(scene.get("title") or ""), str(scene.get("plot") or ""),
                str(scene.get("action") or ""),
            )))
            for ch in _match_characters(dialogue_text, char_rows, extra_text=extra_text):
                involved.add(ch.id)
    return involved


async def decompose_storyboards(
    db: AsyncSession,
    project: Project,
    skip_lock_check: bool = False,
    scope_episode_id: Optional[int] = None,
) -> Dict[str, Any]:
    """AI 拆解：读剧本 content.scenes 逐场景生成 1~3 镜并建 storyboards 行。

    - LLM 失败走规则降级（每场景固定 2 镜、每镜 5s，离线降级路径）；
    - 剧本按项目全部 Script 行合并读取（默认行 episode_id NULL 排最前，其余按剧集
      sequence 升序），兼容分阶段流水线每集一行的落库形态；
      scope_episode_id 传值（按集拆解）时只读取该剧集绑定的剧本行；
    - 每镜 episode_id 随来源剧本行落库（前端可按剧集过滤分镜）；
    - scene_id 按场景名匹配场景资产（未匹配落 NULL）；
    - storyboard_characters 关联对白中出现的角色（link_confidence=0.8）；
    - prompt_locked 写入五要素模板；status='pending'；project.status='storyboard'；
    - 幂等重建语义：scope_episode_id 传值时只删除并重建该集分镜（跨集全局
      重排 sequence），缺省全量重建（既有行为）；
    - P0-1 锁定门禁：skip_lock_check=False 时校验主要角色定妆照就绪，未就绪
      抛 409（携带清单）且不清空既有分镜。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        skip_lock_check: 快速模式——跳过锁定门禁（可能造成角色形象不一致）。
        scope_episode_id: 按集拆解范围剧集 ID（None=全量重建，按集工作流传值）。

    Returns:
        Dict[str, Any]: {"total": 新建分镜数, "degraded": 是否发生过规则降级}。

    Raises:
        HTTPException: 400 —— 项目无剧本或范围内剧本无场景数据；
            409 —— 主要角色定妆照未就绪（body 含未就绪清单）。
    """
    # 读取剧本行（下游接收修复 2026-10-15）：分阶段流水线按剧集落库
    # （每集一行，uk_scripts_project = (project_id, episode_id)），旧快速路径
    # 为单行默认行（episode_id NULL）。此前 scalar_one_or_none 在行数 >1 时
    # 抛 MultipleResultsFound（500），导致流水线产出无法被下游分镜接收。
    # 按集拆解（S-06）：scope_episode_id 传值时只读取该集剧本行。
    script_conds = [Script.project_id == project.id]
    if scope_episode_id is not None:
        script_conds.append(Script.episode_id == scope_episode_id)
    if (
        await db.execute(
            select(Script).where(*script_conds).limit(1)
        )
    ).scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该范围尚无剧本，请先生成剧本")

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
            select(Script).where(*script_conds).order_by(Script.id.asc())
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

    # P0-1 锁定门禁：在删除既有分镜之前校验（409 拦截时不清空用户既有分镜）；
    # 按集拆解只检查「本集涉及的角色」（其他集未定妆角色不拦截本集）
    if not skip_lock_check:
        unready = await check_lock_readiness(db, project, scope_episode_id=scope_episode_id)
        if unready:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
                "code": "asset_lock_not_ready",
                "message": f"{len(unready)} 个主要角色定妆照未就绪，已拦截分镜生成",
                "unready": unready,
            })

    # 项目内场景 / 角色 / 道具资产（用于强绑与实体链接；角色仅取已启用的——
    # 按集工作流：角色设定确认未启用的角色不参与分镜实体链接；
    # 道具过滤软删，方案 B 扩展参与镜头文本匹配）
    scene_rows = (
        await db.execute(select(Scene).where(Scene.project_id == project.id))
    ).scalars().all()
    char_rows = (
        await db.execute(
            select(Character).where(
                Character.project_id == project.id, Character.deleted_at.is_(None),
                Character.enabled_at.is_not(None))
        )
    ).scalars().all()
    prop_rows = (
        await db.execute(
            select(Prop).where(
                Prop.project_id == project.id, Prop.deleted_at.is_(None))
        )
    ).scalars().all()

    # 拆解输入注入角色外观设定：LLM 拆镜时对齐服装/发型/配色，防止画面描述
    # 自由发挥导致与定妆照不一致（生成侧参考图依赖分镜描述与资产形象对齐）；
    # 仅注入主要角色（lead/support/villain），群像不占提示词预算，每角色截 80 字
    character_profiles = "；".join(
        f"{ch.name}：{(extract_appearance_from_description(ch.description) or '外观未描述')[:80]}"
        for ch in char_rows
        if (ch.role_type or "lead") in ("lead", "support", "villain")
    ) or None

    # 幂等重建：按集拆解时只删该集分镜（跨集全局重排 sequence），
    # 缺省全量重建删全部（storyboard_characters 由外键级联删除）
    delete_conds = [Storyboard.project_id == project.id]
    if scope_episode_id is not None:
        delete_conds.append(Storyboard.episode_id == scope_episode_id)
    await db.execute(delete(Storyboard).where(*delete_conds))

    seq = 0
    degraded = False
    for src_script, scene in scene_entries:
        shots = await _llm_shots(scene, character_profiles)
        if any("规则降级" in str(s.get("visual_description") or "") for s in shots):
            degraded = True
        scene_row, scene_name = _match_scene(scene, scene_rows)
        for shot in shots:
            seq += 1
            shot_type = shot["shot_type"]
            camera_movement = shot["camera_movement"]
            visual_description = str(shot.get("visual_description") or "")
            dialogue_text = _dialogue_to_text(shot.get("dialogue"))
            # 方案 B：声音结构化（音效 + 情绪）与分层画面字段
            shot_sfx = shot.get("sfx") or []
            shot_emotion = shot.get("emotion")
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
                scene_id=scene_row.id if scene_row else None,
                # P0-3：绑定场景时写入当前版本快照（NULL 场景无快照）
                scene_version=scene_row.asset_version if scene_row else None,
                # 方案 B：镜头语言 / 画面分层 / 声音结构化
                camera_angle=shot.get("camera_angle"),
                lighting=shot.get("lighting"),
                negative_prompt=shot.get("negative_prompt"),
                audio={
                    "sfx": shot_sfx,
                    "emotion": shot_emotion,
                } if (shot_sfx or shot_emotion) else None,
                prompt_locked="",   # 关联角色确定后回填
                prompt_editable="",
                status="pending",
            )
            db.add(sb)
            await db.flush()  # 取 sb.id 供角色/道具关联使用

            # 分镜-角色关联（RD-24 强绑，link_confidence=0.8）：
            # 一级对白说话人 + 二级画面描述角色名兜底（无对白特写也能绑定）
            linked = _match_characters(dialogue_text, char_rows, extra_text=visual_description or "")
            for ch in linked:
                db.add(StoryboardCharacter(
                    storyboard_id=sb.id,
                    character_id=ch.id,
                    variant_id=None,
                    # P0-3：引用角色时写入当前定妆照版本快照（判过期依据）
                    character_version=ch.asset_version or 1,
                    link_confidence=LINK_CONFIDENCE,
                ))
            # 分镜-道具关联（方案 B）：画面描述 + 对白文本中出现的道具
            # （RD-24 强绑口径，link_confidence=0.6；含 prop_version 快照）
            linked_props = _match_props(f"{visual_description or ''}\n{dialogue_text}", prop_rows)
            for prop in linked_props:
                db.add(StoryboardProp(
                    storyboard_id=sb.id,
                    prop_id=prop.id,
                    prop_version=prop.asset_version or 1,
                    link_confidence=LINK_CONFIDENCE_PROP,
                ))
            # 分层锁定层提示词（RD-04 + 方案 B 八要素）：角色要素注入外貌圣经固定前缀
            sb.prompt_locked = _build_prompt_locked(
                project, scene_name, linked,
                visual_description, shot_type, camera_movement,
                camera_angle=shot.get("camera_angle"),
                lighting=shot.get("lighting"),
                negative_prompt=shot.get("negative_prompt"),
            )

    project.status = "storyboard"
    # 拆解后按项目「单集目标时长」等比对齐（SB-07 同口径缩放，与手动
    # auto-fit-duration 一致）：新建项目选定的单集时长即总时长锚点，
    # LLM 按剧情节点 duration_sec 分配的时长仅是相对节奏，须贴合目标。
    # 按集拆解只缩放该集分镜（其他集已各自对齐，不重复缩放）；全量重建
    # 时项目分镜即本批新镜，直接整体缩放。目标未设置时跳过。
    if project.target_duration:
        fit_conds = [Storyboard.project_id == project.id]
        if scope_episode_id is not None:
            fit_conds.append(Storyboard.episode_id == scope_episode_id)
        fit_sbs = (
            await db.execute(select(Storyboard).where(*fit_conds))
        ).scalars().all()
        fit_total = sum(sb.duration for sb in fit_sbs)
        if fit_total > 0:
            scale = project.target_duration / fit_total
            for sb in fit_sbs:
                sb.duration = max(1, round(sb.duration * scale))
    # 按集拆解：其他集分镜保留旧 sequence，跨集全局重排为连续 1..n
    # （默认剧集分镜排最前，其余按剧集 sequence；缺省全量重建时 seq 本就连续）
    if scope_episode_id is not None:
        await _resequence_episodes(db, project.id)
    await db.commit()
    return {"total": seq, "degraded": degraded}


async def _resequence_episodes(db: AsyncSession, project_id: int) -> None:
    """跨集全局重排分镜 sequence（内部函数，按集拆解后调用）。

    排序口径：默认剧集（episode_id NULL）分镜排最前，其余按所属剧集 sequence
    升序、集内按既有 sequence 与 ID 稳定排序，统一重写为连续 1..n。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。

    Returns:
        None。

    Raises:
        无：纯排序重写，不产生业务异常路径。
    """
    episode_order: Dict[int, int] = {
        ep.id: (ep.sequence if ep.sequence is not None else 0)
        for ep in (
            await db.execute(select(Episode).where(Episode.project_id == project_id))
        ).scalars().all()
    }
    sbs = (
        await db.execute(select(Storyboard).where(Storyboard.project_id == project_id))
    ).scalars().all()

    def _sort_key(sb: Storyboard) -> Tuple[int, int, int]:
        """排序键（内部）：（剧集顺序，集内 sequence，ID 稳定序）。"""
        ep_order = 0 if sb.episode_id is None else episode_order.get(sb.episode_id, 10**9)
        return (ep_order, sb.sequence, sb.id)

    # 两阶段重排：uk_sb_seq=(project_id, episode_id, sequence) 唯一索引下，
    # 同集内 1..n → m..k 的平移会与未改行临时撞车（逐行 UPDATE 触发
    # UniqueViolation）。先把全部行整体偏移到高位区（同集内相对唯一保持，
    # 偏移阶段安全），flush 落位后再按目标序赋 1..n（落位递增且高位不在
    # 目标区间，同样不冲突）。
    for sb in sbs:
        sb.sequence = sb.sequence + 1000000
    await db.flush()
    for idx, sb in enumerate(sorted(sbs, key=_sort_key), start=1):
        sb.sequence = idx


async def confirm_storyboards(db: AsyncSession, project: Project) -> Dict[str, Any]:
    """确认分镜（生成阶段门禁动作）：项目状态 storyboard → producing。

    幂等：已处于 producing 直接返回成功（重复点击「确认分镜」不报错）。
    其余状态（draft/scripting/asset 等）拒绝——分镜流程未完成，先走完拆解。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。

    Returns:
        Dict[str, Any]: {"status": "producing"}。

    Raises:
        HTTPException: 400 —— 项目尚无分镜（未拆解）或状态不在 storyboard/
            producing。
    """
    if project.status == "producing":
        return {"status": "producing"}
    if project.status != "storyboard":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"当前项目状态为「{project.status}」，需先完成剧本与分镜拆解",
        )
    cnt = await db.scalar(
        select(func.count()).select_from(Storyboard).where(Storyboard.project_id == project.id)
    )
    if not cnt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="项目尚无分镜，请先在分镜设计页完成 AI 拆解",
        )
    project.status = "producing"
    await db.commit()
    return {"status": "producing"}


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
        # P0-3：改绑场景时同步刷新版本快照
        sb.scene_version = scene.asset_version or 1

    for field in ("shot_type", "camera_movement", "duration",
                  "visual_description", "dialogue", "prompt_editable",
                  "camera_angle", "lighting", "negative_prompt", "audio"):
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
                # P0-3：改绑角色时写入当前定妆照版本快照（variant 未指定变体版本）
                character_version=ch.asset_version or 1,
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
    # 两阶段重排（同 _resequence）：先整体偏移避开 uk_sb_seq 唯一索引，
    # 再按目标序落位，防止同集内互换（如 [2,1] → [1,2]）撞唯一约束
    for sb in sbs:
        sb.sequence = sb.sequence + 1000000
    await db.flush()
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
        # 方案 B：拆分出的新镜沿用本镜的镜头语言/画面分层/声音结构
        camera_angle=sb.camera_angle,
        lighting=sb.lighting,
        negative_prompt=sb.negative_prompt,
        audio=sb.audio,
        prompt_locked=sb.prompt_locked,
        prompt_editable="",
        status="pending",
        regen_count=0,
    )
    db.add(new_sb)
    await db.flush()  # 取新镜 ID 以复制角色/道具关联

    # 角色关联复制到新镜（两镜沿用同一组绑定角色，含版本快照）
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
            character_version=link.character_version,
            variant_version=link.variant_version,
        ))

    # 道具关联复制到新镜（方案 B：两镜沿用同一组绑定道具，含版本快照）
    old_prop_links = (
        await db.execute(
            select(StoryboardProp).where(StoryboardProp.storyboard_id == sb.id)
        )
    ).scalars().all()
    for plink in old_prop_links:
        db.add(StoryboardProp(
            storyboard_id=new_sb.id,
            prop_id=plink.prop_id,
            link_confidence=plink.link_confidence,
            prop_version=plink.prop_version,
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
            character_version=link.character_version,
            variant_version=link.variant_version,
        ))

    # other 的道具关联并入本镜（方案 B：按 prop_id 去重，撞唯一索引）
    existing_prop_ids = {
        row for row in (
            await db.execute(
                select(StoryboardProp.prop_id).where(
                    StoryboardProp.storyboard_id == sb.id)
            )
        ).scalars().all()
    }
    other_prop_links = (
        await db.execute(
            select(StoryboardProp).where(StoryboardProp.storyboard_id == other.id)
        )
    ).scalars().all()
    for plink in other_prop_links:
        if plink.prop_id in existing_prop_ids:
            continue
        db.add(StoryboardProp(
            storyboard_id=sb.id,
            prop_id=plink.prop_id,
            link_confidence=plink.link_confidence,
            prop_version=plink.prop_version,
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
