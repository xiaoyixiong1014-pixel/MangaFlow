"""模型配置 Pydantic Schema 模块（文件级说明）。

定义逻辑模型目录、平台渠道信息、用户自有渠道管理的出入参模型。
注意：任何出参模型均不包含 api_key 明文/密文，仅提供脱敏展示字段。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class CatalogModelOut(BaseModel):
    """逻辑模型条目输出模型。

    Attributes:
        id: 逻辑模型 ID。
        name: 逻辑模型名。
        is_default: 是否官方默认。
        price_per_unit: 计价规则（JSONB）。
        scene_desc: 适用场景描述。
        doc_url: 官方文档链接。
        health: 降级链路中首选渠道的健康状态（unknown/healthy/degraded/down）。
    """

    id: int
    name: str
    is_default: bool
    price_per_unit: Optional[Any] = None
    scene_desc: Optional[str] = None
    doc_url: Optional[str] = None
    health: str = "unknown"


class CatalogGroupOut(BaseModel):
    """按能力分组后的逻辑模型组。

    Attributes:
        capability: 能力类型（llm/image/video/tts）。
        models: 该能力下的逻辑模型列表。
    """

    capability: str
    models: List[CatalogModelOut]


class PlatformChannelOut(BaseModel):
    """平台渠道信息输出模型（不含任何 Key 信息）。

    Attributes:
        id: 渠道 ID。
        protocol: 协议类型。
        base_url: 接口地址。
        health_status: 健康状态。
        enabled: 是否启用。
        last_check_at: 最近探测时间（ISO 字符串）。
    """

    id: int
    protocol: str
    base_url: str
    health_status: str
    enabled: bool
    last_check_at: Optional[datetime] = None


class CatalogOut(BaseModel):
    """模型目录响应。

    Attributes:
        capabilities: 按能力分组的逻辑模型列表。
        platform_channels: 平台渠道信息列表（不含 Key）。
    """

    capabilities: List[CatalogGroupOut]
    platform_channels: List[PlatformChannelOut]


class ChannelCreateRequest(BaseModel):
    """用户自有渠道创建请求。

    Attributes:
        protocol: 协议类型，当前仅支持 "openai"（OpenAI 兼容协议）。
        base_url: 接口地址（须以 http:// 或 https:// 开头）。
        api_key: API Key 明文（服务端经 AES 加密后落库，不再返回明文）。
        models: 模型目录（JSONB，如 {"chat": ["model-a"]}，可空）。
    """

    protocol: str = Field(default="openai", max_length=30)
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = Field(min_length=1, max_length=200)
    models: Optional[Dict[str, Any]] = None


class ChannelOut(BaseModel):
    """用户自有渠道输出模型（api_key 脱敏：前 4 位 + **** + 后 4 位）。

    Attributes:
        id: 渠道 ID。
        protocol: 协议类型。
        base_url: 接口地址。
        api_key: 脱敏后的 API Key。
        models: 模型目录。
        health_status: 健康状态。
        last_check_at: 最近探测时间。
        enabled: 是否启用。
    """

    id: int
    protocol: str
    base_url: str
    api_key: Optional[str] = None
    models: Optional[Dict[str, Any]] = None
    health_status: str
    last_check_at: Optional[datetime] = None
    enabled: bool


class ChannelTestResponse(BaseModel):
    """渠道连通性测试响应。

    Attributes:
        ok: 是否连通成功。
        error: 失败原因（成功时为 None）。
        health_status: 测试后的健康状态（healthy / down）。
    """

    ok: bool
    error: Optional[str] = None
    health_status: str
