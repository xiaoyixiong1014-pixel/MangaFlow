"""通知中心路由模块（文件级说明）。

实现登录用户的站内通知接口：
- GET  /api/notifications            通知分页列表（创建时间倒序 + 未读数）
- GET  /api/notifications/unread-count  未读数量
- POST /api/notifications/read       标记已读（按 ID 批量 / 全部）
- GET  /api/notifications/stream     SSE 实时推送（订阅进程内广播总线，
  事件 event=message data=通知 JSON；每 30s 心跳，超时主动结束供客户端重连）
"""

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.core.sse import sse_event, sse_stream
from app.models import Notification, User
from app.schemas.notification import (
    NotificationListOut,
    NotificationOut,
    NotificationReadRequest,
    NotificationReadResult,
    UnreadCountOut,
)
from app.services import normalize_pagination
from app.services import notification_service

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

# SSE 心跳间隔（秒）：无新消息时定期发送 heartbeat 帧，防止代理断开空闲连接
_HEARTBEAT_INTERVAL = 30
# SSE 最长推送时长（秒）：超时后主动结束，客户端 EventSource 会自动重连
_SSE_MAX_SECONDS = 600


@router.get("", response_model=NotificationListOut)
async def list_notifications(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationListOut:
    """分页查询当前用户的通知列表（创建时间倒序），附带未读总数。

    Args:
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        NotificationListOut: 分页通知列表与未读数。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）。
    """
    page, page_size = normalize_pagination(page, page_size)
    conditions = [Notification.user_id == user.id]
    total = await db.scalar(
        select(func.count()).select_from(Notification).where(*conditions)
    ) or 0
    unread_count = await db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(*conditions, Notification.read_at.is_(None))
    ) or 0
    rows = await db.execute(
        select(Notification)
        .where(*conditions)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [NotificationOut.model_validate(n) for n in rows.scalars().all()]
    return NotificationListOut(
        total=int(total), page=page, page_size=page_size, unread_count=int(unread_count), items=items
    )


@router.get("/unread-count", response_model=UnreadCountOut)
async def unread_count(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UnreadCountOut:
    """查询当前用户的未读通知数量（前端角标轮询）。

    Args:
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        UnreadCountOut: 未读通知数量。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）。
    """
    count = await db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
    ) or 0
    return UnreadCountOut(unread_count=int(count))


@router.post("/read", response_model=NotificationReadResult)
async def mark_read(
    req: NotificationReadRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationReadResult:
    """标记通知已读：all=True 标记全部未读，否则按 ids 批量标记。

    已读操作幂等（已是已读的记录不计入更新条数）；重复请求返回 updated=0。

    Args:
        req: 标记已读请求（ids 与 all 至少提供一个）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        NotificationReadResult: 本次实际标记已读的条数。

    Raises:
        HTTPException: 400 —— ids 与 all 均未提供，或 ids 为空列表。
    """
    if not req.all and not req.ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="ids 与 all 必须至少提供一个"
        )
    stmt = (
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=func.now())
    )
    if not req.all:
        stmt = stmt.where(Notification.id.in_(req.ids))
    result = await db.execute(stmt)
    await db.commit()
    return NotificationReadResult(updated=result.rowcount or 0)


@router.get("/stream")
async def stream_notifications(
    user: User = Depends(get_current_user),
) -> object:
    """SSE 实时推送当前用户的通知：订阅进程内广播总线。

    事件格式：event=message，data=通知 JSON（与列表接口字段一致）；
    无消息期间每 30s 推送一次 heartbeat 心跳；超过最长推送时长后主动结束
    （客户端 EventSource 自动重连）。

    Args:
        user: 当前登录用户。

    Returns:
        StreamingResponse: text/event-stream 流式响应。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）。
    """

    async def _generator():
        """通知推送生成器：读取订阅队列 → message/heartbeat 事件（内部函数）。

        Yields:
            str: SSE 文本帧（message 数据帧或 heartbeat 心跳帧）。

        Raises:
            无：断连时 StreamingResponse 取消生成器，finally 注销订阅。
        """
        queue = notification_service.subscribe(user.id)
        try:
            deadline = time.monotonic() + _SSE_MAX_SECONDS
            while True:
                if time.monotonic() > deadline:
                    # 超过最长推送时长：主动结束（客户端自动重连）
                    yield sse_event("done", {"timeout": True})
                    return
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL)
                    yield sse_event("message", payload)
                except asyncio.TimeoutError:
                    # 心跳保活：无新消息时定期发送 heartbeat 帧
                    yield sse_event("heartbeat", {"ts": int(time.time())})
        finally:
            notification_service.unsubscribe(user.id, queue)

    return await sse_stream(_generator())
