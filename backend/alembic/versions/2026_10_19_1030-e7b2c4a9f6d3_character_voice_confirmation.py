"""character voice confirmation（音色确认闭环：试音 → 锁定 → 门禁配音）

数据模型扩展（与 init_db.MISSING_COLUMN_DDL_STATEMENTS 同步）：
- characters 加 voice_selected（VARCHAR(20)，锁定时固化的业务音色标识）；
- characters 加 voice_locked（BOOLEAN NOT NULL DEFAULT FALSE，True 后该角色
  所在镜头配音才可发起）；
- characters 加 voice_sample_url（VARCHAR(255)，试音音频产物地址）。

Revision ID: e7b2c4a9f6d3
Revises: d8a1f5c9e3b2
Create Date: 2026-10-19 10:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e7b2c4a9f6d3'
down_revision: Union[str, Sequence[str], None] = 'd8a1f5c9e3b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema（正向：characters 补音色确认三列 + 注释，幂等可往返）。

    幂等处理：本地库可能已由 init_db.MISSING_COLUMN_DDL_STATEMENTS 预先补列
    （ADD COLUMN IF NOT EXISTS），此处先查 information_schema 再加列，避免
    duplicate column 报错；列注释始终覆盖执行（幂等）。

    Raises:
        sqlalchemy.exc.InternalError / OperationalError: 数据库不可达时向上抛出。
    """
    conn = op.get_bind()
    # (列名, 加列语句构造器, server_default SQL 片段或 None, 列注释)
    specs = [
        ('voice_selected',
         lambda: op.add_column('characters', sa.Column(
             'voice_selected', sa.String(20), nullable=True,
             comment='确认音色标识（锁定时固化的 male_01/male_02/female_01/female_02）')),
         None, '确认音色标识（锁定时固化的 male_01/male_02/female_01/female_02）'),
        ('voice_locked',
         lambda: op.add_column('characters', sa.Column(
             'voice_locked', sa.Boolean(), nullable=False,
             server_default=sa.text('false'),
             comment='音色已锁定（True 后该角色所在镜头配音才可发起）')),
         'false', '音色已锁定（True 后该角色所在镜头配音才可发起）'),
        ('voice_sample_url',
         lambda: op.add_column('characters', sa.Column(
             'voice_sample_url', sa.String(255), nullable=True,
             comment='试音音频 URL（「我是XX，这是我的配音」试音产物）')),
         None, '试音音频 URL（「我是XX，这是我的配音」试音产物）'),
    ]
    for col, add_col, default_sql, comment in specs:
        exists = conn.execute(sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'characters' "
            f"AND column_name = '{col}'"
        )).scalar()
        if not exists:
            add_col()
        if default_sql:
            # NOT NULL 列补默认值（存量行为已有 server_default 兜底，此处幂等覆盖）
            conn.execute(sa.text(
                f"ALTER TABLE characters ALTER COLUMN {col} SET DEFAULT {default_sql}"))
        conn.execute(sa.text(f"COMMENT ON COLUMN characters.{col} IS '{comment}'"))


def downgrade() -> None:
    """Downgrade schema（反向：删除音色确认三列）。

    Raises:
        sqlalchemy.exc.InternalError / OperationalError: 存在依赖引用时向上抛出。
    """
    op.drop_column('characters', 'voice_sample_url')
    op.drop_column('characters', 'voice_locked')
    op.drop_column('characters', 'voice_selected')
