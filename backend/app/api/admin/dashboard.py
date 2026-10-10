"""管理端仪表盘路由模块（文件级说明）。

实现平台运营数据概览、运营告警与系统运行指标：
- GET /api/admin/dashboard/overview?range=7d|30d|90d  平台总览统计（用户/营收/积分负债/任务/逐日趋势）
- GET /api/admin/dashboard/alerts                     运营告警（平台故障/大额订单/渠道不健康）
- GET /api/admin/system-metrics                       系统运行指标（24h 任务成功率/死信/队列积压/渠道健康）

全部接口要求运营方（operator）角色，普通用户访问返回 403。
"""

from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_operator
from app.models import ModelChannel, Order, Task, User, Wallet
from app.schemas.admin import AlertItem, DayGeneration, DayRevenue, OverviewOut

# 路由级鉴权：仅运营方可访问，普通用户 403；
# prefix 为 /api/admin（旧接口以 dashboard/ 前缀保持原路径不变，新接口 /system-metrics）
router = APIRouter(
    prefix="/api/admin",
    tags=["admin-dashboard"],
    dependencies=[Depends(require_operator)],
)

# 支持的统计范围取值
_RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90}

# 平台故障错误码（任务级平台故障，RD-28 错误码区分约定）
_PLATFORM_ERROR_CODE = "platform_error"

# 大额订单阈值（元）：paid 订单金额超过该值触发告警
_LARGE_ORDER_THRESHOLD = 1000


@router.get("/dashboard/overview", response_model=OverviewOut)
async def get_overview(
    range_preset: str = Query(default="7d", alias="range", description="统计范围：7d/30d/90d"),
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> OverviewOut:
    """查询平台运营总览统计。

    统计口径：
    - users_total/users_new：全量用户数与范围内注册数；
    - revenue_total/orders_paid：status='paid' 订单金额合计与单数；
    - points_liability：全部钱包可用+冻结积分之和（平台积分负债）；
    - tasks_total/tasks_failed：任务总数与失败数；
    - revenue_by_day / generation_by_day：按 func.date(created_at) 分组聚合，
      范围内缺失日期补 0，保证前端图表连续。

    Args:
        range_preset: 统计范围（7d/30d/90d，别名 range，非法返回 400）。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        OverviewOut: 平台概览统计。

    Raises:
        HTTPException: 400 —— range 取值非法。
    """
    if range_preset not in _RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="range 仅支持 7d/30d/90d"
        )
    days = _RANGE_DAYS[range_preset]
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    # ---------- 总量统计（单条 SELECT 多标量） ----------
    users_total = await db.scalar(select(func.count()).select_from(User)) or 0
    users_new = await db.scalar(
        select(func.count()).select_from(User).where(User.created_at >= start)
    ) or 0
    revenue_total = await db.scalar(
        select(func.coalesce(func.sum(Order.amount), 0)).where(Order.status == "paid")
    ) or 0
    orders_paid = await db.scalar(
        select(func.count()).select_from(Order).where(Order.status == "paid")
    ) or 0
    points_liability = await db.scalar(
        select(func.coalesce(func.sum(Wallet.points_balance + Wallet.frozen_points), 0))
    ) or 0
    tasks_total = await db.scalar(select(func.count()).select_from(Task)) or 0
    tasks_failed = await db.scalar(
        select(func.count()).select_from(Task).where(Task.status == "failed")
    ) or 0

    # ---------- 逐日营收（paid 订单按日期分组聚合） ----------
    revenue_rows = await db.execute(
        select(
            func.date(Order.created_at).label("day"),
            func.coalesce(func.sum(Order.amount), 0).label("amount"),
        )
        .where(Order.status == "paid", Order.created_at >= start)
        .group_by(func.date(Order.created_at))
    )
    revenue_map = {str(row.day): float(row.amount) for row in revenue_rows.all()}

    # ---------- 逐日生成量（image/video/tts 任务按日期分组，条件聚合） ----------
    gen_rows = await db.execute(
        select(
            func.date(Task.created_at).label("day"),
            func.count(Task.id).filter(Task.type == "image").label("image"),
            func.count(Task.id).filter(Task.type == "video").label("video"),
            func.count(Task.id).filter(Task.type == "tts").label("tts"),
        )
        .where(Task.created_at >= start, Task.type.in_(("image", "video", "tts")))
        .group_by(func.date(Task.created_at))
    )
    gen_map = {
        str(row.day): {"image": row.image, "video": row.video, "tts": row.tts}
        for row in gen_rows.all()
    }

    # 补齐范围内每一天（含今天），缺失日期补 0，保证图表 x 轴连续
    revenue_by_day: List[DayRevenue] = []
    generation_by_day: List[DayGeneration] = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).date()
        key = day.isoformat()
        revenue_by_day.append(DayRevenue(date=key, amount=revenue_map.get(key, 0.0)))
        counts = gen_map.get(key, {})
        generation_by_day.append(DayGeneration(
            date=key,
            image=counts.get("image", 0),
            video=counts.get("video", 0),
            tts=counts.get("tts", 0),
        ))

    return OverviewOut(
        users_total=int(users_total),
        users_new=int(users_new),
        revenue_total=float(revenue_total),
        orders_paid=int(orders_paid),
        points_liability=int(points_liability),
        tasks_total=int(tasks_total),
        tasks_failed=int(tasks_failed),
        revenue_by_day=revenue_by_day,
        generation_by_day=generation_by_day,
    )


@router.get("/dashboard/alerts", response_model=List[AlertItem])
async def get_alerts(
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> List[AlertItem]:
    """查询运营告警列表（三类告警合并返回，按时间倒序）。

    告警来源：
    - platform_error：近 24h 内 error_code='platform_error' 的失败任务（级别 error）；
    - large_order：已支付且金额 > 1000 元的订单（级别 warning）；
    - channel_unhealthy：健康状态非 healthy 的模型渠道（级别 warning）。

    Args:
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        List[AlertItem]: 告警列表（可能为空）。

    Raises:
        无：查询异常向上抛出由全局处理器兜底。
    """
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    alerts: List[AlertItem] = []

    # ---------- 1. 近 24h 平台故障任务 ----------
    failed_tasks = (
        await db.execute(
            select(Task).where(
                Task.error_code == _PLATFORM_ERROR_CODE,
                Task.created_at >= day_ago,
            ).order_by(Task.created_at.desc())
        )
    ).scalars().all()
    for t in failed_tasks:
        alerts.append(AlertItem(
            level="error",
            type="platform_error",
            message=(
                f"任务 #{t.id}（{t.type}/{t.phase}）发生平台故障："
                f"{t.error_message or '无错误详情'}"
            ),
            created_at=t.created_at,
        ))

    # ---------- 2. 大额已支付订单（金额 > 1000 元） ----------
    large_orders = (
        await db.execute(
            select(Order).where(Order.status == "paid", Order.amount > _LARGE_ORDER_THRESHOLD)
            .order_by(Order.paid_at.desc().nullslast()).limit(50)
        )
    ).scalars().all()
    for o in large_orders:
        alerts.append(AlertItem(
            level="warning",
            type="large_order",
            message=f"订单 {o.order_no} 金额 {float(o.amount):.2f} 元，超过 {_LARGE_ORDER_THRESHOLD} 元阈值",
            created_at=o.paid_at or o.created_at,
        ))

    # ---------- 3. 健康状态非 healthy 的模型渠道 ----------
    unhealthy_channels = (
        await db.execute(
            select(ModelChannel).where(ModelChannel.health_status != "healthy")
            .order_by(ModelChannel.last_check_at.desc().nullslast())
        )
    ).scalars().all()
    for c in unhealthy_channels:
        owner = "平台" if c.owner_user_id is None else f"用户 #{c.owner_user_id}"
        alerts.append(AlertItem(
            level="warning",
            type="channel_unhealthy",
            message=(
                f"渠道 #{c.id}（{c.protocol} @ {c.base_url}，{owner}）健康状态为 "
                f"{c.health_status}，请及时排查"
            ),
            created_at=c.last_check_at or c.created_at,
        ))

    alerts.sort(key=lambda a: a.created_at or now, reverse=True)
    return alerts


@router.get("/system-metrics")
async def get_system_metrics(
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> dict:
    """查询系统运行指标（管理端系统监控面板数据源）。

    统计口径：
    - task_success_rate_24h / task_total_24h：近 24h 任务总数与
      成功率（done / (done + failed)，无终态任务时为 0）；
    - avg_task_seconds_24h：近 24h 成功任务的平均耗时秒
      （updated_at - created_at，按 FILTER 子句仅对 done 聚合）；
    - dead_letter_count：死信任务数 —— 平台故障且重试耗尽后的终态失败
      （status='failed' 且 error_code='platform_error'，RD-28 口径）；
    - running_count / queued_count：当前进行中/排队中任务数（按状态分组聚合）；
    - channels：全部模型渠道健康快照（id/protocol/health_status/last_check_at）。

    Args:
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        dict: 系统运行指标（字段见上，渠道 last_check_at 为 ISO 时间或 null）。

    Raises:
        无：查询异常向上抛出由全局处理器兜底。
    """
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)

    # ---------- 近 24h 任务总量 / 成功 / 失败 / 平均耗时（单条 SELECT 多标量） ----------
    task_row = (
        await db.execute(
            select(
                func.count(Task.id).label("total"),
                func.count(Task.id).filter(Task.status == "done").label("done"),
                func.count(Task.id).filter(Task.status == "failed").label("failed"),
                func.avg(func.extract("epoch", Task.updated_at - Task.created_at))
                .filter(Task.status == "done")
                .label("avg_seconds"),
            ).where(Task.created_at >= day_ago)
        )
    ).one()
    total_24h = int(task_row.total or 0)
    done_24h = int(task_row.done or 0)
    failed_24h = int(task_row.failed or 0)
    terminal = done_24h + failed_24h
    success_rate = (done_24h / terminal) if terminal > 0 else 0.0
    avg_seconds = float(task_row.avg_seconds) if task_row.avg_seconds is not None else 0.0

    # ---------- 死信：平台故障重试耗尽后的终态失败 ----------
    dead_letter = await db.scalar(
        select(func.count()).select_from(Task).where(
            Task.status == "failed", Task.error_code == _PLATFORM_ERROR_CODE
        )
    ) or 0

    # ---------- 当前队列积压：按任务状态分组计数 ----------
    status_rows = (
        await db.execute(select(Task.status, func.count()).group_by(Task.status))
    ).all()
    status_map = {s: int(c or 0) for s, c in status_rows}

    # ---------- 渠道健康快照 ----------
    channel_rows = (
        await db.execute(
            select(
                ModelChannel.id,
                ModelChannel.protocol,
                ModelChannel.health_status,
                ModelChannel.last_check_at,
            ).order_by(ModelChannel.id)
        )
    ).all()

    return {
        "task_success_rate_24h": round(success_rate, 4),
        "task_total_24h": total_24h,
        "dead_letter_count": int(dead_letter),
        "running_count": status_map.get("running", 0),
        "queued_count": status_map.get("queued", 0),
        "avg_task_seconds_24h": round(avg_seconds, 2),
        "channels": [
            {
                "id": c.id,
                "protocol": c.protocol,
                "health_status": c.health_status,
                "last_check_at": c.last_check_at,
            }
            for c in channel_rows
        ],
    }
