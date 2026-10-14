"""演示占位治理测试模块（文件级说明，P0 缺陷 D1/D2/D3）。

覆盖可信度治理三件事的回归断言：
- D1 密钥治理：config.py 源文件不得包含任何 "sk-" 明文密钥前缀；
- D3 mock 打标：三个 mock provider（image/video/tts）产物文件名统一带
  placeholder_ 前缀、返回 meta 携带 placeholder=True 标记，SVG/视频画面
  含可感知的「演示占位」标注；
- D2 占位成片：项目无片段时占位成片恒为 .mp4 产物（有 ffmpeg 生成黑底
  提示视频；无 ffmpeg 抛 RuntimeError 走任务失败路径，不再返回假 URL）；
- D3 计费豁免：任务成功且 meta.placeholder=True 时冻结积分全额退款，
  流水备注含「演示占位豁免」、幂等键用独立前缀 ph-refund-；未打标任务
  照常扣费（回归对照）。

说明：测试直接调用 provider / compose_service._placeholder_film /
runner._succeed_task，不依赖常驻 worker 与外部网络；退款用例通过局部
重定向 runner 与通知服务的 SessionLocal 指向测试库实现数据隔离（同
test_task_queue.py 的 patched_runner 模式）。
"""

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Project, Task, User, Wallet, WalletTransaction

# 项目 backend 目录（本测试文件位于 backend/tests/ 下）
_BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest_asyncio.fixture
async def patched_runner(test_engine):
    """局部重定向 runner 与通知服务的模块级 SessionLocal 到测试库（夹具）。

    与 conftest.task_runner 的区别：不启动 worker 协程，仅改写会话工厂，
    供测试以直接函数调用方式驱动 runner._succeed_task，保证账务断言确定性。

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
    # 防御性清空任务表：避免全量运行时前序用例残留行干扰断言
    async with factory() as s:
        await s.execute(delete(Task))
        await s.commit()
    original_runner = runner_mod.SessionLocal
    original_notification = notification_mod.SessionLocal
    runner_mod.SessionLocal = factory
    notification_mod.SessionLocal = factory
    try:
        yield runner_mod
    finally:
        runner_mod.SessionLocal = original_runner
        notification_mod.SessionLocal = original_notification


# ---------------------------------------------------------------------------
# D1：密钥治理
# ---------------------------------------------------------------------------
async def test_config_source_has_no_sk_prefix():
    """config.py 源文件不含任何 "sk-" 明文密钥前缀（D1 密钥不落代码）。

    直接读取源码文本断言：5 个渠道 API Key 的默认值必须为空串（密钥仅经
    环境变量 / .env 注入），任何 sk- 前缀字符串均视为明文残留。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 源文件文本中检测到 "sk-" 时中断。
    """
    source = (_BACKEND_DIR / "app" / "core" / "config.py").read_text(encoding="utf-8")
    assert "sk-" not in source, "config.py 不得包含 sk- 明文密钥（D1 密钥治理）"


# ---------------------------------------------------------------------------
# D3：mock provider 打标
# ---------------------------------------------------------------------------
async def test_image_mock_placeholder_mark(tmp_path):
    """image_mock 产物带 placeholder_ 前缀、meta.placeholder=True、画面含「演示占位」。

    Args:
        tmp_path: pytest 临时目录（充当 storage/generated）。

    Returns:
        None。

    Raises:
        AssertionError: 文件名前缀/标记/SVG 文案任一不符时中断。
    """
    from app.providers.image_mock import generate_image

    result = await generate_image("测试提示词：城市夜景", tmp_path, seed=42)
    assert result is not None, "image_mock 应成功产出占位图"
    filename = Path(result["url"]).name
    assert filename.startswith("placeholder_"), f"产物文件名应带 placeholder_ 前缀，实际 {filename}"
    assert result["meta"].get("placeholder") is True, "返回 meta 应携带 placeholder=True 标记"
    # 画面内显著标注：SVG 文本含「演示占位」四字
    svg_text = (tmp_path / filename).read_text(encoding="utf-8")
    assert "演示占位" in svg_text, "SVG 画面内应包含「演示占位」显著标注"


async def test_video_mock_placeholder_mark(tmp_path, monkeypatch):
    """video_mock 产物带 placeholder_ 前缀与标记（有/无 ffmpeg 双分支）。"""
    from app.providers import video_mock

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        # 有 ffmpeg：生成占位 MP4 片段
        result = await video_mock.generate_video("测试提示词", 3, tmp_path, seed=7)
        assert result is not None, "video_mock（有 ffmpeg）应成功产出占位片段"
        filename = Path(result["url"]).name
        assert filename.startswith("placeholder_"), f"产物文件名应带 placeholder_ 前缀，实际 {filename}"
        assert filename.endswith(".mp4")
        assert (tmp_path / filename).is_file(), "占位片段应真实落盘"
        assert result["meta"].get("placeholder") is True, "返回 meta 应携带 placeholder=True 标记"
    else:
        # 无 ffmpeg：降级 SVG 海报分支
        result = await video_mock.generate_video("测试提示词", 3, tmp_path, seed=7)
        assert result is not None and result["meta"].get("poster") is True
        filename = Path(result["url"]).name
        assert filename.startswith("placeholder_")
        assert result["meta"].get("placeholder") is True

    # 无 ffmpeg 强制分支（与上方环境无关，覆盖降级路径的打标断言）
    monkeypatch.setattr(video_mock.shutil, "which", lambda name: None)
    result = await video_mock.generate_video("测试提示词", 3, tmp_path, seed=8)
    assert result is not None, "video_mock（无 ffmpeg）应降级产出 SVG 海报"
    filename = Path(result["url"]).name
    assert filename.startswith("placeholder_"), f"降级海报文件名应带 placeholder_ 前缀，实际 {filename}"
    assert result["meta"].get("placeholder") is True
    svg_text = (tmp_path / filename).read_text(encoding="utf-8")
    assert "演示占位" in svg_text, "降级 SVG 海报画面应包含「演示占位」标注"


async def test_tts_mock_placeholder_mark(tmp_path):
    """tts_mock 产物带 placeholder_ 前缀、meta.placeholder=True、时长回写保留。

    Args:
        tmp_path: pytest 临时目录（充当 storage/generated）。

    Returns:
        None。

    Raises:
        AssertionError: 文件名前缀/标记/时长任一不符时中断。
    """
    from app.providers.tts_mock import generate_tts

    result = await generate_tts("配音测试文本", "female_01", tmp_path)
    assert result is not None, "tts_mock 应成功产出占位音频"
    filename = Path(result["url"]).name
    assert filename.startswith("placeholder_"), f"产物文件名应带 placeholder_ 前缀，实际 {filename}"
    assert (tmp_path / filename).is_file(), "占位音频应真实落盘"
    assert result["meta"].get("placeholder") is True, "返回 meta 应携带 placeholder=True 标记"
    assert result.get("duration_sec", 0) > 0, "时长估算（RD-31 回写依据）应保留"


# ---------------------------------------------------------------------------
# D2：占位成片
# ---------------------------------------------------------------------------
def _film_task_stub(task_id: int = 1234) -> SimpleNamespace:
    """构造占位成片函数所需的最小任务桩（内部辅助）。

    Args:
        task_id: 任务 ID（用于产物命名）。

    Returns:
        SimpleNamespace: 含 id/project_id 的任务桩对象。

    Raises:
        无。
    """
    return SimpleNamespace(id=task_id, project_id=1)


async def test_placeholder_film_produces_mp4_or_raises(tmp_path):
    """占位成片：有 ffmpeg 生成 .mp4 产物；无 ffmpeg 抛 RuntimeError（D2）。

    按本机环境自动分派分支：
    - 有 ffmpeg：断言产物文件落盘且返回 URL 以 .mp4 结尾（<video> 可播放）；
    - 无 ffmpeg：断言抛 RuntimeError（不再返回 SVG 假 URL）。

    Args:
        tmp_path: pytest 临时目录（充当 storage/outputs）。

    Returns:
        None。

    Raises:
        AssertionError: 产物后缀/落盘/异常路径任一不符时中断。
    """
    from app.services.compose_service import _placeholder_film

    ffmpeg = shutil.which("ffmpeg")
    task_stub = _film_task_stub()
    if ffmpeg:
        url = await _placeholder_film(task_stub, tmp_path, ffmpeg)
        assert url.endswith(".mp4"), f"占位成片 URL 应以 .mp4 结尾，实际 {url}"
        assert (tmp_path / f"film_placeholder_{task_stub.id}.mp4").is_file(), "占位成片应真实落盘"
    else:
        with pytest.raises(RuntimeError, match="无可用片段"):
            await _placeholder_film(task_stub, tmp_path, ffmpeg)


async def test_placeholder_film_without_ffmpeg_raises(tmp_path):
    """占位成片无 ffmpeg 分支：恒抛 RuntimeError 且信息含中文原因（D2）。

    无论本机是否装有 ffmpeg，显式传 ffmpeg=None 覆盖降级分支。

    Args:
        tmp_path: pytest 临时目录（充当 storage/outputs）。

    Returns:
        None。

    Raises:
        AssertionError: 未抛出异常或异常信息缺少中文原因时中断。
    """
    from app.services.compose_service import _placeholder_film

    with pytest.raises(RuntimeError) as excinfo:
        await _placeholder_film(_film_task_stub(), tmp_path, None)
    assert "无可用片段" in str(excinfo.value), "异常信息应含中文原因「无可用片段」"
    assert "ffmpeg" in str(excinfo.value), "异常信息应说明缺少 ffmpeg"


# ---------------------------------------------------------------------------
# D3：计费豁免（演示占位退款）
# ---------------------------------------------------------------------------
async def _make_user_with_wallet(db: AsyncSession, points_balance: int, frozen_points: int) -> User:
    """插入一个用户与配套钱包（内部辅助函数）。

    Args:
        db: 测试库会话。
        points_balance: 可用余额预置值。
        frozen_points: 冻结积分预置值（模拟任务创建时已冻结）。

    Returns:
        User: 已落库的用户 ORM 对象。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"pg_{suffix}",
        password_hash="test-hash",
        role="user",
        status=1,
    )
    db.add(user)
    await db.flush()
    db.add(Wallet(
        user_id=user.id,
        points_balance=points_balance,
        frozen_points=frozen_points,
        version=0,
    ))
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
    project = Project(user_id=user_id, title=f"占位治理_{uuid.uuid4().hex[:6]}")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _insert_placeholder_task(
    db: AsyncSession, project_id: int, *, placeholder: bool, cost_points: int = 100
) -> Task:
    """直插一条 running 任务（内部辅助函数）。

    Args:
        db: 测试库会话。
        project_id: 归属项目 ID。
        placeholder: 是否在 meta 写入 placeholder=True 标记。
        cost_points: 冻结积分金额。

    Returns:
        Task: 已落库的任务 ORM 对象。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    task = Task(
        project_id=project_id,
        type="image",
        phase="full",
        status="running",
        retry_count=0,
        cost_points=cost_points,
        priority=5,
        meta={"prompt": "占位治理测试", **({"placeholder": True} if placeholder else {})},
        idempotency_key=f"pg_{uuid.uuid4().hex[:16]}",
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def _get_wallet_snapshot(db: AsyncSession, user_id: int) -> SimpleNamespace:
    """以列选择方式读取钱包最新快照（绕开会话身份映射缓存，内部辅助）。

    Args:
        db: 测试库会话。
        user_id: 用户 ID。

    Returns:
        SimpleNamespace: 含 points_balance / frozen_points 的快照对象。

    Raises:
        AssertionError: 钱包不存在时中断。
    """
    row = (
        await db.execute(
            select(Wallet.points_balance, Wallet.frozen_points).where(Wallet.user_id == user_id)
        )
    ).one_or_none()
    assert row is not None, f"用户 {user_id} 钱包不存在"
    return SimpleNamespace(points_balance=row.points_balance, frozen_points=row.frozen_points)


async def test_placeholder_task_refund_on_success(db, patched_runner):
    """计费豁免：placeholder 任务成功时冻结积分全额退款且备注含「演示占位豁免」。

    场景：任务创建时已冻结 100 积分（余额 900 / 冻结 100），mock 兜底产物
    成功交付（meta.placeholder=True）——任务置 done，冻结全额退回可用余额
    （用户不被扣费），产生独立幂等键（ph-refund-{task_id}）的退款流水。

    Args:
        db: 测试库会话。
        patched_runner: 会话工厂已重定向的 runner 模块。

    Returns:
        None。

    Raises:
        AssertionError: 终态/余额/流水幂等键或备注任一不符时中断。
    """
    user = await _make_user_with_wallet(db, points_balance=900, frozen_points=100)
    project = await _make_project(db, user.id)
    task = await _insert_placeholder_task(db, project.id, placeholder=True, cost_points=100)

    await patched_runner._succeed_task(db, task, user.id)

    assert task.status == "done", "占位任务成功后应置 done"
    assert task.progress == 100

    # 钱包快照：冻结清零、可用余额恢复到冻结前（900 + 100 = 1000）
    wallet = await _get_wallet_snapshot(db, user.id)
    assert wallet.frozen_points == 0, "演示占位豁免后冻结积分应清零"
    assert wallet.points_balance == 1000, "演示占位豁免应把冻结积分全额退回可用余额"

    # 退款流水：独立幂等键前缀 + 备注含「演示占位豁免」
    tx = (
        await db.execute(
            select(WalletTransaction).where(
                WalletTransaction.ref_task_id == task.id,
                WalletTransaction.type == "refund",
            )
        )
    ).scalars().first()
    assert tx is not None, "占位任务成功后应产生退款流水"
    assert tx.idempotency_key == f"ph-refund-{task.id}", "退款幂等键应使用独立前缀 ph-refund-"
    assert tx.amount == 100, "退款金额应等于冻结扣费金额"
    assert tx.bear == "platform", "演示占位豁免应由平台承担"
    assert "演示占位豁免" in (tx.remark or ""), "流水备注应含「演示占位豁免」"

    # 不应产生 consume（扣费）流水
    consume_tx = (
        await db.execute(
            select(WalletTransaction).where(
                WalletTransaction.ref_task_id == task.id,
                WalletTransaction.type == "consume",
            )
        )
    ).scalars().first()
    assert consume_tx is None, "演示占位豁免任务不应产生扣费流水"


async def test_normal_task_still_consumes_on_success(db, patched_runner):
    """回归对照：未打标任务成功时照常扣费，不触发豁免退款。

    Args:
        db: 测试库会话。
        patched_runner: 会话工厂已重定向的 runner 模块。

    Returns:
        None。

    Raises:
        AssertionError: 扣费流水缺失或余额未按冻结扣费结算时中断。
    """
    user = await _make_user_with_wallet(db, points_balance=900, frozen_points=100)
    project = await _make_project(db, user.id)
    task = await _insert_placeholder_task(db, project.id, placeholder=False, cost_points=100)

    await patched_runner._succeed_task(db, task, user.id)

    assert task.status == "done"
    # 钱包：冻结扣费 100（frozen 清零），可用余额不变（900）
    wallet = await _get_wallet_snapshot(db, user.id)
    assert wallet.frozen_points == 0, "正常任务成功后冻结应被扣减清零"
    assert wallet.points_balance == 900, "正常任务扣费不动可用余额（仅冻结转出）"

    consume_tx = (
        await db.execute(
            select(WalletTransaction).where(
                WalletTransaction.ref_task_id == task.id,
                WalletTransaction.type == "consume",
            )
        )
    ).scalars().first()
    assert consume_tx is not None, "正常任务成功后应产生扣费流水"
    assert consume_tx.idempotency_key == f"task_{task.id}_consume"
    refund_tx = (
        await db.execute(
            select(WalletTransaction).where(
                WalletTransaction.ref_task_id == task.id,
                WalletTransaction.type == "refund",
            )
        )
    ).scalars().first()
    assert refund_tx is None, "未打标任务不应触发豁免退款"
