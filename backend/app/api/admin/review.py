"""管理端审核路由模块（文件级说明）。

实现内容审核队列与人工复核（对应三级闸门 RD-34：generate → compose → publish）：
- GET  /api/admin/review?tab=pending|passed|rejected&gate=   审核队列分页
- POST /api/admin/review/{record_id}/handle                  人工复核（approve/reject）

队列口径：
- pending  ：manual_status IS NULL 且 result IN ('suspect', 'violate')——机审可疑/违规待人工；
- passed   ：manual_status = 'approved'；
- rejected ：manual_status = 'rejected'。

全部接口要求运营方角色，普通用户 403。
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_operator
from app.models import Project, ReviewRecord, Storyboard, User
from app.schemas.admin import MessageOut, ReviewHandleRequest, ReviewItem
from app.schemas.common import PageResult
from app.services import normalize_pagination, notification_service

router = APIRouter(
    prefix="/api/admin/review",
    tags=["admin-review"],
    dependencies=[Depends(require_operator)],  # 路由级鉴权：仅运营方可访问，普通用户 403
)

# tab 取值与队列过滤条件的映射说明（实现见 list_reviews）
_TAB_VALUES = ("pending", "passed", "rejected")


@router.get("", response_model=PageResult[ReviewItem])
async def list_reviews(
    tab: str = Query(default="pending", description="队列：pending 待复核 / passed 已通过 / rejected 已驳回"),
    gate: Optional[str] = Query(default=None, description="闸门过滤：generate/compose/publish"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> PageResult[ReviewItem]:
    """分页查询审核队列（创建时间倒序），联项目标题与分镜镜头序号。

    Args:
        tab: 队列类型（pending/passed/rejected，非法返回 400）。
        gate: 闸门过滤（可选）。
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        PageResult[ReviewItem]: 审核记录分页列表。

    Raises:
        HTTPException: 400 —— tab 或 gate 取值非法。
    """
    if tab not in _TAB_VALUES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"tab 仅支持 {'/'.join(_TAB_VALUES)}"
        )
    if gate is not None and gate not in ("generate", "compose", "publish"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="gate 仅支持 generate/compose/publish"
        )
    page, page_size = normalize_pagination(page, page_size)

    # 队列过滤条件
    if tab == "pending":
        condition = ReviewRecord.manual_status.is_(None) & ReviewRecord.result.in_(("suspect", "violate"))
    elif tab == "passed":
        condition = ReviewRecord.manual_status == "approved"
    else:
        condition = ReviewRecord.manual_status == "rejected"
    conditions = [condition]
    if gate is not None:
        conditions.append(ReviewRecord.gate == gate)

    # 联表：项目标题 + 分镜镜头序号（target_id 多态引用，与 storyboards.id 匹配即回填序号）
    base_query = (
        select(ReviewRecord, Project.title.label("project_title"), Storyboard.sequence.label("sb_seq"))
        .outerjoin(Project, Project.id == ReviewRecord.project_id)
        .outerjoin(Storyboard, Storyboard.id == ReviewRecord.target_id)
        .where(*conditions)
    )
    total = await db.scalar(
        select(func.count())
        .select_from(ReviewRecord)
        .outerjoin(Project, Project.id == ReviewRecord.project_id)
        .where(*conditions)
    ) or 0
    rows = await db.execute(
        base_query.order_by(ReviewRecord.created_at.desc(), ReviewRecord.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = [
        ReviewItem(
            id=record.id,
            project_id=record.project_id,
            project_title=project_title,
            target_type=record.target_type,
            target_id=record.target_id,
            storyboard_sequence=sb_seq,
            gate=record.gate,
            result=record.result,
            violation_type=record.violation_type,
            manual_status=record.manual_status,
            handler_id=record.handler_id,
            suggestion=record.suggestion,
            created_at=record.created_at,
        )
        for record, project_title, sb_seq in rows.all()
    ]
    return PageResult(total=int(total), page=page, page_size=page_size, items=items)


@router.post("/{record_id}/handle", response_model=ReviewItem)
async def handle_review(
    record_id: int,
    req: ReviewHandleRequest,
    db: AsyncSession = Depends(get_db),
    operator: User = Depends(require_operator),
) -> ReviewItem:
    """人工复核审核记录：approve 置 manual_status='approved'，reject 置 'rejected'。

    驳回时 suggestion（可操作建议，RD-36）必填；处理人记录为当前管理员。

    Args:
        record_id: 审核记录 ID。
        req: 复核请求（action + 可选 suggestion）。
        db: 请求级异步数据库会话。
        operator: 当前运营方（作为 handler_id 落库）。

    Returns:
        ReviewItem: 复核后的审核记录。

    Raises:
        HTTPException: 400 —— action 非法或 reject 未附 suggestion；
            404 —— 审核记录不存在。
    """
    if req.action not in ("approve", "reject"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="action 仅支持 approve/reject"
        )
    if req.action == "reject" and not (req.suggestion and req.suggestion.strip()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="驳回时必须填写处理建议（suggestion）"
        )

    record = await db.get(ReviewRecord, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="审核记录不存在")

    record.manual_status = "approved" if req.action == "approve" else "rejected"
    record.handler_id = operator.id
    if req.suggestion:
        record.suggestion = req.suggestion
    await db.commit()

    # 通知中心：处置成功后向内容归属用户推送审核结果通知
    # （notify_review_result 自管理会话且内部吞异常，不影响处置主流程）
    owner_id = await db.scalar(select(Project.user_id).where(Project.id == record.project_id)) \
        if record.project_id else None
    if owner_id:
        await notification_service.notify_review_result(
            owner_id,
            record.id,
            result=record.manual_status,
            suggestion=record.suggestion,
        )

    # 联表回填项目标题与分镜序号后返回
    project_title = None
    if record.project_id:
        project_title = await db.scalar(select(Project.title).where(Project.id == record.project_id))
    sb_seq = await db.scalar(
        select(Storyboard.sequence).where(Storyboard.id == record.target_id)
    ) if record.target_id else None

    return ReviewItem(
        id=record.id,
        project_id=record.project_id,
        project_title=project_title,
        target_type=record.target_type,
        target_id=record.target_id,
        storyboard_sequence=sb_seq,
        gate=record.gate,
        result=record.result,
        violation_type=record.violation_type,
        manual_status=record.manual_status,
        handler_id=record.handler_id,
        suggestion=record.suggestion,
        created_at=record.created_at,
    )
