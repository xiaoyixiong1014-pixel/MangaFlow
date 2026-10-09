# 漫序 MangaFlow · 腾讯云 Linux 服务器 Docker 一键部署操作文档

| 项目 | 内容 |
| ---- | ---- |
| 产品名称 | 漫序 MangaFlow |
| 文档版本 | V1.0 |
| 文档类型 | 部署与运维操作手册 |
| 编制日期 | 2026-10-08 |
| 基线文档 | 《漫序 MangaFlow 产品需求文档（PRD）V1.1》、《漫序 MangaFlow 项目架构设计与数据库设计文档 V1.1》 |
| 适用环境 | 腾讯云 CVM（Linux）+ Docker + Nginx |

**说明**：本文档覆盖环境准备、依赖安装、镜像构建、服务启动与验证的完整流程，所有命令可直接执行。技术栈严格遵循 PRD 第 9 章：Vue3 + FastAPI + PostgreSQL 16（pgvector）+ Redis 7 + Celery + LangGraph + FFmpeg + Nginx + Docker。

---

## 1. 部署架构

采用**单机 Docker Compose 部署**，所有服务容器化，通过 Nginx 统一对外。

```mermaid
flowchart TB
    C["用户浏览器"] -->|443 / 80| NG["Nginx 容器<br/>静态资源 + 反向代理"]
    NG -->|/| FE["前端静态资源<br/>Vue3 构建产物"]
    NG -->|/api| API["后端 API 容器<br/>FastAPI + Uvicorn"]
    NG -->|/ws /sse| API
    API --> PG[("PostgreSQL 16<br/>+ pgvector")]
    API --> RD[("Redis 7")]
    API --> CEL["Celery 任务提交"]
    CEL --> WK["Worker 容器<br/>Celery + FFmpeg"]
    WK --> PG
    WK --> RD
    WK --> OSS["对象存储<br/>本地卷 / S3 兼容"]
    FL["Flower 容器<br/>任务监控"] --> RD
```

**容器清单**

| 容器 | 镜像 | 端口 | 说明 |
| ---- | ---- | ---- | ---- |
| nginx | nginx:1.27-alpine | 80 / 443 | 反向代理 + 静态资源 |
| frontend | 自构建（多阶段） | 内部 | 构建产物注入 nginx |
| api | 自构建（Python 3.13） | 8000 | FastAPI 主服务 |
| worker | 复用 api 镜像 | — | Celery Worker（含 FFmpeg） |
| flower | 复用 api 镜像 | 5555 | 任务监控（仅内网） |
| postgres | pgvector/pgvector:pg16 | 5432 | 业务库 + 向量 |
| redis | redis:7-alpine | 6379 | 缓存 + 消息队列 |

---

## 2. 环境准备

### 2.1 服务器规格建议

| 场景 | CPU | 内存 | 系统盘 | 数据盘 | 带宽 |
| ---- | --- | ---- | ------ | ------ | ---- |
| 演示 / 测试 | 4 核 | 8 GB | 100 GB SSD | — | 5 Mbps |
| 生产（推荐） | 8 核 | 16 GB | 100 GB SSD | 200 GB SSD | 10 Mbps+ |

> 说明：视频合成（FFmpeg）与并发任务为主要资源消耗点；若启用本地大模型或高频视频生成，建议 16 核 32 GB 起。

### 2.2 操作系统

支持以下任一（推荐前两项）：

- TencentOS Server 3.1（兼容 CentOS 8 命令）
- Ubuntu 22.04 / 24.04 LTS
- CentOS Stream 9

### 2.3 安全组配置（腾讯云控制台）

| 端口 | 协议 | 来源 | 用途 |
| ---- | ---- | ---- | ---- |
| 22 | TCP | 运维 IP（建议限定） | SSH |
| 80 | TCP | 0.0.0.0/0 | HTTP |
| 443 | TCP | 0.0.0.0/0 | HTTPS |
| 5555 | TCP | **仅内网 / 运维 IP** | Flower 监控（切勿公网开放） |

> 数据库（5432）与 Redis（6379）**不要**在安全组开放，仅容器内网互通。

### 2.4 域名与备案

- 国内地域服务器绑定域名需完成 **ICP 备案**后方可对外提供 80/443 服务。
- 建议提前解析：`A 记录 @ → 服务器公网 IP`、`A 记录 www → 服务器公网 IP`。

### 2.5 系统初始化

```bash
# 更新系统（Ubuntu 系）
sudo apt update && sudo apt upgrade -y

# 更新系统（TencentOS / CentOS 系）
sudo yum update -y

# 设置时区为东八区（避免日志与任务时间错乱）
sudo timedatectl set-timezone Asia/Shanghai
timedatectl

# 创建部署用户（可选，建议使用非 root 用户）
sudo useradd -m -s /bin/bash deploy
sudo usermod -aG sudo deploy
```

---

## 3. 依赖安装

### 3.1 安装 Docker 与 Docker Compose

```bash
# 使用官方脚本安装 Docker（含 Compose v2 插件）
curl -fsSL https://get.docker.com | sudo sh

# 启动并设置开机自启
sudo systemctl enable --now docker

# 将当前用户加入 docker 组（免 sudo，需重新登录生效）
sudo usermod -aG docker $USER
newgrp docker

# 验证
docker --version
docker compose version
```

### 3.2 配置国内镜像加速（腾讯云推荐）

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "registry-mirrors": ["https://mirror.ccs.tencentyun.com"],
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" },
  "live-restore": true
}
EOF

sudo systemctl daemon-reload
sudo systemctl restart docker
docker info | grep -A2 "Registry Mirrors"
```

### 3.3 安装 Git

```bash
# Ubuntu
sudo apt install -y git

# TencentOS / CentOS
sudo yum install -y git

git --version
```

### 3.4 安装基础工具

```bash
# Ubuntu
sudo apt install -y curl wget unzip vim htop

# TencentOS / CentOS
sudo yum install -y curl wget unzip vim htop
```

> FFmpeg **不需要**在宿主机安装，已打包进 `worker` 镜像（见 5.2）。

---

## 4. 代码获取与目录结构

### 4.1 拉取代码

```bash
sudo mkdir -p /opt/mangaflow && sudo chown -R $USER:$USER /opt/mangaflow
cd /opt/mangaflow

git clone <你的仓库地址> .
# 或使用指定分支
# git clone -b main <你的仓库地址> .

git checkout main
```

### 4.2 目录结构

```
/opt/mangaflow
├── backend/                 # FastAPI 后端
│   ├── app/
│   │   ├── api/             # 路由层
│   │   ├── services/        # 业务服务层
│   │   ├── agents/          # LangGraph 多智能体编排
│   │   ├── models/          # SQLAlchemy 模型
│   │   ├── tasks/           # Celery 任务
│   │   ├── core/            # 配置 / 安全 / 依赖
│   │   └── main.py
│   ├── alembic/             # 数据库迁移
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/                # Vue3 前端
│   ├── src/
│   ├── nginx.conf
│   ├── package.json
│   └── Dockerfile
├── deploy/
│   ├── docker-compose.yml
│   ├── docker-compose.monitoring.yml   # 可选：监控栈
│   ├── .env.example
│   ├── nginx/nginx.conf
│   └── conf.d/
│       └── mangaflow.conf
├── scripts/
│   ├── deploy.sh            # 一键部署脚本
│   ├── backup.sh            # 备份脚本
│   ├── restore.sh           # 恢复脚本
│   └── rollback.sh          # 回滚脚本
└── data/
    ├── postgres/            # 数据库持久化
    ├── redis/
    ├── media/               # 本地上传与生成媒体
    └── logs/
```

### 4.3 创建数据目录

```bash
cd /opt/mangaflow
mkdir -p data/postgres data/redis data/media data/logs data/backup
sudo chown -R 999:999 data/postgres   # postgres 容器内 UID
sudo chown -R 1000:1000 data/redis 2>/dev/null || true
```

---

## 5. 配置文件说明

### 5.1 环境变量 `.env`

```bash
cd /opt/mangaflow/deploy
cp .env.example .env
```

编辑 `.env`（**以下为完整字段说明**）：

```dotenv
# ========== 基础 ==========
APP_NAME=MangaFlow
APP_ENV=production
APP_HOST=0.0.0.0
APP_PORT=8000
API_PREFIX=/api
TIMEZONE=Asia/Shanghai
# 32 位以上随机串：openssl rand -hex 32
APP_SECRET_KEY=请替换为随机字符串

# ========== PostgreSQL ==========
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=mangaflow
POSTGRES_USER=mangaflow
POSTGRES_PASSWORD=请替换为强密码
# 异步驱动（SQLAlchemy async）
DATABASE_URL=postgresql+asyncpg://mangaflow:请替换为强密码@postgres:5432/mangaflow
# Alembic 迁移使用同步驱动
ALEMBIC_DATABASE_URL=postgresql+psycopg://mangaflow:请替换为强密码@postgres:5432/mangaflow

# ========== Redis ==========
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_PASSWORD=请替换为强密码
CELERY_BROKER_URL=redis://:请替换为强密码@redis:6379/0
CELERY_RESULT_BACKEND=redis://:请替换为强密码@redis:6379/1

# ========== JWT ==========
JWT_SECRET=请替换为随机字符串
JWT_ALGORITHM=HS256
JWT_EXPIRE_MINUTES=10080          # 7 天（PRD U-02）

# ========== 密钥加密（API Key AES-256，PL-07）==========
# 生成：openssl rand -base64 32
AES_SECRET_KEY=请替换为 32 字节 base64 字符串

# ========== 对象存储（PL-16 / AD 存储配置）==========
STORAGE_DRIVER=local              # local | s3
MEDIA_ROOT=/app/data/media
S3_ENDPOINT=
S3_REGION=
S3_BUCKET=
S3_ACCESS_KEY=
S3_SECRET_KEY=
S3_USE_SSL=true

# ========== 默认模型渠道（PL-28 / AD-03）==========
LLM_BASE_URL=
LLM_API_KEY=
LLM_DEFAULT_MODEL=
IMAGE_BASE_URL=
IMAGE_API_KEY=
IMAGE_DEFAULT_MODEL=
VIDEO_BASE_URL=
VIDEO_API_KEY=
VIDEO_DEFAULT_MODEL=
TTS_BASE_URL=
TTS_API_KEY=
TTS_DEFAULT_MODEL=

# ========== 计费与配额（PL-20 / PL-25）==========
FREE_DAILY_POINTS=200
QUOTA_WARN_THRESHOLD=0.2          # 剩余 20% 预警

# ========== 合规审核（C-01 / RD-34）==========
AUDIT_ENABLE=true

# ========== 可观测（可选）==========
LANGSMITH_TRACING=false
LANGSMITH_API_KEY=
PROMETHEUS_ENABLE=false

# ========== 前端构建参数 ==========
VITE_API_BASE_URL=/api
```

**生成密钥的推荐命令**

```bash
echo "APP_SECRET_KEY=$(openssl rand -hex 32)"
echo "JWT_SECRET=$(openssl rand -hex 32)"
echo "AES_SECRET_KEY=$(openssl rand -base64 32)"
```

### 5.2 后端 Dockerfile

`backend/Dockerfile`：

```dockerfile
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 系统依赖：psycopg 编译、FFmpeg 合成、中文字体（字幕避免乱码）
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libpq-dev curl ffmpeg \
      fonts-noto-cjk fontconfig \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
```

### 5.3 前端 Dockerfile

`frontend/Dockerfile`（多阶段构建）：

```dockerfile
# ---------- 构建阶段 ----------
FROM node:22-alpine AS build
WORKDIR /app

ARG VITE_API_BASE_URL=/api
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

COPY package*.json ./
RUN npm ci

COPY . .
RUN npm run build

# ---------- 运行阶段 ----------
FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
```

### 5.4 Nginx 配置

`deploy/nginx/conf.d/mangaflow.conf`：

```nginx
upstream mangaflow_api {
    server api:8000;
    keepalive 32;
}

server {
    listen 80;
    server_name _;

    client_max_body_size 200m;      # 允许大文件上传（PRD S-01）
    proxy_read_timeout 600s;        # SSE / 长任务
    proxy_send_timeout 600s;

    # 前端静态资源
    location / {
        proxy_pass http://frontend:80;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # 后端 API
    location /api/ {
        proxy_pass http://mangaflow_api;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
    }

    # SSE 流式（剧本生成 / 任务进度）
    location /api/stream/ {
        proxy_pass http://mangaflow_api;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding on;
    }

    # WebSocket（任务通知）
    location /ws/ {
        proxy_pass http://mangaflow_api;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    # 静态媒体（本地上传/生成结果）
    location /media/ {
        alias /app/data/media/;
        expires 7d;
        add_header Cache-Control "public";
    }
}
```

### 5.5 docker-compose.yml

`deploy/docker-compose.yml`：

```yaml
name: mangaflow

services:
  postgres:
    image: pgvector/pgvector:pg16
    container_name: mf-postgres
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      TZ: ${TIMEZONE}
    volumes:
      - ../data/postgres:/var/lib/postgresql/data
      - ./initdb:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks: [mf-net]

  redis:
    image: redis:7-alpine
    container_name: mf-redis
    restart: unless-stopped
    command: >
      redis-server --requirepass ${REDIS_PASSWORD}
      --appendonly yes --maxmemory 1gb --maxmemory-policy allkeys-lru
    volumes:
      - ../data/redis:/data
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD}", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks: [mf-net]

  api:
    build:
      context: ../backend
      dockerfile: Dockerfile
    image: mangaflow/api:${IMAGE_TAG:-latest}
    container_name: mf-api
    restart: unless-stopped
    env_file: [.env]
    environment:
      TZ: ${TIMEZONE}
    volumes:
      - ../data/media:/app/data/media
      - ../data/logs:/app/logs
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }
    networks: [mf-net]

  worker:
    image: mangaflow/api:${IMAGE_TAG:-latest}
    container_name: mf-worker
    restart: unless-stopped
    env_file: [.env]
    environment:
      TZ: ${TIMEZONE}
    command: >
      celery -A app.tasks.celery_app worker
      -Q urgent,normal,batch -c 4 -l info
      --max-tasks-per-child=50
    volumes:
      - ../data/media:/app/data/media
      - ../data/logs:/app/logs
    depends_on:
      api: { condition: service_started }
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }
    networks: [mf-net]

  flower:
    image: mangaflow/api:${IMAGE_TAG:-latest}
    container_name: mf-flower
    restart: unless-stopped
    env_file: [.env]
    command: celery -A app.tasks.celery_app flower --port=5555
    depends_on: [redis]
    ports:
      - "127.0.0.1:5555:5555"    # 仅本机可访问
    networks: [mf-net]

  frontend:
    build:
      context: ../frontend
      dockerfile: Dockerfile
      args:
        VITE_API_BASE_URL: ${VITE_API_BASE_URL}
    image: mangaflow/frontend:${IMAGE_TAG:-latest}
    container_name: mf-frontend
    restart: unless-stopped
    networks: [mf-net]

  nginx:
    image: nginx:1.27-alpine
    container_name: mf-nginx
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./nginx/conf.d:/etc/nginx/conf.d:ro
      - ../data/media:/app/data/media:ro
      - ./nginx/certs:/etc/nginx/certs:ro
    depends_on: [api, frontend]
    networks: [mf-net]

networks:
  mf-net:
    driver: bridge
```

### 5.6 运营管理端独立部署（独立端口/域名，RD-58）

运营管理端为**独立 Vue3 仓库 `mangaflow-admin-web`**，与创作端主仓库（`mangaflow-web`）解耦，按「完全独立端口 / 域名」部署（用户决策）。主 Nginx（§5.4）只服务 `mangaflow-web` + 后端 API，**不再代理 `/admin`**。

**方案 A（推荐，生产）：独立域名 + 独立公网 443**
- 申请独立域名（如 `console.mangaflow.com`），单独 ICP 备案与 TLS 证书。
- 独立 Nginx 服务块（独立容器，或同机独立 `server` 块），监听自身 443，静态资源来自 admin 仓库构建产物；`/api/` 反代到同一 `api:8000`（建议走内网，不暴露公网）。
- 优点：权限边界与证书隔离最彻底，便于独立扩容与独立域名运营。

**方案 B（演示 / 轻量）：同机独立公网端口**
- 复用同一台 CVM，新增 `admin-frontend` 容器，映射独立公网端口（如 `8080:80`）：

  ```yaml
  admin-frontend:
    build:
      context: ../mangaflow-admin-web
      dockerfile: Dockerfile
      args:
        VITE_API_BASE_URL: ${ADMIN_API_BASE_URL:-/api}
    image: mangaflow/admin-frontend:${IMAGE_TAG:-latest}
    container_name: mf-admin-frontend
    restart: unless-stopped
    ports:
      - "8080:80"
    networks: [mf-net]
  ```

- 访问 `http://<公网IP>:8080`；生产建议套独立域名 + TLS，勿长期裸端口暴露。

**共享设计系统**：两仓库均依赖 `@mangaflow/design`（墨青 / 藤紫 token + 通用组件），构建时由 npm 安装，UI 单一事实源，避免双仓漂移（详见架构文档 §3.1.4）。

**鉴权**：管理端登录页（A-01）独立；JWT 由同一 auth 服务签发，靠 `role=operator` 区分，后端 admin 接口按 role 门禁。

**数据库初始化脚本** `deploy/initdb/01-extensions.sql`：

```sql
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector（RD-15 / MA-07）
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- 模糊检索
```

---

## 6. 镜像构建

```bash
cd /opt/mangaflow/deploy

# 使用带时间戳的 tag，便于回滚
export IMAGE_TAG=$(date +%Y%m%d%H%M)

# 构建全部镜像（首次较慢，建议放后台）
docker compose build

# 仅构建后端 / 前端
docker compose build api
docker compose build frontend

# 查看镜像
docker images | grep mangaflow
```

> **回滚要点**：每次发布使用不同的 `IMAGE_TAG`，旧镜像保留，回滚时只需切回旧 tag（见第 14 章）。

---

## 7. 服务启动

```bash
cd /opt/mangaflow/deploy

# 启动全部服务
docker compose up -d

# 查看启动状态（等待 healthy）
docker compose ps

# 查看启动日志
docker compose logs -f --tail=100
```

**期望结果**：`mf-postgres` 与 `mf-redis` 为 `healthy`，`mf-api`、`mf-worker`、`mf-frontend`、`mf-nginx` 为 `running`。

---

## 8. 初始化

### 8.1 数据库迁移

```bash
# 执行 Alembic 迁移（创建全部业务表，见架构文档第 5 章）
docker compose exec api alembic upgrade head

# 确认表已创建
docker compose exec postgres psql -U mangaflow -d mangaflow -c "\dt"
```

### 8.2 校验扩展

```bash
docker compose exec postgres psql -U mangaflow -d mangaflow -c "\dx"
# 应包含 vector 与 pg_trgm
```

### 8.3 创建首个管理员

```bash
docker compose exec api python -m app.cli create-admin \
  --username admin \
  --email admin@example.com \
  --password '请设置强密码'
```

### 8.4 校验 LangGraph 检查点表

> 说明：`langgraph_checkpoints` / `langgraph_checkpoint_writes` / `langgraph_store` 由 LangGraph 的 `PostgresSaver` / `PostgresStore` **首次运行时自动建表**，无需手工脚本。

```bash
# 触发一次建表（执行一个最小图或调用健康检查接口）
curl -s http://localhost/api/health/graph

# 确认表已生成
docker compose exec postgres psql -U mangaflow -d mangaflow -c "\dt langgraph_*"
```

---

## 9. 验证清单

```bash
# 1. 容器状态
docker compose ps

# 2. 健康检查接口
curl -s http://localhost/api/health
# 期望：{"code":0,"data":{"status":"ok"},"message":"success"}

# 3. 数据库连通
docker compose exec api python -c "import asyncio;from app.core.db import check_db;print(asyncio.run(check_db()))"

# 4. Redis 连通
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" ping
# 期望：PONG

# 5. Celery Worker 在线
docker compose exec worker celery -A app.tasks.celery_app inspect ping
# 期望：->  celery@xxx: OK

# 6. 前端可访问
curl -sI http://localhost/ | head -1
# 期望：HTTP/1.1 200 OK

# 7. 上传与媒体目录可写
docker compose exec api touch /app/data/media/.wtest && echo "media ok"

# 8. FFmpeg 可用（合成能力，P-15）
docker compose exec worker ffmpeg -version | head -1

# 9. 中文字体已安装（字幕不乱码，P-16）
docker compose exec worker fc-list :lang=zh | head -3

# 10. 公网访问
# 浏览器打开 https://你的域名，完成注册 → 新建项目 → 剧本生成 全流程冒烟
```

**全流程冒烟脚本（建议上线前执行一遍）**

```bash
# 使用 curl 完成最小闭环验证
TOKEN=$(curl -s -X POST http://localhost/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"account":"admin","password":"请设置强密码"}' | grep -o '"token":"[^"]*"' | cut -d'"' -f4)
echo "token=${TOKEN:0:20}..."

# 创建项目
curl -s -X POST http://localhost/api/projects \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"冒烟测试","genre":"都市","style":"二次元","fidelity":"free","aspect_ratio":"9:16"}'
```

---

## 10. HTTPS 配置（腾讯云 SSL）

### 10.1 申请证书

腾讯云控制台 → SSL 证书 → 申请免费 DV 证书 → 下载 **Nginx 格式**，得到：

```
你的域名_bundle.crt
你的域名.key
```

### 10.2 部署证书

```bash
cd /opt/mangaflow/deploy/nginx
sudo mkdir -p certs
sudo cp /path/to/你的域名_bundle.crt certs/fullchain.pem
sudo cp /path/to/你的域名.key certs/privkey.pem
sudo chmod 600 certs/privkey.pem
```

### 10.3 启用 HTTPS

在 `deploy/nginx/conf.d/mangaflow.conf` 的 `server` 块前追加：

```nginx
server {
    listen 80;
    server_name 你的域名 www.你的域名;
    return 301 https://$host$request_uri;
}
```

并将原 `server` 块改为：

```nginx
server {
    listen 443 ssl http2;
    server_name 你的域名 www.你的域名;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;

    # ...（其余 location 配置保持不变）
}
```

```bash
docker compose restart nginx
curl -sI https://你的域名 | head -1
```

> 证书到期前需更新 cert 文件并 `docker compose restart nginx`；建议配置定时提醒。

---

## 11. 一键部署脚本

`scripts/deploy.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT="/opt/mangaflow"
DEPLOY_DIR="$ROOT/deploy"
BACKUP_DIR="$ROOT/data/backup"
STAMP="$(date +%Y%m%d%H%M)"
export IMAGE_TAG="$STAMP"

log() { echo -e "\033[1;32m[$(date '+%F %T')] $*\033[0m"; }
err() { echo -e "\033[1;31m[$(date '+%F %T')] 错误：$*\033[0m" >&2; }

cd "$ROOT"

# 0. 环境检查
log "检查 Docker 环境"
command -v docker >/dev/null || { err "未安装 Docker"; exit 1; }
docker compose version >/dev/null || { err "未安装 Docker Compose v2"; exit 1; }

# 1. 拉取代码
log "拉取最新代码"
git fetch --all
git reset --hard origin/main

# 2. 检查 .env
if [ ! -f "$DEPLOY_DIR/.env" ]; then
  log "未发现 .env，从模板创建（请务必修改密码与密钥后重新执行）"
  cp "$DEPLOY_DIR/.env.example" "$DEPLOY_DIR/.env"
  err "请编辑 $DEPLOY_DIR/.env 后重新运行本脚本"
  exit 1
fi

# 3. 部署前备份数据库
log "备份数据库"
mkdir -p "$BACKUP_DIR"
if docker ps --format '{{.Names}}' | grep -q '^mf-postgres$'; then
  docker compose -f "$DEPLOY_DIR/docker-compose.yml" exec -T postgres \
    pg_dump -U "$(grep ^POSTGRES_USER "$DEPLOY_DIR/.env" | cut -d= -f2)" \
            -d "$(grep ^POSTGRES_DB "$DEPLOY_DIR/.env" | cut -d= -f2)" \
    | gzip > "$BACKUP_DIR/db_$STAMP.sql.gz" || log "备份跳过（首次部署）"
fi

# 4. 构建镜像
log "构建镜像 tag=$IMAGE_TAG"
docker compose -f "$DEPLOY_DIR/docker-compose.yml" build

# 5. 启动服务
log "启动服务"
cd "$DEPLOY_DIR"
docker compose up -d

# 6. 等待健康
log "等待服务就绪"
for i in $(seq 1 30); do
  if curl -fsS http://localhost/api/health >/dev/null 2>&1; then
    log "API 已就绪"
    break
  fi
  [ "$i" -eq 30 ] && { err "API 启动超时，请查看日志：docker compose logs api"; exit 1; }
  sleep 5
done

# 7. 数据库迁移
log "执行数据库迁移"
docker compose exec -T api alembic upgrade head

# 8. 清理旧镜像（保留最近 3 个 tag）
log "清理悬空镜像"
docker image prune -f >/dev/null

log "部署完成 ✅  IMAGE_TAG=$IMAGE_TAG"
docker compose ps
```

**使用方式**

```bash
cd /opt/mangaflow
chmod +x scripts/*.sh
./scripts/deploy.sh
```

---

## 12. 常见问题排查

| # | 现象 | 排查与解决 |
| - | ---- | ---------- |
| 1 | 容器起不来 / 反复重启 | `docker compose logs <服务名> --tail=200`；常见原因为 `.env` 缺失或密码不一致 |
| 2 | API 报数据库连接失败 | 检查 `DATABASE_URL` 密码与 `POSTGRES_PASSWORD` 是否一致；确认 `postgres` 为 `healthy`（`docker compose ps`） |
| 3 | 报 `extension "vector" does not exist` | 使用了非 pgvector 镜像或未执行 initdb；改用 `pgvector/pgvector:pg16`，并确认 `deploy/initdb/01-extensions.sql` 已挂载 |
| 4 | Celery 任务一直排队不执行 | `docker compose exec worker celery -A app.tasks.celery_app inspect active`；检查 `CELERY_BROKER_URL` 与 Redis 密码；确认队列名与 `-Q` 参数一致 |
| 5 | 视频合成字幕中文显示为方块 | worker 镜像缺中文字体；确认已安装 `fonts-noto-cjk` 并执行 `fc-cache -fv`（见 5.2） |
| 6 | 上传大文件报 413 | 调大 nginx `client_max_body_size`（已设 200m）与后端上传限制，重启 nginx |
| 7 | SSE 流式无输出 / 一次性返回 | nginx 未关闭缓冲；确认 `/api/stream/` 配置含 `proxy_buffering off` 与 `proxy_http_version 1.1` |
| 8 | WebSocket 连接失败 | 检查 `/ws/` 的 `Upgrade` / `Connection` 头配置；安全组放行 443 |
| 9 | 生成任务全部失败且提示模型错误 | 检查 `.env` 中模型渠道 Base URL / API Key；在管理端（AD-03）做连通性测试；确认服务器可出网 |
| 10 | 服务器内存不足 / OOM | `free -h`、`docker stats`；降低 worker 并发（`-c 4` 改小）、限制 Redis `maxmemory`、加 swap |
| 11 | 磁盘写满 | `df -h`；清理 `data/logs`、旧镜像（`docker image prune -a`）、旧备份；媒体建议接 S3（AD 存储配置） |
| 12 | 容器时间与本地不一致 | `.env` 的 `TIMEZONE` 与 compose 的 `TZ` 需同时设置为 `Asia/Shanghai` |
| 13 | 管理端 5555 无法访问 | Flower 已绑定 `127.0.0.1`（安全考虑）；需本地 SSH 隧道：`ssh -L 5555:127.0.0.1:5555 root@服务器IP` |
| 14 | HTTPS 证书报错 | 确认证书路径挂载正确、`privkey.pem` 权限 600、证书链完整（用 `fullchain.pem`） |
| 15 | 部署后页面 404 | 前端构建失败或未重新构建；`docker compose build frontend --no-cache && docker compose up -d frontend` |
| 16 | 迁移报表已存在 | 检查 Alembic 版本：`docker compose exec api alembic current`；必要时 `alembic stamp head` 对齐 |
| 17 | Redis 报 NOAUTH | `REDIS_PASSWORD` 与 `CELERY_*` 中的密码不一致（注意 URL 内密码需 URL 编码特殊字符） |
| 18 | 修改 `.env` 后不生效 | 需重建容器：`docker compose up -d --force-recreate` |

**通用排查命令**

```bash
# 查看某服务最近日志
docker compose logs -f --tail=200 api

# 进入容器
docker compose exec api bash

# 资源占用
docker stats --no-stream

# 网络连通（容器内）
docker compose exec api curl -sI https://www.baidu.com
```

---

## 13. 备份与恢复

### 13.1 备份脚本

`scripts/backup.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT="/opt/mangaflow"
STAMP="$(date +%Y%m%d%H%M)"
BACKUP_DIR="$ROOT/data/backup"
KEEP_DAYS=7

mkdir -p "$BACKUP_DIR"
cd "$ROOT/deploy"

echo "[$(date '+%F %T')] 备份数据库..."
docker compose exec -T postgres pg_dump -U mangaflow -d mangaflow \
  | gzip > "$BACKUP_DIR/db_$STAMP.sql.gz"

echo "[$(date '+%F %T')] 备份媒体文件..."
tar -czf "$BACKUP_DIR/media_$STAMP.tar.gz" -C "$ROOT/data" media

echo "[$(date '+%F %T')] 清理 $KEEP_DAYS 天前的备份..."
find "$BACKUP_DIR" -name "*.gz" -mtime +$KEEP_DAYS -delete

echo "备份完成：$BACKUP_DIR/db_$STAMP.sql.gz"
```

### 13.2 定时任务

```bash
# 每日 3 点备份
crontab -e
# 追加一行：
0 3 * * * /opt/mangaflow/scripts/backup.sh >> /opt/mangaflow/data/logs/backup.log 2>&1
```

> 建议同时开启腾讯云 **云硬盘快照** 策略（每日自动快照，保留 7 天），对应 PRD 数据备份要求。

### 13.3 恢复脚本

`scripts/restore.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT="/opt/mangaflow"
FILE="${1:?用法: restore.sh <db_YYYYmmddHHMM.sql.gz>}"

cd "$ROOT/deploy"

echo "⚠️  即将用 $FILE 覆盖当前数据库，5 秒后执行（Ctrl+C 取消）"
sleep 5

echo "停止应用容器..."
docker compose stop api worker flower

echo "恢复数据库..."
docker compose exec -T postgres dropdb -U mangaflow --if-exists mangaflow
docker compose exec -T postgres createdb -U mangaflow mangaflow
gunzip -c "$FILE" | docker compose exec -T postgres psql -U mangaflow -d mangaflow

echo "启动应用容器..."
docker compose start api worker flower

echo "恢复完成，请执行验证清单（第 9 章）"
```

---

## 14. 回滚方案

采用**镜像 tag 回滚 + 数据库回滚**双轨策略。

### 14.1 应用回滚（推荐，分钟级）

```bash
cd /opt/mangaflow/deploy

# 1. 查看可用历史 tag
docker images mangaflow/api --format '{{.Tag}}\t{{.CreatedAt}}'

# 2. 指定旧 tag 回滚
export IMAGE_TAG=202610071530
docker compose up -d --no-build

# 3. 验证
curl -s http://localhost/api/health
docker compose ps
```

### 14.2 代码回滚

```bash
cd /opt/mangaflow
git log --oneline -10
git reset --hard <目标commit>
./scripts/deploy.sh
```

### 14.3 数据库回滚

| 情形 | 处置 |
| ---- | ---- |
| 迁移可逆 | `docker compose exec api alembic downgrade -1` |
| 迁移不可逆 / 数据损坏 | 用最近备份恢复：`./scripts/restore.sh data/backup/db_YYYYmmddHHMM.sql.gz` |
| 仅个别数据错误 | 定点修复，禁止整库回灌 |

### 14.4 回滚检查清单

- [ ] 容器全部 `running`，`postgres` / `redis` 为 `healthy`
- [ ] `/api/health` 返回 `code=0`
- [ ] 前端首页可访问，登录正常
- [ ] Celery Worker `inspect ping` 正常
- [ ] 关键业务流程冒烟（新建项目 → 剧本生成 → 任务提交）
- [ ] 回滚操作写入运维记录（时间、原因、执行人、结果）

---

## 15. 运维常用命令

```bash
cd /opt/mangaflow/deploy

# 服务
docker compose ps                      # 状态
docker compose up -d                   # 启动 / 更新
docker compose restart api             # 重启单服务
docker compose stop                    # 停止全部
docker compose down                    # 停止并删除容器（保留卷）
docker compose logs -f --tail=200 api  # 实时日志
docker compose exec api bash           # 进入容器

# 资源
docker stats --no-stream               # 资源占用
df -h                                  # 磁盘
free -h                                # 内存
docker system df                       # Docker 占用

# 清理
docker image prune -f                  # 悬空镜像
docker volume ls                       # 卷列表（勿误删数据卷）

# 数据库
docker compose exec postgres psql -U mangaflow -d mangaflow
docker compose exec api alembic current
docker compose exec api alembic history

# 任务
docker compose exec worker celery -A app.tasks.celery_app inspect active
docker compose exec worker celery -A app.tasks.celery_app inspect reserved
docker compose exec worker celery -A app.tasks.celery_app purge   # 清空队列（谨慎）
```

---

## 16. 附录

### 16.1 端口清单

| 端口 | 对外 | 说明 |
| ---- | ---- | ---- |
| 22 | 是（限 IP） | SSH |
| 80 | 是 | HTTP（跳转 HTTPS） |
| 443 | 是 | HTTPS（主域 `mangaflow-web`，方案 A 下 admin 独立域名亦用 443） |
| 8080 | 是（仅方案 B 演示） | 运营管理端独立前端 `mangaflow-admin-web`（独立仓库，RD-58） |
| 5555 | 否（仅本机） | Flower |
| 5432 / 6379 / 8000 | 否 | 仅容器内网 |

### 16.2 持久化目录清单

| 宿主机路径 | 容器路径 | 内容 | 备份 |
| ---------- | -------- | ---- | ---- |
| `data/postgres` | `/var/lib/postgresql/data` | 业务库 + 向量 | 必须 |
| `data/redis` | `/data` | 队列持久化 | 可选 |
| `data/media` | `/app/data/media` | 上传与生成媒体 | 必须 |
| `data/logs` | `/app/logs` | 应用日志 | 可选 |
| `data/backup` | — | 备份文件 | 建议异地 |

### 16.3 上线前检查清单

- [ ] `.env` 中所有默认密码与密钥已更换（含 AES/JWT/DB/Redis）
- [ ] 安全组仅开放 22 / 80 / 443（采用方案 B 时另开 8080），5555 未公网暴露
- [ ] HTTPS 证书已配置且自动跳转生效
- [ ] 数据库与媒体目录挂载持久化，云硬盘快照策略已开启
- [ ] 每日备份定时任务已生效并验证过一次恢复
- [ ] 管理员账号已创建且密码为强密码
- [ ] 模型渠道（AD-03）已配置并连通性测试通过
- [ ] 内容合规审核开关（`AUDIT_ENABLE`）已按上线要求设置
- [ ] 监控告警（SYS-02）已配置：生成成功率、队列积压、模型错误率、资损指标
- [ ] 第 9 章验证清单全部通过

---

*本方案为产品设计参考，具体实施请结合公司合规与法务要求确认。*

*— 文档结束 —*
