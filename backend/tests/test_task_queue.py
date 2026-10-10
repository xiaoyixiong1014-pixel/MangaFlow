"""任务队列调度测试模块（文件级说明）。

覆盖 app/tasks/runner.py 的调度增强（Task 2.1）：
- 优先级领取：priority DESC, created_at ASC, id ASC —— 高优先级先领取，
  同优先级按创建/插入顺序 FIFO；
- 指数退避：next_retry_at 未到期的 queued 任务本轮不被领取；领取失败重试后
  next_retry_at = now + 2^retry_count 分钟（首次重试 2 分钟）；
- 死信：retry_count 达到上限（>= 3）后终态失败并置 dead_letter=True，
  冻结积分按 platform 承担退款。

说明：测试直接调用 runner._process_one()（单次处理单元）而非走 HTTP 客户端，
且不加载 task_runner 常驻 worker（避免并发领取竞争导致断言不稳定）；通过局部
重定向 runner 模块与通知服务的 SessionLocal 指向测试库实现数据隔离。任务类型
统一使用 "llm"（执行器不支持的类型，领取后走平台失败重试/死信路径，全程无
外部网络依赖）。
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Project, Task, User, Wallet, WalletTransaction


@pytest_asyncio.fixture
async def patched_runner(test_engine):
    """局部重定向 runner 与通知服务的模块级 SessionLocal 到测试库（夹具）。

    与 conftest.task_runner 的区别：不启动 worker 协程，仅改写会话工厂，
    供测试以单步方式调用 runner._process_one()，保证领取顺序可断言。

    Args:
        test_engine: 测试库异步引擎。

    Yields:
        runner 模块对象（SessionLocal 已指向测试库）。

    Raises:
        无：夹具结束时还原原始 SessionLocal。
    """
    from app.services import notification_service as notification_mod
    from app.tasks import runner as runner_mod

    factory = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    original_runner = runner_mod.SessionLocal
    original_notification = notification_mod.SessionLocal
    runner_mod.SessionLocal = factory
    notification_mod.SessionLocal = factory
    try:
        yield runner_mod
    finally:
        runner_mod.SessionLocal = original_runner
        notification_mod.SessionLocal = original_notification


async def _make_user_with_wallet(db: AsyncSession, frozen_points: int = 0) -> User:
    """插入一个用户与配套钱包（内部辅助函数）。

    Args:
        db: 测试库会话。
        frozen_points: 钱包预置冻结积分（死信退款场景需要）。

    Returns:
        User: 已落库的用户 ORM 对象。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"tq_{suffix}",
        password_hash="test-hash",
        role="user",
        status=1,
    )
    db.add(user)
    await db.flush()
    db.add(Wallet(user_id=user.id, points_balance=1_000, frozen_points=frozen_points, version=0))
    await db.commit()
    await db.refresh(user)
    return user


async def _make_project(db: AsyncSession, user_id: int) -> Project:
    """插入一个归属指定用户的项目（内部辅助函数）。

    Args:
        db: 测试库会话。
        user_id: 归属用户 ID。

    Returns:
        Project: 已落库的项目 ORM 对象。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    project = Project(user_id=user_id, title=f"队列测试_{uuid.uuid4().hex[:6]}")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _insert_task(db: AsyncSession, project_id: int, **overrides) -> Task:
    """直插一条 queued 任务（内部辅助函数）。

    Args:
        db: 测试库会话。
        project_id: 归属项目 ID。
        **overrides: 可覆盖字段（type/priority/retry_count/next_retry_at/
            cost_points/status/meta）。

    Returns:
        Task: 已落库的任务 ORM 对象。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    task = Task(
        project_id=project_id,
        type=overrides.get("type", "llm"),
        phase=overrides.get("phase", "full"),
        status=overrides.get("status", "queued"),
        retry_count=overrides.get("retry_count", 0),
        cost_points=overrides.get("cost_points", 0),
        priority=overrides.get("priority", 5),
        next_retry_at=overrides.get("next_retry_at"),
        meta=overrides.get("meta"),
        idempotency_key=f"tq_{uuid.uuid4().hex[:16]}",
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def _get_task(db: AsyncSession, task_id: int) -> SimpleNamespace:
    """按主键读取任务行的最新状态快照（内部辅助函数）。

    以「列选择」而非整行 ORM 加载的方式读取，绕开会话身份映射缓存——
    执行器在独立会话中更新任务行，重复读取必须拿到数据库最新提交值，
    并避免过期属性懒加载（MissingGreenlet）风险（同 conftest.get_wallet_model）。

    Args:
        db: 测试库会话。
        task_id: 任务 ID。

    Returns:
        SimpleNamespace: 含 retry_count / status / next_retry_at / dead_letter /
        error_code 的快照对象。

    Raises:
        AssertionError: 任务不存在时中断。
    """
    row = (
        await db.execute(
            select(
                Task.retry_count, Task.status, Task.next_retry_at,
                Task.dead_letter, Task.error_code,
            ).where(Task.id == task_id)
        )
    ).one_or_none()
    assert row is not None, f"任务 {task_id} 不存在"
    return SimpleNamespace(
        retry_count=row.retry_count,
        status=row.status,
        next_retry_at=row.next_retry_at,
        dead_letter=row.dead_letter,
        error_code=row.error_code,
    )


async def test_priority_gao_youxian_xian_lingqu(db, patched_runner):
    """优先级领取：高优先级任务先被领取，同优先级按插入顺序 FIFO。

    Args:
        db: 测试库会话。
        patched_runner: 会话工厂已重定向的 runner 模块。

    Returns:
        None。

    Raises:
        AssertionError: 领取顺序与 priority DESC / id ASC 不符时中断。
    """
    user = await _make_user_with_wallet(db)
    project = await _make_project(db, user.id)
    # 同一事务内 created_at 相同，同优先级由 id ASC 兜底：a 先于 b
    a = await _insert_task(db, project.id, priority=0)
    b = await _insert_task(db, project.id, priority=0)
    high = await _insert_task(db, project.id, priority=9)

    # 第一次领取：应命中 priority=9 的任务（领取后执行失败重试，retry_count 0→1）
    processed = await patched_runner._process_one()
    assert processed is True
    high_row = await _get_task(db, high.id)
    a_row = await _get_task(db, a.id)
    b_row = await _get_task(db, b.id)
    assert high_row.retry_count == 1, "高优先级任务应最先被领取"
    assert high_row.status == "queued", "llm 类型失败后应回队等待指数退避"
    assert a_row.retry_count == 0 and b_row.retry_count == 0, "低优先级任务不应被提前领取"

    # 第二次领取：同优先级 FIFO，命中插入顺序更早的 a
    processed = await patched_runner._process_one()
    assert processed is True
    a_row = await _get_task(db, a.id)
    b_row = await _get_task(db, b.id)
    assert a_row.retry_count == 1, "同优先级应按创建顺序领取"
    assert b_row.retry_count == 0


async def test_next_retry_at_wei_daoqi_bu_lingqu(db, patched_runner):
    """指数退避：next_retry_at 未到期的任务不领取；到期后领取并按 2^n 分钟重排。

    Args:
        db: 测试库会话。
        patched_runner: 会话工厂已重定向的 runner 模块。

    Returns:
        None。

    Raises:
        AssertionError: 未到期被领取或退避间隔不符 2^n 分钟时中断。
    """
    user = await _make_user_with_wallet(db)
    project = await _make_project(db, user.id)
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    task = await _insert_task(db, project.id, priority=5, next_retry_at=future)

    # 未到期：不领取
    processed = await patched_runner._process_one()
    assert processed is False, "next_retry_at 未到期的任务不应被领取"
    row = await _get_task(db, task.id)
    assert row.status == "queued", "未到期任务应保持 queued"

    # 到期（next_retry_at 置为过去）：可领取；失败重试后 next_retry_at ≈ now + 2^1 分钟
    await db.execute(
        update(Task).where(Task.id == task.id).values(next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=5))
    )
    await db.commit()

    processed = await patched_runner._process_one()
    assert processed is True
    row = await _get_task(db, task.id)
    assert row.retry_count == 1
    assert row.status == "queued"
    assert row.next_retry_at is not None, "失败重试后应写入指数退避时间"
    delay = row.next_retry_at - datetime.now(timezone.utc)
    # 首次重试退避 2^1 = 2 分钟（允许调度/提交耗时带来的少量偏差）
    assert timedelta(minutes=1) < delay <= timedelta(minutes=3), f"首次重试退避应约 2 分钟，实际 {delay}"


async def test_retry_haojin_jinru_sixin(db, patched_runner):
    """重试耗尽：retry_count 达上限后终态失败、置死信并按平台承担退款。

    Args:
        db: 测试库会话。
        patched_runner: 会话工厂已重定向的 runner 模块。

    Returns:
        None。

    Raises:
        AssertionError: 死信标记/终态/退款流水缺失时中断。
    """
    user = await _make_user_with_wallet(db, frozen_points=5)
    project = await _make_project(db, user.id)
    task = await _insert_task(db, project.id, priority=5, retry_count=2, cost_points=5)

    processed = await patched_runner._process_one()
    assert processed is True
    row = await _get_task(db, task.id)
    assert row.status == "failed", "重试耗尽后应终态失败"
    assert row.dead_letter is True, "重试耗尽后应进入死信队列"
    assert row.retry_count == 3
    assert row.error_code == "platform_error"

    # 平台承担退款：存在 platform 承担的 refund 流水
    tx = (
        await db.execute(
            select(WalletTransaction).where(
                WalletTransaction.ref_task_id == task.id,
                WalletTransaction.type == "refund",
                WalletTransaction.amount == 5,
            )
        )
    ).scalars().first()
    assert tx is not None, "死信失败后应产生冻结退款流水"
    assert tx.bear == "platform"
