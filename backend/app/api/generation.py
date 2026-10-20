"""生成任务路由模块（文件级说明）。

实现生成任务创建（打样/全量）/ 状态查询 / SSE 进度推送 / 重试与取消：
- POST /api/projects/{project_id}/storyboards/{storyboard_id}/keyframes  关键帧（trial 打样 / full 全量）
- POST /api/projects/{project_id}/storyboards/{storyboard_id}/end-frame  尾帧图像生成（P-03/P-08）
- POST /api/projects/{project_id}/storyboards/{storyboard_id}/video      视频片段
- POST /api/projects/{project_id}/storyboards/{storyboard_id}/tts        配音
- GET  /api/projects/{project_id}/tasks                                  任务分页列表
- GET  /api/tasks/{task_id}                                              任务详情
- POST /api/tasks/{task_id}/replace                                      生成结果上传替换（P-06）
- POST /api/tasks/{task_id}/retry                                        失败任务重试
- POST /api/tasks/{task_id}/cancel                                       取消任务
- GET  /api/projects/{project_id}/tasks/stream                           SSE 进度推送

注意：路由需携带 {project_id} 路径参数，FastAPI router prefix 不支持路径参数，
因此本路由不设 prefix，路由内部书写完整路径。
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.core.sse import sse_event, sse_stream
from app.models import Character, Project, Storyboard, Task, User, Wallet
from app.schemas.common import PageResult
from app.schemas.generation import (
    KeyframeCreateRequest,
    KeyframeCreateResponse,
    TaskOut,
    TaskProgressEvent,
    TaskProgressItem,
    TtsCreateRequest,
    VideoCreateRequest,
    VoicePreviewCreateRequest,
)
from app.services import normalize_pagination
from app.services.generation_service import (
    create_end_frame_task,
    create_keyframe_tasks,
    create_retry_task,
    create_tts_task,
    create_video_task,
    create_voice_preview_task,
    get_owned_project,
    get_storyboard_of_project,
)
from app.services.billing_service import refund_frozen
from app.services.media_service import save_replace_file
from app.tasks.runner import CANCEL_EVENTS, request_cancel

router = APIRouter(tags=["generation"])

# SSE 推送的最长持续时间（秒）
_SSE_MAX_SECONDS = 600
# SSE 每次轮询间隔（秒）
_SSE_INTERVAL = 1.0
# SSE 单次推送的最大任务摘要条数
_SSE_MAX_TASKS = 200


async def _get_owned_task(db: AsyncSession, user_id: int, task_id: int) -> Task:
    """校验任务存在且归属当前用户（经项目归属链校验）。

    Args:
        db: 请求级异步数据库会话。
        user_id: 当前用户 ID。
        task_id: 任务 ID。

    Returns:
        Task: 归属校验通过的任务 ORM 对象。

    Raises:
        HTTPException: 404 —— 任务不存在；403 —— 任务所属项目不属于当前用户。
    """
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if task.project_id is not None:
        project = await db.get(Project, task.project_id)
        if project is None or project.user_id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该任务")
    return task


@router.post(
    "/api/projects/{project_id}/storyboards/{storyboard_id}/keyframes",
    response_model=KeyframeCreateResponse,
)
async def create_keyframes(
    project_id: int,
    storyboard_id: int,
    req: KeyframeCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KeyframeCreateResponse:
    """创建关键帧生成任务（trial 打样：单分镜；full 全量：项目全部分镜）。

    幂等防重：任一涉及分镜已存在排队/进行中的同阶段 image 任务时整体 409。

    Args:
        project_id: 项目 ID。
        storyboard_id: 分镜 ID（trial 的目标分镜）。
        req: 请求体（phase / prompt_editable）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        KeyframeCreateResponse: 新建任务列表与冻结积分总额。

    Raises:
        HTTPException: 400 —— 全量时项目无分镜；403/404 —— 归属校验失败；
            409 —— 防重复提交或积分余额不足。
    """
    project = await get_owned_project(db, user.id, project_id)
    storyboard = await get_storyboard_of_project(db, project_id, storyboard_id)
    tasks, total_cost = await create_keyframe_tasks(
        db, project=project, storyboard=storyboard, phase=req.phase, prompt_editable=req.prompt_editable,
        scope_episode_id=req.episode_id,
    )
    return KeyframeCreateResponse(tasks=[TaskOut.from_task(t) for t in tasks], total_cost=total_cost)


@router.post("/api/projects/{project_id}/storyboards/{storyboard_id}/end-frame", response_model=TaskOut)
async def create_storyboard_end_frame(
    project_id: int,
    storyboard_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskOut:
    """创建分镜尾帧图像生成任务（P-03/P-08：成本 10 积分/张，meta 标记 end_frame）。

    任务成功后由执行器回写 storyboards.end_frame_url；生成前须确保该镜
    关键帧已就绪（尾帧以关键帧画面为基准延续生成）。

    Args:
        project_id: 项目 ID。
        storyboard_id: 分镜 ID。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        TaskOut: 新建的尾帧图像任务。

    Raises:
        HTTPException: 400 —— 分镜尚无关键帧，无法生成尾帧；
            403/404 —— 归属校验失败；409 —— 同分镜已有进行中的尾帧任务或积分不足。
    """
    project = await get_owned_project(db, user.id, project_id)
    storyboard = await get_storyboard_of_project(db, project_id, storyboard_id)
    if not storyboard.keyframe_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该分镜尚未生成关键帧，请先生成关键帧再生成尾帧",
        )
    task = await create_end_frame_task(db, project=project, storyboard=storyboard)
    return TaskOut.from_task(task)


@router.post("/api/projects/{project_id}/storyboards/{storyboard_id}/video", response_model=TaskOut)
async def create_storyboard_video(
    project_id: int,
    storyboard_id: int,
    req: VideoCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskOut:
    """创建视频片段生成任务（成本 = 每秒单价 × 分镜时长）。

    Args:
        project_id: 项目 ID。
        storyboard_id: 分镜 ID。
        req: 请求体（use_end_frame）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        TaskOut: 新建的视频任务。

    Raises:
        HTTPException: 400 —— 使用尾帧但该分镜尾帧尚未生成；
            403/404 —— 归属校验失败；409 —— 积分余额不足。
    """
    project = await get_owned_project(db, user.id, project_id)
    storyboard = await get_storyboard_of_project(db, project_id, storyboard_id)
    task = await create_video_task(
        db,
        project=project,
        storyboard=storyboard,
        use_end_frame=req.use_end_frame,
        resolution=req.resolution,
        fps=req.fps,
        motion_strength=req.motion_strength,
    )
    return TaskOut.from_task(task)


@router.post("/api/projects/{project_id}/storyboards/{storyboard_id}/tts", response_model=TaskOut)
async def create_storyboard_tts(
    project_id: int,
    storyboard_id: int,
    req: TtsCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskOut:
    """创建配音（TTS）生成任务（成本 = ceil(字数/100)×5，文本取对白，空则画面描述）。

    Args:
        project_id: 项目 ID。
        storyboard_id: 分镜 ID。
        req: 请求体（voice / speed / pitch）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        TaskOut: 新建的配音任务。

    Raises:
        HTTPException: 400 —— 无可配音文本；403/404 —— 归属校验失败；409 —— 积分不足。
    """
    project = await get_owned_project(db, user.id, project_id)
    storyboard = await get_storyboard_of_project(db, project_id, storyboard_id)
    task = await create_tts_task(
        db, project=project, storyboard=storyboard, voice=req.voice, speed=req.speed,
        pitch=req.pitch, segment_index=req.segment_index,
    )
    return TaskOut.from_task(task)


@router.post(
    "/api/projects/{project_id}/characters/{character_id}/voice-preview",
    response_model=TaskOut,
)
async def create_character_voice_preview(
    project_id: int,
    character_id: int,
    req: VoicePreviewCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskOut:
    """创建角色音色试音任务（「我是XX，这是我的配音。」一句试音，5 积分）。

    音色：req.voice 显式传入（映射表下拉当前选择）优先，否则按角色
    voice_note + gender 自动路由。产物回写 characters.voice_sample_url。

    Args:
        project_id: 项目 ID。
        character_id: 角色 ID。
        req: 请求体（voice 可空=自动路由）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        TaskOut: 新建的试音任务。

    Raises:
        HTTPException: 403/404 —— 归属校验失败；409 —— 积分不足。
    """
    project = await get_owned_project(db, user.id, project_id)
    character = await db.get(Character, character_id)
    if character is None or character.project_id != project_id or character.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="角色不存在")
    task = await create_voice_preview_task(
        db, project=project, character=character, voice=req.voice
    )
    return TaskOut.from_task(task)


@router.get("/api/projects/{project_id}/tasks", response_model=PageResult[TaskOut])
async def list_project_tasks(
    project_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    type_filter: Optional[str] = Query(default=None, alias="type"),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    phase_filter: Optional[str] = Query(default=None, alias="phase"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PageResult[TaskOut]:
    """分页查询项目生成任务列表（含进度/结果地址/错误信息，可按类型/状态/阶段过滤）。

    Args:
        project_id: 项目 ID。
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        type_filter: 任务类型过滤（image/video/tts/compose；非法返回 400）。
        status_filter: 状态过滤（queued/running/done/failed/canceled；非法返回 400）。
        phase_filter: 阶段过滤（trial/full/incremental；非法返回 400）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        PageResult[TaskOut]: 任务分页列表（按创建时间倒序）。

    Raises:
        HTTPException: 400 —— 过滤参数非法；403/404 —— 项目归属校验失败。
    """
    allowed_types = ("image", "video", "tts", "compose", "llm", "extract")
    allowed_status = ("queued", "running", "done", "failed", "canceled")
    allowed_phase = ("trial", "full", "incremental")
    for name, value, allowed in (("type", type_filter, allowed_types),
                                 ("status", status_filter, allowed_status),
                                 ("phase", phase_filter, allowed_phase)):
        if value is not None and value not in allowed:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{name} 仅支持 {'/'.join(allowed)}",
            )
    await get_owned_project(db, user.id, project_id)
    page, page_size = normalize_pagination(page, page_size)
    conditions = [Task.project_id == project_id]
    if type_filter:
        conditions.append(Task.type == type_filter)
    if status_filter:
        conditions.append(Task.status == status_filter)
    if phase_filter:
        conditions.append(Task.phase == phase_filter)
    total = await db.scalar(select(func.count()).select_from(Task).where(*conditions))
    rows = await db.execute(
        select(Task).where(*conditions)
        .order_by(Task.created_at.desc(), Task.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return PageResult(
        total=total or 0, page=page, page_size=page_size,
        items=[TaskOut.from_task(t) for t in rows.scalars().all()],
    )


@router.get("/api/tasks/{task_id}", response_model=TaskOut)
async def get_task_detail(
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskOut:
    """查询单个任务详情（含进度/结果地址/错误，经项目归属链校验）。

    Args:
        task_id: 任务 ID。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        TaskOut: 任务详情。

    Raises:
        HTTPException: 404 —— 任务不存在；403 —— 任务所属项目不属于当前用户。
    """
    task = await _get_owned_task(db, user.id, task_id)
    return TaskOut.from_task(task)


@router.post("/api/tasks/{task_id}/replace", response_model=TaskOut)
async def replace_task_result(
    task_id: int,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskOut:
    """上传替换生成结果（P-06）：multipart 一步上传，回写任务结果并联动分镜。

    约束：仅 image 任务且状态 done 可替换；文件为 png/jpg/jpeg/webp 且 ≤10MB；
    落盘 storage/replaced/{yyyy-mm}/ 后回写 task.result_url，并在 meta 留痕
    （replaced_from 原地址 / replaced_at ISO 时间 / replaced_by_upload=True）。
    关键帧任务（meta.target_type=storyboard_keyframe 或 image 任务关联分镜且
    非尾帧任务）同步更新 storyboards.keyframe_url（前端缩略图取同字段）。

    Args:
        task_id: 目标任务 ID。
        file: 上传的替换图片文件。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        TaskOut: 更新后的任务详情（result_url 为新文件地址）。

    Raises:
        HTTPException: 404 —— 任务不存在；403 —— 任务不属于当前用户；
            400 —— 任务类型非 image / 状态非 done / 文件格式或大小非法。
    """
    task = await _get_owned_task(db, user.id, task_id)
    if task.type != "image":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="仅图像生成任务支持上传替换"
        )
    if task.status != "done":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="任务尚未完成，无法替换生成结果"
        )

    new_url, _size = await save_replace_file(file)

    meta = dict(task.meta or {})
    old_url = task.result_url
    meta["replaced_from"] = old_url
    meta["replaced_at"] = datetime.now(timezone.utc).isoformat()
    meta["replaced_by_upload"] = True
    # JSONB 整体重新赋值以触发变更检测
    task.meta = meta
    task.result_url = new_url

    # 联动分镜：关键帧任务回写 keyframe_url（尾帧任务回写 end_frame_url，不混用）
    target_type = meta.get("target_type")
    storyboard_id = meta.get("target_id") or task.storyboard_id
    if storyboard_id:
        try:
            sb_id = int(storyboard_id)
        except (TypeError, ValueError):
            sb_id = None
        if sb_id:
            sb = await db.get(Storyboard, sb_id)
            if sb is not None:
                if target_type == "storyboard_end_frame" or meta.get("end_frame"):
                    sb.end_frame_url = new_url
                else:
                    sb.keyframe_url = new_url
                    sb.status = "done"
    await db.commit()
    return TaskOut.from_task(task)


@router.post("/api/tasks/{task_id}/retry", response_model=TaskOut)
async def retry_task(
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskOut:
    """重试失败任务：仅 failed 状态允许，创建新任务（新幂等键、重新冻结积分）。

    Args:
        task_id: 原任务 ID。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        TaskOut: 新建的重试任务。

    Raises:
        HTTPException: 404/403 —— 归属校验失败；409 —— 任务非 failed 或积分不足。
    """
    task = await _get_owned_task(db, user.id, task_id)
    if task.status != "failed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="仅失败任务可重试")
    new_task = await create_retry_task(db, task)
    return TaskOut.from_task(new_task)


@router.post("/api/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """取消任务：queued 直接落库取消并退款；running 置取消事件，由执行器检测后取消退款。

    Args:
        task_id: 任务 ID。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"message": ..., "status": ...}。

    Raises:
        HTTPException: 404/403 —— 归属校验失败；409 —— 任务已是终态，无法取消。
    """
    task = await _get_owned_task(db, user.id, task_id)
    if task.status not in ("queued", "running"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="任务已结束，无法取消")
    if task.status == "queued":
        # 直接置 canceled（条件更新防止与执行器抢占产生竞态）
        result = await db.execute(
            update(Task).where(Task.id == task.id, Task.status == "queued").values(status="canceled")
        )
        if result.rowcount:
            await refund_frozen(
                db,
                user_id=user.id,
                amount=task.cost_points,
                ref_task_id=task.id,
                idempotency_key=f"task_{task.id}_refund",
                bear="user",
                remark="任务取消（排队中），冻结积分退回",
            )
            CANCEL_EVENTS.pop(task.id, None)
            return {"message": "任务已取消，积分已退回", "status": "canceled"}
        # 条件更新未命中：任务已被执行器捡起置 running，转为执行中取消
        task = await _get_owned_task(db, user.id, task_id)
    # running：置位取消事件，执行器分步检测后落库取消并退款
    request_cancel(task.id)
    return {"message": "取消请求已受理，任务执行中将尽快取消并退回积分", "status": "canceling"}


@router.get("/api/projects/{project_id}/tasks/stream")
async def stream_project_tasks(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """SSE 推送项目任务进度：每秒推送 progress 事件，全部终态后推送 done 事件并结束。

    Args:
        project_id: 项目 ID。
        user: 当前登录用户。
        db: 请求级异步数据库会话（仅用于归属校验，轮询使用独立短会话）。

    Returns:
        StreamingResponse: text/event-stream 流式响应。

    Raises:
        HTTPException: 403/404 —— 项目归属校验失败。
    """
    await get_owned_project(db, user.id, project_id)

    async def _generator():
        """进度轮询生成器：查询任务摘要 → 推送 progress/done 事件。

        Yields:
            str: SSE 文本帧。

        Raises:
            无：轮询异常时推送 done 事件后结束，避免连接悬挂。
        """
        from app.core.database import SessionLocal

        deadline = time.monotonic() + _SSE_MAX_SECONDS
        while True:
            try:
                async with SessionLocal() as poll_db:
                    rows = await poll_db.execute(
                        select(Task.id, Task.type, Task.status, Task.progress)
                        .where(Task.project_id == project_id)
                        .order_by(Task.id.desc())
                        .limit(_SSE_MAX_TASKS)
                    )
                    items = [
                        TaskProgressItem(id=r.id, type=r.type, status=r.status, progress=r.progress)
                        for r in rows.all()
                    ]
                running = sum(1 for i in items if i.status in ("queued", "running"))
                yield sse_event("progress", TaskProgressEvent(tasks=items, running=running).model_dump())
                if running == 0:
                    yield sse_event("done", {"running": 0})
                    return
                if time.monotonic() > deadline:
                    # 超过最长推送时长：强制结束（客户端可重连）
                    yield sse_event("done", {"running": running, "timeout": True})
                    return
            except Exception:
                yield sse_event("done", {"error": True})
                return
            await asyncio.sleep(_SSE_INTERVAL)

    return await sse_stream(_generator())
