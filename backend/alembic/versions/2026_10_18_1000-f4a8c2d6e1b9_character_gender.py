"""Character gender（角色性别字段：agent 角色设定生成，生图提示词性别锚定）

characters 加 gender（VARCHAR(10) 可空，取值男性/女性/中性）：剧本创作「角色
设定」的 character_planner agent 生成时输出，确认入池落库；资产管理生成定妆照
时随提示词注入（「角色性别：…」），让生图模型准确把握人物性别特征。

Revision ID: f4a8c2d6e1b9
Revises: e9f3a1c7b5d2
Create Date: 2026-10-18 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f4a8c2d6e1b9'
down_revision: Union[str, Sequence[str], None] = 'e9f3a1c7b5d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema（正向：characters 补 gender 列 + 注释，幂等可往返）。"""
    op.add_column('characters', sa.Column(
        'gender', sa.String(length=10), nullable=True,
        comment='角色性别（男性/女性/中性；角色设定 agent 生成）'))


def downgrade() -> None:
    """Downgrade schema（反向：删除 characters.gender 列）。"""
    op.drop_column('characters', 'gender')
