# 架构对齐与高危缺陷收敛 Spec

## Why

《漫序 MangaFlow 项目进度统计分析报告 V1.1》（2026-10-10）指出三类必须收敛的问题：① 3 项高危缺陷（资产页空白+循环请求、生成产物 404、后台积分列恒 0）直接损害数据可信度；② 7 处架构偏离未拍板（队列中间件、pgvector、对象存储、Alembic、部署文件、LangSmith、Deep Agents 口径），其中队列/迁移/部署被列为「上线硬门槛」；③ 渠道健康 degraded 与图像/视频非真实视觉机审（合规红线）。本 spec 逐项解决或拍板，实现「可演示、可信、可上线」。

## What Changes

### A. 高危缺陷修复（P0，报告 §5.2 / §7.2#1 / R-01）
- A1 **C-BUG-1**：资产页空白 + 接口循环请求（`AssetsView` 前端加载副作用失控）——定位根因并修复，页面正常渲染画廊且无重复请求。
- A2 **C-BUG-2**：生成产物 404（任务成功但媒体文件不可访问）——核对 provider 落盘路径、`media.url` 生成规则与 `/storage` 静态挂载一致性，修复后分镜缩略图/产物可正常显示。
- A3 **A-BUG-1**：后台用户列表积分列恒为 0——修复 admin users 接口聚合或前端字段绑定，积分列与钱包实际值一致。

### B. 架构偏离收敛（7 项逐项拍板，报告 §2.1 / R-10）
- B1 **任务队列**：进程内 runner 补齐「重启丢在途任务」短板——应用启动时将 running 状态孤儿任务自动重置回 queued 重入队（含日志与 metrics 观测）；**拍板：修订架构文档**为「当前单实例进程内 runner（优先级/退避/死信齐备），多实例水平扩展时演进 Celery+Redis」，不引入新中间件。
- B2 **pgvector**：实现「双模向量检索」——启动时探测 `CREATE EXTENSION vector`：可用则建 ivfflat 索引并走 SQL 余弦检索；不可用（如当前开发机）自动降级现有 JSONB+Python 余弦，行为不变。docker-compose 采用 `pgvector/pgvector` 镜像使生产开箱即用。
- B3 **对象存储**：**拍板：修订架构文档**为「当前本地磁盘存储（单机部署），OSS/COS 为多实例部署演进项」；确认 `storage_path()` 已是唯一落盘入口（升级点）。
- B4 **Alembic**：引入 Alembic 并生成 32 表初始迁移基线；空库 `upgrade head` 验证可复现；启动流程兼容（`create_all` 保留为开发兜底，`.env` 可切换 `ALEMBIC_ON_STARTUP`）。
- B5 **部署文件**：Dockerfile（backend + 双前端）+ docker-compose.yml（postgres/pgvector + backend + web + admin-web + nginx）+ Nginx 配置落库；本机 Docker 29.6.1 可用，执行构建冒烟验证。
- B6 **LangSmith**：环境变量接线启用（`LANGSMITH_TRACING/API_KEY/PROJECT`），未配置时零开销；配置单测验证回调生效路径；README 使用说明。
- B7 **Deep Agents 口径**：**拍板：修订架构文档**——自研 plan→并行 exec→reflect→finalize 等价实现（不引入 `deepagents` 依赖），文档表述与实现对齐并留痕。
- B8 **架构文档修订**：《架构设计与数据库设计文档 V1.1》§1.3/§5/§6 按上述拍板结论修订（存储/队列/迁移/DeepAgents 四处口径 + 变更留痕段）。

### C. 渠道健康与 TTS（P0，报告 §7.2#5 / R-02）
- C1 全渠道健康巡检与恢复：跑探测脚本复核 5 渠道，核查 `record_result` 健康回写逻辑确保成功调用可自动复位 degraded；DeepSeek 402 欠费与百炼 TTS 实例无 HTTP TTS 端点两项外部依赖问题留证据归档（已有），健康状态与看板呈现一致。

### D. 图像/视频真实视觉机审（P1 合规红线，报告 §7.2#7 / R-08 / C-01）
- D1 探测 Agnes 网关的视觉理解模型（`GET /models` + 小样实测）；实现视觉审核 provider（图像 URL/base64 输入 → pass/suspect/violate JSON）；接入 runner 生成产物审核链（视觉优先，失败降级现有 LLM 文本机审，`method` 字段标注）；视频抽首帧审核；测试覆盖。

### E. Time Travel 确认与版本回退（P1，报告 §7.2#9 / MA-06 / V-01）
- E1 复核 MA-06 Time Travel 链路（history/rewind 已实现）并留存验证证据（报告口径更新依据）。
- E2 V-01 版本快照回退真实化：`version_snapshots` 回退 API（按快照恢复项目数据）+ 最小前端入口 + 测试。

### F. 顺手收敛项（报告 §5.2/§7 中低成本项）
- F1 **ENV-01**：管理端 Vite `server.host` 显式配置（IPv4 可达）。
- F2 **CV-21 核实**：插件 TS SDK 实际已存在（`mangaflow-design/src/plugins/index.ts`，上轮已实现并构建）——报告信息滞后，修订文档口径即可，无需开发。
- F3 **CV-22 轻量加固**：插件 endpoint 增加协议/主机白名单校验（仅 http/https + 管理端可配信任域），调用留痕已有，不做重量级沙箱（文档标注已知限制）。

### 范围外（明确不做，报告亦列 P2/后续）
- R-07 一键发布三方平台、V-03/V-04 协作（本期不做）；中低缺陷 12 项（P2/P3 批量修复窗口）；重量级插件沙箱隔离；真实 OSS 接入（无云凭证）；Celery 完整迁移（按 B1 拍板为演进路径）。

## Impact

- Affected specs: `harden-capabilities-and-real-models`（已完成，本 spec 为其后置收敛，不改其范围）
- Affected code:
  - 前端：`mangaflow-web/src/views/assets/AssetsView.vue`（或其加载链）、`mangaflow-admin-web`（积分列/Vite host）、`mangaflow-web`（版本回退入口）
  - 后端：`main.py`（启动恢复钩子）、`tasks/runner.py`（孤儿任务重置）、`services/review_service.py` + 新 `providers/vision_review.py`（视觉机审）、`services/model_router.py`/`embedding` 相关（pgvector 双模）、`api/admin/users.py`（积分聚合）、`api/versions.py`（回退）、`core/config.py`（开关）
  - 工程：`backend/alembic/`（新增）、`deploy/`（新增 Dockerfile×3 + compose + nginx）、`mangaflow-admin-web/vite.config.ts`
  - 文档：《架构设计与数据库设计文档 V1.1》修订、`backend/README.md`、报告口径修订建议记录

## ADDED Requirements

### Requirement: 高危缺陷归零
三条 UI 测试高危缺陷（C-BUG-1 / C-BUG-2 / A-BUG-1） SHALL 全部修复并有复现-修复-回归证据；资产页正常渲染、生成产物可访问、后台积分列数值正确。

#### Scenario: 资产页加载
- **WHEN** 用户打开项目资产页
- **THEN** 画廊正常渲染，网络面板中资产接口请求次数与用户操作次数一致（无循环请求）

#### Scenario: 生成产物访问
- **WHEN** 图像任务成功完成
- **THEN** 返回的 media.url 通过静态挂载可 GET 到真实文件（HTTP 200，content-type 正确）

#### Scenario: 后台积分列
- **WHEN** 管理员打开用户管理列表
- **THEN** 积分列显示各用户钱包真实余额（与 DB 一致）

### Requirement: 队列在途任务恢复
后端启动时 SHALL 将 status=running 且非当前进程持有的任务重置为 queued（retry 口径），并在日志/metrics 留痕，使「重启丢在途任务」不再发生。

#### Scenario: 重启恢复
- **WHEN** 存在 running 任务时后端进程重启
- **THEN** 启动完成后这些任务回到 queued 并被正常领取执行，不产生僵尸 running 行

### Requirement: pgvector 双模检索
向量检索 SHALL 支持双模：pgvector 扩展可用时建 ivfflat 索引并走 SQL 余弦 TopK；不可用时自动降级现有 JSONB+Python 余弦，接口行为与返回结构不变。

#### Scenario: 开发机降级
- **WHEN** PostgreSQL 未安装 pgvector（当前开发机实测不可用）
- **THEN** 语义检索与一致性校验照常工作（JSONB 路径），启动日志提示降级

### Requirement: Alembic 迁移基线
项目 SHALL 引入 Alembic 并提交 32 表初始迁移；空数据库 `alembic upgrade head` 后结构与 ORM create_all 一致；启动钩子支持可选的自动 upgrade（默认关闭保持开发体验）。

#### Scenario: 空库迁移复现
- **WHEN** 对全新空库执行 `alembic upgrade head`
- **THEN** 32 张表与索引创建成功，后端可正常启动并通过冒烟

### Requirement: 一键部署落库
仓库 SHALL 提供 backend/双前端 Dockerfile、docker-compose.yml（含 pgvector Postgres 镜像、Nginx 网关）与构建说明；本机 Docker 执行构建与容器编排冒烟。

#### Scenario: compose 冒烟
- **WHEN** `docker compose up -d` 后访问 Nginx 网关
- **THEN** 前端页面可打开、后端 `/api/metrics` 可访问（或按本机端口策略验证到容器级健康）

### Requirement: LangSmith 可选追踪
配置 `LANGSMITH_TRACING=true` 及密钥后，多智能体与 LLM 调用链路 SHALL 自动上报 LangSmith；未配置时零开销无报错。

#### Scenario: 未配置零开销
- **WHEN** 未设置 LangSmith 环境变量启动后端
- **THEN** 一切功能正常、无追踪相关异常

### Requirement: 视觉机审接入
图像（及视频首帧）生成产物 SHALL 优先经视觉模型审核（pass/suspect/violate），失败自动降级文本机审；`review_records.method` 标注 `vision`/`llm-text`/`keyword` 口径不变。

#### Scenario: 图像产物审核
- **WHEN** 图像任务产出真实图片
- **THEN** 审核链调用视觉模型并按结果走拦截/人工/放行，审核方式字段为 vision（视觉不可用时为降级方式）

### Requirement: 版本回退
项目版本快照 SHALL 支持按快照回退（恢复当时数据版本），前端提供回退入口，操作留痕。

#### Scenario: 快照回退
- **WHEN** 用户在版本历史中选择某快照执行回退
- **THEN** 项目数据恢复至该快照状态并生成回退记录（operation_logs）

## MODIFIED Requirements

### Requirement: 架构文档口径一致
《架构设计与数据库设计文档 V1.1》SHALL 与实现逐项对齐：任务队列（进程内 runner + 演进路径）、向量存储（JSONB 双模 + pgvector 演进）、对象存储（本地 + OSS 演进）、迁移（Alembic 基线）、Deep Agents（自研等价实现），每处修订含变更留痕说明。

#### Scenario: 评审对账
- **WHEN** 按报告 §2.1 七项偏离逐项对账
- **THEN** 每项处于「已补齐实现」或「文档已修订拍板」两态之一，无未收敛项

## REMOVED Requirements

### Requirement: （无移除）
**Reason**: 本 spec 不移除任何既有能力；报告中的「未开始项 CV-21」经核实已实现，属报告信息滞后而非功能缺失。
**Migration**: 无。
