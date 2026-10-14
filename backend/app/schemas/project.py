"""项目 Pydantic Schema 模块（文件级说明）。

定义项目创建 / 更新 / 详情 / 版本快照的出入参模型。
"""

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# 项目合法状态机取值（与 projects 表 CHECK 约束一致）
PROJECT_STATUSES = ("draft", "scripting", "asset", "storyboard", "producing", "review", "published")

# 忠实度合法取值（RD-01）
FIDELITY_VALUES = ("free", "outline", "faithful")

# 剧集合法状态取值（S-06；episodes 表无 CHECK 约束，接口层白名单校验，
# 取值对齐项目状态机子集，便于按剧集推进生产流程）
EPISODE_STATUSES = ("draft", "scripting", "storyboard", "producing", "published")


class ProjectCreateRequest(BaseModel):
    """项目创建请求。

    Attributes:
        title: 项目标题（1~100 字符）。
        genre: 体裁（可空）。
        style: 视觉风格（可空）。
        fidelity: 忠实度档位 free/outline/faithful（可空）。
        aspect_ratio: 输出比例，默认 "9:16"。
        target_duration: 目标时长（秒，可空）。
        config: 项目级模型配置（JSONB，可空）。
    """

    title: str = Field(min_length=1, max_length=100)
    genre: Optional[str] = Field(default=None, max_length=20)
    style: Optional[str] = Field(default=None, max_length=20)
    fidelity: Optional[str] = None
    aspect_ratio: str = Field(default="9:16", max_length=10)
    target_duration: Optional[int] = Field(default=None, ge=1)
    config: Optional[Any] = None


class ProjectUpdateRequest(BaseModel):
    """项目更新请求（全部字段可空，None 表示不修改）。

    Attributes:
        title: 项目标题。
        genre: 体裁。
        style: 视觉风格。
        fidelity: 忠实度档位。
        aspect_ratio: 输出比例。
        target_duration: 目标时长（秒）。
        config: 项目级模型配置。
        status: 项目状态（仅允许合法状态机值，非法返回 400）。
    """

    title: Optional[str] = Field(default=None, min_length=1, max_length=100)
    genre: Optional[str] = Field(default=None, max_length=20)
    style: Optional[str] = Field(default=None, max_length=20)
    fidelity: Optional[str] = None
    aspect_ratio: Optional[str] = Field(default=None, max_length=10)
    target_duration: Optional[int] = Field(default=None, ge=1)
    config: Optional[Any] = None
    status: Optional[str] = None


class ProjectOut(BaseModel):
    """项目输出模型。

    Attributes:
        id: 项目 ID。
        title: 项目标题。
        genre: 体裁。
        style: 视觉风格。
        fidelity: 忠实度档位。
        status: 项目状态。
        aspect_ratio: 输出比例。
        target_duration: 目标时长（秒）。
        config: 项目级模型配置。
        cover_url: 封面 URL。
        created_at: 创建时间（ISO 字符串）。
        updated_at: 更新时间（ISO 字符串）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    genre: Optional[str] = None
    style: Optional[str] = None
    fidelity: Optional[str] = None
    status: str
    aspect_ratio: str
    target_duration: Optional[int] = None
    config: Optional[Any] = None
    cover_url: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class EpisodeOut(BaseModel):
    """剧集完整输出模型（S-06 剧集 CRUD 接口）。

    Attributes:
        id: 剧集 ID。
        project_id: 项目 ID。
        title: 剧集标题。
        sequence: 剧集序号。
        script_snapshot: 该剧集剧本快照（JSONB，可空）。
        status: 剧集状态。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    title: str
    sequence: int
    script_snapshot: Optional[Any] = None
    status: str


class EpisodeSummaryOut(BaseModel):
    """剧集摘要输出模型（项目详情内嵌，S-06）。

    Attributes:
        id: 剧集 ID。
        title: 剧集标题。
        sequence: 剧集序号。
        status: 剧集状态。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    sequence: int
    status: str


class EpisodeCreateRequest(BaseModel):
    """剧集创建请求（S-06，POST /api/projects/{project_id}/episodes）。

    Attributes:
        title: 剧集标题（1~100 字符）。
    """

    title: str = Field(min_length=1, max_length=100)


class EpisodeUpdateRequest(BaseModel):
    """剧集更新请求（S-06，PATCH /api/projects/{project_id}/episodes/{episode_id}，
    全部字段可空，None 表示不修改）。

    Attributes:
        title: 剧集标题。
        status: 剧集状态（仅允许 EPISODE_STATUSES 白名单值，非法返回 400）。
    """

    title: Optional[str] = Field(default=None, min_length=1, max_length=100)
    status: Optional[str] = None


class EpisodeReorderRequest(BaseModel):
    """剧集重排请求（S-06，PUT /api/projects/{project_id}/episodes/reorder）。

    Attributes:
        ordered_ids: 目标顺序的剧集 ID 列表（须恰好覆盖项目全部剧集且无重复）。
    """

    ordered_ids: List[int] = Field(min_length=1)


class ProjectDetailOut(ProjectOut):
    """项目详情输出模型（在基础字段上追加子资源概要计数与剧集摘要）。

    Attributes:
        episode_count: 剧集数量。
        script_count: 剧本数量。
        episodes: 剧集摘要数组（按 sequence 升序，S-06）。
    """

    episode_count: int = 0
    script_count: int = 0
    episodes: List[EpisodeSummaryOut] = Field(default_factory=list)


class SnapshotCreateRequest(BaseModel):
    """手动创建版本快照请求。

    Attributes:
        stage: 阶段标识（如 draft / script / storyboard 等，1~20 字符）。
        summary: 快照摘要（JSONB，可空）。
    """

    stage: str = Field(min_length=1, max_length=20)
    summary: Optional[Any] = None
