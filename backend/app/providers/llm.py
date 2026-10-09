"""LLM 提供方模块（文件级说明）。

基于 openai SDK（AsyncOpenAI）封装 DeepSeek 兼容接口：
- get_llm(): 获取配置好的异步客户端
- chat(): 非流式对话补全
- chat_stream(): 流式对话补全（逐段 yield 文本增量）
- health_check(): 渠道健康探测（异常不抛出，返回结果字典）
"""

from typing import AsyncGenerator, Dict, List, Optional

from openai import AsyncOpenAI

from app.core.config import settings


def get_llm() -> AsyncOpenAI:
    """获取配置好的 LLM 异步客户端。

    Returns:
        AsyncOpenAI: 以 deepseek_base_url / deepseek_api_key 初始化的客户端实例。
    """
    return AsyncOpenAI(
        base_url=settings.deepseek_base_url,
        api_key=settings.deepseek_api_key,
    )


async def chat(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    json_mode: bool = False,
) -> str:
    """非流式对话补全。

    Args:
        messages: OpenAI 格式消息列表（role/content）。
        model: 模型名；为 None 时使用配置默认 deepseek_model。
        temperature: 采样温度。
        json_mode: True 时启用 JSON 输出模式（response_format=json_object）。

    Returns:
        str: 模型返回的完整文本内容。

    Raises:
        openai.APIError: 上游接口错误（网络、鉴权、限流等）时抛出。
    """
    client = get_llm()
    kwargs: Dict = {
        "model": model or settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    response = await client.chat.completions.create(**kwargs)
    return response.choices[0].message.content or ""


async def chat_stream(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    json_mode: bool = False,
) -> AsyncGenerator[str, None]:
    """流式对话补全。

    Args:
        messages: OpenAI 格式消息列表（role/content）。
        model: 模型名；为 None 时使用配置默认 deepseek_model。
        temperature: 采样温度。
        json_mode: True 时启用 JSON 输出模式。

    Yields:
        str: 每次产出的文本增量片段（拼接后为完整回答）。

    Raises:
        openai.APIError: 上游接口错误时抛出。
    """
    client = get_llm()
    kwargs: Dict = {
        "model": model or settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    stream = await client.chat.completions.create(**kwargs)
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


async def health_check() -> Dict:
    """LLM 渠道健康探测。

    发送一条最短消息验证连通性；任何异常都被捕获并记录在返回值中，不向上抛出。

    Returns:
        Dict: {"ok": bool, "error": str | None}；ok=True 表示渠道可用。
    """
    try:
        await chat([{"role": "user", "content": "ping"}], temperature=0)
        return {"ok": True, "error": None}
    except Exception as exc:  # noqa: BLE001 —— 健康检查需要捕获一切异常
        return {"ok": False, "error": str(exc)}
