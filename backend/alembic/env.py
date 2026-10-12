"""Alembic 迁移环境配置（文件级说明）。

职责：
- 以异步模式执行迁移：经 asyncpg 异步引擎驱动 upgrade / downgrade / autogenerate
- 连接串解析优先级：环境变量 ALEMBIC_DATABASE_URL > 应用配置 settings.database_url
  （alembic.ini 中 sqlalchemy.url 仅作占位，运行时一律动态注入，便于测试/生产环境覆盖）
- 导入 app.models 触发全部 32 张业务表模型注册，target_metadata 指向 Base.metadata，
  保证 autogenerate 对照完整 ORM 元数据生成差异

使用方式（cwd=backend）：
- 生成迁移：alembic revision --autogenerate -m "..."
- 升级：    alembic upgrade head
- 回滚：    alembic downgrade <target>
"""

import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 将 backend 目录加入 sys.path：alembic.ini 的 prepend_sys_path=. 依赖"以 backend 为
# 工作目录执行 CLI"，此处按 env.py 文件位置兜底，保证程序化调用（启动钩子）在任意
# 工作目录下均可导入 app 包
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.core.config import settings  # noqa: E402
from app.core.database import Base  # noqa: E402
import app.models  # noqa: E402,F401 —— 导入全部模型以注册到 Base.metadata

# Alembic 配置对象：提供 alembic.ini 中各配置项的访问入口
config = context.config

# 日志：沿用 alembic.ini 的 [logger_*] 配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 元数据目标：全部业务模型（Base.metadata 含命名约定，约束名确定性渲染）
target_metadata = Base.metadata

# langgraph-checkpoint-postgres 运行时自管表：由 AsyncPostgresSaver.setup() 在应用
# 启动时自动建表/自动迁移（checkpoint_migrations 为其内置 schema 版本表）。
# 这些表不属于 ORM 元数据，若纳入对比，autogenerate 会给出 drop_table 破坏性建议，
# 故在 include_object 中一律排除。
RUNTIME_MANAGED_TABLES = frozenset({
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
})


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """autogenerate 对象过滤器：排除 langgraph 运行时自管表，其余对象全部纳入。

    Args:
        obj: 当前对比对象（Table / Column / Index / Constraint 等）。
        name: 对象名称。
        type_: 对象类型（table / column / index / foreign_key_constraint 等）。
        reflected: 是否来自数据库反射侧。
        compare_to: 对比另一侧的对应对象（首次生成时为 None）。

    Returns:
        bool: True 纳入对比与生成；False 跳过该对象。
    """
    if type_ == "table" and name in RUNTIME_MANAGED_TABLES:
        return False
    return True


def _resolve_database_url() -> str:
    """解析迁移目标库连接串（优先级：ALEMBIC_DATABASE_URL 环境变量 > settings.database_url）。

    解析结果回写 alembic 配置的 sqlalchemy.url，供 async_engine_from_config 读取；
    应用启动钩子（programmatic API）也可经 config.set_main_option 预先注入运行时 URL。

    Returns:
        str: SQLAlchemy 连接串（asyncpg 异步驱动）。
    """
    url = os.getenv("ALEMBIC_DATABASE_URL") or settings.database_url
    config.set_main_option("sqlalchemy.url", url)
    return url


def run_migrations_offline() -> None:
    """离线模式：不连接数据库，仅将迁移渲染为 SQL 脚本（alembic --sql 模式）。

    Raises:
        Exception: 连接串解析失败时向上抛出。
    """
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        # 显式关闭 server_default 比对：JSONB 默认值与 now() 在 ORM 与数据库之间的
        # 渲染存在格式差异，开启会产生无意义 alter_column 噪音 diff
        compare_server_default=False,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在同步外观连接上执行迁移上下文（由异步连接经 run_sync 桥接调度）。

    Args:
        connection: async_engine 桥接出的同步外观连接。
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_server_default=False,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """异步在线迁移：以 NullPool 建立独立 asyncpg 引擎执行迁移。

    Raises:
        Exception: 数据库连接失败或迁移脚本异常时向上抛出。
    """
    _resolve_database_url()
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式入口：经 asyncio.run 驱动异步迁移（alembic CLI 为同步调用方）。

    注意：应用启动钩子经 asyncio.to_thread 在工作线程中调用 env.py 时，该线程
    不存在运行中的事件循环，asyncio.run 可安全创建并驱动新的事件循环。
    """
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
