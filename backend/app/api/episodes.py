"""剧集管理路由模块（文件级说明，S-06）。

实现剧集 CRUD / 重排接口（全部先校验项目归属，他人或已删除项目一律 404，
不泄露存在性；剧集不属于该项目同样 404）：
- POST   /api/projects/{project_id}/episodes                 创建（sequence 自动取项目内 max+1）
- GET    /api/projects/{project_id}/episodes                 列表（按 sequence 升序）
- PUT    /api/projects/{project_id}/episodes/reorder         重排（按有序 ID 清单重写 sequence）
- PATCH  /api/projects/{project_id}/episodes/{episode_id}    更新（title/status）
- DELETE /api/projects/{project_id}/episodes/{episode_id}    硬删剧集行（引用行 episode_id 置 NULL 保留数据）

注意：FastAPI 的 router prefix 不能携带路径参数，因此本路由不设 prefix，
路由内部直接书写完整路径；reorder 静态路径注册在 {episode_id} 动态路径之前。
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Episode, FilmOutput, Project, Script, Storyboard, User
from app.schemas.project import (
    EPISODE_STATUSES,
    EpisodeCreateRequest,
    EpisodeOut,
    EpisodeReorderRequest,
    EpisodeUpdateRequest,
)
from app.services.script_service import get_owned_project

router = APIRouter(tags=["episodes"])


async def _get_owned_episode(
    db: AsyncSession, project: Project, episode_id: int
) -> Episode:
    """校验并返回归属于指定项目的剧集（跨接口共用）。

    Args:
        db: 请求级异步数据库会话。
        project: 已校验归属的项目。
        episode_id: 剧集 ID。

    Returns:
        Episode: 剧集 ORM 对象。

    Raises:
        HTTPException: 404 —— 剧集不存在或不属于该项目（统一 404，
            避免泄露他人项目剧集的存在性）。
    """
    episode = await db.get(Episode, episode_id)
    if episode is None or episode.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧集不存在")
    return episode


@router.post("/api/projects/{project_id}/episodes", response_model=EpisodeOut)
async def create_episode(
    project_id: int,
    req: EpisodeCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EpisodeOut:
    """创建剧集（sequence 自动取项目内现有最大序号 + 1，初始状态 draft）。

    Args:
        project_id: 项目 ID。
        req: 创建请求（title 必填）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        EpisodeOut: 新建剧集信息。

    Raises:
        HTTPException: 404 —— 项目不存在/已删除/非本人项目；401 —— 未认证。
    """
    project = await get_owned_project(db, project_id, user.id)
    max_seq = await db.scalar(
        select(func.max(Episode.sequence)).where(Episode.project_id == project.id)
    )
    episode = Episode(
        project_id=project.id,
        title=req.title.strip(),
        sequence=(max_seq or 0) + 1,
        status="draft",
    )
    db.add(episode)
    await db.commit()
    await db.refresh(episode)
    return EpisodeOut.model_validate(episode)


@router.get("/api/projects/{project_id}/episodes", response_model=List[EpisodeOut])
async def list_episodes(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[EpisodeOut]:
    """查询项目剧集列表（按 sequence 升序）。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        List[EpisodeOut]: 剧集列表（sequence 升序，平局按 id 升序）。

    Raises:
        HTTPException: 404 —— 项目不存在/已删除/非本人项目。
    """
    project = await get_owned_project(db, project_id, user.id)
    rows = await db.execute(
        select(Episode)
        .where(Episode.project_id == project.id)
        .order_by(Episode.sequence.asc(), Episode.id.asc())
    )
    return [EpisodeOut.model_validate(r) for r in rows.scalars().all()]


@router.put("/api/projects/{project_id}/episodes/reorder")
async def reorder_episodes(
    project_id: int,
    req: EpisodeReorderRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """按 ordered_ids 顺序重写 sequence（1..n）。

    校验规则：ordered_ids 须恰好覆盖项目全部剧集且无重复；含其他项目的
    剧集 ID 一律 400 拒绝。因 uk_episodes_seq 对 (project_id, sequence)
    唯一，先统一写入临时负值避开逐行唯一冲突，再按目标顺序回写 1..n。

    Args:
        project_id: 项目 ID。
        req: 重排请求（ordered_ids）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"total": 重排剧集数}。

    Raises:
        HTTPException: 400 —— 清单未恰好覆盖全部剧集 / 含重复 / 含不属于
            该项目的剧集；404 —— 项目不存在/非本人。
    """
    project = await get_owned_project(db, project_id, user.id)
    episodes = (
        await db.execute(select(Episode).where(Episode.project_id == project.id))
    ).scalars().all()
    by_id = {e.id: e for e in episodes}

    id_list = list(req.ordered_ids)
    if len(set(id_list)) != len(id_list):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ordered_ids 含重复剧集 ID")
    unknown = [eid for eid in id_list if eid not in by_id]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"ordered_ids 含不属于该项目的剧集：{unknown[:5]}",
        )
    if len(id_list) != len(by_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ordered_ids 未恰好覆盖项目全部剧集，无法重排",
        )

    # 两阶段写入：先全部置临时负值（负值同样受唯一约束保护，但与 1..n 不冲突），
    # flush 后再按目标顺序回写 1..n，避免逐行 UPDATE 触发 uk_episodes_seq 冲突
    for episode in episodes:
        episode.sequence = -(episode.id)
    await db.flush()
    for seq, episode_id in enumerate(id_list, start=1):
        by_id[episode_id].sequence = seq
    await db.commit()
    return {"total": len(id_list)}


@router.patch("/api/projects/{project_id}/episodes/{episode_id}", response_model=EpisodeOut)
async def update_episode(
    project_id: int,
    episode_id: int,
    req: EpisodeUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EpisodeOut:
    """更新剧集字段（仅更新传入字段；status 仅允许合法白名单值）。

    Args:
        project_id: 项目 ID。
        episode_id: 剧集 ID。
        req: 更新请求（title/status 可空，None 表示不修改）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        EpisodeOut: 更新后的剧集信息。

    Raises:
        HTTPException: 400 —— status 取值非法；404 —— 项目或剧集不存在/非本人。
    """
    project = await get_owned_project(db, project_id, user.id)
    episode = await _get_owned_episode(db, project, episode_id)
    if req.status is not None:
        if req.status not in EPISODE_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"status 非法，仅允许 {'/'.join(EPISODE_STATUSES)}",
            )
        episode.status = req.status
    if req.title is not None:
        episode.title = req.title.strip()
    await db.commit()
    await db.refresh(episode)
    return EpisodeOut.model_validate(episode)


@router.delete("/api/projects/{project_id}/episodes/{episode_id}")
async def delete_episode(
    project_id: int,
    episode_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除剧集（硬删 episodes 行；引用该剧集的数据行保留并解除绑定）。

    解绑规则：scripts / storyboards / film_outputs 中 episode_id 指向该剧集的
    行统一置 NULL（三表外键本就 ondelete=SET NULL，显式置 NULL 以兼容未声明
    数据库级外键的存量环境），数据本体不删除；删除后该剧集不可恢复。

    Args:
        project_id: 项目 ID。
        episode_id: 剧集 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"message": "剧集已删除", "unbound": 解绑引用行数}。

    Raises:
        HTTPException: 404 —— 项目或剧集不存在/非本人。
    """
    project = await get_owned_project(db, project_id, user.id)
    episode = await _get_owned_episode(db, project, episode_id)

    # 引用该剧集的行保留数据、episode_id 置 NULL（与外键 SET NULL 语义一致）
    unbound = 0
    for model in (Script, Storyboard, FilmOutput):
        rows = await db.execute(
            select(model).where(model.episode_id == episode.id, model.project_id == project.id)
        )
        refs = rows.scalars().all()
        for row in refs:
            row.episode_id = None
        unbound += len(refs)

    await db.delete(episode)
    await db.commit()
    return {"message": "剧集已删除", "unbound": unbound}
