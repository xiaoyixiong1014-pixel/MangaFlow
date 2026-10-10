"""Prometheus 指标暴露端点模块（文件级说明）。

实现 GET /api/metrics：以 Prometheus 文本协议输出全部 mangaflow_ 指标。
- 免鉴权（不挂 get_current_user）：指标仅含方法/路径/状态码/任务状态/渠道
  健康值等聚合数据，无任何用户隐私、业务内容或密钥，可安全供 Prometheus/
  Grafana 等监控系统抓取（生产环境如需收紧，可在网关层做来源限制）；
- 抓取时全量刷新渠道健康 Gauge（model_channels 全表一次轻量 SELECT）；
- 进程内 Gauge（进行中任务数）不持久化，首次抓取时按库中 running 数懒加载
  校准一次（替代改 lifespan，避免并行改动 main.py 冲突）。
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.metrics import init_task_gauge, refresh_channel_gauges

logger = logging.getLogger("mangaflow.api.metrics")

router = APIRouter(prefix="/api/metrics", tags=["metrics"])

# 懒加载初始化标记：保证 tasks_gauge 校准只执行一次（并发抓取下用锁保护）
_initialized = False
_init_lock = asyncio.Lock()


async def _ensure_initialized(db: AsyncSession) -> None:
    """首次抓取时执行一次进程内 Gauge 初始化（内部函数）。

    Args:
        db: 请求级异步数据库会话（当前仅占位；running 数校准独立开会话，
            避免与请求会话生命周期耦合）。

    Returns:
        None

    Raises:
        Exception: 数据库不可达时记录日志并放行（指标仍可输出，Gauge 置 0）。
    """
    global _initialized
    if _initialized:
        return
    async with _init_lock:
        if _initialized:
            return
        try:
            await init_task_gauge()
        except Exception:  # noqa: BLE001 —— 初始化失败不阻断指标抓取
            logger.exception("tasks_gauge 懒加载初始化失败（数据库不可达？）")
        _initialized = True


@router.get("", summary="Prometheus 指标端点（免鉴权）")
async def get_metrics(db: AsyncSession = Depends(get_db)) -> Response:
    """输出 Prometheus 文本格式指标（免鉴权，仅聚合数据无敏感信息）。

    每次抓取先刷新渠道健康 Gauge（model_channels 全表映射为
    1=healthy / 0.5=degraded / 0=down），随后输出全部 mangaflow_ 指标。

    Args:
        db: 请求级异步数据库会话。

    Returns:
        Response: content=generate_latest()，media_type=CONTENT_TYPE_LATEST
        （text/plain; version=0.0.4; charset=utf-8）。

    Raises:
        Exception: 渠道刷新数据库异常向上抛出，由全局异常处理器兜底返回 500
        （正常情况下监控系统会保留上一次成功抓取样本）。
    """
    await _ensure_initialized(db)
    try:
        await refresh_channel_gauges(db)
    except Exception:  # noqa: BLE001 —— 渠道刷新失败仍输出既有指标
        logger.exception("渠道健康 Gauge 刷新失败")
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
