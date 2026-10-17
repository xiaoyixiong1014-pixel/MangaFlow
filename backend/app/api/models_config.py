"""模型配置路由模块（文件级说明）。

实现逻辑模型目录 / 平台渠道信息 / 用户自有 Key 管理 / 渠道健康摘要 / 效果对比：
- GET    /api/models/catalog                逻辑模型（按能力分组，附渠道健康）+ 平台渠道信息（不含 Key）
- GET    /api/models/health-summary         平台渠道健康摘要（D-05，用户端可见只读）
- POST   /api/models/compare                模型效果对比（PL-11，逐渠道生成并返回结果）
- GET    /api/models/channels               当前用户自有渠道（api_key 脱敏前4后4）
- POST   /api/models/channels               新建自有渠道（Key 经 Fernet 加密落库）
- PUT    /api/models/channels/{id}          更新自有渠道（换 Key / 改地址 / 模型目录 / 启停）
- DELETE /api/models/channels/{id}          删除自有渠道（仅自己的）
- POST   /api/models/channels/{id}/test     连通性测试（按渠道能力协议探测），更新健康状态
- POST   /api/models/probe-models           拉取渠道真实模型列表（模型下拉候选，防 SSRF）

注：项目级模型配置存于 projects.config，由 /api/projects 维护，此处不做。
"""

import asyncio
import ipaddress
import socket
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from openai import AsyncOpenAI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_text, encrypt_text
from app.core.database import get_db
from app.core.security import get_current_user
from app.models import LogicalModel, ModelChannel, User
from app.providers.image_agnes import generate_image_real
from app.providers.image_dashscope import generate_image_dashscope
from app.providers.llm import chat_with_meta, probe_llm_channel
from app.schemas.model_config import (
    CatalogGroupOut,
    CatalogModelOut,
    CatalogOut,
    ChannelCreateRequest,
    ChannelOut,
    ChannelProbeModelsOut,
    ChannelProbeModelsRequest,
    ChannelTestResponse,
    ChannelUpdateRequest,
    CompareItemOut,
    CompareRequest,
    CompareResponse,
    HealthChannelItem,
    HealthSummaryOut,
    PlatformChannelOut,
)
from app.services.model_router import ResolvedChannel, _extract_default_model

router = APIRouter(prefix="/api/models", tags=["models"])

# 能力分组展示顺序（PL-26：llm/image/video/tts）
_CAPABILITY_ORDER = ("llm", "image", "video", "tts")

# 渠道连通性测试超时（秒）
_TEST_TIMEOUT_SECONDS = 10.0

# 效果对比单渠道调用超时（秒；图像含生成与下载，放宽到 90s）
_COMPARE_TIMEOUT_SECONDS = 90.0


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


@router.get("/health-summary", response_model=HealthSummaryOut)
async def get_health_summary(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HealthSummaryOut:
    """查询平台渠道健康摘要（D-05，只读，登录用户可见）。

    供工作台顶部「渠道健康标红条」使用：返回全部平台渠道的健康快照与
    down / degraded 计数；不包含任何 Key 信息。

    Args:
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        HealthSummaryOut: 渠道健康列表 + 异常渠道计数。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）。
    """
    rows = (
        await db.execute(
            select(ModelChannel)
            .where(ModelChannel.owner_user_id.is_(None))
            .order_by(ModelChannel.id)
        )
    ).scalars().all()
    items = [HealthChannelItem.model_validate(c) for c in rows]
    return HealthSummaryOut(
        channels=items,
        down_count=sum(1 for c in rows if c.health_status == "down"),
        degraded_count=sum(1 for c in rows if c.health_status == "degraded"),
    )


@router.post("/compare", response_model=CompareResponse)
async def compare_models(
    req: CompareRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompareResponse:
    """模型效果对比（PL-11）：对指定渠道用同一提示词逐渠道生成并返回结果。

    规则：
    - 渠道须为平台渠道（owner_user_id IS NULL）或本人自有渠道，否则该渠道
      返回 ok=False、error="渠道不存在"（不抛 404，避免整批失败）；
    - llm：以渠道自身的 base_url / 解密 Key / models 目录首个模型构造单渠道
      调用链，委托 providers.llm.chat_with_meta（channels 参数指定渠道）；
    - image：构造 ResolvedChannel 委托 providers.image_agnes.generate_image_real
      （结果落 storage/generated，返回 /storage/... 地址）；
    - 单渠道异常（含超时）全部收敛进结果，渠道间互不影响，并发执行。

    Args:
        req: 对比请求（capability/prompt/channel_ids）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        CompareResponse: 与请求顺序一致的逐渠道结果。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）；422 —— 请求体校验失败。
    """
    # 一次批量加载目标渠道（平台渠道或本人自有渠道）
    rows = (
        await db.execute(
            select(ModelChannel).where(
                ModelChannel.id.in_(req.channel_ids),
                (ModelChannel.owner_user_id.is_(None)) | (ModelChannel.owner_user_id == user.id),
            )
        )
    ).scalars().all()
    channel_map: Dict[int, ModelChannel] = {c.id: c for c in rows}

    async def _run_one(channel_id: int) -> CompareItemOut:
        """执行单渠道对比并收敛全部异常（内部函数）。

        Args:
            channel_id: 渠道 ID。

        Returns:
            CompareItemOut: 该渠道的对比结果（失败也返回 ok=False 结构）。

        Raises:
            无：内部捕获全部异常。
        """
        started = time.perf_counter()
        channel = channel_map.get(channel_id)
        if channel is None:
            return CompareItemOut(
                channel_id=channel_id, ok=False,
                error="渠道不存在或无权访问", elapsed_ms=0,
            )
        try:
            if req.capability == "llm":
                model_name = _extract_default_model(channel.models) or "gpt-4o-mini"
                text_out, _meta = await asyncio.wait_for(
                    chat_with_meta(
                        messages=[{"role": "user", "content": req.prompt}],
                        temperature=0.7,
                        channels=[(
                            channel.base_url,
                            _decrypt_api_key(channel.api_key_encrypted) or "missing-key",
                            model_name,
                            f"channel-{channel.id}",
                            channel.protocol,
                        )],
                    ),
                    timeout=_COMPARE_TIMEOUT_SECONDS,
                )
                elapsed = int((time.perf_counter() - started) * 1000)
                return CompareItemOut(
                    channel_id=channel_id, ok=True, text=text_out, elapsed_ms=elapsed,
                )
            # image：构造 ResolvedChannel 真实出图（按渠道协议分派：dashscope 走
            # 百炼原生多模态生成，其余走 OpenAI images 兼容；provider 内部已吞异常）
            resolved = ResolvedChannel(
                channel_id=channel.id,
                name=f"channel-{channel.id}",
                protocol=channel.protocol,
                base_url=channel.base_url,
                api_key=_decrypt_api_key(channel.api_key_encrypted) or "",
                model=_extract_default_model(channel.models) or "image-gen",
                capability="image",
            )
            image_seed = int(time.time() * 1000) % 1_000_000_000 + channel.id
            if channel.protocol == "dashscope":
                image_call = generate_image_dashscope(
                    req.prompt, settings.storage_path() / "generated", image_seed, resolved)
            else:
                image_call = generate_image_real(
                    req.prompt, settings.storage_path() / "generated", image_seed, resolved)
            result = await asyncio.wait_for(
                image_call,
                timeout=_COMPARE_TIMEOUT_SECONDS,
            )
            elapsed = int((time.perf_counter() - started) * 1000)
            if result is None:
                return CompareItemOut(
                    channel_id=channel_id, ok=False,
                    error="图像生成失败（渠道返回异常或超时）", elapsed_ms=elapsed,
                )
            return CompareItemOut(
                channel_id=channel_id, ok=True, image_url=result["url"], elapsed_ms=elapsed,
            )
        except asyncio.TimeoutError:
            elapsed = int((time.perf_counter() - started) * 1000)
            return CompareItemOut(
                channel_id=channel_id, ok=False,
                error=f"生成超时（>{_COMPARE_TIMEOUT_SECONDS:.0f}s）", elapsed_ms=elapsed,
            )
        except Exception as exc:  # noqa: BLE001 —— 对比需要逐渠道收敛一切异常
            elapsed = int((time.perf_counter() - started) * 1000)
            return CompareItemOut(
                channel_id=channel_id, ok=False, error=str(exc), elapsed_ms=elapsed,
            )

    results = list(await asyncio.gather(*(_run_one(cid) for cid in req.channel_ids)))
    return CompareResponse(results=results)


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
            name=c.name,
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
        req: 渠道创建请求（name/protocol/base_url/api_key/models；protocol
            白名单 openai / openai_responses / anthropic 由 Schema 层校验）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ChannelOut: 新建渠道信息（api_key 已脱敏）。

    Raises:
        HTTPException: 400 —— base_url 格式非法；401 —— 未认证；422 —— 协议不在白名单。
    """
    if not req.base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="base_url 须以 http(s):// 开头")

    # 同步主键序列：种子数据以显式 id=1 插入平台渠道，未推进 model_channels_id_seq，
    # 直接 nextval 会与其冲突；此处把序列校准到当前最大 id（幂等、代价可忽略）
    await db.execute(text(
        "SELECT setval(pg_get_serial_sequence('model_channels', 'id'), "
        "GREATEST((SELECT COALESCE(MAX(id), 1) FROM model_channels), 1))"
    ))

    channel = ModelChannel(
        name=(req.name or "").strip() or None,
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
        name=channel.name,
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


@router.put("/channels/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: int,
    req: ChannelUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChannelOut:
    """更新当前用户自有的模型渠道（换 Key / 改地址 / 改模型目录 / 启停）。

    全字段可选语义：None 表示不修改该字段；api_key 仅在提供非空值时以
    Fernet 加密覆盖旧 Key（前端「留空表示保持原 Key」）。任意字段变更后
    渠道健康状态重置为 unknown（旧探测结果不再可信，需重新测试连通）。

    Args:
        channel_id: 渠道 ID（须为本人自有渠道）。
        req: 更新请求（name/base_url/api_key/models/enabled，均可选）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ChannelOut: 更新后的渠道信息（api_key 已脱敏）。

    Raises:
        HTTPException: 400 —— base_url 格式非法；404 —— 渠道不存在或非本人渠道。
    """
    channel = await _get_owned_channel(db, channel_id, user)

    if req.base_url is not None:
        if not req.base_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="base_url 须以 http(s):// 开头")
        channel.base_url = req.base_url
    if req.name is not None:
        channel.name = req.name.strip() or None
    if req.protocol is not None:
        channel.protocol = req.protocol
    if req.api_key is not None:
        channel.api_key_encrypted = encrypt_text(req.api_key)
    if req.models is not None:
        channel.models = req.models
    if req.enabled is not None:
        channel.enabled = req.enabled

    # 配置变更后旧健康状态失效：重置为 unknown 并清空探测时间（等待重新测试）
    channel.health_status = "unknown"
    channel.last_check_at = None
    await db.commit()
    return ChannelOut(
        id=channel.id,
        name=channel.name,
        protocol=channel.protocol,
        base_url=channel.base_url,
        api_key=_mask_api_key(channel.api_key_encrypted) if channel.api_key_encrypted else None,
        models=channel.models,
        health_status=channel.health_status,
        last_check_at=channel.last_check_at,
        enabled=channel.enabled,
    )


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
    """测试自有渠道连通性（按渠道登记的模型用途分派探测），并更新健康状态。

    探测分派（models 目录能力键优先级 chat > image）：
    - 含 "chat" 键：按渠道协议（openai chat / openai responses / anthropic
      messages）发送最短文本消息（probe_llm_channel）；
    - 仅含 "image" 键：向 {base}/images/generations 发极小主体请求，非
      401/403 的 4xx 视为连通（参数合法性不代表渠道不可用，避免真实出图
      计费），401/403/网络异常为 down（与平台渠道健康探测口径一致）。

    Args:
        channel_id: 渠道 ID（须为本人自有渠道）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ChannelTestResponse: ok / error / 更新后的 health_status（healthy 或 down）。

    Raises:
        HTTPException: 404 —— 渠道不存在或非本人渠道；422 —— 协议不在白名单。
    """
    channel = await _get_owned_channel(db, channel_id, user)

    # 从 models 目录提取能力键与首个模型名
    models_cfg: Any = channel.models or {}
    chat_names: list = models_cfg.get("chat") if isinstance(models_cfg, dict) else None
    image_names: list = models_cfg.get("image") if isinstance(models_cfg, dict) else None
    probe_model: Optional[str] = None
    for names in (chat_names, image_names):
        if isinstance(names, list) and names:
            probe_model = str(names[0])
            break

    api_key = _decrypt_api_key(channel.api_key_encrypted) or "missing-key"
    if channel.protocol == "dashscope":
        # DashScope 原生渠道（百炼文生图）：以无效模型名提交探测任务——
        # 鉴权失败返回 401/403，鉴权通过但模型非法返回 4xx 其他值（不计费）
        ok, error = await _probe_dashscope_image(channel.base_url, api_key)
    elif not chat_names and isinstance(image_names, list) and image_names:
        # 图像渠道：images 端点极小请求探测（非 401/403 的 4xx 视为连通，避免计费）
        ok, error = await _probe_image_endpoint(
            channel.base_url, api_key, probe_model or "qwen-image-3.0")
    else:
        ok, error = await probe_llm_channel(
            channel.protocol, channel.base_url, api_key, probe_model or "gpt-4o-mini",
        )

    channel.health_status = "healthy" if ok else "down"
    channel.last_check_at = datetime.now(timezone.utc)
    await db.commit()
    return ChannelTestResponse(ok=ok, error=error, health_status=channel.health_status)


async def _probe_dashscope_image(base_url: str, api_key: str) -> tuple:
    """向 DashScope 原生多模态生成端点发起无效模型探测（内部函数，不计费）。

    以非法模型名同步提交生成请求（qwen-image 走 multimodal-generation 端点，
    不支持异步头，2026-10-16 实测）：401/403 视为鉴权失败（down）；其余 4xx
    （model not found 等参数错误）证明鉴权通过、端点可达（不会产生真实
    生图计费）。

    Args:
        base_url: 渠道接口地址（根域名，含协议）。
        api_key: 明文 API Key。

    Returns:
        tuple: (是否连通, 失败原因；成功为 None)。

    Raises:
        无：全部异常收敛进返回值。
    """
    root = base_url.rstrip("/")
    if "://" in root:
        root = root.split("://", 1)[0] + "://" + root.split("://", 1)[1].split("/", 1)[0]
    url = f"{root}/api/v1/services/aigc/multimodal-generation/generation"
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": "__probe_invalid_model__",
        "input": {"messages": [{"role": "user", "content": [{"text": "ping"}]}]},
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code in (401, 403):
            return False, f"鉴权失败（HTTP {resp.status_code}）"
        if resp.status_code < 500:
            return True, None
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:  # noqa: BLE001 —— 探测需要捕获一切异常
        return False, str(exc)


async def _probe_image_endpoint(
    base_url: str,
    api_key: str,
    model: str,
) -> tuple:
    """向 OpenAI images 兼容端点发起极小请求探测连通性（内部函数）。

    2xx 或非 401/403 的 4xx 视为连通（请求体参数问题不代表渠道不可用，
    避免真实出图产生计费）；401/403 为鉴权失败，网络异常为不可用。

    Args:
        base_url: 渠道接口地址（含 /v1）。
        api_key: 明文 API Key。
        model: 探测模型名。

    Returns:
        tuple: (是否连通, 失败原因；成功为 None)。

    Raises:
        无：全部异常收敛进返回值。
    """
    base = base_url.rstrip("/")
    url = f"{base}/images/generations"
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {"model": model, "prompt": "ping", "n": 1}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code in (401, 403):
            return False, f"鉴权失败（HTTP {resp.status_code}）"
        if 200 <= resp.status_code < 300 or 400 <= resp.status_code < 500:
            return True, None
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:  # noqa: BLE001 —— 探测需要捕获一切异常
        return False, str(exc)


# probe-models 的地址黑名单前缀（防 SSRF：禁止本机/内网/链路本地地址）
_SSRF_HOST_PREFIXES = (
    "localhost", "127.", "10.", "192.168.", "169.254.",
    "0.0.0.0", "[::1]", "[fc", "[fd", "[fe80",
)


def _assert_public_http_url(base_url: str) -> None:
    """校验渠道地址为公网 http(s) 地址（内部函数，防 SSRF）。

    校验规则：必须以 http(s):// 开头；host 为 IP 字面量时必须是公网地址，
    为域名时解析出的首个地址必须是公网地址（拒绝本机回环/内网/链路本地）。

    Args:
        base_url: 待校验的渠道地址。

    Returns:
        None

    Raises:
        HTTPException: 400 —— 地址非法或指向内网/本机。
    """
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="base_url 须以 http(s):// 开头")
    host = urlparse(base_url).hostname or ""
    if not host:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="base_url 缺少主机名")
    if any(host.lower().startswith(p) for p in _SSRF_HOST_PREFIXES):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不允许指向本机或内网地址")
    try:
        infos = socket.getaddrinfo(host, None)
        addr = infos[0][4][0] if infos else ""
        ip = ipaddress.ip_address(addr)
        if not ip.is_global:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不允许指向内网地址")
    except socket.gaierror:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"主机名无法解析：{host}")


@router.post("/probe-models", response_model=ChannelProbeModelsOut)
async def probe_models(
    req: ChannelProbeModelsRequest,
    user: User = Depends(get_current_user),
) -> ChannelProbeModelsOut:
    """拉取渠道真实模型列表（添加渠道对话框的模型下拉候选）。

    以用户提供的地址与 Key 直连渠道 GET {base}/models（OpenAI 兼容与
    Anthropic 均提供该端点；Key 仅本次请求使用不落库）。地址须经公网校验
    （防 SSRF）。全部异常收敛进响应，不向上抛出。

    Args:
        req: 探测请求（protocol/base_url/api_key）。
        user: 当前用户。

    Returns:
        ChannelProbeModelsOut: ok + 模型 ID 列表（排序去重）或失败原因。

    Raises:
        HTTPException: 400 —— 地址非法或指向内网/本机。
    """
    _assert_public_http_url(req.base_url)
    base = req.base_url.rstrip("/")
    if req.protocol == "anthropic":
        url = f"{base}/v1/models" if not base.endswith("/v1") else f"{base}/models"
        headers = {"x-api-key": req.api_key, "anthropic-version": "2023-06-01"}
    elif req.protocol == "dashscope":
        # DashScope 原生渠道：借同账号 compatible-mode 的 /models 拉取模型列表
        root = base.split("://", 1)[0] + "://" + base.split("://", 1)[1].split("/", 1)[0] if "://" in base else base
        url = f"{root}/compatible-mode/v1/models"
        headers = {"Authorization": f"Bearer {req.api_key}"}
    else:
        # openai / openai_responses 均为 {base}/models（Bearer 认证）
        url = f"{base}/models"
        headers = {"Authorization": f"Bearer {req.api_key}"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code >= 400:
            return ChannelProbeModelsOut(ok=False, error=f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = (resp.json() or {}).get("data") or []
        models = sorted({
            str(item.get("id")) for item in data
            if isinstance(item, dict) and item.get("id")
        })
        return ChannelProbeModelsOut(ok=True, models=models)
    except Exception as exc:  # noqa: BLE001 —— 拉取失败收敛进响应
        return ChannelProbeModelsOut(ok=False, error=str(exc))
