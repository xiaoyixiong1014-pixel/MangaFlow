"""画布插件调用服务模块（文件级说明）。

插件执行统一入口（CV-21 / RD-46）：
- builtin: 前缀 → 进程内直接调用内置实现（BUILTIN_PLUGINS 注册表），
  不走 HTTP，供离线/演示环境使用；
- 其余 endpoint_url → httpx POST JSON（timeout 60s），非 2xx 或响应
  非 JSON 时抛 PluginError。

安全边界（CV-22 轻量加固，2026-10-10）：
- validate_endpoint_url：协议仅允许 http/https、禁止内网/保留地址
  （localhost / 127.0.0.0/8 / 10/8 / 172.16-31 / 192.168/16 / 169.254/16 /
  ::1 等，SSRF 防护）、可选运营方信任域白名单（settings.plugin_allowed_hosts，
  空 = 不限公网域名）；安装（POST）与 invoke 前均校验，违规 400；
  "builtin:" 前缀进程内注册表直调不受限；
- 运行时隔离沙箱为已知限制（P2）：当前以 SSRF 防护 + 运营审查 +
  调用留痕（operation_logs）缓解，重量级沙箱（进程/容器级隔离）为后续演进项。

api/canvas_plugins 的手动 invoke 与画布执行引擎（canvas_service 的
plugin 节点分派）共用本模块，保证鉴权/校验之后的调用行为一致。
"""

import ipaddress
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Union
from urllib.parse import urlparse

import httpx

from app.core.config import settings

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


def _parse_ip_literal(host: str) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    """解析主机名为 IP 字面量（内部函数）。

    除标准点分/冒分形式外，兼容十进制整数形式（如 "2130706433" 即 127.0.0.1），
    防止以整数形式绕过内网黑名单（SSRF 常见绕过手法）。

    Args:
        host: 已转小写的主机名。

    Returns:
        Optional[IPv4Address | IPv6Address]: 解析成功返回 IP 对象；非 IP 字面量返回 None。

    Raises:
        无：解析失败一律返回 None，不抛异常。
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if host.isdigit():
        try:
            value = int(host)
            if 0 <= value <= 0xFFFFFFFF:
                return ipaddress.ip_address(value)
        except ValueError:
            return None
    return None


def _is_private_ip(ip: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> bool:
    """判断 IP 是否为内网/保留地址（内部函数，CV-22 SSRF 防护黑名单）。

    覆盖：回环（127.0.0.0/8、::1）、私网（10/8、172.16-31、192.168/16、
    fc00::/7 等 is_private 段）、链路本地（169.254/16、fe80::/10）与保留段；
    IPv4-mapped IPv6（::ffff:x.x.x.x）解包后按 IPv4 判断。

    Args:
        ip: 待判断的 IP 地址对象。

    Returns:
        bool: 属于内网/保留地址返回 True。
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved


def validate_endpoint_url(url: str) -> None:
    """校验插件 endpoint_url 合法性（CV-22 轻量加固，安装与调用前均需通过）。

    规则：
    1. "builtin:" 前缀豁免——进程内注册表直调，不走 HTTP，不受限；
    2. 协议仅允许 http/https（拒绝 file:/ftp: 等其他协议）；
    3. 主机不得为内网保留地址（localhost / 127.0.0.0/8 / 10/8 / 172.16-31 /
       192.168/16 / 169.254/16 / ::1 等，SSRF 防护，含十进制整数形式绕过）；
    4. settings.plugin_allowed_hosts 非空时，主机须命中运营方信任域白名单
       （精确或子域名匹配，大小写不敏感）；空 = 不限公网域名，仅强制第 3 条。

    Args:
        url: 插件地址（endpoint_url / plugin_url）。

    Returns:
        None

    Raises:
        PluginError: 协议非法 / 主机缺失 / 内网保留地址 / 不在信任域白名单。
    """
    if url.startswith("builtin:"):
        return
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise PluginError(f"插件地址仅允许 http/https 协议: {url or '(空)'}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise PluginError(f"插件地址缺少主机名: {url}")
    if host == "localhost" or host.endswith(".localhost"):
        raise PluginError(f"插件地址禁止指向内网主机: {host}")
    ip = _parse_ip_literal(host)
    if ip is not None and _is_private_ip(ip):
        raise PluginError(f"插件地址禁止指向内网/保留地址: {host}")
    allowed_hosts = [h.strip().lower().lstrip(".") for h in settings.plugin_allowed_hosts if h.strip()]
    if allowed_hosts and not any(host == h or host.endswith("." + h) for h in allowed_hosts):
        raise PluginError(f"插件地址主机不在运营方信任域白名单内: {host}")


async def invoke_plugin(plugin, payload: Dict[str, Any]) -> Dict[str, Any]:
    """调用插件：builtin 前缀进程内直调，否则 HTTP POST（CV-21）。

    Args:
        plugin: CanvasPlugin ORM 对象（取 plugin_url 决定调用方式）。
        payload: 调用负载（JSON dict，结构由插件 inputs 定义）。

    Returns:
        Dict[str, Any]: 插件返回的结果 dict。

    Raises:
        PluginError: endpoint_url 校验不通过（CV-22）/ builtin 实现不存在 /
            HTTP 超时 / 非 2xx / 响应非 JSON 对象。
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

    # CV-22：HTTP 调用前再校验一次 endpoint_url（防存量数据或并发改库绕过安装期校验）
    validate_endpoint_url(url)

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
