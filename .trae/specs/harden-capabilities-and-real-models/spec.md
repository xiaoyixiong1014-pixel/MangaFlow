# MangaFlow 能力真实化与全模块补全 Spec

## Why
上一阶段（`build-mangaflow-fullstack`）已完成全系统结构铺开（154 功能点结构覆盖 75%），但《项目进度统计分析报告 V1.0》实证：图像/视频/TTS 全链路为 mock、LLM 仅单渠道无兜底、审核闸门二/三直通、水印仅写标识、一致性校验为伪公式、多智能体缺 HITL/并行/Time Travel、无监控/导出/通知/插件体系（真实能力就绪度约 45%）。本期按进度报告第 5/7 节清单"补芯"：接入用户提供的真实模型渠道，系统性完成 14 个功能模块全部剩余功能。

## What Changes

### 1. 模型路由与真实 Provider（核心）
- 新建 `services/model_router.py`：`resolve_channel(capability, ...)` 按 LogicalModel(is_default/指定) → channel_priority 序列 → ModelChannel(enabled + health_status) 逐级解析可用渠道，全部失败降级 mock 渠道（链尾保底，演示闭环不断）。
- `providers/llm.py` 重构为多渠道：主 DeepSeek、兜底 Agnes，主渠道异常自动切换；保持 `chat/chat_stream` 签名兼容（14 处调用方无需改动）。
- 新增真实 Provider：`providers/image_agnes.py`（OpenAI images API 兼容，url/b64_json 双模式）、`providers/video_agnes.py`（OpenAI videos 风格：创建→轮询→下载，兼容同步/异步响应）、`providers/tts_bailian.py`（OpenAI 兼容 audio/speech 端点，音色映射表）、`providers/embedding.py`（/embeddings 尝试 + 哈希词袋降级）。
- `tasks/runner.py` 的 `_run_image/_run_video/_run_tts` 改经 model_router 选渠道；`init_db.py` 种子新增 Agnes(LLM/Image/Video) 与阿里云百炼(TTS) 渠道及逻辑模型，channel_priority 形成降级链，Key 全部 Fernet 加密存储。
- `api/models_config.py` / `admin/models.py` 健康探测覆盖全部新渠道。

### 2. 任务队列增强与通知（SYS-01、P-09）
- tasks 表新增 `priority`（默认 5）、`next_retry_at`、`dead_letter`；领取 SQL 按 priority 优先 + next_retry_at 到期过滤；重试改指数退避（2^n 分钟）；重试耗尽标记死信，管理端可查看/重新入队。
- 新增站内通知：`notifications` 表 + `api/notifications.py`（列表/未读数/已读）；任务成功/失败/审核结果/配额预警时写入并经现有 SSE 通道推送。

### 3. 多智能体补全（MA-06/08/09/10/11/13/16/17/18/19/20）
- MA-08 HITL：实体提取确认、全量生成前确认两个 `interrupt()` 检查点；SSE 返回 `pending_confirm` 事件，前端确认后 `Command(resume=...)` 续跑。
- MA-10 并行：批量生成用 `Send` map-reduce 分发到并行子节点。
- MA-11 双流：SSE 对话同时产出 `updates`（节点级）与 `messages`（token 级）事件。
- MA-06 Time Travel：`GET /api/agent/threads/{id}/history`（get_state_history）+ `POST .../rewind`（按 checkpoint_id 回退）。
- MA-09：一致性检查、内容审核封装为可复用 Subgraph。
- MA-17/18：deep_research_agent 升级为 Deep Agents 模式（LangGraph 规划-执行-反思循环 + 虚拟文件系统笔记存 langgraph_store），不引入重依赖。
- MA-13：LangSmith 经环境变量 `LANGSMITH_TRACING` 可选接入（langchain 原生支持）。
- MA-16/19：`agents/llm_adapter.py` 统一 ChatModel 封装，三框架口径统一。

### 4. 审核真实化与水印（R-04/08/09、C-01/02/04）
- 分级词表机审（严禁=violate / 敏感=suspect 放人工队列）；图像生成增加提示词机审 + 生成后基于提示词与元数据的 LLM 文本审核（审核方式标注于 review_records.method）。
- 闸门二（compose）真实化：汇总成片全部片段审核记录，存在未处置 violate/suspect 则拦截；闸门三（publish）真实化：全链路记录汇总判定，替换 publish.py 两处直通占位。
- 分级处罚（C-04）：违规累计 → restricted（限流：每日生成任务数上限）/ banned（禁止生成），生成接口统一校验。
- 隐形水印真实化：compose_service 用 ffmpeg drawtext 将 watermark_id 半透明写入画面角落；无 ffmpeg 降级为标识写入。

### 5. 合成增强（P-14/15/16/17）
- 字幕：分镜 narration 自动生成 SRT 并 ffmpeg subtitles filter 烧录（字幕样式参数可选）；BGM：曲库目录 + 管理接口 + amix 混音 + 用户自定义上传；转场：相邻片段 xfade；增量合成：mode=incremental 按 storyboard_ids 局部拼接。
- 全部能力以 ffmpeg 探测为前提，缺失时降级现有占位逻辑。

### 6. 语义检索与一致性真实化（A-08、MA-07）
- `providers/embedding.py` 向量写入 langgraph_store/asset_embeddings（JSONB 余弦相似度，Python 计算，预留 pgvector 切换注释）。
- `consistency_check()` 替换伪公式：视觉提示词 + 参考描述向量相似度 + 锁定状态加权，输出可解释分项。
- 资产库语义搜索接口（按描述文本检索相似资产）。

### 7. 导出能力（U-04/U-05、SB-08、R-04）
- 项目 ZIP 导出（剧本/分镜/资产元数据 JSON + 媒体文件）/ 导入还原；分镜表 Excel(openpyxl)/CSV 导出；成片 MP4 导出下载接口（按分辨率参数转码，无 ffmpeg 时返回原片）。

### 8. 可观测性与安全（SYS-02/03）
- 引入 prometheus-client：`/metrics` 端点（HTTP 计数/延迟直方图/任务状态计数/渠道健康 gauge）；管理端看板新增系统指标区块。
- 越权防护全量回归：所有 /api 资源接口逐一验证归属校验。

### 9. 画布增强（CV-12/13/19/20/21/22）
- 前端：画布内搜索面板、框选（marquee）、复制粘贴、跨画布子树复制。
- 插件体系：`canvas_plugins` 安装/启用/停用/审查接口 + 前端插件面板；插件定义为受控 JSON 描述（端口+执行 endpoint），运行时 HTTP 调用插件 endpoint（规避任意 JS 沙箱风险）；节点插件 SDK（TS 类型定义 npm 包内置于 mangaflow-design）。
- CV-19 MCP 桥：后端 MCP streamable HTTP 客户端（连接外部 MCP server、工具列表化、画布 `mcp_tool` 节点调用）。

### 10. 前端体验补全（D-04/D-05/PL-11/PL-24 等）
- D-04 创作统计改后端聚合接口（项目/图片/视频时长/积分消耗，口径与 PRD 对齐）；D-05 模型渠道健康状态工作台标红提醒；PL-24 配额预警（<20% 通知+SSE）；PL-11 模型效果对比引导页（同任务多模型并排生成对比）；通知铃铛中心；Time Travel 回退 UI；闸门审核状态 UI；导出入口（ZIP/Excel/MP4）；BGM/字幕/转场配置 UI。

### 质量要求（全程约束）
- 接口符合 RESTful 规范，FastAPI /docs 自动文档 + 注解完善；函数/类/文件级中文注释；响应时间：列表类 <500ms、SSE 首帧 <3s；pytest 覆盖新增链路且全绿；双前端 vue-tsc + build 通过。

## 模型渠道接入参数（用户提供，写入 config 默认值与种子数据，Key 加密入库）

| 用途 | 优先级 | base_url | 模型名称 |
| ---- | ------ | -------- | -------- |
| 文本 LLM | 主 | `https://api.deepseek.com`（Key: sk-461126bc...df42eb） | deepseek-flash |
| 文本 LLM | 兜底 | `https://apihub.agnes-ai.com/v1`（Key: sk-Z22X73oT...dNT1H） | agnes-3.0-flash |
| 图像生成 | 1 | `https://apihub.agnes-ai.com/v1/images/generations` | agnes-image-2.5-flash |
| 视频生成 | 1 | `https://apihub.agnes-ai.com/v1/videos` | agnes-video-2.5-flash |
| 配音 TTS | 1 | `https://ws-66dgbbhi5gzzll8r.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`（阿里云百炼） | qwen-audio-3.1-tts-flash |

> 图像/视频/TTS 渠道失败时自动降级至既有 mock 渠道（channel_priority 链尾），保证演示闭环不中断。

## Impact
- Affected specs: `build-mangaflow-fullstack`（增量演进，不推翻）
- Affected code:
  - `backend/app/providers/**`（llm 重构 + 4 个新 provider）
  - `backend/app/services/model_router.py`（新）、`generation_service/review_service/compose_service/asset_service/canvas_service`
  - `backend/app/agents/**`（graph/supervisor/deep_research/llm_adapter）
  - `backend/app/tasks/runner.py`、`backend/app/api/**`（notifications/projects export/storyboards export/publish/agent_chat/canvases/admin）
  - `backend/app/models/**`（tasks/notifications/users 等少量新增字段与表）
  - `backend/app/core/init_db.py`、`backend/requirements.txt`、`backend/tests/**`
  - `mangaflow-web/src/**`（统计/通知/导出/画布/对比/插件 UI）
  - `mangaflow-admin-web/src/**`（指标区/DLQ/插件审查）

## ADDED Requirements

### Requirement: 模型路由与多渠道降级
系统 SHALL 提供统一模型路由层：按能力（llm/image/video/tts）解析默认逻辑模型 → channel_priority 渠道序列 → 依次校验 enabled 与 health_status → 返回首个可用渠道；全部失败时降级 mock 渠道。LLM 调用主渠道（DeepSeek）异常时 SHALL 自动切换兜底渠道（Agnes），对全部现有调用方保持签名兼容。

#### Scenario: 主渠道故障降级
- **WHEN** DeepSeek 接口持续超时
- **THEN** 剧本生成自动改用 Agnes 渠道完成，agent_threads 记录实际使用渠道；渠道 health_status 置 degraded

#### Scenario: 图像生成走真实渠道
- **WHEN** 提交定妆照生成任务
- **THEN** 经路由命中 Agnes 图像渠道并产出真实图片落盘 /storage/generated，失败自动降级 mock 渠道并在 task.meta 记录降级链

### Requirement: 真实视频与 TTS Provider
系统 SHALL 实现视频生成（Agnes：创建任务→轮询状态→下载成片，兼容同步/异步响应，轮询超时可配置）与 TTS（百炼 OpenAI 兼容 audio/speech，音色映射表覆盖多角色音色），产出真实 mp4/wav 并回写时长。

#### Scenario: 视频任务全流程
- **WHEN** 提交 5 秒视频生成任务
- **THEN** runner 调用 Agnes 视频渠道轮询至 completed，mp4 落盘、任务 done、积分正常扣费；渠道失败时降级 mock 并注明

### Requirement: 任务队列增强与通知中心
系统 SHALL 支持任务优先级调度（高优先先领）、指数退避重试（2^n 分钟，上限 3 次）、死信队列（重试耗尽标记 dead_letter，管理端可重派）；并提供站内通知（任务完成/失败/审核结果/配额预警），未读数与已读接口 + SSE 实时推送。

#### Scenario: 失败指数退避与死信
- **WHEN** 一个视频任务连续失败 3 次
- **THEN** 重试间隔按 2/4/8 分钟递增，耗尽后置 dead_letter，管理端死信列表可见并可重新入队，冻结积分全额退款

### Requirement: 多智能体 HITL 与并行
系统 SHALL 在实体提取确认与全量生成前引入 LangGraph `interrupt()` 人工确认点（SSE 事件 `pending_confirm` + `Command(resume)` 续跑），批量生成 SHALL 使用 `Send` 并行分发，对话流 SHALL 同时输出节点级与 token 级事件。

#### Scenario: 生成前人工确认
- **WHEN** 用户触发全量关键帧生成（经 Agent 流程）
- **THEN** 图在确认点暂停并推送 pending_confirm，用户确认后续跑完成批量生成；拒绝则终止且冻结积分退款

#### Scenario: 会话回退
- **WHEN** 用户在工作台选择历史检查点回退
- **THEN** 会话状态恢复至该检查点，可继续对话

### Requirement: 审核闸门真实化与分级处罚
系统 SHALL 实现分级词表机审（严禁/敏感两级）与图像生成内容审核；闸门二 SHALL 汇总成片全部片段审核记录判定（未处置违规即拦截），闸门三 SHALL 全链路汇总判定；违规累计触发分级处罚（restricted 限流 / banned 禁用）。

#### Scenario: 闸门拦截违规成片
- **WHEN** 成片包含未处置的 suspect 审核记录时发布
- **THEN** 闸门三返回拦截并说明原因，publish 状态不推进；人工复核通过后可再发布

### Requirement: 真实水印与合成增强
系统 SHALL 在合成时用 ffmpeg 将 watermark_id 半透明烧录到画面；支持字幕烧录（narration→SRT）、BGM 曲库混音（含自定义上传）、相邻片段转场（xfade）、按 storyboard_ids 增量合成；无 ffmpeg 时全部降级占位。

#### Scenario: 增量合成带字幕与 BGM
- **WHEN** 用户选择部分分镜增量合成并勾选 BGM 与字幕
- **THEN** 产出含烧录字幕、BGM 混音、转场与水印的新版本 film_output，is_current 切换正确

### Requirement: 语义检索与一致性真实化
系统 SHALL 提供 embedding 能力（OpenAI 兼容 /embeddings 优先，哈希词袋降级）并以余弦相似度实现：资产一致性校验（分项可解释评分）与资产语义搜索；向量存 JSONB 并预留 pgvector 切换。

#### Scenario: 一致性校验可解释
- **WHEN** 对角色主照与变体运行一致性校验
- **THEN** 返回基于描述向量相似度与锁定状态的真实分项评分（非伪公式），并支持按文本语义检索相似资产

### Requirement: 数据导出与导入
系统 SHALL 提供项目 ZIP 导出/导入（剧本/分镜/资产元数据 + 媒体文件还原）、分镜表 Excel/CSV 导出、成片 MP4 导出（按分辨率转码）。

#### Scenario: 项目导出再导入
- **WHEN** 导出项目 ZIP 后在新账号导入
- **THEN** 剧本/分镜/资产结构与媒体文件完整还原，任务与积分记录不迁移

### Requirement: 可观测性
系统 SHALL 暴露 `/metrics`（Prometheus 格式：请求计数、延迟直方图、任务状态计数、渠道健康），管理端看板展示系统指标区块；全部资源接口通过越权防护回归。

#### Scenario: 指标可抓取
- **WHEN** GET /api/metrics
- **THEN** 返回 Prometheus 文本格式指标，含任务状态计数与渠道健康 gauge

### Requirement: 画布插件与 MCP 桥
系统 SHALL 提供画布插件管理（安装/启用/停用/审查，插件为受控 JSON 描述 + HTTP endpoint 执行）、节点插件 SDK（TS 类型定义）与 MCP streamable HTTP 客户端桥（外部 MCP server 工具注册为 `mcp_tool` 节点可调用）；画布编辑器支持搜索、框选、复制粘贴与跨画布子树复制。

#### Scenario: 安装并运行插件节点
- **WHEN** 管理员审查通过一个图像放大插件（JSON 描述 + endpoint）
- **THEN** 用户画布可添加该插件节点，执行时经后端代理调用 endpoint 并写回结果节点

### Requirement: 前端体验补全
创作端 SHALL 补全：后端聚合创作统计（D-04）、模型渠道健康标红提醒（D-05）、配额 <20% 预警（PL-24）、模型效果对比引导（PL-11）、通知中心铃铛、导出入口、Time Travel UI、闸门审核状态 UI、BGM/字幕/转场配置 UI、画布搜索/框选/复制与插件面板。

#### Scenario: 工作台统计与健康提醒
- **WHEN** 用户进入工作台
- **THEN** 显示今日/本月创作统计（项目数/图片数/视频时长/积分消耗，来自聚合接口），且存在不健康渠道时顶部标红提醒

## MODIFIED Requirements

### Requirement: LLM Provider（原单渠道 DeepSeek）
改为多渠道主备：DeepSeek 主 + Agnes 兜底，`chat/chat_stream` 内部完成故障切换与健康记录，签名不变。

### Requirement: 任务执行器（原即时重试）
重试改为指数退避（next_retry_at 调度），新增 priority 抢占与 dead_letter 终态；图像/视频/TTS 执行改经模型路由层选择真实渠道。

### Requirement: 审核与发布闸门（原直接通过）
`api/publish.py` 两处直通占位替换为真实汇总判定；compose_service 增加水印烧录与字幕/BGM/转场/增量能力。

### Requirement: 一致性校验（原伪公式）
`asset_service.consistency_check()` 替换为向量相似度 + 锁定状态加权的可解释评分。

## REMOVED Requirements
（无）
