"""项目管理路由模块（文件级说明）。

实现项目 CRUD / 软删除 / 复制 / 版本快照接口（全部校验项目归属，
他人或已删除项目一律 404，不泄露存在性）：
- GET    /api/projects                      分页列表（search/status/sort/order）
- POST   /api/projects                      创建（status='draft'）
- GET    /api/projects/{id}                 详情（含 episode/script 概要计数）
- PUT    /api/projects/{id}                 更新（status 仅允许合法状态机值）
- DELETE /api/projects/{id}                 软删除（deleted_at=now）
- POST   /api/projects/{id}/duplicate       复制项目行（不复制子资源）
- GET    /api/projects/{id}/snapshots       版本快照分页列表
- POST   /api/projects/{id}/snapshots       手动创建快照
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Episode, Project, Script, User, VersionSnapshot
from app.schemas.common import PageResult
from app.schemas.project import (
    PROJECT_STATUSES,
    FIDELITY_VALUES,
    ProjectCreateRequest,
    ProjectDetailOut,
    ProjectOut,
    ProjectUpdateRequest,
    SnapshotCreateRequest,
)
from app.schemas.user import SnapshotOut
from app.services import normalize_pagination

router = APIRouter(prefix="/api/projects", tags=["projects"])

# 列表排序字段白名单
_SORT_COLUMNS = {"updated_at": Project.updated_at, "created_at": Project.created_at}


async def get_owned_project(db: AsyncSession, project_id: int, user: User) -> Project:
    """校验并返回归属于当前用户的未删除项目（跨路由共用）。

    Args:
        db: 请求级异步数据库会话。
        project_id: 项目 ID。
        user: 当前用户。

    Returns:
        Project: 项目 ORM 对象。

    Raises:
        HTTPException: 404 —— 项目不存在、已软删除或不属于当前用户
            （统一 404 以避免泄露他人项目的存在性）。
    """
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == user.id,
            Project.deleted_at.is_(None),
        )
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return project


@router.get("", response_model=PageResult[ProjectOut])
async def list_projects(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    search: Optional[str] = Query(default=None, max_length=100),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    sort: str = Query(default="updated_at"),
    order: str = Query(default="desc"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PageResult[ProjectOut]:
    """分页查询当前用户的项目列表（排除软删除）。

    Args:
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        search: 标题模糊搜索关键字（可空）。
        status_filter: 项目状态过滤（可空；非法值返回 400）。
        sort: 排序字段（updated_at / created_at，非法值返回 400）。
        order: 排序方向（desc / asc，非法值返回 400）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PageResult[ProjectOut]: 项目分页列表。

    Raises:
        HTTPException: 400 —— status/sort/order 取值非法；401 —— 未认证。
    """
    if status_filter is not None and status_filter not in PROJECT_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="status 取值非法")
    if sort not in _SORT_COLUMNS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="sort 仅支持 updated_at/created_at")
    if order not in ("desc", "asc"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="order 仅支持 desc/asc")

    page, page_size = normalize_pagination(page, page_size)
    conditions = [Project.user_id == user.id, Project.deleted_at.is_(None)]
    if search:
        conditions.append(Project.title.ilike(f"%{search}%"))
    if status_filter:
        conditions.append(Project.status == status_filter)

    total = await db.scalar(select(func.count()).select_from(Project).where(*conditions))
    order_col = _SORT_COLUMNS[sort]
    rows = await db.execute(
        select(Project).where(*conditions)
        .order_by(order_col.desc() if order == "desc" else order_col.asc(), Project.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return PageResult(
        total=total or 0, page=page, page_size=page_size,
        items=[ProjectOut.model_validate(r) for r in rows.scalars().all()],
    )


@router.post("", response_model=ProjectOut)
async def create_project(
    req: ProjectCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    """创建新项目（初始状态 draft）。

    Args:
        req: 项目创建请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ProjectOut: 新建项目信息。

    Raises:
        HTTPException: 400 —— fidelity 取值非法；401 —— 未认证。
    """
    if req.fidelity is not None and req.fidelity not in FIDELITY_VALUES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="fidelity 仅支持 free/outline/faithful")
    project = Project(
        user_id=user.id,
        title=req.title.strip(),
        genre=req.genre,
        style=req.style,
        fidelity=req.fidelity,
        status="draft",
        aspect_ratio=req.aspect_ratio,
        target_duration=req.target_duration,
        config=req.config,
    )
    db.add(project)
    await db.commit()
    return ProjectOut.model_validate(project)


@router.get("/{project_id}", response_model=ProjectDetailOut)
async def get_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectDetailOut:
    """查询项目详情（附剧集与剧本概要计数）。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ProjectDetailOut: 项目详情（含 episode_count / script_count）。

    Raises:
        HTTPException: 404 —— 项目不存在/已删除/非本人项目。
    """
    project = await get_owned_project(db, project_id, user)
    episode_count = await db.scalar(
        select(func.count()).select_from(Episode).where(Episode.project_id == project.id)
    )
    script_count = await db.scalar(
        select(func.count()).select_from(Script).where(Script.project_id == project.id)
    )
    detail = ProjectDetailOut.model_validate(project)
    detail.episode_count = episode_count or 0
    detail.script_count = script_count or 0
    return detail


@router.put("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: int,
    req: ProjectUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    """更新项目字段（仅更新传入字段；status 仅允许合法状态机值）。

    Args:
        project_id: 项目 ID。
        req: 更新请求（title/genre/style/fidelity/aspect_ratio/target_duration/config/status）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ProjectOut: 更新后的项目信息。

    Raises:
        HTTPException: 400 —— status/fidelity 取值非法；404 —— 项目不存在/非本人。
    """
    project = await get_owned_project(db, project_id, user)
    if req.status is not None:
        if req.status not in PROJECT_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"status 非法，仅允许 {'/'.join(PROJECT_STATUSES)}",
            )
        project.status = req.status
    if req.fidelity is not None and req.fidelity not in FIDELITY_VALUES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="fidelity 仅支持 free/outline/faithful")
    for field in ("title", "genre", "style", "fidelity", "aspect_ratio", "target_duration", "config"):
        value = getattr(req, field)
        if value is not None:
            setattr(project, field, value if field != "title" else value.strip())
    await db.commit()
    # updated_at 带 onupdate=func.now()，UPDATE 后该属性未加载，直接序列化会触发
    # 异步懒加载（MissingGreenlet），必须先 refresh 重新读取
    await db.refresh(project)
    return ProjectOut.model_validate(project)


@router.delete("/{project_id}")
async def delete_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """软删除项目（仅标记 deleted_at，可从回收站恢复）。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"message": "项目已删除"}。

    Raises:
        HTTPException: 404 —— 项目不存在/已删除/非本人项目。
    """
    project = await get_owned_project(db, project_id, user)
    project.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "项目已删除"}


@router.post("/{project_id}/duplicate", response_model=ProjectOut)
async def duplicate_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    """复制项目：深复制 projects 表行本身（标题追加 " (副本)"，状态重置为 draft）。

    注意：按设计不复制剧集/剧本/分镜/快照等子资源，新项目从草稿重新开始。

    Args:
        project_id: 源项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ProjectOut: 新副本项目信息。

    Raises:
        HTTPException: 404 —— 源项目不存在/已删除/非本人项目。
    """
    project = await get_owned_project(db, project_id, user)
    dup = Project(
        user_id=user.id,
        title=f"{project.title} (副本)",
        genre=project.genre,
        style=project.style,
        fidelity=project.fidelity,
        status="draft",  # 副本一律回到草稿态
        aspect_ratio=project.aspect_ratio,
        target_duration=project.target_duration,
        config=dict(project.config) if project.config else None,
        cover_url=project.cover_url,
        deleted_at=None,
    )
    db.add(dup)
    await db.commit()
    return ProjectOut.model_validate(dup)


@router.get("/{project_id}/snapshots", response_model=PageResult[SnapshotOut])
async def list_project_snapshots(
    project_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PageResult[SnapshotOut]:
    """分页查询项目版本快照（按创建时间倒序）。

    Args:
        project_id: 项目 ID。
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PageResult[SnapshotOut]: 快照分页列表。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目。
    """
    project = await get_owned_project(db, project_id, user)
    page, page_size = normalize_pagination(page, page_size)
    total = await db.scalar(
        select(func.count()).select_from(VersionSnapshot).where(VersionSnapshot.project_id == project.id)
    )
    rows = await db.execute(
        select(VersionSnapshot).where(VersionSnapshot.project_id == project.id)
        .order_by(VersionSnapshot.created_at.desc(), VersionSnapshot.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return PageResult(
        total=total or 0, page=page, page_size=page_size,
        items=[SnapshotOut.model_validate(r) for r in rows.scalars().all()],
    )


@router.post("/{project_id}/snapshots", response_model=SnapshotOut)
async def create_project_snapshot(
    project_id: int,
    req: SnapshotCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SnapshotOut:
    """为项目手动创建版本快照（记录阶段与摘要）。

    Args:
        project_id: 项目 ID。
        req: 快照创建请求（stage/summary）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        SnapshotOut: 新建快照信息。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目。
    """
    project = await get_owned_project(db, project_id, user)
    snapshot = VersionSnapshot(
        project_id=project.id,
        stage=req.stage,
        checkpoint_id=None,  # 手动快照暂无 LangGraph 检查点，与 V-01 自动快照同表不同源
        summary=req.summary,
        impact_scope=None,
    )
    db.add(snapshot)
    await db.commit()
    return SnapshotOut.model_validate(snapshot)
