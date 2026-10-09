"""项目域模型（文件级说明）。

包含表：projects（项目）、episodes（剧集）、version_snapshots（版本快照）。
严格对应架构设计文档 §5.3.6、§5.3.7、§5.3.28。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import TIMESTAMP, BigInteger, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Project(Base):
    """项目表（§5.3.6 projects）。

    Attributes:
        user_id: 归属用户 ID（外键 RESTRICT）。
        title: 项目标题。
        genre: 体裁。
        style: 视觉风格。
        fidelity: 忠实度档位 free/outline/faithful（RD-01）。
        status: 项目状态（草稿→剧本→资产→分镜→成片→审核→发布）。
        aspect_ratio: 输出比例（RD-37 前置）。
        target_duration: 目标时长（秒）。
        config: 项目级模型配置（PL-09，JSONB）。
        cover_url: 封面 URL。
        deleted_at: 软删除时间（RD-19 / PL-19 引用保护）。
    """

    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'scripting', 'asset', 'storyboard', 'producing', 'review', 'published')",
            name="ck_projects_status"),
        CheckConstraint("fidelity IN ('free', 'outline', 'faithful')", name="ck_projects_fidelity"),
        Index("idx_projects_user", "user_id", "updated_at"),
        Index("idx_projects_status", "status"),
        Index("idx_projects_deleted", "deleted_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, comment="归属用户 ID")
    title: Mapped[str] = mapped_column(String(100), nullable=False, comment="项目标题")
    genre: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, comment="体裁")
    style: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, comment="视觉风格")
    fidelity: Mapped[Optional[str]] = mapped_column(String(20), nullable=True,
                                                    comment="忠实度档位：free/outline/faithful（RD-01）")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft", comment="项目状态")
    aspect_ratio: Mapped[str] = mapped_column(String(10), nullable=False, server_default="9:16",
                                              comment="输出比例（RD-37 前置）")
    target_duration: Mapped[Optional[int]] = mapped_column(nullable=True, comment="目标时长（秒）")
    config: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="项目级模型配置（PL-09）")
    cover_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="封面 URL")
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="软删除时间")


class Episode(Base):
    """剧集表（§5.3.7 episodes，S-06）。

    Attributes:
        project_id: 项目 ID（外键 CASCADE）。
        title: 剧集标题。
        sequence: 剧集序号（项目内唯一）。
        script_snapshot: 该剧集剧本快照（JSONB）。
        status: 状态。
    """

    __tablename__ = "episodes"
    __table_args__ = (
        Index("uk_episodes_seq", "project_id", "sequence", unique=True),
        Index("idx_episodes_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID（级联删除）")
    title: Mapped[str] = mapped_column(String(100), nullable=False, comment="剧集标题")
    sequence: Mapped[int] = mapped_column(nullable=False, comment="剧集序号")
    script_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="该剧集剧本快照")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft", comment="状态")


class VersionSnapshot(Base):
    """版本快照表（§5.3.28 version_snapshots，V-01 / RD-08）。

    Attributes:
        project_id: 项目 ID（外键 CASCADE）。
        stage: 阶段。
        checkpoint_id: 对应 LangGraph 检查点 ID（V-01 与 MA-06 同源）。
        summary: 快照摘要（资产数/分镜数/成片数，JSONB）。
        impact_scope: 影响面记录（RD-08，JSONB）。
    """

    __tablename__ = "version_snapshots"
    __table_args__ = (
        Index("idx_vs_project", "project_id", "created_at"),
        Index("idx_vs_stage", "stage"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID（级联删除）")
    stage: Mapped[str] = mapped_column(String(20), nullable=False, comment="阶段")
    checkpoint_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="LangGraph 检查点 ID")
    summary: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="快照摘要")
    impact_scope: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="影响面记录（RD-08）")
