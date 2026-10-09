"""审核域模型（文件级说明）。

包含表：review_records（审核记录）。
严格对应架构设计文档 §5.3.27（R-08 / C-01 ~ C-04，三级审核闸门）。
"""

from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReviewRecord(Base):
    """审核记录表（§5.3.27 review_records，R-08 / C-01 ~ C-04）。

    三级闸门（RD-34）：generate（生成）→ compose（合成）→ publish（发布）。

    Attributes:
        project_id: 项目 ID（外键 CASCADE）。
        target_type: 目标类型 text/image/video/film。
        target_id: 目标 ID（如 storyboard_id，多态引用不建外键）。
        gate: 审核闸门 generate/compose/publish（RD-34 三级闸门）。
        result: 机审结果 pass/suspect/violate。
        violation_type: 违规类型。
        manual_status: 人工复核状态 pending/approved/rejected。
        handler_id: 处理人 ID（外键 SET NULL）。
        suggestion: 可操作建议（RD-36）。
    """

    __tablename__ = "review_records"
    __table_args__ = (
        CheckConstraint("target_type IN ('text', 'image', 'video', 'film')", name="ck_rr_target_type"),
        CheckConstraint("gate IN ('generate', 'compose', 'publish')", name="ck_rr_gate"),
        CheckConstraint("result IN ('pass', 'suspect', 'violate')", name="ck_rr_result"),
        CheckConstraint("manual_status IS NULL OR manual_status IN ('pending', 'approved', 'rejected')",
                        name="ck_rr_manual_status"),
        Index("idx_rr_project", "project_id"),
        Index("idx_rr_gate", "gate"),
        Index("idx_rr_result", "result"),
        Index("idx_rr_manual", "manual_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    project_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, comment="项目 ID（级联删除）")
    target_type: Mapped[str] = mapped_column(String(20), nullable=False, comment="目标类型：text/image/video/film")
    target_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True,
                                                     comment="目标 ID（如 storyboard_id，多态无外键）")
    gate: Mapped[str] = mapped_column(String(20), nullable=False, comment="审核闸门（RD-34 三级闸门）")
    result: Mapped[str] = mapped_column(String(20), nullable=False, comment="机审结果：pass/suspect/violate")
    violation_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, comment="违规类型")
    manual_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True,
                                                         comment="人工复核：pending/approved/rejected")
    handler_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="处理人 ID")
    suggestion: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="可操作建议（RD-36）")
