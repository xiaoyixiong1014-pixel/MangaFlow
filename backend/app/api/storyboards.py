"""分镜路由模块（文件级说明）。

实现分镜域全部接口（每个接口先校验项目归属，他人或已删除项目一律 404）：
- POST /api/projects/{project_id}/storyboards/decompose          AI 拆解（全量重建）
- GET  /api/projects/{project_id}/storyboards                    分镜列表（按 sequence）
- PUT  /api/projects/{project_id}/storyboards/{storyboard_id}    更新（character_links 整体替换）
- POST /api/projects/{project_id}/storyboards/reorder            重排 sequence
- POST /api/projects/{project_id}/storyboards/{storyboard_id}/split    按对白行拆分
- POST /api/projects/{project_id}/storyboards/{storyboard_id}/merge    合并两镜
- POST /api/projects/{project_id}/storyboards/warnings           告警检查
- POST /api/projects/{project_id}/storyboards/{storyboard_id}/regen    重生成计数（RD-25）
- POST /api/projects/{project_id}/storyboards/deliver-to-canvas  投递画布

注意：路由需携带 {project_id} 路径参数，FastAPI router prefix 不支持路径参数，
因此本路由不设 prefix，路由内部书写完整路径；静态路径段（reorder/warnings 等）
注册在带 {storyboard_id} 的路径之前以避免误匹配。
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import User
from app.schemas.storyboard import (
    DeliverCanvasOut,
    DeliverCanvasRequest,
    MergeRequest,
    ReorderRequest,
    SplitRequest,
    StoryboardOut,
    StoryboardRegenOut,
    StoryboardUpdateRequest,
    StoryboardWarningsOut,
)
from app.services.script_service import get_owned_project
from app.services.storyboard_service import (
    decompose_storyboards,
    deliver_to_canvas,
    list_storyboards,
    merge_storyboards,
    reorder_storyboards,
    regen_storyboard,
    split_storyboard,
    storyboard_warnings,
    update_storyboard,
)

router = APIRouter(tags=["storyboards"])


@router.post("/api/projects/{project_id}/storyboards/decompose")
async def decompose(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """AI 拆解：读剧本 content.scenes 逐场景生成 1~3 镜（LLM 失败规则降级 2 镜），
    全量重建语义（先删后建），project.status='storyboard'。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"total": 新建分镜数, "degraded": 是否发生过规则降级}。

    Raises:
        HTTPException: 400 —— 无剧本或剧本无场景数据；404 —— 项目不存在/非本人。
    """
    project = await _owned(db, project_id, user)
    return await decompose_storyboards(db, project)


@router.post("/api/projects/{project_id}/storyboards/reorder")
async def reorder(
    project_id: int,
    req: ReorderRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """按 ordered_ids 顺序重写 sequence（1..n，须恰好覆盖项目全部分镜）。

    Args:
        project_id: 项目 ID。
        req: 重排请求（ordered_ids）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"total": 重排分镜数}。

    Raises:
        HTTPException: 400 —— ordered_ids 未恰好覆盖全部分镜；404 —— 项目不存在。
    """
    project = await _owned(db, project_id, user)
    total = await reorder_storyboards(db, project, req.ordered_ids)
    return {"total": total}


@router.post("/api/projects/{project_id}/storyboards/warnings", response_model=StoryboardWarningsOut)
async def warnings(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StoryboardWarningsOut:
    """分镜告警检查：总时长偏差>±15%、单镜>10s、实体未强绑资产（scene_id 为 NULL）。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StoryboardWarningsOut: {"total_duration","target_duration","over_ratio","warnings"}。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人。
    """
    project = await _owned(db, project_id, user)
    return StoryboardWarningsOut.model_validate(await storyboard_warnings(db, project))


@router.post("/api/projects/{project_id}/storyboards/deliver-to-canvas", response_model=DeliverCanvasOut)
async def deliver_to_canvas_endpoint(
    project_id: int,
    req: Optional[DeliverCanvasRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeliverCanvasOut:
    """分镜投递画布：建 studio_linked 画布 + text/asset_ref/generate 三节点 +
    text→generate、asset_ref→generate 两条连线。

    Args:
        project_id: 项目 ID。
        req: 投递请求（storyboard_id 可缺省，缺省取第一镜）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        DeliverCanvasOut: {"canvas_id": 新建画布 ID}。

    Raises:
        HTTPException: 400 —— 项目尚无分镜；404 —— 项目或分镜不存在。
    """
    project = await _owned(db, project_id, user)
    body = req or DeliverCanvasRequest()
    canvas_id = await deliver_to_canvas(db, user, project, body.storyboard_id)
    return DeliverCanvasOut(canvas_id=canvas_id)


@router.get("/api/projects/{project_id}/storyboards", response_model=List[StoryboardOut])
async def list_storyboards_endpoint(
    project_id: int,
    episode_id: Optional[int] = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[StoryboardOut]:
    """查询分镜列表（按 sequence 升序，每项含 characters 与 scene_name）。

    Args:
        project_id: 项目 ID。
        episode_id: 剧集过滤（可空）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        List[StoryboardOut]: 分镜列表。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人。
    """
    project = await _owned(db, project_id, user)
    items = await list_storyboards(db, project, episode_id)
    return [StoryboardOut.model_validate(i) for i in items]


@router.put("/api/projects/{project_id}/storyboards/{storyboard_id}", response_model=StoryboardOut)
async def update_storyboard_endpoint(
    project_id: int,
    storyboard_id: int,
    req: StoryboardUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StoryboardOut:
    """更新分镜字段；character_links 提供时整体替换角色关联。

    Args:
        project_id: 项目 ID。
        storyboard_id: 分镜 ID。
        req: 更新请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StoryboardOut: 更新后的分镜信息。

    Raises:
        HTTPException: 400 —— scene_id/角色/变体引用非法；404 —— 分镜不存在。
    """
    project = await _owned(db, project_id, user)
    result = await update_storyboard(db, project, storyboard_id, req)
    return StoryboardOut.model_validate(result)


@router.post("/api/projects/{project_id}/storyboards/{storyboard_id}/split", response_model=List[StoryboardOut])
async def split_storyboard_endpoint(
    project_id: int,
    storyboard_id: int,
    req: SplitRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[StoryboardOut]:
    """按对白行拆分为两镜（前 at_line 行留本镜，其余进新镜，sequence 重排）。

    Args:
        project_id: 项目 ID。
        storyboard_id: 分镜 ID。
        req: 拆分请求（at_line）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        List[StoryboardOut]: [拆分后的本镜, 新镜]。

    Raises:
        HTTPException: 400 —— 对白不足两行或行号越界；404 —— 分镜不存在。
    """
    project = await _owned(db, project_id, user)
    items = await split_storyboard(db, project, storyboard_id, req.at_line)
    return [StoryboardOut.model_validate(i) for i in items]


@router.post("/api/projects/{project_id}/storyboards/{storyboard_id}/merge", response_model=StoryboardOut)
async def merge_storyboard_endpoint(
    project_id: int,
    storyboard_id: int,
    req: MergeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StoryboardOut:
    """合并两镜：dialogue 拼接、时长相加、删除 other，sequence 重排。

    Args:
        project_id: 项目 ID。
        storyboard_id: 保留的分镜 ID。
        req: 合并请求（other_id）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StoryboardOut: 合并后的分镜信息。

    Raises:
        HTTPException: 400 —— 与自身合并；404 —— 分镜不存在。
    """
    project = await _owned(db, project_id, user)
    result = await merge_storyboards(db, project, storyboard_id, req.other_id)
    return StoryboardOut.model_validate(result)


@router.post("/api/projects/{project_id}/storyboards/{storyboard_id}/regen", response_model=StoryboardRegenOut)
async def regen_storyboard_endpoint(
    project_id: int,
    storyboard_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StoryboardRegenOut:
    """分镜重生成计数：regen_count += 1；达到 3 次时 suggest_canvas=True（RD-25）。

    Args:
        project_id: 项目 ID。
        storyboard_id: 分镜 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StoryboardRegenOut: {"regen_count", "suggest_canvas"}。

    Raises:
        HTTPException: 404 —— 分镜不存在。
    """
    project = await _owned(db, project_id, user)
    return StoryboardRegenOut.model_validate(await regen_storyboard(db, project, storyboard_id))


async def _owned(db: AsyncSession, project_id: int, user: User):
    """校验项目归属并返回项目对象（本路由共用，统一 404）。

    Args:
        db: 请求级异步数据库会话。
        project_id: 项目 ID。
        user: 当前用户。

    Returns:
        Project: 归属当前用户的项目 ORM 对象。

    Raises:
        HTTPException: 404 —— 项目不存在、已删除或不属于当前用户。
    """
    return await get_owned_project(db, project_id, user.id)
