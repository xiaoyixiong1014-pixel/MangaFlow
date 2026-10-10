"""SSE（Server-Sent Events）推送工具模块（文件级说明）。

提供生成任务进度、Agent 流式输出等场景所需的 SSE 响应封装：
- sse_event(): 将事件名与数据格式化为标准 SSE 文本帧
- sse_stream(): 将异步生成器包装为禁缓存的 StreamingResponse
"""

import json
from typing import Any, AsyncGenerator

from fastapi.responses import StreamingResponse


def sse_event(event: str, data: Any) -> str:
    """将单个事件格式化为 SSE 文本帧。

    Args:
        event: 事件名称（客户端 addEventListener 监听的 event 类型）。
        data: 事件负载，任意可 JSON 序列化对象；中文不做转义。

    Returns:
        str: 形如 "event: {event}\\ndata: {json}\\n\\n" 的 SSE 帧。
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def sse_stream(generator: AsyncGenerator[Any, None]) -> StreamingResponse:
    """将异步生成器包装为 SSE 流式响应。

    Args:
        generator: 产出 SSE 文本帧（通常为 sse_event() 的返回值）的异步生成器。

    Returns:
        StreamingResponse: media_type 为 text/event-stream 的流式响应，
        并携带禁缓存与关闭代理缓冲的响应头。

    Raises:
        Exception: 生成器内部异常将在流式传输过程中中断连接（客户端感知断开）。
    """

    async def _iterator() -> AsyncGenerator[bytes, None]:
        """内部迭代器：把生成器产出编码为 UTF-8 字节流。

        结束/断连时显式关闭被包装的内部生成器，确保其 finally 清理逻辑
        （如通知 SSE 的 unsubscribe 订阅注销）可靠执行，不依赖 GC 兜底。

        Raises:
            无：向上透传迭代异常；正常结束或客户端断连时关闭内部生成器。
        """
        try:
            async for chunk in generator:
                if isinstance(chunk, bytes):
                    yield chunk
                else:
                    yield str(chunk).encode("utf-8")
        finally:
            aclose = getattr(generator, "aclose", None)
            if aclose is not None:
                await aclose()

    return StreamingResponse(
        _iterator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",        # 禁止缓存
            "Connection": "keep-alive",         # 长连接
            "X-Accel-Buffering": "no",          # 关闭 Nginx 等代理的响应缓冲
        },
    )
