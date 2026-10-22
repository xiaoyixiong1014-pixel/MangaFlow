"""媒体素材路由模块（文件级说明）。

实现素材库查询 / 上传 / 详情 / 删除（引用保护）/ meta 更新：
- GET    /api/media              分页查询（media_type/source/project_id/search 过滤；
                                 2026-10-21 改为「项目产物聚合 + 上传素材」合并视图——
                                 图片=项目关键帧、视频=发布成片（film_outputs，非分镜视频）、
                                 音频=角色音色试音（characters.voice_sample_url，非分镜配音）、
                                 文本=剧本正文摘录（scripts.content），source='project'，
                                 合成负数 id 且不可删除；上传素材（media_assets 表）照常可删）
- POST   /api/media/upload       上传文件（50MB 上限，落盘 storage/uploads/{yyyy-mm}/）
- GET    /api/media/{id}         素材详情
- DELETE /api/media/{id}         删除（meta.ref_count > 0 时 409 拒绝，PL-19 引用保护）
- PUT    /api/media/{id}/meta    整体替换 meta 元数据
"""

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import (
    Character,
    FilmOutput,
    MediaAsset,
    Project,
    Script,
    Storyboard,
    User,
)
from app.api.projects import get_owned_project
from app.schemas.common import PageResult
from app.schemas.media import MediaMetaUpdateRequest, MediaOut
from app.services import normalize_pagination
from app.services.media_service import MEDIA_TYPES, infer_media_type, save_upload_file

router = APIRouter(prefix="/api/media", tags=["media"])

# 合法素材来源集合（RD-47 + 2026-10-21 聚合来源 project——仅用于查询过滤，
# 不落库（media_assets 表 CK 约束不含该值），聚合条目 source 固定 'project'）
_SOURCES = ("ai_generated", "upload", "canvas", "studio", "project")

# 聚合条目合成负 id 的类型偏移（互不重叠：image=-sb_id / video=-1e6-film_id
# / audio=-2e6-char_id / text=-3e6-script_id；仅作前端 key 与防误删标识）
_OFF_VIDEO = 1_000_000
_OFF_AUDIO = 2_000_000
_OFF_TEXT = 3_000_000


@router.get("", response_model=PageResult[MediaOut])
async def list_media(
    media_type: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
    project_id: Optional[int] = Query(default=None),
    search: Optional[str] = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PageResult[MediaOut]:
    """分页查询当前用户素材库（排除软删除，支持多维过滤与搜索）。

    Args:
        media_type: 媒体类型过滤（image/video/audio/text；非法返回 400）。
        source: 来源过滤（ai_generated/upload/canvas/studio；非法返回 400）。
        project_id: 按归属项目过滤（可空）。
        search: 按 url 或 meta 文件名模糊搜索（可空）。
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PageResult[MediaOut]: 素材分页列表（创建时间倒序）。

    Raises:
        HTTPException: 400 —— media_type/source 取值非法；401 —— 未认证。
    """
    if media_type is not None and media_type not in MEDIA_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"media_type 仅支持 {'/'.join(MEDIA_TYPES)}"
        )
    if source is not None and source not in _SOURCES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"source 仅支持 {'/'.join(_SOURCES)}"
        )
    page, page_size = normalize_pagination(page, page_size)

    # ---------- 1. 上传素材（media_assets 表，source=project 时跳过） ----------
    db_items: List[MediaOut] = []
    if source != "project":
        conditions = [MediaAsset.user_id == user.id, MediaAsset.deleted_at.is_(None)]
        if media_type:
            conditions.append(MediaAsset.media_type == media_type)
        if source:
            conditions.append(MediaAsset.source == source)
        if project_id is not None:
            conditions.append(MediaAsset.project_id == project_id)
        if search:
            pattern = f"%{search}%"
            conditions.append(or_(
                MediaAsset.url.ilike(pattern),
                func.coalesce(MediaAsset.meta["filename"].astext(), "").ilike(pattern),
            ))
        rows = await db.execute(
            select(MediaAsset).where(*conditions)
            .order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
        )
        db_items = [MediaOut.model_validate(r) for r in rows.scalars().all()]

    # ---------- 2. 项目产物聚合（2026-10-21 存储方式更新） ----------
    # 项目名映射（聚合条目 meta.project_title 展示用）
    proj_rows = await db.execute(
        select(Project.id, Project.title).where(
            Project.user_id == user.id, Project.deleted_at.is_(None)
        )
    )
    titles: Dict[int, str] = {pid: title for pid, title in proj_rows.all()}
    agg: List[MediaOut] = []

    def _mk(
        agg_id: int, mtype: str, url: str, pid: int, created: Optional[datetime], meta: dict,
    ) -> MediaOut:
        """构造聚合素材条目（内部函数，source 固定 project）。"""
        meta = dict(meta)
        meta["project_title"] = titles.get(pid, "")
        return MediaOut(
            id=agg_id, user_id=user.id, project_id=pid, media_type=mtype,
            source="project", url=url, meta=meta, created_at=created,
        )

    # 仅聚合请求的类型（media_type 未指定时四路全聚合）

    def _want(t: str) -> bool:
        """判断是否需要聚合指定类型（media_type 未指定时全部聚合）。"""
        return media_type in (None, t)

    if _want("image"):
        rows = await db.execute(
            select(Storyboard.id, Storyboard.keyframe_url, Storyboard.project_id, Storyboard.created_at)
            .join(Project, Project.id == Storyboard.project_id)
            .where(
                Project.user_id == user.id, Project.deleted_at.is_(None),
                Storyboard.keyframe_url.isnot(None),
            )
        )
        for sid, url, pid, created in rows.all():
            # 排除 SVG 占位图（mock 渠道时代产物）
            if not url or url.endswith(".svg"):
                continue
            if project_id is not None and pid != project_id:
                continue
            agg.append(_mk(-sid, "image", url, pid, created,
                           {"storyboard_id": sid, "kind": "关键帧"}))

    if _want("video"):
        # 发布成片（film_outputs，完整一集，非分镜视频）：全部版本均列出
        rows = await db.execute(
            select(
                FilmOutput.id, FilmOutput.url, FilmOutput.project_id, FilmOutput.created_at,
                FilmOutput.version_no, FilmOutput.resolution, FilmOutput.duration,
                FilmOutput.episode_id, FilmOutput.is_current,
            )
            .join(Project, Project.id == FilmOutput.project_id)
            .where(Project.user_id == user.id, Project.deleted_at.is_(None))
        )
        for fid, url, pid, created, ver, reso, dur, eid, cur in rows.all():
            if project_id is not None and pid != project_id:
                continue
            agg.append(_mk(
                -(_OFF_VIDEO + fid), "video", url, pid, created,
                {"kind": "成片", "version_no": ver, "resolution": reso,
                 "duration": dur, "episode_id": eid, "is_current": bool(cur)},
            ))

    if _want("audio"):
        # 角色音色试音（characters.voice_sample_url，「我是XX，这是我的配音」），
        # 非分镜配音（storyboard.audio_url 不进素材库）
        rows = await db.execute(
            select(Character.id, Character.voice_sample_url, Character.project_id,
                   Character.created_at, Character.name)
            .join(Project, Project.id == Character.project_id)
            .where(
                Project.user_id == user.id, Project.deleted_at.is_(None),
                Character.voice_sample_url.isnot(None),
            )
        )
        for cid, url, pid, created, name in rows.all():
            if not url or project_id is not None and pid != project_id:
                continue
            agg.append(_mk(-(_OFF_AUDIO + cid), "audio", url, pid, created,
                           {"kind": "角色试音", "character_name": name, "character_id": cid}))

    if _want("text"):
        # 剧本正文（scripts.content.value：title + scenes 的 plot/对白拼摘录）
        rows = await db.execute(
            select(Script.id, Script.content, Script.project_id, Script.created_at, Script.episode_id)
            .join(Project, Project.id == Script.project_id)
            .where(Project.user_id == user.id, Project.deleted_at.is_(None))
        )
        for scid, content, pid, created, eid in rows.all():
            if project_id is not None and pid != project_id:
                continue
            value = (content or {}).get("value") or {}
            title = str(value.get("title") or "未命名剧本")
            excerpt_parts: List[str] = [title]
            for scene in (value.get("scenes") or [])[:3]:
                plot = str(scene.get("plot") or "").strip()
                if plot:
                    excerpt_parts.append(plot)
                break
            excerpt = "｜".join(excerpt_parts)[:120]
            agg.append(_mk(
                -(_OFF_TEXT + scid), "text", "", pid, created,
                {"kind": "剧本正文", "title": title, "excerpt": excerpt, "episode_id": eid},
            ))

    # ---------- 3. 合并 + 过滤 + 排序 + 内存分页 ----------
    if search:
        kw = search.strip().lower()
        if kw:
            db_items = [
                m for m in db_items
                if kw in m.url.lower() or kw in json.dumps(m.meta, ensure_ascii=False).lower()
            ]
        agg = [
            m for m in agg
            if kw in m.url.lower() or kw in json.dumps(m.meta, ensure_ascii=False).lower()
        ]
    merged = db_items + agg
    merged.sort(key=lambda m: (m.created_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    total = len(merged)
    return PageResult(
        total=total, page=page, page_size=page_size,
        items=merged[(page - 1) * page_size: page * page_size],
    )


async def _get_owned_media(db: AsyncSession, media_id: int, user: User) -> MediaAsset:
    """获取归属于当前用户且未删除的素材（内部函数）。

    Args:
        db: 请求级异步数据库会话。
        media_id: 素材 ID。
        user: 当前用户。

    Returns:
        MediaAsset: 素材 ORM 对象。

    Raises:
        HTTPException: 404 —— 不存在、已删除或非本人素材。
    """
    result = await db.execute(
        select(MediaAsset).where(
            MediaAsset.id == media_id,
            MediaAsset.user_id == user.id,
            MediaAsset.deleted_at.is_(None),
        )
    )
    media = result.scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="素材不存在")
    return media


@router.post("/upload", response_model=MediaOut)
async def upload_media(
    file: UploadFile = File(...),
    media_type: Optional[str] = Form(default=None),
    project_id: Optional[int] = Form(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MediaOut:
    """上传素材文件：流式落盘 storage/uploads/{yyyy-mm}/{uuid}.{ext} 并登记素材记录。

    media_type 优先取表单值，否则按扩展名推断；project_id 提供时校验项目归属。

    Args:
        file: 上传的文件（≤50MB）。
        media_type: 显式媒体类型（可空）。
        project_id: 归属项目 ID（可空）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        MediaOut: 新建素材记录（source='upload'，meta 含文件名/大小/类型/引用计数）。

    Raises:
        HTTPException: 400 —— 文件超 50MB / 类型无法识别 / 扩展名非法；
            404 —— project_id 对应项目不存在或非本人；401 —— 未认证。
    """
    project = None
    if project_id is not None:
        project = await get_owned_project(db, project_id, user)

    confirmed_type = infer_media_type(file.filename, media_type)
    rel_url, size = await save_upload_file(file, confirmed_type)

    media = MediaAsset(
        user_id=user.id,
        project_id=project.id if project else None,
        media_type=confirmed_type,
        source="upload",
        url=rel_url,
        meta={
            "filename": file.filename,
            "size": size,
            "content_type": file.content_type,
            "ref_count": 0,  # 引用计数，删除保护（PL-19）依据
        },
    )
    db.add(media)
    await db.commit()
    return MediaOut.model_validate(media)


@router.get("/{media_id}", response_model=MediaOut)
async def get_media(
    media_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MediaOut:
    """查询素材详情（含完整 meta 元数据）。

    Args:
        media_id: 素材 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        MediaOut: 素材详情。

    Raises:
        HTTPException: 404 —— 素材不存在/已删除/非本人素材。
    """
    media = await _get_owned_media(db, media_id, user)
    return MediaOut.model_validate(media)


@router.delete("/{media_id}")
async def delete_media(
    media_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除素材（软删除）。

    引用保护（PL-19）：meta.ref_count 存在且 > 0 时返回 409，
    提示素材正被项目/分镜引用，仅可停用不可物理移除。

    Args:
        media_id: 素材 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"message": "素材已删除"}。

    Raises:
        HTTPException: 400 —— 聚合项目产物（合成负 id，随项目产物存续不可删）；
            409 —— 素材正被引用；404 —— 素材不存在/非本人。
    """
    if media_id < 0:
        # 聚合项目产物（关键帧/成片/试音/剧本正文）不落 media_assets 表，
        # 随项目产物存续——直接拒绝删除（前端也已隐藏删除入口）
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="项目产物素材不可删除，请到对应项目中管理",
        )
    media = await _get_owned_media(db, media_id, user)
    meta = media.meta or {}
    ref_count = meta.get("ref_count", 0)
    if isinstance(ref_count, int) and ref_count > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="素材正被引用，仅可停用")
    media.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "素材已删除"}


@router.put("/{media_id}/meta", response_model=MediaOut)
async def update_media_meta(
    media_id: int,
    req: MediaMetaUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MediaOut:
    """整体替换素材 meta 元数据。

    Args:
        media_id: 素材 ID。
        req: meta 更新请求（整个 JSONB 对象替换）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        MediaOut: 更新后的素材详情。

    Raises:
        HTTPException: 404 —— 素材不存在/已删除/非本人素材。
    """
    media = await _get_owned_media(db, media_id, user)
    media.meta = req.meta
    await db.commit()
    return MediaOut.model_validate(media)
