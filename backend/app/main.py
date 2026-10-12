"""应用入口模块（文件级说明）。

负责 FastAPI 应用工厂 create_app()：
- lifespan：启动时执行数据库初始化（失败降级不崩溃）并启动进程内任务执行器
- CORS 中间件：全来源放开
- 全局异常处理器：未捕获异常统一返回 500 JSON
- 注册全部业务与管理端占位路由
- 根路由 GET /

启动方式：`python -m uvicorn app.main:app --port 8000`（cwd=backend）。
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import BASE_DIR, settings
from app.core.database import engine
from app.core.init_db import init_database
from app.core.metrics import HTTPMetricsMiddleware
from app.core.tracing import setup_tracing
from app.tasks.runner import recover_orphan_running_tasks, start_runner

logger = logging.getLogger("mangaflow.main")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def _run_alembic_upgrade() -> None:
    """同步执行 Alembic 迁移至最新版本（alembic upgrade head，程序化 API）。

    使用 alembic 的 command.upgrade 而非 subprocess：进程内调用免于环境/依赖拼接，
    连接串经 alembic.ini 占位由 env.py 动态注入（运行时取 settings.database_url）。

    Raises:
        Exception: 数据库不可达、迁移脚本执行失败时向上抛出，由调用方捕获降级。
    """
    from alembic import command
    from alembic.config import Config

    # alembic.ini 与 alembic/ 版本目录均位于 backend 根目录（BASE_DIR）；
    # 显式 set_main_option 兜底 script_location，兼容任意工作目录下的程序化调用
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。

    启动时：可选执行 Alembic 迁移（ALEMBIC_ON_STARTUP=true 时，先于 create_all），
    随后初始化数据库（建库/建表/种子，失败仅记录日志不阻塞启动），再执行在途
    任务启动恢复（遗留 running 行重置回 queued，先恢复后领取避免竞态），
    并启动进程内异步任务执行器。
    关闭时：释放数据库引擎连接池。

    Args:
        app: FastAPI 应用实例。

    Yields:
        None: 挂起至应用关闭。

    Raises:
        Exception: 初始化异常均被捕获记录，不向上传播（降级启动）。
    """
    # LangSmith 追踪接线（MA-13）：须在任何 LLM/Graph 调用之前注入追踪环境变量；
    # 未配置（langsmith_tracing=False 默认）时零开销直接返回
    setup_tracing()

    if settings.alembic_on_startup:
        try:
            # alembic 的 env.py 在线模式经 asyncio.run 驱动异步迁移，而 lifespan 运行
            # 于主事件循环中——asyncio.run 不能在运行中的循环内调用，故用 to_thread
            # 切到工作线程执行（工作线程无运行中循环，可安全创建新循环）
            await asyncio.to_thread(_run_alembic_upgrade)
            logger.info("Alembic 迁移执行完成（upgrade head）")
        except Exception:
            # 降级策略：迁移失败不阻断启动，交由 create_all + 幂等补列兜底
            logger.exception("Alembic 迁移失败，将降级为 create_all 兜底初始化")

    try:
        await init_database()
        logger.info("数据库初始化完成（建库/建表/种子）")
    except Exception:
        # 降级启动：数据库不可达时服务仍可拉起，接口层面再暴露错误
        logger.exception("数据库初始化失败，服务将以降级模式启动")

    try:
        # pgvector 双模向量检索（Task 6）：create_all 之后、恢复/启动 worker 之前
        # 探测扩展可用性并幂等 ensure 影子向量列 + ivfflat 索引；失败仅告警降级，
        # 检索自动回退 JSONB+Python 余弦路径，不阻断启动
        from app.services.vector_store import ensure_pgvector_schema

        await ensure_pgvector_schema(engine)
    except Exception:
        logger.exception("pgvector 影子列初始化异常，向量检索降级为 JSONB+Python 余弦路径")

    try:
        # 启动恢复（R-03）：先重置进程重启遗留的 running 僵尸任务，再拉起
        # worker——先恢复后领取，保证恢复事务先于任何领取提交，消除竞态；
        # 恢复失败仅记录日志降级继续，不阻断启动（worker 照常服务新任务）
        recovered = await recover_orphan_running_tasks()
        if recovered:
            logger.warning("在途任务恢复完成：%d 条僵尸任务重回队列", recovered)
    except Exception:
        logger.exception("在途任务恢复失败，跳过恢复降级启动")

    try:
        await start_runner(engine)
        logger.info("任务执行器启动完成")
    except Exception:
        logger.exception("任务执行器启动失败")

    yield

    await engine.dispose()
    logger.info("应用已关闭，数据库连接池已释放")


def create_app() -> FastAPI:
    """FastAPI 应用工厂。

    Returns:
        FastAPI: 配置好中间件、异常处理器与全部路由的应用实例。
    """
    app = FastAPI(
        title="漫序 MangaFlow API",
        version="1.0.0",
        description=(
            "漫序 MangaFlow —— AI 漫剧一站式生产平台后端。"
            "覆盖剧本创作、角色/场景/道具资产、分镜生成、画布编排、"
            "成片发布、积分计费与内容审核的完整工作流。"
        ),
        lifespan=lifespan,
    )

    # CORS：全来源、全方法、全头放开（本地开发环境配置）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Prometheus HTTP 指标中间件（纯 ASGI 实现：拦截 http.response.start 记
    # 状态码并计时计数，不缓冲响应流，避免 BaseHTTPMiddleware 与 SSE 冲突）
    app.add_middleware(HTTPMetricsMiddleware)

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """全局兜底异常处理器。

        Args:
            request: 当前请求。
            exc: 未捕获的异常。

        Returns:
            JSONResponse: 统一 500 响应体 {"detail": "服务器内部错误"}。

        Raises:
            无：所有异常在此收敛，避免向上泄漏堆栈。
        """
        logger.exception("未处理异常: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})

    # HTTPException（401/403/404 等）与请求校验异常（422）保持 FastAPI 默认处理行为

    # ---------- 静态文件：生成/上传素材的本地存储对外服务 ----------
    # 生成任务与上传文件的物理目录（config.storage_dir），通过 /storage/* URL 暴露，
    # 前端 <img>/<video>/<audio> 直接引用该地址（生产环境应替换为对象存储 + CDN）。
    # 注意：必须使用 settings.storage_path()（以 PROJECT_ROOT 为基准的绝对路径），
    # 不能直接用相对的 settings.storage_dir —— 服务以 cwd=backend 启动时相对路径
    # 会解析为 backend/backend/storage，导致所有 /storage/* 产物 404（C-BUG-2）。
    from fastapi.staticfiles import StaticFiles

    storage_dir = settings.storage_path()
    storage_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/storage", StaticFiles(directory=str(storage_dir)), name="storage")

    # ---------- 业务路由（占位） ----------
    from app.api import (
        agent_chat,
        announcements,
        assets,
        auth,
        bgm,
        billing,
        canvas_plugins,
        canvases,
        generation,
        mcp,
        media,
        memories,
        models_config,
        notifications,
        plugins_builtin,
        projects,
        prompts,
        publish,
        script,
        storyboards,
        users,
        versions,
    )

    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(projects.router)
    app.include_router(versions.router)
    app.include_router(billing.router)
    app.include_router(prompts.router)
    app.include_router(media.router)
    app.include_router(models_config.router)
    # BGM 曲库：列表（登录用户）/ 上传与下架（运营方）
    app.include_router(bgm.router)
    app.include_router(announcements.router)
    # 以下路由因携带 {project_id} 路径参数，不设 prefix，路由内部写完整路径
    app.include_router(script.router)
    app.include_router(assets.router)
    app.include_router(storyboards.router)
    app.include_router(generation.router)
    app.include_router(publish.router)
    app.include_router(canvases.router)
    app.include_router(agent_chat.router)
    app.include_router(memories.router)
    # 画布插件市场：列表/安装/审查/启停/手动调用（CV-20/21/22，登录用户 + 运营方审查）
    app.include_router(canvas_plugins.router)
    # 内置插件直调端点（CV-21：builtin:text-enhance 等，登录用户）
    app.include_router(plugins_builtin.router)
    # MCP 桥：server 配置（站点设置 KV）/ 工具列举 / 工具调用（CV-19）
    app.include_router(mcp.router)
    # 通知中心：列表/未读数/已读/SSE 实时推送（登录用户）
    app.include_router(notifications.router)

    # Prometheus 指标端点（免鉴权：仅聚合指标，无敏感数据，供监控系统抓取）
    from app.api.metrics import router as metrics_router

    app.include_router(metrics_router)

    # ---------- 管理端路由（占位） ----------
    from app.api.admin import (
        content,
        dashboard,
        dlq,
        models,
        orders,
        review,
        settings as admin_settings,
        users as admin_users,
    )

    app.include_router(dashboard.router)
    app.include_router(admin_users.router)
    app.include_router(review.router)
    app.include_router(models.router)
    app.include_router(orders.router)
    app.include_router(content.router)
    app.include_router(admin_settings.router)
    # 任务死信队列：死信列表 / 重新入队（运营方）
    app.include_router(dlq.router)

    @app.get("/", tags=["meta"])
    async def root() -> dict:
        """根路由：服务自描述信息。

        Returns:
            dict: 服务名称与文档地址。
        """
        return {"name": "MangaFlow API", "docs": "/docs"}

    return app


app = create_app()
