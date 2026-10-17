"""模型路由服务测试模块（文件级说明）。

覆盖 app/services/model_router.py 的渠道降级链解析与健康回写：
- resolve_channels_chain：按 LogicalModel.channel_priority 顺序返回候选渠道；
  过滤 enabled=False、health_status=down；user_id 非空时本人自有 openai 渠道
  （models 含对应能力键）按 id 升序插入链首（自有 Key 优先，owner_user_id 回填）；
  默认模型取渠道 models 目录第一个；引用不存在渠道时跳过；
- 逻辑模型缺失（无默认记录）返回空链；
- mock_sentinel：链尾 mock 哨兵渠道字段正确；
- record_result：成功置 healthy / 失败置 degraded；mock 哨兵（channel_id=0）
  不落库直接跳过。
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LogicalModel, ModelChannel, User
from app.services.model_router import mock_sentinel, record_result, resolve_channels_chain


async def _make_channel(db: AsyncSession, **overrides) -> ModelChannel:
    """插入一条模型渠道（内部辅助函数）。

    Args:
        db: 测试库会话。
        **overrides: 可覆盖字段（protocol/base_url/api_key_encrypted/models/
            health_status/enabled/owner_user_id）。

    Returns:
        ModelChannel: 已落库的渠道 ORM 对象。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    channel = ModelChannel(
        protocol=overrides.get("protocol", "openai"),
        base_url=overrides.get("base_url", "https://api.example.com/v1"),
        api_key_encrypted=overrides.get("api_key_encrypted", "sk-plain-test"),
        models=overrides.get("models", {"image": ["agnes-img-v1"]}),
        health_status=overrides.get("health_status", "unknown"),
        enabled=overrides.get("enabled", True),
        owner_user_id=overrides.get("owner_user_id"),
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)
    return channel


async def _make_default_logical(db: AsyncSession, capability: str, priority: list) -> LogicalModel:
    """插入一条能力默认逻辑模型（内部辅助函数）。

    Args:
        db: 测试库会话。
        capability: 能力类型（llm/image/video/tts）。
        priority: 渠道优先级列表（渠道 id 数组）。

    Returns:
        LogicalModel: 已落库的逻辑模型 ORM 对象。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    logical = LogicalModel(
        capability=capability,
        name=f"路由测试_{uuid.uuid4().hex[:8]}",
        channel_priority=priority,
        is_default=True,
        price_per_unit={"unit": 1, "price": 1},
    )
    db.add(logical)
    await db.commit()
    await db.refresh(logical)
    return logical


async def test_chain_shunxu_yu_guolv(db):
    """降级链解析：按优先级顺序返回，且过滤禁用/ down /私有渠道。

    Args:
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 链内容与过滤规则不符时中断。
    """
    normal = await _make_channel(db)                                   # 正常启用
    disabled = await _make_channel(db, enabled=False)                  # 已禁用
    downed = await _make_channel(db, health_status="down")             # 健康置 down
    # 私有渠道：归属真实用户（外键约束），resolve 应跳过
    suffix = uuid.uuid4().hex[:10]
    owner = User(username=f"mr_{suffix}", password_hash="test-hash", role="user", status=1)
    db.add(owner)
    await db.commit()
    await db.refresh(owner)
    private = await _make_channel(db, owner_user_id=owner.id)
    ghost_id = 999999                                                  # 不存在的渠道引用
    await _make_default_logical(
        db, "image", [normal.id, disabled.id, ghost_id, downed.id, private.id]
    )

    chain = await resolve_channels_chain(db, "image")
    assert [c.channel_id for c in chain] == [normal.id], "应仅保留启用且非 down 的平台渠道"
    head = chain[0]
    assert head.name == f"openai-{normal.id}"
    assert head.protocol == "openai"
    assert head.capability == "image"
    assert head.model == "agnes-img-v1", "默认模型应取渠道 models 目录第一个"
    assert head.api_key == "sk-plain-test", "明文 Key 应降级原值返回"


async def test_chain_youyou_qiangdao_youxian(db):
    """自有渠道优先：user_id 非空时本人 openai 渠道按能力键匹配插入链首。

    验证规则：
    - models 含对应能力键（image→"image"）的自有渠道进入链首（owner_user_id 回填）；
    - models 不含能力键的自有渠道（如仅 "chat"）不参与该能力路由；
    - 平台渠道仍按 channel_priority 追加其后，整体按 id 去重。

    Args:
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 链顺序或过滤结果与自有渠道优先规则不符时中断。
    """
    suffix = uuid.uuid4().hex[:10]
    owner = User(username=f"own_{suffix}", password_hash="test-hash", role="user", status=1)
    db.add(owner)
    await db.commit()
    await db.refresh(owner)
    # 自有图像渠道（models 含 image 键）→ 应插入链首
    own_img = await _make_channel(
        db, owner_user_id=owner.id, models={"image": ["my-image-v1"]}, base_url="https://own.example.com/v1")
    # 自有 chat 渠道（models 仅含 chat 键）→ 不参与 image 能力路由
    own_chat = await _make_channel(db, owner_user_id=owner.id, models={"chat": ["my-chat-v1"]})
    platform = await _make_channel(db)
    await _make_default_logical(db, "image", [platform.id])

    chain = await resolve_channels_chain(db, "image", user_id=owner.id)
    assert [c.channel_id for c in chain] == [own_img.id, platform.id], \
        "自有图像渠道应插链首，平台渠道按优先级追加其后"
    assert chain[0].owner_user_id == owner.id, "自有渠道应回填 owner_user_id 供计费豁免"
    assert chain[0].model == "my-image-v1"
    assert own_chat.id not in [c.channel_id for c in chain], "不含能力键的自有渠道不参与该能力路由"

    # user_id 为 None（旧行为）：仅平台渠道
    chain_plain = await resolve_channels_chain(db, "image")
    assert [c.channel_id for c in chain_plain] == [platform.id]


async def test_chain_wu_luoji_mokuai_fan_kong(db):
    """能力无默认逻辑模型时解析返回空链（不抛异常）。

    Args:
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 返回值非空列表时中断。
    """
    chain = await resolve_channels_chain(db, "video")
    assert chain == []


async def test_record_result_jiankang_huixie(db):
    """健康回写：成功置 healthy、失败置 degraded、mock 哨兵跳过不落库。

    Args:
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 健康状态未按结果回写时中断。
    """
    ok_channel = await _make_channel(db)
    bad_channel = await _make_channel(db)

    await record_result(db, ok_channel.id, True)
    await db.commit()
    await db.refresh(ok_channel)
    assert ok_channel.health_status == "healthy"
    assert ok_channel.last_check_at is not None

    await record_result(db, bad_channel.id, False, error="HTTP 502")
    await db.commit()
    await db.refresh(bad_channel)
    assert bad_channel.health_status == "degraded"

    # mock 哨兵（channel_id=0）：不落库，调用应直接返回且不抛异常
    await record_result(db, 0, False, error="mock 不回写")


async def test_record_result_chenggong_fuwei_degraded(db):
    """成功自动复位：初始 degraded 的渠道一次成功调用后复位 healthy。

    验证 record_result 的双向语义——降级渠道无需人工干预或探测，业务调用
    成功即自动恢复 healthy（Task 8 健康巡检收敛点）。

    Args:
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: degraded 渠道成功调用后未复位 healthy 时中断。
    """
    channel = await _make_channel(db, health_status="degraded")
    assert channel.health_status == "degraded"

    await record_result(db, channel.id, True)
    await db.commit()
    await db.refresh(channel)
    assert channel.health_status == "healthy", "degraded 渠道一次成功调用应自动复位 healthy"
    assert channel.last_check_at is not None, "复位时应同步刷新最近检查时间"


async def test_mock_sentinel_ziduan():
    """mock 哨兵渠道：channel_id=0、协议 mock、模型名带能力后缀。

    Returns:
        None。

    Raises:
        AssertionError: 哨兵字段与约定不符时中断。
    """
    sentinel = mock_sentinel("video")
    assert sentinel.channel_id == 0
    assert sentinel.name == "mock"
    assert sentinel.protocol == "mock"
    assert sentinel.base_url == "local://mock"
    assert sentinel.api_key == ""
    assert sentinel.model == "mock-video"
    assert sentinel.capability == "video"
