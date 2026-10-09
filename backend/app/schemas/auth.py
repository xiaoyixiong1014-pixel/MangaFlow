"""认证 Pydantic Schema 模块（文件级说明）。

定义注册 / 登录 / 刷新令牌 / 当前用户信息的出入参模型。
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.user import UserOut


class RegisterRequest(BaseModel):
    """注册请求。

    Attributes:
        username: 用户名（2~50 字符，唯一）。
        email: 邮箱（唯一，可空）。
        password: 密码（至少 6 位）。
    """

    username: str = Field(min_length=2, max_length=50)
    email: Optional[str] = Field(default=None, max_length=100)
    password: str = Field(min_length=6, max_length=72)


class LoginRequest(BaseModel):
    """登录请求。

    Attributes:
        username: 用户名或邮箱。
        password: 密码。
    """

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=72)


class RefreshRequest(BaseModel):
    """刷新令牌请求。

    Attributes:
        refresh_token: 登录时签发的刷新令牌。
    """

    refresh_token: str = Field(min_length=1)


class WalletBrief(BaseModel):
    """钱包摘要输出模型。

    Attributes:
        points_balance: 可用积分。
        frozen_points: 冻结积分。
        version: 乐观锁版本号。
    """

    points_balance: int
    frozen_points: int
    version: int


class TokenResponse(BaseModel):
    """令牌签发响应（注册 / 登录）。

    Attributes:
        access_token: 访问令牌。
        refresh_token: 刷新令牌。
        token_type: 令牌类型，固定 "bearer"。
        user: 用户信息（刷新接口不返回该字段）。
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: Optional[UserOut] = None


class RefreshResponse(BaseModel):
    """刷新令牌响应（refresh 不换新，仅签发新 access）。

    Attributes:
        access_token: 新签发的访问令牌。
        refresh_token: 原刷新令牌（原样返回）。
        token_type: 令牌类型，固定 "bearer"。
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class AuthMeResponse(BaseModel):
    """当前登录用户信息 + 钱包汇总响应。

    Attributes:
        user: 用户信息。
        wallet: 钱包摘要（可用积分 / 冻结积分 / 版本号）。
    """

    user: UserOut
    wallet: WalletBrief
