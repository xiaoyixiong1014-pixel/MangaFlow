"""画布域模型（文件级说明）。

包含表：canvases（画布）、canvas_nodes（画布节点）、canvas_edges（画布连线）、
canvas_plugins（画布插件）。
严格对应架构设计文档 §5.3.16 ~ §5.3.19。
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
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Canvas(Base):
    """画布表（§5.3.16 canvases，CV-01 ~ CV-03）。

    Attributes:
        project_id: 关联项目 ID（可空 = 独立画布；外键 CASCADE）。
        user_id: 拥有者 ID（外键 CASCADE）。
        title: 画布名称。
        type: 画布类型 standalone/studio_linked（CV-23）。
        source_storyboard_id: 投递来源分镜 ID（外键 SET NULL）。
        viewport: 视口状态（缩放/位移，JSONB）。
        thumbnail_url: 缩略图 URL。
        deleted_at: 软删除时间。
    """

    __tablename__ = "canvases"
    __table_args__ = (
        CheckConstraint("type IN ('standalone', 'studio_linked')", name="ck_canvases_type"),
        Index("idx_canvases_user", "user_id", "updated_at"),
        Index("idx_canvases_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    project_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True,
        comment="关联项目 ID（NULL = 独立画布）")
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="拥有者 ID")
    title: Mapped[str] = mapped_column(String(100), nullable=False, comment="画布名称")
    type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="standalone",
                                      comment="类型：standalone/studio_linked（CV-23）")
    source_storyboard_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("storyboards.id", ondelete="SET NULL"), nullable=True, comment="投递来源分镜 ID")
    viewport: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="视口状态（缩放/位移）")
    thumbnail_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="缩略图 URL")
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="软删除时间")


class CanvasNode(Base):
    """画布节点表（§5.3.17 canvas_nodes，CV-04 ~ CV-08）。

    Attributes:
        canvas_id: 画布 ID（外键 CASCADE）。
        node_type: 节点类型（text/image/video/audio/generate/asset_ref/shot/compose/logic/
            group/plugin/mcp_tool）。
        position_x: 画布 X 坐标（DOUBLE PRECISION）。
        position_y: 画布 Y 坐标。
        size_w: 宽。
        size_h: 高。
        z_index: 层级。
        data: 节点内容（文本/结果 URL，JSONB）。
        config: 节点配置（模型/参数/资产引用，JSONB）。
        is_dirty: 脏节点标记（RD-45 DAG 增量）。
        cache_ref: 缓存结果引用（RD-45）。
        loop_config: 逻辑节点循环语义（RD-48，JSONB）。
        status: 节点状态 idle/queued/running/done/failed。
        parent_group_id: 分组父节点 ID（自引用外键 SET NULL）。
    """

    __tablename__ = "canvas_nodes"
    __table_args__ = (
        CheckConstraint(
            "node_type IN ('text', 'image', 'video', 'audio', 'generate', "
            "'asset_ref', 'shot', 'compose', 'logic', 'group', 'plugin', 'mcp_tool')",
            name="ck_cn_node_type"),
        CheckConstraint("status IN ('idle', 'queued', 'running', 'done', 'failed')", name="ck_cn_status"),
        Index("idx_cn_canvas", "canvas_id"),
        Index("idx_cn_type", "node_type"),
        Index("idx_cn_status", "status"),
        Index("idx_cn_group", "parent_group_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    canvas_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False, comment="画布 ID（级联删除）")
    node_type: Mapped[str] = mapped_column(String(20), nullable=False, comment="节点类型")
    position_x: Mapped[float] = mapped_column(DOUBLE_PRECISION, nullable=False, server_default="0",
                                              comment="画布 X 坐标")
    position_y: Mapped[float] = mapped_column(DOUBLE_PRECISION, nullable=False, server_default="0",
                                              comment="画布 Y 坐标")
    size_w: Mapped[Optional[float]] = mapped_column(DOUBLE_PRECISION, nullable=True, comment="宽")
    size_h: Mapped[Optional[float]] = mapped_column(DOUBLE_PRECISION, nullable=True, comment="高")
    z_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", comment="层级")
    data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="节点内容（文本/结果 URL）")
    config: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="节点配置（模型/参数/资产引用）")
    is_dirty: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false",
                                           comment="脏节点标记（RD-45）")
    cache_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="缓存结果引用（RD-45）")
    loop_config: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="逻辑节点循环语义（RD-48）")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="idle", comment="节点状态")
    # 自引用外键：分组父节点，分组节点删除时子节点脱离分组
    parent_group_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("canvas_nodes.id", ondelete="SET NULL"), nullable=True, comment="分组父节点 ID（自引用）")


class CanvasEdge(Base):
    """画布连线表（§5.3.18 canvas_edges）。

    Attributes:
        canvas_id: 画布 ID（外键 CASCADE）。
        source_node_id: 源节点 ID（外键 CASCADE）。
        target_node_id: 目标节点 ID（外键 CASCADE）。
        source_port: 源端口/输出类型。
        target_port: 目标端口/输入类型。
    """

    __tablename__ = "canvas_edges"
    __table_args__ = (
        # 自环禁止；成环检测在应用层实现（RD-48）
        CheckConstraint("source_node_id <> target_node_id", name="ck_ce_no_self_loop"),
        Index("idx_ce_canvas", "canvas_id"),
        Index("idx_ce_source", "source_node_id"),
        Index("idx_ce_target", "target_node_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    canvas_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("canvases.id", ondelete="CASCADE"), nullable=False, comment="画布 ID（级联删除）")
    source_node_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("canvas_nodes.id", ondelete="CASCADE"), nullable=False, comment="源节点 ID")
    target_node_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("canvas_nodes.id", ondelete="CASCADE"), nullable=False, comment="目标节点 ID")
    source_port: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, comment="源端口/输出类型")
    target_port: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, comment="目标端口/输入类型")


class CanvasPlugin(Base):
    """画布插件表（§5.3.19 canvas_plugins，CV-20 ~ CV-22 / RD-46）。

    Attributes:
        user_id: 安装者 ID（外键 CASCADE）。
        plugin_url: 插件地址。
        name: 插件名。
        version: 版本。
        permissions: 权限清单（读取画布/调用生成等，RD-46，JSONB）。
        granted: 用户是否已显式授权。
        enabled: 是否启用。
        reviewed: 是否通过市场审核。
    """

    __tablename__ = "canvas_plugins"
    __table_args__ = (
        Index("uk_plugins_user_url", "user_id", "plugin_url", unique=True),
        Index("idx_plugins_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="安装者 ID")
    plugin_url: Mapped[str] = mapped_column(String(500), nullable=False, comment="插件地址")
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="插件名")
    version: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, comment="版本")
    permissions: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="权限清单（RD-46）")
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false",
                                          comment="用户是否已显式授权")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", comment="是否启用")
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false",
                                           comment="是否通过市场审核")
