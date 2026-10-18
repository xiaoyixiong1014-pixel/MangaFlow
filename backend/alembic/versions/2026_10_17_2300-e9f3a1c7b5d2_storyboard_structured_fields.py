"""Storyboard structured fields（方案 B 分镜结构化扩展）

分镜 agent 完整化落地：
- storyboards 补 4 列：camera_angle（机位与角度）、lighting（环境与光影）、
  negative_prompt（负面提示词）、audio（JSONB 声音结构化 {sfx, emotion}）；
- 新建 storyboard_props 关联表（RD-24 强绑口径与 storyboard_characters 一致）：
  分镜-道具强绑 + prop_version 版本快照（纳入 stale 判定）。

Revision ID: e9f3a1c7b5d2
Revises: d8e2b7c5a9f3
Create Date: 2026-10-17 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e9f3a1c7b5d2'
down_revision: Union[str, Sequence[str], None] = 'd8e2b7c5a9f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema（正向：storyboards 补 4 列 + 建 storyboard_props 表，幂等可往返）。"""
    op.add_column('storyboards', sa.Column(
        'camera_angle', sa.String(length=20), nullable=True,
        comment='机位与角度：平视/俯拍/仰拍/斜角/过肩/顶摄（方案 B 扩展）'))
    op.add_column('storyboards', sa.Column(
        'lighting', sa.Text(), nullable=True,
        comment='环境与光影描述（分层提示词「环境与光影层」）'))
    op.add_column('storyboards', sa.Column(
        'negative_prompt', sa.Text(), nullable=True,
        comment='负面提示词（分层提示词「负面排除层」）'))
    op.add_column('storyboards', sa.Column(
        'audio', sa.JSON(), nullable=True,
        comment='声音结构化信息：{sfx: 音效列表, emotion: 情绪标签}'))
    op.create_table(
        'storyboard_props',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False, comment='主键'),
        sa.Column('storyboard_id', sa.BigInteger(), nullable=False, comment='分镜 ID（级联删除）'),
        sa.Column('prop_id', sa.BigInteger(), nullable=False, comment='道具资产 ID'),
        sa.Column('link_confidence', sa.Numeric(4, 3), nullable=True, comment='实体链接置信度（低置信标黄）'),
        sa.Column('prop_version', sa.Integer(), nullable=True,
                  comment='引用时 props.asset_version 快照（P0-3）'),
        # TimestampMixin（Base 通用字段）：ORM 会 SELECT created_at/updated_at，建表必须带
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(),
                  nullable=False, comment='创建时间（TIMESTAMPTZ，默认 now()）'),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.func.now(),
                  nullable=False, comment='更新时间（TIMESTAMPTZ，行更新时自动刷新）'),
        sa.ForeignKeyConstraint(['storyboard_id'], ['storyboards.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['prop_id'], ['props.id'], ondelete='RESTRICT'),
        sa.CheckConstraint('link_confidence >= 0 AND link_confidence <= 1', name='ck_sbp_confidence'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('pk_sb_prop', 'storyboard_props', ['storyboard_id', 'prop_id'], unique=True)
    op.create_index('idx_sbp_prop', 'storyboard_props', ['prop_id'])


def downgrade() -> None:
    """Downgrade schema（反向：删 storyboard_props 表与 storyboards 4 列）。"""
    op.drop_index('idx_sbp_prop', table_name='storyboard_props')
    op.drop_index('pk_sb_prop', table_name='storyboard_props')
    op.drop_table('storyboard_props')
    op.drop_column('storyboards', 'audio')
    op.drop_column('storyboards', 'negative_prompt')
    op.drop_column('storyboards', 'lighting')
    op.drop_column('storyboards', 'camera_angle')
