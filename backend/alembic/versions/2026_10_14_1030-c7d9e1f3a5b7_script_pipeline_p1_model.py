"""script pipeline P-1 model（剧本创作模块范式级重构 · Task 2）

P-1 数据模型扩展：
- projects 新增 script_phase（剧本创作阶段状态机，NULL=快速模式），
  附 CHECK 约束 ck_projects_ck_projects_script_phase（四值枚举，NULL 不拦截）
- characters 新增角色域七列（role_type/age/personality/motivation/arc/
  relations/confirmed_at，全部可空），附 CHECK 约束
  ck_characters_ck_characters_role_type（四值枚举）
- episodes 新增大纲域三列（logline/outline/confirmed_at，全部可空）
- version_snapshots.stage 由 varchar(20) 扩宽至 varchar(30)，容纳
  characters_confirmed/outline_confirmed/body_generated 新取值

Revision ID: c7d9e1f3a5b7
Revises: e2b3b5b5c90a
Create Date: 2026-10-14 10:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c7d9e1f3a5b7'
down_revision: Union[str, Sequence[str], None] = 'e2b3b5b5c90a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema（正向：补列 + 补注释 + 建约束 + 扩宽 stage）。

    全部新列均 nullable 且不设 server_default：存量行保持 NULL
    （script_phase NULL 即快速模式，与既有数据语义兼容）。
    约束命名经 op.f() 显式渲染为 naming_convention 结果
    （ck_%(table_name)s_%(constraint_name)s），与 ORM / create_all 完全一致。
    """
    # ---------- projects：阶段状态机列 + CHECK ----------
    op.add_column('projects', sa.Column(
        'script_phase', sa.String(length=30), nullable=True,
        comment='剧本创作阶段：idea/characters/outline/body；NULL=快速模式'))
    # op.f() 包装：标记约束名为 naming_convention 渲染后的最终名（与基线迁移一致），
    # 避免迁移上下文再次套用 ck_%(table_name)s_%(constraint_name)s 造成双重前缀
    op.create_check_constraint(
        op.f('ck_projects_ck_projects_script_phase'), 'projects',
        "script_phase IN ('idea', 'characters', 'outline', 'body')")

    # ---------- characters：P-1 角色域七列 + role_type CHECK ----------
    op.add_column('characters', sa.Column(
        'role_type', sa.String(length=20), nullable=True,
        comment='角色定位（P-1）：lead 主角/support 配角/villain 反派/extra 群像'))
    op.add_column('characters', sa.Column(
        'age', sa.String(length=20), nullable=True,
        comment='年龄设定（P-1，字符串容纳约30岁/少年等弹性表述）'))
    op.add_column('characters', sa.Column(
        'personality', sa.Text(), nullable=True, comment='性格设定（P-1）'))
    op.add_column('characters', sa.Column(
        'motivation', sa.Text(), nullable=True, comment='行为动机（P-1）'))
    op.add_column('characters', sa.Column(
        'arc', sa.Text(), nullable=True, comment='人物弧光（P-1）'))
    op.add_column('characters', sa.Column(
        'relations', postgresql.JSONB(astext_type=sa.Text()), nullable=True,
        comment='人物关系数组（P-1，JSONB：[{target, relation}]）'))
    op.add_column('characters', sa.Column(
        'confirmed_at', sa.TIMESTAMP(timezone=True), nullable=True,
        comment='角色设定确认时间（P-1；NULL=候选/未确认）'))
    op.create_check_constraint(
        op.f('ck_characters_ck_characters_role_type'), 'characters',
        "role_type IN ('lead', 'support', 'villain', 'extra')")

    # ---------- episodes：P-1 大纲域三列 ----------
    op.add_column('episodes', sa.Column(
        'logline', sa.String(length=200), nullable=True, comment='单集一句话梗概（P-1）'))
    op.add_column('episodes', sa.Column(
        'outline', postgresql.JSONB(astext_type=sa.Text()), nullable=True,
        comment='单集大纲（P-1）：core_event/characters/emotion_curve/duration_sec'))
    op.add_column('episodes', sa.Column(
        'confirmed_at', sa.TIMESTAMP(timezone=True), nullable=True,
        comment='单集大纲确认时间（P-1）'))

    # ---------- version_snapshots：stage 扩宽 varchar(20)→varchar(30) ----------
    # PostgreSQL 同类型扩长无需 USING 子句；同步更新列注释补充新取值
    op.alter_column(
        'version_snapshots', 'stage',
        existing_type=sa.String(length=20),
        type_=sa.String(length=30),
        existing_comment='阶段', existing_nullable=False,
        comment='阶段：extract_confirmed/characters_confirmed/outline_confirmed/body_generated 等')


def downgrade() -> None:
    """Downgrade schema（反向：完整回滚 P-1 模型扩展）。

    Raises:
        alembic.util.exc.CommandError / sqlalchemy.exc.DataError:
            若存量数据中存在超长 stage（>20 字符）或依赖新列的业务数据，
            回滚相应步骤时将由数据库约束报错。
    """
    # ---------- version_snapshots：stage 收窄回 varchar(20) ----------
    op.alter_column(
        'version_snapshots', 'stage',
        existing_type=sa.String(length=30),
        type_=sa.String(length=20),
        existing_comment='阶段：extract_confirmed/characters_confirmed/outline_confirmed/body_generated 等',
        existing_nullable=False,
        comment='阶段')

    # ---------- episodes：回滚大纲域三列 ----------
    op.drop_column('episodes', 'confirmed_at')
    op.drop_column('episodes', 'outline')
    op.drop_column('episodes', 'logline')

    # ---------- characters：回滚 CHECK + 角色域七列 ----------
    op.drop_constraint(
        op.f('ck_characters_ck_characters_role_type'), 'characters', type_='check')
    op.drop_column('characters', 'confirmed_at')
    op.drop_column('characters', 'relations')
    op.drop_column('characters', 'arc')
    op.drop_column('characters', 'motivation')
    op.drop_column('characters', 'personality')
    op.drop_column('characters', 'age')
    op.drop_column('characters', 'role_type')

    # ---------- projects：回滚 CHECK + 阶段列 ----------
    op.drop_constraint(
        op.f('ck_projects_ck_projects_script_phase'), 'projects', type_='check')
    op.drop_column('projects', 'script_phase')
