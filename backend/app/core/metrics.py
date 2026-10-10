"""Prometheus 可观测性指标模块（文件级说明）。

集中定义 MangaFlow 全部 Prometheus 指标与维护辅助函数：
- HTTP 请求指标：mangaflow_http_requests_total（计数）、
  mangaflow_http_request_duration_seconds（直方图，由纯 ASGI 中间件采集）；
- 任务指标：mangaflow_tasks_total（状态迁移计数）、
  mangaflow_tasks_active（进行中任务数，由 tasks/runner.py 维护）；
- 渠道健康指标：mangaflow_channel_health（1 健康 / 0.5 降级 / 0 宕机，
  由 services/model_router.py 回写、/api/metrics 抓取时定期刷新）。

指标仅包含方法/路径/状态码/状态名等聚合维度，不含任何用户数据或密钥，
可安全对外暴露给监控系统（详见 api/metrics.py 端点注释）。
"""

import time
from typing import Any, Optional

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models import ModelChannel, Task

# ---------------------------------------------------------------------------
# HTTP 请求指标
# ---------------------------------------------------------------------------

# HTTP 请求总数（按方法/路径/状态码维度）
REQUEST_COUNT = Counter(
    "mangaflow_http_requests_total",
    "HTTP 请求总数",
    ["method", "path", "status"],
)

# HTTP 请求耗时直方图（按方法/路径维度；桶覆盖本地生成的典型耗时量级）
REQUEST_LATENCY = Histogram(
    "mangaflow_http_request_duration_seconds",
    "HTTP 请求耗时",
    ["method", "path"],
    buckets=(0.05, 0.1, 0.5, 1.0, 3.0),
)

# ---------------------------------------------------------------------------
# 任务指标
# ---------------------------------------------------------------------------

# 任务状态迁移计数（queued/running/done/failed/canceled）
TASK_COUNT = Counter(
    "mangaflow_tasks_total",
    "任务状态迁移计数",
    ["status"],
)

# 进行中（running）任务数实时值
TASK_GAUGE = Gauge(
    "mangaflow_tasks_active",
    "进行中任务数",
)

# ---------------------------------------------------------------------------
# 渠道健康指标
# ---------------------------------------------------------------------------

# 渠道健康值（按渠道 ID 维度）：1=healthy / 0.5=degraded / 0=down（unknown 视同 0）
CHANNEL_HEALTH = Gauge(
    "mangaflow_channel_health",
    "渠道健康(1健康 0.5降级 0宕机)",
    ["channel_id"],
)

# health_status 文本 → 数值映射（unknown 未探活按 0 计）
_HEALTH_VALUE_MAP = {"healthy": 1.0, "degraded": 0.5, "down": 0.0, "unknown": 0.0}


def mark_task(status: str) -> None:
    """任务状态迁移计数（由 tasks/runner.py 在每次状态变更处调用）。

    Args:
        status: 迁移目标状态（queued/running/done/failed/canceled）。

    Returns:
        None

    Raises:
        无：计数器自增不会抛出业务异常。
    """
    TASK_COUNT.labels(status=status).inc()


def task_running_delta(n: int) -> None:
    """调整进行中任务数 Gauge（+1 领取置 running / -1 进入终态或回队列）。

    Args:
        n: 增量（通常为 +1 或 -1）。

    Returns:
        None

    Raises:
        无。
    """
    if n:
        TASK_GAUGE.inc(n)


def set_channel_health(channel_id: int, ok: bool, degraded_value: float = 0.5) -> None:
    """回写单个渠道健康 Gauge（由 services/model_router.record_result 调用）。

    Args:
        channel_id: 渠道 ID（0 为本地 mock 哨兵，调用方应先行过滤）。
        ok: 本次调用是否成功（成功置 1.0，失败按降级值处理）。
        degraded_value: 失败时的健康值（默认 0.5 = degraded）。

    Returns:
        None

    Raises:
        无。
    """
    CHANNEL_HEALTH.labels(channel_id=str(channel_id)).set(1.0 if ok else degraded_value)


async def refresh_channel_gauges(session: Any) -> None:
    """按 model_channels 全表刷新渠道健康 Gauge（/api/metrics 抓取时调用）。

    Args:
        session: 异步数据库会话（由调用方管理事务与关闭）。

    Returns:
        None

    Raises:
        Exception: 数据库查询异常向上抛出（由抓取端点兜底返回 500）。
    """
    channels = (await session.execute(select(ModelChannel))).scalars().all()
    for ch in channels:
        CHANNEL_HEALTH.labels(channel_id=str(ch.id)).set(
            _HEALTH_VALUE_MAP.get(ch.health_status, 0.0)
        )


async def init_task_gauge() -> None:
    """初始化进行中任务数 Gauge（进程启动/首次抓取时按库中 running 数校准）。

    用于进程重启后 Gauge 与数据库实际状态对齐（Gauge 不持久化）。

    Args:
        无参数。

    Returns:
        None

    Raises:
        Exception: 数据库不可达时向上抛出（调用方兜底，不影响主流程）。
    """
    async with SessionLocal() as session:
        running = await session.scalar(
            select(func.count()).select_from(Task).where(Task.status == "running")
        ) or 0
    TASK_GAUGE.set(float(running))


class HTTPMetricsMiddleware:
    """纯 ASGI HTTP 指标中间件（类级注释）。

    为什么不用 BaseHTTPMiddleware：BaseHTTPMiddleware 会缓冲/包装响应流，
    与 SSE（text/event-stream）长连接冲突导致流式中断；本中间件以裸 ASGI
    方式仅包装 send 拦截 http.response.start 拿到状态码，完全不干预 body 流。

    统计口径：
    - 计数：每个 HTTP 请求按 (method, path, status) +1；
    - 耗时：请求到达 → 响应头发出（SSE 长连接只统计首字节前耗时，
      避免挂连时长污染直方图）；
    - path 直接取 scope["path"]（不含 query），未做路由模板归一化，
      聚合维度含资源 ID（可观测性与基数之间按当前规模取舍）。

    Attributes:
        app: 内层 ASGI 应用。
    """

    def __init__(self, app: Any) -> None:
        """保存内层 ASGI 应用。

        Args:
            app: 内层 ASGI 应用。

        Returns:
            None

        Raises:
            无。
        """
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        """ASGI 调用入口：非 http 请求直接透传，http 请求计时计数。

        Args:
            scope: ASGI 作用域字典。
            receive: ASGI 接收函数。
            send: ASGI 发送函数。

        Returns:
            None

        Raises:
            Exception: 内层应用异常时记录 500 指标后原样向上抛出。
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "-")
        path = scope.get("path", "-")
        start = time.perf_counter()
        observed = {"flag": False}

        def _record(status_code: int) -> None:
            """记录一次请求指标（内部函数，保证单请求只计一次）。

            Args:
                status_code: 响应状态码。

            Returns:
                None

            Raises:
                无。
            """
            if observed["flag"]:
                return
            observed["flag"] = True
            REQUEST_COUNT.labels(method=method, path=path, status=str(status_code)).inc()
            REQUEST_LATENCY.labels(method=method, path=path).observe(time.perf_counter() - start)

        async def send_wrapper(message: dict) -> None:
            """包装 send：拦截 http.response.start 记录状态码与耗时。

            Args:
                message: ASGI 发送事件（http.response.start / http.response.body）。

            Returns:
                None

            Raises:
                Exception: 下游 send 异常向上传播。
            """
            if message["type"] == "http.response.start":
                _record(int(message.get("status", 500)))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # 内层应用抛出未捕获异常：计 500 后向上抛给异常处理器
            _record(500)
            raise
