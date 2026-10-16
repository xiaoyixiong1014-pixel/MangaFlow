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
        story_setting: 故事设定（P2，JSONB：logline 一句话故事 /
            core_conflict 核心冲突 / worldview 世界观 / source 来源 ai|user /
            updated_at ISO 时间戳；NULL=尚未生成，存量项目不受影响）。
        cover_url: 封面 URL。
        script_phase: 剧本创作阶段（P-1 阶段状态机）。
        deleted_at: 软删除时间（RD-19 / PL-19 引用保护）。
    """

    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'scripting', 'asset', 'storyboard', 'producing', 'review', 'published')",
            name="ck_projects_status"),
        CheckConstraint("fidelity IN ('free', 'outline', 'faithful')", name="ck_projects_fidelity"),
        CheckConstraint("script_phase IN ('idea', 'characters', 'outline', 'body')",
                        name="ck_projects_script_phase"),
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
    # P2 故事设定产物（redesign-script-workspace 方案 §8 模型 A）：可空 JSONB，
    # 存量项目保持 NULL（行为不受影响）；生成时 source="ai"、编辑时 source="user"
    story_setting: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="故事设定（P2）：logline/core_conflict/worldview/source(ai|user)/updated_at")
    cover_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="封面 URL")
    # P-1 剧本流水线阶段状态机：NULL=快速模式（不走分阶段守卫），
    # CHECK 约束 ck_projects_script_phase 限定四值（nullable 列 CHECK 不拦截 NULL）
    script_phase: Mapped[Optional[str]] = mapped_column(
        String(30), nullable=True,
        comment="剧本创作阶段：idea/characters/outline/body；NULL=快速模式")
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="软删除时间")


class Episode(Base):
    """剧集表（§5.3.7 episodes，S-06）。

    Attributes:
        project_id: 项目 ID（外键 CASCADE）。
        title: 剧集标题。
        sequence: 剧集序号（项目内唯一）。
        script_snapshot: 该剧集剧本快照（JSONB）。
        logline: 单集一句话梗概（P-1 大纲域）。
        outline: 单集大纲（P-1，JSONB：core_event/characters/emotion_curve/duration_sec）。
        confirmed_at: 单集大纲确认时间（P-1）。
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
    # P-1 大纲域扩展（全部可空，快速模式/旧数据不填）
    logline: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True, comment="单集一句话梗概（P-1）")
    outline: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="单集大纲（P-1）：core_event/characters/emotion_curve/duration_sec")
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="单集大纲确认时间（P-1）")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft", comment="状态")


class VersionSnapshot(Base):
    """版本快照表（§5.3.28 version_snapshots，V-01 / RD-08）。

    Attributes:
        project_id: 项目 ID（外键 CASCADE）。
        stage: 阶段（P-1 扩展：extract_confirmed/characters_confirmed/
            outline_confirmed/body_generated 等）。
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
    # P-1 扩宽 varchar(20)→varchar(30)：容纳 characters_confirmed/outline_confirmed/
    # body_generated 新取值（既有 extract_confirmed 等取值不受影响）
    stage: Mapped[str] = mapped_column(
        String(30), nullable=False,
        comment="阶段：extract_confirmed/characters_confirmed/outline_confirmed/body_generated 等")
    checkpoint_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="LangGraph 检查点 ID")
    summary: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="快照摘要")
    impact_scope: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="影响面记录（RD-08）")
