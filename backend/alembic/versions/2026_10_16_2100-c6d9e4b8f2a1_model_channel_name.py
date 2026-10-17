"""model channel name（模型配置优化：自有渠道备注名列）

数据模型扩展（与 init_db.MISSING_COLUMN_DDL_STATEMENTS 同步）：
- model_channels 加 name（VARCHAR(64)，可空）：用户自有渠道的展示备注名，
  供「自有 Key 管理」列表与编辑对话框使用；平台渠道无备注保持 NULL。

Revision ID: c6d9e4b8f2a1
Revises: b5e8f2a7c3d9
Create Date: 2026-10-16 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c6d9e4b8f2a1'
down_revision: Union[str, Sequence[str], None] = 'b5e8f2a7c3d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema（正向：model_channels 补 name 列 + 列注释，幂等可往返）。

    Raises:
        sqlalchemy.exc.InternalError / OperationalError: 数据库不可达时向上抛出。
    """
    op.add_column('model_channels', sa.Column(
        'name', sa.String(length=64), nullable=True,
        comment='渠道备注名（用户自有渠道展示用）'))


def downgrade() -> None:
    """Downgrade schema（反向：删除 name 列）。

    Raises:
        sqlalchemy.exc.InternalError / OperationalError: 存在依赖引用时向上抛出。
    """
    op.drop_column('model_channels', 'name')
