"""分镜域 Pydantic Schema 模块（文件级说明）。

定义分镜拆解 / 列表 / 更新 / 重排 / 拆分 / 合并 / 告警 / 重生成 /
投递画布的出入参模型。
"""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class DecomposeRequest(BaseModel):
    """AI 拆解请求（P0-1：可空 body 向后兼容，前端快速模式勾选后传 skip_lock_check）。

    Attributes:
        skip_lock_check: 快速模式——跳过主要角色定妆照锁定门禁（默认 False 严格校验）。
    """

    skip_lock_check: bool = False


class StoryboardCharacterLinkOut(BaseModel):
    """分镜-角色关联输出。

    Attributes:
        character_id: 角色资产 ID。
        name: 角色名（联查回显）。
        variant_id: 该镜头使用的造型变体 ID。
        link_confidence: 实体链接置信度（0~1）。
    """

    character_id: int
    name: str
    variant_id: Optional[int] = None
    link_confidence: Optional[float] = None


class StoryboardPropLinkOut(BaseModel):
    """分镜-道具关联输出（方案 B）。

    Attributes:
        prop_id: 道具资产 ID。
        name: 道具名（联查回显）。
        link_confidence: 实体链接置信度（0~1）。
    """

    prop_id: int
    name: str
    link_confidence: Optional[float] = None


class StoryboardOut(BaseModel):
    """分镜输出模型。

    Attributes:
        id: 分镜 ID。
        sequence: 镜头序号（项目内 1..n）。
        shot_type: 景别。
        camera_movement: 运镜。
        duration: 预计时长（秒）。
        visual_description: 画面描述。
        dialogue: 对白/旁白（多行为 "角色名：台词"）。
        scene_id: 强绑场景资产 ID。
        scene_name: 场景名（联查回显）。
        prompt_locked: 提示词系统锁定层（RD-04）。
        prompt_editable: 提示词用户可编辑层。
        camera_angle: 机位与角度（方案 B，平视/俯拍/仰拍/斜角/过肩/顶摄）。
        lighting: 环境与光影描述（方案 B，分层提示词「环境与光影层」）。
        negative_prompt: 负面提示词（方案 B，分层提示词「负面排除层」）。
        audio: 声音结构化信息（方案 B：{sfx: 音效列表, emotion: 情绪标签}）。
        keyframe_url: 关键帧 URL。
        end_frame_url: 尾帧 URL（P-03/P-08：尾帧任务完成后由执行器回写）。
        video_url: 视频片段 URL。
        audio_url: 配音 URL（TTS 任务完成后回写，可用于行内试听播放）。
        status: 状态 pending/generating/done/failed。
        regen_count: 重生成次数。
        characters: 关联角色列表。
        props: 关联道具列表（方案 B）。
        stale: 引用资产是否已过期（P0-3：版本快照 vs 当前版本，true 前端标黄）。
        stale_reasons: 过期原因文案列表（tooltip 展示）。
    """

    id: int
    sequence: int
    episode_id: Optional[int] = None
    script_id: Optional[int] = None
    shot_type: Optional[str] = None
    camera_movement: Optional[str] = None
    duration: int = 5
    visual_description: Optional[str] = None
    dialogue: Optional[str] = None
    scene_id: Optional[int] = None
    scene_name: Optional[str] = None
    prompt_locked: Optional[str] = None
    prompt_editable: Optional[str] = None
    camera_angle: Optional[str] = None
    lighting: Optional[str] = None
    negative_prompt: Optional[str] = None
    audio: Optional[dict] = None
    keyframe_url: Optional[str] = None
    end_frame_url: Optional[str] = None
    video_url: Optional[str] = None
    audio_url: Optional[str] = None
    status: str = "pending"
    regen_count: int = 0
    characters: List[StoryboardCharacterLinkOut] = Field(default_factory=list)
    props: List[StoryboardPropLinkOut] = Field(default_factory=list)
    stale: bool = False
    stale_reasons: List[str] = Field(default_factory=list)


class StoryboardCharacterLinkIn(BaseModel):
    """分镜-角色关联更新条目（PUT 时整体替换）。

    Attributes:
        character_id: 角色资产 ID（必须属于本项目）。
        variant_id: 造型变体 ID（可空）。
        link_confidence: 实体链接置信度（可空）。
    """

    character_id: int
    variant_id: Optional[int] = None
    link_confidence: Optional[float] = Field(default=None, ge=0, le=1)


class StoryboardUpdateRequest(BaseModel):
    """分镜更新请求（全部字段可空，None 表示不修改）。

    Attributes:
        shot_type: 景别。
        camera_movement: 运镜。
        duration: 预计时长（秒）。
        visual_description: 画面描述。
        dialogue: 对白/旁白。
        prompt_editable: 提示词可编辑层。
        scene_id: 强绑场景资产 ID（须属于本项目）。
        character_links: 角色关联列表（提供则整体替换既有关联）。
        camera_angle: 机位与角度（方案 B，可空表示不修改）。
        lighting: 环境与光影（方案 B，可空表示不修改）。
        negative_prompt: 负面提示词（方案 B，可空表示不修改）。
        audio: 声音结构化信息（方案 B {sfx, emotion}，可空表示不修改）。
    """

    shot_type: Optional[str] = Field(default=None, max_length=20)
    camera_movement: Optional[str] = Field(default=None, max_length=20)
    duration: Optional[int] = Field(default=None, ge=1, le=600)
    visual_description: Optional[str] = None
    dialogue: Optional[str] = None
    prompt_editable: Optional[str] = None
    scene_id: Optional[int] = None
    character_links: Optional[List[StoryboardCharacterLinkIn]] = None
    camera_angle: Optional[str] = Field(default=None, max_length=20)
    lighting: Optional[str] = None
    negative_prompt: Optional[str] = None
    audio: Optional[dict] = None


class ReorderRequest(BaseModel):
    """分镜重排请求。

    Attributes:
        ordered_ids: 按目标顺序排列的分镜 ID 列表（须恰好覆盖项目全部分镜）。
    """

    ordered_ids: List[int] = Field(min_length=1)


class SplitRequest(BaseModel):
    """分镜拆分请求。

    Attributes:
        at_line: 对白拆分行号（1 起；前 at_line 行留在本镜，其余进新镜）。
    """

    at_line: int = Field(ge=1)


class MergeRequest(BaseModel):
    """分镜合并请求。

    Attributes:
        other_id: 被合并（删除）的另一分镜 ID，须与本镜相邻语义上可合并
            （本接口按同项目校验，不强制相邻）。
    """

    other_id: int


class StoryboardWarning(BaseModel):
    """单条分镜告警。

    Attributes:
        storyboard_id: 关联分镜 ID（全局性告警为空）。
        level: 告警级别 warning / error。
        message: 告警内容。
    """

    storyboard_id: Optional[int] = None
    level: str
    message: str


class StoryboardWarningsOut(BaseModel):
    """分镜告警汇总输出。

    Attributes:
        total_duration: 全部分镜预计时长合计（秒）。
        target_duration: 项目目标时长（秒，未设置为 None）。
        over_ratio: 总时长相对目标时长的偏差比例（无目标时为 None）。
        warnings: 告警列表。
    """

    total_duration: int
    target_duration: Optional[int] = None
    over_ratio: Optional[float] = None
    warnings: List[StoryboardWarning] = Field(default_factory=list)


class StoryboardRegenOut(BaseModel):
    """分镜重生成响应。

    Attributes:
        regen_count: 累计重生成次数。
        suggest_canvas: 重生成次数 >= 3 时建议进入画布精修（RD-25）。
    """

    regen_count: int
    suggest_canvas: bool


class DeliverCanvasRequest(BaseModel):
    """分镜投递画布请求。

    Attributes:
        storyboard_id: 来源分镜 ID（缺省取项目第一镜）。
    """

    storyboard_id: Optional[int] = None


class DeliverCanvasOut(BaseModel):
    """分镜投递画布响应。

    Attributes:
        canvas_id: 新建画布 ID。
    """

    canvas_id: int


class ContinuityCheckRequest(BaseModel):
    """连续性检查请求（SB-06）。

    Attributes:
        episode_id: 剧集过滤（可空；None = 项目全部分镜）。
    """

    episode_id: Optional[int] = None


class ContinuityItem(BaseModel):
    """单条连续性问题（SB-06）。

    Attributes:
        storyboard_id: 关联分镜 ID（取问题镜头，供前端定位）。
        type: 问题类型 scene_jump / outfit_conflict / prompt_missing。
        message: 问题描述。
        confidence: 置信级别 info（信息提示）/ suspect（疑似冲突）。
    """

    storyboard_id: Optional[int] = None
    type: str
    message: str
    confidence: str


class ContinuityCheckOut(BaseModel):
    """连续性检查汇总输出（SB-06）。

    Attributes:
        items: 问题列表（无问题时为空数组）。
        checked_pairs: 检查的相邻镜头对数量。
    """

    items: List[ContinuityItem] = Field(default_factory=list)
    checked_pairs: int


class AutoFitDurationRequest(BaseModel):
    """目标时长自动调整请求（SB-07）。

    Attributes:
        target_seconds: 目标总时长（秒，1~3600，越界由服务层返回 400）。
        episode_id: 剧集过滤（可空；None = 项目全部分镜）。
    """

    target_seconds: int
    episode_id: Optional[int] = None


class AutoFitShotDiff(BaseModel):
    """单镜时长调整前后对比（SB-07）。

    Attributes:
        id: 分镜 ID。
        before: 调整前时长（秒）。
        after: 调整后时长（秒）。
    """

    id: int
    before: int
    after: int


class AutoFitDurationOut(BaseModel):
    """目标时长自动调整摘要输出（SB-07）。

    Attributes:
        before_total: 调整前总时长（秒）。
        after_total: 调整后总时长（秒，受每镜保底 1 秒影响，≈ 目标值）。
        target_seconds: 目标总时长（秒）。
        changed: 实际发生变化的镜头数。
        per_shot: 每镜调整前后对比列表。
    """

    before_total: int
    after_total: int
    target_seconds: int
    changed: int
    per_shot: List[AutoFitShotDiff] = Field(default_factory=list)
