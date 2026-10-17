"""分镜域模型（文件级说明）。

包含表：storyboards（分镜）、storyboard_characters（分镜-角色关联）。
严格对应架构设计文档 §5.3.14、§5.3.15。
"""

from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Storyboard(Base):
    """分镜表（§5.3.14 storyboards）。

    Attributes:
        project_id: 项目 ID（外键 CASCADE）。
        episode_id: 所属剧集 ID（外键 SET NULL）。
        script_id: 来源剧本 ID（外键 SET NULL）。
        sequence: 镜头序号。
        shot_type: 景别。
        camera_movement: 运镜。
        duration: 预计时长（秒）。
        actual_duration: 配音回写的实际时长（RD-31，NUMERIC(6,2)）。
        visual_description: 画面描述。
        dialogue: 对白/旁白。
        scene_id: 强绑场景资产 ID（RD-24，外键 SET NULL）。
        prompt_locked: 提示词系统锁定层（RD-04 分层）。
        prompt_editable: 提示词用户可编辑层。
        keyframe_url: 关键帧 URL。
        end_frame_url: 尾帧 URL。
        video_url: 视频片段 URL。
        audio_url: 配音 URL。
        status: 状态 pending/generating/done/failed。
        regen_count: 重生成次数（RD-25 触发画布引导）。
        scene_version: 绑定场景时 scenes.asset_version 快照（P0-3，NULL=旧数据视为一致）。
    """

    __tablename__ = "storyboards"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'generating', 'done', 'failed')", name="ck_sb_status"),
        Index("uk_sb_seq", "project_id", "episode_id", "sequence", unique=True),
        Index("idx_sb_status", "status"),
        Index("idx_sb_scene", "scene_id"),
        Index("idx_sb_regen", "regen_count"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID（级联删除）")
    episode_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("episodes.id", ondelete="SET NULL"), nullable=True, comment="所属剧集 ID")
    script_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("scripts.id", ondelete="SET NULL"), nullable=True, comment="来源剧本 ID")
    sequence: Mapped[int] = mapped_column(nullable=False, comment="镜头序号")
    shot_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, comment="景别")
    camera_movement: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, comment="运镜")
    duration: Mapped[int] = mapped_column(nullable=False, server_default="5", comment="预计时长（秒）")
    actual_duration: Mapped[Optional[float]] = mapped_column(
        Numeric(6, 2), nullable=True, comment="配音回写的实际时长（RD-31）")
    visual_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="画面描述")
    dialogue: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="对白/旁白")
    scene_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("scenes.id", ondelete="SET NULL"), nullable=True, comment="强绑场景资产 ID（RD-24）")
    prompt_locked: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="提示词系统锁定层（RD-04）")
    prompt_editable: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="提示词用户可编辑层")
    keyframe_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="关键帧 URL")
    end_frame_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="尾帧 URL")
    video_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="视频片段 URL")
    audio_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="配音 URL")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending",
                                        comment="状态：pending/generating/done/failed")
    regen_count: Mapped[int] = mapped_column(nullable=False, server_default="0",
                                             comment="重生成次数（RD-25）")
    # P0-3 版本透传：绑定场景资产时的版本快照（NULL=旧数据视为一致，防存量全量误报）
    scene_version: Mapped[Optional[int]] = mapped_column(
        nullable=True, comment="绑定场景时 scenes.asset_version 快照（P0-3）")


class StoryboardCharacter(Base):
    """分镜-角色关联表（§5.3.15 storyboard_characters，RD-24 强绑）。

    用关联表而非 character_ids JSON 字段，以满足 RD-24
    「强绑资产 ID + 外键约束 + 引用保护」的要求。

    Attributes:
        storyboard_id: 分镜 ID（外键 CASCADE）。
        character_id: 角色资产 ID（外键 RESTRICT）。
        variant_id: 该镜头使用的造型变体 ID（外键 RESTRICT）。
        link_confidence: 实体链接置信度（NUMERIC(4,3)，低置信标黄）。
        character_version: 引用时 characters.asset_version 快照（P0-3，NULL=旧数据视为一致）。
        variant_version: 引用时 asset_variants.asset_version 快照（P0-3，variant_id 非空时记录）。
    """

    __tablename__ = "storyboard_characters"
    __table_args__ = (
        CheckConstraint("link_confidence >= 0 AND link_confidence <= 1", name="ck_sbc_confidence"),
        Index("pk_sb_char", "storyboard_id", "character_id", unique=True),
        Index("idx_sbc_character", "character_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    storyboard_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("storyboards.id", ondelete="CASCADE"), nullable=False, comment="分镜 ID（级联删除）")
    character_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("characters.id", ondelete="RESTRICT"), nullable=False, comment="角色资产 ID")
    variant_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("asset_variants.id", ondelete="RESTRICT"), nullable=True,
        comment="该镜头使用的造型变体 ID")
    link_confidence: Mapped[Optional[float]] = mapped_column(
        Numeric(4, 3), nullable=True, comment="实体链接置信度（低置信标黄）")
    # P0-3 版本透传：引用资产时的版本快照（NULL=旧数据视为一致，防存量全量误报）
    character_version: Mapped[Optional[int]] = mapped_column(
        nullable=True, comment="引用时 characters.asset_version 快照（P0-3）")
    variant_version: Mapped[Optional[int]] = mapped_column(
        nullable=True, comment="引用时 asset_variants.asset_version 快照（P0-3）")
