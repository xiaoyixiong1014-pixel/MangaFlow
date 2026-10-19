"""user disabled model ids（模型配置优化：系统模型按用户启用/停用）

数据模型扩展（与 init_db.MISSING_COLUMN_DDL_STATEMENTS 同步）：
- users 加 disabled_model_ids（JSONB DEFAULT '[]'）：用户停用的逻辑模型 ID
  数组；NULL/[]=全部启用（默认行为不变）。停用后生成路由跳过该逻辑模型的
  平台渠道链，仅自有渠道参与 + mock 哨兵兜底。

Revision ID: d8a1f5c9e3b2
Revises: c6d9e4b8f2a1
Create Date: 2026-10-18 10:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd8a1f5c9e3b2'
down_revision: Union[str, Sequence[str], None] = 'f4a8c2d6e1b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema（正向：users 补 disabled_model_ids 列 + 注释，幂等可往返）。

    幂等处理：本地库可能已由 init_db.MISSING_COLUMN_DDL_STATEMENTS 预先补列
    （ADD COLUMN IF NOT EXISTS），此处先查 information_schema 再加列，避免
    duplicate column 报错；列注释始终覆盖执行（幂等）。

    Raises:
        sqlalchemy.exc.InternalError / OperationalError: 数据库不可达时向上抛出。
    """
    conn = op.get_bind()
    exists = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'users' "
        "AND column_name = 'disabled_model_ids'"
    )).scalar()
    if not exists:
        op.add_column('users', sa.Column(
            'disabled_model_ids', postgresql.JSONB(astext_type=sa.Text()),
            nullable=True, server_default=sa.text("'[]'::jsonb"),
            comment='用户停用的逻辑模型 ID 数组（JSONB；NULL/[]=全部启用）'))
    conn.execute(sa.text(
        "COMMENT ON COLUMN users.disabled_model_ids IS "
        "'用户停用的逻辑模型 ID 数组（JSONB；NULL/[]=全部启用）'"))


def downgrade() -> None:
    """Downgrade schema（反向：删除 disabled_model_ids 列）。

    Raises:
        sqlalchemy.exc.InternalError / OperationalError: 存在依赖引用时向上抛出。
    """
    op.drop_column('users', 'disabled_model_ids')
