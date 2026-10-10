"""用户中心路由模块（文件级说明）。

实现个人资料 / 密码修改 / 用量统计 / 操作日志 / 项目快照查询接口（均需登录）：
- GET    /api/users/me           资料详情
- PUT    /api/users/me           更新头像/邮箱
- PUT    /api/users/me/password  校验旧密码后更新密码
- GET    /api/users/me/usage     项目数 / 任务状态计数 / 本月消耗 / 持有积分
- GET    /api/users/me/creation-stats 今日与本月创作统计（D-04）
- GET    /api/users/me/logs      操作日志分页（倒序）
- GET    /api/users/me/snapshots 指定项目的版本快照（校验项目归属）
"""

from datetime import datetime, timezone
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, hash_password, verify_password
from app.models import OperationLog, Project, Storyboard, Task, User, VersionSnapshot, Wallet, WalletTransaction
from app.schemas.common import PageResult
from app.schemas.user import (
    CreationStatsOut,
    CreationStatsPeriod,
    OperationLogOut,
    PasswordChangeRequest,
    SnapshotOut,
    UsageOut,
    UserOut,
    UserUpdateRequest,
)
from app.api.projects import get_owned_project
from app.services import normalize_pagination

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/me", response_model=UserOut)
async def get_me(user: User = Depends(get_current_user)) -> UserOut:
    """获取当前登录用户的资料详情。

    Args:
        user: 由令牌解析出的当前用户。

    Returns:
        UserOut: 用户资料。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）。
    """
    return UserOut.model_validate(user)


@router.put("/me", response_model=UserOut)
async def update_me(
    req: UserUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    """更新当前用户资料（头像/邮箱，仅更新传入字段）。

    Args:
        req: 更新请求（avatar/email 可空）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        UserOut: 更新后的用户资料。

    Raises:
        HTTPException: 409 —— 新邮箱已被其他用户占用。
    """
    if req.email is not None:
        existed = (
            await db.execute(
                select(User).where(User.email == req.email, User.id != user.id)
            )
        ).scalar_one_or_none()
        if existed is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="邮箱已被其他账号占用")
        user.email = req.email
    if req.avatar is not None:
        user.avatar = req.avatar
    await db.commit()
    return UserOut.model_validate(user)


@router.put("/me/password")
async def change_password(
    req: PasswordChangeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, str]:
    """修改密码：校验旧密码后写入新哈希。

    Args:
        req: 密码修改请求（old_password/new_password）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        Dict[str, str]: {"message": "密码修改成功"}。

    Raises:
        HTTPException: 400 —— 旧密码不正确。
    """
    if not verify_password(req.old_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="旧密码不正确")
    user.password_hash = hash_password(req.new_password)
    await db.commit()
    return {"message": "密码修改成功"}


@router.get("/me/usage", response_model=UsageOut)
async def my_usage(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UsageOut:
    """统计当前用户用量：项目数、各状态任务数、本月消耗与持有积分。

    Args:
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        UsageOut: 用量统计结果（points_used_this_month 为本月 consume 流水合计的绝对值；
            points_total 为当前持有总额 = 可用余额 + 冻结）。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）。
    """
    # 项目数（不含软删除）
    project_count = await db.scalar(
        select(func.count()).select_from(Project).where(
            Project.user_id == user.id, Project.deleted_at.is_(None))
    )
    # 任务状态计数（按项目归属关联到用户）
    status_rows = await db.execute(
        select(Task.status, func.count()).join(Project, Task.project_id == Project.id).where(
            Project.user_id == user.id).group_by(Task.status)
    )
    task_count_by_status = {"queued": 0, "running": 0, "done": 0, "failed": 0}
    for row_status, count in status_rows.all():
        if row_status in task_count_by_status:
            task_count_by_status[row_status] = count
    # 本月 consume 流水合计（amount 为负值，取反得正值消耗）
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    used = await db.scalar(
        select(func.coalesce(func.sum(-WalletTransaction.amount), 0)).where(
            WalletTransaction.user_id == user.id,
            WalletTransaction.type == "consume",
            WalletTransaction.created_at >= month_start,
        )
    )
    # 持有积分总额 = 可用 + 冻结
    wallet = (await db.execute(select(Wallet).where(Wallet.user_id == user.id))).scalar_one_or_none()
    points_total = (wallet.points_balance + wallet.frozen_points) if wallet else 0
    return UsageOut(
        project_count=project_count or 0,
        task_count_by_status=task_count_by_status,
        points_used_this_month=int(used or 0),
        points_total=int(points_total),
    )


@router.get("/me/creation-stats", response_model=CreationStatsOut)
async def my_creation_stats(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CreationStatsOut:
    """查询当前用户今日与本月创作统计（D-04）。

    统计口径：
    - projects：按 projects.created_at 计入（不含软删除）；
    - images / video_seconds：按 tasks 表 type=image/video 且 status=done 聚合，
      任务经 project_id 关联归属用户；video_seconds 按任务关联分镜的
      duration 合计（无关联分镜计 0）；
    - points_spent：wallet_transactions 的 consume 流水合计绝对值。

    Args:
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        CreationStatsOut: {"today": {...}, "month": {...}} 两组统计。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）。
    """
    now = datetime.now(timezone.utc)
    # 今日起点（UTC 自然日 0 点）与本月起点（对齐 my_usage 的口径）
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async def _period_stats(start: datetime) -> CreationStatsPeriod:
        """统计单个周期（今日 / 本月）的四项指标（内部函数）。

        Args:
            start: 周期起点（UTC datetime）。

        Returns:
            CreationStatsPeriod: 该周期统计结果。

        Raises:
            无：聚合查询异常向上抛出由全局处理器兜底。
        """
        # 新建项目数（不含软删除）
        projects = await db.scalar(
            select(func.count()).select_from(Project).where(
                Project.user_id == user.id,
                Project.deleted_at.is_(None),
                Project.created_at >= start,
            )
        ) or 0
        # 完成的关键帧任务数
        images = await db.scalar(
            select(func.count()).select_from(Task)
            .join(Project, Task.project_id == Project.id)
            .where(
                Project.user_id == user.id,
                Task.type == "image",
                Task.status == "done",
                Task.created_at >= start,
            )
        ) or 0
        # 完成的视频任务覆盖时长（按关联分镜 duration 合计）
        video_seconds = await db.scalar(
            select(func.coalesce(func.sum(Storyboard.duration), 0))
            .select_from(Task)
            .join(Project, Task.project_id == Project.id)
            .join(Storyboard, Task.storyboard_id == Storyboard.id)
            .where(
                Project.user_id == user.id,
                Task.type == "video",
                Task.status == "done",
                Task.created_at >= start,
            )
        ) or 0
        # 消耗积分（consume 流水为负值，取反）
        points = await db.scalar(
            select(func.coalesce(func.sum(-WalletTransaction.amount), 0)).where(
                WalletTransaction.user_id == user.id,
                WalletTransaction.type == "consume",
                WalletTransaction.created_at >= start,
            )
        ) or 0
        return CreationStatsPeriod(
            projects=int(projects),
            images=int(images),
            video_seconds=int(video_seconds),
            points_spent=int(points),
        )

    return CreationStatsOut(
        today=await _period_stats(today_start),
        month=await _period_stats(month_start),
    )


@router.get("/me/logs", response_model=PageResult[OperationLogOut])
async def my_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PageResult[OperationLogOut]:
    """分页查询当前用户操作日志（按创建时间倒序）。

    Args:
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PageResult[OperationLogOut]: 分页日志列表。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）。
    """
    page, page_size = normalize_pagination(page, page_size)
    total = await db.scalar(
        select(func.count()).select_from(OperationLog).where(OperationLog.user_id == user.id)
    )
    rows = await db.execute(
        select(OperationLog).where(OperationLog.user_id == user.id)
        .order_by(OperationLog.created_at.desc(), OperationLog.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return PageResult(
        total=total or 0, page=page, page_size=page_size,
        items=[OperationLogOut.model_validate(r) for r in rows.scalars().all()],
    )


@router.get("/me/snapshots", response_model=PageResult[SnapshotOut])
async def my_snapshots(
    project_id: int = Query(..., description="项目 ID，用于校验归属"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PageResult[SnapshotOut]:
    """分页查询指定项目的版本快照（校验项目归属，他人/不存在项目返回 404）。

    Args:
        project_id: 项目 ID（必填）。
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PageResult[SnapshotOut]: 快照分页列表（按创建时间倒序）。

    Raises:
        HTTPException: 404 —— 项目不存在、已软删除或不属于当前用户。
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
