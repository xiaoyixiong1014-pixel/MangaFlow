"""模型路由服务模块（文件级说明）。

实现「逻辑模型 → 物理渠道降级链」的路由解析与健康状态回写：
- ResolvedChannel：解析后的渠道视图（含已解密 Key 与默认模型）。
- resolve_channels_chain()：按 LogicalModel.channel_priority 顺序解析候选渠道
  列表（仅平台渠道 enabled=True 且非 down；解密失败按明文降级）。
- record_result()：业务调用后回写渠道健康状态（成功 healthy（degraded 一次
  成功调用即自动复位）/ 失败 degraded）。
- probe_channel()：按能力协议探测单个渠道连通性（llm 发 1 token 请求；
  image/video 用极小主体试探、非 401/403 的 4xx 视为连通以免真实计费；
  tts 发最短合成请求；mock 协议视为恒可用），并回写 health_status/last_check_at。

注意：本地 mock 渠道不落 model_channels 表也可参与降级——调用方
（tasks/runner.py）在链尾追加 channel_id=0 的 mock 哨兵 ResolvedChannel。
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

import httpx
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_text
from app.core.metrics import set_channel_health
from app.models import LogicalModel, ModelChannel
from app.schemas.model_config import SUPPORTED_CHANNEL_PROTOCOLS

logger = logging.getLogger("mangaflow.services.model_router")

# 健康探测超时（秒）
_PROBE_TIMEOUT_SECONDS = 15.0


@dataclass
class ResolvedChannel:
    """解析后的候选渠道视图（数据类）。

    Attributes:
        channel_id: 渠道 ID；0 表示本地 mock 哨兵渠道（不落库）。
        name: 渠道展示名（当前由 protocol + id 拼装，供日志与 fallback_chain 记录）。
        protocol: 协议类型（openai / mock）。
        base_url: 接口地址（mock 为 "local://mock"）。
        api_key: 已解密的明文 API Key（mock 渠道为空串）。
        model: 默认模型名（取渠道 models 目录第一个；可被调用方覆盖）。
        capability: 能力类型（llm/image/video/tts）。
        owner_user_id: 归属用户 ID（None=平台渠道；非空=用户自有渠道，
            供计费豁免与日志溯源使用）。
    """

    channel_id: int
    name: str
    protocol: str
    base_url: str
    api_key: str
    model: str
    capability: str
    owner_user_id: Optional[int] = None


# 能力类型 → 渠道 models 目录键映射（llm 能力的 OpenAI 兼容目录键为 "chat"）
_CAPABILITY_MODELS_KEY = {"llm": "chat", "image": "image", "video": "video", "tts": "tts"}


def _decrypt_key(stored: Optional[str]) -> str:
    """解密渠道存储的 API Key（内部函数）。

    兼容两种存量：Fernet 密文（正常解密）与历史明文（解密失败按原值降级）。

    Args:
        stored: 库中存储的原始字符串（密文/明文/None）。

    Returns:
        str: 明文 API Key；入参为空返回空串。

    Raises:
        无：解密失败统一降级为按明文处理，不向上抛出。
    """
    if not stored:
        return ""
    try:
        return decrypt_text(stored)
    except (ValueError, TypeError):
        return stored


def _extract_default_model(models: Any) -> str:
    """从渠道 models 目录 JSONB 中提取默认模型名（内部函数）。

    Args:
        models: 渠道 models 字段，形如 {"chat": ["m1"]}（字典）或 ["m1"]（列表）。

    Returns:
        str: 第一个可用模型名；无法解析时返回空串。

    Raises:
        无：任意结构异常均返回空串。
    """
    try:
        if isinstance(models, dict):
            for names in models.values():
                if isinstance(names, (list, tuple)) and names:
                    return str(names[0])
        if isinstance(models, (list, tuple)) and models:
            return str(models[0])
    except Exception:  # noqa: BLE001 —— 目录结构异常不应阻断路由
        pass
    return ""


def mock_sentinel(capability: str) -> ResolvedChannel:
    """构造本地 mock 哨兵渠道（公共函数，供 runner 在渠道链尾部追加）。

    Args:
        capability: 能力类型（image/video/tts）。

    Returns:
        ResolvedChannel: channel_id=0、base_url="local://mock" 的 mock 渠道。

    Raises:
        无。
    """
    return ResolvedChannel(
        channel_id=0,
        name="mock",
        protocol="mock",
        base_url="local://mock",
        api_key="",
        model=f"mock-{capability}",
        capability=capability,
    )


async def resolve_channels_chain(
    session: AsyncSession,
    capability: str,
    user_id: Optional[int] = None,
    logical_model_id: Optional[int] = None,
) -> List[ResolvedChannel]:
    """解析指定能力的渠道降级链。

    定位逻辑模型（优先 logical_model_id，其次该能力的 is_default 记录），
    按 channel_priority JSONB 顺序逐个加载平台渠道，过滤规则：
    enabled=True、owner_user_id 为 NULL（平台渠道）、health_status != "down"；
    Key 解密后随渠道视图返回。

    用户自有渠道优先（模型配置优化 2026-10-16）：user_id 非空时，先加载该用户
    自有的 openai 协议渠道（enabled、非 down、models 目录含对应能力键），按
    id 升序插入链首——自有 Key 免生成积分，优先消耗；随后拼接平台渠道链
    （按 id 去重），全部失败仍由调用方追加 mock 哨兵兜底。

    Args:
        session: 异步数据库会话（由调用方管理事务）。
        capability: 能力类型（llm/image/video/tts）。
        user_id: 用户 ID（非空时其自有渠道优先参与路由；None 仅平台渠道）。
        logical_model_id: 指定逻辑模型 ID；为 None 时取该能力默认模型。

    Returns:
        List[ResolvedChannel]: 按降级顺序排列的候选渠道列表（可能为空）。

    Raises:
        无：渠道缺失/目录损坏等场景均跳过并记录日志，不向上抛出。
    """
    # 定位逻辑模型
    if logical_model_id is not None:
        logical = await session.get(LogicalModel, logical_model_id)
    else:
        logical = (
            await session.execute(
                select(LogicalModel).where(
                    LogicalModel.capability == capability,
                    LogicalModel.is_default.is_(True),
                )
            )
        ).scalar_one_or_none()
    if logical is None:
        logger.warning("能力 %s 未找到可用逻辑模型，渠道链为空", capability)
        return []

    priority = logical.channel_priority if isinstance(logical.channel_priority, list) else []
    chain: List[ResolvedChannel] = []
    seen_ids: set = set()

    # 自有渠道优先：用户本人的 LLM 渠道（openai chat / openai responses /
    # anthropic messages 三种协议），models 目录含对应能力键即参与该能力
    if user_id is not None:
        cap_key = _CAPABILITY_MODELS_KEY.get(capability)
        own_rows = (
            await session.execute(
                select(ModelChannel)
                .where(
                    ModelChannel.owner_user_id == user_id,
                    ModelChannel.protocol.in_(SUPPORTED_CHANNEL_PROTOCOLS),
                    ModelChannel.enabled.is_(True),
                    ModelChannel.health_status != "down",
                )
                .order_by(ModelChannel.id)
            )
        ).scalars().all()
        for channel in own_rows:
            models = channel.models if isinstance(channel.models, dict) else {}
            if not cap_key or not isinstance(models.get(cap_key), (list, tuple)) or not models.get(cap_key):
                continue
            seen_ids.add(channel.id)
            chain.append(ResolvedChannel(
                channel_id=channel.id,
                name=channel.name or f"own-{channel.id}",
                protocol=channel.protocol,
                base_url=channel.base_url,
                api_key=_decrypt_key(channel.api_key_encrypted),
                model=_extract_default_model(models) or "",
                capability=capability,
                owner_user_id=user_id,
            ))

    for raw_id in priority:
        try:
            channel_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if channel_id in seen_ids:
            continue
        channel = await session.get(ModelChannel, channel_id)
        if channel is None:
            logger.warning("渠道 id=%s 在 channel_priority 中被引用但不存在，已跳过", channel_id)
            continue
        # 过滤：仅启用的平台渠道，且非 down 状态
        if not channel.enabled or channel.owner_user_id is not None:
            continue
        if channel.health_status == "down":
            logger.info("渠道 id=%s 健康状态为 down，本次路由跳过", channel_id)
            continue
        seen_ids.add(channel.id)
        chain.append(ResolvedChannel(
            channel_id=channel.id,
            name=f"{channel.protocol}-{channel.id}",
            protocol=channel.protocol,
            base_url=channel.base_url,
            api_key=_decrypt_key(channel.api_key_encrypted),
            model=_extract_default_model(channel.models),
            capability=capability,
        ))
    return chain


async def get_user_llm_channel_tuples(user_id: Optional[int]) -> List[Tuple[str, str, str, str, str]]:
    """获取用户自有 LLM 渠道五元组（供 AgentLLM 前置注入默认渠道链）。

    独立短会话查询（调用方多为无请求级会话的 Agent 上下文）：加载该用户
    自有 LLM 渠道（协议 openai chat / openai responses / anthropic messages，
    enabled、非 down、models 含 "chat" 键），解密 Key 后组装
    (base_url, api_key, 首个模型名, 渠道名, 协议) 列表，按 id 升序。查询或
    解密异常均收敛为空列表，不阻断默认渠道链。

    Args:
        user_id: 用户 ID；None 直接返回空列表。

    Returns:
        List[Tuple[str, str, str, str, str]]: 与 providers.llm._channels() 同构的
            渠道五元组列表（可能为空）。

    Raises:
        无：全部异常内部捕获。
    """
    if user_id is None:
        return []
    try:
        from app.core.database import SessionLocal

        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(ModelChannel)
                    .where(
                        ModelChannel.owner_user_id == user_id,
                        ModelChannel.protocol.in_(SUPPORTED_CHANNEL_PROTOCOLS),
                        ModelChannel.enabled.is_(True),
                        ModelChannel.health_status != "down",
                    )
                    .order_by(ModelChannel.id)
                )
            ).scalars().all()
            tuples: List[Tuple[str, str, str, str, str]] = []
            for channel in rows:
                models = channel.models if isinstance(channel.models, dict) else {}
                names = models.get("chat")
                if not isinstance(names, (list, tuple)) or not names:
                    continue
                api_key = _decrypt_key(channel.api_key_encrypted)
                if not api_key or not channel.base_url:
                    continue
                tuples.append((
                    channel.base_url,
                    api_key,
                    str(names[0]),
                    channel.name or f"own-{channel.id}",
                    channel.protocol,
                ))
            return tuples
    except Exception:  # noqa: BLE001 —— 自有渠道加载失败不应阻断默认渠道链
        logger.exception("加载用户自有 LLM 渠道失败（user_id=%s），跳过前置注入", user_id)
        return []


async def record_result(
    session: AsyncSession,
    channel_id: int,
    ok: bool,
    error: Optional[str] = None,
) -> None:
    """回写渠道调用结果的健康状态（由业务方在每次渠道调用后触发）。

    双向自动流转：ok=True → healthy（degraded 渠道一次成功调用即自动复位，
    无需人工干预或探测任务）；ok=False → degraded（连续失败置 down 的策略
    简化为直接 degraded，可由健康探测任务进一步降级）。复位/降级同时刷新
    last_check_at（渠道表无独立 last_healthy_at 列，last_check_at 即最近
    健康变更时间）。401/403 → down 的口径在 probe_channel（探测）侧维护，
    本函数保持 healthy/degraded 二分不变。mock 哨兵（channel_id=0）不落库，
    直接跳过。

    Args:
        session: 异步数据库会话（不主动 commit，由调用方统一提交）。
        channel_id: 渠道 ID（0 表示 mock，忽略）。
        ok: 本次调用是否成功。
        error: 失败原因（仅用于日志）。

    Returns:
        None

    Raises:
        无：渠道不存在等场景记录日志后返回，不影响业务主流程。
    """
    if channel_id == 0:
        return
    try:
        channel = await session.get(ModelChannel, channel_id)
        if channel is None:
            logger.warning("record_result：渠道 id=%s 不存在，跳过健康回写", channel_id)
            return
        channel.health_status = "healthy" if ok else "degraded"
        channel.last_check_at = datetime.now(timezone.utc)
        # 可观测性：同步 Prometheus 渠道健康 Gauge（1 健康 / 0.5 降级）
        set_channel_health(channel_id, ok)
        if not ok and error:
            logger.warning("渠道 id=%s 调用失败，健康置为 degraded：%s", channel_id, error)
    except Exception:  # noqa: BLE001 —— 健康回写失败不应影响业务主流程
        logger.exception("record_result：渠道 id=%s 健康回写异常", channel_id)


async def _probe_llm(base_url: str, api_key: str, model: str) -> Tuple[bool, Optional[str]]:
    """探测 LLM 渠道连通性（内部函数）：发送 1 条最短消息（限 8 token）。

    Args:
        base_url: 渠道接口地址。
        api_key: 明文 API Key。
        model: 探测模型名。

    Returns:
        Tuple[bool, Optional[str]]: (是否连通, 失败原因)。

    Raises:
        无：异常统一收敛进返回值。
    """
    try:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=_PROBE_TIMEOUT_SECONDS, max_retries=0)
        await client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "ping"}], temperature=0, max_tokens=8,
        )
        return True, None
    except Exception as exc:  # noqa: BLE001 —— 探测需要捕获一切异常
        return False, str(exc)


async def _probe_http_endpoint(
    url: str,
    api_key: str,
    payload: dict,
    accept_any_4xx: bool = False,
) -> Tuple[bool, Optional[str]]:
    """以极小主体 POST 探测图像/视频渠道连通性（内部函数）。

    401/403 视为鉴权失败（down）；accept_any_4xx=True 时其余 4xx 视为连通
    （请求体合法性问题，不代表渠道不可用，避免真实出图/出片产生计费）；
    2xx 直接视为连通。

    Args:
        url: 完整探测 URL。
        api_key: 明文 API Key。
        payload: 探测请求体（极小主体）。
        accept_any_4xx: 其余 4xx（非 401/403）是否视为连通。

    Returns:
        Tuple[bool, Optional[str]]: (是否连通, 失败原因)。

    Raises:
        无：网络异常统一收敛进返回值。
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code in (401, 403):
            return False, f"鉴权失败（HTTP {resp.status_code}）"
        if 200 <= resp.status_code < 300:
            return True, None
        if accept_any_4xx and 400 <= resp.status_code < 500:
            return True, None
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:  # noqa: BLE001 —— 网络异常按不可用处理
        return False, str(exc)


async def probe_channel(channel: ModelChannel) -> Tuple[bool, Optional[str]]:
    """按能力协议探测单个渠道连通性，并回写 health_status 与 last_check_at。

    探测策略（避免真实计费优先）：
    - protocol == "mock"：本地占位渠道恒可用，直接 healthy；
    - llm：AsyncOpenAI 发 1 token 最短请求；
    - image：POST /images/generations 极小主体，2xx 或非 401/403 的 4xx 视为连通；
    - video：POST /videos 极小主体，规则同 image；
    - tts：POST /audio/speech 最短文本，接受 2xx；
    - 401/403/网络异常 → down。

    Args:
        channel: 渠道 ORM 对象（探测后原地更新 health_status / last_check_at，
            是否 commit 由调用方决定）。

    Returns:
        Tuple[bool, Optional[str]]: (是否连通, 失败原因)。

    Raises:
        无：全部异常收敛进返回值，不向上抛出。
    """
    ok, error = False, None
    if channel.protocol == "mock":
        ok = True
    else:
        api_key = _decrypt_key(channel.api_key_encrypted)
        model = _extract_default_model(channel.models)
        base_url = (channel.base_url or "").rstrip("/")
        # 能力推断：渠道表无 capability 列，models 目录含 chat 键视为 LLM 渠道，
        # 否则按图像 → 视频 → TTS 端点顺序逐一试探（命中即视为连通）
        if isinstance(channel.models, dict) and "chat" in channel.models:
            ok, error = await _probe_llm(base_url, api_key, model or settings.deepseek_model)
        else:
            # 按 base_url 可达性 + 各端点试探顺序：图像 → 视频 → TTS
            ok, error = await _probe_http_endpoint(
                f"{base_url}/images/generations", api_key,
                {"model": model, "prompt": "ping", "n": 1, "size": "64x64"},
                accept_any_4xx=True,
            )
            if not ok and error and "鉴权" not in (error or ""):
                # 图像端点 404/405 等不能证明渠道不可用，继续尝试视频端点
                v_ok, v_err = await _probe_http_endpoint(
                    f"{base_url}/videos", api_key,
                    {"model": model, "prompt": "ping", "seconds": "1"},
                    accept_any_4xx=True,
                )
                if v_ok:
                    ok, error = True, None
                else:
                    t_ok, t_err = await _probe_http_endpoint(
                        f"{base_url}/audio/speech", api_key,
                        {"model": model, "input": "ping", "voice": "Cherry", "response_format": "wav"},
                    )
                    if t_ok:
                        ok, error = True, None
                    else:
                        error = f"image: {error}; video: {v_err}; tts: {t_err}"

    # 回写健康状态与探测时间（提交由调用方负责）
    channel.health_status = "healthy" if ok else "down"
    channel.last_check_at = datetime.now(timezone.utc)
    logger.info("渠道 id=%s（%s）探测结果：%s %s", channel.id, channel.protocol, ok, error or "")
    return ok, error
