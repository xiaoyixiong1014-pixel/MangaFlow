"""P1 episode asset gating（按集工作流 · 角色资产启用状态）

characters 加 enabled_at（TIMESTAMPTZ 可空）：角色设定确认后为 NULL
（流水线内部数据，不进资产管理），用户经「启用资产」抽取确认入池时置 now
（出现在资产管理并可参与分镜/生成）。

Revision ID: c7f1a9b4e2d6
Revises: c6d9e4b8f2a1
Create Date: 2026-10-17 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c7f1a9b4e2d6'
down_revision: Union[str, Sequence[str], None] = 'c6d9e4b8f2a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema（正向：characters 补 enabled_at 列 + 注释，幂等可往返）。"""
    op.add_column('characters', sa.Column(
        'enabled_at', sa.TIMESTAMP(timezone=True), nullable=True,
        comment='资产启用时间（角色设定确认=NULL 不进资产管理；启用资产确认=now）'))


def downgrade() -> None:
    """Downgrade schema（反向：删除 characters.enabled_at 列）。"""
    op.drop_column('characters', 'enabled_at')
