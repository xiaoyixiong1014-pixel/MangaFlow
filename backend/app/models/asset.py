"""资产域模型（文件级说明）。

包含表：characters（角色资产）、scenes（场景资产）、props（道具资产）、
styles（风格资产）、asset_variants（资产变体）。
严格对应架构设计文档 §5.3.9 ~ §5.3.13。

注意：
- characters.feature_vector 原设计为 vector(1024)，本机无 pgvector 扩展，
  以 JSONB 承载（预留 pgvector 升级点）。
- asset_variants.owner_type/owner_id 为多态引用（character/scene/prop），
  不建数据库外键，归属完整性由应用层校验。
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
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Character(Base):
    """角色资产表（§5.3.9 characters）。

    Attributes:
        project_id: 项目 ID（外键 CASCADE）。
        name: 角色名（项目内唯一）。
        aliases: 别名/称谓集合（RD-05，JSONB）。
        description: 角色描述。
        visual_prompt: 标准化视觉提示词（A-06）。
        feature_vector: 面部特征向量（原 vector(1024)，JSONB 承载，预留 pgvector 升级点）。
        asset_url: 主定妆照 URL。
        face_locked: 面部层锁定（RD-12 锁定分层）。
        outfit_variant_id: 当前服饰变体 ID（RD-12，外键 SET NULL）。
        generation_status: 生成状态 pending/generating/done/failed。
        deleted_at: 软删除时间。
    """

    __tablename__ = "characters"
    __table_args__ = (
        CheckConstraint("generation_status IN ('pending', 'generating', 'done', 'failed')",
                        name="ck_characters_generation_status"),
        Index("idx_characters_project", "project_id"),
        Index("uk_characters_name", "project_id", "name", unique=True),
        Index("idx_characters_locked", "project_id", "face_locked"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID（级联删除）")
    name: Mapped[str] = mapped_column(String(50), nullable=False, comment="角色名")
    aliases: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="别名/称谓集合（RD-05）")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="角色描述")
    visual_prompt: Mapped[str] = mapped_column(Text, nullable=False, comment="标准化视觉提示词（A-06）")
    # 预留 pgvector 升级点：原 vector(1024)，本机无 pgvector，以 JSONB 承载
    feature_vector: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="面部特征向量（JSONB，预留 pgvector 升级点）")
    asset_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="主定妆照 URL")
    face_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false",
                                              comment="面部层锁定（RD-12）")
    outfit_variant_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("asset_variants.id", ondelete="SET NULL"), nullable=True,
        comment="当前服饰变体 ID（RD-12）")
    generation_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending",
                                                   comment="生成状态：pending/generating/done/failed")
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="软删除时间")


class Scene(Base):
    """场景资产表（§5.3.10 scenes）。

    Attributes:
        project_id: 项目 ID（外键 CASCADE）。
        name: 场景名（项目内唯一）。
        description: 场景描述。
        visual_prompt: 视觉提示词。
        asset_url: 概念图 URL（允许为空场景，RD-19）。
        space_signature: 空间布局签名（空间一致性校验，RD-19，JSONB）。
        is_locked: 锁定状态。
        deleted_at: 软删除时间。
    """

    __tablename__ = "scenes"
    __table_args__ = (
        Index("idx_scenes_project", "project_id"),
        Index("uk_scenes_name", "project_id", "name", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID（级联删除）")
    name: Mapped[str] = mapped_column(String(50), nullable=False, comment="场景名")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="场景描述")
    visual_prompt: Mapped[str] = mapped_column(Text, nullable=False, comment="视觉提示词")
    asset_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="概念图 URL（空场景允许为空）")
    space_signature: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="空间布局签名（RD-19）")
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", comment="锁定状态")
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="软删除时间")


class Prop(Base):
    """道具资产表（§5.3.11 props，A-07 / RD-17 / RD-18）。

    Attributes:
        project_id: 项目 ID（外键 CASCADE）。
        name: 道具名。
        category: 分类。
        description: 描述。
        visual_prompt: 视觉提示词。
        asset_url: 标准图 URL。
        ownership: 归属关系（持有者/出现场次/换手记录，RD-18，JSONB）。
        is_locked: 锁定状态。
        deleted_at: 软删除时间。
    """

    __tablename__ = "props"
    __table_args__ = (
        Index("idx_props_project", "project_id"),
        Index("idx_props_category", "project_id", "category"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID（级联删除）")
    name: Mapped[str] = mapped_column(String(50), nullable=False, comment="道具名")
    category: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, comment="分类")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="描述")
    visual_prompt: Mapped[str] = mapped_column(Text, nullable=False, comment="视觉提示词")
    asset_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="标准图 URL")
    ownership: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="归属关系（RD-18）")
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", comment="锁定状态")
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="软删除时间")


class Style(Base):
    """风格资产表（§5.3.12 styles，RD-16）。

    Attributes:
        project_id: 项目 ID（外键 CASCADE）。
        name: 风格名。
        preset_code: 内置预设编码。
        reference_url: 参考图 URL。
        visual_prompt: 风格提示词（全局锚点）。
        is_default: 是否项目默认风格（部分唯一索引 uk_styles_default）。
    """

    __tablename__ = "styles"
    __table_args__ = (
        Index("idx_styles_project", "project_id"),
        Index("uk_styles_default", "project_id", unique=True, postgresql_where=text("is_default")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID（级联删除）")
    name: Mapped[str] = mapped_column(String(50), nullable=False, comment="风格名")
    preset_code: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, comment="内置预设编码")
    reference_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="参考图 URL")
    visual_prompt: Mapped[str] = mapped_column(Text, nullable=False, comment="风格提示词（全局锚点）")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false",
                                             comment="是否项目默认风格")


class AssetVariant(Base):
    """资产变体表（§5.3.13 asset_variants，RD-14 / RD-19）。

    多态归属：owner_type + owner_id 指向 characters / scenes / props 表的行，
    不建数据库外键，归属完整性由应用层校验（架构文档 §5.4 明确此设计）。

    Attributes:
        owner_type: 归属资产类型 character/scene/prop。
        owner_id: 归属资产 ID（多态，无外键）。
        variant_type: 变体类型 outfit/lighting/age/other。
        name: 变体名（如"战斗装""夜景"）。
        inherit_mask: 显式继承/独立声明（RD-14，JSONB，必填）。
        asset_url: 变体图 URL。
        visual_prompt: 变体提示词。
        is_active: 是否启用。
    """

    __tablename__ = "asset_variants"
    __table_args__ = (
        CheckConstraint("owner_type IN ('character', 'scene', 'prop')", name="ck_variants_owner_type"),
        CheckConstraint("variant_type IN ('outfit', 'lighting', 'age', 'other')", name="ck_variants_variant_type"),
        Index("idx_variants_owner", "owner_type", "owner_id"),
        Index("idx_variants_type", "variant_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    owner_type: Mapped[str] = mapped_column(String(20), nullable=False,
                                            comment="归属资产类型：character/scene/prop（多态，无外键）")
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="归属资产 ID（应用层校验完整性）")
    variant_type: Mapped[str] = mapped_column(String(20), nullable=False,
                                              comment="变体类型：outfit/lighting/age/other")
    name: Mapped[str] = mapped_column(String(50), nullable=False, comment="变体名（如战斗装、夜景）")
    inherit_mask: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="显式继承/独立声明（RD-14）")
    asset_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="变体图 URL")
    visual_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="变体提示词")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true", comment="是否启用")
