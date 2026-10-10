"""画布插件调用服务模块（文件级说明）。

插件执行统一入口（CV-21 / RD-46）：
- builtin: 前缀 → 进程内直接调用内置实现（BUILTIN_PLUGINS 注册表），
  不走 HTTP，供离线/演示环境使用；
- 其余 endpoint_url → httpx POST JSON（timeout 60s），非 2xx 或响应
  非 JSON 时抛 PluginError。

api/canvas_plugins 的手动 invoke 与画布执行引擎（canvas_service 的
plugin 节点分派）共用本模块，保证鉴权/校验之后的调用行为一致。
"""

import logging
from typing import Any, Awaitable, Callable, Dict

import httpx

logger = logging.getLogger("mangaflow.plugin_service")

# 内置插件调用超时（秒）：HTTP 插件统一 60s 兜底
_PLUGIN_HTTP_TIMEOUT = 60.0

# 内置插件实现函数签名：入参 payload，返回结果 dict（异步）
BuiltinHandler = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]

# 内置插件注册表：key = endpoint_url 中 "builtin:" 之后的实现名
BUILTIN_PLUGINS: Dict[str, BuiltinHandler] = {}


class PluginError(Exception):
    """插件调用失败异常（文件级语义：统一在服务层收敛，路由层转 HTTPException）。

    Attributes:
        message: 面向用户的失败原因描述。
    """

    def __init__(self, message: str) -> None:
        """初始化异常。

        Args:
            message: 失败原因描述。
        """
        super().__init__(message)
        self.message = message


def register_builtin(name: str, handler: BuiltinHandler) -> None:
    """注册内置插件实现（模块导入期由实现方调用）。

    Args:
        name: 内置实现名（endpoint_url 形如 "builtin:<name>"）。
        handler: 异步处理函数（payload → 结果 dict）。

    Returns:
        None
    """
    BUILTIN_PLUGINS[name] = handler


async def invoke_plugin(plugin, payload: Dict[str, Any]) -> Dict[str, Any]:
    """调用插件：builtin 前缀进程内直调，否则 HTTP POST（CV-21）。

    Args:
        plugin: CanvasPlugin ORM 对象（取 plugin_url 决定调用方式）。
        payload: 调用负载（JSON dict，结构由插件 inputs 定义）。

    Returns:
        Dict[str, Any]: 插件返回的结果 dict。

    Raises:
        PluginError: builtin 实现不存在 / HTTP 超时 / 非 2xx / 响应非 JSON 对象。
    """
    url = plugin.plugin_url or ""
    if url.startswith("builtin:"):
        name = url[len("builtin:"):].strip()
        handler = BUILTIN_PLUGINS.get(name)
        if handler is None:
            raise PluginError(f"内置插件实现不存在: builtin:{name}")
        result = await handler(payload)
        if not isinstance(result, dict):
            raise PluginError("内置插件返回值必须为 dict")
        return result

    # 外部 HTTP 插件：短连接 POST JSON（无状态简化，每次调用独立建连）
    try:
        async with httpx.AsyncClient(timeout=_PLUGIN_HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("插件 %s(%s) HTTP 调用失败: %s", plugin.name, url, exc)
        raise PluginError(f"插件请求失败: {exc}") from exc
    if not (200 <= resp.status_code < 300):
        raise PluginError(f"插件返回非 2xx 状态码: {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise PluginError("插件响应不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise PluginError("插件响应 JSON 必须为对象")
    return data
