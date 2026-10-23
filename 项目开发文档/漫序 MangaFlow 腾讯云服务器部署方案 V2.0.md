# 漫序 MangaFlow 腾讯云服务器部署方案 V2.0

| 项 | 内容 |
| ---- | ---- |
| 文档版本 | V2.0（2026-10-22） |
| 适用环境 | 腾讯云轻量/CVM 单机 Docker Compose 生产部署 |
| 替代关系 | 在 V1.0（网关统一入口 18080 + `/admin/` 子路径）基础上，按新的端口要求重构生产拓扑：**用户端独占 8006、运营端独占 8007**；V1.0 编排（`deploy/docker-compose.yml`）保留用于开发/演示机 |
| 落库实现 | `deploy/docker-compose.prod.yml` + `deploy/web-nginx.prod.conf` + `deploy/admin-nginx.prod.conf` + `deploy/backend-entrypoint.sh`（均已通过 `docker compose config` 校验） |
| 关联文档 | 《漫序 MangaFlow 腾讯云Linux服务器Docker一键部署操作文档 V1.0》、`deploy/README.md` |

---

## 1. 服务器环境与前提

### 1.1 服务器配置

| 项 | 配置 | 部署影响 |
| ---- | ---- | ---- |
| CPU | 2 核 | 资源限额按 2 核分配（见 §2 服务矩阵） |
| 内存 | 2GB | 必须配置 2GB swap（§1.3）；容器内存限额总计 1.54GB，为系统预留余量 |
| 系统盘 | SSD 云硬盘 50GB | 镜像约 2GB + 数据卷 + 日志（已限轮转 10m×3），空间充足 |
| 流量包 | 300GB/月（带宽 4Mbps） | 前端已开启 gzip + 7 天静态缓存；4Mbps 出口约 512KB/s，后续可接 COS+CDN（§11） |
| 操作系统 | Ubuntu Server 24.04 LTS 64bit | 命令均为 bash/sudo |
| 预装应用 | Docker 27.5.1 | 含 Compose v2 插件 |
| 公网 IP | 82.156.216.134 | 文档中所有外网访问地址以此为例 |

### 1.2 环境验证

```bash
docker --version           # 期望：Docker version 27.5.1
docker compose version     # 期望：v2.x 及以上
sudo systemctl is-active docker   # 期望：active
```

### 1.3 配置 2GB swap（必做）

2GB 内存下，前端镜像内执行 `vite build` 有内存峰值，swap 是构建与运行稳定性的前提；生产编排同时已将构建堆上限约束为 `NODE_OPTIONS=--max-old-space-size=1536`（双保险）。

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab   # 重启后仍生效
free -h    # 确认 Swap 一行显示 2.0Gi
```

---

## 2. 部署拓扑

### 2.1 架构示意

```text
                        腾讯云安全组（仅放行 22 / 8006 / 8007）
                                        │
        ┌─────────────────── mf-app 业务网（bridge）───────────────────┐
        │                              │                               │
  宿主 8006 → mf-prod-web:80     宿主 8007 → mf-prod-admin-web:80   mf-prod-backend:8300
  （用户端 SPA + nginx 反代）    （运营端 SPA + nginx 反代）        （FastAPI 单 worker）
        │        │                      │        │                     │        │
        │  /api  └──────────────────────│──/api──┘   StaticFiles←──────┘        │
        │ /storage                      │/storage     （出公网调用 DeepSeek/Agnes/百炼）
        └───────────────────────────────┼───────────────────────────────────────┘
                                        │
                        ┌───── mf-internal 数据网（internal，禁出公网）─────┐
                        │                                                 │
                  mf-prod-backend:8300  ──────→  mf-prod-postgres:5432（pgvector/pg16）
                        命名卷 mf_prod_storage              命名卷 mf_prod_postgres_data
```

要点：

- **无独立网关容器**：用户端 / 运营端两个前端容器内置 nginx 各自反代 `/api`、`/storage` 到后端（SSE 关缓冲 + 600s 长超时 + 上传上限 200m），较 V1.0 少一个容器、少一层转发；
- **后端与数据库不暴露公网**：`backend`、`postgres` 均无宿主端口映射；数据库所在 `mf-internal` 网络标记 `internal: true`，禁止出公网；
- **安全基线**：后端容器以非 root 用户运行（entrypoint 修复存储卷属主后经 gosu 降权）；三处敏感变量为必填语法（未配置强密码/密钥时拒绝启动）；日志全部轮转。

### 2.2 服务矩阵

| 组件 | 源码依据 | 镜像 | 容器内端口 | 公网端口 | 持久卷 | 内存限额 |
| ---- | ---- | ---- | ---- | ---- | ---- | ---- |
| 用户端 SPA | `mangaflow-web`（Vue3 + Vite，`baseURL '/api'`，base=/） | `mangaflow/web` | 80 | **8006** | — | 64M |
| 运营端 SPA | `mangaflow-admin-web`（Vue3 + Vite，`ADMIN_BASE=/`） | `mangaflow/admin-web` | 80 | **8007** | — | 64M |
| 后端 API | `backend/app/main.py`（FastAPI，`uvicorn app.main:app`，挂载 `/api` 路由与 `/storage` StaticFiles） | `mangaflow/backend` | 8300 | 不暴露 | `mf_prod_storage` | 896M |
| 数据库 | pgvector（业务库 + 向量检索） | `pgvector/pgvector:pg16` | 5432 | 不暴露（internal） | `mf_prod_postgres_data` | 512M |

资源预算：容器限额合计 1.54GB + Docker 守护进程与系统约 0.4GB ≈ 2GB，配合 2GB swap 保障峰值；CPU 限额 postgres 1.0 / backend 1.5 / 前端各 0.5。

---

## 3. 源码获取与目录结构

```bash
# 推荐：git 拉取（服务器上检出为 LF 行尾，天然规避 Windows CRLF 问题）
sudo mkdir -p /opt/mangaflow && cd /opt/mangaflow
sudo git clone <仓库地址> .
sudo chown -R $USER:$USER /opt/mangaflow
```

无外网 git 权限时备选：本机执行 `git archive HEAD -o mangaflow.zip`（或排除 `.git` 的 tar 包）后 scp 上传解压。

服务器上的关键目录结构：

```text
/opt/mangaflow/
├── backend/                     # FastAPI 后端源码
├── mangaflow-web/               # 用户端源码（依赖 ../mangaflow-design）
├── mangaflow-admin-web/         # 运营端源码（依赖 ../mangaflow-design）
├── mangaflow-design/            # 前端共享设计系统（file: 依赖，必须随仓库同在）
├── deploy/
│   ├── docker-compose.prod.yml  # ★ 生产编排（本方案）
│   ├── web-nginx.prod.conf      # ★ 用户端生产站点配置（运行时挂载）
│   ├── admin-nginx.prod.conf    # ★ 运营端生产站点配置（运行时挂载）
│   ├── backend-entrypoint.sh    # ★ 后端入口脚本（卷属主修复 + 降权）
│   ├── backend.Dockerfile / web.Dockerfile / admin.Dockerfile
│   ├── .env.example             # 环境变量模板（复制为 .env）
│   └── docker-compose.yml       # 开发/演示编排（本方案不用）
└── .dockerignore                # 构建上下文排除清单（已放行上述 deploy 文件）
```

> 注意：前端依赖本地包 `"@mangaflow/design": "file:../mangaflow-design"`，构建上下文固定为仓库根，**不要**单独进入前端目录构建。

后续更新代码：`cd /opt/mangaflow && git pull`，然后按 §6.3 更新发布流程操作。

---

## 4. 环境变量与密钥

### 4.1 生成 .env

```bash
cd /opt/mangaflow/deploy
cp .env.example .env
chmod 600 .env    # 含密钥，仅 root/属主可读
```

### 4.2 必改项与生成命令

生产编排对以下三项使用必填校验（`${VAR:?}` 语法），不设置会在 `up` 时直接报错拒绝启动：

| 变量 | 生成命令 | 说明 |
| ---- | ---- | ---- |
| `POSTGRES_PASSWORD` | `openssl rand -hex 16` | 数据库密码；hex 字符串不含 `@ / : #` 等特殊字符，连接串免 URL 编码 |
| `JWT_SECRET_KEY` | `openssl rand -hex 32` | JWT 签名密钥 |
| `AES_KEY` | `openssl rand -hex 16` | 模型渠道 API Key 落库加密密钥（32 字符十六进制串，符合 16/24/32 字节要求） |

### 4.3 .env 变量速查

| 分组 | 变量 | 默认值 / 说明 |
| ---- | ---- | ---- |
| PostgreSQL | `POSTGRES_USER` / `POSTGRES_DB` | `postgres` / `mangaflow`（一般不改） |
| 端口 | `WEB_PORT` / `ADMIN_PORT` | **8006 / 8007**（被占用时可改，改后同步调整安全组） |
| 构建加速 | `NPM_REGISTRY` / `PIP_INDEX_URL` | 国内服务器建议 `https://registry.npmmirror.com` / `https://pypi.tuna.tsinghua.edu.cn/simple` |
| 镜像标签 | `IMAGE_TAG` | `latest`；发布时可设时间戳 tag 便于回滚 |
| 模型渠道 Key | `DEEPSEEK_API_KEY` / `AGNES_LLM_API_KEY` / `AGNES_IMAGE_API_KEY` / `AGNES_VIDEO_API_KEY` / `BAILIAN_TTS_API_KEY` | 默认空 = 该渠道走本地 mock 演示占位（产物带 placeholder 标记并免计费，启动日志有告警）；启用真实 AI 能力时线下获取后填入 |
| 业务开关 | `ALEMBIC_ON_STARTUP` / `PUNISHMENT_ENABLED` / `CORS_ORIGINS` / `PLUGIN_ALLOWED_HOSTS` | 默认 `false` / `false` / `["*"]` / `[]`，含义见 `backend/.env.example` 注释 |

> 连接串 `DATABASE_URL` / `ADMIN_DATABASE_URL` 由编排按 `POSTGRES_*` 自动拼串（主机名为服务名 `postgres`），无需手填；如需覆盖整串请保持密码一致。

---

## 5. 防火墙与安全组

### 5.1 腾讯云控制台安全组（主防线）

控制台 → 云服务器 → 安全组 → 绑定实例的入站规则：

| 协议端口 | 来源 | 策略 | 用途 |
| ---- | ---- | ---- | ---- |
| TCP:22 | 建议限定为常用办公出口 IP | 允许 | SSH 管理 |
| TCP:8006 | 0.0.0.0/0 | 允许 | 用户端入口 |
| TCP:8007 | 0.0.0.0/0 | 允许 | 运营端入口 |
| 其余全部 | 0.0.0.0/0 | 拒绝 | 默认拒绝 |

> `5432`（postgres）、`8300`（backend）在编排中本就没有宿主端口映射，公网不可达，**不要**为了"调试方便"临时放行或添加映射。

### 5.2 系统 UFW（可选，与安全组叠加）

```bash
sudo ufw allow 22/tcp && sudo ufw allow 8006/tcp && sudo ufw allow 8007/tcp
sudo ufw enable && sudo ufw status
```

---

## 6. 构建与启动

以下命令均假设当前目录为 `/opt/mangaflow`。

### 6.1 首次构建与启动

```bash
# 0) 前置：已完成 §1.3 swap 与 §4 .env
# 1) 构建全部自建镜像（首次 5~15 分钟；4Mbps 下拉取基础镜像较慢属正常）
sudo docker compose -f deploy/docker-compose.prod.yml build

# 2) 启动（postgres 健康检查通过后 backend 自动建库建表 + 种子数据）
sudo docker compose -f deploy/docker-compose.prod.yml up -d

# 3) 观察状态
sudo docker compose -f deploy/docker-compose.prod.yml ps
sudo docker compose -f deploy/docker-compose.prod.yml logs -f --tail=100 backend
```

期望状态：`mf-prod-postgres` 与 `mf-prod-backend` 为 `Up (healthy)`，两个前端为 `Up (healthy)`；backend 有 90s 启动宽限期（首次建库建表 + 种子），稍等后刷新 `ps`。

### 6.2 常用运维命令

| 操作 | 命令（`DC="sudo docker compose -f deploy/docker-compose.prod.yml"`） |
| ---- | ---- |
| 查看状态 | `$DC ps` |
| 查看日志 | `$DC logs -f --tail=100 <service>`（service ∈ web / admin-web / backend / postgres） |
| 启动 / 停止 | `$DC start` / `$DC stop` |
| 重启 | `$DC restart`（改 `.env` 后用 `$DC up -d --force-recreate`） |
| 停止并删除容器 | `$DC down`（数据卷保留） |
| ⚠ 连数据卷一起删除 | `$DC down -v`（**清库 + 清空媒体文件**，仅测试环境使用） |
| 进入容器调试 | `$DC exec backend sh` / `$DC exec postgres psql -U postgres -d mangaflow` |

### 6.3 更新发布与回滚

```bash
# 更新发布
cd /opt/mangaflow && git pull
sudo docker compose -f deploy/docker-compose.prod.yml build
sudo docker compose -f deploy/docker-compose.prod.yml up -d
sudo docker compose -f deploy/docker-compose.prod.yml ps        # 按 §7 复检

# 回滚到旧版本镜像（若发布时 IMAGE_TAG 用了时间戳 tag）
# .env 中将 IMAGE_TAG 改回上一个可用 tag，然后：
sudo docker compose -f deploy/docker-compose.prod.yml up -d
```

> 数据库结构变更：当前应用启动时自动 `create_all` 兜底；涉及破坏性迁移时在 `.env` 临时设 `ALEMBIC_ON_STARTUP=true` 重启 backend（lifespan 会先执行 `alembic upgrade head`），完成后改回 `false`。

---

## 7. 健康检查与验收

### 7.1 服务器本机检查

```bash
# 容器健康状态（四者均应为 healthy）
sudo docker compose -f deploy/docker-compose.prod.yml ps

# 用户端 SPA 与路由 fallback
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8006/        # 期望 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8006/login   # 期望 200

# 运营端 SPA
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8007/        # 期望 200

# API（经前端 nginx 反代到 backend；/api/metrics 为免鉴权 Prometheus 端点）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8006/api/metrics   # 期望 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8007/api/metrics   # 期望 200

# 媒体目录（backend StaticFiles；200/403/404 均说明反代可达，502 才是异常）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8006/storage/

# 数据库连通与 pgvector 扩展
sudo docker compose -f deploy/docker-compose.prod.yml exec postgres \
  psql -U postgres -d mangaflow -c "select version();"
```

### 7.2 外网浏览器验收

| 检查点 | 地址 | 期望 |
| ---- | ---- | ---- |
| 用户端首页 | `http://82.156.216.134:8006` | 用户端界面正常渲染 |
| 运营端首页 | `http://82.156.216.134:8007` | 运营端登录页正常渲染 |
| SSE 实时性 | 触发一次生成任务 | 进度/通知实时推送（nginx 已关缓冲） |

冒烟测试（种子账号，**上线后立即改密**，见 §11）：

1. 运营端登录：`admin` / `admin123`（operator 角色）；
2. 用户端登录：`demo` / `demo123456`（user 角色，5 万积分）；
3. 走一遍核心链路（创作 → 生成 → 成片列表）确认读写与 `/storage` 媒体回显正常。

> 未配置模型渠道 Key 时，AI 产物为本地 mock 演示占位（带 placeholder 标记、免计费），属预期行为；配置真实 Key 后重启 backend 即启用真实能力。

---

## 8. 日志与资源诊断

```bash
# 实时资源占用（内存列对照 §2.2 限额，接近限额说明需要扩容或排查）
sudo docker stats --no-stream

# 磁盘水位（50GB 盘关注 / 使用率；镜像约 2GB、构建缓存可用 prune 清理）
df -h
sudo docker system df

# 构建缓存 / 悬空镜像清理（不影响运行中的容器与数据卷）
sudo docker builder prune -f
sudo docker image prune -f
```

容器日志已统一配置 `json-file` 轮转（单容器 10MB × 3 份），不会写满磁盘；`docker logs` 与 `compose logs` 通用。

---

## 9. 备份与恢复

### 9.1 手动备份

```bash
mkdir -p /opt/mangaflow/backups
DC="sudo docker compose -f deploy/docker-compose.prod.yml"

# 数据库（自定义格式，pg_restore 可恢复）
$DC exec postgres pg_dump -U postgres -d mangaflow -Fc \
  -f /tmp/mangaflow_$(date +%F).dump
sudo docker cp mf-prod-postgres:/tmp/mangaflow_$(date +%F).dump /opt/mangaflow/backups/

# 媒体存储卷（上传 + 生成产物）
sudo docker run --rm \
  -v mangaflow-prod_mf_prod_storage:/data:ro \
  -v /opt/mangaflow/backups:/backup \
  alpine tar czf /backup/storage_$(date +%F).tgz -C /data .

# 环境与部署配置（不含数据库，但含密钥，注意保管）
sudo tar czf /opt/mangaflow/backups/deploy_conf_$(date +%F).tgz \
  -C /opt/mangaflow/deploy .env docker-compose.prod.yml *.prod.conf
```

### 9.2 定时备份（cron 示例，每日 03:00）

```bash
sudo crontab -e
# 追加（示例：数据库 + 存储，保留 7 天由 find 清理）
0 3 * * * docker compose -f /opt/mangaflow/deploy/docker-compose.prod.yml exec -T postgres pg_dump -U postgres -d mangaflow -Fc > /opt/mangaflow/backups/db_$(date +\%F).dump && find /opt/mangaflow/backups -name 'db_*.dump' -mtime +7 -delete
```

### 9.3 恢复（谨慎：覆盖当前数据，建议先停后端写入）

```bash
# 1) 停止业务写入（保留 postgres 运行）
sudo docker compose -f deploy/docker-compose.prod.yml stop backend

# 2) 恢复数据库
sudo docker cp backups/db_YYYY-MM-DD.dump mf-prod-postgres:/tmp/restore.dump
sudo docker compose -f deploy/docker-compose.prod.yml exec postgres \
  pg_restore -U postgres -d mangaflow --clean --if-exists /tmp/restore.dump

# 3) 恢复媒体存储
sudo docker run --rm -v mangaflow-prod_mf_prod_storage:/data -v /opt/mangaflow/backups:/backup \
  alpine sh -c "cd /data && tar xzf /backup/storage_YYYY-MM-DD.tgz"

# 4) 重启
sudo docker compose -f deploy/docker-compose.prod.yml up -d
```

> 50GB 系统盘空间有限：备份文件定期下载到本地/OSS 后删除服务器侧副本；建议同时启用腾讯云**快照策略**（整盘快照，含卷数据）作为兜底。

---

## 10. 常见问题排查

| # | 现象 | 原因与处理 |
| --- | ---- | ---- |
| 1 | 构建期 `Killed` / OOM | 未按 §1.3 配 swap；确认 `free -h` 有 2G swap。前端构建已内置 `NODE_OPTIONS=--max-old-space-size=1536`，仍 OOM 则先 `docker builder prune` 后重试 |
| 2 | `port is already allocated` | 宿主 8006/8007 被占用：改 `.env` 的 `WEB_PORT`/`ADMIN_PORT` 后 `up -d --force-recreate`，并同步调整安全组 |
| 3 | 前端可打开但接口 502 | backend 未健康：`logs backend` 查看（最常见为 `.env` 密码与连接串不一致、postgres 未 healthy） |
| 4 | SSE 进度/通知不实时 | 确认从 8006/8007 官方入口访问（生产站点配置已全局 `proxy_buffering off` + 600s 超时）；经其他未关缓冲的代理（如 frp/宝塔反代）会攒包 |
| 5 | 上传报 413 | 上传上限已设 200m（`client_max_body_size`）；更大文件需修改 `*.prod.conf` 后 `up -d --force-recreate`（重建前端容器即可，配置为挂载文件） |
| 6 | 生成产物图片/视频 404 | `/storage` 反代 + 命名卷 `mf_prod_storage` 是否在位：`$DC ps` 看 backend 是否 healthy、`$DC exec backend ls /app/backend/storage` |
| 7 | 数据库连接认证失败 | 密码含特殊字符破坏连接串：使用 `openssl rand -hex` 生成的 hex 密码 |
| 8 | 改了 `.env` 不生效 | `$DC up -d --force-recreate` 重建容器（restart 不会重新插值） |
| 9 | 构建报找不到 `@mangaflow/design` | 必须在仓库根以 compose 构建（context=仓库根）；勿单独进入前端目录 `docker build` |
| 10 | 登录报 401/账号不存在 | 首次启动种子未完成：看 backend 日志 `种子用户` 行；或数据库曾 `down -v` 清空，重启 backend 重新种子 |

---

## 11. 安全加固清单与后续演进

**本方案已内置**：

- 对外仅暴露 8006/8007 两个前端端口；postgres / backend 无宿主映射，数据库网络 `internal` 禁出公网；
- `POSTGRES_PASSWORD` / `JWT_SECRET_KEY` / `AES_KEY` 必填校验，杜绝不安全默认值上线；
- 后端容器非 root 运行（entrypoint 修复卷属主 → gosu 降权），uvicorn / ffmpeg 均非特权执行；
- 全部容器内存/CPU 限额 + 日志轮转，防单容器拖垮 2GB 主机、防日志写满磁盘；
- `.dockerignore` 排除 `.env` 与密钥文件，镜像内不含敏感信息。

**上线后必做**：

1. **修改种子账号密码**：`admin/admin123`、`demo/demo123456` 仅为验收冒烟账号，通过产品内改密功能或直接更新数据库后废弃明文；
2. `.env` 权限保持 600，不要提交仓库；密钥泄露时按 §4.2 重新生成并重启。

**后续演进建议**：

| 事项 | 说明 |
| ---- | ---- |
| HTTPS | 当前为 HTTP 明文（登录令牌有被嗅探风险）。申请域名备案后，可在两个前端容器前加一层 nginx/宝塔反代或直接复用本镜像配置证书（`server_name` 换域名 + 443） |
| 带宽优化 | 4Mbps 出口下大图/视频加载偏慢：前端已 gzip + 7 天缓存；体验不足时媒体走 COS + CDN，或升级带宽 |
| CORS 收紧 | 当前 `CORS_ORIGINS=["*"]`；前后端同源反代下无实际跨域面，未来拆分域名部署时改为具体域名数组 |
| 业务开关 | 正式运营将 `PUNISHMENT_ENABLED=true` 启用违规分级处罚拦截 |
| 备份异地化 | §9 备份定期拉回本地或上传 COS；开启云盘快照策略 |
| 监控 | backend 已暴露 Prometheus 指标（`/api/metrics`），可接云监控/Prometheus 抓取 |

---

## 附录 A：与 V1.0 部署方案（网关拓扑）的差异对照

| # | V1.0（`docker-compose.yml`，开发/演示保留） | V2.0（`docker-compose.prod.yml`，腾讯云生产） |
| ---- | ---- | ---- |
| 1 | 网关 nginx 统一入口 18080，`/` → 用户端、`/admin/` → 运营端 | 去网关；用户端 **8006**、运营端 **8007** 独立端口直连 |
| 2 | 管理端构建 `--base=/admin/`（网关子路径） | 生产构建 `ADMIN_BASE=/`（`admin.Dockerfile` 新增 `ADMIN_BASE` 参数，默认值兼容 V1.0） |
| 3 | `/api`、`/storage` 由网关反代 | 由两个前端容器内置 nginx 反代（`*.prod.conf` 运行时挂载） |
| 4 | postgres 15432 / backend 18300 / 前端 15173、15174 调试映射 | **全部移除**，仅暴露 8006/8007 |
| 5 | 敏感变量带开发默认值（`123456` 等） | 必填语法 `${VAR:?}`，未配置拒绝启动 |
| 6 | 无资源限额、无日志轮转 | 内存/CPU 限额（合计 1.54G）+ 日志 10m×3 轮转 |
| 7 | 单一 bridge 网络 | `mf-app` 业务网 + `mf-internal`（internal）数据网隔离 |
| 8 | 后端以 root 运行 | 后端非 root 运行（`backend-entrypoint.sh` + gosu） |
| 9 | postgres 无低内存调优 | `shared_buffers=128MB`、`max_connections=50` 等 2GB 适配参数 |
| 10 | compose 项目名 `mangaflow` | 项目名 `mangaflow-prod`，容器/卷/网络全部隔离，两套编排可并存不冲突 |

## 附录 B：本方案部署文件清单（deploy/）

| 文件 | 状态 | 说明 |
| ---- | ---- | ---- |
| `docker-compose.prod.yml` | 新增 | 生产编排：4 服务、双网络、资源限额、必填密钥校验 |
| `web-nginx.prod.conf` | 新增 | 用户端生产站点：SPA + `/api`、`/storage` 反代 + SSE + gzip + 200m 上传 |
| `admin-nginx.prod.conf` | 新增 | 运营端生产站点（根路径形态）：同上 |
| `backend-entrypoint.sh` | 新增 | 后端入口：存储卷属主修复 → gosu 降权 |
| `backend.Dockerfile` | 修改 | +gosu/appuser 非 root 基线、ENTRYPOINT |
| `admin.Dockerfile` | 修改 | +`ADMIN_BASE` 构建参数（默认 `/admin/` 不变）、+构建堆上限 |
| `web.Dockerfile` | 修改 | +构建堆上限 `NODE_OPTIONS` |
| `.env.example` | 修改 | +生产端口（8006/8007）、模型渠道 Key 段 |
| `.dockerignore`（仓库根） | 修改 | +`backend-entrypoint.sh` 白名单 |
| `docker-compose.yml` / `nginx.conf` / `web-nginx.conf` / `admin-nginx.conf` | 未改动 | V1.0 开发/演示拓扑继续可用 |

**本地已完成的校验**：两份编排均通过 `docker compose config` 校验（v5.2.0）；生产编排渲染复核了端口映射（8006/8007）、资源限额（896M/512M/64M/64M）、双网络与健康检查定义；新增 shell/conf 文件均为 LF 无 BOM。Docker 镜像的实际构建与容器启动验证需在目标服务器（或启动本机 Docker daemon 后）按 §6 执行。
