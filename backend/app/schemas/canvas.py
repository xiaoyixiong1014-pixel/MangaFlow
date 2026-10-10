"""画布 Pydantic Schema 模块（文件级说明）。

定义 AI 画布域全部出入参模型：
- 画布 CRUD：创建（含模板）/ 更新 / 列表项
- 图同步：节点 / 连线批量提交与回显（自动保存全量同步）
- 导入导出：JSON 图导入请求
- 执行：单节点执行与 DAG 增量执行（RD-45）
- 画布助手：对话请求与"回复 + 已应用动作数 + 更新后图"响应

节点类型与 canvas_nodes 表 CHECK 约束（ck_cn_node_type）保持一致；
模板取值限定为三种预置工作流。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PageResult

# 节点类型合法取值（与 canvas_nodes.ck_cn_node_type 约束一致，双保险以应用层校验先行；
# plugin/mcp_tool 为 Task 9 画布增强新增的集成类节点）
NODE_TYPES = (
    "text", "image", "video", "audio", "generate", "asset_ref", "shot",
    "compose", "logic", "group", "plugin", "mcp_tool",
)

# 画布模板合法取值（None 表示空白画布）
CANVAS_TEMPLATES = ("script_to_storyboard", "character_sheet", "style_experiment")

# DAG 增量执行模式合法取值（RD-45）
EXECUTE_MODES = ("all", "dirty", "node")


class CanvasCreateRequest(BaseModel):
    """画布创建请求。

    Attributes:
        title: 画布名称（1~100 字符）。
        project_id: 关联项目 ID（可空 = 独立画布；须归属当前用户）。
        template: 预置模板；None / "script_to_storyboard" / "character_sheet" /
            "style_experiment"，非法值返回 400。
    """

    title: str = Field(min_length=1, max_length=100)
    project_id: Optional[int] = None
    template: Optional[str] = None


class CanvasUpdateRequest(BaseModel):
    """画布基础信息更新请求（全部字段可空，None 表示不修改）。

    Attributes:
        title: 画布名称。
        viewport: 视口状态（缩放/位移，JSONB 原样存储）。
        thumbnail_url: 缩略图 URL。
    """

    title: Optional[str] = Field(default=None, min_length=1, max_length=100)
    viewport: Optional[Dict[str, Any]] = None
    thumbnail_url: Optional[str] = Field(default=None, max_length=255)


class CanvasNodeIn(BaseModel):
    """图同步 - 单节点提交模型（自动保存全量同步）。

    有 id 且为本画布已有节点 → 更新；无 id（或 id 非本画布节点，视为客户端
    临时 id）→ 插入新节点。临时 id 可被同批 edges 的 source_node_id /
    target_node_id 引用，服务端自动映射为真实 id（导入场景同理）。

    Attributes:
        id: 节点 ID（可空；已有节点传真实 id，新建节点可传客户端临时 id）。
        node_type: 节点类型（10 类之一，非法返回 400）。
        position_x: 画布 X 坐标。
        position_y: 画布 Y 坐标。
        size_w: 节点宽（可空）。
        size_h: 节点高（可空）。
        data: 节点内容（文本/提示词/结果 URL 等，JSONB）。
        config: 节点配置（task_type/模型参数等，JSONB）。
        z_index: 层级。
        parent_group_id: 分组父节点 ID（可空；支持临时 id 映射）。
    """

    id: Optional[int] = None
    node_type: str
    position_x: float = 0
    position_y: float = 0
    size_w: Optional[float] = None
    size_h: Optional[float] = None
    data: Optional[Dict[str, Any]] = None
    config: Optional[Dict[str, Any]] = None
    z_index: int = 0
    parent_group_id: Optional[int] = None


class CanvasEdgeIn(BaseModel):
    """图同步 - 单连线提交模型。

    source_node_id / target_node_id 可为真实节点 id 或同批提交的客户端
    临时 id；引用不存在的节点返回 400。source == target 判自环返回 400。

    Attributes:
        id: 连线 ID（可空；已有连线传真实 id，新连线不传）。
        source_node_id: 源节点 ID（真实 id 或同批临时 id）。
        target_node_id: 目标节点 ID（真实 id 或同批临时 id）。
        source_port: 源端口/输出类型（可空）。
        target_port: 目标端口/输入类型（可空）。
    """

    id: Optional[int] = None
    source_node_id: int
    target_node_id: int
    source_port: Optional[str] = None
    target_port: Optional[str] = None


class GraphSyncRequest(BaseModel):
    """图全量同步请求体（PUT /{canvas_id}/graph 与导入共用）。

    语义：提交画布完整节点/连线集合；本次未出现的库内节点/连线将被删除
    （节点删除级联其连线）。提交后整体做自环 + DFS 成环检测，成环返回
    400 且事务回滚、数据不变。

    Attributes:
        nodes: 全量节点列表。
        edges: 全量连线列表。
    """

    nodes: List[CanvasNodeIn] = Field(default_factory=list)
    edges: List[CanvasEdgeIn] = Field(default_factory=list)


class CanvasImportRequest(BaseModel):
    """画布 JSON 导入请求。

    graph 内节点建议携带客户端临时 id（正/负整数均可）以便 edges 引用；
    成环校验同图同步，校验失败返回 400 且不建画布。

    Attributes:
        title: 新画布名称。
        project_id: 关联项目 ID（可空；须归属当前用户）。
        graph: 导入的图（nodes/edges）。
    """

    title: str = Field(min_length=1, max_length=100)
    project_id: Optional[int] = None
    graph: GraphSyncRequest


class CanvasNodeOut(BaseModel):
    """画布节点输出模型。

    Attributes:
        id: 节点 ID。
        node_type: 节点类型。
        position_x / position_y: 画布坐标。
        size_w / size_h: 节点尺寸。
        z_index: 层级。
        data: 节点内容（JSONB）。
        config: 节点配置（JSONB）。
        is_dirty: 脏节点标记（RD-45）。
        cache_ref: 缓存结果引用（RD-45）。
        loop_config: 逻辑节点循环语义（RD-48）。
        status: 节点状态 idle/queued/running/done/failed。
        parent_group_id: 分组父节点 ID。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    node_type: str
    position_x: float
    position_y: float
    size_w: Optional[float] = None
    size_h: Optional[float] = None
    z_index: int = 0
    data: Optional[Dict[str, Any]] = None
    config: Optional[Dict[str, Any]] = None
    is_dirty: bool = False
    cache_ref: Optional[str] = None
    loop_config: Optional[Dict[str, Any]] = None
    status: str = "idle"
    parent_group_id: Optional[int] = None


class CanvasEdgeOut(BaseModel):
    """画布连线输出模型。

    Attributes:
        id: 连线 ID。
        source_node_id: 源节点 ID。
        target_node_id: 目标节点 ID。
        source_port: 源端口/输出类型。
        target_port: 目标端口/输入类型。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_node_id: int
    target_node_id: int
    source_port: Optional[str] = None
    target_port: Optional[str] = None


class CanvasOut(BaseModel):
    """画布基础信息输出模型。

    Attributes:
        id: 画布 ID。
        project_id: 关联项目 ID。
        title: 画布名称。
        type: 类型 standalone/studio_linked。
        viewport: 视口状态。
        thumbnail_url: 缩略图 URL。
        created_at / updated_at: 时间戳（ISO 字符串）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: Optional[int] = None
    title: str
    type: str
    viewport: Optional[Dict[str, Any]] = None
    thumbnail_url: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CanvasGraphOut(BaseModel):
    """画布完整图输出模型（GET 图 / 图同步返回 / 导出共用结构）。

    Attributes:
        canvas: 画布基础信息。
        nodes: 全量节点列表（含真实 id）。
        edges: 全量连线列表（含真实 id）。
    """

    canvas: CanvasOut
    nodes: List[CanvasNodeOut]
    edges: List[CanvasEdgeOut]


class CanvasListItemOut(BaseModel):
    """画布列表项输出模型（含节点/连线计数等缩略字段）。

    Attributes:
        id: 画布 ID。
        project_id: 关联项目 ID。
        title: 画布名称。
        type: 类型。
        thumbnail_url: 缩略图 URL。
        node_count: 节点数量。
        edge_count: 连线数量。
        created_at / updated_at: 时间戳。
    """

    id: int
    project_id: Optional[int] = None
    title: str
    type: str
    thumbnail_url: Optional[str] = None
    node_count: int = 0
    edge_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# 画布分页列表响应（统一 {items,total,page,page_size} 结构）
CanvasListResponse = PageResult[CanvasListItemOut]


class CanvasExecuteRequest(BaseModel):
    """DAG 增量执行请求（RD-45）。

    Attributes:
        mode: 执行模式 all/dirty/node；'node' 时必须同时提供 node_id。
        node_id: 指定执行起点的节点 ID（mode='node' 时必填）。
    """

    mode: str
    node_id: Optional[int] = None


class CanvasExecuteResult(BaseModel):
    """DAG 增量执行结果。

    Attributes:
        executed: 本次实际执行的节点 ID 列表（按拓扑序）。
        skipped: 因"非脏且已有缓存引用"而复用缓存跳过的节点 ID 列表。
        tasks: 由 generate 节点创建的生成任务 ID 列表。
    """

    executed: List[int] = Field(default_factory=list)
    skipped: List[int] = Field(default_factory=list)
    tasks: List[int] = Field(default_factory=list)


class NodeExecuteResult(BaseModel):
    """单节点执行结果（返回节点新状态）。

    Attributes:
        id: 节点 ID。
        node_type: 节点类型。
        status: 执行后状态（text/logic/容器类为 done；generate 为 queued）。
        is_dirty: 执行后脏标记（成功执行后置 False）。
        cache_ref: 执行后缓存引用。
        data: 执行后的节点内容（text/logic 含 output 字段）。
        task_id: generate 节点创建的生成任务 ID（无则为 None）。
    """

    id: int
    node_type: str
    status: str
    is_dirty: bool
    cache_ref: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    task_id: Optional[int] = None


class CloneSubtreeRequest(BaseModel):
    """子图克隆请求（CV 画布增强，Task 9）。

    Attributes:
        node_ids: 待克隆的节点 ID 列表（须属于源画布，缺失节点返回 400）。
        target_canvas_id: 目标画布 ID（缺省克隆到源画布自身）。
    """

    node_ids: List[int] = Field(min_length=1)
    target_canvas_id: Optional[int] = None


class CloneSubtreeResult(BaseModel):
    """子图克隆结果。

    Attributes:
        nodes: 克隆出的节点列表（含真实 id，坐标 +40/+40）。
        edges: 克隆出的连线列表（两端均在克隆集合内的边）。
    """

    nodes: List[CanvasNodeOut] = Field(default_factory=list)
    edges: List[CanvasEdgeOut] = Field(default_factory=list)


class AssistantChatRequest(BaseModel):
    """画布助手对话请求。

    Attributes:
        message: 用户消息（1~2000 字符）。
    """

    message: str = Field(min_length=1, max_length=2000)


class AssistantChatResponse(BaseModel):
    """画布助手对话响应。

    Attributes:
        reply: 助手回复文本（LLM 失败时为离线降级提示）。
        applied: 实际应用的动作数量（add_node/add_edge/update_node）。
        nodes: 应用动作后的最新节点列表。
        edges: 应用动作后的最新连线列表。
    """

    reply: str
    applied: int
    nodes: List[CanvasNodeOut] = Field(default_factory=list)
    edges: List[CanvasEdgeOut] = Field(default_factory=list)
