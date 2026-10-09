"""发布路由模块（文件级说明）。

实现成片合成 / 成片版本管理 / 审核记录查询 / 分享链接（share_token 机制）：
- POST /api/projects/{project_id}/compose                          发起成片合成（闸门二）
- GET  /api/projects/{project_id}/outputs                          成片版本列表
- POST /api/projects/{project_id}/outputs/{output_id}/set-current  切换当前版本（版本回退）
- GET  /api/projects/{project_id}/reviews                          审核记录列表
- POST /api/projects/{project_id}/publish                          发布并生成分享链接（闸门三）
- POST /api/projects/{project_id}/share/revoke                     撤销分享
- GET  /api/share/{token}                                          公开分享页数据（免认证，L-04）

注意：路由需携带 {project_id} 路径参数，FastAPI router prefix 不支持路径参数，
因此本路由不设 prefix，路由内部书写完整路径。
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import FilmOutput, Project, ReviewRecord, User
from app.schemas.common import PageResult
from app.schemas.generation import TaskOut
from app.schemas.publish import (
    ComposeCreateRequest,
    OutputOut,
    PublishRequest,
    PublishResponse,
    ReviewOut,
    ShareInfoResponse,
)
from app.services import normalize_pagination
from app.services.generation_service import create_compose_task, get_owned_project
from app.services.review_service import create_review_record

router = APIRouter(tags=["publish"])


async def _get_owned_output(db: AsyncSession, project_id: int, output_id: int) -> FilmOutput:
    """校验成片版本存在且属于指定项目。

    Args:
        db: 请求级异步数据库会话。
        project_id: 项目 ID。
        output_id: 成片版本 ID。

    Returns:
        FilmOutput: 归属校验通过的成片 ORM 对象。

    Raises:
        HTTPException: 404 —— 成片版本不存在或不属于该项目。
    """
    film = await db.get(FilmOutput, output_id)
    if film is None or film.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成片版本不存在")
    return film


@router.post("/api/projects/{project_id}/compose", response_model=TaskOut)
async def create_compose(
    project_id: int,
    req: ComposeCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskOut:
    """发起成片合成任务（成本 200 积分；同时落一条闸门二 compose 通过记录）。

    Args:
        project_id: 项目 ID。
        req: 请求体（resolution / aspect_ratio / mode）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        TaskOut: 新建的 compose 任务。

    Raises:
        HTTPException: 400 —— 项目无分镜；403/404 —— 归属校验失败；409 —— 积分不足。
    """
    project = await get_owned_project(db, user.id, project_id)
    # 闸门二（RD-34）：合成前机审记录（本阶段演示为直接通过）
    await create_review_record(
        db, project_id=project.id, gate="compose", target_type="film", result="pass"
    )
    task = await create_compose_task(
        db,
        project=project,
        resolution=req.resolution,
        aspect_ratio=req.aspect_ratio or project.aspect_ratio,
        mode=req.mode,
    )
    return TaskOut.from_task(task)


@router.get("/api/projects/{project_id}/outputs", response_model=PageResult[OutputOut])
async def list_project_outputs(
    project_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PageResult[OutputOut]:
    """分页查询项目成片版本列表（含当前版本标记与分享状态）。

    Args:
        project_id: 项目 ID。
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        PageResult[OutputOut]: 成片版本分页列表（按版本号倒序）。

    Raises:
        HTTPException: 403/404 —— 项目归属校验失败。
    """
    await get_owned_project(db, user.id, project_id)
    page, page_size = normalize_pagination(page, page_size)
    total = await db.scalar(
        select(func.count()).select_from(FilmOutput).where(FilmOutput.project_id == project_id)
    )
    rows = await db.execute(
        select(FilmOutput).where(FilmOutput.project_id == project_id)
        .order_by(FilmOutput.version_no.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return PageResult(
        total=total or 0, page=page, page_size=page_size,
        items=[OutputOut.from_output(o) for o in rows.scalars().all()],
    )


@router.post("/api/projects/{project_id}/outputs/{output_id}/set-current", response_model=OutputOut)
async def set_current_output(
    project_id: int,
    output_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OutputOut:
    """将指定成片版本设为当前版本（版本回退，其余版本自动退出当前标记）。

    Args:
        project_id: 项目 ID。
        output_id: 目标成片版本 ID。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        OutputOut: 切换后的成片版本。

    Raises:
        HTTPException: 403/404 —— 归属校验失败。
    """
    await get_owned_project(db, user.id, project_id)
    film = await _get_owned_output(db, project_id, output_id)
    rows = await db.execute(
        select(FilmOutput).where(FilmOutput.project_id == project_id, FilmOutput.is_current.is_(True))
    )
    for old in rows.scalars().all():
        old.is_current = False
    film.is_current = True
    await db.commit()
    return OutputOut.from_output(film)


@router.get("/api/projects/{project_id}/reviews", response_model=PageResult[ReviewOut])
async def list_project_reviews(
    project_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    gate_filter: Optional[str] = Query(default=None, alias="gate"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PageResult[ReviewOut]:
    """分页查询项目审核记录（含三级闸门与人工复核状态）。

    Args:
        project_id: 项目 ID。
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        gate_filter: 闸门过滤 generate/compose/publish（非法返回 400）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        PageResult[ReviewOut]: 审核记录分页列表（按创建时间倒序）。

    Raises:
        HTTPException: 400 —— gate 参数非法；403/404 —— 项目归属校验失败。
    """
    allowed_gates = ("generate", "compose", "publish")
    if gate_filter is not None and gate_filter not in allowed_gates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"gate 仅支持 {'/'.join(allowed_gates)}",
        )
    await get_owned_project(db, user.id, project_id)
    page, page_size = normalize_pagination(page, page_size)
    conditions = [ReviewRecord.project_id == project_id]
    if gate_filter:
        conditions.append(ReviewRecord.gate == gate_filter)
    total = await db.scalar(select(func.count()).select_from(ReviewRecord).where(*conditions))
    rows = await db.execute(
        select(ReviewRecord).where(*conditions)
        .order_by(ReviewRecord.created_at.desc(), ReviewRecord.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return PageResult(
        total=total or 0, page=page, page_size=page_size,
        items=[ReviewOut.from_record(r) for r in rows.scalars().all()],
    )


@router.post("/api/projects/{project_id}/publish", response_model=PublishResponse)
async def publish_project(
    project_id: int,
    req: PublishRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PublishResponse:
    """发布成片：生成 share_token 分享链接（闸门三机审记录 + 项目置 published）。

    Args:
        project_id: 项目 ID。
        req: 请求体（output_id / share_expires_days / share_password）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        PublishResponse: 分享路径 /share/{token} 与过期时间。

    Raises:
        HTTPException: 403/404 —— 归属校验失败；409 —— 积分/状态冲突（透传）。
    """
    project = await get_owned_project(db, user.id, project_id)
    film = await _get_owned_output(db, project_id, req.output_id)
    # 闸门三（RD-34）：发布前机审记录（本阶段演示为直接通过）
    await create_review_record(
        db, project_id=project.id, gate="publish", target_type="film", result="pass"
    )
    now = datetime.now(timezone.utc)
    film.share_token = secrets.token_urlsafe(12)
    film.share_expires_at = now + timedelta(days=req.share_expires_days)
    film.share_password = req.share_password or None
    film.share_revoked = False
    project.status = "published"
    await db.commit()
    return PublishResponse(share_url=f"/share/{film.share_token}", expires_at=film.share_expires_at)


@router.post("/api/projects/{project_id}/share/revoke")
async def revoke_share(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """撤销项目分享：全部携带分享令牌的成片版本标记 share_revoked。

    Args:
        project_id: 项目 ID。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"message": ...}。

    Raises:
        HTTPException: 403/404 —— 项目归属校验失败；409 —— 项目从未发布分享。
    """
    await get_owned_project(db, user.id, project_id)
    rows = await db.execute(
        select(FilmOutput).where(
            FilmOutput.project_id == project_id,
            FilmOutput.share_token.is_not(None),
            FilmOutput.share_revoked.is_(False),
        )
    )
    films = rows.scalars().all()
    if not films:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="项目当前没有有效的分享链接")
    for film in films:
        film.share_revoked = True
    await db.commit()
    return {"message": "分享链接已撤销"}


@router.get("/api/share/{token}", response_model=ShareInfoResponse)
async def get_shared_film(
    token: str,
    x_share_password: Optional[str] = Header(default=None, alias="X-Share-Password"),
    db: AsyncSession = Depends(get_db),
) -> ShareInfoResponse:
    """公开访问分享页数据（免认证，L-04 分享页数据源）。

    校验顺序：令牌不存在/已撤销 → 404；已过期 → 410；提取码不匹配 → 403。

    Args:
        token: 分享令牌（路径参数）。
        x_share_password: 请求头 X-Share-Password（设置了提取码的分享必传）。
        db: 请求级异步数据库会话。

    Returns:
        ShareInfoResponse: 分享页数据（项目标题/成片地址/分辨率/时长/水印标识/发布时间）。

    Raises:
        HTTPException: 404 —— 令牌无效或已撤销；410 —— 分享已过期；403 —— 提取码错误。
    """
    film = (
        await db.execute(select(FilmOutput).where(FilmOutput.share_token == token))
    ).scalar_one_or_none()
    if film is None or film.share_revoked:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分享链接不存在或已撤销")
    if film.share_expires_at is not None:
        expires = film.share_expires_at if film.share_expires_at.tzinfo else film.share_expires_at.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="分享链接已过期")
    if film.share_password and (x_share_password or "") != film.share_password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="提取码错误")
    project = await db.get(Project, film.project_id)
    return ShareInfoResponse(
        title=project.title if project else "未命名项目",
        video_url=film.url,
        resolution=film.resolution,
        duration=film.duration,
        watermark_id=film.watermark_id,
        published_at=film.updated_at,
    )
