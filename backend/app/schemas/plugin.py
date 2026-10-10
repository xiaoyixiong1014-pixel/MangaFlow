"""画布插件与 MCP 集成 Pydantic Schema 模块（文件级说明）。

定义 CV-20/21/22 插件体系与 CV-19 MCP 桥的出入参模型：
- 插件：安装（创建）/ 审查 / 启停 / 手动调用；
- MCP：server 增删查 / 工具列举 / 工具调用。

存储说明：canvas_plugins 表仅有 permissions JSONB 扩展空间，
inputs/outputs/description 一并收纳于 permissions 字典
（{"scopes": [...], "inputs": [...], "outputs": [...], "description": str}），
出参按该结构回显。
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PluginPortIn(BaseModel):
    """插件端口定义（输入/输出端口，对应 mangaflow-design 的 PluginPortDef）。

    Attributes:
        name: 端口名。
        type: 端口数据类型 text/image/json（缺省 text）。
    """

    name: str = Field(min_length=1, max_length=50)
    type: str = Field(default="text", pattern="^(text|image|json)$")


class PluginCreateRequest(BaseModel):
    """插件安装（创建）请求。

    Attributes:
        name: 插件名（1~100 字符）。
        version: 版本号（可空，最长 30）。
        description: 描述（可空，最长 500）。
        endpoint_url: 插件地址（builtin: 前缀 = 内置实现，否则 HTTP URL，最长 500）。
        inputs: 输入端口定义列表。
        outputs: 输出端口定义列表。
        permissions: 权限清单（字符串列表，RD-46）。
    """

    name: str = Field(min_length=1, max_length=100)
    version: Optional[str] = Field(default=None, max_length=30)
    description: Optional[str] = Field(default=None, max_length=500)
    endpoint_url: str = Field(min_length=1, max_length=500)
    inputs: List[PluginPortIn] = Field(default_factory=list)
    outputs: List[PluginPortIn] = Field(default_factory=list)
    permissions: List[str] = Field(default_factory=list)


class PluginOut(BaseModel):
    """插件输出模型。

    Attributes:
        id: 插件 ID。
        user_id: 安装者 ID。
        name: 插件名。
        version: 版本。
        description: 描述（取自 permissions JSONB）。
        endpoint_url: 插件地址（即库列 plugin_url）。
        inputs / outputs: 端口定义（取自 permissions JSONB）。
        permissions: 权限清单（scopes 列表）。
        granted: 用户是否已显式授权。
        enabled: 是否启用。
        reviewed: 是否通过市场审核（True = approved；False = pending）。
    """

    id: int
    user_id: int
    name: str
    version: Optional[str] = None
    description: Optional[str] = None
    endpoint_url: str
    inputs: List[Dict[str, Any]] = Field(default_factory=list)
    outputs: List[Dict[str, Any]] = Field(default_factory=list)
    permissions: List[str] = Field(default_factory=list)
    granted: bool = False
    enabled: bool = False
    reviewed: bool = False


class PluginReviewRequest(BaseModel):
    """插件审查请求（运营方）。

    Attributes:
        approved: True=通过审核（reviewed=True）；False=驳回（reviewed=False）。
        comment: 审查意见（可空，最长 500）。
    """

    approved: bool
    comment: Optional[str] = Field(default=None, max_length=500)


class PluginInvokeRequest(BaseModel):
    """插件手动调用请求。

    Attributes:
        payload: 调用负载（JSON dict，结构由插件 inputs 定义）。
    """

    payload: Dict[str, Any] = Field(default_factory=dict)


class McpServerIn(BaseModel):
    """MCP server 添加请求（运营方）。

    Attributes:
        name: 展示名（1~100 字符）。
        url: MCP streamable HTTP 端点地址（最长 500）。
        headers: 附加请求头（如鉴权头，键值均为字符串）。
    """

    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=500)
    headers: Optional[Dict[str, str]] = None


class McpServerOut(BaseModel):
    """MCP server 输出模型。

    Attributes:
        id: server 标识（KV 内生成的短随机串）。
        name: 展示名。
        url: 端点地址。
        headers: 附加请求头（可空；回显给配置者）。
    """

    id: str
    name: str
    url: str
    headers: Optional[Dict[str, str]] = None


class McpServersOut(BaseModel):
    """MCP server 列表响应。

    Attributes:
        servers: 全部已配置 server。
    """

    servers: List[McpServerOut] = Field(default_factory=list)


class McpCallRequest(BaseModel):
    """MCP 工具调用请求。

    Attributes:
        tool: 工具名。
        arguments: 工具参数（JSON dict，缺省空对象）。
    """

    tool: str = Field(min_length=1, max_length=200)
    arguments: Dict[str, Any] = Field(default_factory=dict)
