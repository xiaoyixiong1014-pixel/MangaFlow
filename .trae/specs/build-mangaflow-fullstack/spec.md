# MangaFlow 全栈系统开发 Spec

## Why
项目已有完整的产品/架构/部署文档、UI 原型描述清单与共享设计包 `mangaflow-design`，但 `backend/`、`mangaflow-web/`、`mangaflow-admin-web/` 均为空壳或脚手架。需要依据文档一次性开发出可运行的全栈系统：FastAPI 后端（LangGraph 多智能体 + PostgreSQL）、两个 Vue3 + Element Plus 前端、一键启停 PowerShell 脚本，并通过测试验证。

## What Changes

### 后端（backend/，全新开发）
- FastAPI + SQLAlchemy 2.0（async）+ PostgreSQL 16，按架构文档 §5 建立 **31 张业务表**（users/wallets/wallet_transactions/orders/operation_logs/projects/episodes/scripts/characters/scenes/props/styles/asset_variants/storyboards/storyboard_characters/canvases/canvas_nodes/canvas_edges/canvas_plugins/tasks/media_assets/film_outputs/model_channels/logical_models/prompts/announcements/review_records/version_snapshots/agent_threads/langgraph_store/asset_embeddings）。
- 自动建库建表 + 种子数据：检测 `mangaflow` 数据库不存在则创建；初始化管理员（admin/admin123，role=operator）、演示用户、DeepSeek 渠道与逻辑模型、公共提示词、公告、积分套餐。
- JWT 认证（access+refresh，role 区分 end_user/operator），密码 bcrypt 哈希。
- LangGraph 多智能体：StateGraph + 总控 Supervisor + 域智能体（剧本/资产/分镜/生成/合成/一致性/画布助手/审核/成本/深度研究），LLM 统一走 DeepSeek OpenAI 兼容接口（base_url=`https://api.deepseek.com`，api_key 按用户提供，model=`deepseek-flash`，可环境变量覆盖）。
- 业务 API（用户端）：认证、个人中心/用量统计、项目 CRUD（软删除）、剧本生成（SSE 流式，LangGraph 剧本 Agent）与编辑、实体提取与冲突裁决、资产管理（角色/场景/道具/风格/变体/锁定/一致性校验）、分镜（AI 拆解/提示词分层/拖拽排序/实体强绑）、生成任务（关键帧打样→全量/视频/TTS/合成，异步任务 runner + SSE 进度）、成片与分享、画布（CRUD/节点连线/JSON 导入导出/环检测/DAG 增量执行/画布助手）、提示词库、素材库（来源标记/引用保护）、模型配置（自有 Key AES-256）、计费（冻结/扣费/幂等退款/充值订单/积分流水）、公告、版本快照。
- 业务 API（管理端，role=operator 门禁）：看板统计、用户管理（积分调整/禁用留痕）、审核队列处置、模型渠道管理（Key 加密/健康探测）、订单管理（人工完成/退款）、内容运营（公告/公共提示词）、系统设置。
- 合规审核：三级闸门记录（generate/compose/publish）写入 review_records；模拟机审（关键词规则）+ 人工复核队列。

### 前端（mangaflow-web/，基于脚手架全新开发）
- 接入 `@mangaflow/design`（file 依赖 + styles.css/element-plus.css），默认创作端深色主题、官网浅色，可明暗切换本地记忆。
- 实现 UI 清单全部页面：L-01 官网首页、L-02/03 登录注册与找回、W-01 Agent 工作台、W-02 项目列表 + S-01 新建弹窗、S-02 剧本创作（双栏流式）、S-03 实体提取确认、S-04 资产管理 + S-05 变体抽屉、S-06 分镜设计、S-07 内容生成 + S-08 配音合成、S-09 审核与发布、C-01 画布列表、C-02 画布编辑器（自研节点画布：10 类节点/连线/小地图/自动保存/助手）、跨项目角色与场景、P-02 提示词库、P-03 素材库、P-04 模型配置、P-05 计费中心、P-06 个人中心、L-04 分享预览、/help、/collab 灰显、403/404。
- 统一布局：左侧固定侧栏 11 项菜单 + 顶栏（积分余额/通知/主题切换/头像）；Studio 五阶段步骤条互跳；≤1360px 侧栏折叠、≤1024px 抽屉化。

### 前端（mangaflow-admin-web/，基于脚手架全新开发）
- 独立浅色管理端：A-01 登录（非 operator 拒绝）、A-02 经营看板（指标卡+趋势图+资损告警区）、A-03 用户管理（积分调整/禁用二次确认留痕）、A-04 审核队列、A-05 模型配置管理（渠道/逻辑模型/健康探测三 Tab）、A-06 订单与计费（五 Tab，高危操作二次确认）、A-07 内容运营（公告+公共提示词）、A-08 系统设置。

### 脚本与测试
- 根目录 `mangaflow.ps1`：`start|stop|status` 子命令，一键完成建库初始化→启动 backend(8000)/mangaflow-web(5173)/mangaflow-admin-web(5174)，停止时安全终止无残留进程，status 显示各服务运行状态。
- 后端 pytest 测试（auth/billing/script/storyboard/tasks/admin），前端 type-check + build 通过，最终全链路冒烟验证。

## Impact
- Affected specs: 无（首个 spec）
- Affected code:
  - `backend/**`（全新）
  - `mangaflow-web/src/**`（脚手架扩展）
  - `mangaflow-admin-web/src/**`（脚手架扩展）
  - `mangaflow.ps1`、`backend/requirements.txt`
  - 只读引用：`项目开发文档/**`（需求与表结构依据）、`mangaflow-design/**`（已构建，一般不需改动）

## 环境约束（已探明）
- PostgreSQL 16 已运行：localhost:5432，postgres/123456，**mangaflow 库尚未创建**（由 init_db 自动创建）。
- **本机无 pgvector 扩展**（仅 pg_trgm）→ 偏差：`langgraph_store.embedding`、`asset_embeddings.embedding` 用 **JSONB** 存储，不建 ivfflat 索引，代码层预留切换 pgvector 的注释说明。
- Redis 3.0.504 可用但过旧 → 偏差：**不引入 Celery**，异步任务用进程内 asyncio 任务执行器 + tasks 表状态 + SSE 推送（保留任务表幂等键、重试计数、错误分类等字段，后续可平滑切换 Celery）。
- Python 3.13.14 / Node 24.18.0 可用。
- LLM：仅 DeepSeek（OpenAI 兼容 /chat/completions），参数走配置（默认按用户提供值）；图像/视频/TTS 无真实渠道 → 实现 **Mock Provider**（落盘占位图/静态视频/合成音频），保证全流程闭环可演示，Provider 层可插拔。
- FFmpeg 本机未必安装 → 合成优先调 ffmpeg，缺失时降级为占位成片并在任务记录注明。

## ADDED Requirements

### Requirement: 数据库与初始化
系统 SHALL 按架构文档 §5 在 PostgreSQL（localhost:5432/mangaflow，postgres/123456）自动建库、建 31 张表、写入种子数据（管理员/演示用户/DeepSeek 渠道与逻辑模型/公共提示词/公告/积分套餐），重复执行幂等。

#### Scenario: 首次初始化
- **WHEN** 运行 init_db（或首次启动 backend）
- **THEN** 创建 mangaflow 库与全部表，存在 admin（operator）与 demo 用户、DeepSeek model_channels/logical_models 记录
- **AND** 再次运行不重复插入、不破坏数据

### Requirement: 认证与用户
系统 SHALL 提供注册/登录/刷新/资料修改/密码修改接口，JWT 携带 role，operator 可访问管理端接口。

#### Scenario: 注册登录
- **WHEN** 用户注册（用户名+邮箱+密码）后登录
- **THEN** 返回 access/refresh token；用 refresh 可换取新 access；错误凭证返回 401

#### Scenario: 越权防护
- **WHEN** 用户 A 访问用户 B 的项目接口
- **THEN** 返回 403/404，不泄露数据

### Requirement: 计费引擎
系统 SHALL 实现积分冻结→成功扣费/失败幂等退款流程，全部流水入 wallet_transactions（含幂等键唯一约束、bear 字段），并支持充值订单、流水查询、低额预警字段。

#### Scenario: 生成任务计费
- **WHEN** 提交生成任务（余额充足）
- **THEN** 先冻结积分；任务成功后冻结转扣费；失败/取消全额退款；重复回调因幂等键不重复扣款

### Requirement: Studio 五阶段流程
系统 SHALL 提供剧本→资产→分镜→生成→发布完整 API：
- 剧本：三输入模式、SSE 流式生成结构化 JSON 剧本、保存编辑、体检、实体提取（角色/场景/道具+别名+冲突）与人工裁决
- 资产：CRUD+生成状态机（pending→generating→done/failed）、主照→锁定（face_locked/outfit_variant_id 两层）→派生变体（inherit_mask）、一致性校验结果
- 分镜：AI 拆解（storyboard_characters 强绑+link_confidence）、提示词两层（prompt_locked/editable）、拖拽重排、时长预警
- 生成：关键帧打样/全量、视频、TTS、合成任务（tasks 表 + 幂等键 + 进度 + SSE）、失败重试、成本归属
- 发布：成片版本（film_outputs/is_current）、三级审核记录、分享链接（token+有效期+密码）、导出

#### Scenario: 剧本流式生成
- **WHEN** 调用剧本生成 SSE 接口
- **THEN** DeepSeek 流式返回并增量写库/推送，完成后剧本 content 结构化落库，状态推进至 scripting→asset 可用

#### Scenario: 任务失败退款
- **WHEN** 生成任务失败（provider 异常）
- **THEN** 任务置 failed + error_code，冻结积分全额退款（bear=platform 或 user 按错误类型），SSE 推送失败事件

### Requirement: 画布
系统 SHALL 提供画布 CRUD、节点/连线管理（10 类节点、自环禁止、应用层成环检测）、JSON 导入导出、增量执行（is_dirty/cache_ref 标记）、节点生成任务、画布助手对话（LangGraph）。

#### Scenario: 成环检测
- **WHEN** 添加会形成环的连线
- **THEN** 返回 400 明确报错，数据不变

#### Scenario: 增量执行
- **WHEN** 修改某节点后执行
- **THEN** 仅该节点及其下游重算，未变更上游复用 cache_ref

### Requirement: LangGraph 多智能体
系统 SHALL 用 LangGraph StateGraph 实现 Supervisor 意图路由 + 域智能体（≥8 类），LLM 统一经 DeepSeek；agent_threads 记录会话，版本快照关联检查点；提供 `/agent/chat` SSE 对话接口与各阶段智能体调用入口。

#### Scenario: 意图路由
- **WHEN** 用户在工作台发送"帮我写一个校园悬疑短片剧本"
- **THEN** Supervisor 识别为剧本创作意图并调用剧本 Agent，SSE 返回生成内容

### Requirement: 管理端 API
系统 SHALL 提供 role=operator 门禁的管理接口：看板统计（用户/收入/订单/生成量/积分负债）、用户管理（调整积分/禁用需 reason 且写 operation_logs）、审核队列（通过/驳回+suggestion）、模型渠道（AES-256 加密 Key、健康探测、优先级）、订单（人工完成/退款留痕）、公告与公共提示词、站点设置。

#### Scenario: 非管理员拒绝
- **WHEN** end_user 调用任意 /api/admin/* 接口
- **THEN** 返回 403

### Requirement: mangaflow-web 用户端前端
系统 SHALL 按 UI 清单 V2.1 实现 §一 全部路由页面，使用 Element Plus + @mangaflow/design token；侧栏 11 项菜单定稿；Studio 顶栏五阶段步骤条；深色创作端/浅色官网+主题切换；界面响应式（1360/1024 断点）；所有数据经 axios 对接真实后端 API（含 SSE 流式渲染、任务进度轮询/SSE、图片预览）。

#### Scenario: 主转化链
- **WHEN** 未登录用户在官网输入创意点击发送
- **THEN** 弹出登录注册弹窗并保留输入，登录成功后落地 /agent 且意图预填

#### Scenario: Studio 联动
- **WHEN** 在 S-02 确认剧本并完成 S-03 实体提取
- **THEN** S-04 出现对应资产卡且可生成定妆照，五阶段步骤条可互跳

### Requirement: mangaflow-admin-web 管理端前端
系统 SHALL 实现独立登录页与 8 个管理页面（横向顶栏导航、浅色主题），对接 /api/admin/* 全部接口；高危操作（积分调整/禁用/退款）二次确认+原因输入。

#### Scenario: 管理员处置审核
- **WHEN** 管理员在 A-04 驳回一条待审记录并填写建议
- **THEN** review_records.manual_status=rejected，用户端 S-09 可见结果

### Requirement: 一键启停脚本
`mangaflow.ps1` SHALL 支持 `start`（检查 PostgreSQL→建库初始化→准备 venv/依赖→启动 backend/两个前端 dev server→输出访问地址）、`stop`（按 PID 文件终止进程并校验端口释放，无残留）、`status`（显示各服务与数据库状态）。

#### Scenario: 一键启动
- **WHEN** 执行 `.\mangaflow.ps1 start`
- **THEN** backend:8000 /docs 可访问，web:5173、admin:5174 可访问，重复执行不重复起进程

#### Scenario: 一键停止
- **WHEN** 执行 `.\mangaflow.ps1 stop`
- **THEN** 三服务进程终止，8000/5173/5174 端口释放

### Requirement: 测试与质量
系统 SHALL 提供后端 pytest 单元/集成测试（认证、计费幂等、剧本生成、分镜强绑、任务退款、管理端门禁等核心链路）且全部通过；前端 vue-tsc type-check 与 build 通过；backend README 说明 API 与启动方式；代码含模块/函数级中文注释。

#### Scenario: 测试通过
- **WHEN** 在 backend 下执行 pytest
- **THEN** 全部用例通过（连本地 mangaflow 测试库或复用主库隔离数据）

## REMOVED Requirements
（无）
