"""MangaFlow 后端 pytest 全局夹具模块（文件级说明）。

职责：
- 会话级夹具 test_engine：幂等创建独立测试库 mangaflow_test（绝不触碰主库
  mangaflow 业务数据），并对其执行 Base.metadata.create_all 建全量业务表；
- 会话级夹具 task_runner：把任务执行器（app.tasks.runner）、画布助手落库
  （app.api.agent_chat）与 app.core.database 模块级 SessionLocal 重定向到
  测试库后，启动进程内常驻任务执行器（与用例同事件循环并发运行）；
- 函数级夹具 db / client / auth / admin_auth：独立数据库会话、内存 HTTP
  客户端（ASGITransport，不监听端口、不触发 lifespan）、随机注册用户与
  运营方账号；
- 提供跨测试文件复用的辅助函数（注册用户 / 建项目 / 插入分镜 / 等待任务
  终态 / 解析 SSE 等）。

说明：
1. lifespan 不会被 ASGITransport 触发，因此 init_database 与 start_runner
   均由本文件显式接管，避免误连主库；
2. LangGraph 检查点连接的是主库（settings.database_url，仅写检查点表，
   无外键耦合），不影响测试库数据隔离；
3. 每个用例结束后 TRUNCATE 全表 RESTART IDENTITY CASCADE，保证用例独立。
"""

import asyncio
import json
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# 测试库常量：与主库 mangaflow 完全隔离
TEST_DB_NAME = "mangaflow_test"
# 由主库连接串派生测试库连接串（仅替换库名，账号/主机保持一致）
TEST_DB_URL = make_url(settings.database_url).set(database=TEST_DB_NAME).render_as_string(
    hide_password=False
)
# 管理库连接串（连 postgres 系统库，用于 CREATE DATABASE）
ADMIN_DB_URL = settings.admin_database_url

# 导入全部 ORM 模型，确保 create_all 能发现 31 张业务表
import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.main import app as fastapi_app


# ---------------------------------------------------------------------------
# 会话级夹具：测试库与任务执行器
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture(scope="session")
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """会话级夹具：创建测试库 mangaflow_test 与全部业务表。

    创建流程：
    1. 连接 postgres 系统库（AUTOCOMMIT）查询 pg_database，库不存在则
       CREATE DATABASE（该语句不能在事务块内执行）；
    2. 对测试库执行 Base.metadata.create_all（幂等，存在即跳过），
       随后执行 init_db 的存量表幂等补列清单（create_all 不给已存在的
       表加列）。

    Yields:
        AsyncEngine: 绑定测试库的异步引擎（pytest session 级共享）。

    Raises:
        Exception: PostgreSQL 连接失败或建库/建表异常时向上抛出。
    """
    # ---------- 1. 建库（幂等：先查 pg_database） ----------
    admin_engine = create_async_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        async with admin_engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TEST_DB_NAME}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}" ENCODING \'UTF8\''))
    finally:
        await admin_engine.dispose()

    # ---------- 2. 建表（幂等 create_all + 存量表补列） ----------
    engine = create_async_engine(TEST_DB_URL, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 测试库同为存量库：create_all 不给已存在的表加列，复用主库的
        # 幂等补列清单（Task 2.1 tasks.priority/next_retry_at/dead_letter 等）
        from app.core.init_db import MISSING_COLUMN_DDL_STATEMENTS
        for _table, ddl in MISSING_COLUMN_DDL_STATEMENTS:
            await conn.execute(text(ddl))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def task_runner(test_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """会话级夹具：启动连接测试库的进程内任务执行器。

    - 将 runner / agent_chat / notification_service / database 四个模块的
      SessionLocal 重定向到测试库会话工厂（生成任务、通知落库等内部会话
      均写测试库）；
    - 启动 3 个 worker 协程常驻当前（session 级）事件循环；
    - 会话结束时取消全部 worker 并还原模块属性。

    Args:
        test_engine: 测试库异步引擎（会话级夹具注入）。

    Yields:
        None。

    Raises:
        Exception: 执行器启动失败时向上抛出。
    """
    from app.api import agent_chat as agent_chat_mod
    from app.core import database as db_mod
    from app.services import notification_service as notification_mod
    from app.tasks import runner as runner_mod

    factory = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    # 记录原值以便会话结束后还原
    originals: List[Tuple[Any, str, Any]] = [
        (agent_chat_mod, "SessionLocal", agent_chat_mod.SessionLocal),
        (db_mod, "SessionLocal", db_mod.SessionLocal),
        (notification_mod, "SessionLocal", notification_mod.SessionLocal),
        (runner_mod, "SessionLocal", runner_mod.SessionLocal),
    ]
    agent_chat_mod.SessionLocal = factory
    db_mod.SessionLocal = factory
    notification_mod.SessionLocal = factory
    runner_mod.SessionLocal = factory

    runner_mod._RUNNER_TASKS.clear()
    await runner_mod.start_runner(test_engine)
    try:
        yield
    finally:
        # 取消全部 worker 协程并等待退出
        tasks = list(runner_mod._RUNNER_TASKS)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # 还原模块级 SessionLocal
        for mod, name, old in originals:
            setattr(mod, name, old)


# ---------------------------------------------------------------------------
# 函数级夹具：数据库会话 / HTTP 客户端 / 账号
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def db(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """函数级夹具：为单个用例提供独立 AsyncSession，用例结束后清空全部表。

    Args:
        test_engine: 测试库异步引擎。

    Yields:
        AsyncSession: 测试库异步会话（可直接 ORM 造数 / 查询断言）。

    Raises:
        Exception: 会话关闭或 TRUNCATE 异常向上抛出。
    """
    factory = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    session = factory()
    try:
        yield session
    finally:
        await session.close()
        # 清空所有业务表并重置自增序列，保证用例之间数据完全独立
        table_names = ", ".join(f'"{name}"' for name in sorted(Base.metadata.tables))
        async with test_engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def client(test_engine: AsyncEngine, task_runner: None) -> AsyncGenerator[httpx.AsyncClient, None]:
    """函数级夹具：内存 ASGI HTTP 客户端（重写 get_db 指向测试库）。

    - 使用 httpx.ASGITransport 直接调用 FastAPI 应用，不监听端口；
    - ASGITransport 不会触发 lifespan，因此不会执行主库 init_database；
    - 每个请求通过依赖覆写获得独立的测试库会话。

    Args:
        test_engine: 测试库异步引擎。
        task_runner: 会话级任务执行器（保证生成类用例可用）。

    Yields:
        httpx.AsyncClient: 已配置依赖覆写的异步客户端（超时放宽至 120s，
        容纳真实 LLM 调用与任务轮询）。

    Raises:
        Exception: 请求过程中未被应用层捕获的异常向上抛出。
    """
    factory = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        """依赖覆写：为每个请求提供测试库会话（内部函数）。

        Yields:
            AsyncSession: 测试库异步会话。

        Raises:
            无：请求结束后在 finally 中确保会话关闭。
        """
        session = factory()
        try:
            yield session
        finally:
            await session.close()

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=httpx.Timeout(120.0)
    ) as ac:
        yield ac
    fastapi_app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def auth(client: httpx.AsyncClient) -> Dict[str, Any]:
    """函数级夹具：注册一个随机用户，返回令牌头与用户信息。

    Args:
        client: 内存 HTTP 客户端。

    Returns:
        Dict[str, Any]: {"headers": 认证请求头, "user": 用户信息,
        "user_id": 用户 ID, "payload": 注册请求体}。

    Raises:
        AssertionError: 注册接口返回非 200 时中断用例。
    """
    data = await register_user(client)
    return {
        "headers": {"Authorization": f"Bearer {data['access_token']}"},
        "user": data["user"],
        "user_id": data["user"]["id"],
        "payload": data["_payload"],
    }


@pytest_asyncio.fixture
async def admin_auth(client: httpx.AsyncClient, db: AsyncSession) -> Dict[str, Any]:
    """函数级夹具：在测试库直插一个运营方（operator）账号并登录。

    Args:
        client: 内存 HTTP 客户端（用于登录换令牌）。
        db: 测试库会话（用于直插 operator 用户与钱包）。

    Returns:
        Dict[str, Any]: {"headers": 认证请求头, "user_id": 运营方 ID}。

    Raises:
        AssertionError: 登录接口返回非 200 时中断用例。
    """
    from app.core.security import hash_password
    from app.models import User, Wallet

    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"operator_{suffix}",
        password_hash=hash_password("Operator@123"),
        role="operator",
        status=1,
    )
    db.add(user)
    await db.flush()
    db.add(Wallet(user_id=user.id, points_balance=1_000_000, frozen_points=0, version=0))
    await db.commit()

    resp = await client.post(
        "/api/auth/login", json={"username": user.username, "password": "Operator@123"}
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"headers": {"Authorization": f"Bearer {token}"}, "user_id": user.id}


# ---------------------------------------------------------------------------
# 跨文件复用的辅助函数（非夹具，测试模块通过 tests.conftest 导入）
# ---------------------------------------------------------------------------
async def register_user(client: httpx.AsyncClient, prefix: str = "user") -> Dict[str, Any]:
    """注册一个随机后缀用户并返回完整响应 JSON。

    Args:
        client: 内存 HTTP 客户端。
        prefix: 用户名前缀（便于按用例区分）。

    Returns:
        Dict[str, Any]: 注册响应 JSON（access_token/refresh_token/user），
        额外附加 "_payload"（本次注册请求体）键。

    Raises:
        AssertionError: 注册返回非 200 时中断。
    """
    suffix = uuid.uuid4().hex[:10]
    payload = {
        "username": f"{prefix}_{suffix}",
        "email": f"{prefix}_{suffix}@mangaflow-test.com",
        "password": "Passw0rd!123",
    }
    resp = await client.post("/api/auth/register", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    data["_payload"] = payload
    return data


async def create_project(
    client: httpx.AsyncClient, headers: Dict[str, str], title: Optional[str] = None, **extra: Any
) -> Dict[str, Any]:
    """为当前用户创建一个随机标题项目并返回响应 JSON。

    Args:
        client: 内存 HTTP 客户端。
        headers: 认证请求头。
        title: 项目标题（缺省随机生成）。
        **extra: 其余合法创建字段（genre/style/target_duration 等）。

    Returns:
        Dict[str, Any]: 项目响应 JSON（含 id/status='draft'）。

    Raises:
        AssertionError: 创建返回非 200 时中断。
    """
    payload = {"title": title or f"测试项目_{uuid.uuid4().hex[:8]}", **extra}
    resp = await client.post("/api/projects", headers=headers, json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def insert_storyboard(db: AsyncSession, project_id: int, sequence: int = 1, **overrides: Any):
    """直接 ORM 插入一条分镜（合成/生成链路的前置数据，绕过 LLM 拆解）。

    Args:
        db: 测试库会话。
        project_id: 归属项目 ID。
        sequence: 镜头序号。
        **overrides: 可覆盖的分镜字段（duration/dialogue/video_url 等）。

    Returns:
        Storyboard: 已落库的分镜 ORM 对象。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    from app.models import Storyboard

    sb = Storyboard(
        project_id=project_id,
        sequence=sequence,
        shot_type=overrides.get("shot_type", "中景"),
        camera_movement=overrides.get("camera_movement", "固定"),
        duration=overrides.get("duration", 5),
        visual_description=overrides.get("visual_description", "测试画面：主角站在天台远眺"),
        dialogue=overrides.get("dialogue", "林小满：测试对白"),
        prompt_locked=overrides.get("prompt_locked", "统一风格：测试风格；画面：测试画面"),
        prompt_editable=overrides.get("prompt_editable", ""),
        video_url=overrides.get("video_url"),
        status=overrides.get("status", "pending"),
    )
    db.add(sb)
    await db.commit()
    await db.refresh(sb)
    return sb


async def get_wallet_model(db: AsyncSession, user_id: int):
    """查询用户钱包的最新余额快照（供余额/冻结断言使用）。

    实现说明：以「列选择」而非整行 ORM 加载的方式读取，完全绕开
    Session 身份映射缓存——执行器（runner）会在并发事务中更新钱包，
    重复读取必须每次都拿到数据库最新提交值，且不引入过期属性懒加载
    （MissingGreenlet）风险。

    Args:
        db: 测试库会话。
        user_id: 用户 ID。

    Returns:
        SimpleNamespace: 含 points_balance / frozen_points / version 的快照对象。

    Raises:
        AssertionError: 钱包不存在时中断。
    """
    from types import SimpleNamespace

    from app.models import Wallet

    row = (
        await db.execute(
            select(Wallet.points_balance, Wallet.frozen_points, Wallet.version).where(
                Wallet.user_id == user_id
            )
        )
    ).one_or_none()
    assert row is not None, f"用户 {user_id} 钱包不存在"
    return SimpleNamespace(
        points_balance=row.points_balance,
        frozen_points=row.frozen_points,
        version=row.version,
    )


async def wait_task_terminal(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    task_id: int,
    timeout: float = 30.0,
    interval: float = 0.3,
) -> Dict[str, Any]:
    """轮询任务详情直至终态（done/failed/canceled）并返回最终任务 JSON。

    Args:
        client: 内存 HTTP 客户端。
        headers: 认证请求头。
        task_id: 任务 ID。
        timeout: 最长等待秒数（默认 30s）。
        interval: 轮询间隔秒数。

    Returns:
        Dict[str, Any]: 终态任务 JSON。

    Raises:
        AssertionError: 超时仍未终态，或查询返回非 200。
    """
    deadline = time.monotonic() + timeout
    task: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = await client.get(f"/api/tasks/{task_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        task = resp.json()
        if task["status"] in ("done", "failed", "canceled"):
            return task
        await asyncio.sleep(interval)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内未到达终态（最后状态：{task.get('status')}）")


def parse_sse_events(raw_text: str) -> List[Tuple[str, Optional[Dict[str, Any]]]]:
    """把 SSE 响应文本解析为 (event, data) 列表。

    Args:
        raw_text: SSE 完整响应文本（形如 "event: x\\ndata: {...}\\n\\n"）。

    Returns:
        List[Tuple[str, Optional[dict]]]: 事件名与 JSON 负载（解析失败为 None）。

    Raises:
        无：解析失败的 data 块负载置 None，不抛异常。
    """
    events: List[Tuple[str, Optional[Dict[str, Any]]]] = []
    for block in raw_text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_name: Optional[str] = None
        data_lines: List[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if event_name is None:
            continue
        payload: Optional[Dict[str, Any]] = None
        if data_lines:
            try:
                parsed = json.loads("\n".join(data_lines))
                payload = parsed if isinstance(parsed, dict) else None
            except (json.JSONDecodeError, ValueError):
                payload = None
        events.append((event_name, payload))
    return events


def unique_suffix() -> str:
    """生成一个随机短后缀（用于标题/名称防冲突）。

    Returns:
        str: 10 位十六进制随机串。
    """
    return uuid.uuid4().hex[:10]
