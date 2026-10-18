"""剧本创作流水线 Pydantic Schema 模块（文件级说明，P-1 范式级重构 Task 2）。

定义剧本创作四阶段（idea → characters → outline → body）接口的出入参模型，
按域分组：
- 角色域：候选/已确认角色档案、批量编辑、重新生成、确认入池；
- 大纲域：分集大纲生成、单集编辑、确认；
- 正文域：单集正文（文章体 + 结构化场景）保存与读取；
- 设定域（P2 故事设定产物）：故事设定生成/读取/编辑/重生成；
- 统一：阶段状态输出 PhaseOut。

本模块独立于 schemas/script.py（旧剧本域，面向快速模式 Script 行），
供 Task 3/4/5 的路由实现直接复用。Out 模型均开启 from_attributes，
可直接从 ORM 对象构造。
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 角色定位合法取值（与 characters.role_type 列 CHECK 约束一致）
RoleType = Literal["lead", "support", "villain", "extra"]


# ---------------------------------------------------------------------------
# 统一：阶段状态
# ---------------------------------------------------------------------------
class PhaseOut(BaseModel):
    """项目剧本创作阶段状态输出（各阶段接口通用回执）。

    Attributes:
        project_id: 项目 ID。
        script_phase: 当前剧本创作阶段 idea/characters/outline/body；
            NULL 表示快速模式。
    """

    project_id: int = Field(description="项目 ID")
    script_phase: Optional[str] = Field(
        default=None, description="剧本创作阶段：idea/characters/outline/body；NULL=快速模式")


# ---------------------------------------------------------------------------
# 角色域
# ---------------------------------------------------------------------------
class CharacterRelation(BaseModel):
    """人物关系条目（characters.relations JSONB 数组元素）。

    Attributes:
        target: 关系目标角色名。
        relation: 关系描述（如「青梅竹马」「宿敌」）。
    """

    target: str = Field(min_length=1, max_length=50, description="关系目标角色名")
    relation: str = Field(min_length=1, max_length=100, description="关系描述（如「青梅竹马」）")


class CharacterProfileOut(BaseModel):
    """角色档案输出（候选 planned / 已入池 confirmed 通用）。

    Attributes:
        id: 角色 ID（planned 候选未落库时为 None）。
        candidate_id: 候选临时 ID（config.script_characters 内的 8 位 hex
            串；仅 planned 候选携带，已入池角色为 None）。
        name: 角色名。
        role_type: 角色定位 lead/support/villain/extra。
        age: 年龄设定（弹性表述，如「约30岁」「少年」）。
        personality: 性格设定。
        appearance: 形象特征（发型/五官/体型/肤色等角色圣经视觉段）。
        outfit: 服装配饰完整拆解（上装/下装/鞋履/配饰/标志性物品，逐项列明）。
        style_note: 风格与画质特点（画风/光线/画质关键词）。
        motivation: 行为动机。
        arc: 人物弧光（角色成长线）。
        relations: 人物关系数组 [{target, relation}]。
        confirmed_at: 角色确认时间（候选为 None）。
        source: 来源标记：planned 候选 / confirmed 已入池。
    """

    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = Field(default=None, description="角色 ID（候选未落库为 None）")
    candidate_id: Optional[str] = Field(
        default=None, description="候选临时 ID（8 位 hex；仅 planned 候选携带）")
    name: str = Field(description="角色名")
    role_type: Optional[RoleType] = Field(default=None, description="角色定位：lead/support/villain/extra")
    age: Optional[str] = Field(default=None, description="年龄设定（弹性表述）")
    gender: Optional[str] = Field(default=None, description="角色性别（男性/女性/中性）")
    personality: Optional[str] = Field(default=None, description="性格设定")
    appearance: Optional[str] = Field(default=None, description="形象特征（角色圣经视觉段一）")
    outfit: Optional[str] = Field(default=None, description="服装配饰完整拆解（角色圣经视觉段二）")
    style_note: Optional[str] = Field(default=None, description="风格与画质特点（角色圣经视觉段三）")
    motivation: Optional[str] = Field(default=None, description="行为动机")
    arc: Optional[str] = Field(default=None, description="人物弧光（角色成长线）")
    relations: List[CharacterRelation] = Field(default_factory=list, description="人物关系数组")
    confirmed_at: Optional[datetime] = Field(default=None, description="角色确认时间（候选为 None）")
    source: Literal["planned", "confirmed"] = Field(description="来源：planned 候选 / confirmed 已入池")


class CharacterGenerateRequest(BaseModel):
    """角色档案批量生成请求（POST characters/generate 预留，Task 3）。

    Attributes:
        instruction: 可选补充指令（用户对角色班底的自然语言要求）。
        episode_count_hint: 可选目标集数提示（供 LLM 控制角色班底规模）。
    """

    instruction: Optional[str] = Field(
        default=None, max_length=2000, description="可选补充指令（对角色班底的要求）")
    episode_count_hint: Optional[int] = Field(
        default=None, ge=1, le=100, description="可选目标集数提示（1-100）")


class CharacterProfileUpsert(BaseModel):
    """角色档案批量编辑 upsert 条目（Task 3：按 id 或 name 匹配，存在则更新、不存在则新增）。

    匹配优先级：id（候选临时 ID）优先，id 未命中再按 name 匹配；
    两者均未命中则作为新增候选入列。

    Attributes:
        id: 候选临时 ID（优先匹配键；None 表示按 name 匹配或纯新增）。
        name: 角色名（必填；按 name 匹配的键，同时作为更新后的名字）。
        role_type: 角色定位（None 表示不修改）。
        age: 年龄设定（None 表示不修改）。
        personality: 性格设定（None 表示不修改）。
        appearance: 形象特征（None 表示不修改）。
        outfit: 服装配饰完整拆解（None 表示不修改）。
        style_note: 风格与画质特点（None 表示不修改）。
        motivation: 行为动机（None 表示不修改）。
        arc: 人物弧光（None 表示不修改）。
        relations: 人物关系数组（None 表示不修改）。
    """

    id: Optional[str] = Field(default=None, description="候选临时 ID（优先匹配键）")
    name: str = Field(min_length=1, max_length=50, description="角色名（name 匹配键/更新值）")
    role_type: Optional[RoleType] = Field(default=None, description="角色定位（None=不修改）")
    age: Optional[str] = Field(default=None, max_length=20, description="年龄设定（None=不修改）")
    gender: Optional[str] = Field(
        default=None, max_length=10,
        description="角色性别（男性/女性/中性；None=不修改）")
    personality: Optional[str] = Field(default=None, description="性格设定（None=不修改）")
    appearance: Optional[str] = Field(default=None, description="形象特征（None=不修改）")
    outfit: Optional[str] = Field(default=None, description="服装配饰完整拆解（None=不修改）")
    style_note: Optional[str] = Field(default=None, description="风格与画质特点（None=不修改）")
    motivation: Optional[str] = Field(default=None, description="行为动机（None=不修改）")
    arc: Optional[str] = Field(default=None, description="人物弧光（None=不修改）")
    relations: Optional[List[CharacterRelation]] = Field(
        default=None, description="人物关系数组（None=不修改）")


class CharacterBatchEditRequest(BaseModel):
    """角色档案批量编辑请求（PUT characters，Task 3）。

    仅作用于 project.config 中的候选列表；已确认入池的角色走既有资产
    PATCH 接口编辑，不在本接口范围内。

    Attributes:
        upsert: 按 id（优先）或 name 匹配的新增/更新条目列表。
        remove_ids: 需要移除的候选临时 ID 列表。
        remove_names: 需要移除的候选角色名列表。
    """

    upsert: List[CharacterProfileUpsert] = Field(
        default_factory=list, description="新增/更新条目（按 id 优先，其次 name 匹配）")
    remove_ids: List[str] = Field(
        default_factory=list, description="移除的候选临时 ID 列表")
    remove_names: List[str] = Field(
        default_factory=list, description="移除的候选角色名列表")


class CharacterRegenerateRequest(BaseModel):
    """角色档案重新生成请求（POST characters/regenerate 预留，Task 3）。

    Attributes:
        instruction: 可选补充指令（描述本轮重生成需要调整的方向）。
    """

    instruction: Optional[str] = Field(
        default=None, max_length=2000, description="可选补充指令（本轮重生成方向）")


class CharacterConfirmItem(BaseModel):
    """角色确认入池条目（CharacterConfirmRequest 元素）。

    Attributes:
        name: 角色名（必填；候选名或用户新改名）。
        role_type: 角色定位（None=沿用候选值）。
        age: 年龄设定（None=沿用候选值）。
        personality: 性格设定（None=沿用候选值）。
        appearance: 形象特征（None=沿用候选值）。
        outfit: 服装配饰完整拆解（None=沿用候选值）。
        style_note: 风格与画质特点（None=沿用候选值）。
        motivation: 行为动机（None=沿用候选值）。
        arc: 人物弧光（None=沿用候选值）。
        relations: 人物关系数组（None=沿用候选值）。
    """

    name: str = Field(min_length=1, max_length=50, description="确认入池的角色名")
    role_type: Optional[RoleType] = Field(default=None, description="角色定位（None=沿用候选）")
    age: Optional[str] = Field(default=None, max_length=20, description="年龄设定（None=沿用候选）")
    gender: Optional[str] = Field(
        default=None, max_length=10,
        description="角色性别（男性/女性/中性；None=沿用候选）")
    personality: Optional[str] = Field(default=None, description="性格设定（None=沿用候选）")
    appearance: Optional[str] = Field(default=None, description="形象特征（None=沿用候选）")
    outfit: Optional[str] = Field(default=None, description="服装配饰完整拆解（None=沿用候选）")
    style_note: Optional[str] = Field(default=None, description="风格与画质特点（None=沿用候选）")
    motivation: Optional[str] = Field(default=None, description="行为动机（None=沿用候选）")
    arc: Optional[str] = Field(default=None, description="人物弧光（None=沿用候选）")
    relations: Optional[List[CharacterRelation]] = Field(
        default=None, description="人物关系数组（None=沿用候选）")


class CharacterConfirmRequest(BaseModel):
    """角色设定确认入池请求（POST characters/confirm 预留，Task 3）。

    Attributes:
        characters: 确认入池的角色条目列表。
    """

    characters: List[CharacterConfirmItem] = Field(
        min_length=1, description="确认入池的角色条目列表")


class CharacterListOut(BaseModel):
    """角色列表输出（候选 + 已入池两组 + 阶段，GET characters 用）。

    Attributes:
        planned: 候选角色档案（config.script_characters 全量，未确认/已确认
            标记均在列）。
        confirmed: 已入池角色档案（characters 表中 confirmed_at 非空的行）。
        phase: 当前项目剧本创作阶段（idea/characters/outline/body；NULL=快速模式）。
    """

    planned: List[CharacterProfileOut] = Field(default_factory=list, description="候选角色档案")
    confirmed: List[CharacterProfileOut] = Field(default_factory=list, description="已入池角色档案")
    phase: Optional[str] = Field(default=None, description="当前剧本创作阶段")


class CharacterCandidatesOut(BaseModel):
    """候选角色列表输出（generate / 批量编辑接口通用回执）。

    Attributes:
        characters: 候选角色档案列表（source=planned，携带 candidate_id）。
        phase: 当前项目剧本创作阶段。
    """

    characters: List[CharacterProfileOut] = Field(
        default_factory=list, description="候选角色档案列表")
    phase: Optional[str] = Field(default=None, description="当前剧本创作阶段")


class CharacterRegenerateOut(BaseModel):
    """单角色重新生成结果输出（POST characters/{cid}/regenerate 用）。

    Attributes:
        character: 重新生成后的角色档案（候选或已入池角色）。
        phase: 当前项目剧本创作阶段。
    """

    character: CharacterProfileOut = Field(description="重新生成后的角色档案")
    phase: Optional[str] = Field(default=None, description="当前剧本创作阶段")


class CharacterConfirmOut(BaseModel):
    """角色确认结果输出。

    Attributes:
        confirmed_count: 本次入池角色数量。
        phase: 确认后的项目剧本阶段（正常推进到 outline；已处于后续阶段时幂等保持）。
    """

    confirmed_count: int = Field(description="本次入池角色数量")
    phase: Optional[str] = Field(description="确认后的剧本阶段（outline）")


# ---------------------------------------------------------------------------
# 大纲域
# ---------------------------------------------------------------------------
class EpisodePlanItem(BaseModel):
    """单集大纲条目（LLM 生成/用户编辑的单集结构，落库映射 episodes.outline）。

    Attributes:
        title: 单集标题。
        logline: 单集一句话梗概。
        core_event: 本集核心事件。
        characters: 本集出场角色名列表。
        emotion_curve: 情绪曲线（如「平静 → 冲突 → 释然」）。
        duration_sec: 目标时长（秒）。
    """

    title: str = Field(min_length=1, max_length=100, description="单集标题")
    logline: str = Field(min_length=1, max_length=200, description="单集一句话梗概")
    core_event: str = Field(min_length=1, description="本集核心事件")
    characters: List[str] = Field(default_factory=list, description="本集出场角色名列表")
    emotion_curve: str = Field(description="情绪曲线（如「平静 → 冲突 → 释然」）")
    duration_sec: Optional[int] = Field(default=None, ge=1, description="目标时长（秒）")


class EpisodePlanGenerateRequest(BaseModel):
    """分集大纲生成请求（POST episodes/plan，Task 4）。

    Attributes:
        target_episodes: 可选目标集数（1-100；不传时由服务端按项目目标时长/
            改编模式启发式决定：target_duration ≥600 秒或小说切分模式建议 3，
            否则 1）。
        from_chapters: 是否以小说章节切分为集（Task 9 小说改编预留，默认 False）。
    """

    target_episodes: Optional[int] = Field(
        default=None, ge=1, le=100, description="目标集数（1-100，不传由 LLM 决定）")
    from_chapters: bool = Field(
        default=False, description="是否按小说章节切分（Task 9 预留）")


class EpisodePlanOutItem(BaseModel):
    """单集大纲输出条目（含落库后的剧集 ID 与确认状态）。

    Attributes:
        episode_id: 剧集 ID。
        sequence: 剧集序号（项目内从 1 起）。
        title: 单集标题。
        logline: 单集一句话梗概。
        core_event: 本集核心事件。
        characters: 本集出场角色名列表。
        emotion_curve: 情绪曲线。
        duration_sec: 目标时长（秒）。
        confirmed_at: 单集确认时间（未确认为 None）。
    """

    model_config = ConfigDict(from_attributes=True)

    episode_id: int = Field(description="剧集 ID")
    sequence: int = Field(description="剧集序号（项目内从 1 起）")
    title: str = Field(description="单集标题")
    logline: Optional[str] = Field(default=None, description="单集一句话梗概")
    core_event: Optional[str] = Field(default=None, description="本集核心事件")
    characters: List[str] = Field(default_factory=list, description="本集出场角色名列表")
    emotion_curve: Optional[str] = Field(default=None, description="情绪曲线")
    duration_sec: Optional[int] = Field(default=None, description="目标时长（秒）")
    confirmed_at: Optional[datetime] = Field(default=None, description="单集确认时间（未确认为 None）")
    phase: Optional[str] = Field(
        default=None, description="项目当前剧本创作阶段（保存大纲自动推进 outline→body 时携带，其余接口可缺省）")


class EpisodePlanOut(BaseModel):
    """分集大纲整体输出。

    Attributes:
        episodes: 单集大纲条目列表（按 sequence 升序）。
        phase: 当前项目剧本创作阶段（idea/characters/outline/body；NULL=快速模式）。
    """

    episodes: List[EpisodePlanOutItem] = Field(default_factory=list, description="单集大纲列表")
    phase: Optional[str] = Field(default=None, description="当前剧本创作阶段")


class EpisodeOutlineEditRequest(BaseModel):
    """单集大纲编辑请求（PUT episodes/{id}/outline，Task 4）。

    全部字段可选：仅传入的字段会被更新（None=不修改）。

    Attributes:
        title: 单集标题。
        logline: 单集一句话梗概。
        core_event: 本集核心事件。
        characters: 本集出场角色名列表。
        emotion_curve: 情绪曲线。
        duration_sec: 目标时长（秒）。
        sequence: 剧集序号（调整单集顺序）。
    """

    title: Optional[str] = Field(default=None, min_length=1, max_length=100, description="单集标题（None=不修改）")
    logline: Optional[str] = Field(default=None, min_length=1, max_length=200, description="一句话梗概（None=不修改）")
    core_event: Optional[str] = Field(default=None, min_length=1, description="核心事件（None=不修改）")
    characters: Optional[List[str]] = Field(default=None, description="出场角色列表（None=不修改）")
    emotion_curve: Optional[str] = Field(default=None, description="情绪曲线（None=不修改）")
    duration_sec: Optional[int] = Field(default=None, ge=1, description="目标时长秒（None=不修改）")
    sequence: Optional[int] = Field(default=None, ge=1, description="剧集序号（None=不修改）")


class EpisodePlanConfirmOut(BaseModel):
    """分集大纲确认结果输出。

    Attributes:
        confirmed_count: 本次确认的单集数量。
        phase: 确认后的项目剧本阶段（body）。
    """

    confirmed_count: int = Field(description="本次确认的单集数量")
    phase: Optional[str] = Field(description="确认后的剧本阶段（body）")


class EpisodeOutlineRegenerateRequest(BaseModel):
    """单集大纲重生成请求（POST episodes/{eid}/outline/regenerate，P3 Task 4）。

    Attributes:
        instruction: 可选调整方向描述（如「冲突提前到开场」「加强悬念」；
            留空表示仅按现有大纲与前后集衔接重写）。
    """

    instruction: Optional[str] = Field(
        default=None, max_length=500, description="可选调整方向描述（留空=按现状重写）")


class EpisodeOutlineRegenerateOut(EpisodePlanOutItem):
    """单集大纲重生成输出（P3 Task 4）。

    继承 EpisodePlanOutItem 的单集大纲字段，额外携带正文存在提醒字段
    （该集已有正文时大纲变更将导致正文与大纲不一致，由用户决定是否重生成
    正文——后端不自动删除正文）。

    Attributes:
        body_exists: 该集是否已有正文（scripts 行存在时为 True，仅提醒不阻断）。
    """

    body_exists: bool = Field(
        default=False, description="该集是否已有正文（True 时提示按需重生成正文）")


class EpisodeOutlineAutofillOut(BaseModel):
    """单集大纲 AI 一键填充输出（建议值，不落库）。

    携带补全后的完整大纲字段（已填字段原样返回、缺失字段为 AI 补全建议），
    前端据此回填编辑表单，用户确认调整后走既有 PUT outline 保存。
    本接口不写库、不打版本快照（留痕发生在保存侧）。

    Attributes:
        sequence: 目标集序号。
        title / logline / core_event / characters / emotion_curve /
            duration_sec: 补全后的单集大纲六字段（建议值）。
        filled_fields: 本次被 AI 填充的字段名列表（对比填充前为空的字段，
            供前端 toast 提示「已补全：核心事件、情绪走向」等）。
    """

    sequence: int = Field(description="目标集序号")
    title: str = Field(description="单集标题（建议值）")
    logline: Optional[str] = Field(default=None, description="一句话梗概（建议值）")
    core_event: Optional[str] = Field(default=None, description="核心事件（建议值）")
    characters: List[str] = Field(default_factory=list, description="出场角色名（建议值）")
    emotion_curve: Optional[str] = Field(default=None, description="情绪走向（建议值）")
    duration_sec: Optional[int] = Field(default=None, description="单集时长秒（建议值）")
    filled_fields: List[str] = Field(
        default_factory=list, description="本次被 AI 填充的字段名（原为空的字段）")


# ---------------------------------------------------------------------------
# 正文域
# ---------------------------------------------------------------------------
class EpisodeBodySaveRequest(BaseModel):
    """单集正文保存请求（PUT episodes/{id}/body，Task 5）。

    raw_text 为文章体整段文本（阅读态）；content 为结构化场景数据
    （scenes 数组，与 Script.content 场景结构对齐，供后续分镜拆解消费）。
    服务端保存时做场次级 source 合并（merge_scene_sources）：内容未变的
    场次保留旧 source，改动/新增场次标 "user"。

    Attributes:
        raw_text: 文章体正文文本（必填）。
        content: 结构化正文（dict，scenes 场景数组；必填）。
        base_version: 可选乐观锁基准版本号（传入且与当前 version 不一致
            返回 409；未传保持旧行为兼容）。
    """

    raw_text: str = Field(min_length=1, description="文章体正文文本（必填）")
    content: Any = Field(
        description="结构化正文（dict，scenes 场景数组；保存时按场次合并 source）")
    base_version: Optional[int] = Field(
        default=None, ge=1, description="乐观锁基准版本号（不匹配返回 409）")


class EpisodeBodyOut(BaseModel):
    """单集正文输出。

    Attributes:
        episode_id: 剧集 ID。
        script_id: 关联剧本行 ID（scripts 表）。
        raw_text: 文章体正文文本。
        content: 结构化正文（scenes 场景数组）。
        version: 正文版本号。
        updated_at: 最近更新时间。
    """

    model_config = ConfigDict(from_attributes=True)

    episode_id: int = Field(description="剧集 ID")
    script_id: int = Field(description="关联剧本行 ID")
    raw_text: Optional[str] = Field(default=None, description="文章体正文文本")
    content: Optional[Any] = Field(default=None, description="结构化正文（scenes 场景数组）")
    version: int = Field(description="正文版本号")
    updated_at: Optional[datetime] = Field(default=None, description="最近更新时间")


# ---------------------------------------------------------------------------
# 剧本子图断线恢复（P-5，Task 6）
# ---------------------------------------------------------------------------
class PipelineInterruptOut(BaseModel):
    """子图挂起的 interrupt 信息（断线恢复回放用）。

    Attributes:
        type: interrupt 类型：confirm_characters/confirm_episodes/confirm_body。
        payload: interrupt 负载（候选数据摘要：message/count/characters 或
            episodes 列表等，结构随类型不同）。
    """

    type: str = Field(
        description="interrupt 类型：confirm_characters/confirm_episodes/confirm_body")
    payload: Optional[Any] = Field(default=None, description="interrupt 负载（候选数据摘要）")


class PipelineStateOut(BaseModel):
    """剧本子图断线恢复状态输出（GET /script/pipeline/state）。

    Attributes:
        thread_id: 子图检查点线程 ID。
        project_id: 项目 ID。
        phase: 当前项目剧本创作阶段（DB 侧）。
        next_nodes: 断点处待继续执行的节点名（无检查点为空列表）。
        pending_interrupt: 挂起的 interrupt（无检查点/无挂起为 None）。
        summary: 进度摘要（character_count 候选角色数 / episode_count 大纲
            集数 / body_count 本轮已生成集数）。
    """

    thread_id: str = Field(description="子图检查点线程 ID")
    project_id: int = Field(description="项目 ID")
    phase: Optional[str] = Field(
        default=None, description="当前剧本创作阶段：idea/characters/outline/body；NULL=快速模式")
    next_nodes: List[str] = Field(
        default_factory=list, description="断点处待继续执行的节点名")
    pending_interrupt: Optional[PipelineInterruptOut] = Field(
        default=None, description="挂起的 interrupt（无则为 null）")
    summary: Dict[str, int] = Field(
        default_factory=dict, description="进度摘要（候选数/集数/已生成集数）")


# ---------------------------------------------------------------------------
# 剧本子图 REST 驱动入口（P-5，Task 6）
# ---------------------------------------------------------------------------
class ScriptPipelineRunRequest(BaseModel):
    """剧本子图启动/续跑请求（POST /script/pipeline/run/stream）。

    Attributes:
        instruction: 可选补充指令（作为入口指令透传给生成节点，如角色班底
            额外要求）。
        auto_confirm: 是否一键串跑（True 时三处确认闸口自动放行，不在
            pending_confirm 处暂停）。
    """

    instruction: Optional[str] = Field(
        default=None, max_length=2000, description="可选补充指令（透传给生成节点）")
    auto_confirm: bool = Field(
        default=False, description="是否一键串跑（跳过三处人工确认闸口）")


class ScriptPipelineResumeRequest(BaseModel):
    """剧本子图确认/退回请求（POST /script/pipeline/resume）。

    decision 为 approved 时按当前候选落库并推进阶段；rejected 时携带
    reason 退回上游节点重新生成。edited_payload 用于提交用户在确认卡片中
    的人工编辑稿（如 {"characters": [...]} / {"episodes": [...]}），确认
    通过时以编辑稿落库（人工修稿优先）。

    Attributes:
        decision: 决策（approved=确认通过 / rejected=退回重生）。
        reason: 可选退回意见（rejected 时作为重生指令/播报文案）。
        edited_payload: 可选人工编辑稿（原样并入 resume 值透传给子图）。
    """

    decision: Literal["approved", "rejected"] = Field(
        default="approved", description="决策：approved=确认 / rejected=退回重生")
    reason: Optional[str] = Field(
        default=None, max_length=2000, description="可选退回意见（rejected 时使用）")
    edited_payload: Optional[Dict[str, Any]] = Field(
        default=None, description="可选人工编辑稿（原样透传给子图，如 characters/episodes）")


class ScriptPipelineStatusOut(BaseModel):
    """剧本子图断线重连状态输出（GET /script/pipeline/status）。

    供前端（Task 11）刷新/重连后恢复进度展示：当前阶段、断点处待执行
    阶段、是否存在待人工确认的闸口，以及角色/大纲/正文摘要。

    Attributes:
        thread_id: 子图检查点线程 ID。
        project_id: 项目 ID。
        script_phase: 项目当前剧本创作阶段（DB 侧；NULL=快速模式）。
        current_stage: 断点处待继续执行的节点名（多个时取首个；无断点为空串）。
        next_nodes: 断点处全部待继续执行的节点名。
        pending_stage: 挂起确认闸口对应的剧本阶段（characters/outline/body）；
            无挂起为空字符串。
        has_pending_confirm: 是否存在待人工确认的闸口。
        pending_confirm: 挂起 interrupt 的完整信息（type/payload；无则为 null）。
        summary: 进度摘要（character_count/episode_count/body_count）。
    """

    thread_id: str = Field(description="子图检查点线程 ID")
    project_id: int = Field(description="项目 ID")
    script_phase: Optional[str] = Field(
        default=None, description="项目当前阶段：idea/characters/outline/body；NULL=快速模式")
    current_stage: str = Field(default="", description="断点处待执行的节点名（无断点为空串）")
    next_nodes: List[str] = Field(
        default_factory=list, description="断点处全部待执行节点名")
    pending_stage: str = Field(
        default="", description="挂起确认闸口对应的剧本阶段（characters/outline/body）")
    has_pending_confirm: bool = Field(default=False, description="是否有待人工确认的闸口")
    pending_confirm: Optional[PipelineInterruptOut] = Field(
        default=None, description="挂起 interrupt 的完整信息（无则为 null）")
    summary: Dict[str, int] = Field(
        default_factory=dict, description="进度摘要（角色/大纲/正文计数）")


# ---------------------------------------------------------------------------
# AI 优化（P-6，Task 7）
# ---------------------------------------------------------------------------
# 优化粒度合法取值：global 全篇 / episode 整集（同 global）/ segment 单场次 /
# field 单字段
OptimizeGrain = Literal["global", "episode", "segment", "field"]


class EpisodeOptimizeRequest(BaseModel):
    """单集 AI 优化预览请求（POST episodes/{eid}/optimize，Task 7）。

    只读预览：服务端据此调用优化 Agent 产出片段化 changes（不落库）。

    Attributes:
        grain: 优化粒度 global/episode/segment/field。
        target: 粒度目标：segment 传场次序号（scene_no，int 或数字串）；
            field 传字段路径（如 "scenes[1].plot"）；global/episode 可省略。
        instruction: 可选补充指令（对优化方向的额外要求）。
    """

    grain: OptimizeGrain = Field(description="优化粒度：global/episode/segment/field")
    target: Optional[Union[int, str]] = Field(
        default=None, description="粒度目标：segment=场次序号；field=字段路径")
    instruction: Optional[str] = Field(
        default=None, max_length=2000, description="可选补充指令（优化方向）")


class OptimizeDiffLine(BaseModel):
    """diff 片段的单行对比单元（行级/句级）。

    Attributes:
        op: 操作类型 equal（未变）/ delete（删除）/ insert（新增）。
        text: 该单元的文本内容。
    """

    op: Literal["equal", "delete", "insert"] = Field(description="操作：equal/delete/insert")
    text: str = Field(description="单元文本")


class OptimizeChangeOut(BaseModel):
    """单条优化片段（片段化 diff，禁止整篇覆盖）。

    Attributes:
        id: 片段稳定 ID（optimize 预览与 apply 提交双侧一致，供接受/拒绝匹配）。
        path: 字段路径（如 "scenes[0].plot"）。
        kind: 片段类型（当前固定 field）。
        original: 变更前原文（预览时即为当前库值）。
        proposed: 优化后建议值。
        reason: 优化理由。
        diff_lines: 行级 diff 单元序列（op + text）。
    """

    id: str = Field(description="片段稳定 ID（apply 匹配用）")
    path: str = Field(description="字段路径（如 scenes[0].plot）")
    kind: str = Field(default="field", description="片段类型（当前固定 field）")
    original: Optional[str] = Field(default=None, description="变更前原文")
    proposed: Optional[str] = Field(default=None, description="优化后建议值")
    reason: str = Field(default="", description="优化理由")
    diff_lines: List[OptimizeDiffLine] = Field(
        default_factory=list, description="行级/句级 diff 单元序列")


class EpisodeOptimizeOut(BaseModel):
    """单集 AI 优化预览输出（不落库）。

    Attributes:
        episode_id: 剧集 ID。
        grain: 本次优化粒度。
        changes: 片段化 diff 列表。
        count: 片段数量（非法路径已在服务端丢弃，故可能少于 LLM 输出条数）。
    """

    episode_id: int = Field(description="剧集 ID")
    grain: str = Field(description="优化粒度")
    changes: List[OptimizeChangeOut] = Field(
        default_factory=list, description="片段化 diff 列表")
    count: int = Field(description="片段数量")


class OptimizeApplyChangeItem(BaseModel):
    """apply 提交的单条片段（前端回传预览时的片段）。

    服务端以 path 重新校验合法性、以 original 与当前库值比对（防并发覆盖）。

    Attributes:
        id: 片段稳定 ID（须与 accept_ids 匹配）。
        path: 字段路径。
        original: 预览时的原文（用于与当前库值比对）。
        proposed: 待应用的建议值。
    """

    id: str = Field(description="片段稳定 ID")
    path: str = Field(description="字段路径")
    original: Optional[str] = Field(default=None, description="预览时原文（并发比对用）")
    proposed: str = Field(description="待应用的建议值")


class EpisodeOptimizeApplyRequest(BaseModel):
    """单集 AI 优化应用请求（POST episodes/{eid}/optimize/apply，Task 7）。

    Attributes:
        accept_ids: 接受应用的片段 ID 列表（未选中的片段原样保留）。
        changes: 预览时的片段列表（服务端据此匹配 id→变更映射）。
        base_version: 可选乐观锁基准版本号（不匹配返回 409）。
    """

    accept_ids: List[str] = Field(
        default_factory=list, description="接受应用的片段 ID 列表")
    changes: List[OptimizeApplyChangeItem] = Field(
        default_factory=list, description="预览时的片段列表（id 匹配用）")
    base_version: Optional[int] = Field(
        default=None, ge=1, description="乐观锁基准版本号（不匹配返回 409）")


class EpisodeOptimizeApplyOut(BaseModel):
    """单集 AI 优化应用结果输出。

    Attributes:
        applied: 实际应用片段数。
        skipped: 跳过片段数（path 非法 / 原文与当前库值不符 / 越界）。
        version: 应用后的剧本版本号（未应用时不递增）。
        content: 应用后的结构化 content（字段级包装 + scenes 元素级 source）。
    """

    applied: int = Field(description="实际应用片段数")
    skipped: int = Field(description="跳过片段数")
    version: int = Field(description="应用后的剧本版本号")
    content: Optional[Any] = Field(default=None, description="应用后的结构化 content")


# ---------------------------------------------------------------------------
# 设定域（P2 故事设定产物，redesign-script-workspace Task 2）
# ---------------------------------------------------------------------------
class StorySettingOut(BaseModel):
    """故事设定内容块输出（projects.story_setting JSONB 的结构化形态）。

    Attributes:
        logline: 一句话故事。
        core_conflict: 核心冲突描述。
        worldview: 世界观设定。
        source: 来源标记：ai 模型生成 / user 用户编辑。
        updated_at: 最近更新时间（ISO 8601 字符串，取自 JSONB 内的
            updated_at 字段原样透传）。
    """

    logline: Optional[str] = Field(default=None, description="一句话故事")
    core_conflict: Optional[str] = Field(default=None, description="核心冲突描述")
    worldview: Optional[str] = Field(default=None, description="世界观设定")
    source: Optional[Literal["ai", "user"]] = Field(
        default=None, description="来源：ai 模型生成 / user 用户编辑")
    updated_at: Optional[str] = Field(
        default=None, description="最近更新时间（ISO 8601 字符串）")


class StorySettingEditRequest(BaseModel):
    """故事设定编辑请求（PUT /script/setting，P2）。

    仅接受 logline/core_conflict/worldview 三个设定字段（合并保存语义：
    None=不修改该字段），source 由服务端强制置 user；至少传入一个字段。

    Attributes:
        logline: 一句话故事（None=不修改）。
        core_conflict: 核心冲突描述（None=不修改）。
        worldview: 世界观设定（None=不修改）。
    """

    logline: Optional[str] = Field(
        default=None, min_length=1, max_length=200, description="一句话故事（None=不修改）")
    core_conflict: Optional[str] = Field(
        default=None, min_length=1, max_length=1000, description="核心冲突描述（None=不修改）")
    worldview: Optional[str] = Field(
        default=None, min_length=1, max_length=2000, description="世界观设定（None=不修改）")

    @model_validator(mode="after")
    def _require_at_least_one(self) -> "StorySettingEditRequest":
        """校验至少传入一个待编辑字段（全 None 的编辑请求无意义）。

        Returns:
            StorySettingEditRequest: 校验通过的原模型实例。

        Raises:
            ValueError: 三个字段均为 None（None 时 FastAPI 转 422 响应）。
        """
        if self.logline is None and self.core_conflict is None and self.worldview is None:
            raise ValueError("至少传入一个待编辑字段（logline/core_conflict/worldview）")
        return self


class StorySettingRegenerateRequest(BaseModel):
    """故事设定重生成请求（POST /script/setting/regenerate，P2）。

    Attributes:
        instruction: 可选补充指令（描述本轮重生成的调整方向，如「冲突更强」）。
    """

    instruction: Optional[str] = Field(
        default=None, max_length=2000, description="可选补充指令（本轮重生成方向）")


class StorySettingEditOut(BaseModel):
    """故事设定编辑结果输出（PUT /script/setting 用）。

    Attributes:
        setting: 保存后的故事设定内容块（source=user）。
        phase: 当前项目剧本创作阶段（编辑不推进阶段，原样返回）。
    """

    setting: StorySettingOut = Field(description="保存后的故事设定内容块")
    phase: Optional[str] = Field(default=None, description="当前剧本创作阶段（编辑不改变）")


class StorySettingGenerateOut(BaseModel):
    """故事设定生成/重生成结果输出（generate / regenerate 通用回执）。

    Attributes:
        setting: 生成后的故事设定内容块（source=ai）。
        characters: 人物候选列表（config.script_characters 现状：generate 为
            全新候选；regenerate 为「已确认候选保留 + 新候选」合并结果）。
        phase: 生成后的项目剧本创作阶段（NULL/idea 起点推进到 characters，
            其余阶段不回退）。
    """

    setting: StorySettingOut = Field(description="生成后的故事设定内容块")
    characters: List[CharacterProfileOut] = Field(
        default_factory=list, description="人物候选列表（与角色域候选同构）")
    phase: Optional[str] = Field(default=None, description="生成后的剧本创作阶段")


class StorySettingDetailOut(BaseModel):
    """故事设定详情输出（GET /script/setting 用）。

    未生成过设定时 setting 为 null（不 404，前端渲染空态）；人物候选与
    已确认角色双组复用角色域 list 口径。

    Attributes:
        setting: 故事设定内容块（未生成时 None）。
        planned: 人物候选档案（config.script_characters 全量）。
        confirmed: 已入池角色档案（characters 表 confirmed_at 非空的行）。
        phase: 当前项目剧本创作阶段。
    """

    setting: Optional[StorySettingOut] = Field(
        default=None, description="故事设定内容块（未生成时 null）")
    planned: List[CharacterProfileOut] = Field(default_factory=list, description="人物候选档案")
    confirmed: List[CharacterProfileOut] = Field(default_factory=list, description="已入池角色档案")
    phase: Optional[str] = Field(default=None, description="当前剧本创作阶段")


# ---------------------------------------------------------------------------
# 对话式改写通道（P4，redesign-script-workspace Task 5）
# ---------------------------------------------------------------------------
# assist 作用域合法取值：setting 故事设定三段 / episode_outline 单集大纲 /
# episode_body 单集正文（复用 optimize 通道）
AssistScope = Literal["setting", "episode_outline", "episode_body"]


class AssistPreviewRequest(BaseModel):
    """对话式改写预览请求（POST /script/assist，P4 Task 5）。

    只读预览：服务端据此调用改写 Agent 产出片段化 changes（不落库）。
    scope/target 语义：
    - setting：target 忽略（可传 "all" 或不传），作用对象为 story_setting
      三段文本（logline/core_conflict/worldview）；
    - episode_outline：target 必填为剧集 ID（int），作用对象为该集
      title/logline 列与 outline JSONB 文本字段；
    - episode_body：target 必填为剧集 ID（int），直接复用 optimize 预览
      路径（grain=global 全篇优化口径），保证行为一致不重造。

    Attributes:
        scope: 改写作用域 setting/episode_outline/episode_body。
        target: 作用目标（setting 忽略；episode 两作用域必须为剧集 ID 整数）。
        instruction: 用户自然语言改写指令（必填，1~500 字）。
    """

    scope: AssistScope = Field(description="改写作用域：setting/episode_outline/episode_body")
    target: Optional[Union[int, str]] = Field(
        default=None, description="作用目标：setting 可省略（填 all 亦可）；episode 两作用域为剧集 ID")
    instruction: str = Field(
        min_length=1, max_length=500, description="用户改写指令（必填，1~500 字）")

    @model_validator(mode="after")
    def _validate_target(self) -> "AssistPreviewRequest":
        """校验 target 与 scope 的匹配（episode 两作用域必须传剧集 ID 整数）。

        Returns:
            AssistPreviewRequest: 校验通过的原模型实例。

        Raises:
            ValueError: scope 为 episode_outline/episode_body 但 target 缺失
                或非整数（None 时 FastAPI 转 422 响应）。
        """
        if self.scope != "setting":
            if isinstance(self.target, bool) or not isinstance(self.target, int):
                raise ValueError("该作用域的 target 必须为剧集 ID（整数）")
        return self


class AssistPreviewOut(BaseModel):
    """对话式改写预览输出（不落库，POST /script/assist 回执）。

    片段结构复用 optimize 现有 OptimizeChangeOut（id 为 sha1(path+original+
    proposed) 前 12 位，与 optimize 片段 ID 口径一致）。

    Attributes:
        scope: 本次改写作用域。
        target: 规范化后的目标标识（setting="all"；episode 两作用域为剧集
            ID 字符串）。
        changes: 片段化 diff 列表（id/path/original/proposed/reason/diff_lines）。
        count: 片段数量（非法 path 已在服务端丢弃，可能少于 LLM 输出条数）。
        summary: LLM 对本轮改动的一句话概括（可空串）。
    """

    scope: AssistScope = Field(description="改写作用域")
    target: Optional[str] = Field(default=None, description="规范化目标标识（all 或剧集 ID）")
    changes: List[OptimizeChangeOut] = Field(
        default_factory=list, description="片段化 diff 列表（复用 optimize 片段模型）")
    count: int = Field(description="片段数量")
    summary: str = Field(default="", description="LLM 本轮改动概括（可空）")


class AssistApplyRequest(BaseModel):
    """对话式改写采纳请求（POST /script/assist/apply，P4 Task 5）。

    accepts 为被采纳的片段数组（对齐 optimize/apply 的片段回传形态：
    id/path/original/proposed），服务端以 path 白名单（按 scope）与
    original 一致性双重校验，不一致计 skipped 防并发覆盖。

    Attributes:
        scope: 改写作用域（与预览一致）。
        target: 作用目标（语义同 AssistPreviewRequest）。
        accepts: 采纳的片段列表（元素含 id/path/original/proposed）。
        base_version: 可选乐观锁基准版本号（仅 episode_body 作用域消费，
            与当前 scripts.version 不一致返回 409）。
    """

    scope: AssistScope = Field(description="改写作用域：setting/episode_outline/episode_body")
    target: Optional[Union[int, str]] = Field(
        default=None, description="作用目标：setting 可省略（填 all 亦可）；episode 两作用域为剧集 ID")
    accepts: List[OptimizeApplyChangeItem] = Field(
        default_factory=list, description="采纳的片段列表（id/path/original/proposed）")
    base_version: Optional[int] = Field(
        default=None, ge=1, description="乐观锁基准版本号（仅 episode_body 消费，不匹配 409）")

    @model_validator(mode="after")
    def _validate_target(self) -> "AssistApplyRequest":
        """校验 target 与 scope 的匹配（episode 两作用域必须传剧集 ID 整数）。

        Returns:
            AssistApplyRequest: 校验通过的原模型实例。

        Raises:
            ValueError: scope 为 episode_outline/episode_body 但 target 缺失
                或非整数（None 时 FastAPI 转 422 响应）。
        """
        if self.scope != "setting":
            if isinstance(self.target, bool) or not isinstance(self.target, int):
                raise ValueError("该作用域的 target 必须为剧集 ID（整数）")
        return self


class AssistApplyOut(BaseModel):
    """对话式改写采纳结果输出（POST /script/assist/apply 回执）。

    乐观锁/版本语义对齐 optimize/apply：applied=0 不递增版本、不落库、
    不写快照；applied>0 写一条 stage=assist_applied 版本快照。

    Attributes:
        applied: 实际应用片段数。
        skipped: 跳过片段数（path 不在作用域白名单 / 原文与当前库值不符 /
            proposed 为空）。
        snapshot_id: 本次采纳写入的版本快照 ID（applied=0 时为 None）。
        version: 应用后的剧本版本号（仅 episode_body 作用域携带；其余 None）。
        phase: 当前项目剧本创作阶段（assist 采纳不改变阶段，原样返回）。
        content: 应用后的结构化 content（仅 episode_body 作用域携带，字段级
            包装 + scenes 元素级 source；其余 None）。
    """

    applied: int = Field(description="实际应用片段数")
    skipped: int = Field(description="跳过片段数")
    snapshot_id: Optional[int] = Field(
        default=None, description="assist_applied 版本快照 ID（applied=0 时为 null）")
    version: Optional[int] = Field(
        default=None, description="应用后的剧本版本号（仅 episode_body 携带）")
    phase: Optional[str] = Field(default=None, description="当前剧本创作阶段（不变）")
    content: Optional[Any] = Field(
        default=None, description="应用后的结构化 content（仅 episode_body 携带）")


# ---------------------------------------------------------------------------
# 引用优化（quote_rewrite）：工作台选中文本「添加对话中」→ 引用 + 优化方向 →
# 可复制的优化文本（纯文本建议，不落库不写快照）
# ---------------------------------------------------------------------------
QuoteRewriteScope = Literal[
    "setting", "characters", "episode_outline", "episode_body"]


class QuoteRewriteRequest(BaseModel):
    """选中文本优化请求（POST /script/assist/quote-rewrite）。

    用户在工作台选中一段文本（textarea/input 选区）点「添加对话中」，引用
    进入助手面板后补充优化方向发送。本接口依据引用原文与方向生成优化文本
    建议（纯文本），由用户复制粘贴回对应位置，**不落库**。

    Attributes:
        scope: 选中文本的作用域（故事设定/角色设定/分集大纲/剧本正文，
            仅用于 LLM 语境标注，不校验阶段）。
        target_episode_id: 目标剧集 ID（大纲/正文作用域下用于「第 N 集」
            语境标注，可选）。
        quote_text: 选中的原文（1~8000 字）。
        instruction: 优化方向描述（1~1000 字）。
    """

    scope: QuoteRewriteScope = Field(
        description="选中文本作用域：setting/characters/episode_outline/episode_body")
    target_episode_id: Optional[int] = Field(
        default=None, ge=1, description="目标剧集 ID（大纲/正文作用域语境标注用，可选）")
    quote_text: str = Field(
        min_length=1, max_length=8000, description="选中的原文（1~8000 字）")
    instruction: str = Field(
        min_length=1, max_length=1000, description="优化方向描述（1~1000 字）")


class QuoteRewriteOut(BaseModel):
    """选中文本优化输出（不落库，POST /script/assist/quote-rewrite 回执）。

    Attributes:
        optimized_text: 优化后的文本（纯文本，前端展示并提供复制）。
    """

    optimized_text: str = Field(description="优化后的文本（纯文本建议，不落库）")
