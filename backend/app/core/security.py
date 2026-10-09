"""安全模块（文件级说明）。

提供：
- bcrypt 密码哈希与校验（hash_password / verify_password）
- JWT 访问/刷新令牌签发与解析（create_access_token / create_refresh_token / decode_token）
- FastAPI 依赖 get_current_user（当前登录用户）与 require_operator（运营方权限）

注意：bcrypt 算法仅取密码前 72 字节，本模块在哈希/校验前统一先按
UTF-8 编码并显式截断到 72 字节，避免不同 bcrypt 版本抛出异常或行为不一致。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models.user import User

# Bearer 令牌提取器：auto_error=False 表示未携带 Authorization 头时返回 None 而非直接 401
_bearer_scheme = HTTPBearer(auto_error=False)

# bcrypt 最大输入长度（字节）
_BCRYPT_MAX_BYTES = 72


def _normalize_password(password: str) -> bytes:
    """将明文密码规范化为 bcrypt 可接受的字节串。

    Args:
        password: 明文密码。

    Returns:
        bytes: UTF-8 编码后截断至 72 字节的密码字节串。
    """
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """对明文密码进行 bcrypt 哈希。

    Args:
        password: 明文密码。

    Returns:
        str: bcrypt 哈希字符串（含盐，可直接入库 password_hash 字段）。

    Raises:
        ValueError: 密码为空或 bcrypt 内部错误时抛出。
    """
    if not password:
        raise ValueError("密码不能为空")
    return bcrypt.hashpw(_normalize_password(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """校验明文密码与 bcrypt 哈希是否匹配。

    Args:
        password: 待校验的明文密码。
        hashed: 数据库中存储的 bcrypt 哈希。

    Returns:
        bool: 匹配返回 True；不匹配或哈希格式非法返回 False。
    """
    try:
        return bcrypt.checkpw(_normalize_password(password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _create_token(user_id: int, role: str, token_type: str, expires_delta: timedelta) -> str:
    """签发 JWT 的内部通用函数。

    Args:
        user_id: 用户 ID。
        role: 用户角色（'user' / 'operator'）。
        token_type: 令牌类型（'access' / 'refresh'）。
        expires_delta: 有效时长。

    Returns:
        str: 签名后的 JWT 字符串。

    Raises:
        jwt.PyJWTError: 签名过程异常时向上抛出。
    """
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: int, role: str) -> str:
    """签发访问令牌（access token）。

    Args:
        user_id: 用户 ID。
        role: 用户角色。

    Returns:
        str: JWT 字符串，有效期 access_token_expire_minutes。
    """
    return _create_token(user_id, role, "access", timedelta(minutes=settings.access_token_expire_minutes))


def create_refresh_token(user_id: int, role: str) -> str:
    """签发刷新令牌（refresh token）。

    Args:
        user_id: 用户 ID。
        role: 用户角色。

    Returns:
        str: JWT 字符串，有效期 refresh_token_expire_days。
    """
    return _create_token(user_id, role, "refresh", timedelta(days=settings.refresh_token_expire_days))


def decode_token(token: str, expected_type: str = "access") -> Dict[str, Any]:
    """解析并校验 JWT。

    Args:
        token: JWT 字符串。
        expected_type: 期望的令牌类型（'access' / 'refresh'），不符则拒绝。

    Returns:
        Dict[str, Any]: JWT payload（含 sub / role / type / exp）。

    Raises:
        HTTPException: 401 —— 令牌过期、签名非法或类型不匹配。
    """
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if payload.get("type") != expected_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌类型不匹配",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI 依赖：解析 Bearer 令牌并返回当前登录用户（ORM 对象）。

    Args:
        credentials: HTTPBearer 自动提取的凭据；未携带时为 None。
        db: 请求级异步数据库会话。

    Returns:
        User: 状态正常（status=1）的用户 ORM 对象。

    Raises:
        HTTPException: 401 —— 未携带令牌、令牌非法/过期，或用户不存在、被禁用。
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或缺少认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(credentials.credentials, expected_type="access")
    user_id_raw = payload.get("sub")
    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证令牌",
        ) from exc
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    if user.status != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号已被禁用")
    return user


async def require_operator(user: User = Depends(get_current_user)) -> User:
    """FastAPI 依赖：要求当前用户为运营方（operator）角色。

    Args:
        user: 由 get_current_user 注入的当前用户。

    Returns:
        User: 校验通过的用户 ORM 对象。

    Raises:
        HTTPException: 403 —— 当前用户角色不是 operator。
    """
    if user.role != "operator":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权限执行该操作")
    return user
