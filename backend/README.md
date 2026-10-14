# MangaFlow 后端（backend）

## 项目简介

MangaFlow（漫序）是 AI 漫剧一站式生产平台，后端基于 FastAPI 提供 REST + SSE 接口，覆盖
「注册登录 → 项目管理 → 剧本 AI 生成 → 实体抽取与资产定妆 → 分镜拆解 → 关键帧/视频/配音生成 →
成片合成 → 三级闸门机审 → 发布分享」的完整创作链路，并配套智能体工作台（agent）、AI 画布（canvases）、
积分计费（billing）、管理后台（admin）等模块。

## 技术栈

- Web 框架：FastAPI + Uvicorn（REST / SSE 流式）
- 数据库：PostgreSQL 16 + SQLAlchemy 2.x（异步，asyncpg 驱动）
- 数据库迁移：Alembic（异步 env.py，连接串经 settings 动态注入）
- 数据校验：Pydantic v2 + pydantic-settings
- 认证：JWT（access / refresh 双令牌），渠道 Key 使用 Fernet/AES 加密落库
- LLM：DeepSeek（OpenAI 兼容协议，流式 SSE）
- 任务：内置异步任务执行器（mock 图像/视频/TTS provider 即时完成）
- 测试：pytest + pytest-asyncio

## 目录结构

```
backend/
├── alembic/           # Alembic 迁移环境（env.py 异步模板 + versions/ 版本脚本）
├── alembic.ini        # Alembic 配置（sqlalchemy.url 仅占位，运行时动态注入）
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

## 数据库迁移（Alembic）

表结构变更的**唯一事实来源**是 Alembic 迁移（版本目录 `alembic/versions/`），初始基线为
`2026_10_10_2012-e2b3b5b5c90a_initial_schema_32_tables.py`（32 张业务表全量建表）。

- 连接串优先级：环境变量 `ALEMBIC_DATABASE_URL` > `settings.database_url`
  （alembic.ini 的 `sqlalchemy.url` 仅占位留空；`env.py` 为 asyncpg 异步模板）。
- 排除范围：langgraph checkpoint 运行时自管表（`checkpoints` / `checkpoint_blobs` /
  `checkpoint_writes` / `checkpoint_migrations`，由 `AsyncPostgresSaver.setup()` 在应用
  启动时自动建表），不纳入迁移对比与生成，避免 autogenerate 产生 drop_table 建议。

常用命令（cwd=backend，解释器用 `.venv\Scripts\alembic.exe`）：

```powershell
# 1. ORM 模型变更后生成迁移（生成后必须人工逐段校验：
#    禁止 drop_table / 改列类型等破坏性 op；确认无害的噪音 diff 手动剔除）
.venv\Scripts\alembic.exe revision --autogenerate -m "描述"

# 2. 升级到最新（幂等：已在 head 时为 no-op）
.venv\Scripts\alembic.exe upgrade head

# 3. 回滚（-1 上一版 / base 全部回滚）
.venv\Scripts\alembic.exe downgrade -1

# 4. 同步性检查：ORM 与目标库 diff 应输出 "No new upgrade operations detected."
.venv\Scripts\alembic.exe check

# 5. 查看当前版本 / 历史
.venv\Scripts\alembic.exe current
.venv\Scripts\alembic.exe history
```

与 `create_all` 的关系：

- `init_db.py` 的 `Base.metadata.create_all()` + 幂等补列仍是**开发环境兜底**
  （`python -m app.core.init_db` / lifespan 自动执行，保证空库可跑、种子可灌）。
- **结构变更走迁移**：新任务涉及表结构时，先改 ORM 模型 → `alembic revision
  --autogenerate` → `alembic upgrade head`，不要手工改库、不要只改 ORM。
- 存量库（本机 mangaflow 主库）已 `alembic stamp head` 打基线（结构已存在，
  不重放建表）；全新环境从空库直接 `alembic upgrade head` 即得完整 schema。

部署建议（`deploy/docker-compose.yml`，backend 以 uvicorn 启动）：

- **方式一（生产推荐）**：backend 启动前显式执行迁移——在部署流水线/容器 entrypoint
  先跑 `alembic upgrade head`（或增加一次性 one-shot 服务），迁移失败在启动前置暴露。
- **方式二（小部署）**：环境变量设 `ALEMBIC_ON_STARTUP=true`，应用 lifespan 会在
  `create_all` 兜底初始化**之前**自动执行 `alembic upgrade head`（程序化 API，经
  工作线程驱动异步迁移）；迁移失败不阻断启动，记 error 日志后降级为 create_all 兜底。
- 默认 `ALEMBIC_ON_STARTUP=false` 保持现状（仅 create_all + 幂等补列）。

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

本轮（2026-10-12 创作链路缺口补全）新增接口的**业务口径文档**（幂等键、降级链、
占位标记、错误码表、性能与索引评估）见 `backend/docs/api-creation-chain.md`。

## 模型渠道配置（5 渠道）

渠道常量定义在 `app/core/config.py`（带开发默认值），生产用 `backend/.env` 覆盖（环境变量名不区分大小写）：

| 渠道 | 能力 | 配置项（BASE_URL / API_KEY / MODEL） | 默认值 |
| --- | --- | --- | --- |
| DeepSeek（主） | 文本 LLM | `DEEPSEEK_*` | `https://api.deepseek.com` / `deepseek-flash` |
| Agnes（兜底） | 文本 LLM | `AGNES_LLM_*` | `https://apihub.agnes-ai.com/v1` / `agnes-3.0-flash` |
| Agnes 图像 | 图像 | `AGNES_IMAGE_*` | 同 Agnes 地址 / `agnes-image-2.5-flash` |
| Agnes 视频 | 视频 | `AGNES_VIDEO_*` | 同 Agnes 地址 / `agnes-video-2.5-flash`（轮询：`VIDEO_POLL_*_SECONDS`） |
| 百炼 TTS | 语音 | `BAILIAN_TTS_*` | 阿里云百炼专属实例 / `qwen-audio-3.1-tts-flash`（仅提供 dashscope WebSocket 端点，走 `dashscope.audio.tts_v2.SpeechSynthesizer`；音色需用该模型专属 `_v3.1` 后缀音色，如 `longanhuan_v3.1`，qwen3-tts 系音色会报 Engine error 411，2026-10-12 实测） |
| 百炼视频 | 视频 | `BAILIAN_WAN_*` | 阿里云百炼专属实例（根域名，DashScope 原生异步协议）/ `wan2.7-i2v` 图生视频（请求体 `input.media=[{type:first_frame,url}]`，url 支持 Base64 data URI——本地关键帧直接传入；轮询 GET 不可携带 X-DashScope-Async 头否则 403，2026-10-12 实测出片；Key 与 TTS 共用 `BAILIAN_TTS_API_KEY`） |

连通性探测脚本：根目录 `.run/llm_image_video_tts_probe.py`（结果见 `.run/probe_result.md`）。

## 渠道降级链与 mock 链尾

- 图像/视频/TTS 任务执行时经 `services/model_router.resolve_channels_chain` 按
  `logical_models.channel_priority` 顺序解析平台渠道（enabled 且非 down），链尾固定追加
  本地 mock 哨兵渠道（channel_id=0），真实渠道全部失败时兜底出占位资源，业务不中断。
- 每次尝试回写渠道健康状态，失败原因累积进任务 `meta.fallback_chain`，实际渠道记
  `meta.provider`（agnes/bailian/mock），供任务详情与 DB 溯源。
- 文本 LLM 走 `providers/llm.py` 内置降级链：DeepSeek 主渠道失败自动切 Agnes 兜底。

## 内容机审：图像/视频真实视觉机审（Task 7，合规红线 R-08/C-01）

生成产物（图像/视频）在既有「提示词词表预检 + LLM 文本机审」之上，新增
**真实视觉机审**：对画面实际视觉内容送视觉理解模型判定（区别于纯文本机审），
判定口径与词表分级一致（严禁→violate / 敏感擦边→suspect / 正常→pass）。

- **视觉模型**：`AGNES_VISION_MODEL`（默认 `agnes-3.0-flash-max`，OpenAI 兼容
  chat/completions 图文混合协议）。经探测（2026-10-10，证据见
  `.run/probe_result.md`「视觉机审模型探测」一节）：Agnes 网关模型列表无显式
  视觉命名模型，但文本旗舰 agnes-2.5-pro-alpha / agnes-3.0-flash-max /
  agnes-2.5-pro 实测均接受图文输入并返回准确图片描述（image_tokens 证明图片
  真实入模），即旗舰文本模型即多模态视觉模型。
- **降级链**：视觉机审（method=`vision`）→ LLM 纯文本机审（method=`llm-text`）
  → 纯词表（method=`keyword`）。视觉调用失败（网络/超时 15s/无模型/输出非法）
  自动降级，任务不阻塞；审核方式以 review_records.method 为准。
- **图像**：生成成功后产物可访问（本地 storage 文件读 base64 / 公网 URL）即送
  审视觉模型（`providers/vision_review.review_image`）。
- **视频**：生成成功后用 ffmpeg 抽取产物首帧（`-frames:v 1`，无 ffmpeg 跳过）
  送视觉机审；抽帧失败降级文本机审。
- **流转**：violate → 任务 failed（content_violation）+ 按用户退款 + 分级处罚；
  suspect → 写审核记录待人工复核；pass → 写通过记录。全部落
  review_records（gate=generate）。违规任务产物 URL 清空，不回写分镜/资产。
- 测试：`tests/test_vision_review.py`（判定解析容错/降级链/图像与视频端到端拦截）。

## 业务开关：违规处罚限流（上线后打开）

- 配置项 `PUNISHMENT_ENABLED`（`app/core/config.py`，默认 `false`）：控制 C-04 分级处罚的
  生成准入拦截（banned → 403 拒绝、restricted → 当日 10 次 → 429 限流）。
- **当前默认关闭 = 生成限制已取消**（上线前策略）：违规处罚的累计（violation_count）与
  站内通知照常生效，仅不拦截提交。
- 产品成功上线后打开：`backend/.env` 写入 `PUNISHMENT_ENABLED=true`（或环境变量同名覆盖），
  重启后端即恢复拦截，无需改代码。

## 健康探测

- `model_channels.health_status`（unknown/healthy/degraded/down）：业务调用成功置
  healthy、失败置 degraded（`model_router.record_result`）；管理端可主动探测
  （`model_router.probe_channel`：llm 发 1 token，image/video 以极小主体试探，401/403 判 down）。
- **成功自动复位**（Task 8 收敛点）：degraded 渠道一次业务调用成功即自动复位
  healthy（`record_result(ok=True)`），无需人工干预或等待探测任务；复位与降级
  均同步刷新 `last_check_at`。401/403 → down 仅出现在探测口径（`probe_channel`），
  业务回写保持 healthy/degraded 二分。
- 呈现一致性：用户端 `GET /api/models/health-summary` 与管理端
  `GET /api/admin/models/channels` 同源读取 `model_channels.health_status`，
  两端视图天然一致。若库中健康值与实际不符（如长期 degraded 但探测 PASS），
  管理端调 `POST /api/admin/models/channels/{id}/test` 主动探测即可按实测回写，
  或经 `PUT /api/admin/models/channels/{id}` 直接修正 `health_status`。
- 健康状态为 down 的渠道在路由解析时自动跳过。

## 向量检索：pgvector 双模（Task 6）

资产语义搜索（`/api/assets/search`）与用户记忆检索（`/api/memories/search`）
为「pgvector SQL 余弦 / Python 余弦」双模，运行时自动分流，返回结构完全一致
（前端无感知）。支撑模块：`app/services/vector_store.py`。

- **启用条件与探测**：应用启动（lifespan，create_all 之后、worker 启动之前）
  执行 `probe_pgvector`：尝试 `CREATE EXTENSION IF NOT EXISTS vector` 并确认
  pg_extension 注册，结果进程内缓存。
- **生产（自动启用）**：`deploy/docker-compose.yml` 使用 `pgvector/pgvector:pg16`
  镜像，探测成功后幂等 ensure 影子列 `asset_embeddings.embedding_vec` 与
  `langgraph_store.embedding_vec`（类型 `vector(dim)`）、从 JSONB 回填存量向量、
  建 ivfflat 索引（`vector_cosine_ops`，lists=100）；检索走 `1 - (embedding_vec
  <=> :qv)`（ivfflat 为距离语义）的索引 TopK。
- **开发机（自动降级）**：本机 PostgreSQL 无 pgvector 扩展（`pg_available_extensions`
  无 vector），探测失败即跳过建列，检索走既有「JSONB 全量扫 + Python 余弦」
  路径，行为与历史版本一致。
- **维度决策**：向量维度不固定（本地 hash-bag 降级恒 256 维；远程
  text-embedding-v4 由模型决定），影子列维度取 ensure 时探测到的「现有 JSONB
  向量最长维度与 256 的较大者」；维度不匹配的向量不写影子列（置 NULL），
  查询向量维度与影子列不一致时自动回退 Python 路径。模型维度整体切换后
  pgvector 路径自动失效，重启 ensure 时按新维度重建影子列并回填。
- **双写策略（迁移期安全）**：JSONB 为主存储始终写入；pgvector 可用时经
  SAVEPOINT（`begin_nested`）同步双写影子列，影子列失败仅告警、绝不影响
  JSONB 主事务。检索 SQL 路径运行期失败（如列被误删）会自愈降级：关闭本进程
  pgvector 开关并回退 Python 路径，重启后重新探测恢复。
- **迁移策略**：影子列与索引用运行时 ensure 而非 Alembic——扩展可能不存在，
  Alembic 迁移无法表达条件 DDL；ensure 幂等，重复启动无副作用。
- **测试取舍**：真实 pgvector 路径需真扩展环境（SQL 真跑，无法用 mock 替代），
  开发机无扩展，测试以降级路径（排序/结构断言）+「探测可用但影子列缺失」的
  运行期自愈回退路径覆盖（`tests/test_vector_dual.py`）。

## LangSmith 可选接入

智能体（LangGraph/LangChain）链路追踪（MA-13，**接线完成，待密钥启用**）：

- **配置字段**（`app/core/config.py`，环境变量名不区分大小写，亦写入 `backend/.env`）：

  | 字段 | 默认值 | 说明 |
  | --- | --- | --- |
  | `LANGSMITH_TRACING` | `false` | 追踪开关，`true` 启用 |
  | `LANGSMITH_API_KEY` | 空 | LangSmith API Key（`lsv2_` 前缀） |
  | `LANGSMITH_PROJECT` | `mangaflow` | trace 上报的目标项目名 |

- **接线方式**：应用启动时 `lifespan` 开头调用 `app/core/tracing.py` 的
  `setup_tracing()`——未启用时零开销直接返回；启用后注入 LangChain 标准追踪
  环境变量（`LANGSMITH_TRACING` / `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT`，
  并同步旧名 `LANGCHAIN_TRACING_V2` / `LANGCHAIN_API_KEY` 兼容），此后全部
  图节点执行、LLM 调用与子图运行自动产出 trace。不引入新依赖
  （langchain-core >= 0.3 原生识别该组环境变量）。
- **`.env` 示例**：

  ```dotenv
  LANGSMITH_TRACING=true
  LANGSMITH_API_KEY=lsv2_pt_xxxxxxxxxxxxxxxx
  LANGSMITH_PROJECT=mangaflow
  ```

- **验证方式**：配置真实 Key 重启后端，触发一次智能体链路（如剧本生成），
  登录 LangSmith 控制台对应项目即可看到按 thread_id 检索的完整 trace；
  未配置 Key 时仅注入开关、上报被 SDK 拒绝，无副作用。
- **现状**：当前无真实 LangSmith Key，链路已就绪待密钥启用；测试见
  `tests/test_tracing.py`（未配置零注入 / 配置后注入与幂等）。

## 多智能体会话与 Time Travel（MA-06）

- 会话域接口（`api/agent_chat.py`）：`GET /api/agent/threads/{id}/history` 返回
  LangGraph 检查点历史（分页）；`POST /api/agent/threads/{id}/rewind` 把
  `agent_threads.rewind_checkpoint` 标记为回溯点，**下次对话**从该检查点分叉
  续聊（一次性语义：分叉续聊完成后自动清空标记）。
- 复核结论：`tests/test_agent_hitl.py` 4 passed（history 分页、rewind 标记、
  分叉续聊、一次性清标记均已覆盖），报告 §4.9「待确认」口径更新为**已实现**。

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
- 重启恢复：应用启动时（lifespan，先于 worker 拉起）把遗留的 `status=running`
  在途任务重置回 `queued`（进度保留，`meta.recover_at` 留痕，不计入 retry_count），
  由 worker 重新领取，避免进程重启产生僵尸任务。

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

## 性能基线与索引评估（2026-10-12 核查）

> 详细评估过程见 `backend/docs/api-creation-chain.md` §11（主链路 API 文档附录）。

**新列查询模式结论**：

- `episodes.project_id` 过滤：`idx_episodes_project` 索引已确认存在（另有
  `uk_episodes_seq(project_id, sequence)` 唯一索引同时覆盖排序），索引完备；
- 资产 `?tag=` JSONB `@>` 过滤：**当前规模不需要 GIN 索引**（单项目资产万级以内，
  project_id 收敛后顺序扫描行数极少）；单表资产行数超 **5 万** 或 tag 过滤 P95
  超 100ms 时建议为四类资产 `tags` 列追加 GIN（`jsonb_path_ops`）索引，DDL 示例见
  api 文档 §11.1（当前不执行）；
- `reference_image_url` / `canvas_nodes.meta`：仅单点读写，不作为过滤条件，不需要索引。

**列表接口分页核查**：episodes / storyboards 列表不分页（数量天然有限，合理）；
资产 / 任务 / 成片 / 审核 / 画布列表沿用既有分页（`normalize_pagination` 钳位
page_size ∈ [1,100]）。

**响应时间基线（本地 dev 口径，非生产压测结论）**：

- 测量方式：`GET /api/metrics` 既有 HTTP 时延直方图
  `mangaflow_http_request_duration_seconds`（分桶 0.05/0.1/0.5/1.0/3.0s），
  或 pytest 用例内 `time.perf_counter()` 内联计时；
- 量级参考（本地单机、mock provider 即时完成）：列表类接口 <50ms、任务创建类
  （含积分冻结与文件落盘）<100ms、幂等同步类 <100ms、导出类 100~500ms；
  compose/decompose 为异步任务，API 创建 <100ms，执行耗时取决于 provider；
- pytest 全量套件单用例均值约 30s 属 fixture 环境（建库/种子）开销，
  **不代表接口响应时间**；生产口径需以 `/api/metrics` 直方图在真实流量下重新标定。

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
