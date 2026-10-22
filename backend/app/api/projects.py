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
- GET    /api/projects/{id}/export          项目 ZIP 导出（manifest + media）
- POST   /api/projects/import               ZIP 导入（全链重建项目数据）
"""

import io
import json
import random
import re
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models import (
    AssetVariant,
    Character,
    Episode,
    FilmOutput,
    Project,
    Prop,
    Scene,
    Script,
    Storyboard,
    StoryboardCharacter,
    Style,
    User,
    VersionSnapshot,
)
from app.schemas.common import PageResult
from app.schemas.project import (
    PROJECT_STATUSES,
    FIDELITY_VALUES,
    EpisodeSummaryOut,
    ProjectCreateRequest,
    ProjectDetailOut,
    ProjectOut,
    ProjectUpdateRequest,
    SnapshotCreateRequest,
)
from app.schemas.user import SnapshotOut
from app.services import normalize_pagination

router = APIRouter(prefix="/api/projects", tags=["projects"])

# 列表排序字段白名单（D-06：recent 为 updated_at 的语义别名，默认排序即最近编辑倒序）
_SORT_COLUMNS = {
    "updated_at": Project.updated_at,
    "created_at": Project.created_at,
    "recent": Project.updated_at,
}


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
    sort: str = Query(default="recent"),
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
        sort: 排序字段（recent=最近编辑 / updated_at / created_at，非法值返回 400；
            recent 为 updated_at 别名，默认 recent + desc 即最近编辑倒序，D-06）。
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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="sort 仅支持 recent/updated_at/created_at")
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
    proj_rows = rows.scalars().all()
    # 随机关键帧封面（2026-10-21 用户确认）：项目已有关键帧时，从其中随机
    # 选取一张作为卡片封面（projects.cover_url 未填时生效）——一次查询取回
    # 本页项目的全部关键帧 URL，Python 侧按项目分组后随机选一张，避免
    # DISTINCT ON 弃用警告且随机性覆盖全部候选
    if proj_rows:
        kf_rows = await db.execute(
            select(Storyboard.project_id, Storyboard.keyframe_url)
            .where(
                Storyboard.project_id.in_([p.id for p in proj_rows]),
                Storyboard.keyframe_url.isnot(None),
            )
        )
        grouped: Dict[int, List[str]] = {}
        for pid, url in kf_rows.all():
            # 排除 SVG 占位图（mock 渠道时代产物，作封面观感差）
            if url and not url.endswith(".svg"):
                grouped.setdefault(pid, []).append(url)
        covers: Dict[int, str] = {
            pid: urls[random.randrange(len(urls))]
            for pid, urls in grouped.items()
        }
    else:
        covers = {}
    items: List[ProjectOut] = []
    for r in proj_rows:
        out = ProjectOut.model_validate(r)
        if not out.cover_url and r.id in covers:
            out.cover_url = covers[r.id]
        items.append(out)
    return PageResult(
        total=total or 0, page=page, page_size=page_size, items=items,
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
    # 剧集摘要数组（S-06）：按 sequence 升序，供工作区剧集切换器直接消费
    episode_rows = await db.execute(
        select(Episode)
        .where(Episode.project_id == project.id)
        .order_by(Episode.sequence.asc(), Episode.id.asc())
    )
    detail = ProjectDetailOut.model_validate(project)
    detail.episode_count = episode_count or 0
    detail.script_count = script_count or 0
    detail.episodes = [
        EpisodeSummaryOut.model_validate(e) for e in episode_rows.scalars().all()
    ]
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


# ---------------------------------------------------------------------------
# 项目 ZIP 导出 / 导入（Task 7.1）
# ---------------------------------------------------------------------------
# manifest 结构版本号（导入时强校验，不匹配返回 400）
MANIFEST_VERSION = 1


def _to_dict(obj) -> dict:
    """把 ORM 对象序列化为纯 dict（仅取表列，不含 relationship）。

    datetime 转 ISO 字符串、Decimal 转 float，保证可 JSON 序列化。

    Args:
        obj: SQLAlchemy ORM 模型实例。

    Returns:
        dict: {列名: 值} 的可 JSON 序列化字典。

    Raises:
        无：纯属性读取与类型转换，不产生异常路径。
    """
    out: dict = {}
    for column in obj.__table__.columns:
        value = getattr(obj, column.key)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = float(value)
        out[column.key] = value
    return out


def _local_media_url(url: Optional[str]) -> Optional[str]:
    """判断 URL 是否指向本地 /storage 静态文件。

    Args:
        url: 任意资源 URL（可空）。

    Returns:
        Optional[str]: 本地存储 URL 返回去掉 /storage/ 前缀的相对路径；
            外链或空值返回 None。

    Raises:
        无：纯字符串运算。
    """
    if not url or not url.startswith("/storage/"):
        return None
    return url.removeprefix("/storage/").lstrip("/") or None


@router.get("/{project_id}/export")
async def export_project_zip(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """把项目全量数据（manifest.json + media/）打包为 ZIP 下载。

    ZIP 结构：
    - manifest.json：version + project（去掉 owner）/episodes/scripts/
      characters/scenes/props/styles/asset_variants/storyboards/
      storyboard_characters 全量字段（datetime 转 ISO）；文件缺失的媒体
      URL 记录在 manifest["missing_media"]；
    - media/：项目内所有本地 /storage 媒体文件，按 URL 相对路径存放。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StreamingResponse: application/zip 字节流（Content-Disposition 附件下载）。

    Raises:
        HTTPException: 404 —— 项目不存在/已删除/非本人项目。
    """
    project = await get_owned_project(db, project_id, user)
    pid = project.id

    # ---------- 1. 全量查询项目各域数据 ----------
    episodes = (await db.execute(select(Episode).where(Episode.project_id == pid)
                                 .order_by(Episode.sequence))).scalars().all()
    scripts = (await db.execute(select(Script).where(Script.project_id == pid))).scalars().all()
    characters = (await db.execute(select(Character).where(Character.project_id == pid))).scalars().all()
    scenes = (await db.execute(select(Scene).where(Scene.project_id == pid))).scalars().all()
    props = (await db.execute(select(Prop).where(Prop.project_id == pid))).scalars().all()
    styles = (await db.execute(select(Style).where(Style.project_id == pid))).scalars().all()
    variants = (await db.execute(select(AssetVariant).where(
        AssetVariant.owner_type.in_(("character", "scene", "prop")),
        AssetVariant.owner_id.in_([o.id for o in (*characters, *scenes, *props)]),
    ))).scalars().all()
    storyboards = (await db.execute(select(Storyboard).where(Storyboard.project_id == pid)
                                    .order_by(Storyboard.sequence))).scalars().all()
    links = []
    if storyboards:
        links = (await db.execute(select(StoryboardCharacter).where(
            StoryboardCharacter.storyboard_id.in_([s.id for s in storyboards])
        ))).scalars().all()
    films = (await db.execute(select(FilmOutput).where(FilmOutput.project_id == pid))).scalars().all()

    # ---------- 2. 组装 manifest（project 去掉 owner=user_id） ----------
    project_dict = _to_dict(project)
    project_dict.pop("user_id", None)
    manifest: dict = {
        "version": MANIFEST_VERSION,
        "project": project_dict,
        "episodes": [_to_dict(e) for e in episodes],
        "scripts": [_to_dict(s) for s in scripts],
        "characters": [_to_dict(c) for c in characters],
        "scenes": [_to_dict(s) for s in scenes],
        "props": [_to_dict(p) for p in props],
        "styles": [_to_dict(s) for s in styles],
        "asset_variants": [_to_dict(v) for v in variants],
        "storyboards": [_to_dict(s) for s in storyboards],
        "storyboard_characters": [_to_dict(l) for l in links],
    }

    # ---------- 3. 收集项目内全部本地媒体 URL（去重保序） ----------
    candidates: list = [project.cover_url]
    for c in characters:
        candidates.append(c.asset_url)
    for s in scenes:
        candidates.append(s.asset_url)
    for p in props:
        candidates.append(p.asset_url)
    for s in styles:
        candidates.append(s.reference_url)
    for v in variants:
        candidates.append(v.asset_url)
    for s in storyboards:
        candidates.extend((s.keyframe_url, s.end_frame_url, s.video_url, s.audio_url))
    for f in films:
        candidates.append(f.url)

    storage_root = settings.storage_path()
    media_files: list = []  # (相对路径, 物理路径)
    missing_media: list = []
    seen: set = set()
    for url in candidates:
        rel = _local_media_url(url)
        if rel is None or rel in seen:
            continue
        seen.add(rel)
        phys = storage_root / rel
        if phys.is_file():
            media_files.append((rel, phys))
        else:
            missing_media.append(url)
    manifest["missing_media"] = missing_media

    # ---------- 4. 打包 ZIP（内存 BytesIO） ----------
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for rel, phys in media_files:
            zf.write(phys, f"media/{rel}")
    buf.seek(0)

    # 附件名：ASCII 兜底 + RFC 5987 UTF-8 扩展（标题可能含中文）
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", project.title).strip("_") or "export"
    ascii_name = f"project_{pid}_{slug}.zip"
    utf8_name = quote(f"project_{pid}_{project.title}.zip")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"
            )
        },
    )


@router.post("/import")
async def import_project_zip(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """从 ZIP 包导入项目：解析 manifest 全链重建（新 ID + 外键重映射），
    media/* 落回本地存储（同名冲突加时间戳），URL 字段同步修正。

    Args:
        file: multipart 上传的 ZIP 文件。
        user: 当前用户（导入项目归属该用户）。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"message", "project_id", "episodes", "storyboards"}（新项目 ID 与重建规模）。

    Raises:
        HTTPException: 400 —— 非 ZIP / 缺 manifest / manifest 解析失败 /
            版本不符；401 —— 未认证。
    """
    data = await file.read()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="导入文件不是合法的 ZIP 包")

    with archive:
        if "manifest.json" not in archive.namelist():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="导入包缺少 manifest.json")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="manifest.json 解析失败")
        if not isinstance(manifest, dict) or not isinstance(manifest.get("project"), dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="manifest 格式非法")
        if manifest.get("version") != MANIFEST_VERSION:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"manifest 版本不符（期望 {MANIFEST_VERSION}，实际 {manifest.get('version')}）",
            )

        # ---------- 1. media/* 落回本地存储，构建 旧相对路径 → 新相对路径 映射 ----------
        storage_root = settings.storage_path()
        media_map: dict = {}
        stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        for name in archive.namelist():
            if not name.startswith("media/") or name.endswith("/"):
                continue
            rel = name[len("media/"):].lstrip("/")
            if not rel or ".." in rel:
                continue  # 跳过空项与路径穿越项
            target = storage_root / rel
            if target.exists():
                # 同名冲突：文件名追加导入时间戳避免覆盖他人文件
                target = target.with_name(f"{target.stem}_imp_{stamp}{target.suffix}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
            media_map[rel] = target.relative_to(storage_root).as_posix()

    def _remap_url(url: Optional[str]) -> Optional[str]:
        """把 /storage URL 重映射到导入后的新文件路径（内部函数）。

        Args:
            url: 原项目媒体 URL（可空）。

        Returns:
            Optional[str]: 命中 media_map 时返回新 URL；否则原样返回。
        """
        rel = _local_media_url(url)
        if rel is not None and rel in media_map:
            return f"/storage/{media_map[rel]}"
        return url

    src = manifest["project"]

    def _old_id(row: dict):
        """读取 manifest 行的旧主键（缺失返回 None，内部函数）。"""
        return row.get("id")

    # ---------- 2. 重建项目主体（状态回到 draft，归属当前用户） ----------
    project = Project(
        user_id=user.id,
        title=f"{src.get('title') or '未命名项目'}（导入）",
        genre=src.get("genre"),
        style=src.get("style"),
        fidelity=src.get("fidelity"),
        status="draft",
        aspect_ratio=src.get("aspect_ratio") or "9:16",
        target_duration=src.get("target_duration"),
        config=src.get("config"),
        cover_url=_remap_url(src.get("cover_url")),
    )
    db.add(project)
    await db.flush()

    # ---------- 3. 依外键依赖序重建各域数据（old_id → new_id 映射） ----------
    ep_map: dict = {}
    for row in manifest.get("episodes", []):
        ep = Episode(
            project_id=project.id,
            title=row.get("title") or "未命名剧集",
            sequence=row.get("sequence") or 1,
            script_snapshot=row.get("script_snapshot"),
            status=row.get("status") or "draft",
        )
        db.add(ep)
        await db.flush()
        if _old_id(row) is not None:
            ep_map[_old_id(row)] = ep.id

    script_map: dict = {}
    for row in manifest.get("scripts", []):
        sc = Script(
            project_id=project.id,
            episode_id=ep_map.get(row.get("episode_id")),
            content=row.get("content") or {},
            raw_text=row.get("raw_text"),
            fences=row.get("fences"),
            health_report=row.get("health_report"),
            version=row.get("version") or 1,
        )
        db.add(sc)
        await db.flush()
        if _old_id(row) is not None:
            script_map[_old_id(row)] = sc.id

    char_map: dict = {}
    char_objs: dict = {}
    for row in manifest.get("characters", []):
        ch = Character(
            project_id=project.id,
            name=row.get("name") or "未命名角色",
            aliases=row.get("aliases"),
            description=row.get("description"),
            visual_prompt=row.get("visual_prompt") or "",
            feature_vector=row.get("feature_vector"),
            asset_url=_remap_url(row.get("asset_url")),
            face_locked=bool(row.get("face_locked")),
            outfit_variant_id=None,  # 变体重建后再回填
            generation_status=row.get("generation_status") or "pending",
            deleted_at=None,
        )
        db.add(ch)
        await db.flush()
        if _old_id(row) is not None:
            char_map[_old_id(row)] = ch.id
            char_objs[ch.id] = ch

    scene_map: dict = {}
    for row in manifest.get("scenes", []):
        sc = Scene(
            project_id=project.id,
            name=row.get("name") or "未命名场景",
            description=row.get("description"),
            visual_prompt=row.get("visual_prompt") or "",
            asset_url=_remap_url(row.get("asset_url")),
            space_signature=row.get("space_signature"),
            is_locked=bool(row.get("is_locked")),
            deleted_at=None,
        )
        db.add(sc)
        await db.flush()
        if _old_id(row) is not None:
            scene_map[_old_id(row)] = sc.id

    prop_map: dict = {}
    for row in manifest.get("props", []):
        pr = Prop(
            project_id=project.id,
            name=row.get("name") or "未命名道具",
            category=row.get("category"),
            description=row.get("description"),
            visual_prompt=row.get("visual_prompt") or "",
            asset_url=_remap_url(row.get("asset_url")),
            ownership=row.get("ownership"),
            is_locked=bool(row.get("is_locked")),
            deleted_at=None,
        )
        db.add(pr)
        await db.flush()
        if _old_id(row) is not None:
            prop_map[_old_id(row)] = pr.id

    for row in manifest.get("styles", []):
        db.add(Style(
            project_id=project.id,
            name=row.get("name") or "未命名风格",
            preset_code=row.get("preset_code"),
            reference_url=_remap_url(row.get("reference_url")),
            visual_prompt=row.get("visual_prompt") or "",
            is_default=bool(row.get("is_default")),
        ))

    variant_map: dict = {}
    owner_maps = {"character": char_map, "scene": scene_map, "prop": prop_map}
    for row in manifest.get("asset_variants", []):
        owner_map = owner_maps.get(row.get("owner_type"))
        new_owner_id = owner_map.get(row.get("owner_id")) if owner_map else None
        if new_owner_id is None:
            continue  # 归属资产缺失则丢弃该变体，避免悬挂 owner_id
        av = AssetVariant(
            owner_type=row.get("owner_type"),
            owner_id=new_owner_id,
            variant_type=row.get("variant_type") or "other",
            name=row.get("name") or "未命名变体",
            inherit_mask=row.get("inherit_mask") or {},
            asset_url=_remap_url(row.get("asset_url")),
            visual_prompt=row.get("visual_prompt"),
            is_active=bool(row.get("is_active")),
        )
        db.add(av)
        await db.flush()
        if _old_id(row) is not None:
            variant_map[_old_id(row)] = av.id

    # 回填角色"当前服饰变体"（RD-12）
    for row in manifest.get("characters", []):
        old_variant = row.get("outfit_variant_id")
        if old_variant is not None and _old_id(row) in char_objs and old_variant in variant_map:
            char_objs[char_map[_old_id(row)]].outfit_variant_id = variant_map[old_variant]

    sb_map: dict = {}
    for row in manifest.get("storyboards", []):
        sb = Storyboard(
            project_id=project.id,
            episode_id=ep_map.get(row.get("episode_id")),
            script_id=script_map.get(row.get("script_id")),
            sequence=row.get("sequence") or 1,
            shot_type=row.get("shot_type"),
            camera_movement=row.get("camera_movement"),
            duration=row.get("duration") or 5,
            actual_duration=row.get("actual_duration"),
            visual_description=row.get("visual_description"),
            dialogue=row.get("dialogue"),
            scene_id=scene_map.get(row.get("scene_id")),
            prompt_locked=row.get("prompt_locked"),
            prompt_editable=row.get("prompt_editable"),
            keyframe_url=_remap_url(row.get("keyframe_url")),
            end_frame_url=_remap_url(row.get("end_frame_url")),
            video_url=_remap_url(row.get("video_url")),
            audio_url=_remap_url(row.get("audio_url")),
            status=row.get("status") or "pending",
            regen_count=row.get("regen_count") or 0,
        )
        db.add(sb)
        await db.flush()
        if _old_id(row) is not None:
            sb_map[_old_id(row)] = sb.id

    link_count = 0
    for row in manifest.get("storyboard_characters", []):
        new_sb = sb_map.get(row.get("storyboard_id"))
        new_char = char_map.get(row.get("character_id"))
        if new_sb is None or new_char is None:
            continue
        db.add(StoryboardCharacter(
            storyboard_id=new_sb,
            character_id=new_char,
            variant_id=variant_map.get(row.get("variant_id")),
            link_confidence=row.get("link_confidence"),
        ))
        link_count += 1

    await db.commit()
    return {
        "message": "导入成功",
        "project_id": project.id,
        "episodes": len(ep_map),
        "storyboards": len(sb_map),
        "storyboard_characters": link_count,
    }
