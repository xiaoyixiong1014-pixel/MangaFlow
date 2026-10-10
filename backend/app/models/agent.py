"""智能体域模型（文件级说明）。

包含表：agent_threads（智能体会话）、langgraph_store（长期记忆 KV）、
asset_embeddings（资产特征向量）。
严格对应架构设计文档 §5.3.29、§5.3.31（MA-04 ~ MA-07 / RD-15）。

注意：
1. langgraph_checkpoints / langgraph_checkpoint_writes 两张检查点表由
   LangGraph PostgresSaver 框架自动创建维护，本模块不手工建模。
2. langgraph_store / asset_embeddings 的 embedding 原设计为 vector(1024)，
   本机无 pgvector 扩展，以 JSONB 承载（预留 pgvector 升级点）。
3. langgraph_store 兼作站点设置 KV 存储（namespace='site_settings'，
   复用说明见 init_db.seed_data）。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import TIMESTAMP, BigInteger, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AgentThread(Base):
    """智能体会话表（§5.3.29 agent_threads，MA-04 / MA-05 / MA-06）。

    Attributes:
        thread_id: 会话标识（格式 project_id 或 project_id:stage，唯一）。
        project_id: 项目 ID（外键 CASCADE，可空）。
        user_id: 用户 ID（外键 CASCADE）。
        stage: 当前阶段。
        state: 共享状态快照（State Schema，JSONB）。
        rewind_checkpoint: MA-06 Time Travel 回溯目标检查点 ID（String 可空）；
            rewind 端点写入，chat_stream 构建运行 config 时作为 checkpoint_id
            注入实现「分叉续聊」，本次对话完成后由服务端清空（一次性生效）。
    """

    __tablename__ = "agent_threads"
    __table_args__ = (
        Index("uk_threads_thread", "thread_id", unique=True),
        Index("idx_threads_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    thread_id: Mapped[str] = mapped_column(String(80), nullable=False,
                                           comment="会话标识（project_id 或 project_id:stage）")
    project_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, comment="项目 ID")
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="用户 ID")
    stage: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, comment="当前阶段")
    state: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="共享状态快照（State Schema）")
    # MA-06 Time Travel：回溯检查点（create_all 不加列，存量库由 init_db 补列逻辑迁移）
    rewind_checkpoint: Mapped[Optional[str]] = mapped_column(
        String(80), nullable=True, comment="回溯目标检查点 ID（一次性，分叉续聊后清空）")


class LanggraphStore(Base):
    """长期记忆 KV 表（§5.3.31 langgraph_store，MA-07）。

    存储资产特征 / 提示词 / 风格 / 用户偏好等长期记忆；
    另兼作站点设置 KV 存储（namespace='site_settings'，复用说明见 seed_data）。

    Attributes:
        namespace: 命名空间。
        key: 键。
        value: 值（JSONB）。
        embedding: 记忆向量（原 vector(1024)，JSONB 承载，预留 pgvector 升级点）。
    """

    __tablename__ = "langgraph_store"
    __table_args__ = (
        Index("uk_store_ns_key", "namespace", "key", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    namespace: Mapped[str] = mapped_column(String(255), nullable=False, comment="命名空间")
    key: Mapped[str] = mapped_column(String(255), nullable=False, comment="键")
    value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="值")
    # 预留 pgvector 升级点：原 vector(1024)，本机无 pgvector，以 JSONB 承载
    embedding: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True, comment="记忆向量（JSONB，预留 pgvector 升级点）")


class AssetEmbedding(Base):
    """资产特征向量表（§5.3.31 asset_embeddings，RD-15）。

    存储资产与参考图特征向量，支撑相似检索与一致性比对。

    Attributes:
        asset_type: 资产类型（character/scene/prop/style 等）。
        asset_id: 资产 ID（多态引用不建外键）。
        embedding: 特征向量（原 vector(1024)，JSONB 承载，预留 pgvector 升级点）。
        model: 提取向量的模型名。
    """

    __tablename__ = "asset_embeddings"
    __table_args__ = (
        Index("idx_ae_asset", "asset_type", "asset_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    asset_type: Mapped[str] = mapped_column(String(20), nullable=False, comment="资产类型")
    asset_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="资产 ID（多态无外键）")
    # 预留 pgvector 升级点：原 vector(1024)，本机无 pgvector，以 JSONB 承载
    embedding: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True, comment="特征向量（JSONB，预留 pgvector 升级点）")
    model: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="提取向量的模型名")
