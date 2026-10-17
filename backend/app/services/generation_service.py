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

from app.core.config import settings
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

    开关说明：全局配置 punishment_enabled（默认 False）关闭时直接放行——
    产品上线前取消生成限制；违规处罚的累计与通知（apply_violation_penalty）
    不受此开关影响，照常生效，上线后置 True 即恢复拦截。

    Args:
        db: 异步数据库会话。
        user_id: 提交用户 ID（用户不存在时静默放行，交由归属校验兜底）。

    Returns:
        None

    Raises:
        HTTPException: 403 —— 账号已被禁用；429 —— 受限用户当日生成次数已达上限。
    """
    # 上线前取消生成限制：处罚开关关闭时跳过准入校验（violation 累计照常）
    if not settings.punishment_enabled:
        return
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
    scope_episode_id: Optional[int] = None,
) -> Tuple[List[Task], int]:
    """创建关键帧生成任务（打样 trial 单分镜 / 全量 full 全部分镜）。

    Args:
        db: 异步数据库会话。
        project: 归属项目。
        storyboard: 请求指定的分镜（trial 的目标；full 时用于定位用户编辑层）。
        phase: "trial" 或 "full"。
        prompt_editable: 用户本次提交的可编辑提示词（仅 trial 生效）。
        scope_episode_id: 按集工作流（S-06）：full 全量时限定该剧集的分镜
            （None=项目全部分镜；trial 单分镜不受影响）。

    Returns:
        Tuple[List[Task], int]: （新建任务列表，冻结积分总额）。

    Raises:
        HTTPException: 409 —— 防重复提交 / 积分余额不足。
    """
    if phase == "trial":
        targets: List[Storyboard] = [storyboard]
    else:
        full_conds = [Storyboard.project_id == project.id]
        if scope_episode_id is not None:
            full_conds.append(Storyboard.episode_id == scope_episode_id)
        rows = await db.execute(
            select(Storyboard)
            .where(*full_conds)
            .order_by(Storyboard.sequence)
        )
        targets = list(rows.scalars().all())
        if not targets:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该范围暂无分镜，无法全量生成")

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
    resolution: str = "720p",
    fps: int = 24,
    motion_strength: str = "medium",
) -> Task:
    """创建视频片段生成任务（成本 = 每秒单价 × 分镜时长，P-12 视频参数）。

    use_end_frame=True 时校验分镜尾帧已生成（storyboards.end_frame_url 非空，
    为空 400 提示先生成尾帧）；三参数写入 meta.video_params，首尾帧地址写入
    meta.first_frame / meta.end_frame 供执行器透传给视频渠道。

    Args:
        db: 异步数据库会话。
        project: 归属项目。
        storyboard: 目标分镜。
        use_end_frame: 是否使用尾帧控制（须尾帧已生成）。
        resolution: 分辨率档位 480p/720p/1080p（缺省 720p）。
        fps: 帧率 24/30（缺省 24）。
        motion_strength: 运动幅度 low/medium/high（缺省 medium）。

    Returns:
        Task: 已落库的新视频任务。

    Raises:
        HTTPException: 400 —— 使用尾帧但该分镜尾帧尚未生成；409 —— 积分余额不足。
    """
    if use_end_frame and not storyboard.end_frame_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该分镜尚未生成尾帧，请先生成尾帧再开启尾帧控制",
        )
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
            # P-12 视频参数（执行器透传给 provider 请求体）
            "video_params": {
                "resolution": resolution,
                "fps": fps,
                "motion_strength": motion_strength,
            },
            # P-03/P-08 首尾帧地址（use_end_frame=False 时 end_frame 记录 None）
            "first_frame": storyboard.keyframe_url,
            "end_frame": storyboard.end_frame_url if use_end_frame else None,
        },
        idempotency_key=f"video_{storyboard.id}_{uuid4().hex[:8]}",
    )


async def create_end_frame_task(
    db: AsyncSession,
    *,
    project: Project,
    storyboard: Storyboard,
) -> Task:
    """创建分镜尾帧图像生成任务（P-03/P-08：成本 10 积分/张）。

    复用关键帧图像任务创建路径（type=image），meta 标记 end_frame=True 与
    target_type=storyboard_end_frame，执行器成功后回写 storyboards.end_frame_url。
    幂等防重：同分镜已存在排队/进行中的尾帧任务时 409。

    Args:
        db: 异步数据库会话。
        project: 归属项目。
        storyboard: 目标分镜（须已有关键帧，由路由层校验）。

    Returns:
        Task: 已落库的新尾帧图像任务。

    Raises:
        HTTPException: 409 —— 同分镜已有进行中的尾帧任务 / 积分余额不足。
    """
    # 尾帧任务独立防重：仅检查 meta 带 end_frame 标记的图像任务（与关键帧互不阻塞）
    cnt = await db.scalar(
        select(func.count()).select_from(Task).where(
            Task.type == "image",
            Task.status.in_(_ACTIVE_STATUS),
            Task.storyboard_id == storyboard.id,
            Task.meta["end_frame"].as_boolean().is_(True),
        )
    )
    if cnt:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该分镜已有进行中的尾帧生成任务，请等待完成或先取消",
        )
    total_cost = IMAGE_COST_PER_FRAME
    await _precheck_balance(db, project.user_id, total_cost)
    prompt = build_prompt(storyboard.prompt_locked, storyboard.prompt_editable, project.style)
    return await freeze_and_create_task(
        db,
        project=project,
        task_type="image",
        phase="full",
        cost_points=IMAGE_COST_PER_FRAME,
        storyboard_id=storyboard.id,
        meta={
            "prompt": prompt,
            "target_type": "storyboard_end_frame",
            "target_id": storyboard.id,
            "storyboard_id": storyboard.id,
            "end_frame": True,
        },
        idempotency_key=f"endframe_{storyboard.id}_{uuid4().hex[:8]}",
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
    episode_id: Optional[int] = None,
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
        episode_id: 可选剧集 ID（S-06；传入时全量模式仅取该剧集分镜并写入
            meta，合成产物 film_outputs.episode_id 绑定该剧集）。
        subtitles: 是否烧录字幕（Task 5.1）。
        subtitle_style: 字幕样式 {font_size, color, position}（可空用默认）。
        bgm: 背景音乐 {url, volume}（Task 5.2，可空表示不加）。
        transition: 转场 {type, duration}（Task 5.3，可空表示不加）。

    Returns:
        Task: 已落库的新合成任务（meta 携带合成增强参数与涉及分镜清单）。

    Raises:
        HTTPException: 400 —— 项目暂无分镜或增量分镜清单为空；409 —— 积分余额不足。
    """
    conds = [Storyboard.project_id == project.id]
    if episode_id is not None:
        # S-06：按剧集圈定合成范围（episode_id 归属已由调用方校验）
        conds.append(Storyboard.episode_id == episode_id)
    if mode == "incremental" and storyboard_ids:
        # 增量合成（Task 5.3）：仅拼接指定分镜（调用方已完成归属与顺序校验）
        targets: List[int] = list(storyboard_ids)
    else:
        rows = await db.execute(
            select(Storyboard).where(*conds).order_by(Storyboard.sequence)
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
            # S-06：剧集范围（None=全部，compose_service 据此过滤并回写 film_outputs）
            "episode_id": episode_id,
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


async def render_stale_storyboards(
    db: AsyncSession,
    *,
    project: Project,
    episode_id: Optional[int] = None,
) -> dict:
    """批量重渲染过期镜头（P0-3 版本透传：「仅重渲染受影响镜头」入口）。

    流程：取项目分镜列表 → 过滤 stale=True（引用资产版本快照与当前版本不一致）
    → 跳过已有排队/进行中关键帧任务的镜头 → 逐镜复用关键帧任务构建逻辑
    （freeze_and_create_task，10 积分/帧）入队重渲染。
    复用 /regen 计数端点会造成语义混淆，故此处独立实现且不递增 regen_count。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        episode_id: 可选剧集过滤（与分镜列表口径一致；None=全部）。

    Returns:
        dict: {"queued": 成功入队数, "skipped": 跳过数（已在队列）,
              "storyboard_ids": 入队镜头 ID 列表, "total_cost": 冻结积分总额}。

    Raises:
        HTTPException: 409 —— 积分余额不足；403/429 —— 分级处罚准入拦截。
    """
    from app.services.storyboard_service import list_storyboards  # 延迟导入避免循环依赖

    outs = await list_storyboards(db, project, episode_id)
    stale_ids = [o["id"] for o in outs if o.get("stale")]
    if not stale_ids:
        return {"queued": 0, "skipped": 0, "storyboard_ids": [], "total_cost": 0}

    # 幂等防重：已有排队/进行中关键帧任务（trial/full 任一阶段）的镜头跳过
    busy_ids = set(
        (await db.execute(
            select(Task.storyboard_id).where(
                Task.type == "image",
                Task.phase.in_(["trial", "full"]),
                Task.status.in_(_ACTIVE_STATUS),
                Task.storyboard_id.in_(stale_ids),
            )
        )
    ).scalars().all())
    target_ids = [sid for sid in stale_ids if sid not in busy_ids]
    skipped = len(stale_ids) - len(target_ids)
    if not target_ids:
        return {"queued": 0, "skipped": skipped, "storyboard_ids": [], "total_cost": 0}

    # 余额预检（总成本 = 目标镜数 × 单帧价，一次冻结循环前统一校验）
    total_cost = len(target_ids) * IMAGE_COST_PER_FRAME
    await _precheck_balance(db, project.user_id, total_cost)

    queued_ids: List[int] = []
    target_set = set(target_ids)
    for o in outs:
        if o["id"] not in target_set:
            continue
        sb = await db.get(Storyboard, o["id"])
        if sb is None:
            continue
        prompt = build_prompt(sb.prompt_locked, sb.prompt_editable, project.style)
        await freeze_and_create_task(
            db,
            project=project,
            task_type="image",
            phase="full",
            cost_points=IMAGE_COST_PER_FRAME,
            storyboard_id=sb.id,
            meta={
                "prompt": prompt,
                "target_type": "storyboard_keyframe",
                "target_id": sb.id,
                # P0-3 溯源：标记本次入队为过期重渲染
                "render_reason": "stale",
            },
            idempotency_key=f"keyframe_{sb.id}_full_{uuid4().hex[:8]}",
        )
        queued_ids.append(sb.id)
    return {
        "queued": len(queued_ids),
        "skipped": skipped,
        "storyboard_ids": queued_ids,
        "total_cost": total_cost,
    }
