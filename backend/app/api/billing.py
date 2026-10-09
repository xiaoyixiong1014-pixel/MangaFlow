"""计费路由模块（文件级说明）。

实现钱包查询 / 积分流水 / 套餐与订单 / 模拟支付 / 月度消耗统计：
- GET  /api/billing/wallet               钱包概要（含低余额标记）
- GET  /api/billing/wallet/summary       近 6 个月 consume 汇总（前端图表用）
- GET  /api/billing/transactions         积分流水分页（倒序，可按类型过滤）
- GET  /api/billing/packages             积分套餐常量列表
- POST /api/billing/orders               创建订单（pending）
- POST /api/billing/orders/{no}/pay      模拟支付成功 → 幂等发放积分
- GET  /api/billing/orders               我的订单分页

注：积分套餐定价以代码常量承载（不建套餐表，见 billing.py 占位说明）。
"""

import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Order, User, Wallet, WalletTransaction
from app.schemas.billing import (
    MonthConsumedOut,
    OrderCreateRequest,
    OrderOut,
    PackageOut,
    PayResponse,
    TransactionOut,
    WalletOut,
)
from app.schemas.common import PageResult
from app.services import normalize_pagination
from app.services.billing_service import grant_points

router = APIRouter(prefix="/api/billing", tags=["billing"])

# 低余额阈值（前端据此提示充值）
_LOW_BALANCE_THRESHOLD = 1000

# 积分套餐定价常量（运营定价直接写在代码中；price 单位为人民币元，
# p_sub 为订阅型套餐，订阅期内每月发放 30000 积分）
_PACKAGES: List[Dict[str, Any]] = [
    {"id": "p_small", "name": "基础包", "points": 1000, "price": 9.9, "subscription": False},
    {"id": "p_mid", "name": "标准包", "points": 5500, "price": 49.9, "subscription": False},
    {"id": "p_large", "name": "专业包", "points": 12000, "price": 99.9, "subscription": False},
    {"id": "p_sub", "name": "订阅·月度", "points": 30000, "price": 199.0, "subscription": True},
]


def _find_package(package_id: str) -> Optional[Dict[str, Any]]:
    """按 ID 查找套餐常量（内部函数）。

    Args:
        package_id: 套餐标识。

    Returns:
        Optional[Dict[str, Any]]: 套餐字典；不存在时为 None。
    """
    return next((p for p in _PACKAGES if p["id"] == package_id), None)


async def _get_wallet(db: AsyncSession, user_id: int) -> Wallet:
    """获取当前用户钱包（内部函数；不存在则创建空钱包兜底）。

    Args:
        db: 请求级异步数据库会话。
        user_id: 用户 ID。

    Returns:
        Wallet: 钱包 ORM 对象。
    """
    wallet = (await db.execute(select(Wallet).where(Wallet.user_id == user_id))).scalar_one_or_none()
    if wallet is None:
        wallet = Wallet(user_id=user_id, points_balance=0, frozen_points=0, version=0)
        db.add(wallet)
        await db.commit()
    return wallet


@router.get("/wallet", response_model=WalletOut)
async def get_wallet(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WalletOut:
    """查询当前用户钱包概要。

    Args:
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        WalletOut: 可用积分 / 冻结积分 / 低余额标记（可用 < 1000）/ 版本号。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）。
    """
    wallet = await _get_wallet(db, user.id)
    return WalletOut(
        points_balance=wallet.points_balance,
        frozen_points=wallet.frozen_points,
        low_balance=wallet.points_balance < _LOW_BALANCE_THRESHOLD,
        version=wallet.version,
    )


@router.get("/wallet/summary", response_model=List[MonthConsumedOut])
async def wallet_summary(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[MonthConsumedOut]:
    """统计近 6 个月每月积分消耗（consume 流水汇总，用于前端图表）。

    月份以 UTC 计算，缺失月份补 0，保证恒返回 6 个数据点。

    Args:
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        List[MonthConsumedOut]: [{"month": "YYYY-MM", "consumed": n}, ...]（升序）。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）。
    """
    now = datetime.now(timezone.utc)
    # 从当前月回推 6 个自然月（升序输出）
    months = []
    y, m = now.year, now.month
    for _ in range(6):
        months.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()

    start = datetime(months[0][0], months[0][1], 1, tzinfo=timezone.utc)
    rows = await db.execute(
        select(
            func.to_char(WalletTransaction.created_at, "YYYY-MM"),
            func.coalesce(func.sum(-WalletTransaction.amount), 0),
        ).where(
            WalletTransaction.user_id == user.id,
            WalletTransaction.type == "consume",
            WalletTransaction.created_at >= start,
        )
        # 注：GROUP BY 1（输出列序号）——若按表达式分组，格式串会成为独立绑定参数，
        # PostgreSQL 无法在计划期做表达式匹配，会报 "created_at 必须出现在 GROUP BY 中"
        .group_by(literal_column("1"))
    )
    totals = {month_key: int(value) for month_key, value in rows.all()}
    return [
        MonthConsumedOut(month=f"{y:04d}-{m:02d}", consumed=totals.get(f"{y:04d}-{m:02d}", 0))
        for y, m in months
    ]


@router.get("/transactions", response_model=PageResult[TransactionOut])
async def list_transactions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    type_filter: Optional[str] = Query(default=None, alias="type"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PageResult[TransactionOut]:
    """分页查询当前用户积分流水（按创建时间倒序，可按类型过滤）。

    Args:
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        type_filter: 流水类型过滤（freeze/consume/refund/recharge/grant；非法返回 400）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PageResult[TransactionOut]: 流水分页列表。

    Raises:
        HTTPException: 400 —— type 取值非法；401 —— 未认证。
    """
    allowed_types = ("freeze", "consume", "refund", "recharge", "grant")
    if type_filter is not None and type_filter not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"type 仅支持 {'/'.join(allowed_types)}"
        )
    page, page_size = normalize_pagination(page, page_size)
    conditions = [WalletTransaction.user_id == user.id]
    if type_filter:
        conditions.append(WalletTransaction.type == type_filter)
    total = await db.scalar(
        select(func.count()).select_from(WalletTransaction).where(*conditions)
    )
    rows = await db.execute(
        select(WalletTransaction).where(*conditions)
        .order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return PageResult(
        total=total or 0, page=page, page_size=page_size,
        items=[TransactionOut.model_validate(r) for r in rows.scalars().all()],
    )


@router.get("/packages", response_model=List[PackageOut])
async def list_packages() -> List[PackageOut]:
    """查询积分套餐常量列表（定价见模块注释，不落库）。

    Returns:
        List[PackageOut]: 全部积分套餐。

    Raises:
        无。
    """
    return [PackageOut(**p) for p in _PACKAGES]


@router.post("/orders", response_model=OrderOut)
async def create_order(
    req: OrderCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrderOut:
    """创建积分购买订单（状态 pending，等待支付）。

    订单号规则：MF + UTC 时间戳（14 位）+ 6 位随机数。

    Args:
        req: 订单创建请求（package_id）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        OrderOut: 新建订单信息。

    Raises:
        HTTPException: 400 —— 套餐标识无效；401 —— 未认证。
    """
    package = _find_package(req.package_id)
    if package is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的积分套餐")
    order_no = f"MF{time.strftime('%Y%m%d%H%M%S', time.gmtime())}{random.randint(0, 999999):06d}"
    order = Order(
        user_id=user.id,
        order_no=order_no,
        product_type="subscription" if package["subscription"] else "points",
        amount=package["price"],
        points=package["points"],
        pay_channel=None,
        status="pending",
    )
    db.add(order)
    await db.commit()
    return OrderOut.model_validate(order)


@router.post("/orders/{order_no}/pay", response_model=PayResponse)
async def pay_order(
    order_no: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PayResponse:
    """模拟支付成功：置订单为 paid 并幂等发放对应积分。

    幂等保障（双保险）：
    1. 已支付订单直接返回当前余额，不重复发放；
    2. grant_points 以 f"order_{order.id}" 为幂等键，同键流水已存在时不重复入账。

    Args:
        order_no: 订单号（路径参数）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PayResponse: 支付后的订单信息与最新可用积分余额。

    Raises:
        HTTPException: 404 —— 订单不存在或非本人订单；
            409 —— 订单状态不允许支付（closed/refunded）。
    """
    order = (
        await db.execute(select(Order).where(Order.order_no == order_no, Order.user_id == user.id))
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="订单不存在")

    wallet = await _get_wallet(db, user.id)
    if order.status == "paid":
        # 幂等：已支付直接返回，不重复发放
        return PayResponse(order=OrderOut.model_validate(order), points_balance=wallet.points_balance)
    if order.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="订单状态不允许支付")

    order.status = "paid"
    order.paid_at = datetime.now(timezone.utc)
    order.pay_channel = "mock"  # 本阶段为模拟支付，无真实支付网关
    # 与订单状态变更同事务提交；幂等键以订单 ID 构造（RD-55）
    await grant_points(
        db,
        user_id=user.id,
        amount=order.points,
        ref_order_id=order.id,
        idempotency_key=f"order_{order.id}",
        remark=f"订单 {order.order_no} 充值到账",
        tx_type="recharge",
    )
    wallet = await _get_wallet(db, user.id)  # 发放后重读最新余额
    return PayResponse(order=OrderOut.model_validate(order), points_balance=wallet.points_balance)


@router.get("/orders", response_model=PageResult[OrderOut])
async def list_orders(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PageResult[OrderOut]:
    """分页查询当前用户订单（按创建时间倒序，可按状态过滤）。

    Args:
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        status_filter: 订单状态过滤（pending/paid/closed/refunded；非法返回 400）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PageResult[OrderOut]: 订单分页列表。

    Raises:
        HTTPException: 400 —— status 取值非法；401 —— 未认证。
    """
    allowed_status = ("pending", "paid", "closed", "refunded")
    if status_filter is not None and status_filter not in allowed_status:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"status 仅支持 {'/'.join(allowed_status)}"
        )
    page, page_size = normalize_pagination(page, page_size)
    conditions = [Order.user_id == user.id]
    if status_filter:
        conditions.append(Order.status == status_filter)
    total = await db.scalar(select(func.count()).select_from(Order).where(*conditions))
    rows = await db.execute(
        select(Order).where(*conditions)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return PageResult(
        total=total or 0, page=page, page_size=page_size,
        items=[OrderOut.model_validate(r) for r in rows.scalars().all()],
    )
