"""story setting P2 model（剧本工作台改造 · P2 故事设定产物）

P2 数据模型扩展：
- projects 新增 story_setting（故事设定 JSONB，可空）：存储结构
  {logline: 一句话故事, core_conflict: 核心冲突, worldview: 世界观,
  source: "ai"|"user", updated_at: ISO 时间戳}。
  全量新增列 nullable 且不设 server_default：存量行保持 NULL
  （尚未生成设定，行为不受影响），新库由 ORM/create_all 直建亦带该列。

Revision ID: a3f7c2e9d4b1
Revises: c7d9e1f3a5b7
Create Date: 2026-10-14 11:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a3f7c2e9d4b1'
down_revision: Union[str, Sequence[str], None] = 'c7d9e1f3a5b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema（正向：projects 补 story_setting JSONB 列 + 注释）。

    幂等可往返：upgrade/downgrade 严格互逆（加列/删列成对），往返后 schema
    还原；JSONB 列无需类型转换，存量行保持 NULL 不受影响。
    """
    op.add_column('projects', sa.Column(
        'story_setting', postgresql.JSONB(astext_type=sa.Text()), nullable=True,
        comment='故事设定（P2）：logline/core_conflict/worldview/source(ai|user)/updated_at'))


def downgrade() -> None:
    """Downgrade schema（反向：删除 projects.story_setting 列）。

    Raises:
        sqlalchemy.exc.InternalError / OperationalError:
            若删除时该列存在依赖对象（如已建表达式索引），由数据库约束报错
            （本迁移不创建任何依赖该列的索引/约束，正常链路不会触发）。
    """
    op.drop_column('projects', 'story_setting')
