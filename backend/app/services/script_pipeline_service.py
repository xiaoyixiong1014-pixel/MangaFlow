"""剧本创作流水线阶段守卫服务（文件级说明）。

P-1 范式级重构（Task 2）：剧本创作四阶段状态机
（idea 创意 → characters 角色 → outline 大纲 → body 正文）的
阶段守卫、推进与里程碑快照逻辑：
- require_phase(): 阶段前置守卫（未达所需阶段抛 409；script_phase 为 NULL
  表示快速模式，不拦截）；
- advance_phase(): 按序推进阶段（仅允许前进一格；跳步/回退/重复推进拒绝）；
- write_stage_snapshot(): 阶段确认/生成时写版本快照（V-01 里程碑留痕）。

设计约定：
- 阶段顺序以 SCRIPT_PHASES 元组下标为唯一事实来源（用 index 比较而非
  字符串大小或手写 if 链）；
- 本服务不主动 commit：写操作仅 add/flush，事务由调用方（API 层）统一
  提交或回滚。

P-5 剧本子图（Task 6）补充：
- save_character_candidates() / save_episode_plan() / confirm_character_entries()
  三组落库逻辑原在 api/script_pipeline.py 接口层内联实现；为让 LangGraph
  子图节点（app/agents/script_pipeline_graph.py）与 REST 接口共用同一数据
  落地（config 候选 / episodes 行 / characters 表），把它们下沉到本模块，
  API 层改为调用（行为保持不变）。commit 约定同上：由调用方统一提交。
"""

import copy
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Character, Episode, FilmOutput, Project, Script, Storyboard, VersionSnapshot
from app.services.script_service import (
    DEFAULT_VISUAL_TEMPLATE,
    build_character_bible_prompt,
)

# 剧本创作四阶段（有序元组：下标即推进顺序，唯一事实来源）
SCRIPT_PHASES = ("idea", "characters", "outline", "body")

# 阶段中文名称映射（提示文案 / 前端展示用）
SCRIPT_PHASE_LABELS = {
    "idea": "创意",
    "characters": "角色",
    "outline": "大纲",
    "body": "正文",
}

# 阶段前置动作提示（409 detail 文案前缀；键=所需阶段）
_PREREQ_ACTION = {
    "idea": "请先完成创意输入",
    "characters": "请先完成创意确认",
    "outline": "请先确认角色设定",
    "body": "请先确认单集大纲",
}

# 版本快照阶段取值（P-1 扩展的三个里程碑 + P2 故事设定两个 + P3 单集大纲
# 重生成一个 + P4 对话式改写采纳一个；对应 version_snapshots.stage 列
# varchar(30)，该列仅建索引无 CHECK 约束，扩值为自由取值的安全扩展——
# 取值长度均 < 30 字符）
# - setting_generated：故事设定生成/重生成完成（P2）
# - setting_edited：故事设定用户编辑保存（P2）
# - episode_outline_regenerated：单集大纲 AI 重生成完成（P3，留痕版本历史 +1）
# - assist_applied：对话式改写通道采纳片段落库（P4，redesign-script-workspace
#   Task 5：applied>0 时写一条留痕快照，applied=0 不写）
CONFIRM_STAGES = (
    "characters_confirmed", "outline_confirmed", "body_generated",
    "setting_generated", "setting_edited", "episode_outline_regenerated",
    "assist_applied",
)


def require_phase(project: Project, phase: str) -> None:
    """阶段前置守卫：校验项目当前阶段是否允许访问目标阶段的资源。

    规则：
    - project.script_phase 为 NULL（快速模式）→ 不拦截，直接放行；
    - 当前阶段序号 >= 所需阶段序号（等于或晚于）→ 放行；
    - 当前阶段序号 < 所需阶段序号（未达目标阶段）→ 抛 409，
      detail 带当前阶段与所需前置动作的中文提示。

    Args:
        project: 目标项目 ORM 对象（须含 script_phase 属性）。
        phase: 所需阶段（SCRIPT_PHASES 之一）。

    Returns:
        None：放行时无返回值。

    Raises:
        HTTPException: 400 阶段名非法（不在 SCRIPT_PHASES 中）；
            409 项目未进入剧本流水线或未达所需阶段。
    """
    # 阶段名合法性（编程/传参错误 → 400）
    if phase not in SCRIPT_PHASES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"未知的剧本创作阶段：{phase}（合法取值：{SCRIPT_PHASES}）")
    current: Optional[str] = project.script_phase
    # 快速模式（NULL）：不走分阶段守卫，直接放行
    if current is None:
        return
    # 允许等于或晚于所需阶段（用有序元组下标比较）
    if SCRIPT_PHASES.index(current) >= SCRIPT_PHASES.index(phase):
        return
    # 未达所需阶段：409 + 中文前置动作提示
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"{_PREREQ_ACTION[phase]}（当前阶段：{current}，所需阶段：{phase}）")


async def advance_phase(db: AsyncSession, project: Project, next_phase: str) -> str:
    """推进剧本创作阶段：校验只能按序前进一格后赋值。

    校验规则（以 SCRIPT_PHASES 下标为序）：
    - next_phase 必须是合法阶段（否则 400）；
    - 仅允许 next 序号 == 当前序号 + 1（当前为 NULL 视作 -1，即快速模式
      项目首次推进只允许进入 idea）；跳步（如 idea→body）、重复推进
      （next == current）、回退均拒绝（409）。

    Args:
        db: 异步数据库会话（本函数不做 flush/commit，事务由调用方统管）。
        project: 目标项目 ORM 对象（推进成功后其 script_phase 被就地更新，
            随调用方 commit 一起落库）。
        next_phase: 目标阶段（SCRIPT_PHASES 之一）。

    Returns:
        str: 推进后的阶段名（即 next_phase）。

    Raises:
        HTTPException: 400 阶段名非法；409 顺序不合法（跳步/重复/回退）。
    """
    # 阶段名合法性（编程/传参错误 → 400）
    if next_phase not in SCRIPT_PHASES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"未知的剧本创作阶段：{next_phase}（合法取值：{SCRIPT_PHASES}）")
    current: Optional[str] = project.script_phase
    # 当前序号：NULL（快速模式）视作 -1，首个可推进阶段为 idea（下标 0）
    current_index = SCRIPT_PHASES.index(current) if current is not None else -1
    next_index = SCRIPT_PHASES.index(next_phase)
    # 只允许前进一格：跳步/重复/回退均拒绝
    if next_index != current_index + 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(f"剧本创作阶段只能按序推进（{' → '.join(SCRIPT_PHASES)}），"
                    f"不能从「{current or '快速模式'}」直接推进到「{next_phase}」"))
    project.script_phase = next_phase
    return next_phase


async def write_stage_snapshot(
    db: AsyncSession,
    project: Project,
    stage: str,
    summary: Optional[dict] = None,
) -> VersionSnapshot:
    """写入阶段里程碑版本快照（characters/outline/body 确认或生成完成时）。

    仅 add + flush（填充自增主键并随当前事务写入），不 commit——
    与调用方的其他写操作（推进阶段、更新资产等）共用同一事务，
    由调用方统一提交或回滚。

    Args:
        db: 异步数据库会话。
        project: 归属项目 ORM 对象（须已有 id）。
        stage: 快照阶段（CONFIRM_STAGES 之一：characters_confirmed/
            outline_confirmed/body_generated/setting_generated/setting_edited）。
        summary: 快照摘要（JSONB：资产数/分镜数/成片数等），可为 None。

    Returns:
        VersionSnapshot: 已 flush 的快照 ORM 对象（含自增 id，
        但尚未提交到数据库）。

    Raises:
        ValueError: stage 不在 CONFIRM_STAGES 取值内（编程错误）。
    """
    # 快照阶段合法性（服务内部传参错误 → ValueError）
    if stage not in CONFIRM_STAGES:
        raise ValueError(
            f"未知的版本快照阶段：{stage}（合法取值：{CONFIRM_STAGES}）")
    snapshot = VersionSnapshot(project_id=project.id, stage=stage, summary=summary)
    db.add(snapshot)
    await db.flush()  # 填充自增主键，不提交事务
    return snapshot


# ---------------------------------------------------------------------------
# P-5 剧本子图与 REST 接口共用的落库函数（Task 6 抽取自 api/script_pipeline.py）
# ---------------------------------------------------------------------------
# 候选角色在 project.config 中的存储键（与 api/script_pipeline.py 原常量一致）
CANDIDATES_KEY = "script_characters"


def build_profile_description(
    gender: Optional[str],
    personality: Optional[str],
    appearance: Optional[str],
    motivation: Optional[str],
    arc: Optional[str],
    outfit: Optional[str] = None,
    style_note: Optional[str] = None,
    voice_note: Optional[str] = None,
) -> Optional[str]:
    """把人设要素拼接为 characters.description 摘要文本。

    性别放最前（生图性别锚定的人类可读位），视觉三段（外观/服装配饰/风格画质）
    连排在动机/弧光之前，便于 extract_appearance_from_description 按段提取
    「外观+服装配饰+风格画质」作为角色圣经视觉锁定描述；音色段供配音路由
    溯源（不参与视觉提取）。

    Args:
        gender: 角色性别（男性/女性/中性；可空）。
        personality: 性格设定。
        appearance: 形象特征。
        motivation: 行为动机。
        arc: 人物弧光。
        outfit: 服装配饰完整拆解（可选）。
        style_note: 风格与画质特点（可选）。
        voice_note: 音色设定（可选）。

    Returns:
        Optional[str]: 「性别：…；性格：…；外观：…；服装配饰：…；风格画质：…；
            音色：…；动机：…；弧光：…」形式摘要；全空时返回 None。

    Raises:
        无：纯字符串拼装。
    """
    parts = []
    if gender:
        parts.append(f"性别：{gender}")
    if personality:
        parts.append(f"性格：{personality}")
    if appearance:
        parts.append(f"外观：{appearance}")
    if outfit:
        parts.append(f"服装配饰：{outfit}")
    if style_note:
        parts.append(f"风格画质：{style_note}")
    if voice_note:
        parts.append(f"音色：{voice_note}")
    if motivation:
        parts.append(f"动机：{motivation}")
    if arc:
        parts.append(f"弧光：{arc}")
    return "；".join(parts) or None


def save_character_candidates(db: AsyncSession, project: Project, candidates: List[dict]) -> List[dict]:
    """把 LLM 生成的角色候选写入 project.config["script_characters"]。

    每条候选带 8 位 hex 临时 id 与 confirmed=False 标记；config 列整体
    重新赋值以触发 SQLAlchemy JSONB 变更检测（就地修改不会被 UPDATE 捕获）。
    纯内存操作不触库（db 参数预留扩展位），事务由调用方统一提交。

    Args:
        db: 异步数据库会话（本函数不使用，保持与其它 service 函数签名一致）。
        project: 归属项目 ORM 对象（就地更新其 config）。
        candidates: plan_characters 产出的规整候选列表。

    Returns:
        List[dict]: 实际写入 config 的候选列表（含临时 id 与 confirmed 标记）。

    Raises:
        无：纯字典构造与赋值。
    """
    stage = [{"id": uuid.uuid4().hex[:8], "confirmed": False, **c} for c in candidates]
    project.config = {**(project.config or {}), CANDIDATES_KEY: stage}
    return stage


async def list_project_episodes(db: AsyncSession, project_id: int) -> List[Episode]:
    """查询项目全部剧集并按 sequence 升序（平局按 id）返回。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。

    Returns:
        List[Episode]: 剧集 ORM 对象列表（sequence 升序；无剧集返回空列表）。

    Raises:
        无：纯查询。
    """
    rows = await db.execute(
        select(Episode)
        .where(Episode.project_id == project_id)
        .order_by(Episode.sequence.asc(), Episode.id.asc())
    )
    return list(rows.scalars().all())


async def _unbind_episode_refs(db: AsyncSession, project_id: int, episode_id: int) -> None:
    """解绑引用指定剧集的数据行（episode_id 置 NULL，保留数据本体）。

    scripts/storyboards/film_outputs 三表的 episode_id 指向该剧集的行统一
    置 NULL（三表外键本就 ondelete=SET NULL，显式置 NULL 以兼容未声明
    数据库级外键的存量环境）。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID（限定解绑范围在本项目内）。
        episode_id: 待解绑的剧集 ID。

    Returns:
        None。

    Raises:
        无：查询与属性赋值，异常向上传播由调用方处理。
    """
    for model in (Script, Storyboard, FilmOutput):
        rows = await db.execute(
            select(model).where(
                model.episode_id == episode_id,
                model.project_id == project_id,
            )
        )
        for row in rows.scalars().all():
            row.episode_id = None


async def save_episode_plan(db: AsyncSession, project: Project, plan_items: List[dict]) -> None:
    """把分集大纲落 episodes 表（多退少补）并推进阶段到 outline。

    落库规则（与原 api/script_pipeline.py 接口层实现一致）：
    - 既有剧集行按 sequence 复用更新（title/logline/outline 整体重赋值）；
    - 集数不足时新建（sequence 连续接在复用行之后）；
    - 多余行硬删，删除前解绑 scripts/storyboards/film_outputs 的 episode_id
      引用（数据保留）；
    - sequence 两阶段重写（先统一置临时负值 flush，再回写 1..n）避开
      uk_episodes_seq 唯一索引；
    - 阶段特例：NULL（快速模式）/idea/characters 直接置 outline（跨两格
      流水线起点赋值，不经 advance_phase 一格校验）。
    本函数只写不提交（含必要的中间 flush），事务由调用方统一提交。

    Args:
        db: 异步数据库会话。
        project: 归属项目 ORM 对象（script_phase 可能被就地更新）。
        plan_items: 规整后的分集大纲列表（元素含 title/logline/core_event/
            characters/emotion_curve/duration_sec）。

    Returns:
        None。

    Raises:
        无：结构异常按缺字段兜底；数据库异常向上传播由调用方处理。
    """
    existing = await list_project_episodes(db, project.id)
    keep = existing[:len(plan_items)]
    drop = existing[len(plan_items):]
    # 多余行硬删：先解绑 scripts/storyboards/film_outputs 引用（数据保留）
    for row in drop:
        await _unbind_episode_refs(db, project.id, row.id)
        await db.delete(row)
    # 两阶段重写 sequence 避开 uk_episodes_seq 唯一索引（与 reorder 同策略：
    # 先统一置临时负值 flush，再回写 1..n）
    for row in keep:
        row.sequence = -(row.id)
    await db.flush()
    for seq, (item, row) in enumerate(zip(plan_items, keep), start=1):
        row.sequence = seq
        row.title = item["title"]
        row.logline = item["logline"] or None
        # outline JSONB 整体重新赋值触发变更检测（就地修改不会被 UPDATE 捕获）
        row.outline = {
            "core_event": item["core_event"],
            "characters": item["characters"],
            "emotion_curve": item["emotion_curve"],
            "duration_sec": item["duration_sec"],
        }
    # 新生成的集数多于既有行：新建（接在复用行之后，sequence 连续）
    for seq in range(len(keep) + 1, len(plan_items) + 1):
        item = plan_items[seq - 1]
        db.add(Episode(
            project_id=project.id,
            sequence=seq,
            title=item["title"],
            logline=item["logline"] or None,
            outline={
                "core_event": item["core_event"],
                "characters": item["characters"],
                "emotion_curve": item["emotion_curve"],
                "duration_sec": item["duration_sec"],
            },
            status="draft",
        ))
    # 阶段特例：NULL（快速模式）/idea/characters 允许直接进入 outline
    # （流水线起点：NULL→outline 属于跨两格赋值，不经 advance_phase 一格校验）
    if project.script_phase in (None, "idea", "characters"):
        project.script_phase = "outline"
    # flush 使新增剧集行在本事务内可见（SessionLocal autoflush=False，调用方
    # 若在 commit 前查询需要依赖本次 flush；commit 语义仍由调用方统一执行）
    await db.flush()


async def confirm_character_entries(db: AsyncSession, project: Project, entries: List[dict]) -> List[str]:
    """角色确认入池（同名幂等）并回写 config 候选标记。

    语义与原 api/script_pipeline.py confirm 接口一致：
    - 请求内同名去重，避免撞 (project_id, name) 唯一索引；
    - 条目人设字段为 None 时沿用同名候选值（按 name 在候选中匹配）；
    - 幂等入池：同名（未删）角色更新人设字段，否则新建（visual_prompt
      缺省用 DEFAULT_VISUAL_TEMPLATE 模板；description 为人设四要素摘要；
      relations 存 JSONB；confirmed_at=now；source="script_planner"）；
    - 回写 config 候选：确认条目同步设定并标记 confirmed=True；候选中无
      同名（用户新增角色）时追加为新候选。
    本函数只写不提交，事务由调用方统一提交。

    Args:
        db: 异步数据库会话。
        project: 归属项目 ORM 对象（config 候选可能被就地重写）。
        entries: 确认条目列表（dict 形式，键为 name/role_type/age/personality/
            appearance/motivation/arc/relations；REST 层由 Pydantic
            model_dump() 转换，子图层直接传候选 dict）。

    Returns:
        List[str]: 实际入池的角色名列表（已去重，保持请求顺序）。

    Raises:
        无：结构异常按缺数据处理；数据库异常向上传播由调用方处理。
    """
    candidates_raw = (project.config or {}).get(CANDIDATES_KEY)
    # 深拷贝隔离：就地修改不影响旧值引用，整体重写时才能触发 JSONB 变更检测
    candidates: List[dict] = (
        copy.deepcopy([c for c in candidates_raw if isinstance(c, dict)])
        if isinstance(candidates_raw, list) else []
    )
    confirmed_names: List[str] = []
    confirmed_at = datetime.now(timezone.utc)
    seen: set = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name or name in seen:
            continue  # 请求内同名去重
        seen.add(name)
        # None 字段沿用候选值（按 name 在候选中匹配；改名确认则全取条目自身）
        cand = next(
            (c for c in candidates if str(c.get("name") or "") == name), None)
        cand = cand if isinstance(cand, dict) else {}
        values = {
            "role_type": item.get("role_type") or cand.get("role_type"),
            "gender": item.get("gender") or cand.get("gender"),
            "age": item.get("age") or cand.get("age"),
            "personality": item.get("personality") or cand.get("personality"),
            "appearance": item.get("appearance") or cand.get("appearance"),
            "outfit": item.get("outfit") or cand.get("outfit"),
            "style_note": item.get("style_note") or cand.get("style_note"),
            "voice_note": item.get("voice_note") or cand.get("voice_note"),
            "motivation": item.get("motivation") or cand.get("motivation"),
            "arc": item.get("arc") or cand.get("arc"),
            "relations": (item.get("relations")
                          if item.get("relations") is not None
                          else list(cand.get("relations") or [])),
        }
        description = build_profile_description(
            values["gender"], values["personality"], values["appearance"],
            values["motivation"], values["arc"],
            outfit=values["outfit"], style_note=values["style_note"],
            voice_note=values["voice_note"])
        # 角色圣经视觉三段（形象特征 + 服装配饰 + 风格画质），生图一致性锁定用
        visual_bible = build_character_bible_prompt(
            name, values["age"],
            "；".join(v for v in (
                values["appearance"], values["outfit"], values["style_note"]) if v))

        # 幂等入池：同名（未删）角色更新人设字段，否则新建
        exist = (
            await db.execute(
                select(Character).where(
                    Character.project_id == project.id,
                    Character.name == name,
                    Character.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if exist is not None:
            exist.role_type = values["role_type"]
            exist.gender = values["gender"]
            exist.age = values["age"]
            exist.personality = values["personality"]
            exist.outfit = values["outfit"]
            exist.style_note = values["style_note"]
            exist.voice_note = values["voice_note"]
            exist.motivation = values["motivation"]
            exist.arc = values["arc"]
            exist.relations = values["relations"]
            exist.description = description or exist.description
            # 圣经 visual_prompt：仅在存在视觉设定时同步重建（保留用户手改的
            # 定制提示词——视觉三段全空不动 visual_prompt）
            if (values["appearance"] or values["outfit"] or values["style_note"]):
                exist.visual_prompt = (
                    f"统一视觉风格，角色圣经（固定外貌与服装，跨图一致）："
                    f"{visual_bible}。"
                    "全身立绘，纯色背景，高细节；五官/发型/服装/画风严格按上述设定，禁止变更。")
            exist.confirmed_at = confirmed_at
        else:
            db.add(Character(
                project_id=project.id,
                name=name,
                aliases=[],
                description=description,
                # 角色圣经固定前缀（AI 漫剧行业惯例）：生图提示词以视觉三段
                # （形象特征+服装配饰+风格画质）开头，跨镜头保持一致性；
                # 三段全空时回退既有缺省模板（description 兜底）
                visual_prompt=(
                    f"统一视觉风格，角色圣经（固定外貌与服装，跨图一致）："
                    f"{visual_bible}。"
                    "全身立绘，纯色背景，高细节；五官/发型/服装/画风严格按上述设定，禁止变更。"
                    if (values["appearance"] or values["outfit"] or values["style_note"] or values["age"])
                    else DEFAULT_VISUAL_TEMPLATE.format(
                        name=name, description=description or name)
                ),
                role_type=values["role_type"],
                gender=values["gender"],
                age=values["age"],
                personality=values["personality"],
                outfit=values["outfit"],
                style_note=values["style_note"],
                voice_note=values["voice_note"],
                motivation=values["motivation"],
                arc=values["arc"],
                relations=values["relations"],
                confirmed_at=confirmed_at,
                source="script_planner",
            ))
        confirmed_names.append(name)

        # 回写候选：确认条目同步设定并标记 confirmed=True；候选中无同名
        # （用户在确认清单中新增的角色）时追加为新候选
        if cand:
            cand.update({**values, "confirmed": True})
        else:
            candidates.append({
                "id": uuid.uuid4().hex[:8],
                "confirmed": True,
                "name": name,
                **values,
            })

    project.config = {**(project.config or {}), CANDIDATES_KEY: candidates}
    # flush 使新增角色行在本事务内可见（autoflush=False；子图节点确认后需要
    # 在同事务内回查已入池角色清单）
    await db.flush()
    return confirmed_names
