"""模型配置域模型（文件级说明）。

包含表：model_channels（模型渠道）、logical_models（逻辑模型）。
严格对应架构设计文档 §5.3.23、§5.3.24。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import TIMESTAMP, BigInteger, Boolean, CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ModelChannel(Base):
    """模型渠道表（§5.3.23 model_channels，PL-07）。

    Attributes:
        name: 渠道备注名（用户自有渠道展示用，可空；平台渠道由系统管理无备注）。
        protocol: 协议类型（如 openai 兼容协议）。
        base_url: 接口地址。
        api_key_encrypted: AES 加密后的 API Key（本阶段种子数据暂存明文，
            后续任务接入 AES 工具后统一升级为密文）。
        owner_user_id: 归属用户 ID（NULL = 平台渠道；非空 = 用户自有 Key，RD-49；
            外键 SET NULL）。
        models: 模型目录（JSONB）。
        health_status: 健康状态 unknown/healthy/degraded/down。
        last_check_at: 最近探测时间。
        enabled: 是否启用。
    """

    __tablename__ = "model_channels"
    __table_args__ = (
        CheckConstraint("health_status IN ('unknown', 'healthy', 'degraded', 'down')", name="ck_mc_health_status"),
        Index("idx_mc_health", "health_status"),
        Index("idx_mc_owner", "owner_user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    name: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, comment="渠道备注名（用户自有渠道展示用）")
    protocol: Mapped[str] = mapped_column(String(30), nullable=False, comment="协议类型")
    base_url: Mapped[str] = mapped_column(String(255), nullable=False, comment="接口地址")
    api_key_encrypted: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True, comment="AES 加密 Key（种子暂存明文，后续接入 AES 升级）")
    owner_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        comment="归属用户 ID（NULL=平台渠道，RD-49）")
    models: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="模型目录")
    health_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="unknown",
                                               comment="健康状态：unknown/healthy/degraded/down")
    last_check_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="最近探测时间")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true", comment="是否启用")


class LogicalModel(Base):
    """逻辑模型表（§5.3.24 logical_models，PL-08 / PL-26 ~ PL-29）。

    逻辑模型与物理渠道解耦：通过 channel_priority 引用渠道 id 列表表达
    故障降级顺序，新增/切换渠道不改业务代码。

    Attributes:
        capability: 能力类型 llm/image/video/tts。
        name: 逻辑模型名（业务侧展示）。
        channel_priority: 渠道优先级列表（故障降级顺序，JSONB 数组存渠道 id）。
        is_default: 是否官方默认（PL-28）。
        price_per_unit: 计价规则（模型单价/参数倍率，JSONB）。
        scene_desc: 适用场景（PL-27）。
        doc_url: 官方文档链接（PL-27）。
    """

    __tablename__ = "logical_models"
    __table_args__ = (
        CheckConstraint("capability IN ('llm', 'image', 'video', 'tts')", name="ck_lm_capability"),
        Index("uk_lm_cap_name", "capability", "name", unique=True),
        Index("idx_lm_default", "capability", postgresql_where=text("is_default")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    capability: Mapped[str] = mapped_column(String(10), nullable=False, comment="能力类型：llm/image/video/tts")
    name: Mapped[str] = mapped_column(String(50), nullable=False, comment="逻辑模型名（业务侧展示）")
    channel_priority: Mapped[dict] = mapped_column(JSONB, nullable=False,
                                                   comment="渠道优先级列表（JSONB 数组，存渠道 id）")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false",
                                             comment="是否官方默认（PL-28）")
    price_per_unit: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="计价规则（单价/参数倍率）")
    scene_desc: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="适用场景（PL-27）")
    doc_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="官方文档链接（PL-27）")
