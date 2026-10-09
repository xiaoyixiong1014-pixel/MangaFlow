"""管理端用户管理路由模块（文件级说明）。

实现运营方的用户运营接口：
- GET  /api/admin/users                 用户分页列表（含钱包余额与项目数，支持搜索/状态过滤）
- GET  /api/admin/users/{user_id}       用户详情（钱包 + 近 10 条操作日志 + 近 5 个项目）
- POST /api/admin/users/{user_id}/points  积分调整（正数发放 / 负数扣减，高危审计）
- POST /api/admin/users/{user_id}/status  启用/禁用用户（高危审计）

说明：operation_logs.user_id 语义为"操作人"，管理端操作统一记录管理员 ID，
目标用户等信息写入 detail（JSONB）。全部接口要求运营方角色，普通用户 403。
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_operator
from app.models import OperationLog, Project, User, Wallet, WalletTransaction
from app.schemas.admin import (
    AdminUserDetail,
    AdminUserItem,
    MessageOut,
    OperationLogBrief,
    PointsAdjustRequest,
    PointsAdjustResponse,
    ProjectBrief,
    StatusToggleRequest,
    WalletBrief,
)
from app.schemas.common import PageResult
from app.services import normalize_pagination

router = APIRouter(
    prefix="/api/admin/users",
    tags=["admin-users"],
    dependencies=[Depends(require_operator)],  # 路由级鉴权：仅运营方可访问，普通用户 403
)


async def _load_wallet_map(db: AsyncSession, user_ids: List[int]) -> dict:
    """批量加载用户钱包（内部函数）。

    Args:
        db: 请求级异步数据库会话。
        user_ids: 用户 ID 列表。

    Returns:
        dict: {user_id: Wallet} 映射。
    """
    if not user_ids:
        return {}
    rows = await db.execute(select(Wallet).where(Wallet.user_id.in_(user_ids)))
    return {w.user_id: w for w in rows.scalars().all()}


async def _load_project_count_map(db: AsyncSession, user_ids: List[int]) -> dict:
    """批量统计用户项目数（内部函数）。

    Args:
        db: 请求级异步数据库会话。
        user_ids: 用户 ID 列表。

    Returns:
        dict: {user_id: project_count} 映射。
    """
    if not user_ids:
        return {}
    rows = await db.execute(
        select(Project.user_id, func.count(Project.id))
        .where(Project.user_id.in_(user_ids))
        .group_by(Project.user_id)
    )
    return {uid: cnt for uid, cnt in rows.all()}


def _get_client_ip(request: Request) -> Optional[str]:
    """提取客户端 IP（内部函数）。

    Args:
        request: 当前请求对象。

    Returns:
        Optional[str]: 客户端 IP；无法获取时为 None。
    """
    return request.client.host if request.client else None


@router.get("", response_model=PageResult[AdminUserItem])
async def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    search: Optional[str] = Query(default=None, description="按用户名/邮箱模糊搜索"),
    status_filter: Optional[int] = Query(default=None, alias="status", ge=0, le=1,
                                         description="状态过滤：0 禁用 / 1 正常"),
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> PageResult[AdminUserItem]:
    """分页查询用户列表（注册时间倒序），附钱包余额与项目数。

    Args:
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        search: 搜索关键字（匹配用户名或邮箱，模糊）。
        status_filter: 状态过滤（0/1）。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        PageResult[AdminUserItem]: 用户分页列表。

    Raises:
        无：查询异常向上抛出由全局处理器兜底。
    """
    page, page_size = normalize_pagination(page, page_size)
    conditions = []
    if search:
        like = f"%{search}%"
        conditions.append(User.username.ilike(like) | User.email.ilike(like))
    if status_filter is not None:
        conditions.append(User.status == status_filter)

    total = await db.scalar(select(func.count()).select_from(User).where(*conditions)) or 0
    rows = await db.execute(
        select(User).where(*conditions)
        .order_by(User.created_at.desc(), User.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    users = rows.scalars().all()
    user_ids = [u.id for u in users]

    wallet_map = await _load_wallet_map(db, user_ids)
    project_map = await _load_project_count_map(db, user_ids)

    items = [
        AdminUserItem(
            id=u.id,
            username=u.username,
            email=u.email,
            role=u.role,
            vip_level=u.vip_level,
            status=u.status,
            points_balance=wallet_map[u.id].points_balance if u.id in wallet_map else 0,
            frozen_points=wallet_map[u.id].frozen_points if u.id in wallet_map else 0,
            project_count=int(project_map.get(u.id, 0)),
            last_login_at=u.last_login_at,
            created_at=u.created_at,
        )
        for u in users
    ]
    return PageResult(total=int(total), page=page, page_size=page_size, items=items)


@router.get("/{user_id}", response_model=AdminUserDetail)
async def get_user_detail(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> AdminUserDetail:
    """查询用户详情：基础信息 + 钱包 + 近 10 条操作日志 + 近 5 个项目。

    Args:
        user_id: 目标用户 ID。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        AdminUserDetail: 用户详情。

    Raises:
        HTTPException: 404 —— 用户不存在。
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    wallet = (
        await db.execute(select(Wallet).where(Wallet.user_id == user_id))
    ).scalar_one_or_none()
    # 日志口径：user_id 为"操作人"（管理员操作记管理员 ID，目标用户写 detail），
    # 因此详情页同时返回"该用户作为操作人"与"detail.target_user_id 指向该用户"的日志
    logs = (
        await db.execute(
            select(OperationLog)
            .where(
                or_(
                    OperationLog.user_id == user_id,
                    OperationLog.detail["target_user_id"].astext == str(user_id),
                )
            )
            .order_by(OperationLog.created_at.desc(), OperationLog.id.desc()).limit(10)
        )
    ).scalars().all()
    projects = (
        await db.execute(
            select(Project).where(Project.user_id == user_id)
            .order_by(Project.created_at.desc(), Project.id.desc()).limit(5)
        )
    ).scalars().all()

    return AdminUserDetail(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        vip_level=user.vip_level,
        status=user.status,
        wallet=WalletBrief.model_validate(wallet) if wallet else None,
        recent_logs=[OperationLogBrief.model_validate(l) for l in logs],
        recent_projects=[ProjectBrief.model_validate(p) for p in projects],
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


async def _ensure_wallet(db: AsyncSession, user_id: int) -> Wallet:
    """获取目标用户钱包，不存在时创建零余额钱包兜底（内部函数）。

    Args:
        db: 请求级异步数据库会话。
        user_id: 用户 ID。

    Returns:
        Wallet: 钱包 ORM 对象。
    """
    wallet = (
        await db.execute(select(Wallet).where(Wallet.user_id == user_id).with_for_update())
    ).scalar_one_or_none()
    if wallet is None:
        wallet = Wallet(user_id=user_id, points_balance=0, frozen_points=0, version=0)
        db.add(wallet)
        await db.flush()  # 取主键，保证后续引用有效
    return wallet


@router.post("/{user_id}/points", response_model=PointsAdjustResponse)
async def adjust_points(
    user_id: int,
    req: PointsAdjustRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    operator: User = Depends(require_operator),
) -> PointsAdjustResponse:
    """管理员调整用户积分（amount 正数发放 / 负数扣减可用余额）。

    - 发放：钱包可用余额直接增加，写 type='grant' 流水；
    - 扣减：可用余额不足时返回 400，写 type='consume' 流水（amount 为负）；
    - 均写 operation_logs（action='admin_adjust_points'，risk_level='high'），
      user_id 记操作管理员，target_user_id/amount/reason 记入 detail。

    Args:
        user_id: 目标用户 ID。
        req: 调整请求（amount 带符号整数 + reason 必填）。
        request: 当前请求（用于记录操作 IP）。
        db: 请求级异步数据库会话。
        operator: 当前运营方。

    Returns:
        PointsAdjustResponse: 调整后的钱包余额。

    Raises:
        HTTPException: 400 —— amount 为 0 或扣减时余额不足；
            404 —— 目标用户不存在。
    """
    if req.amount == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="调整积分不能为 0")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    wallet = await _ensure_wallet(db, user_id)
    if req.amount > 0:
        # 发放：可用余额直接增加（平台赠送口径）
        wallet.points_balance += req.amount
        wallet.version += 1
        db.add(WalletTransaction(
            user_id=user_id,
            type="grant",
            amount=req.amount,
            bear="platform",
            idempotency_key=f"admin_adj_{uuid.uuid4().hex}",
            remark=f"管理员调整：{req.reason}",
        ))
    else:
        # 扣减：可用余额不足则拒绝（400）
        deduct = -req.amount
        if wallet.points_balance < deduct:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"可用积分不足（当前 {wallet.points_balance}，需扣 {deduct}）",
            )
        wallet.points_balance -= deduct
        wallet.version += 1
        db.add(WalletTransaction(
            user_id=user_id,
            type="consume",
            amount=req.amount,  # 负数
            bear="platform",
            idempotency_key=f"admin_adj_{uuid.uuid4().hex}",
            remark=f"管理员调整：{req.reason}",
        ))

    # 高危操作审计：user_id = 操作管理员，目标用户写入 detail
    db.add(OperationLog(
        user_id=operator.id,
        project_id=None,
        action="admin_adjust_points",
        detail={
            "target_user_id": user_id,
            "target_username": user.username,
            "amount": req.amount,
            "reason": req.reason,
        },
        ip=_get_client_ip(request),
        risk_level="high",
    ))
    await db.commit()

    return PointsAdjustResponse(
        points_balance=wallet.points_balance, frozen_points=wallet.frozen_points
    )


@router.post("/{user_id}/status", response_model=MessageOut)
async def toggle_user_status(
    user_id: int,
    req: StatusToggleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    operator: User = Depends(require_operator),
) -> MessageOut:
    """管理员启用/禁用用户账号（禁用后该用户所有令牌立即失效）。

    写 operation_logs（action='admin_toggle_status'，risk_level='high'），
    user_id 记操作管理员，目标用户/目标状态/原因记入 detail。

    Args:
        user_id: 目标用户 ID。
        req: 状态切换请求（status 0/1 + reason 必填）。
        request: 当前请求（用于记录操作 IP）。
        db: 请求级异步数据库会话。
        operator: 当前运营方。

    Returns:
        MessageOut: 操作结果消息。

    Raises:
        HTTPException: 400 —— 目标状态非法或与当前状态一致；
            404 —— 目标用户不存在。
    """
    if req.status not in (0, 1):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="status 仅支持 0 或 1")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if user.status == req.status:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"用户当前已是该状态（{req.status}）",
        )

    user.status = req.status
    db.add(OperationLog(
        user_id=operator.id,
        project_id=None,
        action="admin_toggle_status",
        detail={
            "target_user_id": user_id,
            "target_username": user.username,
            "status": req.status,
            "reason": req.reason,
        },
        ip=_get_client_ip(request),
        risk_level="high",
    ))
    await db.commit()

    action_text = "启用" if req.status == 1 else "禁用"
    return MessageOut(message=f"用户 {user.username} 已{action_text}")
