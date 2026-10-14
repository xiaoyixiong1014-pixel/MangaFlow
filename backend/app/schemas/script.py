"""剧本域 Pydantic Schema 模块（文件级说明）。

定义剧本输入 / 保存 / 输出 / 实体抽取确认的出入参模型。
剧本 content 为 JSONB 自由结构（RD-03 字段级来源标记：每个一级字段
包装为 {"value": ..., "source": "ai"|"user"}），因此一律以 Any 承载。
"""

from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ScriptOut(BaseModel):
    """剧本输出模型。

    Attributes:
        id: 剧本 ID。
        content: 结构化剧本内容（JSONB，字段级 source 标记）。
        raw_text: 原始输入文本。
        fences: 别名归一与冲突记录（RD-05 / RD-06）。
        health_report: 结构化体检结果（RD-02）。
        version: 版本号。
        updated_at: 更新时间。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    content: Optional[Any] = None
    raw_text: Optional[str] = None
    fences: Optional[Any] = None
    health_report: Optional[Any] = None
    version: int
    updated_at: Optional[datetime] = None


class ScriptInputRequest(BaseModel):
    """剧本原始输入请求（PUT /script/input）。

    Attributes:
        mode: 输入模式 idea（点子）/ novel（小说）/ outline（大纲）。
        raw_text: 原始输入文本（非空）。
        episode_id: 可选剧集 ID（S-06；传入时写入该剧本行的 episode_id，
            缺省绑定到未绑定剧集的默认剧本行，存量数据 episode_id 均为 NULL，
            行为与既有接口完全一致）。
    """

    mode: Literal["idea", "novel", "outline"]
    raw_text: str = Field(min_length=1, description="原始输入文本")
    episode_id: Optional[int] = Field(default=None, description="可选剧集 ID（S-06）")


class ScriptSaveRequest(BaseModel):
    """剧本手动保存请求（PUT /script）。

    Attributes:
        content: 用户编辑后的剧本内容（整体以 source=user 包装落库）。
        health_report: 可选的体检结果回写。
        episode_id: 可选剧集 ID（S-06；传入时按该剧集定位剧本行，
            缺省定位未绑定剧集的默认剧本行）。
    """

    content: Any = Field(description="剧本内容（dict，整体包装 source=user）")
    health_report: Optional[Any] = None
    episode_id: Optional[int] = Field(default=None, description="可选剧集 ID（S-06）")


class ExtractedEntity(BaseModel):
    """抽取实体通用条目（角色 / 场景 / 道具共用）。

    Attributes:
        name: 实体名称。
        aliases: 别名列表（仅角色常用）。
        description: 描述。
        confidence: 置信度（0~1）。
        low_confidence: 置信度低于 0.7 时为 True（前端标黄）。
    """

    name: str
    aliases: List[str] = Field(default_factory=list)
    description: str = ""
    confidence: float = 1.0
    low_confidence: bool = False


class ExtractConflict(BaseModel):
    """别名冲突记录（RD-06）。

    Attributes:
        type: 冲突类型（当前固定 alias，名字互相包含）。
        candidates: 冲突候选名列表。
        suggestion: 建议保留的规范名。
    """

    type: str = "alias"
    candidates: List[str]
    suggestion: str


class ExtractResultOut(BaseModel):
    """实体抽取结果（POST /script/extract，同时写入 scripts.fences）。

    Attributes:
        characters: 角色抽取结果。
        scenes: 场景抽取结果。
        props: 道具抽取结果。
        conflicts: 别名冲突列表。
    """

    characters: List[ExtractedEntity] = Field(default_factory=list)
    scenes: List[ExtractedEntity] = Field(default_factory=list)
    props: List[ExtractedEntity] = Field(default_factory=list)
    conflicts: List[ExtractConflict] = Field(default_factory=list)


class ConfirmCharacterItem(BaseModel):
    """确认入池的角色条目。

    Attributes:
        name: 角色名（项目内唯一）。
        aliases: 别名列表。
        description: 角色描述。
        visual_prompt: 视觉提示词（缺省用中文模板生成）。
    """

    name: str = Field(min_length=1, max_length=50)
    aliases: List[str] = Field(default_factory=list)
    description: str = ""
    visual_prompt: Optional[str] = None


class ConfirmSceneItem(BaseModel):
    """确认入池的场景条目。

    Attributes:
        name: 场景名（项目内唯一）。
        description: 场景描述。
        visual_prompt: 视觉提示词（缺省用中文模板生成）。
    """

    name: str = Field(min_length=1, max_length=50)
    description: str = ""
    visual_prompt: Optional[str] = None


class ConfirmPropItem(BaseModel):
    """确认入池的道具条目。

    Attributes:
        name: 道具名。
        description: 道具描述。
        visual_prompt: 视觉提示词（缺省用中文模板生成）。
    """

    name: str = Field(min_length=1, max_length=50)
    description: str = ""
    visual_prompt: Optional[str] = None


class ExtractConfirmRequest(BaseModel):
    """实体抽取确认请求（POST /script/extract/confirm）。

    Attributes:
        characters: 确认入池的角色列表。
        scenes: 确认入池的场景列表。
        props: 确认入池的道具列表。
    """

    characters: List[ConfirmCharacterItem] = Field(default_factory=list)
    scenes: List[ConfirmSceneItem] = Field(default_factory=list)
    props: List[ConfirmPropItem] = Field(default_factory=list)


class ExtractConfirmOut(BaseModel):
    """实体确认结果（返回项目内各类资产现有总数）。

    Attributes:
        characters: 项目内角色资产总数。
        scenes: 项目内场景资产总数。
        props: 项目内道具资产总数。
    """

    characters: int
    scenes: int
    props: int


class ScriptSendToAgentRequest(BaseModel):
    """剧本一键同步到 Agent 会话请求（POST /script/send-to-agent，S-08）。

    Attributes:
        thread_id: 可选既有会话 ID（LangGraph thread_id 字符串；缺省新建会话；
            传入时会校验该会话属于当前用户，不存在返回 404）。
        episode_id: 可选剧集 ID（S-06；传入时按该剧集定位剧本行，
            缺省作用于未绑定剧集的默认剧本行）。
    """

    thread_id: Optional[str] = Field(default=None, description="可选既有会话 ID（缺省新建）")
    episode_id: Optional[int] = Field(default=None, description="可选剧集 ID（S-06）")


class ScriptSendToAgentOut(BaseModel):
    """剧本同步 Agent 会话响应（幂等：同剧本版本重复调用返回既有会话）。

    Attributes:
        thread_id: Agent 会话 ID（新建或既有）。
        duplicated: 是否命中幂等（True = 该剧本版本已同步过，返回既有会话）。
    """

    thread_id: str
    duplicated: bool = False


class ScriptSendToCanvasRequest(BaseModel):
    """剧本投递画布请求（POST /script/send-to-canvas，S-08）。

    Attributes:
        canvas_id: 可选目标画布 ID（缺省取该项目最近一个画布，无则自动
            创建标题为「剧本画布」的画布；传入时校验归属，不匹配返回 404）。
        episode_id: 可选剧集 ID（S-06；传入时按该剧集定位剧本行）。
    """

    canvas_id: Optional[int] = Field(default=None, description="可选目标画布 ID")
    episode_id: Optional[int] = Field(default=None, description="可选剧集 ID（S-06）")


class ScriptSendToCanvasOut(BaseModel):
    """剧本投递画布响应（幂等：同剧本版本重复调用返回既有节点）。

    Attributes:
        canvas_id: 目标画布 ID。
        node_id: 写入的 text 节点 ID（幂等命中时为既有节点）。
        duplicated: 是否命中幂等（True = 该剧本版本已投递过）。
    """

    canvas_id: int
    node_id: int
    duplicated: bool = False
