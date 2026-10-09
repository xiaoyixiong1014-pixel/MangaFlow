"""剧本域模型（文件级说明）。

包含表：scripts（剧本）。
严格对应架构设计文档 §5.3.8。
"""

from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Script(Base):
    """剧本表（§5.3.8 scripts）。

    Attributes:
        project_id: 项目 ID（外键 CASCADE）。
        episode_id: 所属剧集 ID（外键 SET NULL，可空）。
        content: 结构化剧本内容（JSONB，含字段级 source 标记 RD-03）。
        raw_text: 原始输入文本。
        fences: 别名归一与冲突记录（RD-05 / RD-06，JSONB）。
        health_report: 结构化体检结果（RD-02，JSONB）。
        version: 版本号。
    """

    __tablename__ = "scripts"
    __table_args__ = (
        Index("uk_scripts_project", "project_id", "episode_id", unique=True),
        Index("idx_scripts_version", "project_id", "version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID（级联删除）")
    episode_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("episodes.id", ondelete="SET NULL"), nullable=True, comment="所属剧集 ID")
    content: Mapped[dict] = mapped_column(JSONB, nullable=False,
                                          comment="结构化剧本（每字段带 source 标记 ai|user，RD-03）")
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="原始输入文本")
    fences: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="别名归一与冲突记录（RD-05/RD-06）")
    health_report: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="结构化体检结果（RD-02）")
    version: Mapped[int] = mapped_column(nullable=False, server_default="1", comment="版本号")
