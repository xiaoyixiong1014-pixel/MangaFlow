# MangaFlow 后端（backend）

## 项目简介

MangaFlow（漫序）是 AI 漫剧一站式生产平台，后端基于 FastAPI 提供 REST + SSE 接口，覆盖
「注册登录 → 项目管理 → 剧本 AI 生成 → 实体抽取与资产定妆 → 分镜拆解 → 关键帧/视频/配音生成 →
成片合成 → 三级闸门机审 → 发布分享」的完整创作链路，并配套智能体工作台（agent）、AI 画布（canvases）、
积分计费（billing）、管理后台（admin）等模块。

## 技术栈

- Web 框架：FastAPI + Uvicorn（REST / SSE 流式）
- 数据库：PostgreSQL 16 + SQLAlchemy 2.x（异步，asyncpg 驱动）
- 数据校验：Pydantic v2 + pydantic-settings
- 认证：JWT（access / refresh 双令牌），渠道 Key 使用 Fernet/AES 加密落库
- LLM：DeepSeek（OpenAI 兼容协议，流式 SSE）
- 任务：内置异步任务执行器（mock 图像/视频/TTS provider 即时完成）
- 测试：pytest + pytest-asyncio

## 目录结构

```
backend/
├── app/
│   ├── agents/        # 智能体（supervisor / review / intake 等）
│   ├── api/           # 路由层（auth/users/projects/billing/prompts/media/models/
│   │                  #   announcements/script/assets/storyboards/generation/publish/
│   │                  #   canvases/agent_chat + admin 子包）
│   ├── core/          # 配置（config.py）/ 安全（security, crypto）/ SSE / DB 会话
│   ├── models/        # SQLAlchemy ORM 模型
│   ├── providers/     # 模型提供方（llm.py DeepSeek；image/video/tts 为 mock）
│   ├── schemas/       # Pydantic 请求/响应模型
│   ├── services/      # 业务服务层（剧本/资产/分镜/生成/合成/审核/计费/画布）
│   ├── tasks/         # 生成任务执行器（runner）
│   └── main.py        # FastAPI 应用入口（路由注册 / storage 静态挂载）
├── storage/           # 本地文件存储（上传素材、生成产物、备份）
├── tests/             # pytest 回归测试（121 例）
└── .venv/             # Python 虚拟环境
```

## 环境要求

- Python 3.13（开发环境为 3.13.14）
- Node.js 24（仅前端需要；后端启动不依赖）
- PostgreSQL 16（Windows 服务名 `postgresql-x64-16`）
- DeepSeek 配置：在 `app/core/config.py` 中有开发默认值，生产环境建议用 `backend/.env`
  覆盖（环境变量名不区分大小写）：
  - `DEEPSEEK_BASE_URL`：默认 `https://api.deepseek.com`
  - `DEEPSEEK_API_KEY`：默认值已脱敏存储（`sk-4611****42eb`），请替换为自己的 Key
  - `DEEPSEEK_MODEL`：默认 `deepseek-flash`
  - 其他：`DATABASE_URL`（默认 `postgresql+asyncpg://postgres:123456@localhost:5432/mangaflow`）、
    `JWT_SECRET_KEY`（生产务必更换）、`AES_KEY`（渠道 Key 加密）

## 快速开始

方式一（推荐）：项目根目录一键脚本

```powershell
.\mangaflow.ps1 start    # 启动 PostgreSQL 检查 + init_db + backend/web/admin
.\mangaflow.ps1 status   # 查看各服务状态
.\mangaflow.ps1 stop     # 停止三服务（保留 PostgreSQL）
```

方式二：手动启动

```powershell
# 1. 初始化数据库（幂等：建库 + 建表 + 种子数据）
cd backend
.venv\Scripts\python.exe -m app.core.init_db

# 2. 启动后端（端口 8300，见下方端口说明）
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8300

# 3. 启动前端（另开终端，项目根目录）
cd mangaflow-web;       npm run dev   # http://localhost:5173
cd mangaflow-admin-web; npm run dev   # http://localhost:5174
```

启动后：

- API 文档：http://127.0.0.1:8300/docs
- 内置账号：管理员 `admin / admin123`，演示用户 `demo / demo123`

## 端口说明

后端固定使用 **8300**（非默认 8000）：本机 8000 端口被另一个项目（AI_Cook）长期占用，
MangaFlow 全链路（uvicorn、mangaflow-web / mangaflow-admin-web 的 vite proxy、一键脚本）
统一指向 8300。前端 5173（用户端）、5174（管理端）。

## API 概览

| 模块 | 主要端点 | 用途 |
| --- | --- | --- |
| auth `/api/auth` | POST register / login / refresh；GET me | 注册登录、双令牌刷新、当前用户与钱包 |
| users `/api/users` | 资料 / 密码 / 钱包明细 | 个人中心 |
| projects `/api/projects` | CRUD、duplicate、snapshots | 项目管理与快照 |
| script `/api/projects/{id}/script` | GET/PUT script、PUT input、POST generate/stream（SSE）、health、extract(+confirm) | 剧本输入、DeepSeek 流式生成、体检、实体抽取确认 |
| assets `/api/projects/{id}/assets` | characters/scenes/props/styles CRUD、generate、lock/unlock、variants、consistency-check；GET /api/characters | 四类资产与角色定妆 |
| storyboards `/api/projects/{id}/storyboards` | decompose、reorder、warnings、deliver-to-canvas、CRUD、split、merge、regen | 分镜拆解与编排 |
| generation | POST storyboards/{sid}/keyframes\|video\|tts；GET tasks(+stream)、POST tasks/{id}/retry\|cancel | 关键帧/视频/配音任务与进度流 |
| publish | POST compose、GET outputs、set-current、reviews、publish、share/revoke；GET /api/share/{token} | 成片合成与发布分享（分享页免认证） |
| canvases `/api/canvases` | CRUD、graph、import/export、nodes/{id}/execute、execute、stream、assistant/chat | AI 画布编排与执行 |
| agent `/api/agent` | POST chat/stream（SSE）、threads、messages、intake | 智能体工作台 |
| billing `/api/billing` | wallet、summary、transactions、packages、orders(+pay) | 积分钱包与充值订单 |
| models `/api/models` | catalog、channels、channels/test | 用户自有模型渠道 |
| media `/api/media` | upload、GET/{id}、DELETE、meta | 素材上传管理 |
| announcements `/api/announcements` | GET 列表/详情 | 用户端公告 |
| admin `/api/admin/*` | dashboard、users(+points/status)、orders、content、models、review、settings | 管理后台（角色鉴权） |

完整端点与参数见 Swagger：http://127.0.0.1:8300/docs

## 模型渠道配置（5 渠道）

渠道常量定义在 `app/core/config.py`（带开发默认值），生产用 `backend/.env` 覆盖（环境变量名不区分大小写）：

| 渠道 | 能力 | 配置项（BASE_URL / API_KEY / MODEL） | 默认值 |
| --- | --- | --- | --- |
| DeepSeek（主） | 文本 LLM | `DEEPSEEK_*` | `https://api.deepseek.com` / `deepseek-flash` |
| Agnes（兜底） | 文本 LLM | `AGNES_LLM_*` | `https://apihub.agnes-ai.com/v1` / `agnes-3.0-flash` |
| Agnes 图像 | 图像 | `AGNES_IMAGE_*` | 同 Agnes 地址 / `agnes-image-2.5-flash` |
| Agnes 视频 | 视频 | `AGNES_VIDEO_*` | 同 Agnes 地址 / `agnes-video-2.5-flash`（轮询：`VIDEO_POLL_*_SECONDS`） |
| 百炼 TTS | 语音 | `BAILIAN_TTS_*` | 阿里云百炼 OpenAI 兼容地址 / `qwen-audio-3.1-tts-flash` |

连通性探测脚本：根目录 `.run/llm_image_video_tts_probe.py`（结果见 `.run/probe_result.md`）。

## 渠道降级链与 mock 链尾

- 图像/视频/TTS 任务执行时经 `services/model_router.resolve_channels_chain` 按
  `logical_models.channel_priority` 顺序解析平台渠道（enabled 且非 down），链尾固定追加
  本地 mock 哨兵渠道（channel_id=0），真实渠道全部失败时兜底出占位资源，业务不中断。
- 每次尝试回写渠道健康状态，失败原因累积进任务 `meta.fallback_chain`，实际渠道记
  `meta.provider`（agnes/bailian/mock），供任务详情与 DB 溯源。
- 文本 LLM 走 `providers/llm.py` 内置降级链：DeepSeek 主渠道失败自动切 Agnes 兜底。

## 健康探测

- `model_channels.health_status`（unknown/healthy/degraded/down）：业务调用成功置
  healthy、失败置 degraded（`model_router.record_result`）；管理端可主动探测
  （`model_router.probe_channel`：llm 发 1 token，image/video 以极小主体试探，401/403 判 down）。
- 健康状态为 down 的渠道在路由解析时自动跳过。

## LangSmith 可选接入

智能体（LangGraph/LangChain）链路追踪：配置环境变量 `LANGSMITH_TRACING=true`、
`LANGSMITH_API_KEY`、`LANGSMITH_PROJECT`（项目名）即可上报；不配置则完全离线，
不影响任何功能。

## MCP 桥配置

画布 MCP 工具节点读取站点设置 KV（key=`mcp_servers`，value 为
`{"value": [{id, name, url, ...}, ...]}`）：管理端「系统设置」维护服务器清单，
画布按 `server_id` 引用（`api/mcp.py` / `services/canvas_service.py`）。

## 插件体系

- `plugins` 表 + `endpoint_url`：`builtin:` 前缀为进程内实现（注册表
  `services/plugin_service.py`，内置 `builtin:text-enhance` 文本增强）；其余为 HTTP endpoint。
- 新插件需经管理端审查通过并启用后才可被画布调用；种子数据内置一个已审核插件。
- 直调端点：`POST /api/plugins/builtin/text-enhance`（登录用户）。

## 死信队列与指数退避

- 平台故障自动重试最多 3 次，第 n 次失败后 `next_retry_at = now + 2^n 分钟`
  （2/4/8 分钟），未到期任务不被 worker 领取（`tasks/runner.py` 调度 SQL）。
- 重试耗尽置 `dead_letter=True` 进入管理端死信队列
  （`GET /api/admin/tasks/dead-letters`），运营复核后 `POST /{task_id}/requeue` 重新冻结积分入队。

## 通知中心（SSE）

任务成功/失败等终态推送站内通知：`GET /api/notifications`（分页）、
`/unread-count`（未读数）、`POST /read`（标记已读）、`GET /stream`（SSE 实时推送，
`message` 事件 + 30s 心跳）。推送失败不影响任务主流程。

## Prometheus 指标

`GET /api/metrics`（免鉴权）：HTTP 请求计数/时延、任务状态迁移计数、进行中任务
Gauge、渠道健康 Gauge 等聚合指标，供 Prometheus 抓取。

## 测试说明

```powershell
cd backend
.venv\Scripts\python.exe -m pytest tests/ -q    # 121 例全量回归
```

## 已知演示性实现说明

以下模块为演示/占位实现，生产环境需替换：

- **图像 / 视频 / TTS provider（`app/providers/image_mock.py`、`video_mock.py`、`tts_mock.py`）**：
  不调用真实模型，直接生成占位资源（SVG 等）并即时置任务 done；TTS 时长由台词长度模拟。
- **模拟支付（`app/api/billing.py` POST /orders/{order_no}/pay）**：订单直接支付成功并到账积分，
  未对接微信/支付宝等真实支付渠道。
- **规则机审词表（`app/services/review_service.py` 与 `app/agents/review_agent.py` 的
  `BANNED_WORDS`）**：三级闸门（generate/compose/publish）机审为简单敏感词命中
  （当前词表：枪击、血腥、赌博），命中即判违规；未命中直接放行。
- **合成水印（`app/services/compose_service.py`）**：隐形水印仅作为 `watermark_id` 标识写入，
  不做像素级渲染；成片地址为占位资源。
- **备份（`app/api/admin/settings.py`）**：管理端备份写入本地 `storage/backups/`，
  生产应使用对象存储。
