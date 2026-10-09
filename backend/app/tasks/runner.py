"""异步任务执行器模块（文件级说明）。

进程内 asyncio 生成任务执行器（不依赖 Celery/Redis）：
- start_runner()：随 FastAPI lifespan 启动 3 个 worker 协程（协程引用存于
  模块级 _RUNNER_TASKS，应用关闭时由事件循环统一取消，worker 捕获
  CancelledError 后干净退出）；
- 主循环：每秒以 SELECT ... FOR UPDATE SKIP LOCKED 抢占一条 queued 任务，
  置 running 后按任务类型分发执行，进度分步写库（供 SSE 轮询读取）；
- 计费闭环（RD-28）：成功 consume_frozen；平台故障自动重试（≤3 次，成本
  平台承担，超出后按 platform 退款）；内容违规/用户取消按 user 退款；
- 取消支持：CANCEL_EVENTS[task_id] 置位，worker 执行中分步检测后落库
  canceled 并退款（running 场景）；取消接口对 queued 任务直接落库退款。

mock 供应商（可插拔）：providers/image_mock.py、video_mock.py、tts_mock.py，
签名与真实渠道对齐，日后替换实现即可，执行器无需改动。
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import (
    AssetVariant,
    Character,
    MediaAsset,
    Project,
    Prop,
    Scene,
    Storyboard,
    Task,
)
from app.providers.image_mock import generate_image
from app.providers.tts_mock import generate_tts
from app.providers.video_mock import generate_video
from app.services.billing_service import consume_frozen, refund_frozen
from app.services.compose_service import compose_film
from app.services.review_service import create_review_record, find_banned_word

logger = logging.getLogger("mangaflow.tasks.runner")

# 任务取消事件表：task_id -> Event（取消接口置位，worker 执行中检测）
CANCEL_EVENTS: Dict[int, asyncio.Event] = {}

# worker 协程引用（应用关闭时由事件循环取消）
_RUNNER_TASKS: list = []

# worker 数量
_WORKER_COUNT = 3
# 主循环空闲轮询间隔（秒）
_IDLE_INTERVAL = 1.0
# 自动重试上限（达到后判失败并按平台承担退款）
_MAX_RETRY = 3


async def start_runner(engine: Any) -> None:
    """启动进程内异步任务执行器（创建 3 个 worker 协程）。

    Args:
        engine: SQLAlchemy 异步引擎（worker 内部改用 SessionLocal 自建会话，
            本参数仅为兼容 lifespan 既有调用签名保留）。

    Returns:
        None

    Raises:
        无：worker 循环内部自行兜底异常，不会向 lifespan 传播。
    """
    loop = asyncio.get_running_loop()
    for i in range(_WORKER_COUNT):
        t = loop.create_task(_worker_loop(worker_id=i))
        _RUNNER_TASKS.append(t)
    logger.info("任务执行器已启动（%d 个 worker）", _WORKER_COUNT)


def request_cancel(task_id: int) -> None:
    """请求取消任务：置位取消事件（由取消接口调用）。

    Args:
        task_id: 任务 ID。

    Returns:
        None

    Raises:
        无。
    """
    event = CANCEL_EVENTS.get(task_id)
    if event is None:
        event = asyncio.Event()
        CANCEL_EVENTS[task_id] = event
    event.set()


async def _worker_loop(worker_id: int) -> None:
    """worker 主循环：抢占任务 → 执行 → 无任务时休眠。

    Args:
        worker_id: worker 编号（仅用于日志区分）。

    Returns:
        None（协程被取消时结束）。

    Raises:
        无：单次处理异常被捕获记录，循环不中断；CancelledError 向上传播以正常退出。
    """
    logger.info("worker-%d 启动", worker_id)
    while True:
        try:
            processed = await _process_one()
        except asyncio.CancelledError:
            logger.info("worker-%d 收到取消信号，退出", worker_id)
            raise
        except Exception:
            # 单任务异常不影响整体循环
            logger.exception("worker-%d 处理循环出现未预期异常", worker_id)
            processed = False
        if not processed:
            await asyncio.sleep(_IDLE_INTERVAL)


async def _process_one() -> bool:
    """抢占并执行一条排队任务（单次处理单元）。

    会话流程：SELECT ... FOR UPDATE SKIP LOCKED 抢占 → 置 running 提交 →
    分发执行 → 终态提交。抢占不到任务返回 False（调用方休眠）。

    Returns:
        bool: 本次是否处理了一条任务。

    Raises:
        Exception: 数据库异常向上抛出（由 worker 循环兜底记录）。
    """
    async with SessionLocal() as db:
        task = (
            await db.execute(
                select(Task)
                .where(Task.status == "queued")
                .order_by(Task.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if task is None:
            return False
        task.status = "running"
        task.progress = task.progress or 0
        await db.commit()
        logger.info("任务 %s（%s）开始执行", task.id, task.type)
        await _execute_task(db, task)
        return True


async def _set_progress(db, task: Task, progress: int) -> None:
    """写入任务进度并立即提交（供 SSE 轮询读取）。

    Args:
        db: 异步数据库会话。
        task: 任务对象。
        progress: 进度百分比（0~100）。

    Returns:
        None

    Raises:
        Exception: 数据库提交异常向上抛出。
    """
    task.progress = progress
    await db.commit()


async def _is_canceled(task_id: int) -> bool:
    """检测任务是否被请求取消。

    Args:
        task_id: 任务 ID。

    Returns:
        bool: 取消事件已置位返回 True。

    Raises:
        无。
    """
    event = CANCEL_EVENTS.get(task_id)
    return bool(event and event.is_set())


async def _cancel_task(db, task: Task, user_id: int) -> None:
    """落库任务取消状态并按用户承担退款（bear='user'）。

    Args:
        db: 异步数据库会话。
        task: 任务对象。
        user_id: 用户 ID（退款对象）。

    Returns:
        None

    Raises:
        Exception: 退款/提交异常向上抛出（由 _execute_task 兜底）。
    """
    task.status = "canceled"
    task.error_code = None
    task.error_message = "用户取消任务"
    await refund_frozen(
        db,
        user_id=user_id,
        amount=task.cost_points,
        ref_task_id=task.id,
        idempotency_key=f"task_{task.id}_refund",
        bear="user",
        remark="任务取消，冻结积分退回",
    )
    await db.commit()
    CANCEL_EVENTS.pop(task.id, None)


async def _fail_task(db, task: Task, user_id: int, code: str, message: str, bear: str = "platform") -> None:
    """任务失败落库：自动重试（<上限）或终态失败退款。

    Args:
        db: 异步数据库会话。
        task: 任务对象。
        user_id: 用户 ID（退款对象）。
        code: 错误码（platform_error / content_violation 等）。
        message: 错误信息。
        bear: 退款成本承担方（平台故障 platform / 内容违规 user）。

    Returns:
        None

    Raises:
        Exception: 退款/提交异常向上抛出。
    """
    task.error_code = code
    task.error_message = message[:500]
    if code == "content_violation":
        # 内容违规不做重试，直接失败并按用户承担退款（RD-28/RD-34）
        task.status = "failed"
        await refund_frozen(
            db,
            user_id=user_id,
            amount=task.cost_points,
            ref_task_id=task.id,
            idempotency_key=f"task_{task.id}_refund",
            bear="user",
            remark="内容违规，冻结积分退回",
        )
        await db.commit()
        return
    task.retry_count = (task.retry_count or 0) + 1
    if task.retry_count < _MAX_RETRY:
        # 平台故障自动重试：重回队列，成本由平台承担（最终成功走 consume）
        task.status = "queued"
        task.progress = 0
        await db.commit()
        logger.warning("任务 %s 第 %d 次失败，自动重试：%s", task.id, task.retry_count, message)
    else:
        task.status = "failed"
        await refund_frozen(
            db,
            user_id=user_id,
            amount=task.cost_points,
            ref_task_id=task.id,
            idempotency_key=f"task_{task.id}_refund",
            bear=bear,
            remark=f"任务重试 {_MAX_RETRY} 次仍失败，冻结积分退回（平台承担）",
        )
        await db.commit()
        logger.error("任务 %s 重试耗尽，终态失败：%s", task.id, message)


async def _succeed_task(db, task: Task, user_id: int) -> None:
    """任务成功落库：冻结扣费 + 状态/进度终态（RD-28 闭环）。

    Args:
        db: 异步数据库会话。
        task: 任务对象（result_url 已由各类型执行器回填）。
        user_id: 用户 ID（扣费对象）。

    Returns:
        None

    Raises:
        Exception: 扣费/提交异常向上抛出。
    """
    await consume_frozen(
        db,
        user_id=user_id,
        amount=task.cost_points,
        ref_task_id=task.id,
        idempotency_key=f"task_{task.id}_consume",
        remark=f"任务 {task.id}（{task.type}）成功扣费",
    )
    task.status = "done"
    task.progress = 100
    await db.commit()


async def _get_project_user_id(db, task: Task) -> Optional[int]:
    """获取任务归属用户 ID。

    Args:
        db: 异步数据库会话。
        task: 任务对象。

    Returns:
        Optional[int]: 项目归属用户 ID；项目已被删除（project_id 置空）时返回 None。

    Raises:
        无。
    """
    if task.project_id is None:
        return None
    project = await db.get(Project, task.project_id)
    return project.user_id if project else None


async def _check_violation(db, task: Task, user_id: int, text: str, target_type: str) -> bool:
    """机审检查（RD-34 闸门一）：命中违规词表则落审核记录并失败退款。

    Args:
        db: 异步数据库会话。
        task: 任务对象。
        user_id: 用户 ID。
        text: 待审文本（image 为提示词 / tts 为配音文本）。
        target_type: 审核目标类型（image / tts）。

    Returns:
        bool: 命中违规返回 True（任务已置 failed 并退款）；未命中返回 False。

    Raises:
        Exception: 审核记录写入/退款异常向上抛出。
    """
    word = find_banned_word(text)
    if word is None:
        return False
    storyboard_id = (task.meta or {}).get("storyboard_id") or (task.meta or {}).get("target_id")
    await create_review_record(
        db,
        project_id=task.project_id,
        gate="generate",
        target_type=target_type,
        target_id=int(storyboard_id) if storyboard_id else None,
        result="violate",
        violation_type="banned_word",
        suggestion="请修改提示词中的违规内容",
    )
    await _fail_task(db, task, user_id, "content_violation", f"提示词命中违规词「{word}」，已拦截并退款")
    return True


async def _run_image(db, task: Task, user_id: int, meta: dict) -> None:
    """执行图像（关键帧/资产图）任务：mock 生成 → 回写目标资产 → 插入媒体素材。

    Args:
        db: 异步数据库会话。
        task: 任务对象。
        user_id: 用户 ID。
        meta: 任务附加信息（prompt/target_type/target_id）。

    Returns:
        None（失败通过 _fail_task 落库；成功通过 _succeed_task 落库）。

    Raises:
        无：provider 异常被捕获后走失败流程。
    """
    prompt = meta.get("prompt", "")
    # 机审闸门一：违规词命中直接拦截（不调用 provider）
    if await _check_violation(db, task, user_id, prompt, "image"):
        return
    out_dir = settings.storage_path() / "generated"
    result = await generate_image(prompt, out_dir, seed=task.id)
    if result is None:
        await _fail_task(db, task, user_id, "platform_error", "图像供应商返回空结果")
        return
    task.result_url = result["url"]
    # provider meta 合并进任务 meta（JSONB 整体重新赋值以触发变更检测）
    task.meta = {**meta, **(result.get("meta") or {})}

    # 按目标类型回写资产
    target_type = meta.get("target_type")
    target_id = meta.get("target_id")
    try:
        target_id = int(target_id) if target_id else None
    except (TypeError, ValueError):
        target_id = None
    if target_type == "character" and target_id:
        obj = await db.get(Character, target_id)
        if obj:
            obj.asset_url = result["url"]
            obj.generation_status = "done"
    elif target_type == "scene" and target_id:
        obj = await db.get(Scene, target_id)
        if obj:
            obj.asset_url = result["url"]
    elif target_type == "prop" and target_id:
        obj = await db.get(Prop, target_id)
        if obj:
            obj.asset_url = result["url"]
    elif target_type == "variant" and target_id:
        obj = await db.get(AssetVariant, target_id)
        if obj:
            obj.asset_url = result["url"]
    elif target_type == "storyboard_keyframe" and target_id:
        sb = await db.get(Storyboard, target_id)
        if sb:
            sb.keyframe_url = result["url"]
            sb.status = "done"

    # 媒体素材入库（studio 来源，记录任务溯源）
    db.add(MediaAsset(
        user_id=user_id,
        project_id=task.project_id,
        media_type="image",
        source="studio",
        url=result["url"],
        meta={"task_id": task.id, "target_type": target_type, "target_id": target_id},
    ))
    await db.commit()
    await _succeed_task(db, task, user_id)


async def _run_video(db, task: Task, user_id: int, meta: dict) -> None:
    """执行视频片段任务：mock 生成 → 回写分镜 video_url。

    Args:
        db: 异步数据库会话。
        task: 任务对象。
        user_id: 用户 ID。
        meta: 任务附加信息（prompt/duration/storyboard_id）。

    Returns:
        None（失败通过 _fail_task 落库；成功通过 _succeed_task 落库）。

    Raises:
        无：provider 异常被捕获后走失败流程。
    """
    prompt = meta.get("prompt", "")
    duration = int(meta.get("duration", 5) or 5)
    out_dir = settings.storage_path() / "generated"
    result = await generate_video(prompt, duration, out_dir, seed=task.id)
    if result is None:
        await _fail_task(db, task, user_id, "platform_error", "视频供应商返回空结果")
        return
    task.result_url = result["url"]
    task.meta = {**meta, **(result.get("meta") or {})}
    storyboard_id = meta.get("storyboard_id")
    if storyboard_id:
        sb = await db.get(Storyboard, int(storyboard_id))
        if sb:
            sb.video_url = result["url"]
    await db.commit()
    await _succeed_task(db, task, user_id)


async def _run_tts(db, task: Task, user_id: int, meta: dict) -> None:
    """执行配音任务：mock 合成 → 回写分镜 audio_url 与实际时长（RD-31）→ 插入音频素材。

    Args:
        db: 异步数据库会话。
        task: 任务对象。
        user_id: 用户 ID。
        meta: 任务附加信息（text/voice/storyboard_id）。

    Returns:
        None（失败通过 _fail_task 落库；成功通过 _succeed_task 落库）。

    Raises:
        无：provider 异常被捕获后走失败流程。
    """
    text = meta.get("text", "")
    # 机审闸门一：配音文本违规直接拦截
    if await _check_violation(db, task, user_id, text, "tts"):
        return
    out_dir = settings.storage_path() / "generated"
    result = await generate_tts(text, meta.get("voice", "female_01"), out_dir)
    if result is None:
        await _fail_task(db, task, user_id, "platform_error", "语音供应商返回空结果")
        return
    task.result_url = result["url"]
    task.meta = {**meta, "duration_sec": result.get("duration_sec")}
    storyboard_id = meta.get("storyboard_id")
    if storyboard_id:
        sb = await db.get(Storyboard, int(storyboard_id))
        if sb:
            sb.audio_url = result["url"]
            # RD-31：TTS 回写分镜实际时长
            sb.actual_duration = result.get("duration_sec")
    # 音频素材入库
    db.add(MediaAsset(
        user_id=user_id,
        project_id=task.project_id,
        media_type="audio",
        source="studio",
        url=result["url"],
        meta={"task_id": task.id, "storyboard_id": storyboard_id, "voice": meta.get("voice")},
    ))
    await db.commit()
    await _succeed_task(db, task, user_id)


async def _run_compose(db, task: Task, user_id: int) -> None:
    """执行成片合成任务（委托 compose_service.compose_film）。

    Args:
        db: 异步数据库会话。
        task: 任务对象。
        user_id: 用户 ID。

    Returns:
        None（失败通过 _fail_task 落库；成功通过 _succeed_task 落库）。

    Raises:
        无：compose 异常被捕获后走失败流程。
    """
    try:
        url = await compose_film(db, task)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        await _fail_task(db, task, user_id, "platform_error", f"成片合成异常：{exc}")
        return
    task.result_url = url
    await db.commit()
    await _succeed_task(db, task, user_id)


async def _execute_task(db, task: Task) -> None:
    """按任务类型分发执行（含取消检测与计费闭环）。

    Args:
        db: 异步数据库会话。
        task: 已置 running 的任务对象。

    Returns:
        None

    Raises:
        无：所有分支内部自行兜底，不向 worker 循环抛出。
    """
    user_id = await _get_project_user_id(db, task)
    if user_id is None:
        await _fail_task(db, task, 0, "platform_error", "任务归属项目不存在，无法结算", bear="platform")
        return
    meta = task.meta or {}
    try:
        if task.type == "image":
            await _set_progress(db, task, 30)
            if await _is_canceled(task.id):
                await _cancel_task(db, task, user_id)
                return
            await asyncio.sleep(0.5)  # 模拟生成耗时，让进度可被 SSE 观察
            if await _is_canceled(task.id):
                await _cancel_task(db, task, user_id)
                return
            await _set_progress(db, task, 70)
            await _run_image(db, task, user_id, meta)
        elif task.type == "video":
            if await _is_canceled(task.id):
                await _cancel_task(db, task, user_id)
                return
            await _run_video(db, task, user_id, meta)
        elif task.type == "tts":
            if await _is_canceled(task.id):
                await _cancel_task(db, task, user_id)
                return
            await _run_tts(db, task, user_id, meta)
        elif task.type == "compose":
            if await _is_canceled(task.id):
                await _cancel_task(db, task, user_id)
                return
            await _run_compose(db, task, user_id)
        else:
            # llm/extract 等其他类型本执行器不处理：直接按平台失败退款
            await _fail_task(db, task, user_id, "platform_error", f"任务类型 {task.type} 暂不支持自动执行")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            await db.rollback()
            await _fail_task(db, task, user_id, "platform_error", f"任务执行异常：{exc}")
        except Exception:
            logger.exception("任务 %s 失败落库时再次异常", task.id)
