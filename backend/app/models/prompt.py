"""内容运营域模型（文件级说明）。

包含表：prompts（提示词库）、announcements（公告）。
严格对应架构设计文档 §5.3.25、§5.3.26。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import TIMESTAMP, BigInteger, Boolean, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Prompt(Base):
    """提示词库表（§5.3.25 prompts，PL-12 ~ PL-15）。

    Attributes:
        user_id: 归属用户 ID（NULL = 公共提示词；外键 SET NULL，用户删除后转为公共）。
        scope: 范围 public/private。
        capability: 适用能力（角色定妆 / 分镜画面 / 运镜 / 配音）。
        title: 标题。
        content: 内容。
        category: 分类。
        tags: 标签（JSONB）。
        status: 上下架状态（published 已上架 / offline 已下架），
            管理端可切换，用户端列表仅展示 published。
        source_ref: 外部 JSON 来源标识（PL-15）。
    """

    __tablename__ = "prompts"
    __table_args__ = (
        CheckConstraint("scope IN ('public', 'private')", name="ck_prompts_scope"),
        Index("idx_prompts_user", "user_id"),
        Index("idx_prompts_scope", "scope"),
        Index("idx_prompts_cap", "capability"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        comment="归属用户 ID（NULL=公共提示词）")
    scope: Mapped[str] = mapped_column(String(10), nullable=False, server_default="private",
                                       comment="范围：public/private")
    capability: Mapped[Optional[str]] = mapped_column(String(20), nullable=True,
                                                      comment="适用能力：角色定妆/分镜画面/运镜/配音")
    title: Mapped[str] = mapped_column(String(100), nullable=False, comment="标题")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="内容")
    category: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, comment="分类")
    tags: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="标签")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="published",
                                        comment="上下架状态：published 已上架 / offline 已下架")
    source_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="外部 JSON 来源标识（PL-15）")


class Announcement(Base):
    """公告表（§5.3.26 announcements，D-05 / AD 内容运营）。

    Attributes:
        title: 标题。
        content: 内容。
        is_pinned: 是否置顶。
        status: 状态 draft/published/offline。
        published_at: 发布时间。
    """

    __tablename__ = "announcements"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'published', 'offline')", name="ck_ann_status"),
        Index("idx_ann_status", "status", "is_pinned"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="标题")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="内容")
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", comment="是否置顶")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft",
                                        comment="状态：draft/published/offline")
    published_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="发布时间")
