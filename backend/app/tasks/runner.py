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

- 启动恢复（R-03）：lifespan 在 worker 协程拉起之前先调
  recover_orphan_running_tasks()，把进程重启遗留的 running 僵尸行重置回
  queued（进度保持、retry_count 不动，meta 记 recover_at/recover_from 留痕），
  先恢复后领取避免竞态；恢复失败降级不阻断启动。

mock 供应商（可插拔兜底）：providers/image_mock.py、video_mock.py、tts_mock.py。
image/video/tts 任务执行时先经 model_router.resolve_channels_chain 解析
「逻辑模型 → 物理渠道」降级链（Agnes 图像/视频、百炼 TTS 等真实渠道在前），
链尾固定追加本地 mock 哨兵渠道（channel_id=0）兜底：逐候选调用，成功即止，
每次尝试回写渠道健康状态，失败原因累积进任务 meta.fallback_chain 供溯源。

演示占位治理（D3）：mock 兜底产物打 placeholder 标记——provider 返回 meta
携带 placeholder=True（文件名统一带 placeholder_ 前缀、画面/音频可感知占位），
执行器平铺进任务 meta 并在命中 mock 哨兵渠道时兜底补写；任务成功且
placeholder=True 时执行计费豁免（冻结积分全额退款，幂等键前缀 ph-refund-，
备注含「演示占位豁免」），占位产物不向用户收费。

生成产物内容机审（Task 7 / R-08、C-01）：图像生成成功后先做**真实视觉机审**
（providers/vision_review.py 调 Agnes 视觉模型审画面实际内容，method=vision），
失败降级 LLM 纯文本机审（method=llm-text）→ 纯词表（method=keyword）；视频
生成成功后抽取产物首帧（ffmpeg，无则跳过）送视觉机审，降级链同上。violate
判定任务置 failed（content_violation）并按用户承担退款、触发分级处罚；
suspect 写审核记录待人工复核；pass 写通过记录（review_records，gate=generate）。

调度增强（Task 2.1）：
- 优先级调度：领取 SQL 按 priority DESC, created_at ASC 排序，高优先级任务
  优先执行；next_retry_at 未到期的任务本轮不可领取（指数退避：2^n 分钟）；
- 死信队列：重试耗尽（>= _MAX_RETRY）置 dead_letter=True 进入管理端死信
  队列（api/admin/dlq.py），由运营方决定是否重新入队；
- 通知中心：成功/失败终态经 notification_service 推送站内通知（自管理
  会话 + 异常吞掉，不影响任务主流程），成功扣费后余额过低触发配额预警。
"""

import asyncio
import base64
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import func, or_, select

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.metrics import mark_task, task_running_delta
from app.models import (
    AssetEmbedding,
    AssetVariant,
    Character,
    MediaAsset,
    Project,
    Prop,
    Scene,
    Storyboard,
    Task,
    Wallet,
)
from app.providers import vision_review
from app.providers.image_agnes import generate_image_real
from app.providers.image_dashscope import generate_image_dashscope
from app.providers.image_mock import generate_image
from app.providers.tts_bailian import generate_tts_real
from app.providers.tts_mock import generate_tts
from app.providers.video_agnes import generate_video_real
from app.providers.video_mock import generate_video
from app.providers.video_wan import generate_video_wan
from app.services import model_router, notification_service
from app.services.billing_service import consume_frozen, refund_frozen
from app.services.compose_service import compose_film
from app.services.generation_service import freeze_and_create_task
from app.services.model_router import ResolvedChannel
from app.services.review_service import (
    apply_violation_penalty,
    classify_text,
    create_review_record,
    llm_review_content,
)

logger = logging.getLogger("mangaflow.tasks.runner")

# 配额预警阈值：低余额阈值（api/billing._LOW_BALANCE_THRESHOLD）的 20%，
# 成功扣费后钱包可用余额低于该值时推送一次配额预警通知（当日去重）
from app.api.billing import _LOW_BALANCE_THRESHOLD as _LOW_BALANCE_SRC  # noqa: E402

_QUOTA_WARNING_POINTS = int(_LOW_BALANCE_SRC * 0.2)

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
# 视频队列满弹性等待轮数上限（每轮覆盖降级链完整重试 + 间隔等待）
_VIDEO_QUEUE_WAIT_ROUNDS = 6
# 视频队列满轮间等待（秒）：免费渠道高峰满载可维持数分钟，总弹性窗口 ≈ 轮数 × 间隔
_VIDEO_QUEUE_WAIT_INTERVAL = 120.0


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


async def recover_orphan_running_tasks() -> int:
    """启动恢复：将进程重启遗留的 running 僵尸任务重置回 queued（R-03 收敛）。

    进程重启时，重启前正在执行的任务行会停留在 status="running"（原 worker
    已消失，无人继续处理、永不终态、冻结积分被长期占用）。本函数由 lifespan
    在 worker 协程拉起**之前**调用（先恢复后领取，保证恢复事务先于任何领取
    提交，消除竞态），把全部 running 行重置回 queued：

    - progress 保持原值（不打断已有进度展示，重领后分步覆盖）；
    - retry_count 不变（重启恢复不是任务失败，不应计入指数退避）；
    - meta 追加 recover_from="running" 与 recover_at（UTC ISO 时间戳）留痕溯源；
    - 通知服务不推送（重启恢复是运维事件，非任务终态，不打扰用户）。

    单实例部署下本函数在 worker 启动前独占运行，无需行锁/SKIP LOCKED；
    极端多实例误部署时两个进程重复重置为 queued 也幂等无害。

    Returns:
        int: 本次恢复（重置回 queued）的任务条数；无遗留 running 行时为 0。

    Raises:
        Exception: 数据库异常向上抛出（由 lifespan 捕获记录后降级继续启动）。
    """
    ids: list = []
    async with SessionLocal() as db:
        tasks = (
            await db.execute(select(Task).where(Task.status == "running"))
        ).scalars().all()
        if not tasks:
            return 0
        recovered_at = datetime.now(timezone.utc).isoformat()
        for task in tasks:
            ids.append(task.id)
            task.status = "queued"
            # meta 整体重新赋值以触发 JSONB 变更检测（保留既有键，不覆盖）
            task.meta = {
                **(task.meta or {}),
                "recover_from": "running",
                "recover_at": recovered_at,
            }
            # progress 保持原值；retry_count 不动（恢复 ≠ 失败，不重排退避）
        await db.commit()
    logger.warning(
        "启动恢复：检测到 %d 条遗留 running 任务（进程重启僵尸行），已重置回 queued，任务 ID：%s",
        len(ids), ids,
    )
    return len(ids)


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


def _is_placeholder_result(result: Optional[Dict], used_channel: Optional[ResolvedChannel]) -> bool:
    """判定生成结果是否为演示占位产物（D3 打标，内部辅助）。

    判定来源（满足其一即占位）：
    - provider 返回 meta.placeholder=True（三个 mock provider 均携带）；
    - 实际使用渠道为 mock 哨兵渠道（channel_id=0，降级链尾兜底）。

    Args:
        result: provider 生成结果 dict（可空）。
        used_channel: 实际使用的渠道（可空）。

    Returns:
        bool: 演示占位产物返回 True。

    Raises:
        无：纯属性/键值判定，不产生异常路径。
    """
    if bool((result or {}).get("meta", {}).get("placeholder")):
        return True
    return bool(used_channel is not None and used_channel.channel_id == 0)


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

    会话流程：SELECT ... FOR UPDATE SKIP LOCKED 抢占（按 priority DESC,
    created_at ASC 优先级调度；指数退避未到期的任务被 WHERE 过滤）→
    置 running 提交 → 分发执行 → 终态提交。抢占不到任务返回 False（调用方休眠）。

    Returns:
        bool: 本次是否处理了一条任务。

    Raises:
        Exception: 数据库异常向上抛出（由 worker 循环兜底记录）。
    """
    async with SessionLocal() as db:
        task = (
            await db.execute(
                select(Task)
                .where(
                    Task.status == "queued",
                    # 指数退避：next_retry_at 未到期的重试任务本轮不领取
                    or_(Task.next_retry_at.is_(None), Task.next_retry_at <= func.now()),
                )
                .order_by(Task.priority.desc(), Task.created_at.asc(), Task.id.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if task is None:
            return False
        task.status = "running"
        task.progress = task.progress or 0
        await db.commit()
        # 可观测性：running 迁移计数 + 进行中任务数 +1
        mark_task("running")
        task_running_delta(1)
        logger.info("任务 %s（%s，优先级 %s）开始执行", task.id, task.type, task.priority)
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
    # 可观测性：canceled 终态迁移计数 + 释放进行中任务数
    mark_task("canceled")
    task_running_delta(-1)
    CANCEL_EVENTS.pop(task.id, None)


async def _fail_task(db, task: Task, user_id: int, code: str, message: str, bear: str = "platform") -> None:
    """任务失败落库：指数退避重试（<上限）或死信终态失败退款。

    重试策略（Task 2.1）：失败后第 n 次重试延迟 2^n 分钟（2/4/8 分钟指数
    退避），next_retry_at 之前的任务不被 worker 领取；重试耗尽（>= 上限）
    置 dead_letter=True 进入管理端死信队列，同时按 bear 退款并推送失败通知。

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
        # 分级处罚（C-04）：内容违规累计计数，1-2 次受限、≥3 次禁用（通知随处罚落库）
        try:
            await apply_violation_penalty(db, user_id)
        except Exception:  # noqa: BLE001 —— 处罚失败不影响违规拦截主流程
            logger.warning("任务 %s 违规处罚写入失败（user_id=%s）", task.id, user_id, exc_info=True)
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
        # 可观测性：failed 终态迁移计数 + 释放进行中任务数
        mark_task("failed")
        task_running_delta(-1)
        # 通知中心：失败终态推送（独立会话，异常内部吞掉不影响主流程）
        await notification_service.notify_task_failed(
            user_id, task.id, task_type=task.type, reason=message[:200]
        )
        return
    task.retry_count = (task.retry_count or 0) + 1
    if task.retry_count < _MAX_RETRY:
        # 平台故障自动重试：重回队列并设置指数退避（2^n 分钟），成本由平台承担
        task.status = "queued"
        task.progress = 0
        task.next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=2 ** task.retry_count)
        await db.commit()
        # 可观测性：queued 回队迁移计数 + 释放进行中任务数（重领时会再次 +1）
        mark_task("queued")
        task_running_delta(-1)
        logger.warning(
            "任务 %s 第 %d 次失败，%.0f 分钟后自动重试：%s",
            task.id, task.retry_count, 2 ** task.retry_count, message,
        )
    else:
        # 重试耗尽：终态失败 + 死信标记（进入管理端死信队列）+ 平台承担退款
        task.status = "failed"
        task.dead_letter = True
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
        # 可观测性：failed 终态迁移计数 + 释放进行中任务数
        mark_task("failed")
        task_running_delta(-1)
        logger.error("任务 %s 重试耗尽，终态失败并进入死信队列：%s", task.id, message)
        # 通知中心：失败终态推送（独立会话，异常内部吞掉不影响主流程）
        await notification_service.notify_task_failed(
            user_id, task.id, task_type=task.type, reason=message[:200]
        )


async def _succeed_task(db, task: Task, user_id: int) -> None:
    """任务成功落库：冻结扣费 + 状态/进度终态（RD-28 闭环）+ 通知中心推送。

    计费闭环分三路（D3 演示占位治理 / 自有 Key 豁免）：
    - 任务 meta 命中 placeholder 标记（mock 兜底演示占位产物）时执行豁免：
      不扣费，冻结积分全额退回可用余额（退款幂等键用独立前缀
      "ph-refund-{task_id}"，与失败退款 "task_{id}_refund" 互不冲突），
      流水备注含「演示占位豁免」；仅对实际冻结了积分（cost_points>0）的
      任务执行；
    - 任务 meta 命中 own_channel_exempt 标记（实际使用用户自有 Key 渠道
      生成）时同样豁免扣费，冻结积分全额退回（幂等键
      "own-refund-{task_id}"，备注含「自有 Key 豁免」，平台承担）；
    - 其余任务照常 consume_frozen 扣费。

    扣费/退款后检查钱包可用余额，低于配额预警线（低余额阈值 * 0.2，当日
    去重）时推送 quota_warning 通知；任务完成通知经通知服务独立会话发送，
    异常内部吞掉，不影响任务主流程。

    Args:
        db: 异步数据库会话。
        task: 任务对象（result_url 已由各类型执行器回填）。
        user_id: 用户 ID（扣费对象）。

    Returns:
        None

    Raises:
        Exception: 扣费/退款/提交异常向上抛出。
    """
    task_meta = task.meta or {}
    if task_meta.get("placeholder") and (task.cost_points or 0) > 0:
        # 演示占位豁免：占位产物免计费，冻结积分全额退回可用余额（平台承担）
        await refund_frozen(
            db,
            user_id=user_id,
            amount=task.cost_points,
            ref_task_id=task.id,
            idempotency_key=f"ph-refund-{task.id}",
            bear="platform",
            remark=f"演示占位豁免：任务 {task.id}（{task.type}）产物为演示占位，冻结积分退回",
        )
    elif task_meta.get("own_channel_exempt") and (task.cost_points or 0) > 0:
        # 自有 Key 豁免：实际使用用户自有渠道生成，免扣生成积分（平台承担）
        await refund_frozen(
            db,
            user_id=user_id,
            amount=task.cost_points,
            ref_task_id=task.id,
            idempotency_key=f"own-refund-{task.id}",
            bear="platform",
            remark=f"自有 Key 豁免：任务 {task.id}（{task.type}）使用自有模型渠道生成，冻结积分退回",
        )
    else:
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
    # 可观测性：done 终态迁移计数 + 释放进行中任务数
    mark_task("done")
    task_running_delta(-1)
    # 通知中心：完成推送 + 配额预警检查（独立会话，异常内部吞掉不影响主流程）
    await notification_service.notify_task_done(user_id, task.id, task_type=task.type)
    try:
        balance = await db.scalar(
            select(Wallet.points_balance).where(Wallet.user_id == user_id)
        )
        if balance is not None and balance < _QUOTA_WARNING_POINTS:
            await notification_service.notify_quota_warning(user_id, int(balance))
    except Exception:
        logger.warning("任务 %s 配额预警检查失败（user_id=%s）", task.id, user_id, exc_info=True)


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
    """机审检查（RD-34 闸门一，Task 4.1 分级词表）：生成前对文本做词表判定。

    - violate（严禁级命中）：落审核记录（method=keyword）→ 分级处罚 → 失败退款；
    - suspect（敏感级命中）：仅落审核记录（method=keyword，manual_status 置空表示
      待人工，与管理端审核队列 pending 口径一致），不拦截本次生成。

    Args:
        db: 异步数据库会话。
        task: 任务对象。
        user_id: 用户 ID。
        text: 待审文本（image/video 为提示词 / tts 为配音文本）。
        target_type: 审核目标类型（image / video / tts）。

    Returns:
        bool: 判定 violate 返回 True（任务已置 failed 并退款）；pass/suspect 返回 False。

    Raises:
        Exception: 审核记录写入/处罚/退款异常向上抛出。
    """
    result, word, level = classify_text(text)
    if result == "pass":
        return False
    storyboard_id = (task.meta or {}).get("storyboard_id") or (task.meta or {}).get("target_id")
    await create_review_record(
        db,
        project_id=task.project_id,
        gate="generate",
        target_type=target_type,
        target_id=int(storyboard_id) if storyboard_id else None,
        result=result,
        method="keyword",
        violation_type="banned_word" if level == "strict" else "sensitive_word",
        suggestion=(
            "提示词命中严禁词，请修改后重新提交" if level == "strict"
            else "提示词命中敏感词，待人工复核"
        ),
    )
    if result == "suspect":
        # 敏感级：记录待人工，不拦截生成
        logger.info("任务 %s 提示词命中敏感词「%s」，已记录待人工复核", task.id, word)
        return False
    await _fail_task(db, task, user_id, "content_violation", f"提示词命中违规词「{word}」，已拦截并退款")
    return True


async def _review_generated_image(db, task: Task, user_id: int, meta: dict) -> bool:
    """生成成功后的图像内容复审（Task 7 视觉优先 / R-08、C-01）：视觉机审 → LLM 文本机审 → 词表。

    降级链（真实视觉机审优先）：
    1. 产物可访问（task.result_url 可解析为本地文件/URL）时先调视觉模型对画面
       实际视觉内容机审（method=vision，providers.vision_review.review_image）；
    2. 视觉失败（异常/超时/产物不可读）降级既有 LLM 纯文本机审（method=llm-text）；
    3. LLM 再失败降级纯词表判定（method=keyword）。

    复审结果一律写 review_records（gate=generate）：
    - violate：重置结果地址后按 content_violation 失败退款（并触发分级处罚）；
    - suspect：写记录等人工复核，不拦截本次任务；
    - pass：写通过记录。

    Args:
        db: 异步数据库会话。
        task: 任务对象（生成已成功，result_url 已回填）。
        user_id: 用户 ID。
        meta: 任务附加信息（prompt/target_type/target_id/storyboard_id）。

    Returns:
        bool: 复审判定 violate 返回 True（任务已置 failed 并退款）；其余 False。

    Raises:
        无：视觉/LLM 审核异常内部降级；记录/失败落库异常由 _execute_task 兜底。
    """
    prompt = meta.get("prompt", "")
    # 资产描述：分镜关键帧目标取画面描述，与提示词拼接作为文本机审送审文本
    description = ""
    target_id = meta.get("target_id")
    if meta.get("target_type") == "storyboard_keyframe" and target_id:
        sb = await db.get(Storyboard, int(target_id))
        description = (sb.visual_description or "") if sb else ""
    text = f"{prompt} {description}".strip()
    if not text and not task.result_url:
        # 无任何可审对象（无文本且产物不可用，理论少见）：跳过复审，不产生记录
        return False

    method = "llm-text"
    verdict: Optional[str] = None
    reason = ""
    word: Optional[str] = None
    level: Optional[str] = None

    # 第一优先：真实视觉机审（产物可访问时，Task 7 / R-08）
    if task.result_url:
        try:
            vr = await vision_review.review_image(image_url=task.result_url, context=text)
            method = "vision"
            verdict = vr["verdict"]
            reason = vr["reason"]
            logger.info("任务 %s 图像视觉机审完成（%s）：%s", task.id, vr.get("model"), verdict)
        except Exception:  # noqa: BLE001 —— 视觉机审失败降级 LLM 文本机审
            logger.warning("任务 %s 图像视觉机审失败，降级 LLM 文本机审", task.id, exc_info=True)

    # 第二/三优先：LLM 文本机审 → 纯词表（保持既有降级骨架）
    if verdict is None:
        if not text:
            # 视觉失败且无任何可审文本：跳过复审，不产生记录
            return False
        method = "llm-text"
        try:
            verdict, reason = await llm_review_content(text)
        except Exception:  # noqa: BLE001 —— LLM 审核失败降级纯词表判定
            method = "keyword"
            verdict, word, level = classify_text(text)
            reason = (
                f"命中{'严禁' if level == 'strict' else '敏感'}词「{word}」" if word else "词表判定通过"
            )

    storyboard_id = meta.get("storyboard_id") or meta.get("target_id")
    violation_type: Optional[str] = None
    if verdict != "pass":
        if method == "vision":
            violation_type = "vision_review"
        elif method == "llm-text":
            violation_type = "llm_review"
        else:
            violation_type = "banned_word" if level == "strict" else "sensitive_word"
    await create_review_record(
        db,
        project_id=task.project_id,
        gate="generate",
        target_type="image",
        target_id=int(storyboard_id) if storyboard_id else None,
        result=verdict,
        method=method,
        violation_type=violation_type,
        suggestion=reason or None,
    )
    if verdict != "violate":
        if verdict == "suspect":
            logger.info("任务 %s 生成内容复审判定 suspect，已记录待人工复核", task.id)
        return False
    # 违规：清空结果地址（避免失败任务仍暴露产物），走 content_violation 失败退款
    task.result_url = None
    await _fail_task(db, task, user_id, "content_violation",
                     f"生成内容复审判定违规：{reason or '内容不合规'}，已拦截并退款")
    return True


async def _extract_video_first_frame(video_path: Path) -> Optional[bytes]:
    """用 ffmpeg 抽取视频首帧（Task 7 视频视觉机审，内部辅助）。

    通过 asyncio 子进程调用 ffmpeg（-frames:v 1）把视频首帧抽为 PNG 临时
    文件并读取字节；环境无 ffmpeg 命令、抽帧失败或超时均返回 None（由调用
    方降级 LLM 文本机审）。

    Args:
        video_path: 视频文件本地绝对路径（不存在时直接返回 None）。

    Returns:
        Optional[bytes]: 首帧 PNG 字节；无法抽取时返回 None。

    Raises:
        无：内部捕获全部异常（FileNotFoundError/超时/非零退出码）返回 None。
    """
    if not video_path.is_file():
        logger.warning("视频首帧抽取跳过：产物文件不存在 %s", video_path)
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            frame_path = Path(td) / "first_frame.png"
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", str(video_path),
                    "-frames:v", "1", str(frame_path),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except FileNotFoundError:
                # 环境未安装 ffmpeg：跳过抽帧，由调用方降级文本机审
                logger.info("未检测到 ffmpeg，跳过视频首帧抽取")
                return None
            try:
                await asyncio.wait_for(proc.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                proc.kill()
                logger.warning("视频首帧抽取超时（30s），已终止 ffmpeg")
                return None
            if proc.returncode != 0 or not frame_path.is_file():
                logger.warning("视频首帧抽取失败（ffmpeg 退出码 %s）", proc.returncode)
                return None
            return frame_path.read_bytes()
    except Exception:  # noqa: BLE001 —— 抽帧任何异常均降级，不阻断视频任务主流程
        logger.warning("视频首帧抽取异常", exc_info=True)
        return None


async def _review_video_first_frame(db, task: Task, user_id: int, meta: dict) -> bool:
    """视频产物首帧视觉机审（Task 7 / R-08、C-01）：视觉机审 → LLM 文本机审 → 词表。

    视频生成成功后抽取产物首帧（ffmpeg 无则跳过）送视觉模型机审（method=vision）；
    抽帧失败或视觉审核失败降级既有 LLM 纯文本机审（method=llm-text）→ 纯词表
    （method=keyword）。审核结果写 review_records（gate=generate，target_type=video）：
    - violate：清空结果地址后按 content_violation 失败退款（并触发分级处罚）；
    - suspect：写记录等人工复核，不拦截本次任务；
    - pass：写通过记录。

    Args:
        db: 异步数据库会话。
        task: 任务对象（生成已成功，result_url 已回填）。
        user_id: 用户 ID。
        meta: 任务附加信息（prompt/storyboard_id）。

    Returns:
        bool: 判定 violate 返回 True（任务已置 failed 并退款）；其余 False。

    Raises:
        无：视觉/LLM 审核异常内部降级；记录/失败落库异常由 _execute_task 兜底。
    """
    prompt = meta.get("prompt", "")
    storyboard_id = meta.get("storyboard_id")
    sb = None
    description = ""
    if storyboard_id:
        sb = await db.get(Storyboard, int(storyboard_id))
        description = (sb.visual_description or "") if sb else ""
    text = f"{prompt} {description}".strip()

    method = "llm-text"
    verdict: Optional[str] = None
    reason = ""
    word: Optional[str] = None
    level: Optional[str] = None

    # 第一优先：真实视觉机审（产物可访问时抽取首帧送审，Task 7 / R-08）
    first_frame: Optional[bytes] = None
    if task.result_url:
        rel = task.result_url.lstrip("/")
        if rel.startswith("storage/"):
            rel = rel[len("storage/"):]
        video_path = settings.storage_path() / rel
        first_frame = await _extract_video_first_frame(video_path)
    if first_frame:
        try:
            vr = await vision_review.review_image(
                image_b64=base64.b64encode(first_frame).decode(), context=text
            )
            method = "vision"
            verdict = vr["verdict"]
            reason = vr["reason"]
            logger.info("任务 %s 视频首帧视觉机审完成（%s）：%s", task.id, vr.get("model"), verdict)
        except Exception:  # noqa: BLE001 —— 视觉机审失败降级 LLM 文本机审
            logger.warning("任务 %s 视频首帧视觉机审失败，降级 LLM 文本机审", task.id, exc_info=True)

    # 第二/三优先：LLM 文本机审 → 纯词表（抽帧失败或视觉审核失败时进入）
    if verdict is None:
        if not text:
            # 无首帧且无可审文本：跳过复审，不产生记录
            return False
        logger.info("任务 %s 视频首帧不可用，降级文本机审（抽帧失败或视觉审核异常）", task.id)
        method = "llm-text"
        try:
            verdict, reason = await llm_review_content(text)
        except Exception:  # noqa: BLE001 —— LLM 审核失败降级纯词表判定
            method = "keyword"
            verdict, word, level = classify_text(text)
            reason = (
                f"命中{'严禁' if level == 'strict' else '敏感'}词「{word}」" if word else "词表判定通过"
            )

    violation_type: Optional[str] = None
    if verdict != "pass":
        if method == "vision":
            violation_type = "vision_review"
        elif method == "llm-text":
            violation_type = "llm_review"
        else:
            violation_type = "banned_word" if level == "strict" else "sensitive_word"
    await create_review_record(
        db,
        project_id=task.project_id,
        gate="generate",
        target_type="video",
        target_id=int(storyboard_id) if storyboard_id else None,
        result=verdict,
        method=method,
        violation_type=violation_type,
        suggestion=reason or None,
    )
    if verdict != "violate":
        if verdict == "suspect":
            logger.info("任务 %s 视频首帧机审判定 suspect，已记录待人工复核", task.id)
        return False
    # 违规：清空结果地址（避免失败任务仍暴露产物），走 content_violation 失败退款
    task.result_url = None
    await _fail_task(db, task, user_id, "content_violation",
                     f"视频内容机审判定违规：{reason or '内容不合规'}，已拦截并退款")
    return True


async def _generate_with_fallback(
    db,
    capability: str,
    executor,
    user_id: Optional[int] = None,
) -> tuple[Optional[Dict], Optional[ResolvedChannel], list]:
    """按渠道降级链依次尝试生成（渠道执行公共逻辑）。

    通过 model_router.resolve_channels_chain 解析候选渠道链（user_id 非空时
    用户自有渠道优先参与路由），并在链尾追加本地 mock 哨兵渠道（channel_id=0）
    兜底；逐候选执行 executor(channel)，成功（结果非 None）即返回；每个候选
    （无论成败）都回写渠道健康状态，失败原因累积进 fallback 列表供任务 meta
    溯源。

    Args:
        db: 异步数据库会话（解析渠道链与健康状态回写）。
        capability: 能力类型（image/video/tts，与任务 type 一致）。
        executor: 异步回调 async (channel: ResolvedChannel) -> Optional[Dict]，
            内部按 channel_id==0 分派 mock 或真实 provider。
        user_id: 用户 ID（非空时其自有渠道优先参与路由；None 仅平台渠道）。

    Returns:
        tuple: (生成结果或 None, 实际使用的渠道或 None, fallback 失败列表)；
            全部候选失败时 result 与 channel 为 None。

    Raises:
        无：executor 抛出的异常被捕获并计入 fallback，不向上传播。
    """
    chain = await model_router.resolve_channels_chain(db, capability, user_id=user_id)
    chain.append(model_router.mock_sentinel(capability))
    fallback: list = []
    for channel in chain:
        error: Optional[str] = None
        result: Optional[Dict] = None
        try:
            result = await executor(channel)
            if result is None:
                error = "provider 返回空结果"
        except Exception as exc:  # noqa: BLE001 —— 单渠道失败降级下一渠道
            result = None
            error = str(exc)
        if result is not None:
            await model_router.record_result(db, channel.channel_id, True)
            return result, channel, fallback
        fallback.append({"channel": channel.name, "error": error})
        await model_router.record_result(db, channel.channel_id, False, error)
        logger.warning("能力 %s 渠道 %s 生成失败：%s", capability, channel.name, error)
    return None, None, fallback


async def _face_consistency_gate(
    db, task: Task, user_id: int, meta: dict, image_url: str
) -> bool:
    """面部一致性闸门（P0-4）：角色/变体图回写后与主形象档案比对。

    行为分档：
    - 首次生成（库中无 kind='face' 主档案）→ 建档放行（返回 True）；
    - 影子模式（face_consistency_enforce=False，默认）→ 仅把 score 写
      task.meta["face_score"] 供回测，放行；
    - 强拦截（enforce=True）且 score < 阈值：
      未重抽过（meta 无 consistency_retry）→ 自动重抽一次：原任务置 canceled
      （平台承担退款）+ 重建同 meta 任务（meta["consistency_retry"]=1，重新冻结）；
      已重抽过 → 任务置 failed（error_code=consistency_failed，平台承担退款），
      防连环重抽死循环。

    Args:
        db: 异步数据库会话（调用方资产回写已提交）。
        task: 已完成的图像任务对象（gate 可能将其改判终态）。
        user_id: 用户 ID（退款对象）。
        meta: 任务 meta（target_type/target_id/consistency_retry）。
        image_url: 本次生成的图像 URL。

    Returns:
        bool: True=放行走 _succeed_task 扣费；False=任务已被 gate 终结
            （canceled/failed），调用方不再处理。

    Raises:
        无：内部吞异常（视觉调用/向量比对失败按跳过校验放行，不阻塞主链路）。
    """
    from sqlalchemy import select as _select

    from app.services.face_profile import build_face_profile, compare_face_profile

    target_type = meta.get("target_type")
    target_id = meta.get("target_id")
    try:
        target_id = int(target_id) if target_id else None
    except (TypeError, ValueError):
        target_id = None

    # 仅角色定妆照与角色变体图参与面部一致性（场景/道具/关键帧不适用）
    character: Character | None = None
    if target_type == "character" and target_id:
        character = await db.get(Character, target_id)
    elif target_type == "variant" and target_id:
        variant = await db.get(AssetVariant, target_id)
        if variant is not None and variant.owner_type == "character":
            character = await db.get(Character, variant.owner_id)
    if character is None:
        return True

    # 主档案是否存在（kind='face' 向量行）——主 session 只读判断
    master = (
        await db.execute(
            _select(AssetEmbedding).where(
                AssetEmbedding.asset_type == "character",
                AssetEmbedding.asset_id == character.id,
                AssetEmbedding.kind == "face",
            )
        )
    ).scalar_one_or_none()

    # 建档/比对走独立会话：视觉调用与档案写入耗时长且内部可能 rollback，
    # 隔离后不污染主 session 的 task 对象状态（与通知落库同模式）
    async with SessionLocal() as sess:
        if master is None:
            # 首次生成：建档并放行
            ch = await sess.get(Character, character.id)
            if ch is not None:
                await build_face_profile(sess, ch, image_url)
            return True

        ch = await sess.get(Character, character.id)
        if ch is None:
            return True
        score = await compare_face_profile(sess, ch, image_url)
    if score is None:
        return True  # 比对失败按跳过校验处理

    # 影子模式：记录 score 供回测，放行（基于 task.meta 合并，保留 provider 溯源）
    task.meta = {**(task.meta or {}), "face_score": score}
    await db.commit()
    if not settings.face_consistency_enforce:
        logger.info(
            "面部一致性（影子）task=%s character=%s score=%.4f",
            task.id, character.id, score,
        )
        return True

    if score >= settings.face_consistency_threshold:
        return True

    # 强拦截：低于阈值
    if not meta.get("consistency_retry"):
        # 自动重抽一次：原任务置 canceled（平台退款）+ 重建任务（retry=1）
        task.status = "canceled"
        task.error_code = "consistency_retry"
        task.error_message = (
            f"面部一致性校验未通过（score={score:.4f} < {settings.face_consistency_threshold}），已自动重抽"
        )
        await refund_frozen(
            db,
            user_id=user_id,
            amount=task.cost_points,
            ref_task_id=task.id,
            idempotency_key=f"task_{task.id}_refund",
            bear="platform",
            remark="面部一致性重抽，冻结积分退回",
        )
        await db.commit()
        mark_task("canceled")
        task_running_delta(-1)
        # 重建同 meta 任务（重试标记 +1，重新冻结积分）
        project = await db.get(Project, task.project_id)
        if project is not None:
            from uuid import uuid4 as _uuid4

            await freeze_and_create_task(
                db,
                project=project,
                task_type="image",
                phase=task.phase,
                cost_points=task.cost_points,
                meta={**(task.meta or {}), "consistency_retry": 1},
                idempotency_key=f"img_{character.id}_retry_{_uuid4().hex[:8]}",
            )
        logger.warning(
            "面部一致性未通过，已自动重抽 task=%s character=%s score=%.4f",
            task.id, character.id, score,
        )
        return False

    # 已重抽过仍不达标：终态失败（平台承担退款），防死循环
    task.status = "failed"
    task.error_code = "consistency_failed"
    task.error_message = (
        f"重抽后面部一致性仍低于阈值（score={score:.4f} < {settings.face_consistency_threshold}）"
    )
    await refund_frozen(
        db,
        user_id=user_id,
        amount=task.cost_points,
        ref_task_id=task.id,
        idempotency_key=f"task_{task.id}_refund",
        bear="platform",
        remark="面部一致性校验未通过，冻结积分退回",
    )
    await db.commit()
    mark_task("failed")
    task_running_delta(-1)
    logger.warning(
        "面部一致性重抽仍不达标 task=%s character=%s score=%.4f",
        task.id, character.id, score,
    )
    return False


async def _run_image(db, task: Task, user_id: int, meta: dict) -> None:
    """执行图像（关键帧/资产图）任务：按渠道降级链生成 → 回写目标资产 → 插入媒体素材。

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
    # A-04 参考图锚点：任务 meta 携带参考图时透传给 provider（资产图一致性锚定）
    reference_image_url: Optional[str] = meta.get("reference_image_url")
    # 机审闸门一：违规词命中直接拦截（不调用 provider）
    if await _check_violation(db, task, user_id, prompt, "image"):
        return
    out_dir = settings.storage_path() / "generated"

    async def _generate(channel: ResolvedChannel) -> Optional[Dict]:
        """单渠道图像生成回调：channel_id==0 走本地 mock；dashscope 协议走
        百炼原生文生图（qwen-image 系列，OpenAI 兼容模式无 images 端点）；
        其余走 OpenAI images 兼容真实渠道。

        Args:
            channel: 当前候选渠道。

        Returns:
            Optional[Dict]: 生成结果；失败返回 None。

        Raises:
            无（provider 内部已收敛异常；异常由 _generate_with_fallback 捕获）。
        """
        if channel.channel_id == 0:
            return await generate_image(
                prompt, out_dir, seed=task.id, reference_image_url=reference_image_url)
        if channel.protocol == "dashscope":
            return await generate_image_dashscope(
                prompt, out_dir, task.id, channel, reference_image_url=reference_image_url)
        return await generate_image_real(
            prompt, out_dir, task.id, channel, reference_image_url=reference_image_url)

    result, used_channel, fallback = await _generate_with_fallback(
        db, "image", _generate, user_id=user_id)
    if result is None:
        await _fail_task(db, task, user_id, "platform_error", "图像供应商返回空结果（已尝试全部渠道）")
        return
    task.result_url = result["url"]
    # provider meta 合并进任务 meta（JSONB 整体重新赋值以触发变更检测）；
    # 追加降级链 fallback_chain 与实际 provider 溯源（不覆盖既有键）；
    # D3 打标：演示占位产物写入 placeholder 标记（provider meta 或 mock 哨兵命中）；
    # 自有渠道命中：实际使用用户自有 Key 的渠道时写 own_channel_exempt（成功后免积分）
    provider = (result.get("meta") or {}).get("provider") or (used_channel.name if used_channel else "mock")
    task.meta = {
        **meta,
        **(result.get("meta") or {}),
        "fallback_chain": fallback,
        "provider": provider,
        "placeholder": _is_placeholder_result(result, used_channel),
        "own_channel_exempt": bool(
            used_channel and used_channel.owner_user_id == user_id
        ),
    }

    # 内容复审（Task 4.1 图像审核链）：LLM 机审优先，失败降级词表；
    # violate 时任务置 failed 退款并触发分级处罚（先于资产回写执行）
    if await _review_generated_image(db, task, user_id, meta):
        return

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
            # P0-3 版本透传：仅替换/重生成时递增（首次生成保持版本 1）
            if obj.asset_url:
                obj.asset_version = (obj.asset_version or 1) + 1
            obj.asset_url = result["url"]
            obj.generation_status = "done"
    elif target_type == "scene" and target_id:
        obj = await db.get(Scene, target_id)
        if obj:
            if obj.asset_url:
                obj.asset_version = (obj.asset_version or 1) + 1
            obj.asset_url = result["url"]
    elif target_type == "prop" and target_id:
        obj = await db.get(Prop, target_id)
        if obj:
            if obj.asset_url:
                obj.asset_version = (obj.asset_version or 1) + 1
            obj.asset_url = result["url"]
    elif target_type == "variant" and target_id:
        obj = await db.get(AssetVariant, target_id)
        if obj:
            if obj.asset_url:
                obj.asset_version = (obj.asset_version or 1) + 1
            obj.asset_url = result["url"]
    elif target_type == "storyboard_keyframe" and target_id:
        sb = await db.get(Storyboard, target_id)
        if sb:
            sb.keyframe_url = result["url"]
            sb.status = "done"
    elif target_type == "storyboard_end_frame" and target_id:
        # P-03/P-08 尾帧任务：回写分镜尾帧地址（不影响关键帧与分镜状态）
        sb = await db.get(Storyboard, target_id)
        if sb:
            sb.end_frame_url = result["url"]

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
    # P0-4 面部一致性闸门：角色/角色变体图回写后比对主形象档案；
    # 返回 False 表示任务已被闸门改判终态（canceled 重抽 / failed），跳过扣费
    if not await _face_consistency_gate(db, task, user_id, meta, result["url"]):
        return
    await _succeed_task(db, task, user_id)


async def _run_video(db, task: Task, user_id: int, meta: dict) -> None:
    """执行视频片段任务：按渠道降级链生成 → 首帧视觉机审 → 回写分镜 video_url。

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
    # duration/resolution 参数保持与既有 meta 兼容
    duration = int(meta.get("duration", 5) or 5)
    # P-12 视频参数（resolution/fps/motion_strength，创建时写入 meta.video_params）
    video_params: Optional[Dict] = meta.get("video_params") or None
    out_dir = settings.storage_path() / "generated"
    # 首帧本地路径（wan2.7-i2v 图生视频需要关键帧驱动；本地文件以 base64 上传，
    # 无公网 URL 依赖）。取分镜关键帧 URL 的文件名拼接 storage 路径。
    first_frame_path: Optional[Path] = None
    # P-03/P-08 尾帧本地路径（use_end_frame 时驱动 wan 首尾帧控制）
    end_frame_path: Optional[Path] = None
    storyboard_id = meta.get("storyboard_id")
    if storyboard_id:
        sb_for_frame = await db.get(Storyboard, int(storyboard_id))
        if sb_for_frame and sb_for_frame.keyframe_url:
            first_frame_path = settings.storage_path() / "generated" / Path(sb_for_frame.keyframe_url).name
        # 尾帧路径优先取任务 meta 记录的地址（创建时快照），缺省回退分镜当前值
        if sb_for_frame:
            end_frame_src = meta.get("end_frame") or sb_for_frame.end_frame_url
            if end_frame_src:
                end_frame_path = settings.storage_path() / "generated" / Path(end_frame_src).name
    # 机审闸门一：视频提示词违规直接拦截（Task 4.1 补齐 video 目标机审）
    if await _check_violation(db, task, user_id, prompt, "video"):
        return

    async def _generate(channel: ResolvedChannel) -> Optional[Dict]:
        """单渠道视频生成回调：按渠道协议分派 mock / 百炼 wan / Agnes。

        Args:
            channel: 当前候选渠道。

        Returns:
            Optional[Dict]: 生成结果；失败返回 None。

        Raises:
            VideoQueueFullError: Agnes 队列满（供弹性等待重试判定）。
            无其他（provider 内部已收敛异常；异常由 _generate_with_fallback 捕获）。
        """
        if channel.channel_id == 0:
            return await generate_video(
                prompt, duration, out_dir, seed=task.id,
                video_params=video_params,
                first_frame_url=meta.get("first_frame"),
                end_frame_url=meta.get("end_frame"),
            )
        if channel.protocol == "dashscope":
            return await generate_video_wan(
                prompt, duration, out_dir, task.id, channel, first_frame_path,
                video_params=video_params, end_frame_path=end_frame_path,
            )
        return await generate_video_real(
            prompt, duration, out_dir, task.id, channel,
            first_frame_url=meta.get("first_frame"),
            video_params=video_params,
            last_frame_url=meta.get("end_frame"),
        )

    # 队列满弹性等待：Agnes 免费渠道高峰持续 video_queue_full（实测可满载数分钟
    # 以上），为避免过早降级 mock 占位，满载时按轮次等待重试；轮次耗尽才走
    # 降级链（链尾 mock 兜底）。轮数持久化进 meta，任务重跑时累计。
    result: Optional[Dict] = None
    fallback: list = []
    used_channel: Optional[ResolvedChannel] = None
    queue_rounds = int(meta.get("queue_wait_rounds", 0) or 0)
    for _ in range(_VIDEO_QUEUE_WAIT_ROUNDS):
        result, used_channel, fallback = await _generate_with_fallback(
            db, "video", _generate, user_id=user_id)
        queue_hit = any("队列满" in str(f.get("error", "")) for f in fallback)
        if result is not None or not queue_hit:
            break
        queue_rounds += 1
        logger.warning(
            "任务 %s 视频队列满，第 %s/%s 轮等待重试（%ss）",
            task.id, queue_rounds, queue_rounds + _VIDEO_QUEUE_WAIT_ROUNDS,
            _VIDEO_QUEUE_WAIT_INTERVAL,
        )
        task.meta = {**meta, "queue_wait_rounds": queue_rounds}
        await db.commit()
        await asyncio.sleep(_VIDEO_QUEUE_WAIT_INTERVAL)
    if result is None:
        await _fail_task(db, task, user_id, "platform_error", "视频供应商返回空结果（已尝试全部渠道）")
        return
    task.result_url = result["url"]
    # provider meta 合并进任务 meta，追加降级链与实际 provider 溯源（不覆盖既有键）；
    # D3 打标：演示占位产物写入 placeholder 标记（provider meta 或 mock 哨兵命中）；
    # 自有渠道命中：实际使用用户自有 Key 的渠道时写 own_channel_exempt（成功后免积分）
    provider = (result.get("meta") or {}).get("provider") or (used_channel.name if used_channel else "mock")
    task.meta = {
        **meta,
        **(result.get("meta") or {}),
        "fallback_chain": fallback,
        "provider": provider,
        "placeholder": _is_placeholder_result(result, used_channel),
        "own_channel_exempt": bool(
            used_channel and used_channel.owner_user_id == user_id
        ),
    }

    # 视频首帧视觉机审（Task 7 / R-08）：violate 时任务置 failed 退款并触发分级处罚；
    # 先于 video_url 回写执行，避免违规产物暴露进分镜
    if await _review_video_first_frame(db, task, user_id, meta):
        return

    storyboard_id = meta.get("storyboard_id")
    if storyboard_id:
        sb = await db.get(Storyboard, int(storyboard_id))
        if sb:
            sb.video_url = result["url"]
    await db.commit()
    await _succeed_task(db, task, user_id)


async def _run_tts(db, task: Task, user_id: int, meta: dict) -> None:
    """执行配音任务：按渠道降级链合成 → 回写分镜 audio_url 与实际时长（RD-31）→ 插入音频素材。

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
    voice = meta.get("voice", "female_01")
    # 机审闸门一：配音文本违规直接拦截
    if await _check_violation(db, task, user_id, text, "tts"):
        return
    out_dir = settings.storage_path() / "generated"

    async def _generate(channel: ResolvedChannel) -> Optional[Dict]:
        """单渠道语音合成回调：channel_id==0 走本地 mock，否则走百炼真实渠道。

        Args:
            channel: 当前候选渠道。

        Returns:
            Optional[Dict]: 合成结果（含 duration_sec）；失败返回 None。

        Raises:
            无（provider 内部已收敛异常；异常由 _generate_with_fallback 捕获）。
        """
        if channel.channel_id == 0:
            return await generate_tts(text, voice, out_dir)
        return await generate_tts_real(text, voice, out_dir, channel)

    result, used_channel, fallback = await _generate_with_fallback(
        db, "tts", _generate, user_id=user_id)
    if result is None:
        await _fail_task(db, task, user_id, "platform_error", "语音供应商返回空结果（已尝试全部渠道）")
        return
    task.result_url = result["url"]
    # provider meta 合并进任务 meta，duration_sec 置于其后防覆盖（RD-31 依据），
    # 追加降级链 fallback_chain 与实际 provider 溯源（不覆盖既有键）；
    # D3 打标：演示占位产物写入 placeholder 标记（provider meta 或 mock 哨兵命中）；
    # 自有渠道命中：实际使用用户自有 Key 的渠道时写 own_channel_exempt（成功后免积分）
    provider = (result.get("meta") or {}).get("provider") or (used_channel.name if used_channel else "mock")
    task.meta = {
        **meta,
        **(result.get("meta") or {}),
        "duration_sec": result.get("duration_sec"),
        "fallback_chain": fallback,
        "provider": provider,
        "placeholder": _is_placeholder_result(result, used_channel),
        "own_channel_exempt": bool(
            used_channel and used_channel.owner_user_id == user_id
        ),
    }
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
