"""MCP streamable HTTP 最小客户端模块（文件级说明）。

以 JSON-RPC over HTTP POST 实现 MCP（Model Context Protocol）streamable HTTP
传输的最小调用链（CV-19），不引入官方 mcp SDK：
    initialize → notifications/initialized → tools/list / tools/call

协议要点：
- 每个请求 POST JSON-RPC 消息，头 Accept: "application/json, text/event-stream"；
- 服务端可能返回纯 JSON，也可能返回 SSE 帧（"data: {...}" 行），两者都解析；
- 通知消息（notifications/initialized）不带 id、无需读响应体。

简化约定：每个方法调用独立建立短连接（无状态、不维持会话与
Mcp-Session-Id），适合演示规模；连接失败/超时/JSON-RPC error 抛 McpError。
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("mangaflow.mcp_client")

# MCP 请求超时（秒）
_MCP_TIMEOUT = 60.0
# MCP streamable HTTP 协议版本（客户端声明）
_MCP_PROTOCOL_VERSION = "2024-11-05"
# JSON-RPC 请求自增 id（进程内递增即可，短连接不复用会话）
_rpc_id = 0


class McpError(Exception):
    """MCP 调用失败异常（文件级语义：服务层统一收敛，路由层转 HTTPException）。

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


def _parse_payload(text: str) -> Optional[Dict[str, Any]]:
    """解析 HTTP 响应体：兼容纯 JSON 与 SSE 帧（内部函数）。

    Args:
        text: 响应原始文本。

    Returns:
        Optional[Dict[str, Any]]: 首个解析成功的 JSON-RPC 消息对象；
        全部失败返回 None。

    Raises:
        无：解析失败返回 None，由调用方抛 McpError。
    """
    stripped = text.strip()
    if not stripped:
        return None
    # 1) 纯 JSON（最常见）
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    # 2) SSE 帧：逐行收集 "data:" 负载，取首个合法 JSON-RPC 对象
    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:"):].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            parsed = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


async def _rpc(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    method: str,
    params: Optional[Dict[str, Any]] = None,
    notify: bool = False,
) -> Optional[Dict[str, Any]]:
    """发送一条 JSON-RPC 消息（内部通用函数）。

    Args:
        client: 复用的 httpx 异步客户端（同一方法调用的短连接内共享）。
        url: MCP server 端点地址。
        headers: 附加请求头（服务端配置的鉴权头等）。
        method: JSON-RPC 方法名。
        params: 方法参数（可空）。
        notify: True = 通知消息（无 id，不期待响应）；False = 请求。

    Returns:
        Optional[Dict[str, Any]]: 请求模式下返回响应对象（含 result/error）；
        通知模式返回 None。

    Raises:
        McpError: 网络失败 / 超时 / 非 2xx / 响应不可解析 / JSON-RPC error。
    """
    global _rpc_id
    message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if not notify:
        _rpc_id += 1
        message["id"] = _rpc_id

    request_headers = {
        "Content-Type": "application/json",
        # streamable HTTP 约定：同时接受 JSON 与 SSE 两种响应形态
        "Accept": "application/json, text/event-stream",
        **(headers or {}),
    }
    try:
        resp = await client.post(url, json=message, headers=request_headers)
    except httpx.HTTPError as exc:
        raise McpError(f"MCP 请求失败: {exc}") from exc
    if resp.status_code not in (200, 202):
        raise McpError(f"MCP 服务返回非 2xx 状态码: {resp.status_code}")
    if notify:
        return None  # 通知无需读响应
    payload = _parse_payload(resp.text)
    if payload is None:
        raise McpError("MCP 响应不可解析（既非 JSON 也非 SSE 帧）")
    if "error" in payload and payload["error"]:
        err = payload.get("error") or {}
        raise McpError(f"MCP JSON-RPC 错误: {err.get('message') or err}")
    return payload


async def _handshake(
    client: httpx.AsyncClient, url: str, headers: Dict[str, str]
) -> None:
    """MCP 握手：initialize 请求 + initialized 通知（内部函数）。

    Args:
        client: httpx 异步客户端。
        url: MCP server 端点地址。
        headers: 附加请求头。

    Raises:
        McpError: 握手任一步失败。
    """
    await _rpc(
        client, url, headers, "initialize",
        params={
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mangaflow", "version": "1.0.0"},
        },
    )
    # initialized 通知（无 id、不读响应）
    await _rpc(client, url, headers, "notifications/initialized", notify=True)


async def list_tools(url: str, headers: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """连接 MCP server 并列举工具（tools/list）。

    Args:
        url: MCP streamable HTTP 端点地址。
        headers: 附加请求头（如鉴权头，可空）。

    Returns:
        List[Dict[str, Any]]: 工具描述列表（name/description/inputSchema 等）。

    Raises:
        McpError: 握手或列举失败。
    """
    async with httpx.AsyncClient(timeout=_MCP_TIMEOUT) as client:
        await _handshake(client, url, headers or {})
        payload = await _rpc(client, url, headers or {}, "tools/list")
    result = (payload or {}).get("result") or {}
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise McpError("MCP tools/list 响应缺少 tools 列表")
    return [t for t in tools if isinstance(t, dict)]


async def call_tool(
    url: str,
    name: str,
    arguments: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """连接 MCP server 并调用工具（tools/call）。

    Args:
        url: MCP streamable HTTP 端点地址。
        name: 工具名。
        arguments: 工具参数（可空 = 空对象）。
        headers: 附加请求头（可空）。

    Returns:
        Dict[str, Any]: tools/call 的 result 对象（content/isError 等结构）。

    Raises:
        McpError: 握手或调用失败。
    """
    async with httpx.AsyncClient(timeout=_MCP_TIMEOUT) as client:
        await _handshake(client, url, headers or {})
        payload = await _rpc(
            client, url, headers or {}, "tools/call",
            params={"name": name, "arguments": arguments or {}},
        )
    result = (payload or {}).get("result")
    if not isinstance(result, dict):
        raise McpError("MCP tools/call 响应缺少 result 对象")
    return result
