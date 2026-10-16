"""剧本导出与影响面预览服务模块（文件级说明，P-1 范式级重构 Task 11）。

承载两组内聚能力（均与剧本域强相关，故独立成模块而不继续膨胀
script_service.py）：

1. RD-08 影响面预览：`collect_entity_impact` 统计某资产（角色/场景/道具）
   被剧本内容与分镜引用的次数，供「删除前先看影响面」使用。**只读**，
   不落库——与抽取确认后写入 `version_snapshots.impact_scope` 的「事后
   快照」互补（此处是修改前预览）。
2. 剧本导出：`collect_export_episodes` 汇总待导出的集级数据，
   `render_txt` / `render_fountain` 两个**纯函数**负责把结构化数据渲染为
   TXT 与 Fountain 文本（无数据库依赖，便于单元测试）。

Fountain 参考规范：场景标题 `INT./EXT. 地点 - 时间`（大写、前后空行）、
动作行、角色行（大写）紧接对白行、场次之间空行分隔、多集以 `#` Section
语法分节（https://fountain.io/syntax）。
"""

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Character,
    Episode,
    Project,
    Prop,
    Scene,
    Script,
    Storyboard,
    StoryboardCharacter,
)
from app.services.script_service import content_has_payload, content_plain

# 剧本引用场次标签上限（RD-08 预览返回条数）
_IMPACT_LOCATION_LIMIT = 10
# 匹配分镜 ID 返回上限（RD-08 预览返回条数）
_IMPACT_STORYBOARD_LIMIT = 20

# Fountain 角色行「类型化旁白」取值（type 命中时优先于角色名渲染）
_FOUNTAIN_SPEAKER_TYPES = ("旁白", "画外音")

# 内外景前缀映射（int_ext → Fountain 场景标题前缀）
_FOUNTAIN_INT_EXT_PREFIX = {"int": "INT.", "ext": "EXT."}


# ---------------------------------------------------------------------------
# RD-08 影响面预览
# ---------------------------------------------------------------------------
def _count_scene_matches(scene: Dict[str, Any], name: str) -> int:
    """统计单个场次内目标实体名出现的次数（内部函数）。

    匹配范围与 RD-08 口径一致：场次 title/plot/action 文本 + 每条对白的
    character（角色名）与 line（台词）文本；dialogue 兼容字符串形态。

    Args:
        scene: 场次 dict（结构异常按缺字段处理）。
        name: 目标实体名（非空；空名直接返回 0）。

    Returns:
        int: 该场次内实体名出现次数（0 表示未引用）。

    Raises:
        无：结构异常按缺数据处理。
    """
    if not name:
        return 0
    segments: List[str] = [
        str(scene.get("title") or ""),
        str(scene.get("plot") or ""),
        str(scene.get("action") or ""),
    ]
    dialogue = scene.get("dialogue")
    if isinstance(dialogue, list):
        for item in dialogue:
            if isinstance(item, dict):
                segments.append(str(item.get("character") or ""))
                segments.append(str(item.get("line") or ""))
    elif isinstance(dialogue, str):
        segments.append(dialogue)
    return sum(segment.count(name) for segment in segments)


async def _load_scoped_scripts(
    db: AsyncSession, project: Project, episode_id: Optional[int]
) -> List[Tuple[Script, Optional[int]]]:
    """按范围加载剧本行并附带剧集序号（内部函数）。

    范围语义：episode_id 传值仅取该集剧本行；缺省取项目下全部剧本行
    （含未绑定剧集的默认行，序号为 None）。

    Args:
        db: 请求级异步数据库会话。
        project: 已校验归属的项目。
        episode_id: 剧集 ID（可空）。

    Returns:
        List[Tuple[Script, Optional[int]]]: (剧本行, 剧集序号) 列表；
            默认剧本行的剧集序号为 None。

    Raises:
        无：数据库异常向上传播由调用方处理。
    """
    stmt = (
        select(Script, Episode.sequence)
        .outerjoin(Episode, Script.episode_id == Episode.id)
        .where(Script.project_id == project.id)
    )
    if episode_id is not None:
        stmt = stmt.where(Script.episode_id == episode_id)
    rows = (await db.execute(stmt)).all()
    # 有集者按剧集序号升序在前，默认剧本行（序号 None）排后
    rows.sort(key=lambda row: (row[1] is None, row[1] or 0))
    return [(row[0], row[1]) for row in rows]


async def _storyboard_structured_ids(
    db: AsyncSession,
    project: Project,
    entity_type: str,
    entity_id: int,
    episode_id: Optional[int],
) -> set:
    """查询分镜对实体的「结构化引用」ID 集合（内部函数）。

    结构化引用来源（勘察结论：storyboards 具备部分强绑字段）：
    - 角色：`storyboard_characters.character_id` 关联表（RD-24 强绑）；
    - 场景：`storyboards.scene_id`（RD-24 强绑场景资产）；
    - 道具：无结构化引用，返回空集合（仅靠文本匹配）。

    Args:
        db: 请求级异步数据库会话。
        project: 已校验归属的项目。
        entity_type: 实体类型（character/scene/prop）。
        entity_id: 实体资产 ID。
        episode_id: 剧集 ID（可空，用于限定分镜范围）。

    Returns:
        set: 结构化引用的分镜 ID 集合（无结构化引用时为空集合）。

    Raises:
        无：数据库异常向上传播由调用方处理。
    """
    if entity_type == "character":
        stmt = (
            select(StoryboardCharacter.storyboard_id)
            .join(Storyboard, StoryboardCharacter.storyboard_id == Storyboard.id)
            .where(
                Storyboard.project_id == project.id,
                StoryboardCharacter.character_id == entity_id,
            )
        )
        if episode_id is not None:
            stmt = stmt.where(Storyboard.episode_id == episode_id)
        return {row[0] for row in (await db.execute(stmt)).all()}
    if entity_type == "scene":
        stmt = select(Storyboard.id).where(
            Storyboard.project_id == project.id, Storyboard.scene_id == entity_id)
        if episode_id is not None:
            stmt = stmt.where(Storyboard.episode_id == episode_id)
        return {row[0] for row in (await db.execute(stmt)).all()}
    return set()


async def collect_entity_impact(
    db: AsyncSession,
    project: Project,
    entity_type: str,
    entity_id: int,
    entity_name: str,
    episode_id: Optional[int] = None,
) -> Dict[str, Any]:
    """统计实体在剧本与分镜中的引用面（RD-08，只读不落库）。

    统计口径：
    - script_refs：剧本 content 的 scenes 中实体名出现次数（title/plot/
      action + dialogue.character/dialogue.line）；
    - storyboard_refs：分镜「文本匹配 ∪ 结构化引用」去重后的条数——文本
      匹配检查 dialogue/visual_description/prompt_locked/prompt_editable，
      结构化引用见 `_storyboard_structured_ids`。

    Args:
        db: 请求级异步数据库会话。
        project: 已校验归属的项目。
        entity_type: 实体类型（character/scene/prop）。
        entity_id: 实体资产 ID。
        entity_name: 实体名称（匹配关键字）。
        episode_id: 剧集 ID（可空；传入时剧本与分镜均限定该集）。

    Returns:
        Dict[str, Any]: {"script_refs", "storyboard_refs",
            "script_locations", "storyboard_ids", "summary"}。

    Raises:
        无：统计过程不抛出业务异常。
    """
    # ---------- 剧本引用 ----------
    script_refs = 0
    script_locations: List[str] = []
    for script, episode_no in await _load_scoped_scripts(db, project, episode_id):
        scenes = content_plain(script.content).get("scenes") or []
        if not isinstance(scenes, list):
            continue
        for idx, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                continue
            count = _count_scene_matches(scene, entity_name)
            if count <= 0:
                continue
            script_refs += count
            scene_no = scene.get("scene_no") or idx + 1
            label = f"场景{scene_no}"
            if episode_no is not None:
                label = f"第{episode_no}集 · {label}"
            if label not in script_locations and len(script_locations) < _IMPACT_LOCATION_LIMIT:
                script_locations.append(label)

    # ---------- 分镜引用（文本匹配 ∪ 结构化引用） ----------
    sb_stmt = select(Storyboard).where(Storyboard.project_id == project.id)
    if episode_id is not None:
        sb_stmt = sb_stmt.where(Storyboard.episode_id == episode_id)
    storyboards = (await db.execute(sb_stmt.order_by(Storyboard.id))).scalars().all()
    structured_ids = await _storyboard_structured_ids(
        db, project, entity_type, entity_id, episode_id)

    matched_ids: List[int] = []
    for storyboard in storyboards:
        texts = (
            storyboard.dialogue,
            storyboard.visual_description,
            storyboard.prompt_locked,
            storyboard.prompt_editable,
        )
        if storyboard.id in structured_ids or any(
            entity_name and entity_name in (text or "") for text in texts
        ):
            matched_ids.append(storyboard.id)

    return {
        "script_refs": script_refs,
        "storyboard_refs": len(matched_ids),
        "script_locations": script_locations,
        "storyboard_ids": matched_ids[:_IMPACT_STORYBOARD_LIMIT],
        "summary": f"删除后影响 {script_refs} 处剧本引用、{len(matched_ids)} 个分镜",
    }


# ---------------------------------------------------------------------------
# 剧本导出：数据汇总
# ---------------------------------------------------------------------------
async def collect_export_episodes(
    db: AsyncSession, project: Project, episode_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    """汇总待导出的集级数据（按集排序，过滤无正文的空集）。

    数据来源：指定 episode_id 时取该集剧本行；缺省取项目下全部剧本行，按
    episode.sequence 升序（未绑定剧集的默认剧本行作为「快速模式单篇」排在
    最后）。每项含原始文本与解包后的结构化 content，供两种格式渲染消费。

    Args:
        db: 请求级异步数据库会话。
        project: 已校验归属的项目。
        episode_id: 剧集 ID（可空；传入时仅导出该集）。

    Returns:
        List[Dict[str, Any]]: 每项 {"episode_no": Optional[int], "title": str,
            "raw_text": str, "plain": dict}；无任何正文内容时返回空列表。

    Raises:
        无：数据库异常向上传播由调用方处理。
    """
    if episode_id is not None:
        rows: List[Tuple[Script, Optional[int], Optional[str]]] = []
        script = (
            await db.execute(
                select(Script).where(
                    Script.project_id == project.id, Script.episode_id == episode_id)
            )
        ).scalar_one_or_none()
        if script is not None:
            episode = await db.get(Episode, episode_id)
            rows.append((
                script,
                episode.sequence if episode is not None else None,
                episode.title if episode is not None else None,
            ))
    else:
        stmt = (
            select(Script, Episode.sequence, Episode.title)
            .outerjoin(Episode, Script.episode_id == Episode.id)
            .where(Script.project_id == project.id)
        )
        all_rows = (await db.execute(stmt)).all()
        # 有集者按剧集序号升序在前，默认剧本行（序号 None）排后
        all_rows.sort(key=lambda row: (row[1] is None, row[1] or 0))
        rows = [(row[0], row[1], row[2]) for row in all_rows]

    episodes: List[Dict[str, Any]] = []
    for script, episode_no, episode_title in rows:
        plain = content_plain(script.content)
        if not (script.raw_text or "").strip() and not content_has_payload(plain):
            continue  # 无正文内容，跳过（空集不导出）
        title = episode_title or str(plain.get("title") or project.title or "")
        episodes.append({
            "episode_no": episode_no,
            "title": title,
            "raw_text": script.raw_text or "",
            "plain": plain,
        })
    return episodes


# ---------------------------------------------------------------------------
# 剧本导出：纯函数渲染（TXT / Fountain）
# ---------------------------------------------------------------------------
def _episode_heading(episode: Dict[str, Any]) -> str:
    """构造集标题文案（内部函数，纯函数）。

    Args:
        episode: collect_export_episodes 的单项。

    Returns:
        str: 有集序号时形如「第N集 · 标题」，否则仅返回标题。

    Raises:
        无：字段缺失按空值兜底。
    """
    title = str(episode.get("title") or "").strip()
    episode_no = episode.get("episode_no")
    if episode_no is not None:
        return f"第{episode_no}集 · {title}".strip()
    return title


def _render_scenes_text(plain: Dict[str, Any]) -> str:
    """把结构化 scenes 回退渲染为纯文本正文（内部函数，纯函数）。

    用于剧本行无 raw_text 时的 TXT 回退：场景标题、plot、对白行
    「角色名：台词」（无角色名时仅台词）。

    Args:
        plain: content_plain 解包后的结构化内容。

    Returns:
        str: 多行正文文本（无场景时为空串）。

    Raises:
        无：结构异常按缺数据处理。
    """
    lines: List[str] = []
    scenes = plain.get("scenes") or []
    for idx, scene in enumerate(scenes if isinstance(scenes, list) else []):
        if not isinstance(scene, dict):
            continue
        scene_no = scene.get("scene_no") or idx + 1
        title = str(scene.get("title") or "").strip()
        lines.append(f"场景{scene_no} {title}".strip())
        plot = str(scene.get("plot") or "").strip()
        if plot:
            lines.append(plot)
        dialogue = scene.get("dialogue")
        if isinstance(dialogue, list):
            for item in dialogue:
                if not isinstance(item, dict):
                    continue
                line = str(item.get("line") or "").strip()
                if not line:
                    continue
                character = str(item.get("character") or "").strip()
                lines.append(f"{character}：{line}" if character else line)
        lines.append("")
    return "\n".join(lines).strip()


def render_txt(episodes: List[Dict[str, Any]]) -> str:
    """把待导出数据渲染为 TXT 文本（纯函数）。

    多集时以 `===== 第N集 · 标题 =====` 分隔；单集直接输出正文。每集正文
    优先用 raw_text，为空时回退结构化 scenes 渲染（_render_scenes_text）。

    Args:
        episodes: collect_export_episodes 的输出列表。

    Returns:
        str: TXT 文本（末尾含单个换行；无内容时仅返回空串的换行）。

    Raises:
        无：纯字符串拼装。
    """
    multi = len(episodes) > 1
    blocks: List[str] = []
    for episode in episodes:
        body = str(episode.get("raw_text") or "").strip()
        if not body:
            body = _render_scenes_text(episode.get("plain") or {})
        if multi:
            blocks.append(f"===== {_episode_heading(episode)} =====\n{body}")
        else:
            blocks.append(body)
    return "\n\n".join(blocks).strip() + "\n"


def _fountain_scene_heading(scene: Dict[str, Any], index: int) -> str:
    """构造 Fountain 场景标题行（内部函数，纯函数，大写）。

    规则：int_ext 为 int/ext 且 location 非空时用 `INT./EXT. 地点 - 时间`
    （time_of_day 缺失则省略「 - 时间」）；否则回退 `场景N 标题`。

    Args:
        scene: 场次 dict。
        index: 场次下标（0 起，scene_no 缺失时兜底）。

    Returns:
        str: 大写场景标题行。

    Raises:
        无：字段缺失按回退格式处理。
    """
    int_ext = str(scene.get("int_ext") or "").strip().lower()
    location = str(scene.get("location") or "").strip()
    time_of_day = str(scene.get("time_of_day") or "").strip()
    prefix = _FOUNTAIN_INT_EXT_PREFIX.get(int_ext)
    if prefix and location:
        heading = f"{prefix} {location}"
        if time_of_day:
            heading += f" - {time_of_day}"
        return heading.upper()
    scene_no = scene.get("scene_no") or index
    title = str(scene.get("title") or "").strip()
    return f"场景{scene_no} {title}".strip().upper()


def _fountain_speaker(dialogue: Dict[str, Any]) -> str:
    """构造 Fountain 对白角色行（内部函数，纯函数，大写）。

    规则：type 为「旁白」/「画外音」时用该类型；否则用 character（大写）；
    两者皆空时回退 type（如「独白」）；仍为空返回空串（该对白不渲染角色行）。

    Args:
        dialogue: 对白 dict。

    Returns:
        str: 角色行文本（可能为空串）。

    Raises:
        无：字段缺失按空串兜底。
    """
    dialogue_type = str(dialogue.get("type") or "").strip()
    if dialogue_type in _FOUNTAIN_SPEAKER_TYPES:
        return dialogue_type
    character = str(dialogue.get("character") or "").strip()
    if character:
        return character.upper()
    return dialogue_type


def render_fountain(episodes: List[Dict[str, Any]]) -> str:
    """把待导出数据渲染为 Fountain 文本（纯函数，遵循 Fountain 规范）。

    渲染规则：
    - 多集时每集以 `# 第N集 · 标题`（Section 语法）分节，单集不加分节；
    - 场景标题行大写、前后空行（`INT./EXT. 地点 - 时间`，回退 `场景N 标题`）；
    - 动作行取 action（无则 plot）；角色行大写后紧接对白 line；
    - 场次之间空行分隔，连续空行折叠为单个。

    Args:
        episodes: collect_export_episodes 的输出列表。

    Returns:
        str: Fountain 文本（末尾含单个换行）。

    Raises:
        无：结构异常按缺数据处理。
    """
    multi = len(episodes) > 1
    out: List[str] = []

    def _push_blank() -> None:
        """追加一个空行（避免连续空行，内部函数）。

        Returns:
            None
        """
        if out and out[-1] != "":
            out.append("")

    for episode in episodes:
        if multi:
            _push_blank()
            out.append(f"# {_episode_heading(episode)}")
            out.append("")
        plain = episode.get("plain") or {}
        scenes = plain.get("scenes") or []
        for idx, scene in enumerate(scenes if isinstance(scenes, list) else []):
            if not isinstance(scene, dict):
                continue
            _push_blank()  # 场景标题前空行
            out.append(_fountain_scene_heading(scene, idx))
            out.append("")  # 场景标题后空行
            action = str(scene.get("action") or scene.get("plot") or "").strip()
            if action:
                out.append(action)
                out.append("")
            dialogue = scene.get("dialogue")
            if isinstance(dialogue, list):
                for item in dialogue:
                    if not isinstance(item, dict):
                        continue
                    line = str(item.get("line") or "").strip()
                    if not line:
                        continue
                    speaker = _fountain_speaker(item)
                    if speaker:
                        out.append(speaker)
                    out.append(line)
                    out.append("")
    return "\n".join(out).strip() + "\n"
