"""通知中心 Pydantic Schema 模块（文件级说明）。

定义站内通知的响应模型与已读请求模型：
- NotificationOut：单条通知输出；
- NotificationListOut：分页列表（附带未读数，一次请求满足前端角标刷新）；
- UnreadCountOut：未读数响应；
- NotificationReadRequest：标记已读请求（按 ID 批量 / 全部）；
- NotificationReadResult：标记已读结果（更新条数）。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class NotificationOut(BaseModel):
    """单条站内通知输出。

    Attributes:
        id: 通知 ID。
        type: 通知类型 task_done/task_failed/review_result/quota_warning/system。
        title: 通知标题。
        content: 通知正文。
        related_type: 关联对象类型（可空）。
        related_id: 关联对象 ID（可空）。
        read_at: 已读时间（NULL 表示未读）。
        created_at: 创建时间。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    title: str
    content: str
    related_type: Optional[str] = None
    related_id: Optional[int] = None
    read_at: Optional[datetime] = None
    created_at: datetime


class NotificationListOut(BaseModel):
    """通知分页列表输出（附带未读数）。

    Attributes:
        total: 总记录数。
        page: 当前页码（从 1 开始）。
        page_size: 每页条数。
        unread_count: 当前用户未读总数。
        items: 当前页通知列表。
    """

    total: int
    page: int
    page_size: int
    unread_count: int
    items: List[NotificationOut]


class UnreadCountOut(BaseModel):
    """未读通知数响应。

    Attributes:
        unread_count: 当前用户未读通知数量。
    """

    unread_count: int


class NotificationReadRequest(BaseModel):
    """标记已读请求。

    Attributes:
        ids: 要标记已读的通知 ID 列表（与 all 二选一，均未提供返回 400）。
        all: 是否标记全部未读为已读（优先于 ids）。
    """

    ids: Optional[List[int]] = Field(default=None, description="要标记已读的通知 ID 列表")
    all: bool = Field(default=False, description="是否标记全部未读")


class NotificationReadResult(BaseModel):
    """标记已读结果。

    Attributes:
        updated: 本次实际标记已读的条数。
    """

    updated: int
