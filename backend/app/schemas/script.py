"""剧本域 Pydantic Schema 模块（文件级说明）。

定义剧本输入 / 保存 / 输出 / 实体抽取确认的出入参模型。
剧本 content 为 JSONB 自由结构（RD-03 字段级来源标记：每个一级字段
包装为 {"value": ..., "source": "ai"|"user"}），因此一律以 Any 承载。
另含 LLM 生成剧本的结构校验模型：
- ScriptContent（PRD S-03，快速模式）：生成路径每轮输出均须通过该模型
  校验方可落库；
- ScriptBodyContent（P-1 范式级重构 Task 5）：逐集正文结构化门槛
  （outline 可选、locations 可选、scenes 必填）。
P-4 表达力扩展（全部 Optional，旧数据兼容）：SceneItem 增 action/
location/time_of_day/int_ext/sfx/emotion；DialogueItem 增 type/emotion/
character_id；LocationItem 为新增地点条目模型。
"""

from datetime import datetime
from typing import Any, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


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
        base_version: 可选乐观锁基准版本号（并发幂等保护；传入且与剧本
            当前 version 不一致时返回 409，未传保持旧行为兼容既有前端）。
    """

    content: Any = Field(description="剧本内容（dict，整体包装 source=user）")
    health_report: Optional[Any] = None
    episode_id: Optional[int] = Field(default=None, description="可选剧集 ID（S-06）")
    base_version: Optional[int] = Field(
        default=None, description="可选乐观锁基准版本号（不匹配返回 409）")


class OutlineItem(BaseModel):
    """剧本大纲条目（ScriptContent 校验模型子项）。

    Attributes:
        act: 幕名（如「第一幕」）。
        summary: 本幕概要。
    """

    act: str
    summary: str


class DialogueItem(BaseModel):
    """剧本对白条目（ScriptContent 校验模型子项）。

    P-4 表达力扩展：type/emotion/character_id 三个字段全部可选（type 缺省
    「对白」），旧数据（快速模式样例仅 character/line）不受影响。

    Attributes:
        character: 角色名（可空，空串表示旁白/无角色对白）。
        line: 台词内容。
        type: 台词类型（对白/旁白/独白/画外音；缺省「对白」，P-4 扩展）。
        emotion: 台词情绪标注（可空，P-4 扩展）。
        character_id: 角色资产 ID 引用（characters.id；可空，P-4 扩展）。
    """

    character: str = ""
    line: str
    type: Optional[Literal["对白", "旁白", "独白", "画外音"]] = Field(
        default="对白", description="台词类型：对白/旁白/独白/画外音（P-4）")
    emotion: Optional[str] = Field(default=None, description="台词情绪标注（P-4）")
    character_id: Optional[int] = Field(
        default=None, description="角色资产 ID 引用（characters.id，P-4）")

    @field_validator("character", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        """把 LLM 输出的 null 角色名归一为空串（容错旁白对白）。

        Args:
            v: 原始字段值（可能为 None）。

        Returns:
            Any: None 归一为 ""，其余原样透传。

        Raises:
            无：纯值归一。
        """
        return "" if v is None else v


class LocationItem(BaseModel):
    """剧本地点条目（ScriptContent.locations / ScriptBodyContent.locations 子项，P-4）。

    用于沉淀「去重可复用」的地点清单：结构化场次的 location 字段引用
    本清单的 name，避免每场重复书写地点描述。

    Attributes:
        name: 地点名（场景 location 字段引用该值）。
        description: 地点描述（可空）。
        time_of_day: 缺省时段（day/night/dawn/dusk，可空）。
    """

    name: str = Field(min_length=1, description="地点名（场景 location 引用键）")
    description: Optional[str] = Field(default=None, description="地点描述")
    time_of_day: Optional[Literal["day", "night", "dawn", "dusk"]] = Field(
        default=None, description="缺省时段：day/night/dawn/dusk")


class SceneItem(BaseModel):
    """剧本场景条目（ScriptContent 校验模型子项）。

    P-4 表达力扩展：action/location/time_of_day/int_ext/sfx/emotion 全部
    Optional，旧数据（快速模式样例仅核心六字段）不受影响。

    Attributes:
        scene_no: 场景序号（≥1）。
        title: 场景标题。
        plot: 场景剧情描述。
        action: 动作/画面描述（正文结构化场次必填，P-4 扩展）。
        location: 地点名（引用顶层 locations 的 name，P-4 扩展）。
        time_of_day: 时段 day/night/dawn/dusk（P-4 扩展）。
        int_ext: 内外景 int/ext（P-4 扩展）。
        sfx: 音效清单（P-4 扩展）。
        emotion: 场次主导情绪（P-4 扩展）。
        dialogue: 对白列表（可为空）。
        duration_sec: 场景时长（秒，正整数）。
    """

    scene_no: int = Field(ge=1, description="场景序号（≥1）")
    title: str
    plot: str
    action: Optional[str] = Field(default=None, description="动作/画面描述（P-4）")
    location: Optional[str] = Field(
        default=None, description="地点名（引用顶层 locations.name，P-4）")
    time_of_day: Optional[Literal["day", "night", "dawn", "dusk"]] = Field(
        default=None, description="时段：day/night/dawn/dusk（P-4）")
    int_ext: Optional[Literal["int", "ext"]] = Field(
        default=None, description="内外景：int/ext（P-4）")
    sfx: Optional[List[str]] = Field(default=None, description="音效清单（P-4）")
    emotion: Optional[str] = Field(default=None, description="场次主导情绪（P-4）")
    dialogue: List[DialogueItem] = Field(default_factory=list)
    duration_sec: int = Field(gt=0, description="场景时长（秒，正整数）")


class ScriptContent(BaseModel):
    """LLM 生成剧本的结构校验模型（PRD S-03，生成落库前的强制门槛）。

    Attributes:
        title: 剧本标题。
        logline: 一句话故事梗概。
        outline: 三幕大纲（至少 1 项）。
        scenes: 场景列表（至少 1 项）。
    """

    # 容忍 LLM 输出的额外字段（如备注/元信息），仅校验核心结构
    model_config = ConfigDict(extra="ignore")

    title: str
    logline: str
    outline: List[OutlineItem] = Field(min_length=1, description="幕大纲（≥1 项）")
    locations: Optional[List[LocationItem]] = Field(
        default=None, description="去重地点清单（P-4 扩展，可空；场景 location 引用其 name）")
    scenes: List[SceneItem] = Field(min_length=1, description="场景列表（≥1 项）")


class ScriptBodyContent(BaseModel):
    """逐集正文结构化校验模型（P-4，正文生成落库前的结构门槛）。

    与 ScriptContent 的差异：outline 可选（正文 content 无三幕大纲）、
    locations 可选（去重地点清单）；scenes 必填（结构化场次 ≥1）。
    核心六字段语义与 ScriptContent 对齐，供分镜拆解等下游统一消费。

    Attributes:
        title: 单集标题（取剧集 title）。
        logline: 单集一句话梗概（取剧集 logline）。
        outline: 兼容字段（正文路径不使用，可空）。
        locations: 去重地点清单（可空）。
        scenes: 结构化场次列表（≥1 项）。
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    logline: str
    outline: Optional[List[OutlineItem]] = Field(default=None, description="幕大纲（正文路径可空）")
    locations: Optional[List[LocationItem]] = Field(
        default=None, description="去重地点清单（可空；场景 location 引用其 name）")
    scenes: List[SceneItem] = Field(min_length=1, description="场次列表（≥1 项）")


def validate_script_body_content(data: dict) -> Tuple[Optional[dict], Optional[str]]:
    """校验并规范化逐集正文结构化 dict（P-4；口径同 validate_script_content）。

    Args:
        data: 待校验的正文结构 dict（期望 title/logline/scenes，locations 可选）。

    Returns:
        Tuple[Optional[dict], Optional[str]]:
            (规范化剧本 dict, None) —— 校验通过（model_dump(exclude_none=True)，
                剔除未填的可选字段保持落库 JSON 紧凑）；
            (None, 错误摘要) —— 校验失败（摘要形如
                "scenes.0.duration_sec: Input should be greater than 0"，最多 3 条）。

    Raises:
        无：校验失败以错误摘要返回，不抛出。
    """
    try:
        model = ScriptBodyContent.model_validate(data)
        return model.model_dump(exclude_none=True), None
    except ValidationError as exc:
        parts: List[str] = []
        for err in exc.errors()[:3]:
            loc = ".".join(str(x) for x in err.get("loc", []))
            parts.append(f"{loc}: {err.get('msg', '校验失败')}")
        return None, ("正文结构校验失败：" + "；".join(parts)) if parts else "正文结构校验失败"


def validate_script_content(data: dict) -> Tuple[Optional[dict], Optional[str]]:
    """校验并规范化 LLM 生成的剧本 dict（PRD S-03）。

    Args:
        data: 待校验的剧本 dict（通常为 LLM 输出解析结果）。

    Returns:
        Tuple[Optional[dict], Optional[str]]:
            (规范化剧本 dict, None) —— 校验通过（返回 model_dump 规范化结构）；
            (None, 错误摘要) —— 校验失败（摘要形如
            "scenes.0.duration_sec: Input should be greater than 0"，最多 3 条）。

    Raises:
        无：校验失败以错误摘要返回，不抛出。
    """
    try:
        model = ScriptContent.model_validate(data)
        return model.model_dump(), None
    except ValidationError as exc:
        parts: List[str] = []
        for err in exc.errors()[:3]:
            loc = ".".join(str(x) for x in err.get("loc", []))
            parts.append(f"{loc}: {err.get('msg', '校验失败')}")
        return None, ("剧本结构校验失败：" + "；".join(parts)) if parts else "剧本结构校验失败"


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
        episode_id: 按集工作流的当前集 ID（可空=兼容旧调用不记名单）；
            传值时确认入池的资产名 union 追加到该集 enabled_assets 启用名单，
            资产管理页按集视图据此显示「该集已启用的资产」。
    """

    characters: List[ConfirmCharacterItem] = Field(default_factory=list)
    scenes: List[ConfirmSceneItem] = Field(default_factory=list)
    props: List[ConfirmPropItem] = Field(default_factory=list)
    episode_id: Optional[int] = Field(default=None, description="当前集 ID（按集启用名单落库）")


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


# ---------------------------------------------------------------------------
# 小说改编链路（P-1 Task 9 / P-7：章节解析与列表）
# ---------------------------------------------------------------------------
class NovelChapterOut(BaseModel):
    """小说章节输出条目（章节列表接口，不含完整正文）。

    Attributes:
        index: 章节序号（1 起，连续）。
        title: 章节标题（txt 取标题行 / 启发式「第N章」；epub 取 h1~h3 或文件名）。
        char_count: 章节正文字数。
        summary: 章节摘要（正文首 200 字）。
    """

    index: int = Field(description="章节序号（1 起）")
    title: str = Field(description="章节标题")
    char_count: int = Field(description="章节正文字数")
    summary: str = Field(description="章节摘要（正文首 200 字）")


class NovelParseOut(BaseModel):
    """小说章节解析响应（POST /script/input/novel/parse）。

    Attributes:
        project_id: 项目 ID。
        chapter_count: 解析出的章节数。
        chapters: 章节条目列表（index/title/char_count/summary）。
        phase: 项目当前剧本阶段（可能为 None=快速模式）。
    """

    project_id: int = Field(description="项目 ID")
    chapter_count: int = Field(description="章节数")
    chapters: List[NovelChapterOut] = Field(default_factory=list, description="章节条目列表")
    phase: Optional[str] = Field(default=None, description="项目当前剧本阶段")


class NovelChaptersOut(BaseModel):
    """小说章节列表响应（GET /script/input/novel/chapters）。

    Attributes:
        project_id: 项目 ID。
        chapter_count: 暂存章节数（未解析时为 0）。
        chapters: 章节条目列表。
        phase: 项目当前剧本阶段（可能为 None=快速模式）。
    """

    project_id: int = Field(description="项目 ID")
    chapter_count: int = Field(description="暂存章节数")
    chapters: List[NovelChapterOut] = Field(default_factory=list, description="章节条目列表")
    phase: Optional[str] = Field(default=None, description="项目当前剧本阶段")


# ---------------------------------------------------------------------------
# RD-08 影响面预览（GET /script/entities/impact）
# ---------------------------------------------------------------------------
class EntityImpactOut(BaseModel):
    """实体删除影响面预览响应（GET /script/entities/impact，RD-08）。

    与「确认后写入 version_snapshots.impact_scope 的事后快照」互补：本模型
    为**修改前只读即时预览**，仅返回统计口径下的引用情况，不写任何库表。

    Attributes:
        entity_type: 实体类型（character/scene/prop）。
        entity_id: 实体资产 ID。
        entity_name: 实体名称（用于影响面文案展示）。
        script_refs: 剧本内容中该实体名出现的次数（场次文本 + 对白）。
        storyboard_refs: 匹配到该实体的分镜条数（文本匹配 ∪ 结构化引用）。
        script_locations: 剧本引用所在场次标签（如「第1集 · 场景2」，上限 10 条）。
        storyboard_ids: 匹配到的分镜 ID 列表（上限 20 条）。
        summary: 中文影响面摘要文案。
    """

    entity_type: str = Field(description="实体类型：character/scene/prop")
    entity_id: int = Field(description="实体资产 ID")
    entity_name: str = Field(description="实体名称")
    script_refs: int = Field(description="剧本内容中实体名出现次数")
    storyboard_refs: int = Field(description="匹配到的分镜条数")
    script_locations: List[str] = Field(
        default_factory=list, description="剧本引用所在场次标签（上限 10 条）")
    storyboard_ids: List[int] = Field(
        default_factory=list, description="匹配到的分镜 ID（上限 20 条）")
    summary: str = Field(description="中文影响面摘要文案")
