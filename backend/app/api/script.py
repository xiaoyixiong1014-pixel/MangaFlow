"""剧本工作流路由模块（文件级说明）。

实现剧本域全部接口（每个接口先校验项目归属，他人或已删除项目一律 404，
不泄露存在性）：
- GET    /api/projects/{project_id}/script                  取最新剧本行
- PUT    /api/projects/{project_id}/script/input            保存原始输入（idea/novel/outline）
- POST   /api/projects/{project_id}/script/generate/stream  SSE 流式生成剧本（POST 方法）
- PUT    /api/projects/{project_id}/script                  手动保存（RD-03 source=user）
- POST   /api/projects/{project_id}/script/health           规则体检（RD-02）
- POST   /api/projects/{project_id}/script/extract          实体抽取（规则 + LLM 双路）
- POST   /api/projects/{project_id}/script/extract/confirm  抽取确认入池（建资产行）
- POST   /api/projects/{project_id}/script/send-to-agent    剧本一键同步 Agent 会话（S-08，幂等）
- POST   /api/projects/{project_id}/script/send-to-canvas   剧本一键投递画布（S-08，幂等）

注意：FastAPI 的 router prefix 不能携带路径参数，因此本路由不设 prefix，
路由内部直接书写完整路径。
"""

import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.core.sse import sse_event, sse_stream
from app.models import AgentThread, Canvas, CanvasNode, Project, Script, User
from app.providers.llm import chat_stream
from app.schemas.script import (
    ExtractConfirmOut,
    ExtractConfirmRequest,
    ExtractResultOut,
    ScriptInputRequest,
    ScriptOut,
    ScriptSaveRequest,
    ScriptSendToAgentOut,
    ScriptSendToAgentRequest,
    ScriptSendToCanvasOut,
    ScriptSendToCanvasRequest,
)
from app.services.script_service import (
    build_fallback_script,
    build_generate_messages,
    get_owned_project,
    get_script_or_404,
    get_script_scoped_or_404,
    parse_script_json,
    run_health_check,
    confirm_extract,
    extract_entities,
    validate_episode,
    wrap_content_fields,
)

router = APIRouter(tags=["script"])


@router.get("/api/projects/{project_id}/script", response_model=ScriptOut)
async def get_script(
    project_id: int,
    episode_id: Optional[int] = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScriptOut:
    """取剧本行（可按剧集过滤）；对应范围尚无剧本时返回 404。

    Args:
        project_id: 项目 ID。
        episode_id: 可选剧集 ID（S-06；未指定时定位「未绑定剧集」的默认
            剧本行——存量数据 episode_id 均为 NULL，行为与既有接口一致；
            传入时按该剧集过滤）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ScriptOut: 剧本信息（content/raw_text/fences/health_report/version/updated_at）。

    Raises:
        HTTPException: 404 —— 项目不存在或对应范围剧本未创建。
    """
    project = await get_owned_project(db, project_id, user.id)
    script = await get_script_scoped_or_404(db, project, episode_id)
    return ScriptOut.model_validate(script)


@router.put("/api/projects/{project_id}/script/input", response_model=ScriptOut)
async def save_script_input(
    project_id: int,
    req: ScriptInputRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScriptOut:
    """保存剧本原始输入：不存在则建剧本行，存在则覆盖 raw_text。

    content 中记录输入模式 {"input_mode": mode}（保留既有字段做合并）。

    Args:
        project_id: 项目 ID。
        req: 输入请求（mode: idea/novel/outline，raw_text 非空；episode_id
            可选，S-06 传入时绑定该剧集的剧本行）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ScriptOut: 保存后的剧本信息。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目或剧集不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    if req.episode_id is not None:
        # S-06：绑定指定剧集的剧本行（校验剧集归属，避免跨项目写挂外键）
        await validate_episode(db, project, req.episode_id)
    script = (
        await db.execute(
            select(Script).where(
                Script.project_id == project.id,
                Script.episode_id.is_(None)
                if req.episode_id is None
                else Script.episode_id == req.episode_id,
            )
        )
    ).scalar_one_or_none()
    if script is None:
        # 首次保存：新建剧本行，content 仅记录输入模式
        script = Script(
            project_id=project.id,
            episode_id=req.episode_id,
            content={"input_mode": req.mode},
            raw_text=req.raw_text,
            version=1,
        )
        db.add(script)
    else:
        script.raw_text = req.raw_text
        # content 合并记录输入模式（不覆盖已生成的剧本字段）
        merged = dict(script.content or {})
        merged["input_mode"] = req.mode
        script.content = merged
    await db.commit()
    await db.refresh(script)
    return ScriptOut.model_validate(script)


@router.post("/api/projects/{project_id}/script/generate/stream")
async def generate_script_stream(
    project_id: int,
    episode_id: Optional[int] = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """SSE 流式生成剧本（POST 方法，text/event-stream）。

    事件序列：
    - `delta`：{"text": 增量文本}（LLM 增量原样透传）；
    - `error`：{"message": "模型不可用，已使用离线降级生成"}（仅 LLM 异常时；
       降级为基于 raw_text 拆句的确定性 3 幕占位剧本，离线降级路径）；
    - `done`：{"script_id", "version", "content"}（content 每个一级字段
       已按 RD-03 包装 {"value":…, "source":"ai"}；version+1；project.status='scripting'）。

    Args:
        project_id: 项目 ID。
        episode_id: 可选剧集 ID（S-06；未指定时作用于「未绑定剧集」的默认
            剧本行，与既有行为一致；传入时按该剧集定位剧本行）。
        user: 当前用户。
        db: 请求级异步数据库会话（SSE 流期间保持有效）。

    Returns:
        StreamingResponse: SSE 流式响应。

    Raises:
        HTTPException: 400 —— 剧本无原始输入文本；404 —— 项目/剧集或剧本不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    if episode_id is not None:
        await validate_episode(db, project, episode_id)
    script = await get_script_scoped_or_404(db, project, episode_id)
    if not (script.raw_text or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="剧本无原始输入文本，请先保存剧本输入")
    messages = build_generate_messages(project, script.raw_text)

    async def event_generator() -> AsyncGenerator[str, None]:
        """SSE 事件生成器：delta 透传 → 解析（容错）落库 → done。

        Yields:
            str: sse_event 格式化的事件帧。

        Raises:
            无：LLM 异常在内部捕获并走离线降级，保证始终发出 done 事件。
        """
        collected: List[str] = []
        degraded = False
        try:
            async for delta in chat_stream(messages):
                collected.append(delta)
                yield sse_event("delta", {"text": delta})  # 模型增量原样透传
        except Exception:  # noqa: BLE001 —— LLM 不可用时必须走离线降级
            degraded = True

        raw_full = "".join(collected)
        if degraded or not raw_full.strip():
            # 离线降级：基于 raw_text 拆句构造确定性 3 幕占位剧本（不依赖模型）
            parsed = build_fallback_script(script.raw_text or "")
            yield sse_event("error", {"message": "模型不可用，已使用离线降级生成"})
        else:
            # 解析容错：剥 ```json 围栏；失败则 content={"raw": 全文}
            parsed = parse_script_json(raw_full)

        # RD-03 字段级来源标记：每个一级字段包装 {"value":…, "source":"ai"}
        script.content = wrap_content_fields(parsed, "ai")
        script.version += 1
        project.status = "scripting"
        await db.commit()
        yield sse_event("done", {
            "script_id": script.id,
            "version": script.version,
            "content": script.content,
        })

    return await sse_stream(event_generator())


@router.put("/api/projects/{project_id}/script", response_model=ScriptOut)
async def save_script(
    project_id: int,
    req: ScriptSaveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScriptOut:
    """手动保存剧本：content 整体包装 {"value": content, "source": "user"}（RD-03），
    version += 1；可选回写 health_report。

    Args:
        project_id: 项目 ID。
        req: 保存请求（content 必填，health_report/episode_id 可选；episode_id
            传入时按该剧集定位剧本行，缺省定位默认剧本行）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ScriptOut: 保存后的剧本信息。

    Raises:
        HTTPException: 404 —— 项目不存在、剧集不存在或对应范围剧本未创建。
    """
    project = await get_owned_project(db, project_id, user.id)
    if req.episode_id is not None:
        await validate_episode(db, project, req.episode_id)
    script = await get_script_scoped_or_404(db, project, req.episode_id)
    # 整体包装为 user 来源（RD-03 字段级来源标记）
    script.content = {"value": req.content, "source": "user"}
    script.version += 1
    if req.health_report is not None:
        script.health_report = req.health_report
    await db.commit()
    await db.refresh(script)
    return ScriptOut.model_validate(script)


@router.post("/api/projects/{project_id}/script/health")
async def script_health(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """剧本规则体检（RD-02），结果落库 health_report 并返回。

    体检规则：无对白 / 对白无角色名 / 场景时长合计与 target_duration 偏差>20%
    （详见 script_service.run_health_check 注释）。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"score": 0~100, "issues": [{"level","message","location"}]}。

    Raises:
        HTTPException: 404 —— 项目或剧本不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    script = await get_script_or_404(db, project)
    report = run_health_check(project, script)
    script.health_report = report
    await db.commit()
    return report


@router.post("/api/projects/{project_id}/script/extract", response_model=ExtractResultOut)
async def script_extract(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ExtractResultOut:
    """实体抽取（规则 + LLM 双路）：结果存 scripts.fences 并返回。

    LLM 失败自动降级为规则结果；别名冲突检测（名字互相包含）产出 conflicts；
    confidence<0.7 的条目标记 low_confidence=true。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ExtractResultOut: {"characters","scenes","props","conflicts"}。

    Raises:
        HTTPException: 400 —— 剧本无原始输入文本；404 —— 项目或剧本不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    script = await get_script_or_404(db, project)
    result = await extract_entities(db, project, script)
    return ExtractResultOut.model_validate(result)


@router.post("/api/projects/{project_id}/script/extract/confirm", response_model=ExtractConfirmOut)
async def script_extract_confirm(
    project_id: int,
    req: ExtractConfirmRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ExtractConfirmOut:
    """抽取确认入池：按确认清单建/更 characters / scenes / props 资产行，
    project.status='asset'，写 version_snapshots(stage='extract_confirmed')。

    Args:
        project_id: 项目 ID。
        req: 确认清单（characters/scenes/props）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ExtractConfirmOut: 项目内各类资产现有总数。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目。
    """
    project = await get_owned_project(db, project_id, user.id)
    counts = await confirm_extract(db, project, req)
    return ExtractConfirmOut.model_validate(counts)


# ---------------------------------------------------------------------------
# S-08 剧本一键同步（Agent 会话 / 画布投递，均幂等）
# ---------------------------------------------------------------------------

def _content_plain(content: Any) -> Dict[str, Any]:
    """把剧本 content JSONB 解包为纯结构 dict（内部函数）。

    兼容三种落库形态（与前端 normalizeScript 口径一致）：
    - 整体包装 {"value": {...}, "source": "user"}（手动保存）；
    - 字段级包装 {"title": {"value":…, "source":…}, ...}（AI 生成，RD-03）；
    - 纯文本 {"raw": 全文} 或其他自由结构（原样返回）。

    Args:
        content: 剧本 content JSONB 值（可能为 None）。

    Returns:
        Dict[str, Any]: 解包后的纯结构；content 非法时返回空 dict。

    Raises:
        无：结构异常一律按空结构处理，不抛出。
    """
    if not isinstance(content, dict):
        return {}
    if isinstance(content.get("value"), dict) and "source" in content:
        return dict(content["value"])
    plain: Dict[str, Any] = {}
    for key, val in content.items():
        if isinstance(val, dict) and "value" in val and "source" in val:
            plain[key] = val["value"]
        else:
            plain[key] = val
    return plain


def _content_has_payload(plain: Dict[str, Any]) -> bool:
    """判断解包后的剧本结构是否含实质内容（内部函数）。

    Args:
        plain: _content_plain 的解包结果。

    Returns:
        bool: raw/title/logline/outline/scenes 任一非空即为 True。

    Raises:
        无：纯字典取值判断。
    """
    if plain.get("raw") or plain.get("title") or plain.get("logline"):
        return True
    if plain.get("outline") or plain.get("scenes"):
        return True
    return False


def _collect_dialogue_characters(plain: Dict[str, Any]) -> List[str]:
    """从剧本结构中收集出现过的角色名（内部函数，去重保序，限 10 个）。

    Args:
        plain: _content_plain 的解包结果。

    Returns:
        List[str]: 角色名列表（无角色数据时为空列表）。

    Raises:
        无：结构异常按缺数据处理。
    """
    names: List[str] = []
    for scene in (plain.get("scenes") or []):
        if not isinstance(scene, dict):
            continue
        for dlg in (scene.get("dialogue") or []):
            if isinstance(dlg, dict):
                name = str(dlg.get("character") or "").strip()
                if name and name not in names:
                    names.append(name)
    return names[:10]


def _build_script_digest(project: Project, script: Script,
                         plain: Dict[str, Any], episode_id: Optional[int]) -> str:
    """构造携带剧本上下文的首条用户消息摘要（内部函数，中文结构化要点）。

    Args:
        project: 已校验归属的项目（取标题兜底）。
        script: 剧本 ORM 对象。
        plain: _content_plain 的解包结果。
        episode_id: 剧集 ID（可空，用于标注同步范围）。

    Returns:
        str: 多行中文摘要文本（标题/集数/梗概/大纲/场景/角色/原文节选）。

    Raises:
        无：纯字符串拼装。
    """
    title = str(plain.get("title") or project.title or "未命名剧本")
    lines: List[str] = [f"【剧本同步】《{title}》（版本 v{script.version}）"]
    if episode_id is not None:
        lines.append(f"- 剧集：episode_id={episode_id}")
    if plain.get("logline"):
        lines.append(f"- 一句话梗概：{plain['logline']}")
    outline = plain.get("outline") or []
    if outline:
        acts = []
        for act in outline:
            if isinstance(act, dict):
                acts.append(f"{act.get('act', '')} {act.get('summary', '')}".strip())
        if acts:
            lines.append(f"- 三幕大纲：{'；'.join(acts[:5])}")
    scenes = plain.get("scenes") or []
    if scenes:
        names = []
        for idx, sc in enumerate(scenes, start=1):
            if isinstance(sc, dict):
                names.append(f"场景{idx}《{sc.get('title', '')}》")
        lines.append(f"- 场景（共 {len(scenes)} 场）：{'、'.join(names[:8])}")
    chars = _collect_dialogue_characters(plain)
    if chars:
        lines.append(f"- 角色：{'、'.join(chars)}")
    raw = (script.raw_text or "").strip()
    if raw:
        lines.append(f"- 原始输入节选：{raw[:200]}{'…' if len(raw) > 200 else ''}")
    lines.append("以上为剧本上下文摘要，请基于该剧本为我提供后续创作协助。")
    return "\n".join(lines)


def _require_script_payload(script: Script) -> Dict[str, Any]:
    """校验剧本非空并返回解包结构（内部函数，两个同步端点共用）。

    Args:
        script: 剧本 ORM 对象。

    Returns:
        Dict[str, Any]: 解包后的纯结构。

    Raises:
        HTTPException: 400 —— 剧本无原始输入且无结构化内容（空剧本）。
    """
    plain = _content_plain(script.content)
    if not (script.raw_text or "").strip() and not _content_has_payload(plain):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="剧本内容为空，请先生成或保存剧本")
    return plain


@router.post("/api/projects/{project_id}/script/send-to-agent", response_model=ScriptSendToAgentOut)
async def send_script_to_agent(
    project_id: int,
    req: Optional[ScriptSendToAgentRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScriptSendToAgentOut:
    """剧本一键同步 Agent 会话（S-08）：构造携带剧本上下文的首条用户消息，
    落库 agent_threads（state.messages 插入首条 user 消息，state.script_sync
    记录 {project_id, script_version} 同步标记）。

    幂等语义：同一剧本版本（project_id + script_version）重复调用不重复建
    会话——命中既有同步标记时直接返回该会话 ID 与 duplicated=true。
    消息摘要同时写入 agent_threads.state JSONB（GET /api/agent/threads/{id}/messages
    可回放）；LangGraph 检查点侧上下文由用户在会话内继续对话时自然建立。

    Args:
        project_id: 项目 ID。
        req: 同步请求（thread_id 可选=复用既有会话，episode_id 可选=S-06）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ScriptSendToAgentOut: {"thread_id": 会话 ID, "duplicated": 是否命中幂等}。

    Raises:
        HTTPException: 400 —— 剧本内容为空；404 —— 项目/剧集/剧本不存在，
            或传入的 thread_id 不属于当前用户。
    """
    project = await get_owned_project(db, project_id, user.id)
    body = req or ScriptSendToAgentRequest()
    if body.episode_id is not None:
        await validate_episode(db, project, body.episode_id)
    script = await get_script_scoped_or_404(db, project, body.episode_id)
    plain = _require_script_payload(script)

    sync_meta = {"project_id": project.id, "script_version": script.version}
    # 幂等查重：同项目同剧本版本已有同步标记 → 返回既有会话（JSONB 路径 CAST 比较）
    existing = (
        await db.execute(
            select(AgentThread)
            .where(
                AgentThread.user_id == user.id,
                AgentThread.project_id == project.id,
                AgentThread.state["script_sync"]["project_id"].as_integer() == project.id,
                AgentThread.state["script_sync"]["script_version"].as_integer() == script.version,
            )
            .order_by(AgentThread.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return ScriptSendToAgentOut(thread_id=existing.thread_id, duplicated=True)

    message = _build_script_digest(project, script, plain, body.episode_id)
    if body.thread_id:
        # 复用既有会话：校验归属后追加首条剧本消息与同步标记
        thread = (
            await db.execute(
                select(AgentThread).where(
                    AgentThread.thread_id == body.thread_id,
                    AgentThread.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if thread is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
        state = dict(thread.state or {})
        msgs = list(state.get("messages") or [])
        msgs.append({"role": "user", "content": message})
        # JSONB 整体重新赋值以触发变更检测（与 runner 既有约定一致）
        thread.state = {**state, "messages": msgs, "script_sync": sync_meta}
        thread.stage = thread.stage or "script"
        thread.project_id = thread.project_id or project.id
        await db.commit()
        return ScriptSendToAgentOut(thread_id=thread.thread_id, duplicated=False)

    # 新建会话：thread_id 命名规则与 agent_chat.chat_stream 缺省一致
    new_thread_id = f"u{user.id}_{uuid.uuid4().hex[:8]}"
    db.add(AgentThread(
        thread_id=new_thread_id,
        user_id=user.id,
        project_id=project.id,
        stage="script",
        state={
            "intent": "script",
            "messages": [{"role": "user", "content": message}],
            "script_sync": sync_meta,
        },
    ))
    await db.commit()
    return ScriptSendToAgentOut(thread_id=new_thread_id, duplicated=False)


@router.post("/api/projects/{project_id}/script/send-to-canvas", response_model=ScriptSendToCanvasOut)
async def send_script_to_canvas(
    project_id: int,
    req: Optional[ScriptSendToCanvasRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScriptSendToCanvasOut:
    """剧本一键投递画布（S-08）：向目标画布写入一个 text 类型节点（data=
    {"text": 剧本摘要, "label": 剧本标识}），节点位置排布与分镜投递
    （storyboards.deliver_to_canvas）的 text 节点一致。

    幂等语义：canvas_nodes.meta 记录 script_sync={project_id, script_version}，
    同剧本版本重复投递查重命中时返回既有节点与 duplicated=true（不新建）。

    目标画布选择：body.canvas_id 指定时校验归属（不属于该项目/本人/已软删
    一律 404）；缺省取该项目最近创建的画布，项目尚无画布时自动创建
    标题为「剧本画布」的 standalone 画布。

    Args:
        project_id: 项目 ID。
        req: 投递请求（canvas_id 可选，episode_id 可选=S-06）。
        user: 当前用户（画布拥有者）。
        db: 请求级异步数据库会话。

    Returns:
        ScriptSendToCanvasOut: {"canvas_id", "node_id", "duplicated"}。

    Raises:
        HTTPException: 400 —— 剧本内容为空；404 —— 项目/剧集/剧本/画布不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    body = req or ScriptSendToCanvasRequest()
    if body.episode_id is not None:
        await validate_episode(db, project, body.episode_id)
    script = await get_script_scoped_or_404(db, project, body.episode_id)
    plain = _require_script_payload(script)

    sync_meta = {"project_id": project.id, "script_version": script.version}
    # 幂等查重：项目内已有同剧本版本的投递节点 → 返回既有节点（联表排除软删画布）
    existing_node = (
        await db.execute(
            select(CanvasNode)
            .join(Canvas, CanvasNode.canvas_id == Canvas.id)
            .where(
                CanvasNode.node_type == "text",
                CanvasNode.meta["script_sync"]["project_id"].as_integer() == project.id,
                CanvasNode.meta["script_sync"]["script_version"].as_integer() == script.version,
                Canvas.deleted_at.is_(None),
            )
            .order_by(CanvasNode.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_node is not None:
        return ScriptSendToCanvasOut(
            canvas_id=existing_node.canvas_id, node_id=existing_node.id, duplicated=True)

    # 目标画布：指定则校验归属；缺省取项目最近画布，无则创建「剧本画布」
    if body.canvas_id is not None:
        canvas = await db.get(Canvas, body.canvas_id)
        if (canvas is None or canvas.project_id != project.id
                or canvas.user_id != user.id or canvas.deleted_at is not None):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="画布不存在")
    else:
        canvas = (
            await db.execute(
                select(Canvas)
                .where(
                    Canvas.project_id == project.id,
                    Canvas.user_id == user.id,
                    Canvas.deleted_at.is_(None),
                )
                .order_by(Canvas.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if canvas is None:
            canvas = Canvas(user_id=user.id, project_id=project.id,
                            title="剧本画布", type="standalone")
            db.add(canvas)
            await db.flush()  # 取 canvas.id 供节点外键使用

    node = CanvasNode(
        canvas_id=canvas.id,
        node_type="text",
        position_x=100.0, position_y=100.0,
        data={"text": _build_script_digest(project, script, plain, body.episode_id),
              "label": f"剧本 · v{script.version}"},
        meta={"script_sync": sync_meta},
    )
    db.add(node)
    await db.commit()
    return ScriptSendToCanvasOut(canvas_id=canvas.id, node_id=node.id, duplicated=False)
