# 漫序 MangaFlow · 一键部署（deploy/ 目录说明）

本目录是《漫序 MangaFlow 腾讯云Linux服务器Docker一键部署操作文档 V1.0》的**落库实现**：部署方案从文档变为仓库内可执行文件。前置条件仅需 Docker（含 Compose v2 插件），`docker --version` ≥ 24 即可。

## 1. 文件清单

| 文件 | 说明 |
| ---- | ---- |
| `backend.Dockerfile` | 后端镜像：python:3.12-slim + uvicorn，容器内端口 8300；内置 ffmpeg 与中文字体（视频合成/字幕烧录，P-15/P-16） |
| `web.Dockerfile` | 创作端镜像：node:22-alpine 构建（镜像内先构建共享包 mangaflow-design）→ nginx:alpine 托管 dist |
| `admin.Dockerfile` | 管理端镜像：同上，构建时 `vite build --base=/admin/` 适配子路径部署 |
| `web-nginx.conf` | 创作端镜像内置站点配置（SPA history 路由 fallback） |
| `admin-nginx.conf` | 管理端镜像内置站点配置（剥 `/admin/` 前缀 + SPA fallback） |
| `nginx.conf` | **网关**：`/` → 创作端、`/admin/` → 管理端、`/api` `/storage` → 后端（SSE 支持） |
| `docker-compose.yml` | 全套服务编排（postgres / backend / web / admin-web / nginx） |
| `.env.example` | 环境变量模板（复制为 `.env` 后按需修改） |

仓库根的 `.dockerignore` 控制构建上下文体积（排除 `.git`/`node_modules`/`.venv`/`storage`/测试报告/开发文档等）。

## 2. 端口约定（宿主统一 1xxxx 高位）

| 宿主端口 | 容器 | 服务 | 备注 |
| -------- | ---- | ---- | ---- |
| **18080** | 80 | nginx 网关 | ★ 对外统一入口 |
| 15432 | 5432 | postgres (pgvector/pgvector:pg16) | 仅本机调试用，生产建议删除该映射 |
| 18300 | 8300 | backend (FastAPI) | 仅冒烟/调试用，正常走网关 `/api` |
| 15173 | 80 | web（创作端） | 仅调试用，正常走网关 `/` |
| 15174 | 80 | admin-web（管理端） | 仅调试用，正常走网关 `/admin/` |

> 高位映射是为了不与本机开发环境已占用的 5432 / 8300 / 5173 / 5174 / 80 冲突；容器之间通过 compose 网络以服务名互访，端口彼此独立。

## 3. 一键部署步骤

```bash
# 0) 准备环境变量（密码/密钥默认值仅用于本地演示，生产必须修改）
cd deploy
cp .env.example .env
# 编辑 .env：至少替换 POSTGRES_PASSWORD / JWT_SECRET_KEY / AES_KEY

# 1) 构建全部自建镜像（首次构建较慢，npm/pip 镜像源见 §5 常见问题）
docker compose -f deploy/docker-compose.yml build

# 2) 启动全部服务（postgres 先行健康检查，backend 随后自动建库建表+种子）
docker compose -f deploy/docker-compose.yml up -d

# 3) 观察状态（期望 postgres 为 healthy，其余 running）
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f --tail=100 backend
```

## 4. 验证清单

```bash
# 1. 后端健康（Prometheus 指标端点，直连后端映射端口）
curl http://localhost:18300/api/metrics        # 期望：text/plain Prometheus 输出

# 2. 走网关访问 API
curl http://localhost:18080/api/metrics        # 期望：同上（网关反代生效）

# 3. 媒体静态目录（后端 StaticFiles 挂载）
curl -I http://localhost:18080/storage/        # 期望：200/403/404 均说明反代可达（非 502）

# 4. 创作端 SPA + history 路由 fallback
curl -I http://localhost:18080/                # 期望：200
curl -I http://localhost:18080/login           # 期望：200（fallback 到 index.html）

# 5. 管理端 SPA + 子路径 fallback
curl -I http://localhost:18080/admin/          # 期望：200
curl -I http://localhost:18080/admin/users     # 期望：200（fallback）

# 6. 数据库连通与扩展
docker compose -f deploy/docker-compose.yml exec postgres \
  psql -U postgres -d mangaflow -c "select version();"
```

收尾（保留数据卷）：

```bash
docker compose -f deploy/docker-compose.yml down          # 停止并删除容器，卷保留
docker compose -f deploy/docker-compose.yml down -v       # ⚠ 连数据卷一起删除（清库）
```

## 5. 常见问题

| # | 现象 | 处理 |
| - | ---- | ---- |
| 1 | 端口占用（`bind: address already in use`） | 修改 `docker-compose.yml` 对应 `ports` 左侧宿主端口即可，容器内端口勿动 |
| 2 | 基础镜像/依赖拉取慢 | 基础镜像走 Docker 镜像加速器（见原部署文档 §3.2）；npm 依赖在 `deploy/.env` 设 `NPM_REGISTRY=https://registry.npmmirror.com`，pip 设 `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` 后重新 build |
| 3 | npm 构建失败提示找不到 `@mangaflow/design` | build context 必须是仓库根（compose 已配置 `context: ..`）；勿单独进入前端目录手动 build |
| 4 | 访问 502 | `docker compose ps` 查看 backend 是否 healthy；`docker compose logs backend` 看数据库连接（常见为 `.env` 密码与连接串不一致） |
| 5 | SSE 通知/生成进度不实时 | 确认使用网关入口访问（`nginx.conf` 已全局 `proxy_buffering off` + 600s 超时），而非其他未关缓冲的反代 |
| 6 | 管理端样式/JS 404 | 管理端必须经 `/admin/` 前缀访问（构建 base=`/admin/`），直连 `admin-web:15174` 裸根会跳转 `/admin/` |
| 7 | 数据库密码含特殊字符导致连不上 | 密码避免 `@ / : # ?` 等字符，或自行 URL 编码后同步修改 `.env` 中的连接串 |
| 8 | 修改了 `.env` 不生效 | `docker compose up -d --force-recreate` 重建容器 |

## 6. 与《部署文档 V1.0》的对应关系与差异

**保持一致的部分**：单机 Docker Compose 架构、pgvector/pgvector:pg16 数据库镜像、ffmpeg + 中文字体打进后端镜像、`client_max_body_size 200m`、SSE 关缓冲长超时思路、`.env.example` 环境变量模式、data 持久化卷思路（本实现用命名卷替代文档的宿主 bind 目录，`down -v` 可清）。

**有差异的部分（以本目录落库文件为准）**：

| # | 文档 V1.0 | 本实现 | 原因 |
| - | --------- | ------ | ---- |
| 1 | 后端端口 8000 | **8300**（容器内与映射 18300） | 本仓库开发约定 8300（本机 8000 被占用），与 `mangaflow.ps1`、vite 代理配置一致 |
| 2 | Python 3.13-slim | **Python 3.12-slim** | 本仓库 requirements 以 3.12 实测为准 |
| 3 | 前端镜像 node:20（容器清单）/node:22（§5.3） | **node:22-alpine** | 两个前端 `engines` 要求 `^22.18.0 \|\| >=24.12.0`，node:20 不满足 |
| 4 | api 镜像 context=`../backend` | **context=仓库根** | 前端依赖本地包 `mangaflow-design`（`file:../` 相对引用），context 必须包含它 |
| 5 | 管理端独立端口 8080（方案 B） | **网关 `/admin/` 子路径**（构建 `--base=/admin/`） | 统一单入口 18080，免额外端口/域名；文档方案 A（独立域名）可复用本镜像实现 |
| 6 | Redis / Celery worker / Flower 容器 | **暂未落库** | 当前代码库任务执行为进程内实现，无 Redis/Celery 依赖；后续引入时按文档补齐 |
| 7 | `/media/` 由 nginx alias 本地目录 | **`/storage/` 反代 backend StaticFiles** | 与当前代码实现一致（FastAPI 挂载 `/storage`），避免双份存储路径配置 |
| 8 | `/ws/` WebSocket 代理段 | **暂未落库** | 当前代码仅 SSE 推送；新增 WS 端点时仿照 SSE 段补 Upgrade 头即可 |
| 9 | `deploy/initdb/01-extensions.sql` 建扩展 | **暂未落库** | pgvector 镜像自带 vector 扩展，`CREATE EXTENSION` 由后续使用向量功能的任务执行时补 |
| 10 | 花哨的 health/worker 验证步骤 | 验证聚焦 `/api/metrics` 与前端可达性 | 与当前代码实际暴露的端点对齐 |

## 7. 一键脚本（可选）

Windows（PowerShell，仓库根执行）：

```powershell
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml ps
```

Linux/macOS 同理。后续可按文档 §11 增加 `scripts/deploy.sh`（备份/迁移/回滚增强），本目录先提供编排层最小可执行集。
