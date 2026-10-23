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
# V2.0 变更：非 root 运行基线——安装 gosu + appuser，ENTRYPOINT 先修复存储卷
#   属主再降权执行 uvicorn / ffmpeg（见 deploy/backend-entrypoint.sh）
# =============================================================================

# ---- 阶段说明：单阶段构建，python:3.12-slim 基础 ----
FROM python:3.12-slim

# ARG: pip 国内镜像加速开关（默认官方源；国内构建可传
#   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple）
ARG PIP_INDEX_URL=https://pypi.org/simple
# ENV: 将 ARG 固化为 pip 运行时索引配置
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

# ARG: apt 国内镜像加速开关（默认腾讯云内网镜像——腾讯云服务器走内网免流量包
#   且速度极快；非腾讯云环境可传 mirrors.aliyun.com 等，传空串则保留官方源）
ARG APT_MIRROR=mirrors.cloud.tencent.com

# ENV: Python 运行行为与时区（日志实时输出、不留字节码、东八区）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# 系统依赖：ffmpeg（成片合成）、中文字体（字幕避免方块）、fontconfig（字体缓存）
# gosu：entrypoint 完成存储卷属主修复后从 root 降权到 appuser（非 root 运行基线）
# 说明：requirements 内全部为预编译 wheel（asyncpg/psycopg[binary]/bcrypt 等），
#       无需 build-essential 编译工具链，保持镜像精简
# apt 源替换：trixie 镜像使用 deb822 格式的 debian.sources；APT_MIRROR 为空时跳过
RUN if [ -n "${APT_MIRROR}" ] && [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && if [ -n "${APT_MIRROR}" ] && [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk fontconfig gosu \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# 非特权运行用户：uvicorn / ffmpeg 以 appuser 身份执行（HOME 供 fontconfig 等写用户级缓存）
RUN groupadd -r appuser && useradd -r -g appuser -m -d /home/appuser -s /usr/sbin/nologin appuser
ENV HOME=/home/appuser

# 工作目录：/app（镜像内布局与仓库一致，/app/backend 即仓库的 backend/ 目录）
WORKDIR /app

# 先单独安装 CPU 版 torch（lipsync 本地推理依赖）：PyPI 的 Linux wheel 默认
# 捆绑 CUDA（连同 nvidia 依赖约 2GB+，不适合 2GB 内存服务器），CPU 推理走
# pytorch 官方 cpu index（约 190MB）。独立成层：代码变更时缓存不失效。
# requirements.txt 不写 torch，避免 pip 从默认源解析到 CUDA 版。
RUN pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu

# 再拷贝依赖清单并安装（利用 Docker 层缓存：代码变更不重复安装依赖）
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 再拷贝后端代码（.dockerignore 已排除 .venv/__pycache__/storage/tests 等无关内容）
COPY backend/ /app/backend/
WORKDIR /app/backend

# 代码与存储目录属主交给 appuser（存储卷属主由 entrypoint 启动时按需修复）
RUN chown -R appuser:appuser /app/backend /home/appuser

# 入口脚本：存储卷属主修复 → gosu 降权 → 执行 CMD
COPY deploy/backend-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# EXPOSE: 容器内服务端口（本仓库约定 8300）
EXPOSE 8300

# HEALTHCHECK: 通过免鉴权的 /api/metrics 探活（slim 镜像无 curl，改用 python 标准库；
# 该端点会刷新 DB 侧 Gauge，可同时反映数据库连通性；start-period 覆盖首次建库建表耗时）
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8300/api/metrics', timeout=4)"

# CMD: uvicorn 启动 FastAPI 应用（应用在 lifespan 中自动建库/建表/种子，失败降级启动）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300"]
