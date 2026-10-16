"""剧本工作流路由模块（文件级说明）。

实现剧本域全部接口（每个接口先校验项目归属，他人或已删除项目一律 404，
不泄露存在性）：
- GET    /api/projects/{project_id}/script                  取最新剧本行
- PUT    /api/projects/{project_id}/script/input            保存原始输入（idea/novel/outline）
- POST   /api/projects/{project_id}/script/generate/stream  SSE 流式生成剧本（POST 方法）：
     结构校验（PRD S-03）+ 失败带错误摘要自动重试（最多 3 轮）+ 进程内生成锁
     （同项目并发 409）+ 乐观锁落库（version 不匹配不发 done）
- PUT    /api/projects/{project_id}/script                  手动保存（RD-03：
     场次级 source 差异化合并 + 顶层整体 source=user，可选 base_version 乐观锁）
- POST   /api/projects/{project_id}/script/health           规则体检（RD-02：
     四条决议规则 + 时长/语速校验，可选 episode_id 按集/聚合体检）
- POST   /api/projects/{project_id}/script/extract          实体抽取（规则 + LLM 双路）
- POST   /api/projects/{project_id}/script/extract/confirm  抽取确认入池（建资产行）
- POST   /api/projects/{project_id}/script/send-to-agent    剧本一键同步 Agent 会话（S-08，幂等）
- POST   /api/projects/{project_id}/script/send-to-canvas   剧本一键投递画布（S-08，幂等）
- POST   /api/projects/{project_id}/script/input/novel/parse      小说章节解析（P-7，txt/epub）
- GET    /api/projects/{project_id}/script/input/novel/chapters   小说章节暂存列表（P-7）

注意：FastAPI 的 router prefix 不能携带路径参数，因此本路由不设 prefix，
路由内部直接书写完整路径。
"""

import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm_adapter import get_last_usage, llm
from app.core.database import get_db
from app.core.security import get_current_user
from app.core.sse import sse_event, sse_stream
from app.models import (
    AgentThread,
    Canvas,
    CanvasNode,
    Character,
    Project,
    Prop,
    Scene,
    Script,
    User,
)
from app.schemas.script import (
    EntityImpactOut,
    ExtractConfirmOut,
    ExtractConfirmRequest,
    ExtractResultOut,
    NovelChaptersOut,
    NovelChapterOut,
    NovelParseOut,
    ScriptInputRequest,
    ScriptOut,
    ScriptSaveRequest,
    ScriptSendToAgentOut,
    ScriptSendToAgentRequest,
    ScriptSendToCanvasOut,
    ScriptSendToCanvasRequest,
    validate_script_content,
)
from app.services.novel_parser import parse_novel, summarize_chapters
from app.services.script_export_service import (
    collect_entity_impact,
    collect_export_episodes,
    render_fountain,
    render_txt,
)
from app.services.script_service import (
    acquire_generate_lock,
    build_fallback_script,
    build_generate_messages,
    build_novel_context,
    confirm_extract,
    content_has_payload,
    content_plain,
    extract_entities,
    get_owned_project,
    get_script_or_404,
    get_script_scoped_or_404,
    merge_scene_sources,
    parse_script_json,
    release_generate_lock,
    run_project_health_check,
    validate_episode,
    wrap_content_fields,
)

router = APIRouter(tags=["script"])

# 剧本生成最多流式尝试轮数（每轮输出校验失败则携带错误摘要重试，PRD S-03）
MAX_GENERATE_ATTEMPTS = 3

# ---------------------------------------------------------------------------
# 小说改编链路常量（P-1 Task 9 / P-7）
# ---------------------------------------------------------------------------
# 上传大小上限（字节，20MB）：超出返回 413
_NOVEL_MAX_BYTES = 20 * 1024 * 1024
# 章节暂存在 project.config 中的键（与 script_characters 同模式）
_NOVEL_CHAPTERS_KEY = "novel_chapters"
# 章节正文入库预览上限（字）：完整正文体积过大不入库（决策见 parse 接口注释）
_NOVEL_CONTENT_PREVIEW = 2000
# raw_text 直存全文的字数上限，超出则改用「章节摘要拼接」
_NOVEL_RAW_TEXT_MAX = 20000


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
    - `delta`：{"text": 增量文本}（LLM 增量原样透传，最多 3 轮流式尝试）；
    - `error`：两种场景——① LLM 完全异常（连接失败）时「模型不可用，已使用
       离线降级生成」，降级为基于 raw_text 拆句的确定性 3 幕占位剧本后仍会
       发出 done；② 3 轮输出均未通过结构校验时「剧本 JSON 校验失败（已重试
       3 次）…」，此时不落库、不发 done、版本不加；③ 乐观锁冲突时「剧本已被
       其他操作修改，请刷新后重试」，同样不落库；
    - `done`：{"script_id", "version", "content", "usage"}（content 每个一级
       字段已按 RD-03 包装 {"value":…, "source":"ai"}；version+1；usage 为
       本次生成的 LLM 用量归因记录，无记录时为 null）。

    并发保护：同项目进程内生成锁（并发第二个请求 409）；落库采用乐观锁
    UPDATE ... WHERE version=流开始时的版本，冲突不发 done。

    Args:
        project_id: 项目 ID。
        episode_id: 可选剧集 ID（S-06；未指定时作用于「未绑定剧集」的默认
            剧本行，与既有行为一致；传入时按该剧集定位剧本行）。
        user: 当前用户。
        db: 请求级异步数据库会话（SSE 流期间保持有效）。

    Returns:
        StreamingResponse: SSE 流式响应。

    Raises:
        HTTPException: 400 —— 剧本无原始输入文本；404 —— 项目/剧集或剧本不存在；
            409 —— 同项目已有生成流进行中。
    """
    project = await get_owned_project(db, project_id, user.id)
    if episode_id is not None:
        await validate_episode(db, project, episode_id)
    script = await get_script_scoped_or_404(db, project, episode_id)
    if not (script.raw_text or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="剧本无原始输入文本，请先保存剧本输入")
    # 输入源（P-7 长文截断消除）：novel 模式且已解析章节时改用「章节摘要 +
    # 正文节选」拼接上下文（build_novel_context），不再走 raw_text[:4000]；
    # 其余模式沿用 raw_text 与既有 4000 字截断。
    plain_input = content_plain(script.content)
    if str(plain_input.get("input_mode") or "") == "novel" and _load_novel_chapters(project):
        messages = build_generate_messages(
            project, build_novel_context(project, fallback_text=script.raw_text),
            max_chars=12000)
    else:
        messages = build_generate_messages(project, script.raw_text)
    # 并发幂等保护（单进程语义）：流开始前占锁，同项目并发第二个请求 409；
    # 锁在 event_generator 的 finally 中释放（响应体发送完毕后才执行）
    if not acquire_generate_lock(project.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="正在生成中，请稍候")
    # 流期间需要用到的剧本属性提前取为局部变量，避免生成器阶段访问已被
    # 乐观锁 UPDATE 过期的 ORM 属性（MissingGreenlet 风险）
    script_id = script.id
    fallback_raw = script.raw_text or ""
    fallback_target = project.target_duration
    start_version = script.version

    async def event_generator() -> AsyncGenerator[str, None]:
        """SSE 事件生成器：多轮流式尝试 → 每轮完整收集后结构校验 → 落库 → done。

        重试链（PRD S-03）：每轮流式透传 delta；轮末校验输出（parse_script_json
        双重把关），失败则把「上一轮原始输出（assistant）+ 校验错误摘要 + 纠错
        指令（user）」追加进 messages 再来一轮；3 轮耗尽仅发 error（不落库、
        不发 done、版本不加）。LLM 完全异常（连接失败）走 build_fallback_script
        离线降级（fallback 结构可稳定通过 ScriptContent 校验）+ error + done。

        Yields:
            str: sse_event 格式化的事件帧。

        Raises:
            无：LLM 异常在内部捕获并走离线降级；校验耗尽与乐观锁冲突以
                error 事件表达，均不向上抛出。
        """
        try:
            current_messages = list(messages)
            parsed: Optional[Dict[str, Any]] = None
            for attempt in range(1, MAX_GENERATE_ATTEMPTS + 1):
                collected: List[str] = []
                degraded = False
                try:
                    # 统一 LLM 出入口：经 llm_adapter 单例调用（带用量观测）
                    async for delta in llm.chat_stream(None, current_messages, agent_name="script"):
                        collected.append(delta)
                        yield sse_event("delta", {"text": delta})  # 模型增量原样透传
                except Exception:  # noqa: BLE001 —— LLM 不可用时必须走离线降级
                    degraded = True

                raw_full = "".join(collected)
                if degraded or not raw_full.strip():
                    # 离线降级：基于 raw_text 拆句构造确定性 3 幕占位剧本（不依赖模型；
                    # 时长按项目目标时长均分，保证 fallback 可过时长体检 ±10%）
                    parsed = build_fallback_script(fallback_raw, fallback_target)
                    yield sse_event("error", {"message": "模型不可用，已使用离线降级生成"})
                    break

                # 每轮完整收集后双重校验：JSON 解析 + ScriptContent 结构校验
                parsed, err = parse_script_json(raw_full)
                if parsed is not None:
                    break
                if attempt < MAX_GENERATE_ATTEMPTS:
                    # 纠错重试：把上一轮原始输出与校验错误摘要追加进消息再试一轮
                    current_messages = current_messages + [
                        {"role": "assistant", "content": raw_full},
                        {"role": "user", "content": (
                            f"你上一轮输出的剧本 JSON 未通过结构校验：{err}\n"
                            "请修正上述问题后，重新仅输出一个严格合法的完整剧本 JSON"
                            "（禁止 markdown 代码块围栏与任何解释文字）。"
                        )},
                    ]

            if parsed is None:
                # 3 轮耗尽：不落库、不发 done、版本不加
                yield sse_event("error", {"message": "剧本 JSON 校验失败（已重试 3 次），请重试或精简输入"})
                return

            # RD-03 字段级来源标记：每个一级字段包装 {"value":…, "source":"ai"}
            new_content = wrap_content_fields(parsed, "ai")
            new_version = start_version + 1
            # 乐观锁落库：仅当版本仍为流开始时的快照值才允许写入
            lock_result = await db.execute(
                update(Script)
                .where(Script.id == script_id, Script.version == start_version)
                .values(content=new_content, version=new_version)
            )
            if lock_result.rowcount == 0:
                # 版本已被其他操作推进：放弃本次写入
                await db.rollback()
                yield sse_event("error", {"message": "剧本已被其他操作修改，请刷新后重试"})
                return
            project.status = "scripting"
            await db.commit()
            # 用量归因：读取最近一次 LLM 调用的用量记录（mock/离线路径为 null）
            usage = get_last_usage()
            yield sse_event("done", {
                "script_id": script_id,
                "version": new_version,
                "content": new_content,
                "usage": usage,
            })
        finally:
            # 释放进程内生成锁（生成器耗尽或客户端断连触发 aclose 时均会执行）
            release_generate_lock(project.id)

    return await sse_stream(event_generator())


@router.put("/api/projects/{project_id}/script", response_model=ScriptOut)
async def save_script(
    project_id: int,
    req: ScriptSaveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScriptOut:
    """手动保存剧本：场次级 source 差异化合并 + 顶层整体包装 source=user
    （RD-03），version += 1；可选回写 health_report。

    来源标记（P-1 Task 8 场次级 source 补齐）：读取 DB 旧 content 与请求新
    content 的 scenes 逐场比对（复用 merge_scene_sources：scene_no+title 匹配
    + 内容指纹）——改动/新增场次标 "user"、未改动场次保留旧 source（无旧数据
    时全 "ai"）；顶层 title/logline/outline 属用户直接编辑区，仍按「整体 user」
    包装（保持原语义）。content_plain 可正常解包该形态。

    并发保护：req.base_version 传入时做乐观锁校验——与剧本当前 version 不一致
    返回 409（剧本已被其他操作修改）；未传保持旧行为（前端兼容）。

    Args:
        project_id: 项目 ID。
        req: 保存请求（content 必填，health_report/base_version/episode_id 可选；
            episode_id 传入时按该剧集定位剧本行，缺省定位默认剧本行）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ScriptOut: 保存后的剧本信息。

    Raises:
        HTTPException: 404 —— 项目不存在、剧集不存在或对应范围剧本未创建；
            409 —— base_version 与剧本当前版本不一致。
    """
    project = await get_owned_project(db, project_id, user.id)
    if req.episode_id is not None:
        await validate_episode(db, project, req.episode_id)
    script = await get_script_scoped_or_404(db, project, req.episode_id)
    # 可选乐观锁：仅当调用方显式传入 base_version 时校验（兼容未传的旧客户端）
    if req.base_version is not None and req.base_version != script.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="剧本已被其他操作修改，请刷新")
    # 场次级 source 合并（改动场次 user、未动场次保留旧 source、首次全 ai）
    # 后再整体包装为 user 来源落库（RD-03）
    merged = merge_scene_sources(script.content, req.content)
    script.content = {"value": merged, "source": "user"}
    script.version += 1
    if req.health_report is not None:
        script.health_report = req.health_report
    await db.commit()
    await db.refresh(script)
    return ScriptOut.model_validate(script)


@router.post("/api/projects/{project_id}/script/health")
async def script_health(
    project_id: int,
    episode_id: Optional[int] = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """剧本规则体检（RD-02），结果落库 health_report 并返回。

    体检规则（P-1 Task 8 决议四条 + 时长/语速校验，详见
    script_service.run_health_check 注释）：
    角色出场断层 / 对白过长 / 缺动作描写 / 缺高潮点 + 时长偏差>10% +
    对白语速交叉校验。

    范围（P-1 Task 8 按集体检）：
    - episode_id 传值：仅体检该集（script 按集定位，角色清单取项目已确认角色）；
    - 不传：聚合体检项目下全部集（各集分别体检 + 汇总 issues，location 带
      「第N集 · 」前缀）；无集级剧本行（快速模式）时按单剧本逻辑体检默认行。

    落库：报告的 health_report 写到对应剧本行（指定集→该集行；聚合/快速模式
    →默认剧本行，若不存在则仅返回不落库）。

    Args:
        project_id: 项目 ID。
        episode_id: 可选剧集 ID（S-06；缺省聚合体检全部集/单剧本）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"score": 0~100, "issues": [{"level","message","location"}]}。

    Raises:
        HTTPException: 404 —— 项目或剧本不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    report = await run_project_health_check(db, project, episode_id)
    # 报告落库到目标剧本行（best-effort：行不存在时仅返回不落库）
    target_script = (
        await db.execute(
            select(Script).where(
                Script.project_id == project.id,
                Script.episode_id.is_(None) if episode_id is None
                else Script.episode_id == episode_id,
            )
        )
    ).scalar_one_or_none()
    if target_script is not None:
        target_script.health_report = report
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
# RD-08 影响面预览 + 剧本导出（P-1 范式级重构 Task 11）
# ---------------------------------------------------------------------------
# 合法实体类型 → 资产模型映射（影响面预览按类型定位实体行）
_IMPACT_ENTITY_MODELS: Dict[str, Any] = {
    "character": Character,
    "scene": Scene,
    "prop": Prop,
}
# 合法导出格式（txt 纯文本 / fountain 结构化剧本）
_EXPORT_FORMATS = ("txt", "fountain")


@router.get("/api/projects/{project_id}/script/entities/impact",
            response_model=EntityImpactOut)
async def script_entity_impact(
    project_id: int,
    entity_type: str = Query(..., description="实体类型：character/scene/prop"),
    entity_id: int = Query(..., description="实体资产 ID"),
    episode_id: Optional[int] = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EntityImpactOut:
    """实体删除影响面预览（RD-08）：统计实体在剧本与分镜中的引用情况。

    与「抽取确认后写入 version_snapshots.impact_scope 的事后快照」互补——
    本接口为**修改前只读预览**，仅即时计算返回，不落任何库表。

    统计口径（详见 services/script_export_service.collect_entity_impact）：
    - script_refs：剧本 scenes 中实体名出现次数（title/plot/action +
      dialogue.character/dialogue.line）；
    - storyboard_refs：分镜「文本匹配（dialogue/visual_description/
      prompt_locked/prompt_editable）∪ 结构化引用（角色→关联表、场景→
      scene_id）」去重后的条数。

    Args:
        project_id: 项目 ID。
        entity_type: 实体类型（character/scene/prop；非法值 400）。
        entity_id: 实体资产 ID（须属于该项目且未软删，否则 404）。
        episode_id: 可选剧集 ID（传入时剧本与分镜统计均限定该集）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        EntityImpactOut: 影响面预览（script_refs/storyboard_refs/
            script_locations/storyboard_ids/summary）。

    Raises:
        HTTPException: 400 —— entity_type 非法；404 —— 项目不存在/非本人、
            实体不存在/已软删/不属于该项目，或剧集不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    if entity_type not in _IMPACT_ENTITY_MODELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="entity_type 必须为 character/scene/prop")
    if episode_id is not None:
        await validate_episode(db, project, episode_id)
    # 实体须属于该项目且未软删（统一 404，不泄露存在性）
    entity = await db.get(_IMPACT_ENTITY_MODELS[entity_type], entity_id)
    if (entity is None or entity.project_id != project.id
            or entity.deleted_at is not None):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="实体不存在")
    impact = await collect_entity_impact(
        db, project, entity_type, entity_id, entity.name, episode_id)
    return EntityImpactOut(
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity.name,
        **impact,
    )


@router.get("/api/projects/{project_id}/script/export")
async def script_export(
    project_id: int,
    fmt: str = Query(..., alias="format", description="导出格式：txt/fountain"),
    episode_id: Optional[int] = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """剧本导出（RD-08 关联交互缺陷 4）：TXT / Fountain 两种格式下载。

    数据来源：指定 episode_id 时导出该集剧本行；缺省导出项目下全部集，
    按 episode.sequence 排序（未绑定剧集的默认剧本行作为快速模式单篇排在
    最后）。无任何正文内容时返回 404。

    - TXT：多集以「===== 第N集 · 标题 =====」分隔；每集正文优先 raw_text，
      为空回退结构化 scenes 渲染；
    - Fountain：按规范结构化渲染（场景标题 INT./EXT.、角色行大写、对白紧随、
      多集以 `# ` Section 分节），渲染规则见 render_fountain docstring。

    Args:
        project_id: 项目 ID。
        fmt: 导出格式（txt/fountain；非法值 400）。
        episode_id: 可选剧集 ID（缺省导出全部集）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        Response: 文本响应体，Content-Disposition 附件（文件名经 UTF-8 URL
            编码：项目标题 + .txt/.fountain）。

    Raises:
        HTTPException: 400 —— format 非法；404 —— 项目不存在/非本人、剧集
            不存在，或暂无剧本内容可导出。
    """
    project = await get_owned_project(db, project_id, user.id)
    if fmt not in _EXPORT_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="format 必须为 txt 或 fountain")
    if episode_id is not None:
        await validate_episode(db, project, episode_id)
    episodes = await collect_export_episodes(db, project, episode_id)
    if not episodes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="暂无剧本内容可导出")
    if fmt == "fountain":
        body = render_fountain(episodes)
        extension = "fountain"
    else:
        body = render_txt(episodes)
        extension = "txt"
    base = (project.title or "").strip() or "script"
    filename = f"{base}.{extension}"
    # RFC 5987：中文文件名以 UTF-8 百分号编码写入 filename*
    disposition = f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )


# ---------------------------------------------------------------------------
# S-08 剧本一键同步（Agent 会话 / 画布投递，均幂等）
# ---------------------------------------------------------------------------
# 说明：content 解包工具（content_plain / content_has_payload）已迁移至
# app.services.script_service（公开函数），此处自 service 导入使用。

def _collect_dialogue_characters(plain: Dict[str, Any]) -> List[str]:
    """从剧本结构中收集出现过的角色名（内部函数，去重保序，限 10 个）。

    Args:
        plain: content_plain 的解包结果。

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
        plain: content_plain 的解包结果。
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
    plain = content_plain(script.content)
    if not (script.raw_text or "").strip() and not content_has_payload(plain):
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


# ---------------------------------------------------------------------------
# 小说改编链路（P-1 Task 9 / P-7）：章节解析 + 暂存列表
# ---------------------------------------------------------------------------
def _load_novel_chapters(project: Project) -> List[dict]:
    """从 project.config 读取暂存的小说章节列表（内部函数，结构容错）。

    Args:
        project: 项目 ORM 对象。

    Returns:
        List[dict]: 章节条目列表（config 缺失/非列表时返回空列表；
            元素结构为 {"index","title","char_count","summary","content_preview"}）。

    Raises:
        无：结构异常按空列表处理。
    """
    raw = (project.config or {}).get(_NOVEL_CHAPTERS_KEY)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _store_novel_chapters(project: Project, chapters: List[dict],
                          summaries: List[dict]) -> None:
    """把解析结果写入 project.config（内部函数，触发 JSONB 变更检测）。

    存储决策：**完整正文不入库**（长篇小说正文可达数 MB，落 JSONB 会撑大
    projects 行、拖慢任何读项目的查询）。这里只存章节元信息 + 摘要 + 正文
    前 2000 字预览（content_preview）：下游生成/抽取需要的长上下文由
    build_novel_context 用摘要 + 预览拼装；完整正文由前端持有，需要时重传。

    Args:
        project: 项目 ORM 对象（就地更新其 config，整体重新赋值触发变更检测）。
        chapters: 解析出的章节列表（含 content）。
        summaries: summarize_chapters 输出的摘要列表（与 chapters 等长）。

    Returns:
        None。

    Raises:
        无：纯字典构造与赋值。
    """
    stored: List[dict] = []
    for chapter, summary in zip(chapters, summaries):
        content = str(chapter.get("content") or "")
        stored.append({
            "index": int(chapter.get("index") or len(stored) + 1),
            "title": str(chapter.get("title") or "").strip(),
            "char_count": len(content),
            "summary": str(summary.get("summary") or ""),
            "content_preview": content[:_NOVEL_CONTENT_PREVIEW],
        })
    project.config = {**(project.config or {}), _NOVEL_CHAPTERS_KEY: stored}


def _novel_chapter_outs(chapters: List[dict]) -> List[NovelChapterOut]:
    """把暂存章节 dict 转为输出模型列表（内部函数）。

    Args:
        chapters: config 中的章节条目列表。

    Returns:
        List[NovelChapterOut]: 章节输出条目列表（字段缺失按 0/空串兜底）。

    Raises:
        无：结构异常按缺字段兜底（Pydantic 校验）。
    """
    return [
        NovelChapterOut(
            index=int(chapter.get("index") or idx + 1),
            title=str(chapter.get("title") or ""),
            char_count=int(chapter.get("char_count") or 0),
            summary=str(chapter.get("summary") or ""),
        )
        for idx, chapter in enumerate(chapters)
    ]


async def _sync_novel_raw_text(db: AsyncSession, project: Project,
                               chapters: List[dict]) -> None:
    """把解析出的全文（或章节摘要拼接）回写剧本行 raw_text（内部函数）。

    全文长度 ≤ _NOVEL_RAW_TEXT_MAX 时直存拼接全文；超长时改用「章节摘要
    拼接」避免 raw_text 无界膨胀。同时置 content["input_mode"]="novel"
    （与 PUT /script/input 的 novel 模式语义一致）。

    Args:
        db: 请求级异步数据库会话。
        project: 已校验归属的项目。
        chapters: 解析出的章节列表（含 content）。

    Returns:
        None。

    Raises:
        无：数据库异常向上传播由调用方处理。
    """
    full = "\n\n".join(str(chapter.get("content") or "") for chapter in chapters).strip()
    if len(full) > _NOVEL_RAW_TEXT_MAX:
        full = "\n".join(
            f"第{chapter.get('index')}章 {chapter.get('title')}："
            f"{str(chapter.get('content') or '')[:200]}"
            for chapter in chapters
        )
    script = (
        await db.execute(
            select(Script).where(
                Script.project_id == project.id, Script.episode_id.is_(None))
        )
    ).scalar_one_or_none()
    if script is None:
        db.add(Script(
            project_id=project.id,
            content={"input_mode": "novel"},
            raw_text=full,
            version=1,
        ))
    else:
        script.raw_text = full
        merged = dict(script.content or {})
        merged["input_mode"] = "novel"
        script.content = merged


async def _read_novel_payload(request: Request) -> tuple:
    """读取小说上传内容，双支持 multipart 文件与 JSON 文本（内部函数）。

    优先识别 multipart/form-data（表单字段名 file，适合 epub 二进制）；否则
    按 JSON 读取 {raw_text, filename?}（适合直接粘贴 txt 文本）。统一做
    20MB 大小限制与空内容校验。

    Args:
        request: FastAPI 请求对象（用于按 content-type 分派读取）。

    Returns:
        tuple: (filename, data)，文件名与文件二进制内容。

    Raises:
        HTTPException: 400 —— 缺文件/空内容/JSON 非法；413 —— 超过 20MB 上限。
    """
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="缺少上传文件（表单字段名应为 file）")
        data = await upload.read()
        filename = (getattr(upload, "filename", None) or "untitled.txt").strip()
    else:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 —— 非法 JSON 统一转 400
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="请求体需为 multipart 文件或 JSON {raw_text, filename}")
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="请求体需为 JSON 对象")
        raw_text = str(body.get("raw_text") or "")
        if not raw_text.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="raw_text 不能为空")
        data = raw_text.encode("utf-8")
        filename = str(body.get("filename") or "untitled.txt").strip()
    if len(data) > _NOVEL_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="文件过大（上限 20MB）")
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="文件内容为空")
    return filename, data


@router.post("/api/projects/{project_id}/script/input/novel/parse",
             response_model=NovelParseOut)
async def parse_novel_chapters(
    project_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NovelParseOut:
    """解析小说章节（txt/epub）并暂存到 project.config["novel_chapters"]。

    流程：归属校验 → 读取上传内容（multipart 文件或 JSON 文本，20MB 上限）
    → parse_novel 分派解析（.txt 正则切分 / .epub 纯标准库解析；非法格式
    400）→ summarize_chapters 生成摘要 → 章节元信息 + 摘要 + 正文前 2000 字
    预览落 config（完整正文不入库，决策见 _store_novel_chapters）→ 回写剧本
    行 raw_text 为拼接全文（超长改用章节摘要拼接）+ input_mode=novel → 提交。

    Args:
        project_id: 项目 ID。
        request: 请求对象（按 content-type 分派 multipart / JSON 读取）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        NovelParseOut: {"project_id", "chapter_count", "chapters", "phase"}。

    Raises:
        HTTPException: 400 —— 格式不受支持/无章节/请求体非法；404 —— 项目
            不存在或非本人项目；413 —— 文件超过 20MB。
    """
    project = await get_owned_project(db, project_id, user.id)
    filename, data = await _read_novel_payload(request)
    try:
        chapters = parse_novel(filename, data)
    except ValueError as exc:
        # 非法扩展名 / epub 损坏 / 文本解码失败统一转 400
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    summaries = summarize_chapters(chapters)
    _store_novel_chapters(project, chapters, summaries)
    await _sync_novel_raw_text(db, project, chapters)
    await db.commit()
    stored = _load_novel_chapters(project)
    return NovelParseOut(
        project_id=project.id,
        chapter_count=len(stored),
        chapters=_novel_chapter_outs(stored),
        phase=project.script_phase,
    )


@router.get("/api/projects/{project_id}/script/input/novel/chapters",
            response_model=NovelChaptersOut)
async def get_novel_chapters(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NovelChaptersOut:
    """读取暂存的小说章节列表（未解析时返回空列表，不 404）。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        NovelChaptersOut: {"project_id", "chapter_count", "chapters", "phase"}。

    Raises:
        HTTPException: 404 —— 项目不存在或非本人项目。
    """
    project = await get_owned_project(db, project_id, user.id)
    chapters = _load_novel_chapters(project)
    return NovelChaptersOut(
        project_id=project.id,
        chapter_count=len(chapters),
        chapters=_novel_chapter_outs(chapters),
        phase=project.script_phase,
    )
