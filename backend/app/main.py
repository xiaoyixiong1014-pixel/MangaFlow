"""应用入口模块（文件级说明）。

负责 FastAPI 应用工厂 create_app()：
- lifespan：启动时执行数据库初始化（失败降级不崩溃）并启动进程内任务执行器
- CORS 中间件：全来源放开
- 全局异常处理器：未捕获异常统一返回 500 JSON
- 注册全部业务与管理端占位路由
- 根路由 GET /

启动方式：`python -m uvicorn app.main:app --port 8000`（cwd=backend）。
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.database import engine
from app.core.init_db import init_database
from app.tasks.runner import start_runner

logger = logging.getLogger("mangaflow.main")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。

    启动时：初始化数据库（建库/建表/种子，失败仅记录日志不阻塞启动），
    随后启动进程内异步任务执行器（当前为占位 no-op）。
    关闭时：释放数据库引擎连接池。

    Args:
        app: FastAPI 应用实例。

    Yields:
        None: 挂起至应用关闭。

    Raises:
        Exception: 初始化异常均被捕获记录，不向上传播（降级启动）。
    """
    try:
        await init_database()
        logger.info("数据库初始化完成（建库/建表/种子）")
    except Exception:
        # 降级启动：数据库不可达时服务仍可拉起，接口层面再暴露错误
        logger.exception("数据库初始化失败，服务将以降级模式启动")

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
    from fastapi.staticfiles import StaticFiles

    storage_dir = Path(settings.storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/storage", StaticFiles(directory=str(storage_dir)), name="storage")

    # ---------- 业务路由（占位） ----------
    from app.api import (
        agent_chat,
        announcements,
        assets,
        auth,
        billing,
        canvases,
        generation,
        media,
        models_config,
        projects,
        prompts,
        publish,
        script,
        storyboards,
        users,
    )

    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(projects.router)
    app.include_router(billing.router)
    app.include_router(prompts.router)
    app.include_router(media.router)
    app.include_router(models_config.router)
    app.include_router(announcements.router)
    # 以下路由因携带 {project_id} 路径参数，不设 prefix，路由内部写完整路径
    app.include_router(script.router)
    app.include_router(assets.router)
    app.include_router(storyboards.router)
    app.include_router(generation.router)
    app.include_router(publish.router)
    app.include_router(canvases.router)
    app.include_router(agent_chat.router)

    # ---------- 管理端路由（占位） ----------
    from app.api.admin import (
        content,
        dashboard,
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

    @app.get("/", tags=["meta"])
    async def root() -> dict:
        """根路由：服务自描述信息。

        Returns:
            dict: 服务名称与文档地址。
        """
        return {"name": "MangaFlow API", "docs": "/docs"}

    return app


app = create_app()
