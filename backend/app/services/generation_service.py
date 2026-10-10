"""生成任务服务模块（文件级说明）。

承载生成类任务（关键帧 image / 视频 video / 配音 tts / 合成 compose）的
创建编排逻辑：归属校验、幂等防重、积分冻结（预扣）与任务落库。
路由层（app/api/generation.py、app/api/publish.py）只做参数编排，
事务细节统一收敛在本模块。

计费约定（RD-28 冻结-扣费-退款闭环）：
- 关键帧 10 积分/张；视频 单价×时长（logical_models.price_per_unit.per_second，
  缺省 20/秒）；配音 ceil(字数/100)×5 积分；成片合成 200 积分/次。
"""

import math
from typing import List, Optional, Tuple
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LogicalModel, Project, Storyboard, Task, User, Wallet
from app.services.billing_service import freeze_points

# 关键帧单价（积分/张）
IMAGE_COST_PER_FRAME = 10
# 成片合成单价（积分/次）
COMPOSE_COST = 200
# 视频单价缺省值（积分/秒，logical_models 未配置时兜底）
DEFAULT_VIDEO_PER_SECOND = 20
# 配音计费常量：每 100 字 5 积分
TTS_COST_PER_100_CHARS = 5

# 受限用户（account_status=restricted）每日生成任务数上限（C-04 分级处罚限流）
RESTRICTED_DAILY_TASK_LIMIT = 10

# 允许的任务创建状态集合（幂等防重检查用）
_ACTIVE_STATUS = ("queued", "running")


async def ensure_generation_allowed(db: AsyncSession, user_id: int) -> None:
    """生成接口统一准入校验（C-04 分级处罚）。

    - banned（禁用）：一律 403 拒绝；
    - restricted（受限）：当日（自然日）已创建任务数达到上限时 429 限流；
    - active：直接放行。

    Args:
        db: 异步数据库会话。
        user_id: 提交用户 ID（用户不存在时静默放行，交由归属校验兜底）。

    Returns:
        None

    Raises:
        HTTPException: 403 —— 账号已被禁用；429 —— 受限用户当日生成次数已达上限。
    """
    user = await db.get(User, user_id)
    if user is None:
        return
    if user.account_status == "banned":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被禁用，如有疑问请联系运营方申诉")
    if user.account_status == "restricted":
        cnt = await db.scalar(
            select(func.count())
            .select_from(Task)
            .join(Project, Project.id == Task.project_id)
            .where(
                Project.user_id == user_id,
                # 当日起点（数据库侧自然日截断，避免应用/数据库时区偏差）
                Task.created_at >= func.date_trunc("day", func.now()),
            )
        )
        if int(cnt or 0) >= RESTRICTED_DAILY_TASK_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"今日生成次数已达上限（{RESTRICTED_DAILY_TASK_LIMIT} 次），请明日再试或联系运营方",
            )


async def get_owned_project(db: AsyncSession, user_id: int, project_id: int) -> Project:
    """校验项目归属并返回项目对象。

    Args:
        db: 异步数据库会话。
        user_id: 当前用户 ID。
        project_id: 项目 ID。

    Returns:
        Project: 归属当前用户的项目 ORM 对象。

    Raises:
        HTTPException: 404 —— 项目不存在；403 —— 项目不属于当前用户。
    """
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    if project.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该项目")
    return project


async def get_storyboard_of_project(
    db: AsyncSession, project_id: int, storyboard_id: int
) -> Storyboard:
    """校验分镜存在且属于指定项目。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。
        storyboard_id: 分镜 ID。

    Returns:
        Storyboard: 分镜 ORM 对象。

    Raises:
        HTTPException: 404 —— 分镜不存在或不属于该项目。
    """
    sb = await db.get(Storyboard, storyboard_id)
    if sb is None or sb.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分镜不存在")
    return sb


def build_prompt(*parts: Optional[str]) -> str:
    """拼接提示词各层（RD-04 分层：锁定层 + 可编辑层 + 风格锚点）。

    Args:
        *parts: 提示词片段（忽略空值），顺序为 prompt_locked → prompt_editable → 项目 style。

    Returns:
        str: 以单空格连接的非空片段；全空时返回空串。

    Raises:
        无：纯字符串处理，不产生异常路径。
    """
    return " ".join(p.strip() for p in parts if p and p.strip())


async def get_video_price(db: AsyncSession) -> int:
    """读取视频逻辑模型的每秒单价（元数据来自 logical_models.price_per_unit）。

    Args:
        db: 异步数据库会话。

    Returns:
        int: per_second 单价（整数积分/秒）；未配置模型或字段缺失时取缺省 20。

    Raises:
        无：查不到模型时静默降级为缺省值。
    """
    lm = (
        await db.execute(
            select(LogicalModel).where(
                LogicalModel.capability == "video", LogicalModel.is_default.is_(True)
            )
        )
    ).scalars().first()
    if lm is None:
        lm = (
            await db.execute(select(LogicalModel).where(LogicalModel.capability == "video"))
        ).scalars().first()
    per_second = (lm.price_per_unit or {}).get("per_second", DEFAULT_VIDEO_PER_SECOND) if lm else DEFAULT_VIDEO_PER_SECOND
    try:
        return max(1, int(per_second))
    except (TypeError, ValueError):
        return DEFAULT_VIDEO_PER_SECOND


async def _precheck_balance(db: AsyncSession, user_id: int, total_cost: int) -> None:
    """积分余额预检（避免逐任务冻结中途余额不足留下半提交状态）。

    Args:
        db: 异步数据库会话。
        user_id: 用户 ID。
        total_cost: 本次要冻结的积分总额。

    Raises:
        HTTPException: 409 —— 可用积分余额不足。
    """
    balance = await db.scalar(select(Wallet.points_balance).where(Wallet.user_id == user_id))
    if balance is None or balance < total_cost:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="积分余额不足，无法冻结")


async def freeze_and_create_task(
    db: AsyncSession,
    *,
    project: Project,
    task_type: str,
    phase: str,
    cost_points: int,
    storyboard_id: Optional[int] = None,
    meta: Optional[dict] = None,
    idempotency_key: str,
) -> Task:
    """冻结积分并创建生成任务（同一事务提交，RD-28 预扣）。

    任务创建统一准入口：先执行 C-04 分级处罚准入校验（banned 拒绝 /
    restricted 日限流），再冻结积分落任务。关键帧/视频/配音/合成/重试等
    全部任务创建路径均经由此函数，校验逻辑单点收敛。

    Args:
        db: 异步数据库会话（freeze_points 内部提交事务，任务随同落库）。
        project: 归属项目（取 user_id 与 project_id）。
        task_type: 任务类型 image/video/tts/compose。
        phase: 阶段 trial/full/incremental。
        cost_points: 冻结积分数量。
        storyboard_id: 关联分镜 ID（可空）。
        meta: 任务附加信息（prompt/时长/目标资产等，JSONB）。
        idempotency_key: 冻结流水幂等键。

    Returns:
        Task: 已落库（含自增 ID）的新任务对象。

    Raises:
        HTTPException: 403 —— 账号已被禁用；429 —— 受限用户当日次数达上限；
            409 —— 积分余额不足；400 —— 参数非法（透传自 billing_service）。
    """
    # C-04 分级处罚准入校验（banned 403 / restricted 日限流 429）
    await ensure_generation_allowed(db, project.user_id)
    task = Task(
        project_id=project.id,
        storyboard_id=storyboard_id,
        type=task_type,
        phase=phase,
        status="queued",
        cost_points=cost_points,
        idempotency_key=idempotency_key,
        meta=meta or {},
    )
    db.add(task)
    await db.flush()  # 先取自增主键，供冻结流水 ref_task_id 引用
    await freeze_points(
        db,
        user_id=project.user_id,
        amount=cost_points,
        ref_task_id=task.id,
        idempotency_key=idempotency_key,
    )
    return task


async def _ensure_no_active_image_task(
    db: AsyncSession, storyboard_ids: List[int], phase: str
) -> None:
    """幂等防重：任一涉及分镜已存在排队/进行中的同阶段关键帧任务则拒绝。

    Args:
        db: 异步数据库会话。
        storyboard_ids: 涉及的分镜 ID 列表。
        phase: 阶段 trial/full。

    Raises:
        HTTPException: 409 —— 存在进行中的同阶段任务（防重复提交）。
    """
    if not storyboard_ids:
        return
    cnt = await db.scalar(
        select(func.count()).select_from(Task).where(
            Task.type == "image",
            Task.phase == phase,
            Task.status.in_(_ACTIVE_STATUS),
            Task.storyboard_id.in_(storyboard_ids),
        )
    )
    if cnt:
        # 防重复提交：同分镜同阶段已有排队/进行中的任务
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该分镜已有进行中的同阶段生成任务，请等待完成或先取消",
        )


async def create_keyframe_tasks(
    db: AsyncSession,
    *,
    project: Project,
    storyboard: Storyboard,
    phase: str,
    prompt_editable: Optional[str],
) -> Tuple[List[Task], int]:
    """创建关键帧生成任务（打样 trial 单分镜 / 全量 full 全部分镜）。

    Args:
        db: 异步数据库会话。
        project: 归属项目。
        storyboard: 请求指定的分镜（trial 的目标；full 时用于定位用户编辑层）。
        phase: "trial" 或 "full"。
        prompt_editable: 用户本次提交的可编辑提示词（仅 trial 生效）。

    Returns:
        Tuple[List[Task], int]: （新建任务列表，冻结积分总额）。

    Raises:
        HTTPException: 409 —— 防重复提交 / 积分余额不足。
    """
    if phase == "trial":
        targets: List[Storyboard] = [storyboard]
    else:
        rows = await db.execute(
            select(Storyboard)
            .where(Storyboard.project_id == project.id)
            .order_by(Storyboard.sequence)
        )
        targets = list(rows.scalars().all())
        if not targets:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="项目暂无分镜，无法全量生成")

    await _ensure_no_active_image_task(db, [sb.id for sb in targets], phase)

    total_cost = len(targets) * IMAGE_COST_PER_FRAME
    await _precheck_balance(db, project.user_id, total_cost)

    tasks: List[Task] = []
    for sb in targets:
        # 提示词分层拼接：锁定层 + 可编辑层（trial 用请求值，full 用分镜自身字段）+ 风格锚点
        editable = prompt_editable if phase == "trial" else sb.prompt_editable
        prompt = build_prompt(sb.prompt_locked, editable, project.style)
        idem = f"keyframe_{sb.id}_{phase}_{uuid4().hex[:8]}"
        task = await freeze_and_create_task(
            db,
            project=project,
            task_type="image",
            phase=phase,
            cost_points=IMAGE_COST_PER_FRAME,
            storyboard_id=sb.id,
            meta={
                "prompt": prompt,
                "target_type": "storyboard_keyframe",
                "target_id": sb.id,
            },
            idempotency_key=idem,
        )
        tasks.append(task)
    return tasks, total_cost


async def create_video_task(
    db: AsyncSession,
    *,
    project: Project,
    storyboard: Storyboard,
    use_end_frame: bool,
) -> Task:
    """创建视频片段生成任务（成本 = 每秒单价 × 分镜时长）。

    Args:
        db: 异步数据库会话。
        project: 归属项目。
        storyboard: 目标分镜。
        use_end_frame: 是否使用尾帧控制（Mock 下仅记录于任务 meta）。

    Returns:
        Task: 已落库的新视频任务。

    Raises:
        HTTPException: 409 —— 积分余额不足。
    """
    per_second = await get_video_price(db)
    cost = max(1, int(per_second * (storyboard.duration or 5)))
    prompt = build_prompt(storyboard.prompt_locked, storyboard.prompt_editable, project.style)
    await _precheck_balance(db, project.user_id, cost)
    return await freeze_and_create_task(
        db,
        project=project,
        task_type="video",
        phase="full",
        cost_points=cost,
        storyboard_id=storyboard.id,
        meta={
            "prompt": prompt,
            "duration": storyboard.duration or 5,
            "storyboard_id": storyboard.id,
            "use_end_frame": use_end_frame,
        },
        idempotency_key=f"video_{storyboard.id}_{uuid4().hex[:8]}",
    )


async def create_tts_task(
    db: AsyncSession,
    *,
    project: Project,
    storyboard: Storyboard,
    voice: str,
    speed: float,
    pitch: float,
) -> Task:
    """创建配音（TTS）生成任务（成本 = ceil(字数/100)×5 积分）。

    Args:
        db: 异步数据库会话。
        project: 归属项目。
        storyboard: 目标分镜（文本取 dialogue，为空回退 visual_description）。
        voice: 音色标识 male_01 / female_01。
        speed: 语速倍率。
        pitch: 音调倍率。

    Returns:
        Task: 已落库的新配音任务。

    Raises:
        HTTPException: 400 —— 分镜无可配音文本；409 —— 积分余额不足。
    """
    text = (storyboard.dialogue or "").strip() or (storyboard.visual_description or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该分镜无可配音文本（对白与画面描述均为空）")
    cost = max(1, math.ceil(len(text) / 100) * TTS_COST_PER_100_CHARS)
    await _precheck_balance(db, project.user_id, cost)
    return await freeze_and_create_task(
        db,
        project=project,
        task_type="tts",
        phase="full",
        cost_points=cost,
        storyboard_id=storyboard.id,
        meta={
            "text": text,
            "voice": voice,
            "speed": speed,
            "pitch": pitch,
            "storyboard_id": storyboard.id,
        },
        idempotency_key=f"tts_{storyboard.id}_{uuid4().hex[:8]}",
    )


async def create_compose_task(
    db: AsyncSession,
    *,
    project: Project,
    resolution: str,
    aspect_ratio: str,
    mode: str,
    storyboard_ids: Optional[List[int]] = None,
    subtitles: bool = False,
    subtitle_style: Optional[dict] = None,
    bgm: Optional[dict] = None,
    transition: Optional[dict] = None,
) -> Task:
    """创建成片合成任务（成本 200 积分/次）。

    Args:
        db: 异步数据库会话。
        project: 归属项目。
        resolution: 分辨率 720P/1080P/2K。
        aspect_ratio: 画幅比例 9:16/16:9/1:1（缺省沿用项目配置）。
        mode: 合成模式 incremental/full（映射为任务 phase）。
        storyboard_ids: 增量合成指定分镜 ID 列表（Task 5.3；仅 incremental 且
            非空时生效，空/None 回退项目全部分镜）。
        subtitles: 是否烧录字幕（Task 5.1）。
        subtitle_style: 字幕样式 {font_size, color, position}（可空用默认）。
        bgm: 背景音乐 {url, volume}（Task 5.2，可空表示不加）。
        transition: 转场 {type, duration}（Task 5.3，可空表示不加）。

    Returns:
        Task: 已落库的新合成任务（meta 携带合成增强参数与涉及分镜清单）。

    Raises:
        HTTPException: 400 —— 项目暂无分镜或增量分镜清单为空；409 —— 积分余额不足。
    """
    if mode == "incremental" and storyboard_ids:
        # 增量合成（Task 5.3）：仅拼接指定分镜（调用方已完成归属与顺序校验）
        targets: List[int] = list(storyboard_ids)
    else:
        rows = await db.execute(
            select(Storyboard)
            .where(Storyboard.project_id == project.id)
            .order_by(Storyboard.sequence)
        )
        targets = [sb.id for sb in rows.scalars().all()]
    if not targets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="项目暂无分镜，无法合成")
    await _precheck_balance(db, project.user_id, COMPOSE_COST)
    return await freeze_and_create_task(
        db,
        project=project,
        task_type="compose",
        phase=mode,  # incremental/full 直接映射任务 phase（约束允许）
        cost_points=COMPOSE_COST,
        storyboard_id=None,
        meta={
            "resolution": resolution,
            "aspect_ratio": aspect_ratio or project.aspect_ratio,
            "mode": mode,
            "storyboard_ids": targets,
            # Task 5 合成增强参数（None 项由 compose_service 按缺省处理）
            "subtitles": bool(subtitles),
            "subtitle_style": subtitle_style,
            "bgm": bgm,
            "transition": transition,
        },
        idempotency_key=f"compose_{project.id}_{uuid4().hex[:8]}",
    )


async def create_retry_task(db: AsyncSession, task: Task) -> Task:
    """为失败任务创建重试任务（新幂等键，重新冻结积分）。

    Args:
        db: 异步数据库会话。
        task: 原失败任务（须已通过 status=='failed' 校验）。

    Returns:
        Task: 已落库的新重试任务（继承原任务的类型/阶段/meta/积分）。

    Raises:
        HTTPException: 409 —— 积分余额不足。
    """
    project = await db.get(Project, task.project_id)
    await _precheck_balance(db, project.user_id, task.cost_points)
    return await freeze_and_create_task(
        db,
        project=project,
        task_type=task.type,
        phase=task.phase,
        cost_points=task.cost_points,
        storyboard_id=task.storyboard_id,
        meta=task.meta,
        idempotency_key=f"retry_{task.id}_{uuid4().hex[:8]}",
    )
