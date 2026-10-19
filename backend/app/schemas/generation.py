"""生成任务 Pydantic Schema 模块（文件级说明）。

定义生成域的请求/响应模型：关键帧（打样/全量）、视频片段、配音任务的
创建请求，任务详情/列表输出，以及 SSE 进度事件负载。
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class KeyframeCreateRequest(BaseModel):
    """关键帧生成请求。

    Attributes:
        phase: 生成阶段 trial（打样，单分镜）/ full（全量，项目全部分镜）。
        prompt_editable: 用户可编辑提示词补充（仅 trial 生效，拼接在锁定层之后）。
        episode_id: 按集工作流（S-06）：full 全量时限定该剧集的分镜
            （None=项目全部分镜，既有行为；trial 单分镜不受影响）。
    """

    phase: Literal["trial", "full"] = "trial"
    prompt_editable: Optional[str] = Field(default=None, max_length=2000)
    episode_id: Optional[int] = None


class VideoCreateRequest(BaseModel):
    """视频片段生成请求（P-12 视频参数配置）。

    Attributes:
        use_end_frame: 是否使用尾帧控制生成（须该分镜尾帧已生成，否则 400）。
        resolution: 输出分辨率档位（480p/720p/1080p，默认 720p）。
        fps: 帧率（24/30，默认 24）。
        motion_strength: 运动幅度（low/medium/high，默认 medium）。
    """

    use_end_frame: bool = False
    resolution: Literal["480p", "720p", "1080p"] = "720p"
    fps: Literal[24, 30] = 24
    motion_strength: Literal["low", "medium", "high"] = "medium"


class TtsCreateRequest(BaseModel):
    """配音（TTS）生成请求。

    Attributes:
        voice: 音色标识（male_01/male_02/female_01/female_02；None/缺省=按
            分镜绑定角色的音色设定自动路由）。
        speed: 语速倍率（0.5 ~ 2.0）。
        pitch: 音调倍率（0.5 ~ 2.0）。
    """

    voice: Optional[Literal["male_01", "male_02", "female_01", "female_02"]] = None
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch: float = Field(default=1.0, ge=0.5, le=2.0)


class TaskOut(BaseModel):
    """生成任务输出模型。

    Attributes:
        id: 任务 ID。
        project_id: 项目 ID。
        storyboard_id: 关联分镜 ID。
        type: 任务类型 image/video/tts/compose/llm/extract。
        phase: 阶段 trial/full/incremental。
        status: 状态 queued/running/done/failed/canceled。
        progress: 进度百分比。
        result_url: 结果资源地址。
        error_code: 错误码（content_violation/platform_error 等）。
        error: 错误信息（error_message 的对外别名）。
        retry_count: 已重试次数。
        cost_points: 冻结/消耗积分。
        estimated_seconds: 预估剩余秒数。
        created_at: 创建时间。
        placeholder: 是否为演示占位产物（D3：mock 兜底渠道产物打标，
            从任务 meta 提取；True 时对应任务已执行计费豁免）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: Optional[int]
    storyboard_id: Optional[int]
    type: str
    phase: str
    status: str
    progress: int
    result_url: Optional[str] = None
    error_code: Optional[str] = None
    error: Optional[str] = None
    retry_count: int = 0
    cost_points: int = 0
    estimated_seconds: Optional[int] = None
    created_at: Optional[datetime] = None
    placeholder: bool = False

    @classmethod
    def from_task(cls, task) -> "TaskOut":
        """从 ORM 任务对象构造输出模型（error 字段取 error_message）。

        Args:
            task: Task ORM 对象。

        Returns:
            TaskOut: 任务输出模型实例（placeholder 从 meta 提取，缺省 False）。

        Raises:
            无：字段均为简单映射，不产生异常路径。
        """
        return cls(
            id=task.id,
            project_id=task.project_id,
            storyboard_id=task.storyboard_id,
            type=task.type,
            phase=task.phase,
            status=task.status,
            progress=task.progress,
            result_url=task.result_url,
            error_code=task.error_code,
            error=task.error_message,
            retry_count=task.retry_count,
            cost_points=task.cost_points,
            estimated_seconds=task.estimated_seconds,
            created_at=task.created_at,
            placeholder=bool((task.meta or {}).get("placeholder")),
        )


class KeyframeCreateResponse(BaseModel):
    """关键帧任务创建响应。

    Attributes:
        tasks: 新建任务列表。
        total_cost: 本次冻结积分总额。
    """

    tasks: List[TaskOut]
    total_cost: int


class TaskProgressItem(BaseModel):
    """SSE 进度事件中的任务摘要项。

    Attributes:
        id: 任务 ID。
        type: 任务类型。
        status: 任务状态。
        progress: 进度百分比。
    """

    id: int
    type: str
    status: str
    progress: int


class TaskProgressEvent(BaseModel):
    """SSE progress 事件负载。

    Attributes:
        tasks: 项目任务摘要列表。
        running: 非终态（queued/running）任务数。
    """

    tasks: List[TaskProgressItem]
    running: int
