"""通知中心服务模块（文件级说明）。

实现站内通知的写入与实时推送：
- 进程内 SSE 广播总线：模块级订阅表 ``{user_id: set[asyncio.Queue]}}``，
  API 层 subscribe/unsubscribe 注册连接，_bus_publish 以 put_nowait 扇出
  通知负载（队列满则丢弃该条，保护慢消费者不阻塞发布方）；
- create_notification()：写库 + 广播（传入调用方会话，flush 后即有自增 ID）；
- notify_* 便捷函数族：各自 ``async with SessionLocal()`` 自管理会话，供
  任务执行器（runner）等非请求上下文直接调用；内部捕获全部异常只记日志，
  保证通知失败不影响主业务流程。

通知类型（与 Notification.type 约束一致）：
task_done / task_failed / review_result / quota_warning / system。
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional, Set

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal
from app.models import Notification

logger = logging.getLogger("mangaflow.services.notification")

# 进程内 SSE 广播总线：user_id -> 该用户当前在线连接的消息队列集合
_SUBSCRIBERS: Dict[int, Set[asyncio.Queue]] = {}

# 单连接消息队列容量上限（超出即丢弃新消息，防止慢消费者撑爆内存）
_QUEUE_MAXSIZE = 100

# 允许的通知类型集合（与模型 CheckConstraint 一致）
NOTIFICATION_TYPES = ("task_done", "task_failed", "review_result", "quota_warning", "system")


def subscribe(user_id: int) -> asyncio.Queue:
    """为用户注册一条 SSE 订阅队列（连接建立时调用）。

    Args:
        user_id: 订阅用户 ID。

    Returns:
        asyncio.Queue: 该连接专属的消息队列（容量 _QUEUE_MAXSIZE）。

    Raises:
        无。
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    _SUBSCRIBERS.setdefault(user_id, set()).add(queue)
    return queue


def unsubscribe(user_id: int, queue: asyncio.Queue) -> None:
    """注销用户的 SSE 订阅队列（连接断开时调用）。

    Args:
        user_id: 订阅用户 ID。
        queue: subscribe 返回的消息队列。

    Returns:
        None。

    Raises:
        无。
    """
    queues = _SUBSCRIBERS.get(user_id)
    if queues is None:
        return
    queues.discard(queue)
    if not queues:
        # 最后一条连接断开后清理用户条目，避免字典无限膨胀
        _SUBSCRIBERS.pop(user_id, None)


def _bus_publish(user_id: int, payload: dict) -> None:
    """向用户全部在线连接扇出一条通知负载（进程内广播）。

    Args:
        user_id: 目标用户 ID。
        payload: 通知负载（可 JSON 序列化 dict，由 SSE 层直发）。

    Returns:
        None。

    Raises:
        无：队列满（QueueFull）时丢弃该连接的本条消息，不影响其他连接。
    """
    for queue in tuple(_SUBSCRIBERS.get(user_id, ())):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("用户 %s 的 SSE 队列已满，丢弃一条通知推送", user_id)


async def create_notification(
    session: AsyncSession,
    user_id: int,
    type: str,
    title: str,
    content: str,
    related_type: Optional[str] = None,
    related_id: Optional[int] = None,
) -> Notification:
    """写入一条站内通知并广播给该用户在线连接。

    事务边界说明：本函数只 add + flush（取得自增 ID 后即广播），提交由调用方
    控制；便捷函数族在自管理会话中调用本函数后负责 commit。

    Args:
        session: 异步数据库会话（调用方持有）。
        user_id: 接收用户 ID。
        type: 通知类型（task_done/task_failed/review_result/quota_warning/system）。
        title: 通知标题。
        content: 通知正文。
        related_type: 关联对象类型（可空）。
        related_id: 关联对象 ID（可空）。

    Returns:
        Notification: 已 flush（含自增 ID）的通知 ORM 对象。

    Raises:
        ValueError: type 不在允许类型集合内。
        Exception: 数据库写入异常向上抛出（由调用方兜底）。
    """
    if type not in NOTIFICATION_TYPES:
        raise ValueError(f"通知类型非法：{type}（允许：{'/'.join(NOTIFICATION_TYPES)}）")
    notification = Notification(
        user_id=user_id,
        type=type,
        title=title[:100],
        content=content,
        related_type=related_type,
        related_id=related_id,
    )
    session.add(notification)
    await session.flush()
    # flush 后已有自增 ID：广播负载与库内记录一致（SSE 消费者不再回查数据库）
    _bus_publish(user_id, _notification_payload(notification))
    return notification


def _notification_payload(notification: Notification) -> dict:
    """将通知 ORM 对象序列化为广播/API 负载 dict（内部函数）。

    Args:
        notification: 通知 ORM 对象。

    Returns:
        dict: 含 id/type/title/content/related_*/read_at/created_at 的负载。

    Raises:
        无。
    """
    def _iso(value: Optional[datetime]) -> Optional[str]:
        """把时间字段转为 ISO 字符串（内部函数）。

        Args:
            value: 时间值（可空）。

        Returns:
            Optional[str]: ISO 格式字符串；入参为空返回 None。
        """
        return value.isoformat() if value else None

    return {
        "id": notification.id,
        "type": notification.type,
        "title": notification.title,
        "content": notification.content,
        "related_type": notification.related_type,
        "related_id": notification.related_id,
        "read_at": _iso(notification.read_at),
        "created_at": _iso(notification.created_at),
    }


async def notify_task_done(
    user_id: int,
    task_id: int,
    task_type: Optional[str] = None,
    title: Optional[str] = None,
    content: Optional[str] = None,
) -> None:
    """发送「任务完成」通知（自管理会话，异常吞掉只记日志）。

    Args:
        user_id: 接收用户 ID（非正数直接跳过，任务可能已无归属用户）。
        task_id: 任务 ID（作为关联对象 ID）。
        task_type: 任务类型（image/video/tts/compose，用于文案）。
        title: 自定义标题（缺省使用默认文案）。
        content: 自定义正文（缺省使用默认文案）。

    Returns:
        None。

    Raises:
        无：内部捕获全部异常并记录 warning。
    """
    if user_id is None or user_id <= 0:
        return
    type_name = task_type or "生成"
    try:
        async with SessionLocal() as session:
            await create_notification(
                session,
                user_id=user_id,
                type="task_done",
                title=title or "生成任务完成",
                content=content or f"您的「{type_name}」任务（#{task_id}）已成功完成。",
                related_type="task",
                related_id=task_id,
            )
            await session.commit()
    except Exception:
        logger.warning("任务 %s 完成通知发送失败（user_id=%s）", task_id, user_id, exc_info=True)


async def notify_task_failed(
    user_id: int,
    task_id: int,
    task_type: Optional[str] = None,
    reason: str = "",
    title: Optional[str] = None,
    content: Optional[str] = None,
) -> None:
    """发送「任务失败」通知（自管理会话，异常吞掉只记日志）。

    Args:
        user_id: 接收用户 ID（非正数直接跳过）。
        task_id: 任务 ID（作为关联对象 ID）。
        task_type: 任务类型（用于文案）。
        reason: 失败原因摘要（拼入默认正文）。
        title: 自定义标题（缺省使用默认文案）。
        content: 自定义正文（缺省使用默认文案）。

    Returns:
        None。

    Raises:
        无：内部捕获全部异常并记录 warning。
    """
    if user_id is None or user_id <= 0:
        return
    type_name = task_type or "生成"
    suffix = f"：{reason}" if reason else ""
    try:
        async with SessionLocal() as session:
            await create_notification(
                session,
                user_id=user_id,
                type="task_failed",
                title=title or "生成任务失败",
                content=content or f"您的「{type_name}」任务（#{task_id}）执行失败{suffix}",
                related_type="task",
                related_id=task_id,
            )
            await session.commit()
    except Exception:
        logger.warning("任务 %s 失败通知发送失败（user_id=%s）", task_id, user_id, exc_info=True)


async def notify_review_result(
    user_id: int,
    record_id: int,
    result: str,
    suggestion: Optional[str] = None,
    title: Optional[str] = None,
    content: Optional[str] = None,
) -> None:
    """发送「审核结果」通知（自管理会话，异常吞掉只记日志）。

    Args:
        user_id: 接收用户 ID（非正数直接跳过）。
        record_id: 审核记录 ID（作为关联对象 ID）。
        result: 复核结果（approved 通过 / rejected 驳回）。
        suggestion: 驳回时的处理建议（拼入正文）。
        title: 自定义标题（缺省使用默认文案）。
        content: 自定义正文（缺省使用默认文案）。

    Returns:
        None。

    Raises:
        无：内部捕获全部异常并记录 warning。
    """
    if user_id is None or user_id <= 0:
        return
    if content is None:
        if result == "approved":
            content = "您提交的内容已通过人工审核。"
        else:
            content = "您提交的内容未通过审核" + (f"，处理建议：{suggestion}" if suggestion else "。")
    try:
        async with SessionLocal() as session:
            await create_notification(
                session,
                user_id=user_id,
                type="review_result",
                title=title or "审核结果通知",
                content=content,
                related_type="review",
                related_id=record_id,
            )
            await session.commit()
    except Exception:
        logger.warning("审核记录 %s 结果通知发送失败（user_id=%s）", record_id, user_id, exc_info=True)


async def notify_quota_warning(user_id: int, balance: int) -> None:
    """发送「积分余额不足」预警通知（自管理会话，异常吞掉只记日志）。

    去重策略：同一用户当日（自然日）已存在未读的 quota_warning 通知时跳过，
    避免任务执行器批量扣费时重复轰炸。

    Args:
        user_id: 接收用户 ID（非正数直接跳过）。
        balance: 当前可用积分余额（拼入正文）。

    Returns:
        None。

    Raises:
        无：内部捕获全部异常并记录 warning。
    """
    if user_id is None or user_id <= 0:
        return
    try:
        async with SessionLocal() as session:
            duplicated = await session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.user_id == user_id,
                    Notification.type == "quota_warning",
                    Notification.read_at.is_(None),
                    # 当日起点（数据库侧自然日截断，避免应用/数据库时区偏差）
                    Notification.created_at >= func.date_trunc("day", func.now()),
                )
            )
            if int(duplicated or 0) > 0:
                return
            await create_notification(
                session,
                user_id=user_id,
                type="quota_warning",
                title="积分余额不足提醒",
                content=f"您的积分余额（{balance}）已低于预警线，请及时充值以免影响生成。",
            )
            await session.commit()
    except Exception:
        logger.warning("配额预警通知发送失败（user_id=%s）", user_id, exc_info=True)
