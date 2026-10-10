"""管理端任务死信队列路由模块（文件级说明）。

实现重试耗尽任务（tasks.dead_letter=True）的运营管理：
- GET  /api/admin/tasks/dead-letters          死信任务分页列表
- POST /api/admin/tasks/{task_id}/requeue     死信任务重新入队

重新入队语义（Task 2.2）：
- 任务终态失败时冻结积分已退还，requeue 需重新冻结积分（余额不足返回 400）；
- 重置 dead_letter=False / status=queued / retry_count=0 / next_retry_at=NULL /
  错误信息清空，worker 将按优先级重新领取执行；
- reason 必填并写入 operation_logs 审计留痕。

全部接口要求运营方角色，普通用户 403。
"""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_operator
from app.models import OperationLog, Project, Task, User
from app.schemas.admin import DeadLetterItem, RequeueRequest, RequeueResponse
from app.services import normalize_pagination
from app.services.billing_service import freeze_points

router = APIRouter(
    prefix="/api/admin/tasks",
    tags=["admin-dlq"],
    dependencies=[Depends(require_operator)],  # 路由级鉴权：仅运营方可访问，普通用户 403
)


@router.get("/dead-letters")
async def list_dead_letters(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> dict:
    """分页查询死信任务列表（创建时间倒序），联项目回填归属用户。

    Args:
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        dict: PageResult 风格分页结构（total/page/page_size/items）。

    Raises:
        HTTPException: 401/403 —— 未认证或无运营方权限（由依赖抛出）。
    """
    page, page_size = normalize_pagination(page, page_size)
    # 联表：经项目回填归属用户 ID（项目已删除时 user_id 为 NULL）
    base_query = (
        select(Task, Project.user_id.label("owner_id"))
        .outerjoin(Project, Project.id == Task.project_id)
        .where(Task.dead_letter.is_(True))
    )
    total = await db.scalar(
        select(func.count())
        .select_from(Task)
        .outerjoin(Project, Project.id == Task.project_id)
        .where(Task.dead_letter.is_(True))
    ) or 0
    rows = await db.execute(
        base_query.order_by(Task.created_at.desc(), Task.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [
        DeadLetterItem(
            id=task.id,
            type=task.type,
            status=task.status,
            error_code=task.error_code,
            error=task.error_message,
            retry_count=task.retry_count or 0,
            cost_points=task.cost_points or 0,
            created_at=task.created_at,
            project_id=task.project_id,
            user_id=owner_id,
        )
        for task, owner_id in rows.all()
    ]
    return {"total": int(total), "page": page, "page_size": page_size, "items": items}


@router.post("/{task_id}/requeue", response_model=RequeueResponse)
async def requeue_dead_letter(
    task_id: int,
    req: RequeueRequest,
    db: AsyncSession = Depends(get_db),
    operator: User = Depends(require_operator),
) -> RequeueResponse:
    """将死信任务重新入队：重新冻结积分并重置调度状态。

    Args:
        task_id: 任务 ID。
        req: 重新入队请求（reason 必填，写入审计日志）。
        db: 请求级异步数据库会话。
        operator: 当前运营方（作为审计操作人落库）。

    Returns:
        RequeueResponse: 重置后的任务 ID 与状态。

    Raises:
        HTTPException: 404 —— 任务不存在；400 —— 任务不在死信队列、
            归属项目已删除无法结算，或用户积分余额不足无法重新冻结。
    """
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    if not task.dead_letter:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="任务不在死信队列中，无法重新入队"
        )

    # 定位归属用户（终态退款已回钱包，requeue 需从其钱包重新冻结）
    owner_id: Optional[int] = None
    if task.project_id:
        owner_id = await db.scalar(select(Project.user_id).where(Project.id == task.project_id))
    if owner_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="任务归属项目不存在或已删除，无法重新入队结算",
        )

    # 重新冻结积分（幂等键带随机后缀：同一任务可被多次死信/重入队）
    if task.cost_points and task.cost_points > 0:
        try:
            await freeze_points(
                db,
                user_id=owner_id,
                amount=task.cost_points,
                ref_task_id=task.id,
                idempotency_key=f"task_{task.id}_requeue_{uuid.uuid4().hex[:8]}",
            )
        except HTTPException as exc:
            # freeze_points 余额不足抛 409：对外统一转为 400 语义
            if exc.status_code == status.HTTP_409_CONFLICT:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="用户积分余额不足，无法重新冻结积分",
                )
            raise

    # 重置调度状态：退出死信、清空退避与错误信息、重试计数归零
    task.dead_letter = False
    task.status = "queued"
    task.retry_count = 0
    task.next_retry_at = None
    task.error_code = None
    task.error_message = None
    task.progress = 0

    # 高危操作审计留痕
    db.add(OperationLog(
        user_id=operator.id,
        project_id=task.project_id,
        action="admin_requeue_task",
        detail={
            "task_id": task.id,
            "task_type": task.type,
            "cost_points": task.cost_points,
            "target_user_id": owner_id,
            "reason": req.reason,
        },
        risk_level="normal",
    ))
    await db.commit()
    return RequeueResponse(id=task.id, status=task.status, message="任务已重新入队")
