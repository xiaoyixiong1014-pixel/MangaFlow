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

注意：FastAPI 的 router prefix 不能携带路径参数，因此本路由不设 prefix，
路由内部直接书写完整路径。
"""

from typing import AsyncGenerator, List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.core.sse import sse_event, sse_stream
from app.models import Script, User
from app.providers.llm import chat_stream
from app.schemas.script import (
    ExtractConfirmOut,
    ExtractConfirmRequest,
    ExtractResultOut,
    ScriptInputRequest,
    ScriptOut,
    ScriptSaveRequest,
)
from app.services.script_service import (
    build_fallback_script,
    build_generate_messages,
    get_owned_project,
    get_script_or_404,
    parse_script_json,
    run_health_check,
    confirm_extract,
    extract_entities,
    wrap_content_fields,
)

router = APIRouter(tags=["script"])


@router.get("/api/projects/{project_id}/script", response_model=ScriptOut)
async def get_script(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScriptOut:
    """取项目最新（唯一）剧本行；尚无剧本时返回 404。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ScriptOut: 剧本信息（content/raw_text/fences/health_report/version/updated_at）。

    Raises:
        HTTPException: 404 —— 项目不存在或剧本未创建。
    """
    project = await get_owned_project(db, project_id, user.id)
    script = await get_script_or_404(db, project)
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
        req: 输入请求（mode: idea/novel/outline，raw_text 非空）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ScriptOut: 保存后的剧本信息。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目。
    """
    project = await get_owned_project(db, project_id, user.id)
    script = (
        await db.execute(select(Script).where(Script.project_id == project.id))
    ).scalar_one_or_none()
    if script is None:
        # 首次保存：新建剧本行，content 仅记录输入模式
        script = Script(
            project_id=project.id,
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
        user: 当前用户。
        db: 请求级异步数据库会话（SSE 流期间保持有效）。

    Returns:
        StreamingResponse: SSE 流式响应。

    Raises:
        HTTPException: 400 —— 剧本无原始输入文本；404 —— 项目或剧本不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    script = await get_script_or_404(db, project)
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
        req: 保存请求（content 必填，health_report 可选）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ScriptOut: 保存后的剧本信息。

    Raises:
        HTTPException: 404 —— 项目不存在或剧本未创建。
    """
    project = await get_owned_project(db, project_id, user.id)
    script = await get_script_or_404(db, project)
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
