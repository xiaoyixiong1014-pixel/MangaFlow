"""发布路由模块（文件级说明）。

实现成片合成 / 成片版本管理 / 审核记录查询 / 分享链接（share_token 机制）：
- POST /api/projects/{project_id}/compose                          发起成片合成（闸门二）
- GET  /api/projects/{project_id}/outputs                          成片版本列表
- POST /api/projects/{project_id}/outputs/{output_id}/set-current  切换当前版本（版本回退）
- DELETE /api/projects/{project_id}/outputs/{output_id}            删除成片版本（分享保护 + 当前版本自动接任）
- GET  /api/projects/{project_id}/reviews                          审核记录列表
- POST /api/projects/{project_id}/publish                          发布并生成分享链接（闸门三）
- POST /api/projects/{project_id}/share/revoke                     撤销分享
- GET  /api/share/{token}                                          公开分享页数据（免认证，L-04）
- GET  /api/publish/outputs/{output_id}/download                   成片 MP4 下载（可选转码）

注意：路由需携带 {project_id} 路径参数，FastAPI router prefix 不支持路径参数，
因此本路由不设 prefix，路由内部书写完整路径。
"""

import asyncio
import secrets
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Episode, FilmOutput, Project, ReviewRecord, Storyboard, User
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


async def _resolve_involved_storyboard_ids(
    db: AsyncSession,
    project_id: int,
    storyboard_ids: Optional[List[int]],
    episode_id: Optional[int] = None,
) -> List[int]:
    """解析本次合成涉及的分镜 ID 集合（增量模式按指定清单过滤，Task 5.3；
    S-06 传入 episode_id 时仅取该剧集的分镜）。

    Args:
        db: 请求级异步数据库会话。
        project_id: 项目 ID。
        storyboard_ids: 请求指定的分镜 ID 清单（None/空表示全量）。
        episode_id: 可选剧集 ID（S-06；None 表示不限剧集，向后兼容）。

    Returns:
        List[int]: 涉及分镜 ID（按镜头序号排序）。

    Raises:
        HTTPException: 400 —— 指定清单含不属于该项目的分镜。
    """
    conds = [Storyboard.project_id == project_id]
    if episode_id is not None:
        conds.append(Storyboard.episode_id == episode_id)
    rows = await db.execute(
        select(Storyboard.id).where(*conds).order_by(Storyboard.sequence)
    )
    all_ids = list(rows.scalars().all())
    if not storyboard_ids:
        return all_ids
    id_set = set(all_ids)
    invalid = [sid for sid in storyboard_ids if sid not in id_set]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"storyboard_ids 含不属于该项目的分镜：{invalid[:5]}",
        )
    # 保持镜头顺序输出（去重）
    wanted = set(storyboard_ids)
    return [sid for sid in all_ids if sid in wanted]


async def _check_compose_gate(db: AsyncSession, project_id: int, involved_ids: List[int]) -> None:
    """闸门二判定（RD-34，Task 4.2）：合成前审核记录真实校验。

    汇总本次合成涉及分镜的 generate 闸门机审记录：
    - 任一 violate 且未被人工 approved → 400 拦截（附原因）；
    - 任一 suspect 且尚未人工处置（manual_status 为 NULL/pending）→ 400 拦截
      （提示待人工审核；NULL 与 pending 同属"待人工"口径，与管理端审核队列一致）。

    Args:
        db: 请求级异步数据库会话。
        project_id: 项目 ID。
        involved_ids: 本次合成涉及的分镜 ID 列表。

    Returns:
        None

    Raises:
        HTTPException: 400 —— 存在未放行的违规/待人工复核记录。
    """
    if not involved_ids:
        return
    rows = await db.execute(
        select(ReviewRecord).where(
            ReviewRecord.project_id == project_id,
            ReviewRecord.gate == "generate",
            ReviewRecord.target_id.in_(involved_ids),
            ReviewRecord.result.in_(("suspect", "violate")),
        )
    )
    records = rows.scalars().all()
    blocked_violate = [r for r in records if r.result == "violate" and r.manual_status != "approved"]
    blocked_suspect = [
        r for r in records if r.result == "suspect" and r.manual_status in (None, "pending")
    ]
    if blocked_violate:
        first = blocked_violate[0]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"存在未通过人工复核的违规内容（记录 #{first.id}："
                    f"{first.suggestion or '内容违规已拦截'}），无法发起合成"),
        )
    if blocked_suspect:
        first = blocked_suspect[0]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"存在待人工复核的可疑内容（记录 #{first.id}），"
                    f"请等待审核通过后再发起合成"),
        )


async def _check_publish_gate(db: AsyncSession, project_id: int) -> None:
    """闸门三判定（RD-34，Task 4.2）：发布前汇总项目全部闸门审核记录。

    - 任一闸门存在 violate 且未被人工 approved → 400 拦截；
    - 任一闸门存在 suspect 且未人工处置（NULL/pending）→ 400 拦截；
    - 全部放行才允许 publish 推进。

    Args:
        db: 请求级异步数据库会话。
        project_id: 项目 ID。

    Returns:
        None

    Raises:
        HTTPException: 400 —— 项目存在未放行的违规/待人工复核记录。
    """
    rows = await db.execute(
        select(ReviewRecord).where(
            ReviewRecord.project_id == project_id,
            ReviewRecord.result.in_(("suspect", "violate")),
        )
    )
    records = rows.scalars().all()
    blocked_violate = [r for r in records if r.result == "violate" and r.manual_status != "approved"]
    blocked_suspect = [
        r for r in records if r.result == "suspect" and r.manual_status in (None, "pending")
    ]
    if blocked_violate:
        first = blocked_violate[0]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"项目存在未放行的违规审核记录（记录 #{first.id}，闸门 {first.gate}），无法发布"),
        )
    if blocked_suspect:
        first = blocked_suspect[0]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"项目存在待人工复核的可疑审核记录（记录 #{first.id}，闸门 {first.gate}），"
                    f"请等待审核通过后再发布"),
        )


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

    闸门二（RD-34，Task 4.2）：先汇总本次合成涉及分镜的 generate 机审记录，
    存在未放行违规/待人工可疑内容时 400 拦截；全部放行才写 compose 通过记录
    并创建合成任务。

    Args:
        project_id: 项目 ID。
        req: 请求体（resolution / aspect_ratio / mode / storyboard_ids /
            episode_id / subtitles / subtitle_style / bgm / transition）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        TaskOut: 新建的 compose 任务。

    Raises:
        HTTPException: 400 —— 项目无分镜/增量清单非法/闸门二拦截；
            403/404 —— 归属校验失败（含 episode_id 不属于该项目）；
            409 —— 积分不足。
    """
    project = await get_owned_project(db, user.id, project_id)
    # S-06：episode_id 传入时校验剧集归属（不属于该项目一律 404）
    if req.episode_id is not None:
        episode = await db.get(Episode, req.episode_id)
        if episode is None or episode.project_id != project.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧集不存在")
    # 解析本次合成涉及的分镜（增量模式按指定清单过滤；episode_id 传入时仅取该剧集分镜）
    involved_ids = await _resolve_involved_storyboard_ids(
        db, project.id, req.storyboard_ids if req.mode == "incremental" else None,
        episode_id=req.episode_id,
    )
    # 闸门二（RD-34）：合成前审核记录真实判定，未放行即拦截
    await _check_compose_gate(db, project.id, involved_ids)
    # 判定通过：落闸门二 compose 通过记录（method=gate 标识闸门聚合判定）
    await create_review_record(
        db, project_id=project.id, gate="compose", target_type="film", result="pass", method="gate"
    )
    task = await create_compose_task(
        db,
        project=project,
        resolution=req.resolution,
        aspect_ratio=req.aspect_ratio or project.aspect_ratio,
        mode=req.mode,
        storyboard_ids=involved_ids if req.mode == "incremental" else None,
        episode_id=req.episode_id,
        subtitles=req.subtitles,
        subtitle_style=req.subtitle_style.model_dump() if req.subtitle_style else None,
        bgm=req.bgm.model_dump() if req.bgm else None,
        transition=req.transition.model_dump() if req.transition else None,
        intro_card=req.intro_card,
        outro_card=req.outro_card,
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
    films = list(rows.scalars().all())
    # 绑定剧集序号映射（成片整合二期 2026-10-22）：版本条目展示「第N集」
    ep_ids = {f.episode_id for f in films if f.episode_id is not None}
    ep_nos: dict = {}
    if ep_ids:
        ep_rows = await db.execute(
            select(Episode.id, Episode.sequence).where(Episode.id.in_(ep_ids))
        )
        ep_nos = {eid: seq for eid, seq in ep_rows.all()}
    return PageResult(
        total=total or 0, page=page, page_size=page_size,
        items=[
            OutputOut.from_output(o, episode_no=ep_nos.get(o.episode_id))
            for o in films
        ],
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
    ep_no: Optional[int] = None
    if film.episode_id is not None:
        ep = await db.get(Episode, film.episode_id)
        ep_no = ep.sequence if ep else None
    return OutputOut.from_output(film, episode_no=ep_no)


@router.delete("/api/projects/{project_id}/outputs/{output_id}")
async def delete_project_output(
    project_id: int,
    output_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除成片版本（成片整合二期 2026-10-22）。

    硬删 film_outputs 记录（storage/outputs 物理文件保留，便于数据恢复与
    其他版本引用不受影响）；已发布且分享仍在有效期的版本须先撤回分享；
    删除当前版本时自动把剩余最新版本置为当前，保证项目恒有可用成片。

    Args:
        project_id: 项目 ID。
        output_id: 目标成片版本 ID。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"ok": True, "new_current_version_no": 删除后接任的当前版本号（可 None）}。

    Raises:
        HTTPException: 403/404 —— 归属校验失败；400 —— 分享未撤回时拦截删除。
    """
    await get_owned_project(db, user.id, project_id)
    film = await _get_owned_output(db, project_id, output_id)
    # 分享保护：分享链接仍有效（已发布 + 未撤销 + 未过期）时禁止删除
    now = datetime.now(timezone.utc)
    if (
        film.share_token
        and not film.share_revoked
        and (film.share_expires_at is None or film.share_expires_at > now)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该版本已发布分享且链接仍有效，请先在分享设置中撤回后再删除",
        )
    was_current = bool(film.is_current)
    await db.delete(film)
    new_current_no: Optional[int] = None
    if was_current:
        # 删除的是当前版本：剩余最新版本自动接任为当前
        rows = await db.execute(
            select(FilmOutput)
            .where(FilmOutput.project_id == project_id)
            .order_by(FilmOutput.version_no.desc())
            .limit(1)
        )
        newest = rows.scalars().first()
        if newest is not None:
            newest.is_current = True
            new_current_no = newest.version_no
    await db.commit()
    return {"ok": True, "new_current_version_no": new_current_no}


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

    闸门三（RD-34，Task 4.2）：发布前汇总项目全部闸门的审核记录，存在未放行
    违规/待人工可疑内容时 400 拦截；全部放行才写 publish 通过记录并推进发布。

    Args:
        project_id: 项目 ID。
        req: 请求体（output_id / share_expires_days / share_password）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        PublishResponse: 分享路径 /share/{token} 与过期时间。

    Raises:
        HTTPException: 400 —— 闸门三拦截；403/404 —— 归属校验失败；
            409 —— 积分/状态冲突（透传）。
    """
    project = await get_owned_project(db, user.id, project_id)
    film = await _get_owned_output(db, project_id, req.output_id)
    # 闸门三（RD-34）：发布前全量审核记录真实判定，未放行即拦截
    await _check_publish_gate(db, project.id)
    # 判定通过：落闸门三 publish 通过记录（method=gate 标识闸门聚合判定）
    await create_review_record(
        db, project_id=project.id, gate="publish", target_type="film", result="pass", method="gate"
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


# ---------------------------------------------------------------------------
# 成片 MP4 下载（Task 7.3）
# ---------------------------------------------------------------------------
# 档位 → 约束边长映射（竖屏取高、横屏取宽；720p 竖屏 9:16 即 720x1280），
# 说明：映射值统一取该档位的"长边"像素（720p→1280 / 1080p→1920 / 2k→2048），
# 竖屏时作为 ffmpeg scale 的目标高度，横屏时作为目标宽度。
_RESOLUTION_TARGETS = {"720p": 1280, "1080p": 1920, "2k": 2048}
# ffmpeg 转码子进程超时（秒）
_FFMPEG_TIMEOUT = 180


def _build_scale_filter(aspect_ratio: str, target: int) -> str:
    """按画幅比例构造 ffmpeg scale 滤镜表达式（等比缩放，另一边取偶数）。

    规则（RD-37 前置约定）：解析 "W:H" 并比较宽高——
    - 横屏（w >= h，如 16:9）：scale={target}:-2，以目标值约束宽度；
    - 竖屏（w < h，如 9:16）：scale=-2:{target}，以目标值约束高度。
    -2 表示保持宽高比并自动取偶数像素（libx264 编码要求）。

    Args:
        aspect_ratio: 项目画幅比例（如 "9:16" / "16:9" / "1:1"）。
        target: 档位对应的约束边长像素（1280/1920/2048）。

    Returns:
        str: 形如 "scale=-2:1280" 的滤镜表达式。

    Raises:
        无：比例解析失败时按竖屏 9:16 处理，不产生异常路径。
    """
    try:
        w_str, h_str = str(aspect_ratio).split(":", 1)
        w, h = int(w_str.strip()), int(h_str.strip())
    except (ValueError, AttributeError):
        w, h = 9, 16
    if w >= h:
        return f"scale={target}:-2"
    return f"scale=-2:{target}"


async def _transcode_film(src: Path, output_id: int, res_key: str,
                          aspect_ratio: str, ffmpeg: str) -> Optional[Path]:
    """把原片转码为指定档位 MP4（子进程放线程池，超时/失败返回 None 降级）。

    输出固定为 storage/exports/film_{output_id}_{res_key}.mp4：同参数产物
    已存在时直接复用，不重复转码。

    Args:
        src: 原片本地物理路径。
        output_id: 成片版本 ID（用于产物命名）。
        res_key: 档位键（720p/1080p/2k）。
        aspect_ratio: 项目画幅比例（决定横竖屏缩放方向）。
        ffmpeg: ffmpeg 可执行文件绝对路径。

    Returns:
        Optional[Path]: 转码产物路径；失败/超时返回 None（调用方降级原片）。

    Raises:
        无：内部捕获全部异常返回 None。
    """
    target = _RESOLUTION_TARGETS[res_key]
    out_dir = settings.storage_path() / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"film_{output_id}_{res_key}.mp4"
    if out_path.exists():
        return out_path
    cmd = [
        ffmpeg, "-y",
        "-i", str(src),
        "-vf", _build_scale_filter(aspect_ratio, target),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac",
        str(out_path),
    ]
    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd,
            timeout=_FFMPEG_TIMEOUT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        # 超时/异常：清理可能的半成品文件，避免下次误复用
        out_path.unlink(missing_ok=True)
        return None
    if proc.returncode != 0 or not out_path.exists():
        # 转码失败：清理残留输出（ffmpeg 失败时可能已创建空/半成品文件）
        out_path.unlink(missing_ok=True)
        return None
    return out_path


@router.get("/api/publish/outputs/{output_id}/download")
async def download_film_output(
    output_id: int,
    resolution: Optional[str] = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """下载成片 MP4（归属链校验：output → project → owner）。

    行为：
    - resolution 传入且有效（720p/1080p/2k）且本机有 ffmpeg：先按项目画幅
      比例转码（storage/exports/film_{id}_{res}.mp4，同参数复用），转码失败
      降级返回原片；
    - 未传 resolution 或本机无 ffmpeg：直接返回原片文件。

    Args:
        output_id: 成片版本 ID。
        resolution: 目标档位（720p/1080p/2k，大小写不敏感；可空）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        FileResponse: video/mp4 文件流（filename 为下载文件名）。

    Raises:
        HTTPException: 400 —— resolution 取值非法；404 —— 成片不存在/
            非本人项目/原片文件缺失或不可下载。
    """
    film = await db.get(FilmOutput, output_id)
    if film is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成片版本不存在")
    project = await db.get(Project, film.project_id)
    # 归属校验：非本人项目或已软删除一律 404，不泄露存在性
    if project is None or project.user_id != user.id or project.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成片版本不存在")

    # 定位原片物理路径（compose 产物为 /storage/... 本地 URL）
    url = film.url or ""
    if not url.startswith("/storage/"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成片文件不可下载")
    src = settings.storage_path() / url.removeprefix("/storage/").lstrip("/")
    if not src.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成片文件不存在")

    res_key = resolution.strip().lower() if resolution else None
    if res_key is not None and res_key not in _RESOLUTION_TARGETS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"resolution 仅支持 {'/'.join(_RESOLUTION_TARGETS)}",
        )

    final_path = src
    if res_key is not None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            # 转码失败（返回 None）则降级返回原片
            final_path = (await _transcode_film(
                src, output_id, res_key, project.aspect_ratio, ffmpeg)) or src

    download_name = f"film_{output_id}_{res_key}.mp4" if res_key else src.name
    return FileResponse(final_path, media_type="video/mp4", filename=download_name)
