"""模型配置路由模块（文件级说明）。

实现逻辑模型目录 / 平台渠道信息 / 用户自有 Key 管理：
- GET    /api/models/catalog                逻辑模型（按能力分组，附渠道健康）+ 平台渠道信息（不含 Key）
- GET    /api/models/channels               当前用户自有渠道（api_key 脱敏前4后4）
- POST   /api/models/channels               新建自有渠道（Key 经 Fernet 加密落库）
- DELETE /api/models/channels/{id}          删除自有渠道（仅自己的）
- POST   /api/models/channels/{id}/test     连通性测试（openai 协议），更新健康状态

注：项目级模型配置存于 projects.config，由 /api/projects 维护，此处不做。
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from openai import AsyncOpenAI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_text, encrypt_text
from app.core.database import get_db
from app.core.security import get_current_user
from app.models import LogicalModel, ModelChannel, User
from app.schemas.model_config import (
    CatalogGroupOut,
    CatalogModelOut,
    CatalogOut,
    ChannelCreateRequest,
    ChannelOut,
    ChannelTestResponse,
    PlatformChannelOut,
)

router = APIRouter(prefix="/api/models", tags=["models"])

# 能力分组展示顺序（PL-26：llm/image/video/tts）
_CAPABILITY_ORDER = ("llm", "image", "video", "tts")

# 渠道连通性测试超时（秒）
_TEST_TIMEOUT_SECONDS = 10.0


def _mask_api_key(stored: str) -> str:
    """将库中存储的 API Key 脱敏为前 4 位 + **** + 后 4 位（内部函数）。

    兼容两种存量：Fernet 密文（先解密再脱敏）与历史明文（直接脱敏）。

    Args:
        stored: 库中存储的原始字符串（密文或明文）。

    Returns:
        str: 脱敏后的 Key；长度不足 8 位时仅返回 "****"。
    """
    try:
        plain = decrypt_text(stored)
    except (ValueError, TypeError):
        plain = stored  # 解密失败按明文降级处理（兼容历史数据）
    if len(plain) <= 8:
        return "****"
    return f"{plain[:4]}****{plain[-4:]}"


def _decrypt_api_key(stored: Optional[str]) -> Optional[str]:
    """解密库中 API Key（内部函数，仅用于发起连通性测试）。

    Args:
        stored: 库中存储的原始字符串。

    Returns:
        Optional[str]: 解密后的明文 Key；解密失败按明文降级；入参为空返回 None。
    """
    if not stored:
        return None
    try:
        return decrypt_text(stored)
    except (ValueError, TypeError):
        return stored


@router.get("/catalog", response_model=CatalogOut)
async def get_catalog(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CatalogOut:
    """查询逻辑模型目录：按能力分组返回模型与首选渠道健康状态，附平台渠道信息。

    health 取该模型降级链路（channel_priority）中第一个渠道的健康状态，
    渠道不存在时为 'unknown'（PL-08 / PL-26~PL-29）。

    Args:
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        CatalogOut: 能力分组模型列表 + 平台渠道列表（不含任何 Key）。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）。
    """
    models = (await db.execute(select(LogicalModel).order_by(LogicalModel.id))).scalars().all()
    channels = (await db.execute(select(ModelChannel))).scalars().all()
    channel_health: Dict[int, str] = {c.id: c.health_status for c in channels}

    # 按能力分组（固定顺序输出）
    grouped: Dict[str, List[CatalogModelOut]] = {cap: [] for cap in _CAPABILITY_ORDER}
    for model in models:
        priority = model.channel_priority if isinstance(model.channel_priority, list) else []
        first_channel = priority[0] if priority else None
        health = channel_health.get(first_channel, "unknown") if isinstance(first_channel, int) else "unknown"
        item = CatalogModelOut(
            id=model.id,
            name=model.name,
            is_default=model.is_default,
            price_per_unit=model.price_per_unit,
            scene_desc=model.scene_desc,
            doc_url=model.doc_url,
            health=health,
        )
        grouped.setdefault(model.capability, []).append(item)

    capabilities = [
        CatalogGroupOut(capability=cap, models=grouped[cap])
        for cap in _CAPABILITY_ORDER if grouped.get(cap)
    ]
    platform_channels = [
        PlatformChannelOut(
            id=c.id,
            protocol=c.protocol,
            base_url=c.base_url,
            health_status=c.health_status,
            enabled=c.enabled,
            last_check_at=c.last_check_at,
        )
        for c in channels if c.owner_user_id is None
    ]
    return CatalogOut(capabilities=capabilities, platform_channels=platform_channels)


@router.get("/channels", response_model=List[ChannelOut])
async def list_channels(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[ChannelOut]:
    """查询当前用户自有的模型渠道列表（api_key 脱敏显示）。

    Args:
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        List[ChannelOut]: 用户自有渠道列表。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）。
    """
    rows = (
        await db.execute(
            select(ModelChannel).where(ModelChannel.owner_user_id == user.id).order_by(ModelChannel.id)
        )
    ).scalars().all()
    return [
        ChannelOut(
            id=c.id,
            protocol=c.protocol,
            base_url=c.base_url,
            api_key=_mask_api_key(c.api_key_encrypted) if c.api_key_encrypted else None,
            models=c.models,
            health_status=c.health_status,
            last_check_at=c.last_check_at,
            enabled=c.enabled,
        )
        for c in rows
    ]


@router.post("/channels", response_model=ChannelOut)
async def create_channel(
    req: ChannelCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChannelOut:
    """新建用户自有渠道：API Key 经 Fernet 加密后落库（RD-49）。

    Args:
        req: 渠道创建请求（protocol/base_url/api_key/models）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ChannelOut: 新建渠道信息（api_key 已脱敏）。

    Raises:
        HTTPException: 400 —— 协议暂不支持或 base_url 格式非法；401 —— 未认证。
    """
    if req.protocol != "openai":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前仅支持 openai 兼容协议")
    if not req.base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="base_url 须以 http(s):// 开头")

    # 同步主键序列：种子数据以显式 id=1 插入平台渠道，未推进 model_channels_id_seq，
    # 直接 nextval 会与其冲突；此处把序列校准到当前最大 id（幂等、代价可忽略）
    await db.execute(text(
        "SELECT setval(pg_get_serial_sequence('model_channels', 'id'), "
        "GREATEST((SELECT COALESCE(MAX(id), 1) FROM model_channels), 1))"
    ))

    channel = ModelChannel(
        protocol=req.protocol,
        base_url=req.base_url,
        api_key_encrypted=encrypt_text(req.api_key),  # 密文落库，接口层永不回传
        owner_user_id=user.id,
        models=req.models,
        health_status="unknown",
        enabled=True,
    )
    db.add(channel)
    await db.commit()
    return ChannelOut(
        id=channel.id,
        protocol=channel.protocol,
        base_url=channel.base_url,
        api_key=_mask_api_key(channel.api_key_encrypted),
        models=channel.models,
        health_status=channel.health_status,
        last_check_at=channel.last_check_at,
        enabled=channel.enabled,
    )


async def _get_owned_channel(db: AsyncSession, channel_id: int, user: User) -> ModelChannel:
    """获取归属于当前用户的模型渠道（内部函数）。

    Args:
        db: 请求级异步数据库会话。
        channel_id: 渠道 ID。
        user: 当前用户。

    Returns:
        ModelChannel: 渠道 ORM 对象。

    Raises:
        HTTPException: 404 —— 渠道不存在或非本人渠道（平台渠道亦视为不可操作）。
    """
    channel = (
        await db.execute(
            select(ModelChannel).where(
                ModelChannel.id == channel_id, ModelChannel.owner_user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="渠道不存在")
    return channel


@router.delete("/channels/{channel_id}")
async def delete_channel(
    channel_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除当前用户自有的模型渠道（物理删除）。

    Args:
        channel_id: 渠道 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"message": "渠道已删除"}。

    Raises:
        HTTPException: 404 —— 渠道不存在或非本人渠道。
    """
    channel = await _get_owned_channel(db, channel_id, user)
    await db.delete(channel)
    await db.commit()
    return {"message": "渠道已删除"}


@router.post("/channels/{channel_id}/test", response_model=ChannelTestResponse)
async def test_channel(
    channel_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChannelTestResponse:
    """测试自有渠道连通性（openai 协议）：发送一条最短消息验证，并更新健康状态。

    探测逻辑与 providers/llm.py 的 health_check 一致（异常不向上抛出，
    全部收敛进结果），但以渠道自身 base_url / api_key 构造客户端。

    Args:
        channel_id: 渠道 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ChannelTestResponse: ok / error / 更新后的 health_status（healthy 或 down）。

    Raises:
        HTTPException: 400 —— 协议不支持；404 —— 渠道不存在或非本人渠道。
    """
    channel = await _get_owned_channel(db, channel_id, user)
    if channel.protocol != "openai":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前仅支持 openai 兼容协议测试")

    # 从 models 目录中取第一个模型名作为探测模型，缺省回退平台默认
    probe_model: Optional[str] = None
    models_cfg: Any = channel.models or {}
    if isinstance(models_cfg, dict):
        for names in models_cfg.values():
            if isinstance(names, list) and names:
                probe_model = str(names[0])
                break

    api_key = _decrypt_api_key(channel.api_key_encrypted) or "missing-key"
    client = AsyncOpenAI(
        base_url=channel.base_url,
        api_key=api_key,
        timeout=_TEST_TIMEOUT_SECONDS,
        max_retries=0,
    )
    ok, error = True, None
    try:
        await client.chat.completions.create(
            model=probe_model or "gpt-4o-mini",
            messages=[{"role": "user", "content": "ping"}],
            temperature=0,
            max_tokens=8,
        )
    except Exception as exc:  # noqa: BLE001 —— 健康检查需要捕获一切异常
        ok, error = False, str(exc)

    channel.health_status = "healthy" if ok else "down"
    channel.last_check_at = datetime.now(timezone.utc)
    await db.commit()
    return ChannelTestResponse(ok=ok, error=error, health_status=channel.health_status)
