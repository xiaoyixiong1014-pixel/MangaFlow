"""认证路由模块（文件级说明）。

实现注册 / 登录 / 刷新令牌 / 当前用户信息接口：
- POST /api/auth/register：用户名/邮箱唯一校验 → 建用户（role='user'）+ 钱包
  （初始 5000 积分，写一条 grant 流水，幂等键 reg_{user_id}）→ 签发令牌对
- POST /api/auth/login：username 可填用户名或邮箱，校验 status=1，更新最近登录时间
- POST /api/auth/refresh：校验 refresh 类型令牌，签发新 access（refresh 不换新）
- GET /api/auth/me：当前用户信息 + 钱包汇总

JWT payload 约定：sub=str(user.id)，role=user.role（'user'/'operator'），type，exp。
"""

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.models import User, Wallet, WalletTransaction
from app.schemas.auth import (
    AuthMeResponse,
    LoginRequest,
    RefreshRequest,
    RefreshResponse,
    RegisterRequest,
    TokenResponse,
)
from app.schemas.user import UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 注册赠送积分（开发环境标准额度）
_REGISTER_GRANT_POINTS = 5000

# 邮箱格式宽松校验（本地@域名）
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# 新用户钱包初始值
_WALLET_INITIAL = {"points_balance": _REGISTER_GRANT_POINTS, "frozen_points": 0, "version": 0}


def _issue_token_response(user: User) -> TokenResponse:
    """为用户签发 access/refresh 令牌对并组装响应（内部函数）。

    Args:
        user: 用户 ORM 对象。

    Returns:
        TokenResponse: 含 access_token / refresh_token / token_type / user 的响应体。
    """
    return TokenResponse(
        access_token=create_access_token(user.id, user.role),
        refresh_token=create_refresh_token(user.id, user.role),
        token_type="bearer",
        user=UserOut.model_validate(user),
    )


@router.post("/register", response_model=TokenResponse)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    """用户注册：创建用户与钱包并赠送初始积分，随即签发令牌对。

    Args:
        req: 注册请求（username/email/password）。
        db: 请求级异步数据库会话。

    Returns:
        TokenResponse: 令牌对与新建用户信息。

    Raises:
        HTTPException: 400 —— 邮箱格式非法；409 —— 用户名或邮箱已被占用。
    """
    username = req.username.strip()
    email = req.email.strip() if req.email else None
    if email and not _EMAIL_RE.match(email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="邮箱格式不正确")

    # 唯一性校验：用户名 / 邮箱
    conflict = await db.execute(
        select(User).where(or_(User.username == username, User.email == email if email else False))
    )
    existed = conflict.scalars().first()
    if existed is not None:
        field = "用户名" if existed.username == username else "邮箱"
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{field}已被占用")

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(req.password),
        role="user",
        status=1,
    )
    db.add(user)
    await db.flush()  # 取自增 ID 供钱包与流水外键使用

    wallet = Wallet(user_id=user.id, **_WALLET_INITIAL)
    db.add(wallet)
    # 注册赠送流水（幂等键含用户 ID，天然唯一）
    db.add(WalletTransaction(
        user_id=user.id,
        type="grant",
        amount=_REGISTER_GRANT_POINTS,  # 符号约定：grant 为正
        bear="platform",
        ref_task_id=None,
        ref_order_id=None,
        idempotency_key=f"reg_{user.id}",
        remark="新用户注册赠送积分",
    ))
    await db.commit()
    return _issue_token_response(user)


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    """用户登录：username 支持用户名或邮箱，校验通过后更新最近登录时间并签发令牌。

    Args:
        req: 登录请求（username/password）。
        db: 请求级异步数据库会话。

    Returns:
        TokenResponse: 令牌对与用户信息。

    Raises:
        HTTPException: 401 —— 用户不存在或密码错误；403 —— 账号已被禁用。
    """
    account = req.username.strip()
    result = await db.execute(
        select(User).where(or_(User.username == account, User.email == account))
    )
    user = result.scalar_one_or_none()
    if user is None or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    if user.status != 1:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被禁用，请联系管理员")

    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    return _issue_token_response(user)


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(req: RefreshRequest, db: AsyncSession = Depends(get_db)) -> RefreshResponse:
    """刷新访问令牌：校验 refresh 类型令牌后签发新 access（refresh 不换新）。

    Args:
        req: 刷新请求（refresh_token）。
        db: 请求级异步数据库会话（用于校验用户仍然有效）。

    Returns:
        RefreshResponse: 新 access_token 与原样返回的 refresh_token。

    Raises:
        HTTPException: 401 —— 刷新令牌非法/过期/类型不符，或用户已不存在、被禁用。
    """
    payload = decode_token(req.refresh_token, expected_type="refresh")
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的刷新令牌") from exc
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or user.status != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已被禁用")
    return RefreshResponse(
        access_token=create_access_token(user.id, user.role),
        refresh_token=req.refresh_token,
        token_type="bearer",
    )


@router.get("/me", response_model=AuthMeResponse)
async def me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AuthMeResponse:
    """获取当前登录用户信息与钱包汇总。

    Args:
        user: 由令牌解析出的当前用户。
        db: 请求级异步数据库会话。

    Returns:
        AuthMeResponse: 用户信息 + 钱包摘要（可用积分/冻结积分/版本号）。

    Raises:
        HTTPException: 401 —— 未认证（由 get_current_user 抛出）；404 —— 钱包缺失。
    """
    wallet = (
        await db.execute(select(Wallet).where(Wallet.user_id == user.id))
    ).scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户钱包不存在")
    return AuthMeResponse(
        user=UserOut.model_validate(user),
        wallet={
            "points_balance": wallet.points_balance,
            "frozen_points": wallet.frozen_points,
            "version": wallet.version,
        },
    )
