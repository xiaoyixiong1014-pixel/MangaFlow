"""用户中心 Pydantic Schema 模块（文件级说明）。

定义用户资料、密码修改、用量统计、操作日志、版本快照等出入参模型。
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class UserOut(BaseModel):
    """用户信息输出模型。

    Attributes:
        id: 用户 ID。
        username: 用户名。
        email: 邮箱。
        avatar: 头像 URL。
        role: 角色（user / operator）。
        vip_level: 会员等级。
        status: 状态（0 禁用 / 1 正常）。
        last_login_at: 最近登录时间（ISO 字符串）。
        created_at: 注册时间（ISO 字符串）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: Optional[str] = None
    avatar: Optional[str] = None
    role: str
    vip_level: int = 0
    status: int = 1
    last_login_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class UserUpdateRequest(BaseModel):
    """个人资料更新请求。

    Attributes:
        avatar: 新头像 URL（可空，None 表示不修改）。
        email: 新邮箱（可空，None 表示不修改）。
    """

    avatar: Optional[str] = Field(default=None, max_length=255)
    email: Optional[str] = Field(default=None, max_length=100)


class PasswordChangeRequest(BaseModel):
    """修改密码请求。

    Attributes:
        old_password: 旧密码。
        new_password: 新密码（至少 6 位）。
    """

    old_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=6, max_length=72)


class UsageOut(BaseModel):
    """用户用量统计输出模型。

    Attributes:
        project_count: 未删除的项目总数。
        task_count_by_status: 各状态生成任务计数（queued/running/done/failed）。
        points_used_this_month: 本月消耗积分（consume 流水合计，正值）。
        points_total: 当前持有积分总额（可用余额 + 冻结）。
    """

    project_count: int
    task_count_by_status: dict
    points_used_this_month: int
    points_total: int


class OperationLogOut(BaseModel):
    """操作日志输出模型。

    Attributes:
        id: 日志 ID。
        project_id: 关联项目 ID（可空）。
        action: 操作类型。
        detail: 操作详情（JSONB）。
        ip: 操作 IP。
        risk_level: 风险等级（normal / high）。
        created_at: 操作时间（ISO 字符串）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: Optional[int] = None
    action: str
    detail: Optional[Any] = None
    ip: Optional[str] = None
    risk_level: str = "normal"
    created_at: Optional[datetime] = None


class SnapshotOut(BaseModel):
    """版本快照输出模型。

    Attributes:
        id: 快照 ID。
        project_id: 项目 ID。
        stage: 阶段。
        checkpoint_id: LangGraph 检查点 ID（可空）。
        summary: 快照摘要（JSONB）。
        impact_scope: 影响面记录（JSONB）。
        created_at: 创建时间（ISO 字符串）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    stage: str
    checkpoint_id: Optional[str] = None
    summary: Optional[Any] = None
    impact_scope: Optional[Any] = None
    created_at: Optional[datetime] = None
