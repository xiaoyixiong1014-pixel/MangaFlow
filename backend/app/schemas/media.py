"""媒体素材 Pydantic Schema 模块（文件级说明）。

定义素材查询、上传结果、meta 更新的出入参模型。
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class MediaOut(BaseModel):
    """媒体素材输出模型。

    Attributes:
        id: 素材 ID。
        user_id: 归属用户 ID。
        project_id: 归属项目 ID（可空）。
        media_type: 媒体类型（image/video/audio/text）。
        source: 素材来源（ai_generated/upload/canvas/studio）。
        url: 稳定地址（相对存储路径）。
        meta: 元数据（尺寸/时长/文件名/引用计数等 JSONB）。
        created_at: 上传时间（ISO 字符串）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    project_id: Optional[int] = None
    media_type: str
    source: str
    url: str
    meta: Optional[Any] = None
    created_at: Optional[datetime] = None


class MediaMetaUpdateRequest(BaseModel):
    """素材 meta 更新请求（整体替换 meta 字段）。

    Attributes:
        meta: 新的元数据对象（JSONB）。
    """

    meta: dict
