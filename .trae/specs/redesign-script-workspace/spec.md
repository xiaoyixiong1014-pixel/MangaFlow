# 剧本创作模块页面与流程改造 Spec（redesign-script-workspace）

> 依据：《漫序 MangaFlow 剧本创作模块页面与流程改造方案 V1.0》（2026-10-14）。
> 决策点拍板（按方案 §11 推荐，全部采纳 A 选项）：
> **D1** 直接开工；**D2** 合并为三步（故事设定 / 分集大纲 / 剧本正文），人物小传并入故事设定卡；
> **D3** 对话式共创做 B 档（可执行修改 + diff 采纳 + 自动版本点，可回退）；
> **D4** `script_phase` 取值保持 `characters` 不变，UI 改称"故事设定"，零数据迁移；
> **D5** 左侧参数栏只读 + 跳转项目设置。
> 基线：`rebuild-script-pipeline` spec 已交付的四步工作台（后端 22 接口 + 前端 StepIdea/StepCharacters/StepEpisodesPlan/StepEpisodeBody）。

## Why

现行四步工作台按"数据阶段"而非"用户动作"划分步骤，创意步堆叠了"输入回显双栏 + 大纲导航 + 场景卡 + 全局工具 + 体检 + 快速模式开关"共 7 类功能（对应方案 §1.2 冗余清单）；AI 助手抽屉仅出建议不改稿（明示"不会直接改写剧本"），与"对话式共创"目标相悖。需要重构为**左中右三栏 + 三步定稿 + 对话式共创**的对齐形态，并补齐故事设定产物、单集大纲重生成、可执行改写通道三处后端缺口。

## What Changes

- **P1 三栏布局与减负（纯前端）**：`ScriptView.vue` 重构为三栏栅格（左参数栏 / 中三卡片工作区 / 右 AI 助手面板）+ 顶部通栏（标题、版本历史、导出、设置）；`el-steps` 替换为同屏三卡片当前步高亮；剥离 StepIdea 冗余块（双栏合并为可折叠输入区、大纲导航与场景卡迁出、工具行上移、快速模式折叠为次要入口）。
- **P2 故事设定产物（后端 + 前端）**：`projects.story_setting` JSONB 新增字段（nullable，方案 §8 模型 A）；`setting_planner` Agent 生成"一句话故事 / 核心冲突 / 世界观 / 人物小传"（人物部分复用 `plan_characters`）；新增 `POST /script/setting/generate`、`GET /script/setting`、`PUT /script/setting`、`POST /script/setting/regenerate` 四接口；前端 `StorySettingCard.vue` + 分流自动加载（路径 A 自动生成 + "AI 正在为您构建故事宇宙…"；路径 B 解析进度 + 预计集数确认弹窗）。
- **P3 单集大纲重生成 + 漫剧格式渲染**：新增 `POST /script/episodes/{eid}/outline/regenerate`（作用域收窄到 1 集，复用 `plan_episodes` 提示词与校验）；`utils/manga-format.ts` 将结构化 `scenes[]` 渲染为漫剧规范文本（【场景：…】【镜头：…】角色（情绪）：台词）；`StepEpisodesPlan.vue`/`StepEpisodeBody.vue` 改造为 `EpisodeOutlineCard.vue`/`ScriptBodyCard.vue` 并入中部工作区。
- **P4 对话式共创（B 档）**：新增 `POST /script/assist`（`{scope: setting|episode_outline|episode_body, target, instruction}` → diff 预览，不落库）与 `POST /script/assist/apply`（采纳片段落库 + 自动写版本快照）；复用 `optimize_episode_content` + `build_field_diffs` 机制，作用域从正文扩展到故事设定与单集大纲；`AssistantPanel.vue` 常驻对话区（diff 卡片红删绿增、采纳/拒绝/全部采纳、三组快捷 Chips、历史版本对比与回退）；移除"本助手不会直接改写剧本"旧文案。
- **P5 下游衔接**：「确认剧本正文 → 格式化拆解」串联为单一确认动作：正文出场角色与已确认角色对账（缺失提示补建）、`locations` 去重清单对账、影响面预览（RD-08），一键分镜拆解 + 可选投递画布（复用 SB-01/SB-09）。
- **质量收尾**：单元/集成/冒烟测试、API 文档、性能基线、双前端 type-check + build。

## Impact

- **Affected specs**: 无既有 spec 覆盖本次 UI 改造；基线依赖 `rebuild-script-pipeline`（已全部交付）；关联 `complete-creation-chain-gaps`（剧集 CRUD）、`harden-capabilities-and-real-models`（llm_adapter/检查点）。
- **Affected code**:
  - 后端：`backend/app/models/project.py`（Project 增 `story_setting`）、`backend/alembic/versions/`（新迁移）、`backend/app/agents/script_pipeline.py`（新增 `setting_planner`；assist 复用 optimize 机制）、`backend/app/api/script_pipeline.py`（新增 setting 域 4 接口 + outline/regenerate + assist 2 接口）、`backend/app/schemas/script_pipeline.py`、`backend/app/services/script_pipeline_service.py` 与 `script_service.py`（diff/快照复用）。
  - 前端：`mangaflow-web/src/views/studio/ScriptView.vue`（三栏重构）、`mangaflow-web/src/components/script/`（新增 ParamSidebar/AssistantPanel/StorySettingCard；StepIdea 减负；StepEpisodesPlan/StepEpisodeBody 改造为卡片）、`mangaflow-web/src/utils/manga-format.ts`（新增）、`mangaflow-web/src/api/scriptPipeline.ts`（新增 7 接口封装）。
  - 测试：`backend/tests/` 新增 3~4 个测试文件 + 全量回归；`.run/smoke_script_pipeline.py` 扩展。

## ADDED Requirements

### Requirement: 三栏工作台布局（P1）
剧本创作页 SHALL 重构为三栏布局：左侧常驻参数栏（体裁/视觉风格/忠实度/输出比例/目标时长 + 忠实度语义提示 + 预估消耗 + 当前阶段集数），中部主工作区（故事设定/分集大纲/剧本正文三卡片同屏排列、当前步骤卡片高亮），右侧常驻 AI 助手面板；顶部通栏承载标题、版本历史、导出与项目设置入口。三栏在小屏（<1200px）折叠为单栏纵向。

#### Scenario: 三栏同屏渲染
- **WHEN** 用户打开 `/projects/:id/script`
- **THEN** 页面同屏呈现左参数栏、中部三卡片（未生成阶段显示占位态）、右助手面板，当前阶段对应卡片高亮，顶部通栏含版本历史与导出按钮。

#### Scenario: 参数栏只读展示
- **WHEN** 用户查看左侧参数栏
- **THEN** 显示项目全部创作参数（数据来自 `GET /api/projects/{id}` 的 `ProjectOut`），忠实度旁有语义提示文案（自由发挥/遵循大纲/严格忠实），「修改」跳转项目设置页（D5）。

### Requirement: 冗余剥离与内容迁移（P1）
StepIdea 冗余块 SHALL 按以下映射处置：左右双栏（原始输入/AI 生成）合并为单一可折叠输入区；「大纲导航」迁入分集大纲卡片；「场景卡列表」迁入剧本正文卡片；工具行（版本历史/导出）迁至顶部通栏；「快速模式」开关折叠为次要入口（保留功能，方案 §1.2 #3）。

#### Scenario: 创意输入区减负
- **WHEN** 用户在故事设定卡片查看输入区
- **THEN** 仅见一个可折叠的输入区（含创意文本与快速模式次要入口），不再出现大纲导航与场景卡。

### Requirement: 故事设定产物与接口（P2，方案 §8 #1~4）
系统 SHALL 提供"故事设定"产物：一句话故事（logline）/ 核心冲突 / 世界观 / 主要人物小传。存储采用 `projects.story_setting` JSONB 新增字段（nullable，存量项目不受影响）。人物小传复用现有角色域数据（config 候选 + characters 表），`script_phase` 取值不变（D4）。

#### Scenario: 生成故事设定
- **WHEN** 用户（或分流自动流程）触发 `POST /script/setting/generate`
- **THEN** `setting_planner` Agent 基于"创意 + 体裁/风格 + 忠实度档位（RD-01 `build_fidelity_directive`）"生成设定四段与人物候选，人物候选写入 `config.script_characters`（与现角色域一致），设定四段落入 `story_setting`，响应携带最新 `phase`。

#### Scenario: 编辑与重生成
- **WHEN** 用户编辑设定文本后调用 `PUT /script/setting`（RD-03 `source=user`），或对不满意字段触发 `POST /script/setting/regenerate`（可带 instruction）
- **THEN** 编辑仅更新设定字段并标记用户来源；重生成按 instruction 重出设定段，已确认角色不回退；两接口均写版本快照。

### Requirement: 分流自动加载（P2，方案 §4）
按新建项目输入类型 SHALL 分流：
- **路径 A（一句话创意）**：经路由参数（如 `?auto=1`）标记后自动触发故事设定生成，期间显示"AI 正在为您构建故事宇宙…"状态文案，完成后停留在故事设定卡片等待确认/编辑；
- **路径 B（小说/粘贴大纲）**：展示解析进度（复用 `novel/parse` 章节切分），随后生成分集大纲；当预计集数超过阈值（默认 10 集，常量可调）时弹窗"您的小说预计拆解为 N 集，是否确认？"，用户确认后方进入分集大纲卡片。

#### Scenario: 一句话创意自动生成
- **WHEN** 用户从新建弹窗以一句话创意创建项目并跳转剧本页（携带 auto 标记）
- **THEN** 页面自动开始生成故事设定并显示构建中文案；生成完成后不再需要手动点「开始生成」。

#### Scenario: 长篇集数确认
- **WHEN** 小说解析推定的分集数超过阈值
- **THEN** 弹窗显式展示预计集数与确认/取消操作，确认后才调用分集大纲生成。

### Requirement: 单集大纲重生成（P3，方案 §8 #5）
系统 SHALL 提供 `POST /script/episodes/{eid}/outline/regenerate`：仅重生成指定一集的大纲（标题/核心剧情点/出场人物/情绪走向/时长），复用 `plan_episodes` 的提示词骨架、已确认角色强约束与忠实度挂点，作用域收窄到该集；该集正文若已生成则提示将失效（正文不自动删除，由用户决定重生成）。

#### Scenario: 单集重生成
- **WHEN** 用户对第 2 集点击「重新生成」并可附带 instruction
- **THEN** 仅第 2 集 `episodes` 行的 outline 更新（version+1），其余集不受影响；若该集已有正文，响应携带提醒字段。

### Requirement: 漫剧规范格式渲染（P3，方案 §3.3）
前端 SHALL 提供 `utils/manga-format.ts` 渲染器，把结构化 `scenes[]`（action/location/time_of_day/int_ext/sfx/emotion + dialogue[].type/emotion/character_id，字段已齐备）渲染为漫剧规范文本视图（形如【场景：地点，时间，内/外景】+ 动作行 + 角色（情绪）：台词），与现有"文章体 / 场次结构"视图并列；渲染为纯前端行为，可选支持导出该格式。

#### Scenario: 正文漫剧格式视图
- **WHEN** 用户在剧本正文卡片切换到"漫剧格式"视图
- **THEN** 每个场次渲染为【场景：…】标题行、动作描写行与"角色（情绪）：台词"行；旁白/独白/画外音按 type 区分标注。

### Requirement: 对话式改写通道（P4，方案 §6 / §8 #7，B 档）
系统 SHALL 提供可执行改写的对话通道：
- `POST /script/assist`：`{scope: setting|episode_outline|episode_body, target, instruction}` → 返回 diff 预览片段（不落库），复用 `optimize` 四粒度的 diff 机制（`build_field_diffs`），作用域从正文扩展到故事设定与单集大纲；
- `POST /script/assist/apply`：接受选中片段后落库（片段 `path+original` 一致性校验防并发覆盖），自动写版本快照（stage 标记 assist 采纳），支持「采纳 / 拒绝 / 全部采纳」；
- 前端 `AssistantPanel.vue` 为常驻对话区：自然语言指令作用于当前聚焦对象（故事设定 / 某集大纲 / 正文某段），修改回执以 diff 卡片呈现（红删绿增），每次采纳自动打版本点；预设三组快捷 Chips（剧情 / 台词 / 节奏，方案 §6.2，即预置 instruction 走同一通道，不新增协议）；历史版本列表支持对比与回退（复用 `/snapshots`、`/versions/{id}/rollback`）；
- 禁止一键整篇覆盖；旧抽屉"本助手不会直接改写剧本"文案 SHALL 移除。

#### Scenario: 指令改写并采纳
- **WHEN** 用户在助手面板输入"第 1 集冲突太弱，提前安排反派出场"（或点击对应 Chip）且当前聚焦第 1 集大纲
- **THEN** 返回该集大纲的 diff 卡片（红删绿增），用户点「采纳」后仅选中片段落库，自动新增一条版本快照，版本列表可对比回退。

#### Scenario: 全部采纳
- **WHEN** 用户点击 diff 回执的「全部采纳」
- **THEN** 本次预览的全部片段按片段校验规则应用落库，写单条版本快照。

### Requirement: 下游格式化拆解串联（P5，方案 §7）
「确认剧本正文」SHALL 串联为格式化拆解确认动作：① 正文出场角色与已确认角色对账（缺失者提示补建）；② `locations` 去重清单对账（缺失场景概念图资产提示）；③ 影响面预览（复用 RD-08 接口）；随后一键分镜拆解（SB-01）并可选投递画布（SB-09），均复用既有接口，仅做 UI 串联与对账提示。

#### Scenario: 确认后一键拆解
- **WHEN** 用户在剧本正文卡片点击「确认剧本，生成资产」且对账无缺失
- **THEN** 弹出拆解确认框（展示角色/场景对账结果与影响面摘要），确认后触发分镜拆解并提示可投递画布。

## MODIFIED Requirements

### Requirement: 剧本创作页步骤结构（原四步工作台）
剧本创作页 SHALL 由"顶部 el-steps 四步条 + 四步独立页"改为"同屏三卡片 + 当前步高亮"：三卡片为 ① 故事设定（含人物小传，原角色设定步骤并入，D2）、② 分集大纲、③ 剧本正文；`script_phase` 映射保持 `NULL/idea→①、characters→①、outline→②、body→③`，未达阶段卡片呈占位/锁定态，已确认内容置灰禁编逻辑保留。快速模式（`script_phase=NULL`）链路保留：步骤①内提供折叠的快速模式入口，原"一句话→生成→编辑→确认剧本"行为不变。

#### Scenario: 阶段卡片高亮与推进
- **WHEN** `script_phase='outline'` 时用户打开剧本页
- **THEN** 分集大纲卡片高亮且可交互，故事设定卡片呈已确认态（可回看编辑人物小传），剧本正文卡片为待生成占位；点击故事设定卡内「下一步：生成分集大纲」可顺序推进。

#### Scenario: 故事设定确认推进
- **WHEN** 用户在故事设定卡片点击「确认故事设定，进入分集大纲」
- **THEN** 复用现有角色确认入池逻辑（`characters/confirm`），设定文本一并保存，`script_phase` 推进为 `outline`（映射保持 characters 值语义，D4）。

### Requirement: AI 助手形态（原抽屉式建议助手）
AI 助手 SHALL 由抽屉式"仅出建议"升级为右侧常驻 `AssistantPanel`：对话式共创（ADDED Requirement 见上），聚焦对象跟随中部卡片选中态切换；抽屉交互移除。

#### Scenario: 聚焦联动
- **WHEN** 用户点选第 2 集大纲卡片使其成为当前步
- **THEN** 助手面板的聚焦对象显示"第 2 集 · 分集大纲"，快捷 Chips 切换为大纲相关指令组。

## REMOVED Requirements

### Requirement: StepIdea 双栏「原始输入 / AI 生成」并置布局
**Reason**: 输入已在新建弹窗填写，此处回显重复；右栏生成前为空壳（方案 §1.2 #1）。
**Migration**: 合并为单一可折叠输入区，仅保留创意文本编辑与重新生成入口。

### Requirement: 创意步内「大纲导航」与「场景卡列表」区块
**Reason**: 属后续步骤内容越界展示（方案 §1.2 #2）。
**Migration**: 大纲导航迁入分集大纲卡片；场景卡列表迁入剧本正文卡片（改造为卡片组件的一部分）。

### Requirement: 抽屉式 AI 剧本助手及其"不会直接改写剧本"声明
**Reason**: 能力不足且与对话式共创目标相悖（方案 §1.2 #4、§6.3）。
**Migration**: 升级为右侧常驻 `AssistantPanel`，支持可执行改写 + diff 采纳 + 自动版本点；旧文案删除。
