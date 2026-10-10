"""管理端模型管理路由模块（文件级说明）。

实现模型渠道与逻辑模型的运营管理：
- GET    /api/admin/models/channels             渠道列表（API Key 脱敏）
- POST   /api/admin/models/channels             新建渠道（Key 经 Fernet 加密落库）
- PUT    /api/admin/models/channels/{id}        更新渠道（部分更新）
- DELETE /api/admin/models/channels/{id}        删除渠道（平台渠道且被逻辑模型引用时拒绝）
- POST   /api/admin/models/channels/{id}/test   渠道连通性探测（openai 兼容调用）
- GET    /api/admin/models/logical              逻辑模型列表
- POST   /api/admin/models/logical              新建逻辑模型
- PUT    /api/admin/models/logical/{id}         更新逻辑模型（含同能力默认唯一切换）

全部接口要求运营方角色，普通用户 403。
"""

import time
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_text, encrypt_text
from app.core.database import get_db
from app.core.security import require_operator
from app.models import LogicalModel, ModelChannel, User
from app.schemas.admin import (
    ChannelCreateRequest,
    ChannelOut,
    ChannelTestResponse,
    ChannelUpdateRequest,
    LogicalModelCreateRequest,
    LogicalModelOut,
    LogicalModelUpdateRequest,
)
from app.services.model_router import probe_channel

router = APIRouter(
    prefix="/api/admin/models",
    tags=["admin-models"],
    dependencies=[Depends(require_operator)],  # 路由级鉴权：仅运营方可访问，普通用户 403
)

# 允许的渠道协议（当前仅 openai 兼容协议）
_ALLOWED_PROTOCOLS = ("openai",)
# 允许的健康状态取值
_ALLOWED_HEALTH = ("unknown", "healthy", "degraded", "down")
# 允许的能力取值
_ALLOWED_CAPABILITIES = ("llm", "image", "video", "tts")


def _mask_key(plain_key: str) -> str:
    """脱敏 API Key（内部函数）：保留前 4 后 4 位并标注总长度。

    Args:
        plain_key: 明文 API Key。

    Returns:
        str: 形如 "sk-4****2eb(len=38)" 的脱敏串；空串原样返回。
    """
    if not plain_key:
        return ""
    if len(plain_key) <= 8:
        return f"****(len={len(plain_key)})"
    return f"{plain_key[:4]}****{plain_key[-4:]}(len={len(plain_key)})"


def _masked_stored_key(channel: ModelChannel) -> str:
    """读取渠道存储的 Key 并脱敏（内部函数）。

    存储值可能是 Fernet 密文（新数据）或明文（历史种子数据）：
    先尝试解密，失败则按明文降级处理，再统一脱敏。

    Args:
        channel: 渠道 ORM 对象。

    Returns:
        str: 脱敏后的 Key 展示串。
    """
    stored = channel.api_key_encrypted or ""
    if not stored:
        return ""
    try:
        plain = decrypt_text(stored)
    except ValueError:
        plain = stored  # 兼容历史明文存储
    return _mask_key(plain)


def _channel_to_out(channel: ModelChannel) -> ChannelOut:
    """将渠道 ORM 对象转换为输出模型（内部函数）。

    Args:
        channel: 渠道 ORM 对象。

    Returns:
        ChannelOut: 脱敏后的渠道输出。
    """
    return ChannelOut(
        id=channel.id,
        protocol=channel.protocol,
        base_url=channel.base_url,
        api_key_masked=_masked_stored_key(channel),
        owner_user_id=channel.owner_user_id,
        owner_type="platform" if channel.owner_user_id is None else "user",
        models=channel.models,
        health_status=channel.health_status,
        last_check_at=channel.last_check_at,
        enabled=channel.enabled,
        created_at=channel.created_at,
    )


async def _get_channel_or_404(db: AsyncSession, channel_id: int) -> ModelChannel:
    """按 ID 获取渠道，不存在时抛 404（内部函数）。

    Args:
        db: 请求级异步数据库会话。
        channel_id: 渠道 ID。

    Returns:
        ModelChannel: 渠道 ORM 对象。

    Raises:
        HTTPException: 404 —— 渠道不存在。
    """
    channel = await db.get(ModelChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="渠道不存在")
    return channel


@router.get("/channels", response_model=List[ChannelOut])
async def list_channels(
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> List[ChannelOut]:
    """查询全部模型渠道（API Key 脱敏展示）。

    Args:
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        List[ChannelOut]: 渠道列表（owner_user_id 为 NULL 表示平台渠道）。

    Raises:
        无：查询异常向上抛出由全局处理器兜底。
    """
    rows = await db.execute(select(ModelChannel).order_by(ModelChannel.id))
    return [_channel_to_out(c) for c in rows.scalars().all()]


@router.post("/channels", response_model=ChannelOut)
async def create_channel(
    req: ChannelCreateRequest,
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> ChannelOut:
    """新建模型渠道：明文 API Key 经 Fernet 加密后落库（api_key_encrypted）。

    当前运营端新建渠道归平台所有（owner_user_id=NULL）；用户自有 Key
    （RD-49）由用户端配置流程写入，不在本接口范围。

    Args:
        req: 渠道创建请求（protocol/base_url/api_key/models）。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        ChannelOut: 创建后的渠道（Key 已脱敏）。

    Raises:
        HTTPException: 400 —— protocol 非 openai 兼容协议。
    """
    if req.protocol not in _ALLOWED_PROTOCOLS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"protocol 当前仅支持 {'/'.join(_ALLOWED_PROTOCOLS)}",
        )
    channel = ModelChannel(
        protocol=req.protocol,
        base_url=req.base_url,
        api_key_encrypted=encrypt_text(req.api_key),
        owner_user_id=None,
        models=req.models,
        health_status="unknown",  # 新建渠道未经探测，先置 unknown
        enabled=True,
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)
    return _channel_to_out(channel)


@router.put("/channels/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: int,
    req: ChannelUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> ChannelOut:
    """更新渠道（部分更新：base_url / enabled / health_status）。

    Args:
        channel_id: 渠道 ID。
        req: 渠道更新请求（字段均可选）。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        ChannelOut: 更新后的渠道（Key 已脱敏）。

    Raises:
        HTTPException: 400 —— health_status 取值非法；404 —— 渠道不存在。
    """
    channel = await _get_channel_or_404(db, channel_id)
    if req.health_status is not None:
        if req.health_status not in _ALLOWED_HEALTH:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"health_status 仅支持 {'/'.join(_ALLOWED_HEALTH)}",
            )
        channel.health_status = req.health_status
    if req.base_url is not None:
        channel.base_url = req.base_url
    if req.enabled is not None:
        channel.enabled = req.enabled
    await db.commit()
    await db.refresh(channel)
    return _channel_to_out(channel)


@router.delete("/channels/{channel_id}", response_model=ChannelOut)
async def delete_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> ChannelOut:
    """删除模型渠道（物理删除）。

    保护规则：平台渠道（owner_user_id IS NULL）且仍被任何逻辑模型的
    channel_priority 引用时拒绝删除（400）；用户自有渠道可直接删除。

    Args:
        channel_id: 渠道 ID。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        ChannelOut: 被删除的渠道信息。

    Raises:
        HTTPException: 400 —— 平台渠道仍被逻辑模型引用；404 —— 渠道不存在。
    """
    channel = await _get_channel_or_404(db, channel_id)

    if channel.owner_user_id is None:
        # channel_priority 为 JSONB 数组，逻辑模型量级小，取全量在 Python 侧判断引用
        logical_rows = await db.execute(select(LogicalModel.id, LogicalModel.channel_priority))
        referenced = [
            lm_id for lm_id, priority in logical_rows.all()
            if isinstance(priority, list) and channel_id in priority
        ]
        if referenced:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"平台渠道仍被逻辑模型引用（{referenced}），请先调整 channel_priority",
            )

    out = _channel_to_out(channel)
    await db.delete(channel)
    await db.commit()
    return out


@router.post("/channels/{channel_id}/test", response_model=ChannelTestResponse)
async def test_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> ChannelTestResponse:
    """渠道连通性探测（按协议与能力分派探测策略）。

    探测实现委托 model_router.probe_channel：
    - mock 协议：本地占位渠道直接视为连通；
    - openai 协议：models 目录含 chat 键走 LLM 最短消息探测；否则按
      图像/视频/TTS 端点以极小主体试探（非 401/403 的 4xx 视为连通，
      避免真实出图/出片产生计费）。
    probe_channel 原地回写 health_status（healthy/down）与 last_check_at，
    此处仅负责提交。

    Args:
        channel_id: 渠道 ID。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        ChannelTestResponse: {"ok", "error", "latency_ms"}（探测失败也返回 200）。

    Raises:
        HTTPException: 404 —— 渠道不存在。
    """
    channel = await _get_channel_or_404(db, channel_id)
    started = time.perf_counter()
    ok, error = await probe_channel(channel)
    latency_ms = int((time.perf_counter() - started) * 1000)
    await db.commit()
    return ChannelTestResponse(ok=ok, error=error, latency_ms=latency_ms)


@router.get("/logical", response_model=List[LogicalModelOut])
async def list_logical_models(
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> List[LogicalModelOut]:
    """查询全部逻辑模型（按能力 + 名称排序）。

    Args:
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        List[LogicalModelOut]: 逻辑模型列表。

    Raises:
        无：查询异常向上抛出由全局处理器兜底。
    """
    rows = await db.execute(
        select(LogicalModel).order_by(LogicalModel.capability, LogicalModel.name)
    )
    return [LogicalModelOut.model_validate(m) for m in rows.scalars().all()]


@router.post("/logical", response_model=LogicalModelOut)
async def create_logical_model(
    req: LogicalModelCreateRequest,
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> LogicalModelOut:
    """新建逻辑模型（同能力下名称唯一）。

    Args:
        req: 逻辑模型创建请求。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        LogicalModelOut: 创建后的逻辑模型。

    Raises:
        HTTPException: 400 —— capability 非法；409 —— 同能力下名称已存在。
    """
    if req.capability not in _ALLOWED_CAPABILITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"capability 仅支持 {'/'.join(_ALLOWED_CAPABILITIES)}",
        )
    exists = await db.scalar(
        select(LogicalModel.id).where(
            LogicalModel.capability == req.capability, LogicalModel.name == req.name
        )
    )
    if exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="同能力下已存在同名逻辑模型"
        )
    model = LogicalModel(
        capability=req.capability,
        name=req.name,
        channel_priority=req.channel_priority,
        is_default=False,  # 新模型默认非默认，需显式通过 PUT 设为默认
        price_per_unit=req.price_per_unit,
    )
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return LogicalModelOut.model_validate(model)


@router.put("/logical/{model_id}", response_model=LogicalModelOut)
async def update_logical_model(
    model_id: int,
    req: LogicalModelUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> LogicalModelOut:
    """更新逻辑模型（部分更新：渠道优先级/默认标记/计价/场景/文档）。

    is_default=True 时执行"同能力互斥默认"切换：先将同 capability 的其他
    模型 is_default 全部置 False，再置目标为 True。这是对部分唯一索引
    idx_lm_default（capability + is_default=true 仅允许一行）的语义维护，
    保证任意时刻每种能力至多一个官方默认模型（PL-28）。

    Args:
        model_id: 逻辑模型 ID。
        req: 逻辑模型更新请求（字段均可选）。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        LogicalModelOut: 更新后的逻辑模型。

    Raises:
        HTTPException: 404 —— 逻辑模型不存在。
    """
    model = await db.get(LogicalModel, model_id)
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="逻辑模型不存在")

    if req.channel_priority is not None:
        model.channel_priority = req.channel_priority
    if req.price_per_unit is not None:
        model.price_per_unit = req.price_per_unit
    if req.scene_desc is not None:
        model.scene_desc = req.scene_desc
    if req.doc_url is not None:
        model.doc_url = req.doc_url
    if req.is_default is not None:
        if req.is_default:
            # 同能力互斥：其他默认模型全部取消默认（部分唯一索引语义）
            others = (
                await db.execute(
                    select(LogicalModel).where(
                        LogicalModel.capability == model.capability,
                        LogicalModel.id != model.id,
                        LogicalModel.is_default.is_(True),
                    )
                )
            ).scalars().all()
            for other in others:
                other.is_default = False
        model.is_default = req.is_default
    await db.commit()
    await db.refresh(model)
    return LogicalModelOut.model_validate(model)
