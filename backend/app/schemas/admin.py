"""管理端（运营方）Pydantic Schema 模块（文件级说明）。

集中定义运营管理端全部接口的请求/响应模型，按模块分组：
- 仪表盘：平台概览统计、运营告警；
- 用户管理：用户列表/详情、积分调整、启用禁用；
- 内容审核：审核队列条目、人工复核请求；
- 模型管理：渠道（Key 脱敏）、逻辑模型、渠道连通性探测；
- 订单管理：订单列表、人工完结/关闭/退款；
- 内容运营：公告 CRUD、公共提示词上下架；
- 站点设置：KV 读写、备份文件。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# ==========================================================================
# 仪表盘（dashboard）
# ==========================================================================


class DayRevenue(BaseModel):
    """单日营收统计项。

    Attributes:
        date: 日期（YYYY-MM-DD）。
        amount: 当日已支付订单金额合计（元）。
    """

    date: str
    amount: float


class DayGeneration(BaseModel):
    """单日生成量统计项。

    Attributes:
        date: 日期（YYYY-MM-DD）。
        image: 当日图像生成任务数。
        video: 当日视频生成任务数。
        tts: 当日配音生成任务数。
    """

    date: str
    image: int = 0
    video: int = 0
    tts: int = 0


class OverviewOut(BaseModel):
    """平台运营概览响应。

    Attributes:
        users_total: 用户总数。
        users_new: 统计范围内新注册用户数。
        revenue_total: 已支付订单金额合计（元）。
        orders_paid: 已支付订单数。
        points_liability: 平台积分负债（全部钱包可用+冻结之和）。
        tasks_total: 生成任务总数。
        tasks_failed: 失败任务数。
        revenue_by_day: 范围内逐日营收。
        generation_by_day: 范围内逐日生成量。
    """

    users_total: int
    users_new: int
    revenue_total: float
    orders_paid: int
    points_liability: int
    tasks_total: int
    tasks_failed: int
    revenue_by_day: List[DayRevenue]
    generation_by_day: List[DayGeneration]


class AlertItem(BaseModel):
    """运营告警条目。

    Attributes:
        level: 告警级别 error/warning。
        type: 告警类型 platform_error/large_order/channel_unhealthy。
        message: 告警描述文本。
        created_at: 告警关联记录时间。
    """

    level: str
    type: str
    message: str
    created_at: Optional[datetime] = None


# ==========================================================================
# 用户管理（users）
# ==========================================================================


class OperationLogBrief(BaseModel):
    """操作日志摘要。

    Attributes:
        id: 日志 ID。
        action: 操作类型。
        detail: 操作详情（JSONB）。
        risk_level: 风险等级 normal/high。
        created_at: 操作时间。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    action: str
    detail: Optional[dict] = None
    risk_level: str
    created_at: Optional[datetime] = None


class ProjectBrief(BaseModel):
    """项目摘要。

    Attributes:
        id: 项目 ID。
        title: 项目标题。
        status: 项目状态。
        created_at: 创建时间。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    status: str
    created_at: Optional[datetime] = None


class WalletBrief(BaseModel):
    """钱包摘要。

    Attributes:
        points_balance: 可用积分。
        frozen_points: 冻结积分。
        version: 乐观锁版本号。
    """

    model_config = ConfigDict(from_attributes=True)

    points_balance: int
    frozen_points: int
    version: int


class AdminUserItem(BaseModel):
    """管理端用户列表条目。

    Attributes:
        id: 用户 ID。
        username: 用户名。
        email: 邮箱。
        role: 角色。
        vip_level: 会员等级。
        status: 状态 0 禁用 / 1 正常。
        points_balance: 可用积分（无钱包时为 0）。
        frozen_points: 冻结积分（无钱包时为 0）。
        project_count: 项目数。
        last_login_at: 最近登录时间。
        created_at: 注册时间。
    """

    id: int
    username: str
    email: Optional[str] = None
    role: str
    vip_level: int
    status: int
    points_balance: int = 0
    frozen_points: int = 0
    project_count: int = 0
    last_login_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class AdminUserDetail(BaseModel):
    """管理端用户详情响应。

    Attributes:
        id: 用户 ID。
        username: 用户名。
        email: 邮箱。
        role: 角色。
        vip_level: 会员等级。
        status: 状态。
        wallet: 钱包信息（不存在时为 None）。
        recent_logs: 近 10 条该用户作为操作人的操作日志。
        recent_projects: 近 5 个项目。
        last_login_at: 最近登录时间。
        created_at: 注册时间。
    """

    id: int
    username: str
    email: Optional[str] = None
    role: str
    vip_level: int
    status: int
    wallet: Optional[WalletBrief] = None
    recent_logs: List[OperationLogBrief] = []
    recent_projects: List[ProjectBrief] = []
    last_login_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class PointsAdjustRequest(BaseModel):
    """管理员积分调整请求。

    Attributes:
        amount: 调整积分（正数发放 / 负数扣减）。
        reason: 调整原因（必填，落审计日志）。
    """

    amount: int
    reason: str = Field(min_length=1)


class PointsAdjustResponse(BaseModel):
    """积分调整响应。

    Attributes:
        points_balance: 调整后的可用积分余额。
        frozen_points: 当前冻结积分。
    """

    points_balance: int
    frozen_points: int


class StatusToggleRequest(BaseModel):
    """管理员启用/禁用用户请求。

    Attributes:
        status: 目标状态 0 禁用 / 1 启用。
        reason: 操作原因（必填，落审计日志）。
    """

    status: int
    reason: str = Field(min_length=1)


class MessageOut(BaseModel):
    """通用操作消息响应。

    Attributes:
        message: 提示信息。
    """

    message: str


# ==========================================================================
# 内容审核（review）
# ==========================================================================


class ReviewItem(BaseModel):
    """审核队列条目。

    Attributes:
        id: 审核记录 ID。
        project_id: 项目 ID。
        project_title: 项目标题（联表，可为 None）。
        target_type: 目标类型 text/image/video/film。
        target_id: 目标 ID。
        storyboard_sequence: 关联分镜镜头序号（联表，可为 None）。
        gate: 审核闸门 generate/compose/publish。
        result: 机审结果 pass/suspect/violate。
        violation_type: 违规类型。
        manual_status: 人工复核状态。
        handler_id: 处理人 ID。
        suggestion: 处理建议。
        created_at: 记录时间。
    """

    id: int
    project_id: Optional[int] = None
    project_title: Optional[str] = None
    target_type: str
    target_id: Optional[int] = None
    storyboard_sequence: Optional[int] = None
    gate: str
    result: str
    violation_type: Optional[str] = None
    manual_status: Optional[str] = None
    handler_id: Optional[int] = None
    suggestion: Optional[str] = None
    created_at: Optional[datetime] = None


class ReviewHandleRequest(BaseModel):
    """人工复核请求。

    Attributes:
        action: 处置动作 approve 通过 / reject 驳回。
        suggestion: 处理建议；reject 时必填。
    """

    action: str
    suggestion: Optional[str] = None


# ==========================================================================
# 模型管理（models）
# ==========================================================================


class ChannelOut(BaseModel):
    """模型渠道输出（API Key 脱敏）。

    Attributes:
        id: 渠道 ID。
        protocol: 协议类型。
        base_url: 接口地址。
        api_key_masked: 脱敏后的 Key（前 4 后 4 + 长度）。
        owner_user_id: 归属用户 ID（NULL = 平台渠道）。
        owner_type: 归属类型 platform/user。
        models: 模型目录（JSONB）。
        health_status: 健康状态。
        last_check_at: 最近探测时间。
        enabled: 是否启用。
        created_at: 创建时间。
    """

    id: int
    protocol: str
    base_url: str
    api_key_masked: str = ""
    owner_user_id: Optional[int] = None
    owner_type: str = "platform"
    models: Optional[dict] = None
    health_status: str
    last_check_at: Optional[datetime] = None
    enabled: bool
    created_at: Optional[datetime] = None


class ChannelCreateRequest(BaseModel):
    """新建渠道请求。

    Attributes:
        protocol: 协议类型（当前仅支持 openai 兼容协议）。
        base_url: 接口地址。
        api_key: 明文 API Key（服务端 Fernet 加密后落库）。
        models: 模型目录（可选 JSONB，如 {"chat": ["model-a"]}）。
    """

    protocol: str
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    models: Optional[dict] = None


class ChannelUpdateRequest(BaseModel):
    """更新渠道请求（部分更新）。

    Attributes:
        base_url: 新接口地址（可选）。
        enabled: 启用/停用（可选）。
        health_status: 手动设置健康状态（可选）。
    """

    base_url: Optional[str] = None
    enabled: Optional[bool] = None
    health_status: Optional[str] = None


class ChannelTestResponse(BaseModel):
    """渠道连通性测试响应（探测失败也返回 200，由 ok 字段表达结果）。

    Attributes:
        ok: 是否连通成功。
        error: 失败原因（成功时为 None）。
        latency_ms: 本次探测耗时（毫秒）。
    """

    ok: bool
    error: Optional[str] = None
    latency_ms: int


class LogicalModelOut(BaseModel):
    """逻辑模型输出。

    Attributes:
        id: 逻辑模型 ID。
        capability: 能力类型 llm/image/video/tts。
        name: 逻辑模型名。
        channel_priority: 渠道优先级列表（存渠道 id，故障降级顺序）。
        is_default: 是否官方默认。
        price_per_unit: 计价规则（JSONB）。
        scene_desc: 适用场景。
        doc_url: 官方文档链接。
        created_at: 创建时间。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    capability: str
    name: str
    channel_priority: list
    is_default: bool
    price_per_unit: Optional[dict] = None
    scene_desc: Optional[str] = None
    doc_url: Optional[str] = None
    created_at: Optional[datetime] = None


class LogicalModelUpdateRequest(BaseModel):
    """更新逻辑模型请求（部分更新）。

    Attributes:
        channel_priority: 新渠道优先级列表（可选）。
        is_default: 设为同能力下默认（可选，会将同能力其他默认置 False）。
        price_per_unit: 新计价规则（可选）。
        scene_desc: 新适用场景（可选）。
        doc_url: 新文档链接（可选）。
    """

    channel_priority: Optional[list] = None
    is_default: Optional[bool] = None
    price_per_unit: Optional[dict] = None
    scene_desc: Optional[str] = None
    doc_url: Optional[str] = None


class LogicalModelCreateRequest(BaseModel):
    """新建逻辑模型请求。

    Attributes:
        capability: 能力类型 llm/image/video/tts。
        name: 逻辑模型名（同能力下唯一）。
        channel_priority: 渠道优先级列表。
        price_per_unit: 计价规则（可选）。
    """

    capability: str
    name: str = Field(min_length=1)
    channel_priority: list
    price_per_unit: Optional[dict] = None


# ==========================================================================
# 订单管理（orders）
# ==========================================================================


class AdminOrderItem(BaseModel):
    """管理端订单列表条目。

    Attributes:
        id: 订单 ID。
        order_no: 订单号。
        user_id: 用户 ID。
        username: 下单用户名（联表）。
        product_type: 商品类型 points/subscription。
        amount: 金额（元）。
        points: 兑换积分。
        pay_channel: 支付渠道。
        status: 订单状态 pending/paid/closed/refunded。
        paid_at: 支付时间。
        created_at: 创建时间。
    """

    id: int
    order_no: str
    user_id: int
    username: Optional[str] = None
    product_type: str
    amount: float
    points: int
    pay_channel: Optional[str] = None
    status: str
    paid_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class OrderActionRequest(BaseModel):
    """订单人工完结/关闭请求。

    Attributes:
        reason: 操作原因（可选，落审计日志）。
    """

    reason: Optional[str] = None


class RefundRequest(BaseModel):
    """订单退款请求。

    Attributes:
        reason: 退款原因（必填；二次确认由前端负责，后端只留痕）。
    """

    reason: str = Field(min_length=1)


class RefundResponse(BaseModel):
    """退款处理响应。

    Attributes:
        message: 处理结果描述。
        order_id: 订单 ID。
        refunded_points: 实际回收积分数（可能因余额不足部分回收）。
    """

    message: str
    order_id: int
    refunded_points: int


# ==========================================================================
# 内容运营（content）
# ==========================================================================


class AdminAnnouncementItem(BaseModel):
    """管理端公告条目（含草稿/下架状态，区别于用户端仅返回已发布）。

    Attributes:
        id: 公告 ID。
        title: 标题。
        content: 内容。
        is_pinned: 是否置顶。
        status: 状态 draft/published/offline。
        published_at: 发布时间。
        created_at: 创建时间。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    content: str
    is_pinned: bool
    status: str
    published_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class AnnouncementCreateRequest(BaseModel):
    """新建公告请求。

    Attributes:
        title: 标题。
        content: 内容。
        is_pinned: 是否置顶（默认 False）。
        status: 发布状态 published 立即发布 / draft 存草稿（默认 published）。
    """

    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    is_pinned: bool = False
    status: str = "published"


class AnnouncementUpdateRequest(BaseModel):
    """更新公告请求（部分更新）。

    Attributes:
        title: 新标题（可选）。
        content: 新内容（可选）。
        is_pinned: 是否置顶（可选）。
        status: 新状态 draft/published/offline（可选）。
    """

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    content: Optional[str] = Field(default=None, min_length=1)
    is_pinned: Optional[bool] = None
    status: Optional[str] = None


class AdminPromptItem(BaseModel):
    """管理端公共提示词条目（含上下架状态）。

    Attributes:
        id: 提示词 ID。
        user_id: 归属用户 ID（公共提示词为 NULL）。
        scope: 范围 public/private。
        capability: 适用能力。
        title: 标题。
        content: 内容。
        category: 分类。
        tags: 标签（JSONB；种子数据为数组、管理端可能传对象，故用 Any 承载）。
        status: 上下架状态 published/offline。
        created_at: 创建时间。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int] = None
    scope: str
    capability: Optional[str] = None
    title: str
    content: str
    category: Optional[str] = None
    tags: Optional[Any] = None
    status: str = "published"
    created_at: Optional[datetime] = None


class PromptCreateRequest(BaseModel):
    """新建公共提示词请求。

    Attributes:
        title: 标题。
        content: 内容。
        capability: 适用能力（可选：角色定妆/分镜画面/运镜/配音）。
        category: 分类（可选）。
        tags: 标签（可选 JSONB；种子数据为数组、管理端可能传对象，故用 Any 承载）。
    """

    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1)
    capability: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[Any] = None


class PromptStatusRequest(BaseModel):
    """提示词上下架请求。

    Attributes:
        status: 目标状态 published 上架 / offline 下架。
    """

    status: str


# ==========================================================================
# 站点设置（settings）
# ==========================================================================


class SettingsPutRequest(BaseModel):
    """站点设置写入请求。

    Attributes:
        settings: 设置 KV 字典（逐 key upsert 到 langgraph_store）。
    """

    settings: Dict[str, Any]


class BackupFileItem(BaseModel):
    """备份文件条目。

    Attributes:
        filename: 文件名。
        size_bytes: 文件大小（字节）。
        modified_at: 最后修改时间。
    """

    filename: str
    size_bytes: int
    modified_at: datetime


class BackupCreateResponse(BaseModel):
    """备份生成响应。

    Attributes:
        message: 处理结果描述。
        filename: 生成的备份文件名。
    """

    message: str
    filename: str
