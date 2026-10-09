"""媒体域模型（文件级说明）。

包含表：media_assets（媒体素材）、film_outputs（成片版本）。
严格对应架构设计文档 §5.3.21、§5.3.22。

注意：film_outputs 在架构文档基础上追加了 4 个"分享功能扩展字段"
（share_token / share_expires_at / share_password / share_revoked），
架构文档未定义独立分享表，经确认允许扩展于本表。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MediaAsset(Base):
    """媒体素材表（§5.3.21 media_assets，PL-16 ~ PL-19）。

    Attributes:
        user_id: 归属用户 ID（外键 RESTRICT）。
        project_id: 归属项目 ID（外键 SET NULL）。
        media_type: 媒体类型 image/video/audio/text。
        source: 素材来源 ai_generated/upload/canvas/studio（RD-47 标记画布产物）。
        url: 稳定地址。
        meta: 元数据（尺寸/时长/来源任务/引用关系，JSONB）。
        deleted_at: 软删除时间（引用保护 PL-19）。
    """

    __tablename__ = "media_assets"
    __table_args__ = (
        CheckConstraint("media_type IN ('image', 'video', 'audio', 'text')", name="ck_ma_media_type"),
        CheckConstraint("source IN ('ai_generated', 'upload', 'canvas', 'studio')", name="ck_ma_source"),
        Index("idx_ma_user", "user_id", "created_at"),
        Index("idx_ma_project", "project_id"),
        Index("idx_ma_type", "media_type"),
        Index("idx_ma_source", "source"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, comment="归属用户 ID")
    project_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, comment="归属项目 ID")
    media_type: Mapped[str] = mapped_column(String(10), nullable=False, comment="媒体类型")
    source: Mapped[str] = mapped_column(String(20), nullable=False, comment="素材来源（RD-47）")
    url: Mapped[str] = mapped_column(String(255), nullable=False, comment="稳定地址")
    meta: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="尺寸/时长/来源任务/引用关系")
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="软删除时间（引用保护 PL-19）")


class FilmOutput(Base):
    """成片版本表（§5.3.22 film_outputs，RD-40）。

    Attributes:
        project_id: 项目 ID（外键 CASCADE）。
        episode_id: 剧集 ID（外键 SET NULL）。
        version_no: 版本号。
        url: 成片地址。
        resolution: 分辨率 720P/1080P/2K。
        aspect_ratio: 画幅比例 9:16/16:9/1:1。
        duration: 时长（秒）。
        watermark_id: 隐形溯源水印标识（R-09）。
        is_current: 是否当前版本。
        share_token: 分享令牌（分享功能扩展字段，唯一）。
        share_expires_at: 分享过期时间（分享功能扩展字段）。
        share_password: 分享提取码（分享功能扩展字段）。
        share_revoked: 分享是否已撤销（分享功能扩展字段）。
    """

    __tablename__ = "film_outputs"
    __table_args__ = (
        CheckConstraint("resolution IN ('720P', '1080P', '2K')", name="ck_outputs_resolution"),
        Index("uk_outputs_ver", "project_id", "episode_id", "version_no", unique=True),
        Index("idx_outputs_current", "project_id", postgresql_where=text("is_current")),
        Index("uk_film_outputs_share_token", "share_token", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID（级联删除）")
    episode_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("episodes.id", ondelete="SET NULL"), nullable=True, comment="剧集 ID")
    version_no: Mapped[int] = mapped_column(nullable=False, comment="版本号")
    url: Mapped[str] = mapped_column(String(255), nullable=False, comment="成片地址")
    resolution: Mapped[str] = mapped_column(String(10), nullable=False, comment="分辨率：720P/1080P/2K")
    aspect_ratio: Mapped[str] = mapped_column(String(10), nullable=False, comment="画幅比例：9:16/16:9/1:1")
    duration: Mapped[int] = mapped_column(nullable=False, comment="时长（秒）")
    watermark_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="隐形溯源水印标识（R-09）")
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true",
                                             comment="是否当前版本")

    # ---------- 分享功能扩展字段（架构文档未定义分享表，扩展于本表） ----------
    share_token: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, comment="分享功能扩展字段：分享令牌（唯一）")
    share_expires_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="分享功能扩展字段：分享过期时间")
    share_password: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, comment="分享功能扩展字段：分享提取码")
    share_revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", comment="分享功能扩展字段：分享是否已撤销")
