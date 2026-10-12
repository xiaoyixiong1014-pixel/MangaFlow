# 创作链路缺口补全 Spec（complete-creation-chain-gaps）

## Why

《漫序 MangaFlow 创作链路功能核对报告 V1.0》（2026-10-12）实证：五阶段主链路完成度约 75%（60 个功能点：✅36 / 🟡18 / ❌6），存在 3 项阻断级可信度缺陷（密钥明文入库、占位 SVG 被当"成片"返回、mock 兜底产物不可交付）与 1 项结构性缺口（剧集管理有表无接口无 UI）。本 spec 按报告 §7 优先级建议，系统性补全 P0/P1/P2 缺口，使主链路功能完整、可信、可交付。

## What Changes

### P0 可信度与合规（对应报告缺陷 D1/D2/D3）
- **D1 密钥治理**：`backend/app/core/config.py` 中 5 处 `sk-` 明文默认值（deepseek/agnes_llm/agnes_image/agnes_video/bailian_tts，行 85/92/97/102/120）改为环境变量注入（默认空串），新增 `.env.example` 模板，启动时对"渠道已启用但密钥为空"打告警日志。
- **D2 占位成片修复**：`compose_service._poster_film_url()`（compose_service.py:451）不再返回 SVG；改为优先用 ffmpeg lavfi 生成可播放的"占位 MP4"（含"演示占位"提示文字），无 ffmpeg 时任务明确置为 failed 并给出原因，不再把 `.svg` 当成片 URL 写入 film_outputs。
- **D3 mock 兜底可交付性治理**：image_mock / video_mock / tts_mock 产物统一叠加"演示占位"标识（画面文字 + 文件名 `placeholder_` 前缀 + task.meta.placeholder=true）；带占位标记的任务扣费豁免（生成时已扣的积分在确认产物为 mock 后自动退款）；前端产物卡片显示"演示占位"角标，发布闸门对含占位片段的成片给出提示（不阻断，按决策点 2 方案 B）。

### P0 核心场景（对应报告 S-06）
- **剧集管理落地**：新增 episodes CRUD API（列表/创建/更新/删除/排序），复用既有 `episodes` 表（models/project.py:63）；剧本、分镜、生成、发布链路支持按剧集组织（episode_id 已在各表预留）；前端 StudioLayout 增加剧集切换器，ScriptView 支持按剧集生成与查看剧本；资产支持跨剧集复用入口（决策点 3 方案 A：基础 CRUD 先上，RD-09 剧集级覆盖暂不做）。

### P1 链路补全（对应报告 S-08 / A-04 / P-06 / P-12 / P-03 / P-08 / A-10）
- **S-08 剧本一键同步**：新增 `POST /api/script/send-to-agent`（携带剧本上下文发起 Agent 会话）与 `POST /api/script/send-to-canvas`（剧本节点投递画布），幂等可重入；ScriptView 增加对应按钮。
- **A-04 参考图锚点**：characters/scenes 增加 `reference_image_url` 字段（Alembic 迁移），AssetsView 上传替换改为真实落库（`/api/media/upload` → 资产字段 → 生成时作为图生图参考图入参，image_agnes 支持 images 入参，mock 渠道记录 meta）。
- **P-06 生成页上传替换**：生成任务产物新增上传替换接口（`POST /api/generation/tasks/{id}/replace`，校验归属与格式，落库并回写 storyboard 缩略图/产物 URL），前端 ProductionView 增加替换入口。
- **P-12 视频参数配置**：`VideoCreateRequest`（generation.py:24）扩展 `resolution/fps/motion_strength` 三参数（决策点 5 方案 A），透传真实渠道（video_agnes/video_wan），任务 meta 记录；前端视频 Tab 增加配置项。
- **P-03/P-08 首尾帧落地**：新增尾帧关键帧生成能力（storyboards.end_frame_url 真实写入），`use_end_frame=true` 时视频渠道以首尾帧模式入参（真实渠道支持时；mock 记录 meta）；前端视频 Tab 支持"指定尾帧"选择。
- **A-10/CV-24 画布沉淀回流**：新增 `POST /api/canvas/nodes/{id}/sink`（画布图像产物沉淀为角色/场景/道具资产，带来源标记 source=canvas 与 RD-47 特征描述自动补全），画布节点面板增加"沉淀入资产库"操作。

### P2 收尾（对应报告 A-05 / SB-08 / SB-06 / SB-07 / R-06 / R-03 / P-16 / P-17 / D-04 / D-06 / S-09）
- **A-05 资产增强**：资产标签（tags 字段 + 标签搜索）、批量删除接口与 UI。
- **SB-08 分镜导出 PDF**：`GET /api/storyboards/episodes/{id}/export?format=pdf`（reportlab/FPDF 中文方案，与既有 Excel 并列）。
- **SB-06 连续性检查**：`POST /api/storyboards/continuity-check`，按 RD-23"窄而准"口径：相邻镜头比对角色出场/服装变体/场景衔接/提示词关键词冲突，仅报告高置信度问题（宁可漏报不误报），分镜页展示预警。
- **SB-07 自动调整**：目标总时长偏差超阈值时提供"按比例缩放各镜时长/提示生成密度调整建议"接口与一键应用。
- **R-06 分享二维码**：发布页分享卡片生成二维码（前端 qrcode 库渲染分享链接，无需后端）。
- **R-03 片段重生成直连**：PublishView 分镜条增加"重生成该片段"按钮，直达生成页并预选对应分镜（复用既有增量合成）。
- **P-16 双语字幕**：字幕样式增加双语开关（原文 + 译文两行 ASS），译文由 LLM 翻译生成。
- **P-17 转场类型**：xfade 链透传 type（fade/dissolve/wipeblack 等原生名映射：淡入淡出/叠化/闪黑），不再一律按 fade。
- **D-04/D-06 工作台补全**：ProjectListView 统计卡展示图片数/视频时长/积分消耗（接口已有）；项目列表"最近编辑"置顶（updated_at 排序项 + 默认排序）。
- **S-09 失败三路径显式化**：剧本生成失败面板提供"重试 / 换模型 / 精简输入后重试"三个显式动作（换模型调用既有渠道配置，精简输入提供输入裁剪提示）。

### 质量与交付
- **API 文档**：新增 `backend/docs/api-creation-chain.md`（主链路全部接口：方法/路径/参数/响应/错误码，RESTful 口径说明）；FastAPI /docs 已自动生成 OpenAPI，文档补充业务口径。
- **测试**：新增 pytest 用例覆盖上述全部新增接口与逻辑（mock 外部渠道）；全量回归出证据（对应报告 D9）；双端 vue-tsc + build 通过。
- **性能保障**：新增接口一律分页/限参，episodes/资产列表查询补索引校验，复用既有连接池；回归阶段对主链路核心接口做冒烟压测记录（响应时间基线）。

## Impact

- Affected specs: 无既有 spec 覆盖此范围（build-mangaflow-fullstack / fix-critical-defects-and-architecture-alignment / harden-capabilities-and-real-models 均已完成，本 spec 为其后续收敛轮）。
- Affected code:
  - 后端：`backend/app/core/config.py`、`backend/app/services/{compose_service,script_service,asset_service,storyboard_service,generation_service}.py`、`backend/app/api/{script,assets,storyboards,generation,publish,canvases}.py`、`backend/app/providers/{image_mock,video_mock,tts_mock,image_agnes,video_agnes,video_wan}.py`、`backend/app/models/project.py`（新增字段走 Alembic 迁移）、`backend/app/tasks/runner.py`
  - 前端：`mangaflow-web/src/views/studio/{ScriptView,AssetsView,StoryboardView,ProductionView,PublishView}.vue`、`mangaflow-web/src/views/projects/ProjectListView.vue`、`mangaflow-web/src/layouts/StudioLayout.vue`、画布编辑器视图
  - 新增文件：`.env.example`、`backend/docs/api-creation-chain.md`、Alembic 迁移脚本、若干 pytest 测试文件
- 决策点采用报告建议：水印口径维持可见角标（仅文档口径，不动代码）；mock 兜底走方案 B（打标 + 扣费豁免）；剧集管理基础 CRUD 先上；队列中间件（Celery+Redis）本期不动（报告口径"上线前"，属架构演进另立 change）；P-12 做全三参数。

## ADDED Requirements

### Requirement: 密钥环境变量注入（D1）
系统 SHALL 从环境变量读取全部模型渠道密钥，配置文件中不得包含任何明文密钥默认值；启动时对缺密钥的已启用渠道输出 WARNING。

#### Scenario: 无 .env 启动
- **WHEN** 开发者不配置任何密钥启动后端
- **THEN** 服务正常启动，真实渠道因密钥为空走降级链（mock），日志含"渠道缺少密钥"告警，仓库中无 `sk-` 明文

### Requirement: 占位成片可播（D2）
项目无可用片段时合成任务 SHALL 产出可播放的占位 MP4（或明确失败态），不得将 SVG 作为成片 URL 返回。

#### Scenario: 空项目合成
- **WHEN** 对无任何视频片段的项目发起合成
- **THEN** film_outputs 中的产物 URL 以 `.mp4` 结尾且 `<video>` 可播放，画面含"演示占位"标识；无 ffmpeg 环境时任务置为 failed 并说明原因

### Requirement: mock 兜底产物打标与扣费豁免（D3）
mock 渠道产物 SHALL 携带统一占位标识（文件名前缀 + meta.placeholder + 画面标识文字），且 SHALL 对占位产物执行扣费豁免（原路退款）。

#### Scenario: 渠道降级生成图像
- **WHEN** 图像渠道降级到 mock 并产出占位图
- **THEN** task.meta.placeholder=true、文件名以 `placeholder_` 开头、画面含"演示占位"文字、用户积分被退还且流水备注"演示占位豁免"

### Requirement: 剧集管理 CRUD（S-06）
系统 SHALL 提供 episodes 的创建/列表/更新/删除/排序接口与前端切换器，剧本/分镜/生成/发布可按剧集组织。

#### Scenario: 多剧集项目流程
- **WHEN** 用户在项目中创建 2 个剧集并分别在剧本页切换生成剧本
- **THEN** 两剧集剧本互不覆盖，分镜与成片按所选剧集过滤展示

### Requirement: 剧本同步 Agent/画布（S-08）
系统 SHALL 提供剧本一键投递 Agent 会话与画布的接口及按钮。

#### Scenario: 一键投递画布
- **WHEN** 用户在 ScriptView 点击"投递画布"
- **THEN** 画布出现携带剧本内容的节点，重复点击产生幂等提示而非重复节点

### Requirement: 资产参考图锚点（A-04）
用户 SHALL 可为角色/场景上传参考图，生成任务 SHALL 将参考图作为图生图入参传递给真实渠道。

#### Scenario: 参考图生图
- **WHEN** 用户上传参考图并点击"生成定妆照"
- **THEN** 请求携带参考图 URL（真实渠道按图生图入参，mock 记录 meta.reference_image_url）

### Requirement: 生成结果上传替换（P-06）
用户 SHALL 可在生成页上传自定义图片替换 AI 产物，替换结果落库并联动分镜缩略图与合成。

#### Scenario: 替换关键帧
- **WHEN** 用户对已完成的关键帧任务上传本地图
- **THEN** 任务产物 URL 更新为上传文件，分镜缩略图同步更新，替换记录留痕（meta.replaced_from）

### Requirement: 视频参数配置（P-12）
视频生成请求 SHALL 支持 resolution/fps/motion_strength 三参数，参数透传渠道并写入任务 meta。

#### Scenario: 自定义参数生成
- **WHEN** 用户选择 1080P/30fps/运动幅度"高"发起视频生成
- **THEN** task.meta 记录三参数，真实渠道请求携带对应字段

### Requirement: 首尾帧视频生成（P-03/P-08）
系统 SHALL 支持为分镜生成尾帧关键帧，并在 `use_end_frame=true` 时以首尾帧模式发起视频生成。

#### Scenario: 尾帧模式
- **WHEN** 用户为分镜生成尾帧后勾选"使用尾帧"发起视频
- **THEN** 视频任务 meta 记录首尾帧 URL，真实渠道按首尾帧模式入参

### Requirement: 画布沉淀回流（A-10/CV-24）
用户 SHALL 可将画布图像节点产物沉淀为资产（角色/场景/道具），带来源标记与自动特征描述。

#### Scenario: 沉淀节点产物
- **WHEN** 用户对含产物的图像节点执行"沉淀入资产库"并选择类型
- **THEN** 资产库新增对应记录（source=canvas，visual_prompt 由 LLM 自动补全），资产页可见

### Requirement: P2 批量收尾
系统 SHALL 提供：资产标签搜索与批量删除（A-05）、分镜 PDF 导出（SB-08）、相邻镜头连续性检查（SB-06，只报高置信度）、目标时长自动调整建议（SB-07）、分享二维码（R-06）、发布页片段重生成直连（R-03）、双语字幕（P-16）、xfade 转场类型映射（P-17）、工作台统计完整展示与最近编辑置顶（D-04/D-06）、剧本失败三路径（S-09）。

#### Scenario: 连续性检查不误报
- **WHEN** 相邻两镜角色服装/场景无冲突
- **THEN** 检查结果为空而非罗列低置信提示

## MODIFIED Requirements

### Requirement: 合成产物返回口径（原 compose 逻辑）
`compose_film` 不得以 SVG 充当成片；所有降级产物必须可播放或任务明确失败（见 ADDED 占位成片要求）。

### Requirement: 视频生成请求模型（原 VideoCreateRequest 仅 use_end_frame）
`VideoCreateRequest` SHALL 包含 use_end_frame/resolution/fps/motion_strength，均可选且有默认值，向后兼容既有调用。

## REMOVED Requirements

### Requirement: SVG 占位成片（原 _poster_film_url 行为）
**Reason**: SVG 无法被 `<video>` 播放、无法转码导出，端到端断裂（报告缺陷 D2）。
**Migration**: 由"占位 MP4 / 明确失败态"替代，既有 `film_poster_*.svg` 历史文件不再被新任务引用（不做数据清洗，避免破坏历史记录）。
