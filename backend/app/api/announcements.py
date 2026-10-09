"""公告路由模块（文件级说明）。

实现已发布公告查询：status='published'，按置顶优先、发布时间倒序返回全部字段。
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models import Announcement

router = APIRouter(prefix="/api/announcements", tags=["announcements"])


class AnnouncementOut(BaseModel):
    """公告输出模型（定义于本文件，避免超出约定的新建 schema 文件范围）。

    Attributes:
        id: 公告 ID。
        title: 标题。
        content: 内容。
        is_pinned: 是否置顶。
        status: 状态（draft/published/offline）。
        published_at: 发布时间（ISO 字符串）。
        created_at: 创建时间（ISO 字符串）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    content: str
    is_pinned: bool
    status: str
    published_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


@router.get("", response_model=List[AnnouncementOut])
async def list_announcements(db: AsyncSession = Depends(get_db)) -> List[AnnouncementOut]:
    """查询已发布公告列表（置顶优先，其余按发布时间倒序；公开接口，登录前可见）。

    Args:
        db: 请求级异步数据库会话。

    Returns:
        List[AnnouncementOut]: 已发布公告列表（全量，公告量级小不分页）。

    Raises:
        无：公开接口，不要求认证。
    """
    rows = await db.execute(
        select(Announcement)
        .where(Announcement.status == "published")
        .order_by(Announcement.is_pinned.desc(), Announcement.published_at.desc(), Announcement.id.desc())
    )
    return [AnnouncementOut.model_validate(r) for r in rows.scalars().all()]
