"""通知中心域模型（文件级说明）。

包含表：notifications（用户站内通知）。
承载任务完成/失败、审核结果、配额预警、系统公告等站内消息，
配合 services/notification_service.py 的进程内 SSE 广播总线实现实时推送。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Notification(Base):
    """用户站内通知表（notifications）。

    Attributes:
        user_id: 归属用户 ID（外键 CASCADE：用户删除时通知随之清理）。
        type: 通知类型 task_done/task_failed/review_result/quota_warning/system。
        title: 通知标题（列表页单行展示）。
        content: 通知正文（详情展示）。
        related_type: 关联对象类型（task/review/order 等，可空）。
        related_id: 关联对象 ID（多态引用，可空）。
        read_at: 已读时间（NULL 表示未读；已读后不可逆）。
        created_at / updated_at: 通用时间戳（TimestampMixin 自动维护）。
    """

    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            "type IN ('task_done', 'task_failed', 'review_result', 'quota_warning', 'system')",
            name="ck_notifications_type",
        ),
        # 高频查询：按用户拉取列表 + 未读数统计（user_id + read_at 组合索引）
        Index("idx_notifications_user_read", "user_id", "read_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
        comment="归属用户 ID")
    type: Mapped[str] = mapped_column(String(30), nullable=False, comment="通知类型")
    title: Mapped[str] = mapped_column(String(100), nullable=False, comment="通知标题")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="通知正文")
    related_type: Mapped[Optional[str]] = mapped_column(
        String(30), nullable=True, comment="关联对象类型（task/review 等）")
    related_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True, comment="关联对象 ID（多态引用）")
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="已读时间（NULL 表示未读）")
