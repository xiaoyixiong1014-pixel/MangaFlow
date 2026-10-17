"""P0 asset consistency loop（资产一致性闭环 · P0-3 版本透传 / P0-4 视觉一致性 / P2 快赢）

数据模型扩展（与 init_db.MISSING_COLUMN_DDL_STATEMENTS 同步）：
- P0-3 版本透传：characters/scenes/props/asset_variants 各加 asset_version
  （INTEGER NOT NULL DEFAULT 1，首次生成置 1、替换/重生成回写 +1）；
  storyboards 加 scene_version、storyboard_characters 加 character_version /
  variant_version（引用时版本快照，NULL=旧数据视为一致防存量误报）。
- P0-4 视觉一致性：characters 加 face_profile（JSONB，Agnes 面部档案）；
  asset_embeddings 加 kind（VARCHAR(20) NOT NULL DEFAULT 'text'，text/face，
  CHECK 约束 ck_ae_kind）+ 索引 idx_ae_asset_kind。
- P2 styles 软删统一：styles 加 deleted_at（TIMESTAMPTZ，可空），
  与 characters/scenes/props 软删语义对齐。

Revision ID: b5e8f2a7c3d9
Revises: a3f7c2e9d4b1
Create Date: 2026-10-16 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b5e8f2a7c3d9'
down_revision: Union[str, Sequence[str], None] = 'a3f7c2e9d4b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema（正向：补列 + 索引 + CHECK，幂等可往返）。

    加列/删列严格成对互逆；版本列 NOT NULL DEFAULT 1 使存量行自动填充 1
    （语义=当前版本基线，旧快照列均为 NULL 视为一致）。
    """
    # P0-3：四类资产版本号
    op.add_column('characters', sa.Column(
        'asset_version', sa.Integer(), nullable=False, server_default='1',
        comment='资产图版本号（P0-3：图回写/替换时递增）'))
    op.add_column('scenes', sa.Column(
        'asset_version', sa.Integer(), nullable=False, server_default='1',
        comment='资产图版本号（P0-3：图回写/替换时递增）'))
    op.add_column('props', sa.Column(
        'asset_version', sa.Integer(), nullable=False, server_default='1',
        comment='资产图版本号（P0-3：图回写/替换时递增）'))
    op.add_column('asset_variants', sa.Column(
        'asset_version', sa.Integer(), nullable=False, server_default='1',
        comment='资产图版本号（P0-3：图回写/替换时递增）'))
    # P0-3：分镜引用版本快照（NULL=旧数据视为一致）
    op.add_column('storyboards', sa.Column(
        'scene_version', sa.Integer(), nullable=True,
        comment='绑定场景时 scenes.asset_version 快照（P0-3）'))
    op.add_column('storyboard_characters', sa.Column(
        'character_version', sa.Integer(), nullable=True,
        comment='引用时 characters.asset_version 快照（P0-3）'))
    op.add_column('storyboard_characters', sa.Column(
        'variant_version', sa.Integer(), nullable=True,
        comment='引用时 asset_variants.asset_version 快照（P0-3）'))
    # P0-4：面部档案 + 向量类别
    op.add_column('characters', sa.Column(
        'face_profile', postgresql.JSONB(astext_type=sa.Text()), nullable=True,
        comment='面部档案（Agnes 视觉提取的结构化特征 JSON，P0-4）'))
    op.add_column('asset_embeddings', sa.Column(
        'kind', sa.String(length=20), nullable=False, server_default='text',
        comment='向量类别：text 文本特征 / face 面部档案特征（P0-4）'))
    op.create_index('idx_ae_asset_kind', 'asset_embeddings',
                    ['asset_type', 'asset_id', 'kind'])
    op.create_check_constraint('ck_ae_kind', 'asset_embeddings',
                               "kind IN ('text', 'face')")
    # P2：styles 软删统一
    op.add_column('styles', sa.Column(
        'deleted_at', sa.TIMESTAMP(timezone=True), nullable=True,
        comment='软删除时间'))


def downgrade() -> None:
    """Downgrade schema（反向：按 upgrade 逆序删列/删索引/删约束）。

    Raises:
        sqlalchemy.exc.InternalError / OperationalError:
            若存在依赖被删对象的引用（正常链路不会触发，本迁移对象间无依赖）。
    """
    op.drop_column('styles', 'deleted_at')
    op.drop_constraint('ck_ae_kind', 'asset_embeddings', type_='check')
    op.drop_index('idx_ae_asset_kind', table_name='asset_embeddings')
    op.drop_column('asset_embeddings', 'kind')
    op.drop_column('characters', 'face_profile')
    op.drop_column('storyboard_characters', 'variant_version')
    op.drop_column('storyboard_characters', 'character_version')
    op.drop_column('storyboards', 'scene_version')
    op.drop_column('asset_variants', 'asset_version')
    op.drop_column('props', 'asset_version')
    op.drop_column('scenes', 'asset_version')
    op.drop_column('characters', 'asset_version')
