"""剧本创作流水线路由模块（文件级说明，P-1 范式级重构 Task 3/4/5）。

实现 P-2 角色设定阶段（Task 3）、P-3 分集大纲阶段（Task 4）与 P-4 逐集
正文阶段（Task 5）全部接口（每个接口先经 get_owned_project 校验项目归属，
他人或已删除项目一律 404，不泄露存在性）：

角色域（Task 3）：
- POST /api/projects/{project_id}/script/characters/generate            LLM 生成角色班底候选
- GET  /api/projects/{project_id}/script/characters                     候选 + 已入池角色列表
- PUT  /api/projects/{project_id}/script/characters                     批量编辑候选
- POST /api/projects/{project_id}/script/characters/{cid}/regenerate    单角色重新生成
- POST /api/projects/{project_id}/script/characters/confirm             确认入池并推进阶段

故事设定域（P2，redesign-script-workspace Task 2）：
- POST /api/projects/{project_id}/script/setting/generate               LLM 生成故事设定三段 + 人物候选
- GET  /api/projects/{project_id}/script/setting                        设定内容（可空）+ 人物候选/已确认双组
- PUT  /api/projects/{project_id}/script/setting                        合并编辑设定三段（source=user）
- POST /api/projects/{project_id}/script/setting/regenerate             重出设定段（已确认角色不回退）

分集大纲域（Task 4）：
- POST /api/projects/{project_id}/script/episodes/plan                  LLM 生成分集大纲（复用/新建/删多余 episodes 行）
- GET  /api/projects/{project_id}/script/episodes/plan                  分集大纲列表（按 sequence 升序）
- PUT  /api/projects/{project_id}/script/episodes/{eid}/outline         单集大纲编辑（含 sequence 互换重排）
- POST /api/projects/{project_id}/script/episodes/{eid}/outline/regenerate
        单集大纲 AI 重生成（P3，redesign-script-workspace Task 4：作用域
        收窄为 1 集，复用 plan_episodes 提示词骨架与规整校验；仅更新该集
        title/logline/outline，写 episode_outline_regenerated 快照留痕；
        该集已有正文时响应携带 body_exists=true 提醒，不自动删正文）
- POST /api/projects/{project_id}/script/episodes/plan/confirm          全量确认大纲并推进阶段到 body

正文域（Task 5，精细流水线专属：phase 必须为 body，NULL 快速模式不放行）：
- POST /api/projects/{project_id}/script/episodes/{eid}/body/generate/stream
        SSE 流式生成单集正文（两段式：文章体正文 delta 分段流式 → 结构化
        场次 → done；scripts 行按 (project_id, episode_id) upsert + 乐观锁）
- PUT  /api/projects/{project_id}/script/episodes/{eid}/body            保存用户修改（场次级 source 合并）
- POST /api/projects/{project_id}/script/episodes/{eid}/body/regenerate 单集正文重生成（要求已有正文，覆盖 + version+1）

对话式改写通道（P4，redesign-script-workspace Task 5）：
- POST /api/projects/{project_id}/script/assist
        对话式改写预览（scope=setting/episode_outline/episode_body，
        不落库）：setting 作用 story_setting 三段（phase=body 锁定 409、
        未生成 409）；episode_outline 作用该集 title/logline/outline
        （守卫对齐单集重生成口径）；episode_body 直接复用 optimize 预览
        路径（grain=global）。diff 片段 id 口径与 optimize 一致。
- POST /api/projects/{project_id}/script/assist/apply
        采纳片段落库（path 白名单按 scope + original 与当前库值一致性
        校验，不一致 skipped）：setting 置 source=user；episode_body
        复用 optimize/apply 落库口径（场次 source=ai_optimized + 乐观锁）；
        applied>0 写一条 stage=assist_applied 版本快照，applied=0 不递增
        版本不写快照。

子图断线恢复域（Task 6 / P-5）：
- POST /api/projects/{project_id}/script/pipeline/run/stream
        启动/续跑剧本子图（SSE 事件：node / pending_confirm / error / done）；
        thread_id=f"script_{project_id}_{user.id}"（同项目同用户幂等续跑同
        一线程，已挂起确认时直接回放 pending_confirm 而不重跑图）。
- POST /api/projects/{project_id}/script/pipeline/resume
        确认/退回续跑（body {decision, reason?, edited_payload?} →
        Command(resume=...)），SSE 事件同上。
- GET  /api/projects/{project_id}/script/pipeline/state?thread_id=xxx
        剧本子图检查点回放（next_nodes / pending_interrupt / 进度摘要）。
- GET  /api/projects/{project_id}/script/pipeline/status
        断线重连状态（script_phase / current_stage / pending_stage /
        has_pending_confirm / 进度摘要），供前端 Task 11 刷新恢复用。
        与 REST 三阶段接口共存：REST 是「分步驱动」路径（Studio 四步页面
        主用）；LangGraph 子图（app/agents/script_pipeline_graph.py）是
        「整跑 + 断点续跑」路径（Agent 会话主用），两者共享同一数据落地
        （config 候选 / episodes / scripts，落库逻辑统一在
        services/script_pipeline_service.py），前端 Task 11 按需选择。

数据约定：
- 未确认的候选角色存 project.config JSONB 的 "script_characters" 键
  （每条带 8 位 hex 临时 id 与 confirmed 标记，不落库）；
- 故事设定存 projects.story_setting JSONB（P2：{logline, core_conflict,
  worldview, source: "ai"|"user", updated_at: ISO 时间戳}；NULL=尚未生成）；
  生成/重生成置 source="ai" 并写 setting_generated 快照；编辑置 source="user"
  并写 setting_edited 快照（不改阶段）；重生成时 config 中 confirmed=True
  的候选不动、未入池候选随重生成刷新（与已确认同名的新候选丢弃）；
- 确认后写入 characters 资产表（同名幂等更新/新建，confirmed_at 落值），
  并把确认结果回写 config 候选（confirmed=True 标记）；
- 分集大纲落 episodes 表（title/logline 列 + outline JSONB：
  core_event/characters/emotion_curve/duration_sec；confirmed_at 为单集
  大纲确认时间）；plan 重生成时既有行按 sequence 复用更新，多退少补，
  删除多余行前解绑 scripts/storyboards/film_outputs 的 episode_id 引用；
- 阶段推进：generate 时 NULL/idea 直接置 characters（进入流水线起点特例，
  NULL→characters 属于跨两格赋值，不经 advance_phase 的一格校验）；
  confirm 时 characters → outline（经 advance_phase 校验）；
  plan 生成时 NULL/idea/characters 直接置 outline（NULL→outline 为跨两格
  起点特例）；大纲 confirm 时 outline → body（经 advance_phase 校验）；
  phase=body 后分集大纲锁定：plan 重生成拒绝（409），单集仅允许编辑文案
  字段、禁止 sequence 重排；
- 正文落 scripts 表（episode_id 绑定该集，uk_scripts_project 保证
  (project_id, episode_id) 唯一）：raw_text=文章体全文，content=RD-03
  字段级包装（title/logline/locations/scenes，source=ai）；episode.status
  置 scripted；保存接口落库为整体包装 {"value": merged, "source": "user"}
  （场次级 source 已按 merge_scene_sources 合并）。

注意：FastAPI 的 router prefix 不能携带路径参数，因此本路由不设 prefix，
路由内部直接书写完整路径。LLM 相关失败统一 502。
"""

import copy
import logging
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from langgraph.types import Command
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm_adapter import get_last_usage
from app.agents.script_pipeline import (
    ASSIST_PATH_WHITELIST,
    assist_rewrite,
    optimize_episode_content,
    plan_characters,
    plan_episode_outline_autofill,
    plan_episode_outline_regen,
    plan_episodes,
    plan_episode_scenes,
    plan_story_setting,
    is_placeholder_episode_title,
    rewrite_quote,
    stream_episode_body,
)
from app.agents.script_pipeline_graph import get_compiled_script_pipeline
from app.core.database import get_db
from app.core.security import get_current_user
from app.core.sse import sse_event, sse_stream
from app.models import Character, Episode, Project, Script, User
from app.schemas.script import validate_script_body_content
from app.schemas.script_pipeline import (
    AssistApplyOut,
    AssistApplyRequest,
    AssistPreviewOut,
    AssistPreviewRequest,
    CharacterBatchEditRequest,
    CharacterCandidatesOut,
    CharacterConfirmOut,
    CharacterConfirmRequest,
    CharacterGenerateRequest,
    CharacterListOut,
    CharacterProfileOut,
    CharacterRegenerateOut,
    CharacterRegenerateRequest,
    EpisodeBodyOut,
    EpisodeBodySaveRequest,
    EpisodeOptimizeApplyOut,
    EpisodeOptimizeApplyRequest,
    EpisodeOptimizeOut,
    EpisodeOptimizeRequest,
    EpisodeOutlineAutofillOut,
    EpisodeOutlineEditRequest,
    EpisodeOutlineRegenerateOut,
    EpisodeOutlineRegenerateRequest,
    EpisodePlanConfirmOut,
    EpisodePlanGenerateRequest,
    EpisodePlanOut,
    EpisodePlanOutItem,
    PipelineInterruptOut,
    PipelineStateOut,
    QuoteRewriteOut,
    QuoteRewriteRequest,
    ScriptPipelineResumeRequest,
    ScriptPipelineRunRequest,
    ScriptPipelineStatusOut,
    StorySettingDetailOut,
    StorySettingEditOut,
    StorySettingEditRequest,
    StorySettingGenerateOut,
    StorySettingOut,
    StorySettingRegenerateRequest,
)
from app.services.script_pipeline_service import (
    advance_phase,
    build_profile_description as _build_profile_description,
    confirm_character_entries,
    list_project_episodes as _list_project_episodes,
    save_character_candidates,
    save_episode_plan,
    write_stage_snapshot,
)
from app.services.script_service import (
    PATH_MISSING,
    acquire_generate_lock,
    apply_optimize_changes,
    build_character_bible_prompt,
    build_extract_source_text,
    build_novel_context,
    build_field_diffs,
    content_has_payload,
    content_plain,
    get_owned_project,
    get_value_by_path,
    is_optimizable_path,
    merge_scene_sources,
    release_generate_lock,
    wrap_content_fields,
)

router = APIRouter(tags=["script-pipeline"])

logger = logging.getLogger(__name__)

# 候选角色在 project.config 中的存储键
_CANDIDATES_KEY = "script_characters"

# 暂存小说章节在 project.config 中的键（P-7，与 /script/input/novel/parse 一致）
_NOVEL_CHAPTERS_KEY = "novel_chapters"

# 分集映射集数上限（与 EpisodePlanGenerateRequest.target_episodes 的 le=100 对齐）
_EPISODE_TARGET_MAX = 100


# ---------------------------------------------------------------------------
# 内部工具函数（候选读写 / 结构转换 / 描述拼接）
# ---------------------------------------------------------------------------
def _load_candidates(project: Project) -> List[dict]:
    """从 project.config 读取候选角色列表（内部函数，结构容错 + 深拷贝隔离）。

    必须深拷贝：调用方对候选的就地修改（update 元素 / 标记 confirmed）不能
    波及 project.config 的旧值引用，否则 _save_candidates 整体重新赋值时
    SQLAlchemy 的 flush 比较会发现新旧值相等而跳过 UPDATE（JSONB 就地修改
    的经典陷阱），导致变更静默丢失。

    Args:
        project: 已校验归属的项目 ORM 对象。

    Returns:
        List[dict]: 候选列表的深拷贝（config 缺失、非列表或元素非 dict 时
            返回空列表）。

    Raises:
        无：config 结构异常按空候选处理。
    """
    raw = (project.config or {}).get(_CANDIDATES_KEY)
    if not isinstance(raw, list):
        return []
    return copy.deepcopy([item for item in raw if isinstance(item, dict)])


def _save_candidates(project: Project, candidates: List[dict]) -> None:
    """把候选列表整体写回 project.config（内部函数，触发 JSONB 变更检测）。

    JSONB 列的就地修改（append/update 元素）不会被 SQLAlchemy 变更检测
    捕获，必须对 config 列整体重新赋值（config = {**config, ...}）。

    Args:
        project: 已校验归属的项目 ORM 对象（就地更新其 config）。
        candidates: 新的候选列表。

    Returns:
        None。

    Raises:
        无：纯字典构造与赋值。
    """
    project.config = {**(project.config or {}), _CANDIDATES_KEY: candidates}


def _candidate_to_out(candidate: dict) -> CharacterProfileOut:
    """把 config 候选 dict 转为输出模型（内部函数，source=planned）。

    Args:
        candidate: config 中的候选条目（含 id/name/人设字段/confirmed）。

    Returns:
        CharacterProfileOut: 候选档案（id=None，candidate_id=临时 ID）。

    Raises:
        无：字段缺失按 None/空列表兜底（Pydantic 校验）。
    """
    return CharacterProfileOut(
        id=None,
        candidate_id=str(candidate.get("id") or "") or None,
        name=str(candidate.get("name") or "").strip() or "未命名",
        role_type=candidate.get("role_type"),
        age=candidate.get("age"),
        personality=candidate.get("personality"),
        appearance=candidate.get("appearance"),
        outfit=candidate.get("outfit"),
        style_note=candidate.get("style_note"),
        motivation=candidate.get("motivation"),
        arc=candidate.get("arc"),
        relations=list(candidate.get("relations") or []),
        confirmed_at=None,
        source="planned",
    )


def _character_to_out(character: Character) -> CharacterProfileOut:
    """把已入池的 Character ORM 对象转为输出模型（内部函数，source=confirmed）。

    Args:
        character: characters 表 ORM 对象（含 P-1 人设扩展列）。

    Returns:
        CharacterProfileOut: 已确认档案（candidate_id=None；appearance
            回读映射自 characters.description 摘要）。

    Raises:
        无：Pydantic 结构校验。
    """
    return CharacterProfileOut(
        id=character.id,
        candidate_id=None,
        name=character.name,
        role_type=character.role_type,
        age=character.age,
        personality=character.personality,
        appearance=character.description,
        outfit=character.outfit,
        style_note=character.style_note,
        motivation=character.motivation,
        arc=character.arc,
        relations=list(character.relations or []),
        confirmed_at=character.confirmed_at,
        source="confirmed",
    )


def _candidates_out(project: Project) -> List[CharacterProfileOut]:
    """读取 config 候选并转换为输出模型列表（内部函数）。

    Args:
        project: 已校验归属的项目 ORM 对象。

    Returns:
        List[CharacterProfileOut]: 候选档案列表（无候选时为空列表）。

    Raises:
        无：结构异常按空候选处理。
    """
    return [_candidate_to_out(c) for c in _load_candidates(project)]


async def _get_source_text(db: AsyncSession, project: Project) -> Optional[str]:
    """读取剧本输入源文本供 LLM 提示词使用（内部函数）。

    分阶段流水线按剧集落库（每集一行），单行 scalar_one_or_none 在多行时
    抛 MultipleResultsFound（与 decompose_storyboards 同源问题）——统一读取
    全部行按剧集序合并：逐行取 raw_text 非空，否则取结构化 content 解包的
    纯文本（build_extract_source_text），各段以空行拼接；均无 → None。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。

    Returns:
        Optional[str]: 源文本；项目尚无剧本行或剧本完全为空时返回 None。

    Raises:
        无：build_extract_source_text 的 400 分支仅在已确认有 payload 后
            才会走到，实际不会触发。
    """
    # 剧集序映射（默认行 episode_id NULL 视为第 0 集排最前）
    episode_order: Dict[int, int] = {
        ep.id: (ep.sequence if ep.sequence is not None else 0)
        for ep in (
            await db.execute(select(Episode).where(Episode.project_id == project.id))
        ).scalars().all()
    }
    script_rows = (
        await db.execute(select(Script).where(Script.project_id == project.id))
    ).scalars().all()
    script_rows = sorted(
        script_rows,
        key=lambda s: (
            0 if s.episode_id is None else episode_order.get(s.episode_id, 10**9),
            s.id,
        ),
    )
    segments: List[str] = []
    for row in script_rows:
        raw_text = (row.raw_text or "").strip()
        if raw_text:
            segments.append(raw_text)
            continue
        plain = content_plain(row.content)
        if content_has_payload(plain):
            segments.append(build_extract_source_text(row))
    return "\n\n".join(segments).strip() or None


def _require_script_input(script: Optional[Script]) -> None:
    """校验剧本已保存输入（内部函数：raw_text 或 content 有实质 payload）。

    Args:
        script: 项目剧本行（可能为 None）。

    Returns:
        None。

    Raises:
        HTTPException: 409 —— 项目尚未保存任何剧本输入（提示先保存输入）。
    """
    if script is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="请先保存剧本输入")
    plain = content_plain(script.content)
    if not (script.raw_text or "").strip() and not content_has_payload(plain):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="请先保存剧本输入")


# ---------------------------------------------------------------------------
# 1. POST /characters/generate：LLM 生成角色班底候选
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/script/characters/generate",
             response_model=CharacterCandidatesOut)
async def generate_characters(
    project_id: int,
    req: Optional[CharacterGenerateRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CharacterCandidatesOut:
    """LLM 生成角色班底候选：结果写入 project.config["script_characters"]。

    流程：归属校验 → 校验剧本已有输入（无则 409）→ 调 plan_characters
    （内部失败重试 2 次，仍失败 502）→ 候选写 config（每条带 8 位 hex
    临时 id 与 confirmed=False）→ 阶段为 NULL/idea 时置 characters
    （进入流水线起点特例：NULL 视作 -1，允许直接置 characters）→ 提交。

    Args:
        project_id: 项目 ID。
        req: 可选请求体（instruction 为用户对角色班底的自然语言要求）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        CharacterCandidatesOut: {"characters": 候选列表, "phase": 当前阶段}。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目；409 —— 剧本尚未保存
            输入；502 —— LLM 生成与解析多次失败。
    """
    project = await get_owned_project(db, project_id, user.id)
    script = (
        await db.execute(select(Script).where(Script.project_id == project.id))
    ).scalar_one_or_none()
    _require_script_input(script)
    # 源文本：raw_text 优先，无则取结构化剧本纯文本（前面已确认有内容）
    source_text = (script.raw_text or "").strip() or build_extract_source_text(script)

    candidates = await plan_characters(
        project, source_text, instruction=(req.instruction if req else None))
    # 候选写 config（每条带 8 位 hex 临时 id 与 confirmed=False；落库逻辑与
    # 子图节点共用，service 只写不提交）
    save_character_candidates(db, project, candidates)
    # 阶段特例：NULL（快速模式）/idea 允许直接进入 characters（流水线起点）
    if project.script_phase in (None, "idea"):
        project.script_phase = "characters"
    await db.commit()
    return CharacterCandidatesOut(characters=_candidates_out(project),
                                  phase=project.script_phase)


# ---------------------------------------------------------------------------
# 2. GET /characters：候选 + 已入池角色列表
# ---------------------------------------------------------------------------
@router.get("/api/projects/{project_id}/script/characters",
            response_model=CharacterListOut)
async def list_characters(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CharacterListOut:
    """读取角色设定双列表：planned=config 候选（未生成过为空），confirmed
    为 characters 表中 confirmed_at 非空的已确认角色（含人设字段）。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        CharacterListOut: {"planned": [...], "confirmed": [...], "phase": ...}。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目。
    """
    project = await get_owned_project(db, project_id, user.id)
    confirmed_rows = (
        await db.execute(
            select(Character)
            .where(
                Character.project_id == project.id,
                Character.deleted_at.is_(None),
                Character.confirmed_at.is_not(None),
            )
            .order_by(Character.id)
        )
    ).scalars().all()
    return CharacterListOut(
        planned=_candidates_out(project),
        confirmed=[_character_to_out(row) for row in confirmed_rows],
        phase=project.script_phase,
    )


# ---------------------------------------------------------------------------
# 3. PUT /characters：批量编辑候选（仅动 config，不碰已入池资产）
# ---------------------------------------------------------------------------
@router.put("/api/projects/{project_id}/script/characters",
            response_model=CharacterCandidatesOut)
async def edit_characters(
    project_id: int,
    req: CharacterBatchEditRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CharacterCandidatesOut:
    """批量编辑候选角色：upsert（按候选临时 id 优先、其次 name 匹配更新/
    新增）+ remove（按 id/name 删除），写回 project.config 并返回候选列表。

    已确认入池的角色不在本接口编辑（走既有资产 PATCH /api/assets 接口）。

    Args:
        project_id: 项目 ID。
        req: 批量编辑请求（upsert 条目 + remove_ids/remove_names）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        CharacterCandidatesOut: {"characters": 编辑后的候选列表, "phase": ...}。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目。
    """
    project = await get_owned_project(db, project_id, user.id)
    candidates = _load_candidates(project)

    # 先执行删除：按临时 id 或角色名移除
    remove_ids = {str(v) for v in req.remove_ids}
    remove_names = set(req.remove_names)
    if remove_ids or remove_names:
        candidates = [
            c for c in candidates
            if str(c.get("id") or "") not in remove_ids
            and str(c.get("name") or "") not in remove_names
        ]

    # 再执行 upsert：id 优先匹配，未命中按 name 匹配，均未命中则新增
    for item in req.upsert:
        updates = {"name": item.name}
        for field in ("role_type", "age", "personality", "appearance",
                      "outfit", "style_note", "motivation", "arc"):
            value = getattr(item, field)
            if value is not None:
                updates[field] = value
        if item.relations is not None:
            updates["relations"] = [r.model_dump() for r in item.relations]
        target = None
        if item.id:
            target = next(
                (c for c in candidates if str(c.get("id") or "") == item.id), None)
        if target is None:
            target = next(
                (c for c in candidates if str(c.get("name") or "") == item.name), None)
        if target is not None:
            target.update(updates)
        else:
            candidates.append(
                {"id": uuid.uuid4().hex[:8], "confirmed": False, **updates})

    _save_candidates(project, candidates)
    await db.commit()
    return CharacterCandidatesOut(characters=_candidates_out(project),
                                  phase=project.script_phase)


# ---------------------------------------------------------------------------
# 4. POST /characters/{cid}/regenerate：单角色重新生成
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/script/characters/{cid}/regenerate",
             response_model=CharacterRegenerateOut)
async def regenerate_character(
    project_id: int,
    cid: str,
    req: Optional[CharacterRegenerateRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CharacterRegenerateOut:
    """单角色重新生成：优先在 config 候选中按临时 id 匹配；未命中时尝试
    按 characters 表主键匹配已确认角色，两者皆无 → 404。

    - 候选路径：构造「重新设计角色 {name}（当前设定：…）+ 用户 instruction」
      单角色指令调 plan_characters，取返回的第一个角色替换该候选设定
      （保留原候选 id 与 confirmed 标记，其余候选不动）；
    - 已入池路径：以该角色 name 作为 instruction 调 plan_characters，
      取第一个角色更新 DB 行的人设字段（name 保持不动，避免撞唯一索引）。

    Args:
        project_id: 项目 ID。
        cid: 候选临时 ID（8 位 hex）或已入池角色数字 ID 的字符串形式。
        req: 可选请求体（instruction 为本轮重生成方向描述）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        CharacterRegenerateOut: {"character": 重生成后的档案, "phase": ...}。

    Raises:
        HTTPException: 404 —— 项目不存在或 cid 无法匹配任何候选/角色；
            502 —— LLM 生成与解析多次失败。
    """
    project = await get_owned_project(db, project_id, user.id)
    source_text = await _get_source_text(db, project) or ""
    user_instruction = (req.instruction if req else None) or ""

    # ---------- 路径 1：config 候选按临时 id 匹配 ----------
    candidates = _load_candidates(project)
    target = next((c for c in candidates if str(c.get("id") or "") == cid), None)
    if target is not None:
        # 当前设定摘要（供 LLM 参考原设计）
        digest = _build_profile_description(
            target.get("personality"), target.get("appearance"),
            target.get("motivation"), target.get("arc")) or target.get("name") or ""
        instruction = f"重新设计角色「{target.get('name')}」（当前设定：{digest}）"
        if user_instruction:
            instruction += f"。{user_instruction}"
        result = await plan_characters(project, source_text, instruction=instruction)
        new_char = result[0]
        # 替换设定字段：保留原候选 id 与 confirmed 标记，其余候选不动
        target.update({
            "name": new_char["name"],
            "role_type": new_char["role_type"],
            "age": new_char["age"],
            "personality": new_char["personality"],
            "appearance": new_char["appearance"],
            "outfit": new_char.get("outfit"),
            "style_note": new_char.get("style_note"),
            "motivation": new_char["motivation"],
            "arc": new_char["arc"],
            "relations": new_char["relations"],
        })
        _save_candidates(project, candidates)
        await db.commit()
        return CharacterRegenerateOut(character=_candidate_to_out(target),
                                      phase=project.script_phase)

    # ---------- 路径 2：已入池角色按数字主键匹配 ----------
    try:
        character_id = int(cid)
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="角色不存在")
    row = await db.get(Character, character_id)
    if row is None or row.project_id != project.id or row.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="角色不存在")
    # 以角色 name 作为 instruction 重新设计（名字不回写，仅更新人设字段）
    instruction = f"重新设计角色「{row.name}」"
    if user_instruction:
        instruction += f"。{user_instruction}"
    result = await plan_characters(project, source_text, instruction=instruction)
    new_char = result[0]
    row.role_type = new_char["role_type"]
    row.age = new_char["age"]
    row.personality = new_char["personality"]
    row.outfit = new_char.get("outfit")
    row.style_note = new_char.get("style_note")
    row.motivation = new_char["motivation"]
    row.arc = new_char["arc"]
    row.relations = new_char["relations"]
    row.description = _build_profile_description(
        new_char["personality"], new_char["appearance"],
        new_char["motivation"], new_char["arc"],
        outfit=new_char.get("outfit"), style_note=new_char.get("style_note"),
    ) or row.description
    # 圣经 visual_prompt 同步重建（重生成覆盖人设，视觉三段随之刷新）
    visual_bible = build_character_bible_prompt(
        row.name, row.age,
        "；".join(v for v in (
            new_char.get("appearance"), new_char.get("outfit"),
            new_char.get("style_note")) if v))
    if visual_bible:
        row.visual_prompt = (
            f"统一风格插画，角色圣经（固定外貌与服装，跨图一致）："
            f"{visual_bible}。"
            "全身立绘，纯色背景，高细节；五官/发型/服装/画风严格按上述设定，禁止变更。")
    await db.commit()
    return CharacterRegenerateOut(character=_character_to_out(row),
                                  phase=project.script_phase)


# ---------------------------------------------------------------------------
# 5. POST /characters/confirm：确认入池并推进阶段
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/script/characters/confirm",
             response_model=CharacterConfirmOut)
async def confirm_characters(
    project_id: int,
    req: CharacterConfirmRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CharacterConfirmOut:
    """角色设定确认入池：确认清单（可编辑后的人设，缺省字段沿用候选值）
    幂等写入 characters 表（同名更新人设字段，否则新建；visual_prompt 缺省
    用 DEFAULT_VISUAL_TEMPLATE 模板；description 为人设四要素摘要；
    relations 存 JSONB；confirmed_at=now；source="script_planner"）。

    同时：把确认结果回写 project.config 候选（confirmed=True 标记）→
    写 characters_confirmed 里程碑快照 → 阶段 characters 推进到 outline
    （NULL/idea 特例直接置 outline；outline 及之后幂等保持不再推进）→ 提交。

    Args:
        project_id: 项目 ID。
        req: 确认清单（characters 数组，字段为 None 时沿用候选值）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        CharacterConfirmOut: {"confirmed_count": 入池角色数, "phase": 确认后阶段}。

    Raises:
        HTTPException: 400 —— 项目尚无角色候选（请先生成角色）；404 ——
            项目不存在/非本人项目；502 不适用（本接口不调 LLM）。
    """
    project = await get_owned_project(db, project_id, user.id)
    candidates = _load_candidates(project)
    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="尚无角色候选，请先生成角色")

    # 确认入池（同名幂等 + 回写 config 候选 confirmed=True；落库逻辑与子图
    # 节点共用，service 只写不提交；条目 None 字段沿用候选值）
    confirmed_names = await confirm_character_entries(
        db, project, [item.model_dump() for item in req.characters])

    # 里程碑快照（V-01 留痕；add+flush 不 commit，与本接口事务同批提交）
    await write_stage_snapshot(
        db, project, "characters_confirmed",
        summary={"confirmed_count": len(confirmed_names), "names": confirmed_names},
    )
    # 阶段推进：characters → outline（一格校验）；NULL/idea 特例直接置；
    # outline 及之后重复确认幂等保持，不再推进
    if project.script_phase == "characters":
        await advance_phase(db, project, "outline")
    elif project.script_phase in (None, "idea"):
        project.script_phase = "outline"
    await db.commit()
    return CharacterConfirmOut(confirmed_count=len(confirmed_names),
                               phase=project.script_phase)


# ---------------------------------------------------------------------------
# 故事设定域内部工具（P2，redesign-script-workspace Task 2）
# ---------------------------------------------------------------------------
def _setting_to_out(setting: Optional[dict]) -> Optional[StorySettingOut]:
    """把 projects.story_setting JSONB 转为输出模型（内部函数，结构容错）。

    Args:
        setting: story_setting JSONB 值（None/非 dict 表示尚未生成设定）。

    Returns:
        Optional[StorySettingOut]: 设定内容块；未生成（None/非 dict）时返回
            None。source 非法取值归 None（防御脏数据）。

    Raises:
        无：结构异常按缺字段兜底。
    """
    if not isinstance(setting, dict):
        return None
    source = str(setting.get("source") or "")
    return StorySettingOut(
        logline=setting.get("logline"),
        core_conflict=setting.get("core_conflict"),
        worldview=setting.get("worldview"),
        source=source if source in ("ai", "user") else None,
        updated_at=setting.get("updated_at"),
    )


def _build_setting_payload(result: dict, source: str) -> dict:
    """把 plan_story_setting 的产出组装为 story_setting 落库结构（内部函数）。

    Args:
        result: plan_story_setting 的规整产出（含 logline/core_conflict/
            worldview；characters 由调用方另行写入 config 候选）。
        source: 来源标记（"ai" 生成/重生成；"user" 编辑）。

    Returns:
        dict: {logline, core_conflict, worldview, source, updated_at}
            （updated_at 为当前 UTC 时间的 ISO 8601 字符串）。

    Raises:
        无：纯字典构造。
    """
    return {
        "logline": result["logline"],
        "core_conflict": result["core_conflict"],
        "worldview": result["worldview"],
        "source": source,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# 5.1 POST /setting/generate：LLM 生成故事设定 + 人物候选
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/script/setting/generate",
             response_model=StorySettingGenerateOut)
async def generate_story_setting(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StorySettingGenerateOut:
    """LLM 生成故事设定：设定三段落 projects.story_setting（source="ai"），
    人物候选写入 config["script_characters"]（与角色域 generate 完全一致）。

    流程：归属校验 → phase=body 拒绝（正文阶段设定锁定，409）→ 校验剧本
    已有输入（无则 409）→ 调 plan_story_setting（内部失败重试 2 次，仍失败
    502）→ 设定三段落 story_setting → 人物候选写 config → 阶段为 NULL/idea
    时置 characters（流水线起点特例，沿用角色域 generate 口径；characters
    及以后不回退）→ 写 setting_generated 版本快照 → 提交。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StorySettingGenerateOut: {"setting": 设定内容块, "characters": 人物
            候选列表, "phase": 生成后阶段}。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目；409 —— phase=body
            设定已锁定 / 剧本尚未保存输入；502 —— LLM 生成与解析多次失败。
    """
    project = await get_owned_project(db, project_id, user.id)
    # 阶段守卫：正文阶段设定锁定（409，中文提示）
    if project.script_phase == "body":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="正文已开始生成，故事设定已锁定")
    script = (
        await db.execute(select(Script).where(Script.project_id == project.id))
    ).scalar_one_or_none()
    _require_script_input(script)
    # 源文本：raw_text 优先，无则取结构化剧本纯文本（前面已确认有内容）
    source_text = (script.raw_text or "").strip() or build_extract_source_text(script)

    result = await plan_story_setting(project, source_text)
    # 设定三段落 story_setting（整体重新赋值触发 JSONB 变更检测；source=ai）
    project.story_setting = _build_setting_payload(result, "ai")
    # 人物候选写入 config（与角色域 generate 一致：全量刷新，带临时 id 标记）
    save_character_candidates(db, project, result["characters"])
    # 阶段特例：NULL（快速模式）/idea 允许直接进入 characters（流水线起点）；
    # characters 及以后不回退
    if project.script_phase in (None, "idea"):
        project.script_phase = "characters"
    # 版本快照（V-01 留痕，setting_generated）
    await write_stage_snapshot(
        db, project, "setting_generated",
        summary={"logline": result["logline"],
                 "character_count": len(result["characters"])},
    )
    await db.commit()
    return StorySettingGenerateOut(
        setting=_setting_to_out(project.story_setting),
        characters=_candidates_out(project),
        phase=project.script_phase,
    )


# ---------------------------------------------------------------------------
# 5.2 GET /setting：设定内容（可空）+ 人物候选/已确认双组
# ---------------------------------------------------------------------------
@router.get("/api/projects/{project_id}/script/setting",
            response_model=StorySettingDetailOut)
async def get_story_setting(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StorySettingDetailOut:
    """读取故事设定详情：setting 为 story_setting JSONB 结构化输出（未生成
    时 null，不 404）；人物候选/已确认双组复用角色域 list 口径。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StorySettingDetailOut: {"setting": 设定内容块|null, "planned": 候选,
            "confirmed": 已入池, "phase": 当前阶段}。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目。
    """
    project = await get_owned_project(db, project_id, user.id)
    confirmed_rows = (
        await db.execute(
            select(Character)
            .where(
                Character.project_id == project.id,
                Character.deleted_at.is_(None),
                Character.confirmed_at.is_not(None),
            )
            .order_by(Character.id)
        )
    ).scalars().all()
    return StorySettingDetailOut(
        setting=_setting_to_out(project.story_setting),
        planned=_candidates_out(project),
        confirmed=[_character_to_out(row) for row in confirmed_rows],
        phase=project.script_phase,
    )


# ---------------------------------------------------------------------------
# 5.3 PUT /setting：合并编辑设定三段（source=user）
# ---------------------------------------------------------------------------
@router.put("/api/projects/{project_id}/script/setting",
            response_model=StorySettingEditOut)
async def edit_story_setting(
    project_id: int,
    req: StorySettingEditRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StorySettingEditOut:
    """合并编辑故事设定：仅覆盖传入字段（None=不修改），source 强制置
    "user" 并刷新 updated_at；写 setting_edited 版本快照；不改变阶段。

    从未生成过设定的项目亦允许直接编辑保存（用户手动撰写设定）。

    Args:
        project_id: 项目 ID。
        req: 编辑请求（logline/core_conflict/worldview，至少一个非 None，
            Pydantic 校验）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StorySettingEditOut: {"setting": 保存后的设定内容块, "phase": 当前阶段}。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目；422 —— 请求体校验失败
            （空 body / 空串 / 三字段全 None / 超长）。
    """
    project = await get_owned_project(db, project_id, user.id)
    current = project.story_setting if isinstance(project.story_setting, dict) else {}
    # 合并保存：仅覆盖传入字段，整体重新赋值触发 JSONB 变更检测
    project.story_setting = {
        "logline": req.logline if req.logline is not None else (current.get("logline") or None),
        "core_conflict": (req.core_conflict if req.core_conflict is not None
                          else (current.get("core_conflict") or None)),
        "worldview": req.worldview if req.worldview is not None else (current.get("worldview") or None),
        "source": "user",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    # 版本快照（V-01 留痕，setting_edited）；不改变阶段
    await write_stage_snapshot(
        db, project, "setting_edited",
        summary={"logline": project.story_setting["logline"], "source": "user"},
    )
    await db.commit()
    return StorySettingEditOut(
        setting=_setting_to_out(project.story_setting),
        phase=project.script_phase,
    )


# ---------------------------------------------------------------------------
# 5.4 POST /setting/regenerate：重出设定段（已确认角色不回退）
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/script/setting/regenerate",
             response_model=StorySettingGenerateOut)
async def regenerate_story_setting(
    project_id: int,
    req: Optional[StorySettingRegenerateRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StorySettingGenerateOut:
    """重出故事设定段（source="ai"）：设定三段按当前创意 + 可选 instruction
    重新生成；config 中 confirmed=True 的已确认候选不动、未入池候选随重生成
    刷新（与已确认同名的新候选丢弃，避免重复）；已入池 characters 表不回退。

    流程：归属校验 → phase=body 拒绝（409）→ 组装指令（当前一句话故事摘要 +
    用户调整方向）→ plan_story_setting → 设定三段落 story_setting →
    候选合并（confirmed 保留 + 新候选）→ 写 setting_generated 快照 → 提交。
    阶段不回退。

    Args:
        project_id: 项目 ID。
        req: 可选请求体（instruction 为本轮重生成的调整方向）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StorySettingGenerateOut: {"setting": 重生成后的设定内容块,
            "characters": 合并后的候选列表, "phase": 当前阶段}。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目；409 —— phase=body
            设定已锁定；502 —— LLM 生成与解析多次失败。
    """
    project = await get_owned_project(db, project_id, user.id)
    if project.script_phase == "body":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="正文已开始生成，故事设定已锁定")
    source_text = await _get_source_text(db, project) or ""
    # 组装重生成指令：告知 LLM 当前一句话故事与用户调整方向
    current = project.story_setting if isinstance(project.story_setting, dict) else {}
    instruction_parts: List[str] = []
    if current.get("logline"):
        instruction_parts.append(f"当前一句话故事：{current['logline']}")
    if req and req.instruction:
        instruction_parts.append(f"调整要求：{req.instruction}")
    instruction = "；".join(instruction_parts) or None

    result = await plan_story_setting(project, source_text, instruction=instruction)
    # 设定三段重出（source=ai）
    project.story_setting = _build_setting_payload(result, "ai")
    # 候选合并：confirmed=True 的候选原样保留；未入池候选随重生成刷新；
    # 与已确认同名的新候选丢弃（已确认角色不回退语义的自然延伸）
    candidates = _load_candidates(project)
    confirmed_keep = [c for c in candidates if c.get("confirmed")]
    confirmed_names = {str(c.get("name") or "") for c in confirmed_keep}
    fresh = [
        {"id": uuid.uuid4().hex[:8], "confirmed": False, **c}
        for c in result["characters"]
        if str(c.get("name") or "") not in confirmed_names
    ]
    _save_candidates(project, confirmed_keep + fresh)
    # 版本快照（V-01 留痕，setting_generated）；阶段不回退
    await write_stage_snapshot(
        db, project, "setting_generated",
        summary={"logline": result["logline"],
                 "confirmed_kept": len(confirmed_keep),
                 "fresh_candidates": len(fresh)},
    )
    await db.commit()
    return StorySettingGenerateOut(
        setting=_setting_to_out(project.story_setting),
        characters=_candidates_out(project),
        phase=project.script_phase,
    )


# ---------------------------------------------------------------------------
# 分集大纲域内部工具（P-3，Task 4）
# ---------------------------------------------------------------------------
# 单集 sequence 合法上限（与生成请求 target_episodes 的 1~100 区分：
# 编辑接口允许在更大范围内互换序号，上限 1000 防御异常大值）
_SEQUENCE_MAX = 1000


def _load_novel_chapters(project: Project) -> List[dict]:
    """从 project.config 读取暂存的小说章节列表（内部函数，结构容错）。

    Args:
        project: 已校验归属的项目 ORM 对象。

    Returns:
        List[dict]: 章节条目列表（含 index/title/summary/content_preview）；
            config 缺失或非列表时返回空列表。

    Raises:
        无：结构异常按空列表处理。
    """
    raw = (project.config or {}).get(_NOVEL_CHAPTERS_KEY)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _map_chapters_to_episodes(chapters: List[dict], target: int) -> List[str]:
    """把章节映射为分集摘要列表（内部函数，长度恰为 target）。

    映射算法（简单策略，注释说明）：
    - target == 章节数：每章一集，一一对应；
    - target < 章节数：按比例合并相邻章节（divmod 均分，前 rem 组各多 1 章），
      组内各章摘要以「；」连接成一段；
    - target > 章节数：按章节内分段（divmod 均分，前 rem 章各多拆 1 段），
      每段摘要标注「（k/parts）」。

    每段文本形如「第N章 标题：摘要」，最终供 episode_planner 提示词注入。

    Args:
        chapters: 暂存章节列表（非空）。
        target: 目标集数（1~_EPISODE_TARGET_MAX）。

    Returns:
        List[str]: 摘要文本列表，长度恰为 target。

    Raises:
        无：结构异常按缺字段兜底（空 chapters 由调用方前置校验拦截）。
    """
    target = max(1, target)
    count = len(chapters)

    def _label(chapter: dict) -> str:
        """把单章格式化为「第N章 标题：摘要」文本（内部函数）。

        Args:
            chapter: 章节条目 dict。

        Returns:
            str: 格式化后的单章文本。

        Raises:
            无。
        """
        return (f"第{chapter.get('index')}章 {chapter.get('title')}："
                f"{chapter.get('summary') or ''}")

    if target == count:
        return [_label(chapter) for chapter in chapters]
    if target < count:
        # 合并相邻章节：均分为 target 组（前 rem 组各多 1 章）
        base, rem = divmod(count, target)
        result: List[str] = []
        cursor = 0
        for group_idx in range(target):
            size = base + (1 if group_idx < rem else 0)
            group = chapters[cursor:cursor + size]
            cursor += size
            result.append("；".join(_label(chapter) for chapter in group))
        return result
    # target > 章节数：章节内分段（前 rem 章各多拆 1 段）
    base, rem = divmod(target, count)
    result = []
    for chapter_idx, chapter in enumerate(chapters):
        parts = base + (1 if chapter_idx < rem else 0)
        for seg in range(1, parts + 1):
            result.append(f"{_label(chapter)}（{seg}/{parts}）")
    return result[:target]


def _default_target_episodes(project: Project, from_chapters: bool) -> int:
    """target_episodes 缺省启发式（内部函数）：估算建议集数。

    规则（简单启发式，注释说明）：项目目标时长 ≥600 秒（多集体量）或
    小说切分模式（from_chapters=True，Task 9 小说改编通常多章多集）时
    建议 3 集；否则（短剧/快速模式）建议 1 集。

    Args:
        project: 已校验归属的项目 ORM 对象。
        from_chapters: 请求是否按小说章节切分（Task 9 预留）。

    Returns:
        int: 建议目标集数（1 或 3）。

    Raises:
        无：纯数值比较。
    """
    if (project.target_duration or 0) >= 600 or from_chapters:
        return 3
    return 1


def _episode_to_out(episode: Episode) -> EpisodePlanOutItem:
    """把 Episode ORM 对象转为分集大纲输出模型（内部函数）。

    title/logline/confirmed_at 取列值；core_event/characters/emotion_curve/
    duration_sec 从 outline JSONB 解包（缺失按 None/空列表兜底）。

    Args:
        episode: episodes 表 ORM 对象。

    Returns:
        EpisodePlanOutItem: 单集大纲输出条目。

    Raises:
        无：outline 结构异常按缺字段兜底。
    """
    outline = episode.outline if isinstance(episode.outline, dict) else {}
    return EpisodePlanOutItem(
        episode_id=episode.id,
        sequence=episode.sequence,
        title=episode.title,
        logline=episode.logline,
        core_event=outline.get("core_event"),
        characters=[str(c) for c in (outline.get("characters") or [])],
        emotion_curve=outline.get("emotion_curve"),
        duration_sec=outline.get("duration_sec"),
        confirmed_at=episode.confirmed_at,
    )


async def _get_owned_episode(db: AsyncSession, project: Project, episode_id: int) -> Episode:
    """校验并返回归属于指定项目的剧集（内部函数，404 语义与既有剧集 CRUD 一致）。

    Args:
        db: 请求级异步数据库会话。
        project: 已校验归属的项目。
        episode_id: 剧集 ID。

    Returns:
        Episode: 剧集 ORM 对象。

    Raises:
        HTTPException: 404 —— 剧集不存在或不属于该项目（统一 404，
            避免泄露他人项目剧集的存在性）。
    """
    episode = await db.get(Episode, episode_id)
    if episode is None or episode.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧集不存在")
    return episode


# ---------------------------------------------------------------------------
# 6. POST /script/episodes/plan：LLM 生成分集大纲（复用/新建/删多余 episodes 行）
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/script/episodes/plan",
             response_model=EpisodePlanOut)
async def generate_episode_plan(
    project_id: int,
    req: Optional[EpisodePlanGenerateRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EpisodePlanOut:
    """LLM 生成分集大纲并落 episodes 表（多退少补：既有行复用更新、不足
    新建、多余硬删）。

    流程：归属校验 → phase=body 拒绝重生成（大纲已锁定，409）→ 校验有
    已确认角色（characters 表 confirmed_at 非空行数 ≥1，否则 409）→
    target_episodes 与章节摘要确定 → plan_episodes（内部失败重试 2 次，
    仍失败 502）→ 落库（既有行按 sequence 复用更新 title/logline/outline
    并重写 sequence 为 1..n；多余行删除前解绑 scripts/storyboards/
    film_outputs 引用）→ 阶段为 NULL/idea/characters 时置 outline
    （NULL→outline 为快速模式跨两格起点特例，不经 advance_phase 一格校验）
    → 提交并返回全量分集。

    from_chapters 分支（P-7 小说改编链路）：校验 config["novel_chapters"]
    非空（否则 409「请先解析小说章节」）→ 目标集数缺省为章节数（每章一集，
    超过上限则合并）、显式传值优先 → 按 _map_chapters_to_episodes 把章节
    合并/拆分为恰好 target 段摘要 → 连同章节上下文（build_novel_context）
    经现有 chapter_summaries 通道注入 episode_planner。

    Args:
        project_id: 项目 ID。
        req: 可选请求体（target_episodes 目标集数 / from_chapters 小说切分）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        EpisodePlanOut: {"episodes": 全量分集（sequence 升序）, "phase": 当前阶段}。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目；409 —— phase=body
            已锁定 / 尚未确认角色设定 / from_chapters 但尚未解析小说章节；
            502 —— LLM 生成与解析多次失败。
    """
    project = await get_owned_project(db, project_id, user.id)
    # body 阶段大纲锁定：正文已开始，不允许整卷重生成（单集走 PUT 微调）
    if project.script_phase == "body":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="正文已开始生成，分集大纲已锁定；如需调整请编辑单集大纲")
    # 前置校验：至少 1 个已确认角色（characters 表 confirmed_at 非空）
    confirmed = (
        await db.execute(
            select(Character)
            .where(
                Character.project_id == project.id,
                Character.deleted_at.is_(None),
                Character.confirmed_at.is_not(None),
            )
            .order_by(Character.id)
        )
    ).scalars().all()
    if not confirmed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="请先确认角色设定")

    from_chapters = bool(req.from_chapters) if req else False
    # 章节切分模式（P-7）：校验已解析章节 → 按章节映射分集 → 注入章节摘要
    chapter_summaries: Optional[List[str]] = None
    if from_chapters:
        chapters = _load_novel_chapters(project)
        if not chapters:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="请先解析小说章节")
        if req and req.target_episodes:
            target = req.target_episodes  # 显式目标集数优先（跨章合并/拆分）
        else:
            target = min(len(chapters), _EPISODE_TARGET_MAX)  # 缺省每章一集
        chapter_summaries = _map_chapters_to_episodes(chapters, target)
    else:
        # 目标集数：请求显式传值优先；缺省走启发式（≥600s / 小说切分 → 3，否则 1）
        target = (
            req.target_episodes if req and req.target_episodes
            else _default_target_episodes(project, from_chapters)
        )
    # 源文本：章节模式用「章节摘要 + 正文节选」拼接长上下文（P-7 截断消除），
    # 其余模式沿用既有取源（raw_text 优先）
    if from_chapters:
        source_text = build_novel_context(project) or (
            await _get_source_text(db, project) or "")
    else:
        source_text = await _get_source_text(db, project) or ""
    items = await plan_episodes(
        project, source_text, list(confirmed), target,
        from_chapters=from_chapters, chapter_summaries=chapter_summaries)

    # ---------- 落库：既有行复用更新 + 不足新建 + 多余硬删（与子图节点共用，
    # service 内含阶段特例置 outline；只写不提交，由本接口统一提交） ----------
    await save_episode_plan(db, project, items)
    await db.commit()

    episodes = await _list_project_episodes(db, project.id)
    return EpisodePlanOut(
        episodes=[_episode_to_out(e) for e in episodes],
        phase=project.script_phase,
    )


# ---------------------------------------------------------------------------
# 7. GET /script/episodes/plan：分集大纲列表
# ---------------------------------------------------------------------------
@router.get("/api/projects/{project_id}/script/episodes/plan",
            response_model=EpisodePlanOut)
async def get_episode_plan(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EpisodePlanOut:
    """读取项目全部分集大纲（按 sequence 升序，含大纲字段与确认状态）。

    无任何剧集时返回空列表（不 404，方便前端首次渲染空态）。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        EpisodePlanOut: {"episodes": 全量分集, "phase": 当前阶段}。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目。
    """
    project = await get_owned_project(db, project_id, user.id)
    episodes = await _list_project_episodes(db, project.id)
    return EpisodePlanOut(
        episodes=[_episode_to_out(e) for e in episodes],
        phase=project.script_phase,
    )


# ---------------------------------------------------------------------------
# 8. PUT /script/episodes/{eid}/outline：单集大纲编辑
# ---------------------------------------------------------------------------
@router.put("/api/projects/{project_id}/script/episodes/{eid}/outline",
            response_model=EpisodePlanOutItem)
async def edit_episode_outline(
    project_id: int,
    eid: int,
    req: EpisodeOutlineEditRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EpisodePlanOutItem:
    """编辑单集大纲：更新 title/logline 列与 outline JSONB 内的大纲字段
    （core_event/characters/emotion_curve/duration_sec，服务端组装）；
    可选 sequence 互换重排（与目标序号的同项目剧集互换 sequence）。

    phase=body（大纲已确认）时保持已确认语义：仅允许微调文案字段
    （title/logline/outline 四字段），禁止 sequence 重排（409）。

    阶段自动推进（产品简化：取消「确认大纲」整体动作）：phase=outline 时
    保存任意一集大纲即把项目阶段推进到 body——保存即视为该集定稿，用户
    可直接进入步骤 4 逐集生成正文；响应携带最新 phase 供前端同步步骤高亮。

    Args:
        project_id: 项目 ID。
        eid: 剧集 ID。
        req: 编辑请求（字段平铺，None=不修改）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        EpisodePlanOutItem: 编辑后的单集详情（携带最新 phase）。

    Raises:
        HTTPException: 400 —— sequence 超出 1~1000；404 —— 项目不存在/
            非本人项目，或剧集不存在/不属于该项目；409 —— body 阶段禁止
            sequence 重排。
    """
    project = await get_owned_project(db, project_id, user.id)
    episode = await _get_owned_episode(db, project, eid)

    # sequence 重排：body 阶段禁止（大纲已确认，仅允许文案微调）
    if req.sequence is not None and project.script_phase == "body":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="正文已开始生成，禁止调整分集顺序；如需调整请回退大纲阶段")
    if req.sequence is not None and req.sequence != episode.sequence:
        if not (1 <= req.sequence <= _SEQUENCE_MAX):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"sequence 须在 1~{_SEQUENCE_MAX} 范围内")
        other = (
            await db.execute(
                select(Episode).where(
                    Episode.project_id == project.id,
                    Episode.sequence == req.sequence,
                )
            )
        ).scalar_one_or_none()
        if other is not None and other.id != episode.id:
            # 互换序号：两阶段写入避开 uk_episodes_seq 唯一索引
            original = episode.sequence
            other.sequence = -(other.id)
            episode.sequence = -(episode.id)
            await db.flush()
            other.sequence = original
            episode.sequence = req.sequence
        else:
            # 目标序号空缺（无同序号行）：直接占用
            episode.sequence = req.sequence

    # 列字段更新（title/logline 文案微调）
    if req.title is not None:
        episode.title = req.title.strip()
    if req.logline is not None:
        episode.logline = req.logline
    # outline JSONB 更新：仅覆盖传入字段，整体重新赋值触发变更检测
    outline = dict(episode.outline or {})
    if req.core_event is not None:
        outline["core_event"] = req.core_event
    if req.characters is not None:
        outline["characters"] = req.characters
    if req.emotion_curve is not None:
        outline["emotion_curve"] = req.emotion_curve
    if req.duration_sec is not None:
        outline["duration_sec"] = req.duration_sec
    if outline != (episode.outline or {}):
        episode.outline = outline
    # 阶段自动推进：outline 阶段保存单集大纲即视为定稿，推进到 body
    # （产品简化：取消「确认大纲」整体动作；body 阶段重复保存幂等不重推）
    if project.script_phase == "outline":
        project.script_phase = "body"
    await db.commit()
    await db.refresh(episode)
    out = _episode_to_out(episode)
    out.phase = project.script_phase
    return out


# ---------------------------------------------------------------------------
# 9. POST /script/episodes/plan/confirm：全量确认大纲并推进阶段
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/script/episodes/plan/confirm",
             response_model=EpisodePlanConfirmOut)
async def confirm_episode_plan(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EpisodePlanConfirmOut:
    """确认全部分集大纲：全部 episodes 置 confirmed_at（幂等——已确认的
    保持原值不覆盖）→ 写 outline_confirmed 里程碑快照 → 阶段 outline 推进
    到 body（经 advance_phase 一格校验；NULL/idea/characters 特例直接置
    body，快速模式 NULL→body 为跨两格起点特例）→ 提交。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        EpisodePlanConfirmOut: {"confirmed_count": 确认单集数, "phase": 确认后阶段}。

    Raises:
        HTTPException: 400 —— 项目尚无任何剧集（请先生成分集大纲）；
            404 —— 项目不存在/非本人项目。
    """
    project = await get_owned_project(db, project_id, user.id)
    episodes = await _list_project_episodes(db, project.id)
    if not episodes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="尚无分集大纲，请先生成")
    # 幂等确认：已确认的保持原 confirmed_at，未确认的置 now
    confirmed_at = datetime.now(timezone.utc)
    for episode in episodes:
        if episode.confirmed_at is None:
            episode.confirmed_at = confirmed_at
    # 里程碑快照（V-01 留痕；add+flush 不 commit，与本接口事务同批提交）
    await write_stage_snapshot(
        db, project, "outline_confirmed",
        summary={
            "episode_count": len(episodes),
            "titles": [e.title for e in episodes],
        },
    )
    # 阶段推进：outline → body（一格校验）；NULL/idea/characters 特例直接置
    # body（快速模式 NULL→body 为跨两格起点特例）；body 及之后幂等保持
    if project.script_phase == "outline":
        await advance_phase(db, project, "body")
    elif project.script_phase in (None, "idea", "characters"):
        project.script_phase = "body"
    await db.commit()
    return EpisodePlanConfirmOut(confirmed_count=len(episodes),
                                 phase=project.script_phase)


# ---------------------------------------------------------------------------
# 9.1 POST /script/episodes/{eid}/outline/regenerate：单集大纲 AI 重生成（P3）
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/script/episodes/{eid}/outline/regenerate",
             response_model=EpisodeOutlineRegenerateOut)
async def regenerate_episode_outline(
    project_id: int,
    eid: int,
    req: Optional[EpisodeOutlineRegenerateRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EpisodeOutlineRegenerateOut:
    """单集大纲 AI 重生成（P3，redesign-script-workspace Task 4）：仅重写指定
    一集的大纲（标题/一句话梗概/核心事件/出场角色/情绪走向/时长），其余集
    不受影响、阶段不变。

    流程：归属校验（404）→ 阶段守卫（outline/body 可用，与 plan 锁定口径
    一致——plan 在 body 阶段整体锁定 409，单集保持可编辑/可重写；未达大纲
    阶段 409）→ 剧集归属（404，eid 不属于该项目统一 404 不泄露存在性）→
    该集大纲已生成（409）→ 已确认角色 ≥1（409）→ 取前后集钩子上下文 →
    plan_episode_outline_regen（复用 plan_episodes 提示词骨架与
    _normalize_episode_candidates 规整，内部重试 2 次，仍失败 502）→
    仅更新该集 title/logline/outline JSONB → 写 episode_outline_regenerated
    版本快照留痕（episodes 行无 version 列，版本历史以快照 +1 记账）→ 提交。

    正文提醒：该集已有正文（scripts 行存在）时响应携带 body_exists=true，
    仅提醒「大纲变更后正文将不一致，请按需重生成正文」，不自动删除正文。

    Args:
        project_id: 项目 ID。
        eid: 剧集 ID。
        req: 可选请求体（instruction 为本轮重生成的调整方向，可空）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        EpisodeOutlineRegenerateOut: 重生成后的单集大纲（含 body_exists 提醒字段）。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目，或剧集不存在/不属于
            该项目；409 —— 阶段未达 outline / 该集大纲未生成 / 无已确认角色；
            502 —— LLM 生成与解析多次失败。
    """
    project = await get_owned_project(db, project_id, user.id)
    # 阶段守卫：outline/body 可用（未达大纲阶段无法重写单集大纲）
    if project.script_phase not in ("outline", "body"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="请先进入分集大纲阶段（确认角色设定并生成分集大纲）")
    episode = await _get_owned_episode(db, project, eid)
    outline = _require_episode_outline(episode)
    confirmed_characters = await _list_confirmed_characters(db, project.id)
    if not confirmed_characters:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="请先确认角色设定")

    # 前后集钩子上下文（按 sequence 定位相邻集；第 1 集/最后一集对应侧为 None）
    episodes_all = await _list_project_episodes(db, project.id)
    index = next((i for i, e in enumerate(episodes_all) if e.id == episode.id), -1)
    prev_episode = episodes_all[index - 1] if index > 0 else None
    next_episode = (
        episodes_all[index + 1] if 0 <= index < len(episodes_all) - 1 else None)

    # 正文存在性：仅提醒不阻断（不自动删正文，由用户决定是否重生成）
    existing_script = await _get_episode_script(db, project.id, eid)
    body_exists = existing_script is not None

    # LLM 重生成（复用 plan_episodes 骨架与规整校验，作用域收窄为单集）
    item = await plan_episode_outline_regen(
        project, episode, prev_episode, next_episode, confirmed_characters,
        instruction=(req.instruction if req else None))

    # 落库：仅更新该集 title/logline/outline JSONB（LLM 空值字段沿用旧值兜底）
    episode.title = item["title"]
    if item["logline"]:
        episode.logline = item["logline"]
    episode.outline = {
        "core_event": item["core_event"] or (outline.get("core_event") or None),
        "characters": item["characters"],
        "emotion_curve": item["emotion_curve"] or (outline.get("emotion_curve") or None),
        "duration_sec": item["duration_sec"] or outline.get("duration_sec"),
    }
    # 版本快照留痕（V-01：版本历史 +1；add+flush 不 commit，与本接口事务同批提交）
    await write_stage_snapshot(
        db, project, "episode_outline_regenerated",
        summary={
            "episode_id": episode.id,
            "sequence": episode.sequence,
            "title": episode.title,
            "instruction": (req.instruction if req and req.instruction else None),
        },
    )
    await db.commit()
    await db.refresh(episode)
    return EpisodeOutlineRegenerateOut(
        **_episode_to_out(episode).model_dump(), body_exists=body_exists)


# ---------------------------------------------------------------------------
# 9.2 POST /script/episodes/{eid}/outline/autofill：单集大纲 AI 一键填充（P3）
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/script/episodes/{eid}/outline/autofill",
             response_model=EpisodeOutlineAutofillOut)
async def autofill_episode_outline(
    project_id: int,
    eid: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EpisodeOutlineAutofillOut:
    """单集大纲 AI 一键填充：依据故事设定/角色设定/本集已填内容补全缺失字段。

    与 regenerate 的差异：regenerate 整集覆盖重写并落库；本接口只针对
    用户未填写的字段给补全建议（已填字段原样返回），**不落库、不打版本
    快照**——响应为建议值，前端回填编辑表单，用户确认/调整（或经 AI 助手
    改写）后走既有 PUT outline 保存，留痕发生在保存侧。

    适用空集：outline 未生成（非 dict/空 dict）时所有字段均视为未填写，
    由 AI 全量填充（典型场景：body 阶段「新增集」后的空集补全）。

    流程：归属校验（404）→ 阶段守卫（outline/body 可用，未达 409）→
    剧集归属（404）→ 已确认角色 ≥1（409）→ 取前后集钩子上下文与故事设定 →
    plan_episode_outline_autofill（复用 regen 重试骨架与名单外角色过滤，
    3 次尝试失败 502）→ 对比填充前各字段空缺情况计算 filled_fields →
    返回建议值。

    Args:
        project_id: 项目 ID。
        eid: 剧集 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        EpisodeOutlineAutofillOut: 补全后的单集大纲建议值 + 本次填充字段清单。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目，或剧集不存在/不属于
            该项目；409 —— 阶段未达 outline / 无已确认角色；
            502 —— LLM 生成与解析多次失败。
    """
    project = await get_owned_project(db, project_id, user.id)
    # 阶段守卫：outline/body 可用（与 regenerate 同口径）
    if project.script_phase not in ("outline", "body"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="请先进入分集大纲阶段（确认角色设定并生成分集大纲）")
    episode = await _get_owned_episode(db, project, eid)
    # 宽松读取大纲（空集 outline 非 dict/空 dict → 全字段视为未填写，允许填充）
    outline = episode.outline if isinstance(episode.outline, dict) else {}
    confirmed_characters = await _list_confirmed_characters(db, project.id)
    if not confirmed_characters:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="请先确认角色设定")

    # 前后集钩子上下文（按 sequence 定位相邻集；第 1 集/最后一集对应侧为 None）
    episodes_all = await _list_project_episodes(db, project.id)
    index = next((i for i, e in enumerate(episodes_all) if e.id == episode.id), -1)
    prev_episode = episodes_all[index - 1] if index > 0 else None
    next_episode = (
        episodes_all[index + 1] if 0 <= index < len(episodes_all) - 1 else None)

    # LLM 填充（只补缺失字段；建议值不落库，保存由 PUT outline 承接）
    item = await plan_episode_outline_autofill(
        project, episode, confirmed_characters,
        story_setting=project.story_setting,
        prev_episode=prev_episode, next_episode=next_episode)

    # 计算本次被填充的字段（填充前为空、建议值非空），供前端 toast 提示；
    # 占位标题「第 X 集」视为未填写——AI 生成真标题时计入「标题」
    filled: List[str] = []
    if is_placeholder_episode_title(episode.title) and item.get("title"):
        filled.append("标题")
    if not episode.logline and item.get("logline"):
        filled.append("一句话梗概")
    if not outline.get("core_event") and item.get("core_event"):
        filled.append("核心事件")
    if not outline.get("characters") and item.get("characters"):
        filled.append("出场角色")
    if not outline.get("emotion_curve") and item.get("emotion_curve"):
        filled.append("情绪走向")
    if not outline.get("duration_sec") and item.get("duration_sec"):
        filled.append("单集时长")
    return EpisodeOutlineAutofillOut(
        sequence=episode.sequence,
        title=item.get("title") or episode.title or f"第 {episode.sequence} 集",
        logline=item.get("logline") or episode.logline,
        core_event=item.get("core_event") or outline.get("core_event"),
        characters=item.get("characters") or list(outline.get("characters") or []),
        emotion_curve=item.get("emotion_curve") or outline.get("emotion_curve"),
        duration_sec=item.get("duration_sec") or outline.get("duration_sec"),
        filled_fields=filled,
    )


# ---------------------------------------------------------------------------
# 正文域内部工具（P-4，Task 5）
# ---------------------------------------------------------------------------
def _require_body_phase(project: Project) -> None:
    """正文域阶段守卫（内部函数）：script_phase 必须为 body。

    正文三接口（生成/保存/重生成）为精细流水线专属：phase 非 body
    （含 NULL 快速模式）一律 409——不用 require_phase（其对 NULL 放行）。

    Args:
        project: 已校验归属的项目 ORM 对象。

    Returns:
        None。

    Raises:
        HTTPException: 409 —— 当前阶段不是 body（提示先确认分集大纲）。
    """
    if project.script_phase != "body":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="请先确认分集大纲")


def _require_episode_outline(episode: Episode) -> dict:
    """校验该集大纲已生成并返回大纲 JSONB（内部函数）。

    Args:
        episode: 剧集 ORM 对象。

    Returns:
        dict: 该集大纲 JSONB（非 dict 视为未生成抛 409）。

    Raises:
        HTTPException: 409 —— 该集尚未生成分集大纲。
    """
    outline = episode.outline if isinstance(episode.outline, dict) else None
    if not outline:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="请先生成分集大纲")
    return outline


async def _list_confirmed_characters(db: AsyncSession, project_id: int) -> List[Character]:
    """查询项目已确认角色列表（confirmed_at 非空、未删除，按 id 升序，内部函数）。

    Args:
        db: 请求级异步数据库会话。
        project_id: 项目 ID。

    Returns:
        List[Character]: 已确认角色 ORM 列表（可能为空，由调用方决定 409）。

    Raises:
        无：纯查询。
    """
    rows = await db.execute(
        select(Character)
        .where(
            Character.project_id == project_id,
            Character.deleted_at.is_(None),
            Character.confirmed_at.is_not(None),
        )
        .order_by(Character.id)
    )
    return list(rows.scalars().all())


async def _get_episode_script(
    db: AsyncSession, project_id: int, episode_id: int
) -> Optional[Script]:
    """按 (project_id, episode_id) 定位单集正文剧本行（内部函数）。

    Args:
        db: 请求级异步数据库会话。
        project_id: 项目 ID。
        episode_id: 剧集 ID。

    Returns:
        Optional[Script]: 剧本行；该集尚未生成正文时为 None。

    Raises:
        无：纯查询。
    """
    return (
        await db.execute(
            select(Script).where(
                Script.project_id == project_id,
                Script.episode_id == episode_id,
            )
        )
    ).scalar_one_or_none()


async def _episode_body_event_generator(
    db: AsyncSession,
    project: Project,
    episode: Episode,
    confirmed_characters: List[Character],
    script_row: Optional[Script],
) -> AsyncGenerator[str, None]:
    """单集正文生成的 SSE 事件生成器（生成/重生成两接口共用，内部函数）。

    事件序列：
    - `delta`：{"text": 正文增量}（两段式第一段，分段流式持续透传）；
    - `status`：{"message": "正在生成结构化场次"}（进入第二段前发出）；
    - `error`：LLM 异常 / 结构化 3 轮耗尽 / 乐观锁冲突（不落库、不发 done）；
    - `done`：{"episode_id", "script_id", "version", "raw_text", "content",
      "usage"}（content 为 RD-03 字段级包装，source=ai）。

    落库：scripts 行按 (project_id, episode_id) upsert——首次新建 version=1；
    已存在行走乐观锁 UPDATE WHERE version=旧值（冲突发 error 不落库）；
    episode.status 置 scripted。

    Args:
        db: 请求级异步数据库会话（SSE 流期间保持有效）。
        project: 已校验归属的项目（生成锁持有方，finally 中释放）。
        episode: 目标剧集（title/logline/outline 提供正文上下文）。
        confirmed_characters: 已确认角色列表（character_id 回填依据）。
        script_row: 既有剧本行（None 表示首次生成）。

    Yields:
        str: sse_event 格式化的事件帧。

    Raises:
        无：内部异常均转 error 事件，不向上抛出。
    """
    start_version = script_row.version if script_row is not None else 0
    script_id = script_row.id if script_row is not None else None
    try:
        # ---------- 两段式第一段：文章体正文（分段流式，delta 持续透传） ----------
        raw_parts: List[str] = []
        try:
            async for kind, chunk in stream_episode_body(project, episode, confirmed_characters):
                if kind == "delta":
                    raw_parts.append(chunk)
                    yield sse_event("delta", {"text": chunk})
        except Exception:  # noqa: BLE001 —— LLM 完全异常转 error 事件
            yield sse_event("error", {"message": "模型不可用，正文生成失败，请重试"})
            return
        raw_text = "".join(raw_parts)
        if not raw_text.strip():
            yield sse_event("error", {"message": "正文生成失败，请重试"})
            return

        # ---------- 两段式第二段：结构化场次（非流式 json_mode） ----------
        yield sse_event("status", {"message": "正在生成结构化场次"})
        try:
            structured = await plan_episode_scenes(
                project, episode, confirmed_characters, raw_text)
        except HTTPException as exc:
            # 3 轮重试耗尽的 502：转 error 事件（不落库、不发 done）
            yield sse_event("error", {"message": str(exc.detail)})
            return

        content_payload = {
            "title": episode.title,
            "logline": episode.logline or "",
            "locations": structured.get("locations") or [],
            "scenes": structured.get("scenes") or [],
        }
        # 结构门槛（ScriptBodyContent：scenes ≥1、字段白名单）
        validated, err = validate_script_body_content(content_payload)
        if validated is None:
            yield sse_event("error", {"message": err or "正文结构校验失败"})
            return
        # RD-03 字段级来源标记：正文各一级字段包装为 ai 来源
        new_content = wrap_content_fields(validated, "ai")

        # ---------- 落库：首次新建 version=1；已有行乐观锁 version+1 ----------
        if script_row is None:
            new_script = Script(
                project_id=project.id,
                episode_id=episode.id,
                content=new_content,
                raw_text=raw_text,
                version=1,
            )
            db.add(new_script)
            await db.flush()  # 取自增 id 供 done 事件
            script_id = new_script.id
            new_version = 1
        else:
            new_version = start_version + 1
            lock_result = await db.execute(
                update(Script)
                .where(Script.id == script_id, Script.version == start_version)
                .values(content=new_content, raw_text=raw_text, version=new_version)
            )
            if lock_result.rowcount == 0:
                # 版本已被其他操作推进：放弃本次写入
                await db.rollback()
                yield sse_event("error", {"message": "剧本已被其他操作修改，请刷新后重试"})
                return
        episode.status = "scripted"
        await db.commit()
        # 用量归因：读取最近一次 LLM 调用的用量记录（mock/离线路径为 null）
        usage = get_last_usage()
        yield sse_event("done", {
            "episode_id": episode.id,
            "script_id": script_id,
            "version": new_version,
            "raw_text": raw_text,
            "content": new_content,
            "usage": usage,
        })
    finally:
        # 释放进程内生成锁（生成器耗尽或客户端断连触发 aclose 时均会执行）
        release_generate_lock(project.id)


# ---------------------------------------------------------------------------
# 10. POST /script/episodes/{eid}/body/generate/stream：SSE 流式生成单集正文
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/script/episodes/{eid}/body/generate/stream")
async def generate_episode_body_stream(
    project_id: int,
    eid: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """SSE 流式生成单集正文（POST 方法，text/event-stream，P-4 精细流水线专属）。

    前置校验（按序）：项目归属 → 剧集归属（404）→ phase 必须 body
    （409 请先确认分集大纲）→ 该集大纲已生成（409 请先生成分集大纲）→
    已确认角色 ≥1（409 请先确认角色设定）→ 进程内生成锁（409 正在生成中）。

    事件序列与落库语义见 _episode_body_event_generator。

    Args:
        project_id: 项目 ID。
        eid: 剧集 ID。
        user: 当前用户。
        db: 请求级异步数据库会话（SSE 流期间保持有效）。

    Returns:
        StreamingResponse: SSE 流式响应。

    Raises:
        HTTPException: 404 —— 项目/剧集不存在或不归属；409 —— 阶段未达
            body / 大纲未生成 / 无已确认角色 / 同项目已有生成流进行中。
    """
    project = await get_owned_project(db, project_id, user.id)
    episode = await _get_owned_episode(db, project, eid)
    _require_body_phase(project)
    _require_episode_outline(episode)
    confirmed_characters = await _list_confirmed_characters(db, project.id)
    if not confirmed_characters:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="请先确认角色设定")
    script_row = await _get_episode_script(db, project.id, eid)
    # 并发幂等保护（单进程语义）：流开始前占锁，生成器 finally 中释放
    if not acquire_generate_lock(project.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="正在生成中，请稍候")
    return await sse_stream(
        _episode_body_event_generator(db, project, episode, confirmed_characters, script_row))


# ---------------------------------------------------------------------------
# 11. PUT /script/episodes/{eid}/body：保存用户修改的单集正文
# ---------------------------------------------------------------------------
@router.put("/api/projects/{project_id}/script/episodes/{eid}/body",
            response_model=EpisodeBodyOut)
async def save_episode_body(
    project_id: int,
    eid: int,
    req: EpisodeBodySaveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EpisodeBodyOut:
    """保存用户修改的单集正文（场次级 source 合并，RD-03 场次粒度扩展）。

    流程：归属校验 → phase 必须 body（409）→ 定位剧本行（无则新建
    version=1）→ 可选乐观锁（base_version 与当前 version 不一致 409，
    口径同 PUT /script）→ merge_scene_sources 场次级 source 合并（内容
    未变的场次保留旧 source、改动/新增标 user、首次保存全 ai）→ content
    整体包装 {"value": merged, "source": "user"} 落库、version+1 →
    返回 EpisodeBodyOut。

    Args:
        project_id: 项目 ID。
        eid: 剧集 ID。
        req: 保存请求（raw_text/content 必填，base_version 可选）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        EpisodeBodyOut: 保存后的单集正文（episode_id/script_id/raw_text/
            content/version/updated_at）。

    Raises:
        HTTPException: 404 —— 项目/剧集不存在或不归属；409 —— 阶段未达
            body 或 base_version 不匹配。
    """
    project = await get_owned_project(db, project_id, user.id)
    episode = await _get_owned_episode(db, project, eid)
    _require_body_phase(project)
    script = await _get_episode_script(db, project.id, eid)
    # 可选乐观锁：仅当调用方显式传入 base_version 时校验（兼容未传的旧客户端）
    if (script is not None and req.base_version is not None
            and req.base_version != script.version):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="剧本已被其他操作修改，请刷新")
    if script is None:
        # 首次保存：新建剧本行（merge_scene_sources 对无旧数据场景全标 ai），
        # version 从 1 起不再自增
        script = Script(project_id=project.id, episode_id=episode.id,
                        content={}, raw_text=None, version=1)
        db.add(script)
        await db.flush()  # 取自增 id
    else:
        script.version += 1
    # 场次级 source 合并后再整体包装为 user 来源落库（RD-03）
    merged = merge_scene_sources(script.content, req.content)
    script.content = {"value": merged, "source": "user"}
    script.raw_text = req.raw_text
    await db.commit()
    await db.refresh(script)
    return EpisodeBodyOut(
        episode_id=episode.id,
        script_id=script.id,
        raw_text=script.raw_text,
        content=script.content,
        version=script.version,
        updated_at=script.updated_at,
    )


# ---------------------------------------------------------------------------
# 12. POST /script/episodes/{eid}/body/regenerate：单集正文重生成
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/script/episodes/{eid}/body/regenerate")
async def regenerate_episode_body_stream(
    project_id: int,
    eid: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """单集正文重生成（SSE，事件序列与生成接口同款，覆盖语义）。

    与生成接口的语义差异：要求该集已有正文（scripts 行存在且 raw_text
    非空，否则 400「该集尚未生成正文」）；重生成 = 覆盖 raw_text/content、
    version+1（乐观锁）。内部复用同一 event_generator。

    Args:
        project_id: 项目 ID。
        eid: 剧集 ID。
        user: 当前用户。
        db: 请求级异步数据库会话（SSE 流期间保持有效）。

    Returns:
        StreamingResponse: SSE 流式响应。

    Raises:
        HTTPException: 400 —— 该集尚未生成正文；404 —— 项目/剧集不存在或
            不归属；409 —— 阶段未达 body / 大纲未生成 / 无已确认角色 /
            同项目已有生成流进行中。
    """
    project = await get_owned_project(db, project_id, user.id)
    episode = await _get_owned_episode(db, project, eid)
    _require_body_phase(project)
    _require_episode_outline(episode)
    confirmed_characters = await _list_confirmed_characters(db, project.id)
    if not confirmed_characters:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="请先确认角色设定")
    script_row = await _get_episode_script(db, project.id, eid)
    # 重生成前置：该集必须已有正文（区分于首次生成接口）
    if script_row is None or not (script_row.raw_text or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="该集尚未生成正文")
    if not acquire_generate_lock(project.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="正在生成中，请稍候")
    return await sse_stream(
        _episode_body_event_generator(db, project, episode, confirmed_characters, script_row))


# ---------------------------------------------------------------------------
# 13. POST /script/episodes/{eid}/optimize：AI 优化预览（不落库，Task 7）
# ---------------------------------------------------------------------------
def _require_episode_body(script: Optional[Script]) -> None:
    """校验该集已有正文（scripts 行存在且 raw_text 非空，内部函数）。

    Args:
        script: 该集剧本行（None 表示尚未生成）。

    Returns:
        None。

    Raises:
        HTTPException: 400 —— 该集尚未生成正文。
    """
    if script is None or not (script.raw_text or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="该集尚未生成正文")


def _resolve_optimize_target(plain: dict, grain: str, target) -> Optional[object]:
    """校验并规范化 optimize 的 target 参数（内部函数）。

    - grain=segment：target 必需，转 int 场次序号并要求该场次存在；
    - grain=field：target 必需为合法可优化路径（白名单）且能解析到当前值；
    - grain=global/episode：忽略 target，返回 None。

    Args:
        plain: 解包后的本集纯结构。
        grain: 优化粒度 global/episode/segment/field。
        target: 原始 target（int / str / None）。

    Returns:
        Optional[object]: 规范化后的 target（segment=int 场次序号；
            field=str 路径；global/episode=None）。

    Raises:
        HTTPException: 400 —— segment/field 缺 target 或 target 非法/不存在。
    """
    if grain == "segment":
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="segment 粒度需提供场次序号 target")
        try:
            scene_no = int(target)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="segment 粒度的 target 必须为场次序号")
        scenes = plain.get("scenes") if isinstance(plain.get("scenes"), list) else []
        if not any(isinstance(s, dict) and s.get("scene_no") == scene_no for s in scenes):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="指定场次不存在")
        return scene_no
    if grain == "field":
        if not isinstance(target, str) or not is_optimizable_path(target):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="field 粒度需提供合法的字段路径 target")
        if get_value_by_path(plain, target, PATH_MISSING) is PATH_MISSING:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="目标字段不存在")
        return target
    return None


@router.post("/api/projects/{project_id}/script/episodes/{eid}/optimize",
             response_model=EpisodeOptimizeOut)
async def optimize_episode(
    project_id: int,
    eid: int,
    req: EpisodeOptimizeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EpisodeOptimizeOut:
    """AI 优化预览（四粒度，不落库，P-6 Task 7）。

    流程：归属校验 → phase 必须 body（409）→ 该集已有正文（scripts 行
    存在且 raw_text 非空，否则 400）→ grain=segment/field 校验 target
    （400）→ optimize_episode_content（内部重试 2 次，失败 502）→
    build_field_diffs 产出片段化 diff → **不落库**直接返回。

    Args:
        project_id: 项目 ID。
        eid: 剧集 ID。
        req: 优化请求（grain / target / instruction）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        EpisodeOptimizeOut: {"episode_id", "grain", "changes": [diff 片段],
            "count"}。

    Raises:
        HTTPException: 400 —— 该集无正文 / segment/field 的 target 缺失或非法；
            404 —— 项目/剧集不存在或不归属；409 —— 阶段未达 body；502 ——
            优化 LLM 多次失败。
    """
    project = await get_owned_project(db, project_id, user.id)
    episode = await _get_owned_episode(db, project, eid)
    _require_body_phase(project)
    script = await _get_episode_script(db, project.id, eid)
    _require_episode_body(script)
    plain = content_plain(script.content)
    target = _resolve_optimize_target(plain, req.grain, req.target)
    changes = await optimize_episode_content(
        project, episode, plain, req.grain, target, req.instruction)
    diffs = build_field_diffs(plain, changes)
    return EpisodeOptimizeOut(
        episode_id=eid, grain=req.grain, changes=diffs, count=len(diffs))


# ---------------------------------------------------------------------------
# 14. POST /script/episodes/{eid}/optimize/apply：应用选中优化片段（Task 7）
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/script/episodes/{eid}/optimize/apply",
             response_model=EpisodeOptimizeApplyOut)
async def apply_episode_optimize(
    project_id: int,
    eid: int,
    req: EpisodeOptimizeApplyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EpisodeOptimizeApplyOut:
    """应用选中的优化片段（字段级并发安全 + source 标记，P-6 Task 7）。

    流程：归属校验 → phase 必须 body（409）→ 该集已有正文（400）→ 可选
    乐观锁（base_version 与当前 version 不一致 409）→ 按 accept_ids 从
    req.changes 匹配片段 → apply_optimize_changes（path 白名单 + 越界 +
    original 与当前库值一致校验，不一致跳过计 skipped）→ 应用后
    version+1 并以乐观锁 UPDATE WHERE version 落库（冲突 409）→ 返回。

    Args:
        project_id: 项目 ID。
        eid: 剧集 ID。
        req: 应用请求（accept_ids / changes / base_version）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        EpisodeOptimizeApplyOut: {"applied", "skipped", "version", "content"}。

    Raises:
        HTTPException: 400 —— 该集无正文；404 —— 项目/剧集不存在或不归属；
            409 —— 阶段未达 body / base_version 不匹配 / 并发写入冲突。
    """
    project = await get_owned_project(db, project_id, user.id)
    await _get_owned_episode(db, project, eid)
    _require_body_phase(project)
    script = await _get_episode_script(db, project.id, eid)
    _require_episode_body(script)
    if req.base_version is not None and req.base_version != script.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="剧本已被其他操作修改，请刷新")
    accept_ids = set(req.accept_ids)
    accepted = [c.model_dump() for c in req.changes if c.id in accept_ids]
    new_content, applied, skipped = apply_optimize_changes(script.content, accepted)
    if applied == 0:
        # 无任何片段被应用：不递增版本、不落库
        return EpisodeOptimizeApplyOut(
            applied=0, skipped=skipped, version=script.version, content=script.content)
    new_version = script.version + 1
    lock_result = await db.execute(
        update(Script)
        .where(Script.id == script.id, Script.version == script.version)
        .values(content=new_content, version=new_version)
    )
    if lock_result.rowcount == 0:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="剧本已被其他操作修改，请刷新")
    await db.commit()
    await db.refresh(script)
    return EpisodeOptimizeApplyOut(
        applied=applied, skipped=skipped, version=script.version, content=script.content)


# ---------------------------------------------------------------------------
# 14.1 对话式改写通道（P4，redesign-script-workspace Task 5）
# ---------------------------------------------------------------------------
# 设定三段字段（assist setting 作用域的 path 白名单，取自 agents 的统一事实来源）
_ASSIST_SETTING_PATHS = ASSIST_PATH_WHITELIST["setting"]
# 单集大纲可改写字段（assist episode_outline 作用域的 path 白名单）
_ASSIST_OUTLINE_PATHS = ASSIST_PATH_WHITELIST["episode_outline"]

# outline 可改写字段的落库截长上限（与 _normalize_episode_candidates 的
# 列宽对齐：episodes.title String(100) / logline String(200) / outline
# JSONB 字段沿用生成期截长口径）
_ASSIST_OUTLINE_TEXT_LIMITS = {
    "title": 100, "logline": 200, "core_event": 2000, "emotion_curve": 100,
}


def _assist_setting_payload(setting: Optional[dict]) -> Optional[dict]:
    """构造 assist setting 作用域的上下文 payload（内部函数，空态归一）。

    Args:
        setting: projects.story_setting JSONB 值（可能为 None/非 dict）。

    Returns:
        Optional[dict]: {"logline", "core_conflict", "worldview"} 三段文本
            （个别缺失字段为 None）；setting 缺失或三段全空时返回 None
            （调用方据此抛 409）。

    Raises:
        无：结构异常按缺字段兜底。
    """
    if not isinstance(setting, dict):
        return None
    payload = {key: setting.get(key) for key in _ASSIST_SETTING_PATHS}
    if not any(str(v or "").strip() for v in payload.values()):
        return None  # 三段全空视作未生成
    return payload


def _assist_outline_payload(episode: Episode) -> dict:
    """构造 assist episode_outline 作用域的上下文 payload（内部函数）。

    作用对象 = 该集 title/logline 列 + outline JSONB 文本字段
    （core_event/emotion_curve 直取；characters 名单以顿号连接为展示文本，
    duration_sec 为整数字段不参与文本 diff 机制，不在白名单内）。

    Args:
        episode: 剧集 ORM 对象（title/logline 列 + outline JSONB）。

    Returns:
        dict: {"title", "logline", "core_event", "characters",
            "emotion_curve"} 扁平 payload（缺失字段归空串）。

    Raises:
        无：结构异常按缺字段兜底。
    """
    outline = episode.outline if isinstance(episode.outline, dict) else {}
    return {
        "title": episode.title or "",
        "logline": episode.logline or "",
        "core_event": str(outline.get("core_event") or ""),
        "characters": "、".join(str(c) for c in (outline.get("characters") or [])),
        "emotion_curve": str(outline.get("emotion_curve") or ""),
    }


def _assist_want_have(item: dict, current) -> tuple:
    """构造 apply 片段校验的（回传原文, 当前库值）文本二元组（内部函数）。

    Args:
        item: 采纳片段 dict（含 original/proposed）。
        current: 该 path 的当前库值（str/None/list）。

    Returns:
        tuple: (want, have) 两个字符串；列表值以顿号连接，None 归空串，
            供 original 一致性比较。

    Raises:
        无：纯文本归一。
    """
    want = "" if item.get("original") is None else str(item.get("original"))
    if isinstance(current, list):
        have = "、".join(str(v) for v in current)
    else:
        have = "" if current is None else str(current)
    return want, have


@router.post("/api/projects/{project_id}/script/assist/quote-rewrite",
             response_model=QuoteRewriteOut)
async def quote_rewrite_endpoint(
    project_id: int,
    req: QuoteRewriteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> QuoteRewriteOut:
    """选中文本优化（工作台「添加对话中」链路，纯文本建议不落库）。

    用户在工作台（故事设定/角色设定/分集大纲/剧本正文）选中文本点「添加对话
    中」，引用进入助手面板并补充优化方向后发送。本接口把引用原文 + 优化方向
    交给 rewrite_quote（agent_name=script_assist，纯文本通道，重试 2 次），
    返回优化后的文本供用户复制粘贴回对应位置——**不落库、不写版本快照、不改
    script_phase**，因此不设阶段守卫（锁定阶段的既有内容同样可请求优化建议）。

    Args:
        project_id: 项目 ID。
        req: 优化请求（scope/target_episode_id/quote_text/instruction）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        QuoteRewriteOut: {optimized_text} 优化后的文本。

    Raises:
        HTTPException: 404 —— 项目不存在/不归属；422 —— 参数校验失败；
            502 —— 优化 LLM 多次失败。
    """
    project = await get_owned_project(db, project_id, user.id)
    optimized = await rewrite_quote(
        project, req.scope, req.quote_text, req.instruction,
        target_episode_id=req.target_episode_id)
    return QuoteRewriteOut(optimized_text=optimized)


@router.post("/api/projects/{project_id}/script/assist",
             response_model=AssistPreviewOut)
async def assist_preview(
    project_id: int,
    req: AssistPreviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssistPreviewOut:
    """对话式改写预览（P4，redesign-script-workspace Task 5，不落库）。

    按作用域分发：
    - setting：作用 projects.story_setting 三段文本。守卫：phase=body 时
      409「故事设定已锁定」（与 setting 域口径一致）；setting 未生成（或
      三段全空）409「请先生成故事设定」。走 assist_rewrite（agent_name=
      script_assist，重试 2 次，失败 502）。
    - episode_outline：作用该集 title/logline 列与 outline JSONB 文本字段。
      守卫对齐 Task 4 单集重生成口径：outline/body 阶段可用（未达 409）、
      eid 归属 404、该集大纲未生成 409。
    - episode_body：直接复用现有 optimize 预览路径（内部转发
      optimize_episode_content，grain=global 由 LLM 自定范围），保证行为
      一致不重造。守卫与 optimize 一致：phase 必须 body（409）、该集已有
      正文（400）。

    diff 统一用 build_field_diffs 对「当前库值 vs 建议值」产出片段
    （id = sha1(path+original+proposed)[:12]，与 optimize 片段口径一致），
    **不落库**直接返回。

    Args:
        project_id: 项目 ID。
        req: 预览请求（scope / target / instruction）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        AssistPreviewOut: {scope, target, changes: [片段], count, summary}。

    Raises:
        HTTPException: 400 —— episode_body 该集无正文；404 —— 项目或剧集
            不存在/不归属；409 —— setting 已锁定或未生成 / 阶段未达大纲 /
            该集大纲未生成 / 阶段未达 body；422 —— scope/target/instruction
            校验失败；502 —— 改写 LLM 多次失败。
    """
    project = await get_owned_project(db, project_id, user.id)
    scope = req.scope

    if scope == "setting":
        # 阶段守卫：正文阶段设定锁定（与 setting 域口径一致）
        if project.script_phase == "body":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="正文已开始生成，故事设定已锁定")
        payload = _assist_setting_payload(project.story_setting)
        if payload is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="请先生成故事设定")
        changes, summary = await assist_rewrite("setting", payload, req.instruction)
        diffs = build_field_diffs(payload, changes)
        return AssistPreviewOut(scope=scope, target="all", changes=diffs,
                                count=len(diffs), summary=summary)

    if scope == "episode_outline":
        # 阶段守卫：outline/body 可用（对齐 Task 4 单集重生成口径）
        if project.script_phase not in ("outline", "body"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="请先进入分集大纲阶段（确认角色设定并生成分集大纲）")
        episode = await _get_owned_episode(db, project, req.target)
        _require_episode_outline(episode)
        payload = _assist_outline_payload(episode)
        changes, summary = await assist_rewrite("episode_outline", payload, req.instruction)
        diffs = build_field_diffs(payload, changes)
        return AssistPreviewOut(scope=scope, target=str(episode.id), changes=diffs,
                                count=len(diffs), summary=summary)

    # scope == "episode_body"：直接复用 optimize 预览路径（grain=global 全篇，
    # 行为与 optimize 口径完全一致，不重造）
    episode = await _get_owned_episode(db, project, req.target)
    _require_body_phase(project)
    script = await _get_episode_script(db, project.id, episode.id)
    _require_episode_body(script)
    plain = content_plain(script.content)
    changes = await optimize_episode_content(
        project, episode, plain, "global", None, req.instruction)
    diffs = build_field_diffs(plain, changes)
    return AssistPreviewOut(scope=scope, target=str(episode.id), changes=diffs,
                            count=len(diffs), summary="")


@router.post("/api/projects/{project_id}/script/assist/apply",
             response_model=AssistApplyOut)
async def apply_assist_changes(
    project_id: int,
    req: AssistApplyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssistApplyOut:
    """采纳对话式改写片段并落库（P4，片段回传校验 + 版本快照留痕）。

    逐片段校验：path 白名单（按 scope）→ proposed 非空 → original 与当前
    库值一致（不一致计 skipped 防并发覆盖）。按作用域落库：
    - setting：更新 story_setting 对应字段；用户采纳 AI 建议语义上属于
      「确认」动作，source 置 "user"（与手动编辑保存同口径）并刷新
      updated_at；
    - episode_outline：更新该集 title/logline 列与 outline JSONB 对应字段
      （characters 按顿号拆回名单；文本字段截长与列宽对齐）；
    - episode_body：复用 optimize/apply 既有落库逻辑（apply_optimize_changes
      的 path 白名单 + original 校验 + 场次 source=ai_optimized 重写 +
      UPDATE WHERE version 乐观锁，口径原样）。

    乐观锁/版本语义对齐 optimize/apply：applied=0 不递增版本、不落库、
    不写快照；applied>0 写一条 stage=assist_applied 版本快照（CONFIRM_STAGES
    扩值）。assist 采纳不改变项目阶段。

    Args:
        project_id: 项目 ID。
        req: 采纳请求（scope / target / accepts / base_version）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        AssistApplyOut: {applied, skipped, snapshot_id?, version?, phase,
        content?}（version/content 仅 episode_body 作用域携带）。

    Raises:
        HTTPException: 400 —— episode_body 该集无正文；404 —— 项目或剧集
            不存在/不归属；409 —— setting 已锁定或未生成 / 阶段未达大纲 /
            该集大纲未生成 / 阶段未达 body / base_version 不匹配 / 并发
            写入冲突；422 —— scope/target 校验失败。
    """
    project = await get_owned_project(db, project_id, user.id)
    scope = req.scope
    accepts = [item.model_dump() for item in req.accepts]

    # ---------- scope=setting：更新 story_setting 三段（source 置 user） ----------
    if scope == "setting":
        if project.script_phase == "body":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="正文已开始生成，故事设定已锁定")
        setting = project.story_setting if isinstance(project.story_setting, dict) else None
        if _assist_setting_payload(setting) is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="请先生成故事设定")
        applied = skipped = 0
        applied_paths: List[str] = []
        merged = {key: setting.get(key) for key in _ASSIST_SETTING_PATHS}
        for item in accepts:
            path = str(item.get("path") or "")
            if path not in _ASSIST_SETTING_PATHS:
                skipped += 1  # path 不在作用域白名单
                continue
            proposed = str(item.get("proposed") or "").strip()
            if not proposed:
                skipped += 1  # 空建议无意义
                continue
            want, have = _assist_want_have(item, setting.get(path))
            if want != have:
                skipped += 1  # 原文与当前库值不符（并发改动），跳过防覆盖
                continue
            merged[path] = proposed[:200] if path == "logline" else (
                proposed[:1000] if path == "core_conflict" else proposed[:2000])
            applied += 1
            applied_paths.append(path)
        if applied == 0:
            # 无任何片段被应用：不落库、不写快照
            return AssistApplyOut(applied=0, skipped=skipped, snapshot_id=None,
                                  version=None, phase=project.script_phase,
                                  content=None)
        # 用户采纳 AI 建议属「确认」动作：source 置 user（与手动编辑保存同
        # 口径）；整体重新赋值触发 JSONB 变更检测
        project.story_setting = {
            **merged,
            "source": "user",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        snapshot = await write_stage_snapshot(
            db, project, "assist_applied",
            summary={"scope": scope, "target": "all",
                     "applied": applied, "paths": applied_paths},
        )
        await db.commit()
        return AssistApplyOut(applied=applied, skipped=skipped,
                              snapshot_id=snapshot.id, version=None,
                              phase=project.script_phase, content=None)

    # ---------- scope=episode_outline：更新该集 title/logline/outline ----------
    if scope == "episode_outline":
        if project.script_phase not in ("outline", "body"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="请先进入分集大纲阶段（确认角色设定并生成分集大纲）")
        episode = await _get_owned_episode(db, project, req.target)
        outline = _require_episode_outline(episode)
        applied = skipped = 0
        applied_paths = []
        new_title: Optional[str] = None
        new_logline: Optional[str] = None
        new_characters: Optional[List[str]] = None
        new_outline = dict(outline)
        for item in accepts:
            path = str(item.get("path") or "")
            if path not in _ASSIST_OUTLINE_PATHS:
                skipped += 1
                continue
            proposed = str(item.get("proposed") or "").strip()
            if not proposed:
                skipped += 1
                continue
            if path == "characters":
                current = outline.get("characters") or []
            elif path in ("core_event", "emotion_curve"):
                current = outline.get(path)
            else:  # title / logline 列字段
                current = getattr(episode, path)
            want, have = _assist_want_have(item, current)
            if want != have:
                skipped += 1  # 原文与当前库值不符（并发改动），跳过防覆盖
                continue
            if path == "characters":
                new_characters = [s for s in (t.strip() for t in proposed.split("、")) if s]
            elif path in ("core_event", "emotion_curve"):
                new_outline[path] = proposed[:_ASSIST_OUTLINE_TEXT_LIMITS[path]]
            elif path == "title":
                new_title = proposed[:_ASSIST_OUTLINE_TEXT_LIMITS["title"]]
            else:
                new_logline = proposed[:_ASSIST_OUTLINE_TEXT_LIMITS["logline"]]
            applied += 1
            applied_paths.append(path)
        if applied == 0:
            # 无任何片段被应用：不落库、不写快照
            return AssistApplyOut(applied=0, skipped=skipped, snapshot_id=None,
                                  version=None, phase=project.script_phase,
                                  content=None)
        # 落库：列字段与 outline JSONB 均整体重新赋值触发变更检测
        if new_title is not None:
            episode.title = new_title
        if new_logline is not None:
            episode.logline = new_logline
        if new_characters is not None:
            new_outline["characters"] = new_characters
        episode.outline = new_outline
        snapshot = await write_stage_snapshot(
            db, project, "assist_applied",
            summary={"scope": scope, "target": str(episode.id),
                     "applied": applied, "paths": applied_paths},
        )
        await db.commit()
        return AssistApplyOut(applied=applied, skipped=skipped,
                              snapshot_id=snapshot.id, version=None,
                              phase=project.script_phase, content=None)

    # ---------- scope=episode_body：复用 optimize/apply 既有落库逻辑 ----------
    episode = await _get_owned_episode(db, project, req.target)
    _require_body_phase(project)
    script = await _get_episode_script(db, project.id, episode.id)
    _require_episode_body(script)
    # 可选乐观锁：仅当调用方显式传入 base_version 时校验（口径同 optimize/apply）
    if req.base_version is not None and req.base_version != script.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="剧本已被其他操作修改，请刷新")
    # 片段应用（path 白名单 + original 一致性 + 场次 source=ai_optimized 重写，
    # 口径与 optimize/apply 完全一致）
    new_content, applied, skipped = apply_optimize_changes(script.content, accepts)
    if applied == 0:
        # 无任何片段被应用：不递增版本、不落库、不写快照
        return AssistApplyOut(applied=0, skipped=skipped, snapshot_id=None,
                              version=script.version, phase=project.script_phase,
                              content=script.content)
    new_version = script.version + 1
    lock_result = await db.execute(
        update(Script)
        .where(Script.id == script.id, Script.version == script.version)
        .values(content=new_content, version=new_version)
    )
    if lock_result.rowcount == 0:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="剧本已被其他操作修改，请刷新")
    snapshot = await write_stage_snapshot(
        db, project, "assist_applied",
        summary={"scope": scope, "target": str(episode.id),
                 "applied": applied, "episode_id": episode.id},
    )
    await db.commit()
    await db.refresh(script)
    return AssistApplyOut(applied=applied, skipped=skipped,
                          snapshot_id=snapshot.id, version=script.version,
                          phase=project.script_phase, content=script.content)


# ---------------------------------------------------------------------------
# 子图 REST 驱动入口内部工具（P-5，Task 6）
# ---------------------------------------------------------------------------
# 子图检查点 thread_id 前缀（REST 驱动入口固定命名 script_{project_id}_{user_id}，
# 与主图会话线程 u{user_id}_xxx 区分；同用户同项目幂等续跑同一线程）
_PIPELINE_THREAD_PREFIX = "script"


def _pipeline_thread_id(project_id: int, user_id: int) -> str:
    """构造剧本子图检查点线程 ID（REST 驱动入口统一命名，内部函数）。

    命名规则 script_{project_id}_{user_id}：同一用户在同一项目上的多次
    run/stream 调用幂等续跑同一线程；不同用户/项目天然隔离。

    Args:
        project_id: 项目 ID。
        user_id: 当前用户 ID。

    Returns:
        str: 形如 "script_12_34" 的检查点线程 ID。

    Raises:
        无：纯字符串拼装。
    """
    return f"{_PIPELINE_THREAD_PREFIX}_{project_id}_{user_id}"


async def _load_pipeline_snapshot(graph, thread_id: str) -> dict:
    """读取剧本子图检查点快照（内部函数，异常按无检查点处理）。

    从 aget_state 快照中提取断点信息：状态值（含 user_id/project_id 归属
    通道与 bodies 计数）、待执行节点名、挂起 interrupt（原始值与输出模型
    两种形态，供 SSE 事件与只读端点分别使用）。

    Args:
        graph: 编译后的剧本子图（含 checkpointer）。
        thread_id: 检查点线程 ID。

    Returns:
        dict: {"values": 状态值, "next_nodes": List[str],
        "pending": Optional[PipelineInterruptOut],
        "pending_value": Optional[dict]（原始 interrupt 值，含 type/stage/payload）,
        "body_count": int（本轮子图已生成正文集数）}。

    Raises:
        不抛出异常：检查点读取失败时返回空快照（前端按「无断点」渲染）。
    """
    empty = {"values": {}, "next_nodes": [], "pending": None,
             "pending_value": None, "body_count": 0}
    try:
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    except Exception as exc:  # noqa: BLE001 —— 检查点读取失败按无检查点处理
        logger.warning("读取剧本子图检查点失败（thread_id=%s）：%s", thread_id, exc)
        return empty
    values = snapshot.values or {}
    next_nodes = [str(node) for node in (snapshot.next or ())]
    pending_value: Optional[dict] = None
    # 遍历被中断任务的 interrupt 列表，取首个带 type 的值（三闸口之一）
    for task in (snapshot.tasks or ()):
        for itp in (getattr(task, "interrupts", None) or ()):
            value = getattr(itp, "value", None)
            if isinstance(value, dict) and value.get("type"):
                pending_value = value
                break
        if pending_value is not None:
            break
    pending = (
        PipelineInterruptOut(type=str(pending_value["type"]), payload=pending_value.get("payload"))
        if pending_value is not None else None
    )
    return {
        "values": values,
        "next_nodes": next_nodes,
        "pending": pending,
        "pending_value": pending_value,
        "body_count": len(values.get("bodies") or []),
    }


def _assert_thread_owner(values: dict, user_id: int, project_id: int) -> None:
    """校验检查点线程归属（内部函数，thread_id 前缀 + state.user_id 双重校验）。

    线程 ID 已内嵌 user_id/project_id（前缀粗校验）；此处再对检查点状态中的
    user_id/project_id 通道做细校验，他人线程或跨项目访问一律 404（不泄露
    存在性）。无检查点（空状态）时不做校验（首次启动场景）。

    Args:
        values: 检查点状态值（可能为空 dict）。
        user_id: 当前用户 ID。
        project_id: 项目 ID。

    Returns:
        None。

    Raises:
        HTTPException: 404 —— 检查点归属与当前用户/项目不一致。
    """
    if not values:
        return
    owner = values.get("user_id")
    bound_project = values.get("project_id")
    if owner not in (None, user_id) or bound_project not in (None, project_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="剧本流水线会话不存在")


def _pipeline_pending_event(thread_id: str, pending_value: Optional[dict]) -> str:
    """把挂起 interrupt 格式化为 pending_confirm 事件帧（内部函数）。

    Args:
        thread_id: 检查点线程 ID。
        pending_value: 原始 interrupt 值（含 type/stage/payload）。

    Returns:
        str: SSE 事件帧（stage 由子图 interrupt 的 stage 字段给出：characters/
        outline/body；缺失时按 confirm_type 兜底映射）。

    Raises:
        不抛出异常：结构异常按空 payload 兜底。
    """
    value = pending_value if isinstance(pending_value, dict) else {}
    confirm_type = str(value.get("type") or "")
    stage = str(value.get("stage") or "") or _CONFIRM_STAGE_BY_TYPE.get(confirm_type, "")
    return sse_event("pending_confirm", {
        "thread_id": thread_id,
        "stage": stage,
        "confirm_type": confirm_type,
        "payload": value.get("payload"),
    })


async def _read_script_phase(project_id: int) -> Optional[str]:
    """读取项目当前剧本创作阶段（内部函数，SSE 生成器内独立会话）。

    说明：SSE 生成器生命周期跨越响应体发送阶段，此处不复用请求级会话
    （依赖注入的会话随响应结束关闭），改用运行时导入的会话工厂新开独立
    会话（运行时导入以兼容测试期的工厂重定向）。

    Args:
        project_id: 项目 ID。

    Returns:
        Optional[str]: 项目 script_phase（NULL=快速模式）；项目不存在/读取
        失败时返回 None。

    Raises:
        不抛出异常：数据库异常捕获后返回 None（done 事件阶段字段置空）。
    """
    from app.core.database import SessionLocal  # 运行时导入：兼容测试期工厂重定向

    try:
        async with SessionLocal() as session:
            project = await session.get(Project, project_id)
            return project.script_phase if project is not None else None
    except Exception as exc:  # noqa: BLE001 —— 阶段读取失败不影响流收尾
        logger.warning("读取项目剧本阶段失败（project_id=%s）：%s", project_id, exc)
        return None


async def _pipeline_event_generator(
    *,
    graph,
    config: dict,
    graph_input,
    thread_id: str,
    project_id: int,
    pending_value: Optional[dict] = None,
) -> AsyncGenerator[str, None]:
    """剧本子图 SSE 事件生成器（run/stream 与 resume 共用，内部函数）。

    事件协议：
    - node：{"node": 节点名} —— 每完成一个节点推送一次（进度可视）；
    - pending_confirm：{"thread_id", "stage", "confirm_type", "payload"} ——
      触达三处人工确认闸口（interrupt）时推送，本次流结束，等 resume 续跑；
    - error：{"message"} —— 节点 error 通道非空（前置不满足/退回重生等）或
      图执行异常；
    - done：{"thread_id", "script_phase", "interrupted", "error"} —— 流收尾
      （无论暂停/完成/报错均发出，便于前端收敛 UI）。

    graph_input 为 None 时表示「不重跑图」（已挂起待确认或已完成的幂等重入）：
    仅回放 pending_confirm（若有）与 done。

    Args:
        graph: 编译后的剧本子图（含 checkpointer）。
        config: 运行配置（{"configurable": {"thread_id": ...}}）。
        graph_input: 图输入（首次运行 dict；续跑 Command(resume=...)；
            None 表示仅回放状态不执行）。
        thread_id: 检查点线程 ID。
        project_id: 项目 ID（done 事件回读 script_phase 用）。
        pending_value: 幂等重入时已有挂起 interrupt 的原始值（可空）。

    Yields:
        str: sse_event 格式化的 SSE 事件帧。

    Raises:
        不抛出异常：图执行异常转 error 事件后正常收尾。
    """
    interrupted = pending_value
    error_message = ""
    if graph_input is not None:
        try:
            async for chunk in graph.astream(graph_input, config=config, stream_mode="updates"):
                # HITL：interrupt 更新 → 本次流结束（检查点已保存暂停态）
                if isinstance(chunk, dict) and "__interrupt__" in chunk:
                    for itp in (chunk.get("__interrupt__") or ()):
                        value = getattr(itp, "value", None)
                        if isinstance(value, dict) and value.get("type"):
                            interrupted = value
                            break
                    # 被中断节点因抛 GraphInterrupt 不会出现在 updates 流中，
                    # 从检查点快照补推 node 事件，保证前端进度完整
                    snapshot_next = (await _load_pipeline_snapshot(graph, thread_id))["next_nodes"]
                    for node_name in snapshot_next:
                        yield sse_event("node", {"node": node_name})
                    break
                for node_name, update_ in (chunk or {}).items():
                    if node_name == "__interrupt__" or not isinstance(update_, dict):
                        continue
                    yield sse_event("node", {"node": node_name})
                    # 节点 error 通道非空（前置不满足/退回重生中）：透传给前端
                    if update_.get("error"):
                        error_message = str(update_["error"])
        except Exception as exc:  # noqa: BLE001 —— 图级异常兜底为 error 事件
            logger.warning("剧本子图执行异常（thread_id=%s）：%s", thread_id, exc)
            error_message = "剧本流水线执行失败，请稍后重试"
            interrupted = None
    if interrupted is not None:
        yield _pipeline_pending_event(thread_id, interrupted)
    if error_message:
        yield sse_event("error", {"message": error_message})
    phase = await _read_script_phase(project_id)
    yield sse_event("done", {
        "thread_id": thread_id,
        "script_phase": phase,
        "interrupted": interrupted is not None,
        "error": error_message or None,
    })


# interrupt type → 剧本阶段兜底映射（子图 interrupt 已带 stage 字段，此处防御旧检查点）
_CONFIRM_STAGE_BY_TYPE = {
    "confirm_characters": "characters",
    "confirm_episodes": "outline",
    "confirm_body": "body",
}


# ---------------------------------------------------------------------------
# 13. GET /script/pipeline/state：剧本子图断线恢复（P-5，Task 6）
# ---------------------------------------------------------------------------
@router.get("/api/projects/{project_id}/script/pipeline/state",
            response_model=PipelineStateOut)
async def get_pipeline_state(
    project_id: int,
    thread_id: str = Query(min_length=1, max_length=80, description="子图检查点线程 ID"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PipelineStateOut:
    """剧本子图断线恢复端点：按 thread_id 从检查点回放子图运行状态。

    通过子图 aget_state(thread_id) 读取检查点快照，返回：
    - next_nodes：断点处待继续执行的节点名（interrupt 暂停时为被中断节点）；
    - pending_interrupt：挂起的 interrupt（type/payload，即 confirm_characters/
      confirm_episodes/confirm_body 之一与候选数据摘要）；无检查点/无挂起为 null；
    - summary：进度摘要（phase=项目阶段 / character_count=config 候选角色数 /
      episode_count=大纲集数 / body_count=本轮子图已生成集数，DB 与检查点混合）。

    与 Agent 会话恢复（POST /api/agent/chat/resume）的关系：本端点只读回放
    （供前端渲染「继续/重试」卡片），续跑仍走 resume 端点。

    Args:
        project_id: 项目 ID。
        thread_id: 子图检查点线程 ID（查询参数）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PipelineStateOut: {thread_id, project_id, phase, next_nodes,
        pending_interrupt, summary}。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目；401 —— 未登录。
    """
    project = await get_owned_project(db, project_id, user.id)
    graph = await get_compiled_script_pipeline()
    snapshot = await _load_pipeline_snapshot(graph, thread_id)
    # 归属细校验：检查点内 user_id/project_id 与当前访问者一致（他人线程 404）
    _assert_thread_owner(snapshot["values"], user.id, project.id)
    # 进度摘要（DB 侧）：候选角色数与大纲集数
    candidates = _load_candidates(project)
    episodes = await _list_project_episodes(db, project.id)
    return PipelineStateOut(
        thread_id=thread_id,
        project_id=project.id,
        phase=project.script_phase,
        next_nodes=snapshot["next_nodes"],
        pending_interrupt=snapshot["pending"],
        summary={
            "character_count": len(candidates),
            "episode_count": len(episodes),
            "body_count": snapshot["body_count"],
        },
    )


# ---------------------------------------------------------------------------
# 14. GET /script/pipeline/status：剧本子图断线重连状态（P-5，Task 6）
# ---------------------------------------------------------------------------
@router.get("/api/projects/{project_id}/script/pipeline/status",
            response_model=ScriptPipelineStatusOut)
async def get_pipeline_status(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScriptPipelineStatusOut:
    """剧本子图断线重连状态：返回当前阶段、断点处阶段与待确认闸口摘要。

    thread_id 由服务端按 script_{project_id}_{user_id} 规则推导（与
    run/stream、resume 一致），无需前端传参；归属经项目归属（404）与检查点
    状态 user_id/project_id（404）双重校验。

    返回（供前端 Task 11 刷新/重连恢复）：
    - script_phase：项目当前剧本阶段（DB 侧）；
    - current_stage：断点处待执行节点名（无断点为空串）；
    - pending_stage：挂起确认闸口对应阶段 characters/outline/body（无为空串）；
    - has_pending_confirm：是否存在待人工确认；
    - summary：character_count / episode_count / body_count 进度摘要。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ScriptPipelineStatusOut: 断线重连状态（见上）。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目，或检查点线程归属不符；
            401 —— 未登录。
    """
    project = await get_owned_project(db, project_id, user.id)
    thread_id = _pipeline_thread_id(project_id, user.id)
    graph = await get_compiled_script_pipeline()
    snapshot = await _load_pipeline_snapshot(graph, thread_id)
    _assert_thread_owner(snapshot["values"], user.id, project.id)
    pending_value = snapshot["pending_value"] or {}
    # 挂起闸口阶段：优先取 interrupt 的 stage 字段，缺省按 type 兜底映射
    pending_stage = str(pending_value.get("stage") or "") or _CONFIRM_STAGE_BY_TYPE.get(
        str(pending_value.get("type") or ""), "")
    candidates = _load_candidates(project)
    episodes = await _list_project_episodes(db, project.id)
    return ScriptPipelineStatusOut(
        thread_id=thread_id,
        project_id=project.id,
        script_phase=project.script_phase,
        current_stage=snapshot["next_nodes"][0] if snapshot["next_nodes"] else "",
        next_nodes=snapshot["next_nodes"],
        pending_stage=pending_stage,
        has_pending_confirm=snapshot["pending"] is not None,
        pending_confirm=snapshot["pending"],
        summary={
            "character_count": len(candidates),
            "episode_count": len(episodes),
            "body_count": snapshot["body_count"],
        },
    )


# ---------------------------------------------------------------------------
# 15. POST /script/pipeline/run/stream：启动/续跑剧本子图（SSE，P-5，Task 6）
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/script/pipeline/run/stream")
async def run_script_pipeline_stream(
    project_id: int,
    req: Optional[ScriptPipelineRunRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """启动/续跑剧本四阶段子图（SSE，P-5「整跑 + 断点续跑」路径）。

    幂等语义（thread_id 固定为 script_{project_id}_{user_id}）：
    - 无检查点 → 以 {"project_id", "user_id", "instruction", "auto_confirm"}
      启动子图，跑到首个确认闸口（pending_confirm）或全流程完成（done）；
    - 已有挂起确认闸口 → 不重跑图（避免重复 LLM 调用），直接回放
      pending_confirm + done；auto_confirm=True 时以「自动确认」续跑至下一个
      闸口/完成；
    - 已完成（body 确认）→ 直接回放 done。

    事件协议与 SSE 事件生成器见 _pipeline_event_generator。

    Args:
        project_id: 项目 ID。
        req: 可选请求体（instruction 入口补充指令 / auto_confirm 一键串跑）。
        user: 当前登录用户。
        db: 请求级异步数据库会话（归属校验与检查点回放）。

    Returns:
        StreamingResponse: SSE 流式响应（node/pending_confirm/error/done）。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目，或检查点线程归属不符；
            401 —— 未登录。
    """
    project = await get_owned_project(db, project_id, user.id)
    thread_id = _pipeline_thread_id(project_id, user.id)
    auto_confirm = bool(req.auto_confirm) if req else False
    graph = await get_compiled_script_pipeline()
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await _load_pipeline_snapshot(graph, thread_id)
    _assert_thread_owner(snapshot["values"], user.id, project.id)

    pending_value = snapshot["pending_value"]
    completed = bool((snapshot["values"].get("confirmed_flags") or {}).get("body"))
    if pending_value is not None:
        # 已挂起待确认：auto_confirm 时自动确认续跑，否则原样回放待确认事件
        graph_input = (
            Command(resume={"decision": "approved", "auto": True}) if auto_confirm else None)
    elif not snapshot["next_nodes"] and completed:
        # 流水线已完成：幂等回放 done（不重跑，避免重复 LLM 调用）
        graph_input = None
    else:
        graph_input = {
            "project_id": project.id,
            "user_id": user.id,
            "instruction": (req.instruction if req else None),
            "auto_confirm": auto_confirm,
        }
    return await sse_stream(_pipeline_event_generator(
        graph=graph,
        config=config,
        graph_input=graph_input,
        thread_id=thread_id,
        project_id=project.id,
        pending_value=pending_value,
    ))


# ---------------------------------------------------------------------------
# 16. POST /script/pipeline/resume：确认/退回续跑剧本子图（SSE，P-5，Task 6）
# ---------------------------------------------------------------------------
@router.post("/api/projects/{project_id}/script/pipeline/resume")
async def resume_script_pipeline(
    project_id: int,
    req: ScriptPipelineResumeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """确认/退回续跑剧本子图（SSE，复用 MA-08 pending_confirm/resume 协议）。

    请求 body：{"decision": "approved"|"rejected", "reason"?, "edited_payload"?}；
    服务端组装 resume 值 {"decision", "approved", "reason"?, **edited_payload}
    经 Command(resume=...) 续跑被中断的节点：
    - approved：按当前候选（或 edited_payload 中的人工编辑稿）落库并推进阶段，
      继续跑到下一个闸口或完成；
    - rejected：回退上游生成节点按 reason 重新生成，随后再次停在确认闸口。

    前置：该线程必须存在挂起的确认闸口（无检查点 404；有检查点但无挂起 409）。

    Args:
        project_id: 项目 ID。
        req: 确认/退回请求体（decision 必填，reason/edited_payload 可选）。
        user: 当前登录用户。
        db: 请求级异步数据库会话（归属校验与检查点回放）。

    Returns:
        StreamingResponse: SSE 流式响应（node/pending_confirm/error/done）。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目，检查点线程不存在或归属
            不符；409 —— 当前没有待确认的闸口（无可续跑的断点）；401 —— 未登录。
    """
    project = await get_owned_project(db, project_id, user.id)
    thread_id = _pipeline_thread_id(project_id, user.id)
    graph = await get_compiled_script_pipeline()
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await _load_pipeline_snapshot(graph, thread_id)
    _assert_thread_owner(snapshot["values"], user.id, project.id)
    if not snapshot["values"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="剧本流水线会话不存在")
    if snapshot["pending_value"] is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="当前没有待确认的剧本流水线断点")

    resume_value = {
        "decision": req.decision,
        "approved": req.decision == "approved",
    }
    if req.reason:
        resume_value["reason"] = req.reason
    if req.edited_payload:
        resume_value.update(req.edited_payload)
    return await sse_stream(_pipeline_event_generator(
        graph=graph,
        config=config,
        graph_input=Command(resume=resume_value),
        thread_id=thread_id,
        project_id=project.id,
    ))
