"""生成任务域模型（文件级说明）。

包含表：tasks（生成任务）。
严格对应架构设计文档 §5.3.20（P-02 ~ P-12 / SYS-01）。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Task(Base):
    """生成任务表（§5.3.20 tasks）。

    Attributes:
        project_id: 项目 ID（外键 SET NULL）。
        storyboard_id: 关联分镜 ID（外键 SET NULL）。
        canvas_node_id: 关联画布节点 ID（外键 SET NULL）。
        type: 任务类型 image/video/tts/compose/llm/extract。
        phase: 阶段 trial（打样）/full（全量）/incremental（增量，RD-27/RD-32）。
        status: 状态 queued/running/done/failed/canceled。
        provider_task_id: 上游任务 ID。
        result_url: 结果地址。
        error_code: 错误码（区分平台故障/内容违规，RD-28）。
        error_message: 错误信息。
        retry_count: 重试次数。
        cost_points: 消耗积分。
        idempotency_key: 幂等键（唯一，P-11 / RD-28）。
        estimated_seconds: 预估剩余秒数（RD-29）。
        progress: 进度百分比（0-100）。
        priority: 调度优先级（0-9，越大越先被 worker 领取，默认 5）。
        next_retry_at: 下次允许重试的时间（指数退避调度，NULL 表示立即可领）。
        dead_letter: 死信标记（重试耗尽仍失败的任务置 True，进入管理端死信队列）。
    """

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("type IN ('image', 'video', 'tts', 'compose', 'llm', 'extract')", name="ck_tasks_type"),
        CheckConstraint("phase IN ('trial', 'full', 'incremental')", name="ck_tasks_phase"),
        CheckConstraint("status IN ('queued', 'running', 'done', 'failed', 'canceled')", name="ck_tasks_status"),
        CheckConstraint("progress >= 0 AND progress <= 100", name="ck_tasks_progress"),
        Index("uk_tasks_idem", "idempotency_key", unique=True),
        Index("idx_tasks_status", "status"),
        Index("idx_tasks_project", "project_id"),
        Index("idx_tasks_sb", "storyboard_id"),
        Index("idx_tasks_node", "canvas_node_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    project_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, comment="项目 ID")
    storyboard_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("storyboards.id", ondelete="SET NULL"), nullable=True, comment="关联分镜 ID")
    canvas_node_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("canvas_nodes.id", ondelete="SET NULL"), nullable=True, comment="关联画布节点 ID")
    type: Mapped[str] = mapped_column(String(20), nullable=False, comment="任务类型")
    phase: Mapped[str] = mapped_column(String(20), nullable=False, server_default="full",
                                       comment="阶段：trial/full/incremental（RD-27/RD-32）")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued",
                                        comment="状态：queued/running/done/failed/canceled")
    provider_task_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="上游任务 ID")
    result_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="结果地址")
    error_code: Mapped[Optional[str]] = mapped_column(String(30), nullable=True,
                                                      comment="错误码（区分平台故障/内容违规）")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="错误信息")
    retry_count: Mapped[int] = mapped_column(nullable=False, server_default="0", comment="重试次数")
    cost_points: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0", comment="消耗积分")
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False, comment="幂等键（P-11/RD-28）")
    estimated_seconds: Mapped[Optional[int]] = mapped_column(nullable=True, comment="预估剩余秒数（RD-29）")
    progress: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0", comment="进度百分比")
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="5", index=True,
        comment="调度优先级（0-9，越大越先执行，默认 5）")
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="下次允许重试时间（指数退避；NULL 表示立即可领取）")
    dead_letter: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", index=True,
        comment="死信标记（重试耗尽仍失败，进入管理端死信队列）")
    # 任务附加信息：目标资产/提示词/时长等
    meta: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True,
                                                 comment="任务附加信息：目标资产/提示词/时长等")
