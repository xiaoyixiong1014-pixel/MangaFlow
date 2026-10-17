"""Episode enabled assets（按集工作流 · 该集已启用资产名单）

episodes 加 enabled_assets（JSONB 可空）：该集经「启用资产」抽取确认入池的
资产名名单（{"characters":[...],"scenes":[...],"props":[...],"styles":[...]}，
union 追加）；资产管理页按集视图仅显示名单内资产，NULL=该集未启用过任何资产。

Revision ID: d8e2b7c5a9f3
Revises: c7f1a9b4e2d6
Create Date: 2026-10-17 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd8e2b7c5a9f3'
down_revision: Union[str, Sequence[str], None] = 'c7f1a9b4e2d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema（正向：episodes 补 enabled_assets 列 + 注释，幂等可往返）。"""
    op.add_column('episodes', sa.Column(
        'enabled_assets', sa.JSON(), nullable=True,
        comment='该集已启用资产名名单（启用资产确认入池时 union 追加）'))


def downgrade() -> None:
    """Downgrade schema（反向：删除 episodes.enabled_assets 列）。"""
    op.drop_column('episodes', 'enabled_assets')
