"""LLM 提供方模块（文件级说明）。

基于 openai SDK（AsyncOpenAI）封装 OpenAI 兼容接口，支持多渠道故障降级：
- get_llm(): 获取首选渠道（DeepSeek）的异步客户端（保留兼容旧调用方）
- chat(): 非流式对话补全（内部走 chat_with_meta 多渠道降级）
- chat_with_meta(): 非流式补全并返回渠道元信息（channel/model）
- chat_stream(): 流式对话补全（逐段 yield 文本增量，内部走 chat_stream_with_meta）
- chat_stream_with_meta(): 流式补全并返回渠道元信息；渠道切换仅在
  「建立连接失败 / 首块返回前失败」时进行（已输出内容后不可重试）
- health_check(): 逐渠道健康探测（异常不抛出，返回结果字典）

渠道顺序由 _channels() 决定：DeepSeek 为主渠道，Agnes 为兜底渠道；
某渠道失败时记录 warning 日志并自动尝试下一渠道，全部失败抛出最后的异常。
"""

import logging
from typing import AsyncGenerator, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger("mangaflow.providers.llm")


def _channels() -> List[Tuple[str, str, str, str]]:
    """构造 LLM 渠道降级链（内部函数）。

    Returns:
        List[Tuple[str, str, str, str]]: 每项为
            (base_url, api_key, model, 渠道名)，顺序即降级优先级
            （DeepSeek 主渠道在前，Agnes 兜底在后）。

    Raises:
        无：纯配置读取。
    """
    return [
        (settings.deepseek_base_url, settings.deepseek_api_key, settings.deepseek_model, "deepseek"),
        (settings.agnes_llm_base_url, settings.agnes_llm_api_key, settings.agnes_llm_model, "agnes"),
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


async def chat_with_meta(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    json_mode: bool = False,
    channels: Optional[List[Tuple[str, str, str, str]]] = None,
) -> Tuple[str, Dict]:
    """非流式对话补全（多渠道降级），返回内容与渠道元信息。

    依次尝试渠道链中的渠道：某渠道失败记录 warning 并尝试下一渠道；
    全部失败时抛出最后一个渠道的异常。

    Args:
        messages: OpenAI 格式消息列表（role/content）。
        model: 模型名；为 None 时使用各渠道配置的默认模型。
        temperature: 采样温度。
        json_mode: True 时启用 JSON 输出模式（response_format=json_object）。
        channels: 自定义渠道链（每项为 (base_url, api_key, model, name) 四元组）；
            为 None 时使用默认配置渠道链（DeepSeek → Agnes）。
            供模型效果对比等需要指定单一渠道的调用方使用（PL-11）。

    Returns:
        Tuple[str, Dict]: (补全文本, {"channel": 渠道名, "model": 实际模型名})。

    Raises:
        openai.APIError: 全部渠道均失败时抛出最后一个渠道的上游异常。
    """
    last_exc: Optional[Exception] = None
    for channel in (channels if channels is not None else _channels()):
        base_url, api_key, _default_model, name = channel
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        kwargs = _build_kwargs(channel, messages, model, temperature, json_mode)
        try:
            response = await client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content or ""
            return content, {"channel": name, "model": kwargs["model"]}
        except Exception as exc:  # noqa: BLE001 —— 渠道降级需要捕获一切上游异常
            last_exc = exc
            logger.warning("LLM 渠道 %s 调用失败，切换下一渠道：%s", name, exc)
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
) -> AsyncGenerator[Tuple[str, Optional[Dict]], None]:
    """流式对话补全（多渠道降级），逐段产出 (文本增量, 渠道元信息)。

    渠道降级仅在「建立连接失败 / 首个内容块返回前失败」时进行——一旦开始
    向调用方输出内容，中途失败不再切换渠道（避免内容重复），直接向上抛出。
    渠道元信息仅在收到首个内容块时随首段增量返回一次（后续增量为 None），
    调用方可据此记录实际使用的渠道。

    Args:
        messages: OpenAI 格式消息列表（role/content）。
        model: 模型名；为 None 时使用各渠道配置的默认模型。
        temperature: 采样温度。
        json_mode: True 时启用 JSON 输出模式。

    Yields:
        Tuple[str, Optional[Dict]]: (文本增量, 渠道元信息)；
            仅首段增量携带 {"channel", "model"}，其后均为 None。

    Raises:
        openai.APIError: 首块前全部渠道失败时抛出最后一个异常；
            输出中途失败时抛出当前渠道的异常（不重试）。
    """
    last_exc: Optional[Exception] = None
    for channel in _channels():
        base_url, api_key, _default_model, name = channel
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        kwargs = _build_kwargs(channel, messages, model, temperature, json_mode, stream=True)
        emitted = False  # 当前渠道是否已产出内容（决定失败时能否安全降级）
        try:
            # 建立连接：此阶段失败可安全切换下一渠道
            stream = await client.chat.completions.create(**kwargs)
            first_chunk = None
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    first_chunk = chunk.choices[0].delta.content
                    break
            if first_chunk is None:
                # 流正常结束但无任何内容：视为本渠道失败，切换下一渠道
                raise RuntimeError(f"渠道 {name} 流式返回为空")
            # 先置标记再输出：此后任何异常都不允许降级（内容已交付调用方）
            emitted = True
            yield first_chunk, {"channel": name, "model": kwargs["model"]}
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content, None
            return
        except Exception as exc:  # noqa: BLE001 —— 首块前失败切换渠道，中途失败抛出
            if emitted:
                raise
            last_exc = exc
            logger.warning("LLM 流式渠道 %s 建立失败，切换下一渠道：%s", name, exc)
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
    for _base_url, _api_key, default_model, name in _channels():
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
