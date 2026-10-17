"""LLM 提供方模块（文件级说明）。

基于 openai SDK（AsyncOpenAI）与 httpx 封装多协议 LLM 接口，支持多渠道故障降级：
- get_llm(): 获取首选渠道（DeepSeek）的异步客户端（保留兼容旧调用方）
- chat(): 非流式对话补全（内部走 chat_with_meta 多渠道降级）
- chat_with_meta(): 非流式补全并返回渠道元信息（channel/model）
- chat_stream(): 流式对话补全（逐段 yield 文本增量，内部走 chat_stream_with_meta）
- chat_stream_with_meta(): 流式补全并返回渠道元信息；渠道切换仅在
  「建立连接失败 / 首块返回前失败」时进行（已输出内容后不可重试）
- health_check(): 逐渠道健康探测（异常不抛出，返回结果字典）
- probe_llm_channel(): 按协议探测单个渠道连通性（渠道测试接口用）
- 网关错误体识别（_looks_like_gateway_error）：部分聚合网关把 429/402 限流
  包装成 HTTP 200 + 错误 JSON 体返回，SDK 层不抛异常——非流式响应与流式
  首块均做内容级校验，命中错误体视为本渠道失败继续降级

协议分派（模型配置优化 2026-10-16，自有渠道三种 API 格式真实接入）：
- openai：OpenAI Chat Completions（SDK chat.completions，平台默认链）；
- openai_responses：OpenAI Responses API（SDK responses.create，/responses 端点）；
- anthropic：Anthropic Messages（httpx 直调 {base}/v1/messages，system 消息
  抽出为 system 字段，max_tokens 必填，json_mode 以 system 提示约束）。
渠道元组为 (base_url, api_key, model, name[, protocol])，缺省协议按 openai。

渠道顺序由 _channels() 决定：DeepSeek 为主渠道，Agnes 为兜底渠道；
某渠道失败时记录 warning 日志并自动尝试下一渠道，全部失败抛出最后的异常。
"""

import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx
from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger("mangaflow.providers.llm")

# 已知网关限流/配额错误特征词（小写匹配，含空格与下划线两种书写形态）。
# 部分聚合网关会把 429/402 等上游错误包装成 HTTP 200 + 错误 JSON 体返回
# （如 {"message":"too many requests, please slow down and retry"}），
# SDK 层不抛异常，降级机制因此失效。
_GATEWAY_ERROR_KEYWORDS = (
    "too many requests",
    "rate limit",
    "ratelimit",
    "insufficient quota",
    "insufficient_quota",
    "quota exceeded",
    "quota_exceeded",
    "insufficient balance",
    "insufficient_balance",
    "arrearage",
    "api key exhausted",
)
# 网关错误体允许出现的键集合（正常业务 JSON 必含业务字段，键集合不会整体
# 落在该白名单内——以此与"内容里恰好提到限流词"的正常响应区分开）
_GATEWAY_ERROR_KEYS = frozenset({
    "message", "error", "code", "type", "param", "request_id",
    "detail", "status", "status_code", "reason",
})
# 错误体长度上限（正常业务 JSON 均为长文本；网关错误体普遍 < 300 字符）
_GATEWAY_ERROR_MAX_LEN = 500


def _iter_json_strings(data: Any) -> Any:
    """递归产出 JSON 结构中的全部字符串值（内部生成器函数）。

    Args:
        data: 任意 JSON 解析结果（str/dict/list/标量）。

    Yields:
        str: 结构中出现的每一个字符串值（含嵌套 dict/list 内层）。

    Raises:
        无：标量类型直接跳过。
    """
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for value in data.values():
            yield from _iter_json_strings(value)
    elif isinstance(data, list):
        for value in data:
            yield from _iter_json_strings(value)


def _looks_like_gateway_error(content: str) -> bool:
    """检测 200 响应体是否为网关包装的错误 JSON（内部函数）。

    判定条件（全部满足才判为错误体，最大限度防误伤正常业务 JSON）：
    1. 内容非空且长度 ≤ _GATEWAY_ERROR_MAX_LEN；
    2. 小写文本命中 _GATEWAY_ERROR_KEYWORDS 之一；
    3. 内容可解析为 JSON 对象（dict），且其顶层键全部落在错误体键白名单
       _GATEWAY_ERROR_KEYS 内（业务响应必含 logline/scenes 等业务键，
       键集合不会整体落在白名单内）；
    4. 结构中（含嵌套 error 子对象）至少一个字符串值命中特征词。

    Args:
        content: 渠道返回的完整响应文本。

    Returns:
        bool: True 表示该内容是网关错误体（应视为本渠道失败并降级）。

    Raises:
        无：解析失败一律返回 False。
    """
    stripped = (content or "").strip()
    if not stripped or len(stripped) > _GATEWAY_ERROR_MAX_LEN:
        return False
    if not any(kw in stripped.lower() for kw in _GATEWAY_ERROR_KEYWORDS):
        return False
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        # 非 JSON 纯文本错误体（如 "Error: too many requests"）：整体即错误
        return stripped.lower().startswith(("error", "too many", "rate limit"))
    if not isinstance(data, dict):
        return False
    if not set(data.keys()).issubset(_GATEWAY_ERROR_KEYS):
        return False
    return any(
        any(kw in s.lower() for kw in _GATEWAY_ERROR_KEYWORDS)
        for s in _iter_json_strings(data)
    )


def _channels() -> List[Tuple[str, str, str, str, str]]:
    """构造 LLM 渠道降级链（内部函数）。

    Returns:
        List[Tuple[str, str, str, str, str]]: 每项为
            (base_url, api_key, model, 渠道名, 协议)，顺序即降级优先级
            （DeepSeek 主渠道在前，Agnes 兜底在后；平台链均为 openai 协议）。

    Raises:
        无：纯配置读取。
    """
    return [
        (settings.deepseek_base_url, settings.deepseek_api_key, settings.deepseek_model, "deepseek", "openai"),
        (settings.agnes_llm_base_url, settings.agnes_llm_api_key, settings.agnes_llm_model, "agnes", "openai"),
    ]


def get_llm() -> AsyncOpenAI:
    """获取配置好的 LLM 异步客户端（首选渠道 DeepSeek）。

    Returns:
        AsyncOpenAI: 以 deepseek_base_url / deepseek_api_key 初始化的客户端实例。

    Raises:
        无。
    """
    return AsyncOpenAI(
        base_url=settings.deepseek_base_url,
        api_key=settings.deepseek_api_key,
    )


def _build_kwargs(
    channel: Tuple[str, str, str, str],
    messages: List[Dict[str, str]],
    model: Optional[str],
    temperature: float,
    json_mode: bool,
    stream: bool = False,
) -> Dict:
    """组装单渠道补全请求参数（内部函数）。

    Args:
        channel: 渠道四元组 (base_url, api_key, model, name)。
        messages: OpenAI 格式消息列表（role/content）。
        model: 调用方指定模型名；为 None 时使用渠道默认模型。
        temperature: 采样温度。
        json_mode: True 时启用 JSON 输出模式（response_format=json_object）。
        stream: 是否流式请求。

    Returns:
        Dict: 可直接传给 client.chat.completions.create 的参数字典。

    Raises:
        无。
    """
    kwargs: Dict = {
        "model": model or channel[2],
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if stream:
        kwargs["stream"] = True
    return kwargs


# ---------- 多协议调用实现（openai chat / openai responses / anthropic messages） ----------

# httpx 调用超时（秒）：与 openai SDK 默认量级对齐
_HTTP_TIMEOUT_SECONDS = 120.0

# Anthropic Messages 协议默认最大输出 token（该协议 max_tokens 为必填项）
_ANTHROPIC_MAX_TOKENS = 8192

# Anthropic Messages 协议版本头
_ANTHROPIC_VERSION = "2023-06-01"

# JSON 输出约束提示（Anthropic 协议无原生 json mode，以 system 提示约束）
_JSON_ONLY_SUFFIX = "\n\n请只输出合法的 JSON 对象，禁止输出任何非 JSON 内容（含代码块围栏）。"


def _channel_protocol(channel: Tuple) -> str:
    """读取渠道元组的协议位（内部函数，兼容旧四元组缺省 openai）。

    Args:
        channel: 渠道元组 (base_url, api_key, model, name[, protocol])。

    Returns:
        str: 协议类型（openai / openai_responses / anthropic）。

    Raises:
        无。
    """
    return str(channel[4]) if len(channel) > 4 else "openai"


def _anthropic_messages_url(base_url: str) -> str:
    """拼装 Anthropic Messages 端点 URL（内部函数）。

    兼容两种填法：根地址（https://api.anthropic.com）与已带 /v1 的地址，
    统一规范化为 {base}/v1/messages（rstrip 斜杠后无 /v1 则补）。

    Args:
        base_url: 渠道接口地址。

    Returns:
        str: 完整 messages 端点 URL。

    Raises:
        无。
    """
    base = (base_url or "").rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return f"{base}/messages"


def _split_system_messages(messages: List[Dict[str, str]]) -> Tuple[str, List[Dict[str, str]]]:
    """把 OpenAI 风格消息中的 system 项抽出（内部函数，Anthropic 协议用）。

    Args:
        messages: OpenAI 格式消息列表（role/content）。

    Returns:
        Tuple[str, List[Dict[str, str]]]: (合并后的 system 文本, 其余 user/assistant 消息)。

    Raises:
        无。
    """
    system_parts: List[str] = []
    rest: List[Dict[str, str]] = []
    for msg in messages:
        role = str(msg.get("role") or "user")
        content = msg.get("content") or ""
        if role in ("system", "developer"):
            system_parts.append(str(content))
        else:
            rest.append({"role": role if role in ("user", "assistant") else "user", "content": str(content)})
    return "\n\n".join(system_parts), rest


def _extract_anthropic_text(data: Dict[str, Any]) -> str:
    """解析 Anthropic Messages 响应体中的文本（内部函数）。

    Args:
        data: 响应 JSON（content 数组含 text 块）。

    Returns:
        str: 全部 text 块拼接结果（无内容返回空串）。

    Raises:
        无：结构异常按空串兜底。
    """
    parts: List[str] = []
    try:
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
    except Exception:  # noqa: BLE001 —— 解析失败按空文本兜底
        pass
    return "".join(parts)


async def _call_openai_responses(
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    json_mode: bool,
) -> str:
    """以 OpenAI Responses API 协议发起非流式补全（内部函数）。

    通过 openai SDK 的 client.responses.create 调用（渠道端点须支持
    {base}/responses）；system/developer 角色消息随 input 列表透传。

    Args:
        base_url: 渠道接口地址（含 /v1）。
        api_key: 明文 API Key。
        model: 模型名。
        messages: OpenAI 格式消息列表。
        temperature: 采样温度。
        json_mode: True 时启用 JSON 输出格式（text.format=json_object）。

    Returns:
        str: 模型返回的完整文本（response.output_text）。

    Raises:
        Exception: 网络/鉴权/网关错误向上抛出（由主循环降级下一渠道）。
    """
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    kwargs: Dict[str, Any] = {
        "model": model,
        "input": messages,
        "temperature": temperature,
    }
    if json_mode:
        kwargs["text"] = {"format": {"type": "json_object"}}
    response = await client.responses.create(**kwargs)
    return str(response.output_text or "")


async def _call_anthropic_messages(
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    json_mode: bool,
) -> str:
    """以 Anthropic Messages 协议发起非流式补全（内部函数，httpx 直调）。

    请求头 x-api-key + anthropic-version；OpenAI 的 system 消息抽出为
    system 字段；max_tokens 必填（8192）；temperature 收敛到 [0,1]。

    Args:
        base_url: 渠道接口地址（根地址或已带 /v1）。
        api_key: 明文 API Key。
        model: 模型名。
        messages: OpenAI 格式消息列表。
        temperature: 采样温度。
        json_mode: True 时在 system 追加 JSON 输出约束（该协议无原生 json mode）。

    Returns:
        str: 模型返回的完整文本。

    Raises:
        RuntimeError: 非 2xx 响应（含状态码与响应体前 200 字符）。
        httpx.HTTPError: 网络异常向上抛出（由主循环降级下一渠道）。
    """
    system_text, chat_messages = _split_system_messages(messages)
    if json_mode:
        system_text = f"{system_text}{_JSON_ONLY_SUFFIX}" if system_text else _JSON_ONLY_SUFFIX.strip()
    payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": _ANTHROPIC_MAX_TOKENS,
        "messages": chat_messages,
        "temperature": max(0.0, min(1.0, temperature)),
    }
    if system_text:
        payload["system"] = system_text
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.post(_anthropic_messages_url(base_url), headers=headers, json=payload)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return _extract_anthropic_text(resp.json())


async def _call_openai_chat(
    channel: Tuple,
    messages: List[Dict[str, str]],
    model: Optional[str],
    temperature: float,
    json_mode: bool,
) -> str:
    """以 OpenAI Chat Completions 协议发起非流式补全（内部函数，SDK 调用）。

    Args:
        channel: 渠道元组（_build_kwargs 使用其 model 位）。
        messages: OpenAI 格式消息列表。
        model: 调用方指定模型名；None 用渠道默认。
        temperature: 采样温度。
        json_mode: True 时启用 response_format=json_object。

    Returns:
        str: 模型返回的完整文本。

    Raises:
        Exception: 网络/鉴权/网关错误向上抛出（由主循环降级下一渠道）。
    """
    client = AsyncOpenAI(base_url=channel[0], api_key=channel[1])
    kwargs = _build_kwargs(channel, messages, model, temperature, json_mode)
    response = await client.chat.completions.create(**kwargs)
    return response.choices[0].message.content or ""


async def _call_channel(
    channel: Tuple,
    messages: List[Dict[str, str]],
    model: Optional[str],
    temperature: float,
    json_mode: bool,
) -> str:
    """按渠道协议分派非流式补全（内部函数）。

    Args:
        channel: 渠道元组 (base_url, api_key, model, name[, protocol])。
        messages: OpenAI 格式消息列表。
        model: 调用方指定模型名；None 用渠道默认。
        temperature: 采样温度。
        json_mode: 是否启用 JSON 输出模式。

    Returns:
        str: 模型返回的完整文本。

    Raises:
        Exception: 调用失败向上抛出（由主循环降级下一渠道）。
    """
    base_url, api_key, default_model, _name = channel[0], channel[1], channel[2], channel[3]
    protocol = _channel_protocol(channel)
    if protocol == "openai_responses":
        return await _call_openai_responses(
            base_url, api_key, model or default_model, messages, temperature, json_mode)
    if protocol == "anthropic":
        return await _call_anthropic_messages(
            base_url, api_key, model or default_model, messages, temperature, json_mode)
    # openai（默认）：SDK chat.completions
    return await _call_openai_chat(channel, messages, model, temperature, json_mode)


async def _stream_openai_responses(
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    json_mode: bool,
) -> AsyncGenerator[str, None]:
    """以 OpenAI Responses API 协议流式补全（内部函数）。

    逐事件产出文本增量：response.output_text.delta 事件的 delta 字段。

    Args:
        base_url: 渠道接口地址（含 /v1）。
        api_key: 明文 API Key。
        model: 模型名。
        messages: OpenAI 格式消息列表。
        temperature: 采样温度。
        json_mode: True 时启用 JSON 输出格式。

    Yields:
        str: 文本增量片段。

    Raises:
        Exception: 建立连接/鉴权失败向上抛出（首块前可降级下一渠道）。
    """
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    kwargs: Dict[str, Any] = {
        "model": model,
        "input": messages,
        "temperature": temperature,
        "stream": True,
    }
    if json_mode:
        kwargs["text"] = {"format": {"type": "json_object"}}
    stream = await client.responses.create(**kwargs)
    async for event in stream:
        if getattr(event, "type", "") == "response.output_text.delta":
            delta = getattr(event, "delta", None)
            if delta:
                yield str(delta)


async def _stream_anthropic_messages(
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    json_mode: bool,
) -> AsyncGenerator[str, None]:
    """以 Anthropic Messages 协议流式补全（内部函数，httpx SSE 解析）。

    解析 SSE data 行：content_block_delta 事件的 delta.text 为文本增量；
    message_stop / error 事件终止。非 2xx 在读响应头阶段即抛出。

    Args:
        base_url: 渠道接口地址（根地址或已带 /v1）。
        api_key: 明文 API Key。
        model: 模型名。
        messages: OpenAI 格式消息列表。
        temperature: 采样温度。
        json_mode: True 时在 system 追加 JSON 输出约束。

    Yields:
        str: 文本增量片段。

    Raises:
        RuntimeError: 非 2xx 响应（含状态码与响应体前 200 字符）。
        httpx.HTTPError: 网络异常向上抛出。
    """
    system_text, chat_messages = _split_system_messages(messages)
    if json_mode:
        system_text = f"{system_text}{_JSON_ONLY_SUFFIX}" if system_text else _JSON_ONLY_SUFFIX.strip()
    payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": _ANTHROPIC_MAX_TOKENS,
        "messages": chat_messages,
        "temperature": max(0.0, min(1.0, temperature)),
        "stream": True,
    }
    if system_text:
        payload["system"] = system_text
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        async with client.stream(
            "POST", _anthropic_messages_url(base_url), headers=headers, json=payload,
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {resp.status_code}: {body[:200]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except (json.JSONDecodeError, ValueError):
                    continue
                if event.get("type") == "content_block_delta":
                    delta = (event.get("delta") or {})
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield str(delta["text"])
                elif event.get("type") == "message_stop":
                    return


def _stream_channel(
    channel: Tuple,
    messages: List[Dict[str, str]],
    model: Optional[str],
    temperature: float,
    json_mode: bool,
) -> AsyncGenerator[str, None]:
    """按渠道协议分派流式补全（内部函数）。

    Args:
        channel: 渠道元组 (base_url, api_key, model, name[, protocol])。
        messages: OpenAI 格式消息列表。
        model: 调用方指定模型名；None 用渠道默认。
        temperature: 采样温度。
        json_mode: 是否启用 JSON 输出模式。

    Yields:
        str: 文本增量片段。

    Raises:
        Exception: 建立连接/鉴权失败向上抛出（首块前可降级下一渠道）。
    """
    base_url, api_key, default_model, _name = channel[0], channel[1], channel[2], channel[3]
    protocol = _channel_protocol(channel)
    if protocol == "openai_responses":
        return _stream_openai_responses(
            base_url, api_key, model or default_model, messages, temperature, json_mode)
    if protocol == "anthropic":
        return _stream_anthropic_messages(
            base_url, api_key, model or default_model, messages, temperature, json_mode)
    # openai（默认）：SDK chat.completions 流式（保留原有生成器实现于主循环内联逻辑，
    # 此处为统一分派重新实现）
    return _stream_openai_chat(base_url, api_key, channel, messages, model, temperature, json_mode)


async def _stream_openai_chat(
    base_url: str,
    api_key: str,
    channel: Tuple,
    messages: List[Dict[str, str]],
    model: Optional[str],
    temperature: float,
    json_mode: bool,
) -> AsyncGenerator[str, None]:
    """以 OpenAI Chat Completions 协议流式补全（内部函数）。

    Args:
        base_url: 渠道接口地址。
        api_key: 明文 API Key。
        channel: 渠道元组（_build_kwargs 使用其 model 位）。
        messages: OpenAI 格式消息列表。
        model: 调用方指定模型名；None 用渠道默认。
        temperature: 采样温度。
        json_mode: 是否启用 JSON 输出模式。

    Yields:
        str: 文本增量片段。

    Raises:
        Exception: 建立连接/鉴权失败向上抛出。
    """
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    kwargs = _build_kwargs(channel, messages, model, temperature, json_mode, stream=True)
    stream = await client.chat.completions.create(**kwargs)
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


async def chat_with_meta(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    json_mode: bool = False,
    channels: Optional[List[Tuple[str, str, str, str, str]]] = None,
) -> Tuple[str, Dict]:
    """非流式对话补全（多渠道降级），返回内容与渠道元信息。

    依次尝试渠道链中的渠道（按渠道协议分派：openai chat / openai responses /
    anthropic messages）：某渠道失败记录 warning 并尝试下一渠道；全部失败时
    抛出最后一个渠道的异常。

    Args:
        messages: OpenAI 格式消息列表（role/content）。
        model: 模型名；为 None 时使用各渠道配置的默认模型。
        temperature: 采样温度。
        json_mode: True 时启用 JSON 输出模式（openai: response_format=json_object；
            responses: text.format=json_object；anthropic: system 提示约束）。
        channels: 自定义渠道链（每项为 (base_url, api_key, model, name[, protocol])
            五元组，缺省协议按 openai 处理）；为 None 时使用默认配置渠道链
            （DeepSeek → Agnes）。供模型效果对比等需要指定单一渠道的调用方使用。

    Returns:
        Tuple[str, Dict]: (补全文本, {"channel": 渠道名, "model": 实际模型名})。

    Raises:
        openai.APIError: 全部渠道均失败时抛出最后一个渠道的上游异常。
    """
    last_exc: Optional[Exception] = None
    for channel in (channels if channels is not None else _channels()):
        _base_url, _api_key, _default_model, name = channel[0], channel[1], channel[2], channel[3]
        kwargs_model = model or channel[2]
        try:
            content = await _call_channel(channel, messages, model, temperature, json_mode)
            if _looks_like_gateway_error(content):
                # HTTP 200 但内容是网关错误体（限流/配额包装成 200）：
                # 视为本渠道失败，继续降级，不把错误体交付业务层
                raise RuntimeError(f"网关错误体（HTTP 200 包装）: {content[:120]}")
            return content, {"channel": name, "model": kwargs_model}
        except Exception as exc:  # noqa: BLE001 —— 渠道降级需要捕获一切上游异常
            last_exc = exc
            logger.warning("LLM 渠道 %s（%s）调用失败，切换下一渠道：%s",
                           name, _channel_protocol(channel), exc)
    assert last_exc is not None  # 渠道链非空，必然存在最后一个异常
    raise last_exc


async def chat(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    json_mode: bool = False,
) -> str:
    """非流式对话补全（多渠道降级，仅返回文本内容）。

    Args:
        messages: OpenAI 格式消息列表（role/content）。
        model: 模型名；为 None 时使用各渠道配置的默认模型。
        temperature: 采样温度。
        json_mode: True 时启用 JSON 输出模式（response_format=json_object）。

    Returns:
        str: 模型返回的完整文本内容。

    Raises:
        openai.APIError: 全部渠道均失败时抛出最后一个渠道的上游异常。
    """
    content, _meta = await chat_with_meta(messages, model=model, temperature=temperature, json_mode=json_mode)
    return content


async def chat_stream_with_meta(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    json_mode: bool = False,
    channels: Optional[List[Tuple[str, str, str, str, str]]] = None,
) -> AsyncGenerator[Tuple[str, Optional[Dict]], None]:
    """流式对话补全（多渠道降级），逐段产出 (文本增量, 渠道元信息)。

    渠道降级仅在「建立连接失败 / 首个内容块返回前失败」时进行——一旦开始
    向调用方输出内容，中途失败不再切换渠道（避免内容重复），直接向上抛出。
    首块若为完整网关错误体（限流/配额包装成 200 流式返回）同样判定本渠道
    失败并降级。渠道元信息仅在收到首个内容块时随首段增量返回一次（后续
    增量为 None），调用方可据此记录实际使用的渠道。按渠道协议分派：
    openai chat / openai responses / anthropic messages。

    Args:
        messages: OpenAI 格式消息列表（role/content）。
        model: 模型名；为 None 时使用各渠道配置的默认模型。
        temperature: 采样温度。
        json_mode: True 时启用 JSON 输出模式（协议差异同 chat_with_meta）。
        channels: 自定义渠道链（每项为 (base_url, api_key, model, name[, protocol])
            五元组，缺省协议按 openai 处理）；为 None 时使用默认配置渠道链。

    Yields:
        Tuple[str, Optional[Dict]]: (文本增量, 渠道元信息)；
            仅首段增量携带 {"channel", "model"}，其后均为 None。

    Raises:
        openai.APIError: 首块前全部渠道失败时抛出最后一个异常；
            输出中途失败时抛出当前渠道的异常（不重试）。
    """
    last_exc: Optional[Exception] = None
    for channel in (channels if channels is not None else _channels()):
        _base_url, _api_key, _default_model, name = channel[0], channel[1], channel[2], channel[3]
        kwargs_model = model or channel[2]
        emitted = False  # 当前渠道是否已产出内容（决定失败时能否安全降级）
        try:
            # 建立连接并产出首块：此阶段失败可安全切换下一渠道
            # （_stream_channel 返回异步生成器，首块迭代触发实际请求）
            stream = _stream_channel(channel, messages, model, temperature, json_mode)
            first_chunk = None
            async for piece in stream:
                first_chunk = piece
                break
            if first_chunk is None:
                # 流正常结束但无任何内容：视为本渠道失败，切换下一渠道
                raise RuntimeError(f"渠道 {name} 流式返回为空")
            if _looks_like_gateway_error(first_chunk):
                # 首块即完整网关错误体（限流/配额被网关包装成 200 流式返回）：
                # 尚未向调用方产出内容，可安全判定本渠道失败并切换下一渠道
                raise RuntimeError(f"网关错误体（HTTP 200 流式包装）: {first_chunk[:120]}")
            # 先置标记再输出：此后任何异常都不允许降级（内容已交付调用方）
            emitted = True
            yield first_chunk, {"channel": name, "model": kwargs_model}
            async for piece in stream:
                yield piece, None
            return
        except Exception as exc:  # noqa: BLE001 —— 首块前失败切换渠道，中途失败抛出
            if emitted:
                raise
            last_exc = exc
            logger.warning("LLM 流式渠道 %s（%s）建立失败，切换下一渠道：%s",
                           name, _channel_protocol(channel), exc)
    assert last_exc is not None
    raise last_exc


async def chat_stream(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    json_mode: bool = False,
) -> AsyncGenerator[str, None]:
    """流式对话补全（多渠道降级，仅产出文本增量，签名与旧版兼容）。

    Args:
        messages: OpenAI 格式消息列表（role/content）。
        model: 模型名；为 None 时使用各渠道配置的默认模型。
        temperature: 采样温度。
        json_mode: True 时启用 JSON 输出模式。

    Yields:
        str: 每次产出的文本增量片段（拼接后为完整回答）。

    Raises:
        openai.APIError: 首块前全部渠道失败时抛出最后一个异常；
            输出中途失败时抛出当前渠道的异常（不重试）。
    """
    async for piece, _meta in chat_stream_with_meta(messages, model=model, temperature=temperature, json_mode=json_mode):
        yield piece


async def health_check() -> Dict:
    """LLM 渠道健康探测（逐渠道）。

    对 _channels() 中每个渠道独立发送一条最短消息验证连通性；任何异常都
    被捕获并记录在返回值中，不向上抛出。

    Returns:
        Dict: {"ok": 所有渠道是否全部可用,
               "error": 首个失败渠道的错误（全可用为 None）,
               "channels": [{"name", "ok", "error"} 逐渠道探测结果]}。

    Raises:
        无：内部捕获全部异常。
    """
    results: List[Dict] = []
    for _base_url, _api_key, default_model, name, _protocol in _channels():
        try:
            client = AsyncOpenAI(base_url=_base_url, api_key=_api_key, timeout=15.0, max_retries=0)
            await client.chat.completions.create(
                model=default_model,
                messages=[{"role": "user", "content": "ping"}],
                temperature=0,
                max_tokens=8,
            )
            results.append({"name": name, "ok": True, "error": None})
        except Exception as exc:  # noqa: BLE001 —— 健康检查需要捕获一切异常
            results.append({"name": name, "ok": False, "error": str(exc)})
    all_ok = all(item["ok"] for item in results)
    first_error = next((item["error"] for item in results if not item["ok"]), None)
    return {"ok": all_ok, "error": first_error, "channels": results}


async def probe_llm_channel(
    protocol: str,
    base_url: str,
    api_key: str,
    model: str,
) -> Tuple[bool, Optional[str]]:
    """按协议探测单个 LLM 渠道连通性（公共函数，供渠道测试接口调用）。

    对指定渠道发送一条最短消息（约 8 token）验证连通性；全部异常收敛进
    返回值，不向上抛出。

    Args:
        protocol: 渠道协议（openai / openai_responses / anthropic）。
        base_url: 渠道接口地址。
        api_key: 明文 API Key。
        model: 探测模型名。

    Returns:
        Tuple[bool, Optional[str]]: (是否连通, 失败原因；成功为 None)。

    Raises:
        无：全部异常内部捕获。
    """
    try:
        if protocol == "openai_responses":
            client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=15.0, max_retries=0)
            await client.responses.create(model=model, input="ping", max_output_tokens=16)
            return True, None
        if protocol == "anthropic":
            headers = {
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            }
            payload = {
                "model": model,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "ping"}],
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(_anthropic_messages_url(base_url), headers=headers, json=payload)
            if resp.status_code >= 400:
                return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
            return True, None
        if protocol == "openai":
            client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=15.0, max_retries=0)
            await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                temperature=0,
                max_tokens=8,
            )
            return True, None
        return False, f"不支持的协议类型：{protocol}"
    except Exception as exc:  # noqa: BLE001 —— 探测需要捕获一切异常
        return False, str(exc)
