"""模型配置 Pydantic Schema 模块（文件级说明）。

定义逻辑模型目录、平台渠道信息、用户自有渠道管理的出入参模型。
注意：任何出参模型均不包含 api_key 明文/密文，仅提供脱敏展示字段。
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# 用户自有渠道支持的 API 协议白名单（OpenAI 兼容 chat / OpenAI Responses /
# Anthropic Messages / DashScope 原生（百炼文生图，仅图像用途））
SUPPORTED_CHANNEL_PROTOCOLS = ("openai", "openai_responses", "anthropic", "dashscope")


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
        name: 渠道备注名（可空，仅用于展示）。
        protocol: 协议类型（openai = OpenAI Chat Completions 兼容 /
            openai_responses = OpenAI Responses API / anthropic = Anthropic Messages /
            dashscope = DashScope 原生，百炼文生图，仅图像用途）。
        base_url: 接口地址（须以 http:// 或 https:// 开头）。
        api_key: API Key 明文（服务端经 AES 加密后落库，不再返回明文）。
        models: 模型目录（JSONB，如 {"chat": ["model-a"]}，可空）。
    """

    name: Optional[str] = Field(default=None, max_length=64)
    protocol: Literal["openai", "openai_responses", "anthropic", "dashscope"] = "openai"
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = Field(min_length=1, max_length=200)
    models: Optional[Dict[str, Any]] = None


class ChannelUpdateRequest(BaseModel):
    """用户自有渠道更新请求（PUT /api/models/channels/{id}，全字段可选）。

    语义：None 表示不修改该字段；api_key 仅在提供非空值时更新（配合前端
    「留空表示保持原 Key」的交互）。任意配置变更后渠道健康状态重置为
    unknown（旧探测结果不再可信，需重新测试）。

    Attributes:
        name: 渠道备注名（None=不修改；空串=清空备注）。
        protocol: 协议类型（None=不修改，白名单同创建请求）。
        base_url: 接口地址（None=不修改）。
        api_key: 新 API Key 明文（None=不修改；非空则加密覆盖旧 Key）。
        models: 模型目录（None=不修改）。
        enabled: 是否启用（None=不修改）。
    """

    name: Optional[str] = Field(default=None, max_length=64)
    protocol: Optional[Literal["openai", "openai_responses", "anthropic", "dashscope"]] = None
    base_url: Optional[str] = Field(default=None, max_length=255)
    api_key: Optional[str] = Field(default=None, min_length=1, max_length=200)
    models: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None


class ChannelOut(BaseModel):
    """用户自有渠道输出模型（api_key 脱敏：前 4 位 + **** + 后 4 位）。

    Attributes:
        id: 渠道 ID。
        name: 渠道备注名（可空）。
        protocol: 协议类型。
        base_url: 接口地址。
        api_key: 脱敏后的 API Key。
        models: 模型目录。
        health_status: 健康状态。
        last_check_at: 最近探测时间。
        enabled: 是否启用。
    """

    id: int
    name: Optional[str] = None
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


class ChannelProbeModelsRequest(BaseModel):
    """拉取渠道真实模型列表请求（POST /api/models/probe-models）。

    以用户提供的地址与 Key 直连渠道 GET {base}/models（OpenAI 兼容与
    Anthropic 均提供该端点），用于「添加渠道」对话框的模型下拉候选。

    Attributes:
        protocol: 协议类型（openai / openai_responses / anthropic / dashscope）。
        base_url: 接口地址（http/https 开头；内网/本机地址被拒绝）。
        api_key: API Key 明文（仅本次请求使用，不落库）。
    """

    protocol: Literal["openai", "openai_responses", "anthropic", "dashscope"] = "openai"
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = Field(min_length=1, max_length=200)


class ChannelProbeModelsOut(BaseModel):
    """渠道真实模型列表响应。

    Attributes:
        ok: 是否拉取成功。
        models: 模型 ID 列表（成功时非空；失败为空列表）。
        error: 失败原因（成功时为 None）。
    """

    ok: bool
    models: List[str] = []
    error: Optional[str] = None


class HealthChannelItem(BaseModel):
    """渠道健康摘要条目（D-05，GET /api/models/health-summary 内嵌）。

    Attributes:
        id: 渠道 ID。
        protocol: 协议类型。
        base_url: 接口地址。
        health_status: 健康状态（unknown/healthy/degraded/down）。
        enabled: 是否启用。
        last_check_at: 最近探测时间。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    protocol: str
    base_url: str
    health_status: str
    enabled: bool
    last_check_at: Optional[datetime] = None


class HealthSummaryOut(BaseModel):
    """渠道健康摘要响应（D-05，用户端可见的只读端点）。

    Attributes:
        channels: 平台渠道健康快照列表（不含 Key）。
        down_count: down 状态渠道数。
        degraded_count: degraded 状态渠道数。
    """

    channels: List[HealthChannelItem]
    down_count: int = 0
    degraded_count: int = 0


class CompareRequest(BaseModel):
    """模型效果对比请求（PL-11，POST /api/models/compare）。

    Attributes:
        capability: 对比能力（llm 文本 / image 图像）。
        prompt: 对比用提示词（1~2000 字）。
        channel_ids: 参与对比的渠道 ID 列表（1~4 个；平台渠道或本人自有渠道）。
    """

    capability: Literal["llm", "image"]
    prompt: str = Field(min_length=1, max_length=2000)
    channel_ids: List[int] = Field(min_length=1, max_length=4)


class CompareItemOut(BaseModel):
    """单渠道对比结果（PL-11）。

    Attributes:
        channel_id: 渠道 ID。
        ok: 是否生成成功。
        text: 文本能力（llm）的生成结果（成功时非空，其余为 None）。
        image_url: 图像能力（image）的结果地址（/storage/...，成功时非空）。
        elapsed_ms: 本次调用耗时（毫秒）。
        error: 失败原因（成功时为 None）。
    """

    channel_id: int
    ok: bool
    text: Optional[str] = None
    image_url: Optional[str] = None
    elapsed_ms: int = 0
    error: Optional[str] = None


class CompareResponse(BaseModel):
    """模型效果对比响应（PL-11）。

    Attributes:
        results: 与请求 channel_ids 顺序一致的结果列表（逐渠道隔离，单渠道
            失败不影响其他渠道）。
    """

    results: List[CompareItemOut]
