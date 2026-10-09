"""数据库连接与会话管理模块（文件级说明）。

提供：
- 异步引擎 engine（连接 mangaflow 业务库）
- 异步会话工厂 SessionLocal
- FastAPI 依赖 get_db()
- 全模型声明基类 Base（通过 TimestampMixin 自带 created_at / updated_at 通用时间戳列）

说明：本机未安装 pgvector 扩展，凡文档中的 vector(1024) 向量列一律以 JSONB
承载（字段名仍叫 embedding），模型文件头已注明预留 pgvector 升级点。
"""

from datetime import datetime
from typing import AsyncGenerator

from sqlalchemy import TIMESTAMP, MetaData, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import settings

# 约束命名约定：保证 FK/UQ/CK/PK 生成确定性名称，便于后续迁移与差异比对
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class TimestampMixin:
    """时间戳混入类。

    为所有继承 Base 的业务模型自动添加 created_at / updated_at 两个
    TIMESTAMPTZ 通用列（对应架构文档 §5.1 通用字段规范）。
    """

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间（TIMESTAMPTZ，默认 now()）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间（TIMESTAMPTZ，行更新时自动刷新）",
    )


class Base(TimestampMixin, DeclarativeBase):
    """全模型声明基类。

    所有业务模型继承本类；子类自动获得 created_at / updated_at 列，
    并统一使用带命名约定的 metadata。
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# 异步引擎：echo=False 不打印 SQL；pool_pre_ping 取连接前探活，防止失效连接
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

# 异步会话工厂：expire_on_commit=False 提交后不失效对象，避免懒加载触发隐式异步 IO
SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：提供一个请求级异步数据库会话。

    Yields:
        AsyncSession: 异步会话对象，请求结束后在 finally 中确保关闭。

    Raises:
        Exception: 数据库连接/执行异常向上抛出，由全局异常处理器兜底。
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        await session.close()
