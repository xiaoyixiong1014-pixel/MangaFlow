# =============================================================================
# 漫序 MangaFlow 后端镜像（文件级说明）
#
# 用途   : FastAPI 后端（uvicorn 启动，容器内端口 8300）
# 基础   : python:3.12-slim
# 上下文 : 仓库根（docker build -f deploy/backend.Dockerfile .）
# 产物   : mangaflow/backend 镜像，ENTRYPOINT 为 uvicorn app.main:app
#
# 与部署文档 V1.0 §5.2 的对应关系：
#   - 保留 ffmpeg + fonts-noto-cjk（视频合成、字幕烧录不乱码，P-15/P-16）
#   - 端口由文档的 8000 调整为本仓库约定的 8300（本机 8000 被其他项目占用）
#   - HEALTHCHECK 使用 /api/metrics（免鉴权 Prometheus 端点，能同时反映 DB 连通性）
# =============================================================================

# ---- 阶段说明：单阶段构建，python:3.12-slim 基础 ----
FROM python:3.12-slim

# ARG: pip 国内镜像加速开关（默认官方源；国内构建可传
#   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple）
ARG PIP_INDEX_URL=https://pypi.org/simple
# ENV: 将 ARG 固化为 pip 运行时索引配置
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

# ENV: Python 运行行为与时区（日志实时输出、不留字节码、东八区）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# 系统依赖：ffmpeg（成片合成）、中文字体（字幕避免方块）、fontconfig（字体缓存）
# 说明：requirements 内全部为预编译 wheel（asyncpg/psycopg[binary]/bcrypt 等），
#       无需 build-essential 编译工具链，保持镜像精简
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk fontconfig \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# 工作目录：/app（镜像内布局与仓库一致，/app/backend 即仓库的 backend/ 目录）
WORKDIR /app

# 先拷贝依赖清单并安装（利用 Docker 层缓存：代码变更不重复安装依赖）
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 再拷贝后端代码（.dockerignore 已排除 .venv/__pycache__/storage/tests 等无关内容）
COPY backend/ /app/backend/
WORKDIR /app/backend

# EXPOSE: 容器内服务端口（本仓库约定 8300）
EXPOSE 8300

# HEALTHCHECK: 通过免鉴权的 /api/metrics 探活（slim 镜像无 curl，改用 python 标准库；
# 该端点会刷新 DB 侧 Gauge，可同时反映数据库连通性；start-period 覆盖首次建库建表耗时）
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8300/api/metrics', timeout=4)"

# CMD: uvicorn 启动 FastAPI 应用（应用在 lifespan 中自动建库/建表/种子，失败降级启动）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300"]
