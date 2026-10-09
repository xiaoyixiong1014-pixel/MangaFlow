"""管理端订单路由模块（文件级说明）。

实现运营方的订单管理与人工干预：
- GET  /api/admin/orders                  订单分页列表（联用户名，支持状态/搜索过滤）
- POST /api/admin/orders/{id}/complete    人工完结：pending → paid（幂等发放积分）
- POST /api/admin/orders/{id}/close       人工关闭：pending → closed
- POST /api/admin/orders/{id}/refund      退款：paid → refunded（回收积分，高危审计）

审计口径：操作日志 user_id 记操作管理员，订单号/原因等写入 detail。
二次确认由前端负责，后端只留痕。全部接口要求运营方角色，普通用户 403。
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_operator
from app.models import OperationLog, Order, User, Wallet, WalletTransaction
from app.schemas.admin import (
    AdminOrderItem,
    MessageOut,
    OrderActionRequest,
    RefundRequest,
    RefundResponse,
)
from app.schemas.common import PageResult
from app.services import normalize_pagination
from app.services.billing_service import grant_points

router = APIRouter(
    prefix="/api/admin/orders",
    tags=["admin-orders"],
    dependencies=[Depends(require_operator)],  # 路由级鉴权：仅运营方可访问，普通用户 403
)

# 允许的状态过滤取值
_ALLOWED_STATUS = ("pending", "paid", "closed", "refunded")


def _get_client_ip(request: Request) -> Optional[str]:
    """提取客户端 IP（内部函数）。

    Args:
        request: 当前请求对象。

    Returns:
        Optional[str]: 客户端 IP；无法获取时为 None。
    """
    return request.client.host if request.client else None


async def _get_order_or_404(db: AsyncSession, order_id: int) -> Order:
    """按 ID 获取订单，不存在时抛 404（内部函数）。

    Args:
        db: 请求级异步数据库会话。
        order_id: 订单 ID。

    Returns:
        Order: 订单 ORM 对象。

    Raises:
        HTTPException: 404 —— 订单不存在。
    """
    order = await db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="订单不存在")
    return order


@router.get("", response_model=PageResult[AdminOrderItem])
async def list_orders(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    status_filter: Optional[str] = Query(default=None, alias="status",
                                         description="状态过滤：pending/paid/closed/refunded"),
    search: Optional[str] = Query(default=None,
                                  description="搜索：订单号精确匹配或用户名模糊匹配"),
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> PageResult[AdminOrderItem]:
    """分页查询订单列表（创建时间倒序，联用户名）。

    搜索规则：search 先按订单号精确匹配；同时按用户名模糊匹配，二者取并集。

    Args:
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        status_filter: 订单状态过滤（非法返回 400）。
        search: 搜索关键字（订单号精确 / 用户名模糊）。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        PageResult[AdminOrderItem]: 订单分页列表。

    Raises:
        HTTPException: 400 —— status 取值非法。
    """
    if status_filter is not None and status_filter not in _ALLOWED_STATUS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"status 仅支持 {'/'.join(_ALLOWED_STATUS)}",
        )
    page, page_size = normalize_pagination(page, page_size)

    conditions = []
    if status_filter:
        conditions.append(Order.status == status_filter)
    if search:
        like = f"%{search}%"
        # 订单号精确 或 用户名模糊（子查询命中用户 id 集合）
        username_user_ids = select(User.id).where(User.username.ilike(like)).scalar_subquery()
        conditions.append((Order.order_no == search) | Order.user_id.in_(username_user_ids))

    base = select(Order, User.username.label("username")).outerjoin(
        User, User.id == Order.user_id
    ).where(*conditions)
    total = await db.scalar(select(func.count()).select_from(Order).where(*conditions)) or 0
    rows = await db.execute(
        base.order_by(Order.created_at.desc(), Order.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = [
        AdminOrderItem(
            id=order.id,
            order_no=order.order_no,
            user_id=order.user_id,
            username=username,
            product_type=order.product_type,
            amount=float(order.amount),
            points=order.points,
            pay_channel=order.pay_channel,
            status=order.status,
            paid_at=order.paid_at,
            created_at=order.created_at,
        )
        for order, username in rows.all()
    ]
    return PageResult(total=int(total), page=page, page_size=page_size, items=items)


@router.post("/{order_id}/complete", response_model=AdminOrderItem)
async def complete_order(
    order_id: int,
    req: OrderActionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    operator: User = Depends(require_operator),
) -> AdminOrderItem:
    """人工完结订单：pending → paid，并幂等发放对应积分。

    与用户端模拟支付共用幂等键 f"order_{order.id}"（RD-55）——若积分已因
    支付发放过，grant_points 直接短路，不会重复入账。

    Args:
        order_id: 订单 ID。
        req: 操作请求（reason 可选，落审计日志）。
        request: 当前请求（用于记录操作 IP）。
        db: 请求级异步数据库会话。
        operator: 当前运营方。

    Returns:
        AdminOrderItem: 完结后的订单信息。

    Raises:
        HTTPException: 404 —— 订单不存在；409 —— 订单状态不允许完结。
    """
    order = await _get_order_or_404(db, order_id)
    if order.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"订单状态为 {order.status}，仅 pending 订单可人工完结",
        )

    order.status = "paid"
    order.paid_at = datetime.now(timezone.utc)
    order.pay_channel = order.pay_channel or "admin"
    await grant_points(
        db,
        user_id=order.user_id,
        amount=order.points,
        ref_order_id=order.id,
        idempotency_key=f"order_{order.id}",
        remark=f"订单 {order.order_no} 人工完结发放",
        tx_type="recharge",
    )
    db.add(OperationLog(
        user_id=operator.id,
        project_id=None,
        action="admin_complete_order",
        detail={"order_id": order.id, "order_no": order.order_no, "reason": req.reason},
        ip=_get_client_ip(request),
        risk_level="normal",
    ))
    await db.commit()
    await db.refresh(order)

    username = await db.scalar(select(User.username).where(User.id == order.user_id))
    return AdminOrderItem(
        id=order.id,
        order_no=order.order_no,
        user_id=order.user_id,
        username=username,
        product_type=order.product_type,
        amount=float(order.amount),
        points=order.points,
        pay_channel=order.pay_channel,
        status=order.status,
        paid_at=order.paid_at,
        created_at=order.created_at,
    )


@router.post("/{order_id}/close", response_model=AdminOrderItem)
async def close_order(
    order_id: int,
    req: OrderActionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    operator: User = Depends(require_operator),
) -> AdminOrderItem:
    """人工关闭订单：pending → closed（不发放积分）。

    Args:
        order_id: 订单 ID。
        req: 操作请求（reason 可选，落审计日志）。
        request: 当前请求（用于记录操作 IP）。
        db: 请求级异步数据库会话。
        operator: 当前运营方。

    Returns:
        AdminOrderItem: 关闭后的订单信息。

    Raises:
        HTTPException: 404 —— 订单不存在；409 —— 订单状态不允许关闭。
    """
    order = await _get_order_or_404(db, order_id)
    if order.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"订单状态为 {order.status}，仅 pending 订单可关闭",
        )

    order.status = "closed"
    db.add(OperationLog(
        user_id=operator.id,
        project_id=None,
        action="admin_close_order",
        detail={"order_id": order.id, "order_no": order.order_no, "reason": req.reason},
        ip=_get_client_ip(request),
        risk_level="normal",
    ))
    await db.commit()
    await db.refresh(order)

    username = await db.scalar(select(User.username).where(User.id == order.user_id))
    return AdminOrderItem(
        id=order.id,
        order_no=order.order_no,
        user_id=order.user_id,
        username=username,
        product_type=order.product_type,
        amount=float(order.amount),
        points=order.points,
        pay_channel=order.pay_channel,
        status=order.status,
        paid_at=order.paid_at,
        created_at=order.created_at,
    )


@router.post("/{order_id}/refund", response_model=RefundResponse)
async def refund_order(
    order_id: int,
    req: RefundRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    operator: User = Depends(require_operator),
) -> RefundResponse:
    """订单退款：paid → refunded，回收订单对应积分。

    回收规则：行锁钱包后直接扣减可用余额（type='consume' 流水，amount 为负）；
    余额不足时扣至 0 并在流水 remark 注明"部分回收"。高危操作审计
    （action='admin_refund_order'，risk_level='high'）。二次确认由前端负责。

    Args:
        order_id: 订单 ID。
        req: 退款请求（reason 必填）。
        request: 当前请求（用于记录操作 IP）。
        db: 请求级异步数据库会话。
        operator: 当前运营方。

    Returns:
        RefundResponse: 处理结果与实际回收积分数。

    Raises:
        HTTPException: 404 —— 订单不存在；409 —— 订单状态不允许退款。
    """
    order = await _get_order_or_404(db, order_id)
    if order.status != "paid":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"订单状态为 {order.status}，仅 paid 订单可退款",
        )
    if order.points <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="订单无对应积分，无法执行积分回收"
        )

    # 行锁钱包执行回收
    wallet = (
        await db.execute(select(Wallet).where(Wallet.user_id == order.user_id).with_for_update())
    ).scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户钱包不存在")

    balance = wallet.points_balance
    refunded_points = min(order.points, balance)  # 余额不足时部分回收
    partial = refunded_points < order.points
    wallet.points_balance -= refunded_points
    wallet.version += 1
    db.add(WalletTransaction(
        user_id=order.user_id,
        type="consume",
        amount=-refunded_points,
        bear="platform",
        ref_order_id=order.id,
        # 幂等键以订单为维度：重复退款请求已被订单状态检查拦截（409）
        idempotency_key=f"refund_order_{order.id}_{uuid.uuid4().hex[:8]}",
        remark=(
            f"订单 {order.order_no} 退款回收（{req.reason}）"
            + ("；余额不足，已部分回收" if partial else "")
        ),
    ))

    order.status = "refunded"
    db.add(OperationLog(
        user_id=operator.id,
        project_id=None,
        action="admin_refund_order",
        detail={
            "order_id": order.id,
            "order_no": order.order_no,
            "reason": req.reason,
            "order_points": order.points,
            "refunded_points": refunded_points,
            "partial": partial,
        },
        ip=_get_client_ip(request),
        risk_level="high",  # 高危资金操作（RD-56）
    ))
    await db.commit()

    return RefundResponse(
        message=(
            f"订单 {order.order_no} 已退款，回收积分 {refunded_points}"
            + ("（部分回收：余额不足）" if partial else "")
        ),
        order_id=order.id,
        refunded_points=refunded_points,
    )
