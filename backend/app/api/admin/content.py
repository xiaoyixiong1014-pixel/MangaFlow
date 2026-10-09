"""管理端内容运营路由模块（文件级说明）。

实现公告管理与公共提示词运营：
- GET    /api/admin/content/announcements         公告分页（含草稿/下架，支持状态过滤）
- POST   /api/admin/content/announcements         新建公告（published 立即发布 / draft 存草稿）
- PUT    /api/admin/content/announcements/{id}    更新公告（部分更新）
- DELETE /api/admin/content/announcements/{id}    删除公告（物理删除）
- GET    /api/admin/content/prompts               公共提示词分页（含上下架状态）
- POST   /api/admin/content/prompts               新建公共提示词（user_id=NULL / scope=public）
- PUT    /api/admin/content/prompts/{id}/status   提示词上下架
- DELETE /api/admin/content/prompts/{id}          删除公共提示词

公告同步说明：发布即落库（status='published'），用户端 GET /api/announcements
为公开只读接口，前端通过轮询/刷新获取最新公告，服务端无需推送。

提示词上下架说明：本接口切换 prompts.status（published/offline），用户端提示词
列表只应展示 status='published' 的记录——顶层 /api/prompts 需过滤
status='published'，集成阶段统一处理（本次任务不允许修改顶层文件，特此记录）。

全部接口要求运营方角色，普通用户 403。
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_operator
from app.models import Announcement, Prompt, User
from app.schemas.admin import (
    AdminAnnouncementItem,
    AdminPromptItem,
    AnnouncementCreateRequest,
    AnnouncementUpdateRequest,
    MessageOut,
    PromptCreateRequest,
    PromptStatusRequest,
)
from app.schemas.common import PageResult
from app.services import normalize_pagination

router = APIRouter(
    prefix="/api/admin/content",
    tags=["admin-content"],
    dependencies=[Depends(require_operator)],  # 路由级鉴权：仅运营方可访问，普通用户 403
)

# 公告允许的状态取值
_ANN_STATUS = ("draft", "published", "offline")
# 提示词上下架允许的状态取值
_PROMPT_STATUS = ("published", "offline")


async def _get_announcement_or_404(db: AsyncSession, announcement_id: int) -> Announcement:
    """按 ID 获取公告，不存在时抛 404（内部函数）。

    Args:
        db: 请求级异步数据库会话。
        announcement_id: 公告 ID。

    Returns:
        Announcement: 公告 ORM 对象。

    Raises:
        HTTPException: 404 —— 公告不存在。
    """
    announcement = await db.get(Announcement, announcement_id)
    if announcement is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="公告不存在")
    return announcement


async def _get_public_prompt_or_404(db: AsyncSession, prompt_id: int) -> Prompt:
    """按 ID 获取公共提示词（scope='public'），不存在时抛 404（内部函数）。

    Args:
        db: 请求级异步数据库会话。
        prompt_id: 提示词 ID。

    Returns:
        Prompt: 提示词 ORM 对象。

    Raises:
        HTTPException: 404 —— 提示词不存在或非公共提示词。
    """
    prompt = await db.get(Prompt, prompt_id)
    if prompt is None or prompt.scope != "public":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="公共提示词不存在")
    return prompt


# ==========================================================================
# 公告管理
# ==========================================================================


@router.get("/announcements", response_model=PageResult[AdminAnnouncementItem])
async def list_announcements(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    status_filter: Optional[str] = Query(default=None, alias="status",
                                         description="状态过滤：draft/published/offline"),
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> PageResult[AdminAnnouncementItem]:
    """分页查询公告列表（管理端含草稿与下架公告，置顶优先、创建时间倒序）。

    Args:
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        status_filter: 状态过滤（非法返回 400）。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        PageResult[AdminAnnouncementItem]: 公告分页列表。

    Raises:
        HTTPException: 400 —— status 取值非法。
    """
    if status_filter is not None and status_filter not in _ANN_STATUS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"status 仅支持 {'/'.join(_ANN_STATUS)}"
        )
    page, page_size = normalize_pagination(page, page_size)

    conditions = []
    if status_filter:
        conditions.append(Announcement.status == status_filter)
    total = await db.scalar(
        select(func.count()).select_from(Announcement).where(*conditions)
    ) or 0
    rows = await db.execute(
        select(Announcement).where(*conditions)
        .order_by(Announcement.is_pinned.desc(), Announcement.created_at.desc(), Announcement.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return PageResult(
        total=int(total), page=page, page_size=page_size,
        items=[AdminAnnouncementItem.model_validate(a) for a in rows.scalars().all()],
    )


@router.post("/announcements", response_model=AdminAnnouncementItem)
async def create_announcement(
    req: AnnouncementCreateRequest,
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> AdminAnnouncementItem:
    """新建公告：status='published' 立即发布（写 published_at，用户端刷新/轮询可见）；
    status='draft' 存草稿（用户端不可见）。

    Args:
        req: 公告创建请求。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        AdminAnnouncementItem: 创建后的公告。

    Raises:
        HTTPException: 400 —— status 取值非法。
    """
    if req.status not in ("published", "draft"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="status 仅支持 published/draft")
    announcement = Announcement(
        title=req.title,
        content=req.content,
        is_pinned=req.is_pinned,
        status=req.status,
        published_at=datetime.now(timezone.utc) if req.status == "published" else None,
    )
    db.add(announcement)
    await db.commit()
    await db.refresh(announcement)
    return AdminAnnouncementItem.model_validate(announcement)


@router.put("/announcements/{announcement_id}", response_model=AdminAnnouncementItem)
async def update_announcement(
    announcement_id: int,
    req: AnnouncementUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> AdminAnnouncementItem:
    """更新公告（部分更新）。状态切换为 published 时刷新发布时间。

    Args:
        announcement_id: 公告 ID。
        req: 公告更新请求（字段均可选）。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        AdminAnnouncementItem: 更新后的公告。

    Raises:
        HTTPException: 400 —— status 取值非法；404 —— 公告不存在。
    """
    announcement = await _get_announcement_or_404(db, announcement_id)
    if req.status is not None and req.status not in _ANN_STATUS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"status 仅支持 {'/'.join(_ANN_STATUS)}"
        )

    if req.title is not None:
        announcement.title = req.title
    if req.content is not None:
        announcement.content = req.content
    if req.is_pinned is not None:
        announcement.is_pinned = req.is_pinned
    if req.status is not None and req.status != announcement.status:
        announcement.status = req.status
        # 首次/重新发布时刷新发布时间，用户端按发布时间排序展示
        if req.status == "published":
            announcement.published_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(announcement)
    return AdminAnnouncementItem.model_validate(announcement)


@router.delete("/announcements/{announcement_id}", response_model=MessageOut)
async def delete_announcement(
    announcement_id: int,
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> MessageOut:
    """删除公告（物理删除）。

    Args:
        announcement_id: 公告 ID。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        MessageOut: 操作结果消息。

    Raises:
        HTTPException: 404 —— 公告不存在。
    """
    announcement = await _get_announcement_or_404(db, announcement_id)
    await db.delete(announcement)
    await db.commit()
    return MessageOut(message=f"公告 #{announcement_id} 已删除")


# ==========================================================================
# 公共提示词管理
# ==========================================================================


@router.get("/prompts", response_model=PageResult[AdminPromptItem])
async def list_prompts(
    scope: str = Query(default="public", description="范围过滤：public（公共提示词）"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> PageResult[AdminPromptItem]:
    """分页查询公共提示词（scope='public'，含上下架状态，创建时间倒序）。

    Args:
        scope: 范围过滤（当前管理端仅支持 public）。
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        PageResult[AdminPromptItem]: 公共提示词分页列表。

    Raises:
        HTTPException: 400 —— scope 非 public。
    """
    if scope != "public":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="管理端当前仅支持 scope=public")
    page, page_size = normalize_pagination(page, page_size)

    conditions = [Prompt.scope == "public"]
    total = await db.scalar(select(func.count()).select_from(Prompt).where(*conditions)) or 0
    rows = await db.execute(
        select(Prompt).where(*conditions)
        .order_by(Prompt.created_at.desc(), Prompt.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return PageResult(
        total=int(total), page=page, page_size=page_size,
        items=[AdminPromptItem.model_validate(p) for p in rows.scalars().all()],
    )


@router.post("/prompts", response_model=AdminPromptItem)
async def create_prompt(
    req: PromptCreateRequest,
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> AdminPromptItem:
    """新建公共提示词：user_id=NULL、scope='public'、status='published'（直接上架）。

    Args:
        req: 提示词创建请求。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        AdminPromptItem: 创建后的公共提示词。

    Raises:
        无：参数校验由 Pydantic 承担，其余异常向上抛出。
    """
    prompt = Prompt(
        user_id=None,          # NULL = 公共提示词（非任何用户私有）
        scope="public",
        capability=req.capability,
        title=req.title,
        content=req.content,
        category=req.category,
        tags=req.tags,
        status="published",    # 新建默认上架
    )
    db.add(prompt)
    await db.commit()
    await db.refresh(prompt)
    return AdminPromptItem.model_validate(prompt)


@router.put("/prompts/{prompt_id}/status", response_model=AdminPromptItem)
async def update_prompt_status(
    prompt_id: int,
    req: PromptStatusRequest,
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> AdminPromptItem:
    """切换公共提示词上下架状态（published 上架 / offline 下架）。

    注意：用户端提示词列表只应展示 status='published'——顶层 /api/prompts
    需过滤 status='published'，集成阶段统一处理（本次任务不允许修改顶层文件，
    特此记录，避免遗漏）。

    Args:
        prompt_id: 提示词 ID。
        req: 状态切换请求。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        AdminPromptItem: 更新后的提示词。

    Raises:
        HTTPException: 400 —— status 取值非法；404 —— 公共提示词不存在。
    """
    if req.status not in _PROMPT_STATUS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"status 仅支持 {'/'.join(_PROMPT_STATUS)}"
        )
    prompt = await _get_public_prompt_or_404(db, prompt_id)
    prompt.status = req.status
    await db.commit()
    await db.refresh(prompt)
    return AdminPromptItem.model_validate(prompt)


@router.delete("/prompts/{prompt_id}", response_model=MessageOut)
async def delete_prompt(
    prompt_id: int,
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> MessageOut:
    """删除公共提示词（物理删除）。

    Args:
        prompt_id: 提示词 ID。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        MessageOut: 操作结果消息。

    Raises:
        HTTPException: 404 —— 公共提示词不存在。
    """
    prompt = await _get_public_prompt_or_404(db, prompt_id)
    await db.delete(prompt)
    await db.commit()
    return MessageOut(message=f"公共提示词 #{prompt_id} 已删除")
