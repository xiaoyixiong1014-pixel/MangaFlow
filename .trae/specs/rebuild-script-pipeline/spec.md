# 剧本创作模块范式级重构 Spec（rebuild-script-pipeline）

> 依据：《漫序 MangaFlow 剧本创作模块不足评估 V1.1》（2026-10-14），含 §3.6 范式级缺口（P0 最高优先级）、§3.1~§3.5 结构性/表达力/决议/架构缺陷、§5 修复优先级。
> 决策点拍板（§3.6.11 建议采纳）：① 保留快速模式（快速+精细并存，默认精细）；② 分集正文复用 `scripts` 表（按 `episode_id`）；③ 每阶段人工确认；④ 短剧默认 1 集 / 3 分钟，长篇按章切分；⑤ 角色前置设定、抽取降级为补充；⑥ 本范式纳入本轮。

## Why

剧本是全链路（分镜/资产/生成/配音/审核）的唯一事实源。当前实现把"角色设定 → 分集大纲 → 逐集正文"三段式生产流水线压缩成一次 LLM 调用的"单次成稿"，且存在抽取源错误、无 Schema 校验、SSE 中断丢稿等结构性缺陷——范式级缺口叠加表达力缺口会被下游逐级放大，直接威胁 M-02（核心闭环完成率 ≥60%）与 M-04（角色一致性投诉率 ≤5%），并使 US-02（小说批量改编）与场景 2（MCN 30 集）不成立。

## What Changes

### A. P0 断链修复（结构性缺陷，成本最低、立即止损）
- **抽取源改判**：实体抽取数据源从 `raw_text`（用户原始输入）改为"生成后的结构化剧本 `content`"，novel 模式下 raw_text 兜底（消解【不足 1】）。
- **Schema 校验 + 重试**：定义 `ScriptContent` Pydantic 模型；解析后强制校验；校验失败携错误重试 ≤3 次；仍失败显式报错，禁止静默降级为 `{"raw": 全文}`（消解【不足 2】，兑现 PRD S-03 业务规则）。
- **统一 LLM 出入口**：剧本生成从直连 `providers.llm.chat_stream` 改走 `agents/llm_adapter.py`（`agent_name="script"`），获得双渠道降级与成本归因（消解【不足 8】）。
- **并发幂等保护**：生成加"生成中"状态位 + 版本号乐观锁（`WHERE version = ?`），防双开标签页竞态覆盖（消解【不足 9】）。

### B. P0 范式级流水线（角色设定 → 分集大纲 → 逐集正文 → 修改/AI 优化）
- **数据模型 + 阶段状态机**：
  - `projects` 新增 `script_phase`（`idea → characters → outline → body`；NULL = 快速模式）；
  - `characters` 新增人设字段 `role_type/age/personality/motivation/arc/relations(JSONB)/confirmed_at`；
  - `episodes` 新增 `logline/outline(JSONB)/confirmed_at`；
  - `version_snapshots.stage` 新增取值 `characters_confirmed/outline_confirmed/body_generated`；
  - Alembic 迁移，新列全部 nullable，存量数据视为快速模式，不破坏现有功能。
- **角色设定阶段（5 接口 + `character_planner` Agent）**：生成角色人设卡（姓名/定位/年龄/性格/外貌/动机/弧光/关系）、读取、批量编辑、单角色重生成、确认入池（`phase='outline'`）。
- **分集大纲阶段（4 接口 + `episode_planner` Agent）**：基于角色设定生成 N 集大纲（标题/一句话/核心事件/出场角色/情绪走向/单集时长）、读取、编辑单集大纲、确认（`phase='body'`）。
- **逐集正文阶段（3 接口 + `episode_writer` Agent）**：按"本集大纲 + 角色设定强约束"逐集 SSE 流式生成文章体正文 + 结构化 `scenes[]`；单集重生成；用户保存修改。正文落 `scripts`（`uk(project_id, episode_id)`），`raw_text` 存文章纯文本。
- **剧本子图编排（LangGraph）**：新建剧本子图（`input_node → character_planner → interrupt确认 → episode_planner → interrupt确认 → episode_writer×N（Send 并行）→ interrupt确认`），复用 PostgresSaver 检查点（断点续跑）、子图封装范例、`Command(resume)`（消解【不足 3】，兑现 PRD S-03 检查点续跑）。
- **AI 优化四粒度 + diff（RD-07）**：`optimize` 接口生成 diff 预览（不落库，粒度：全局/单集/单段/单字段）+ `optimize/apply` 接口接受选中片段后落库；**禁止一键整篇覆盖**；前端 diff 预览 + 接受/拒绝。

### C. P0 表达力升级（与逐集正文阶段合并交付）
- **剧本 Schema 升级为"漫剧可拍摄剧本"**：场次内新增 `action`（动作/画面描述）、`dialogue[].type`（对白/旁白/独白/画外音）、`dialogue[].emotion`、`location` + 独立 `locations[]`（可复用地点清单，去重）、`time_of_day`（日/夜）、`int_ext`（内/外）、`sfx`（音效提示）、`dialogue[].character_id`（角色资产 ID 引用）。
- **时长口径统一**：prompt 要求与目标时长自洽（默认按体裁：短剧单集 180s 起步）；体检阈值收紧至 ±10%；新增"对白字数÷语速 vs 场次时长"交叉校验。

### D. P1 决议对齐
- **RD-02 体检规则替换**：实际四条改为决议四条——角色出场断层 / 对白过长 / 缺动作描写 / 缺高潮点；体检升级为按集体检；issue 支持定位。
- **RD-03 字段级来源标记落地**：场景数组元素级 source（场次级）；手动保存按字段合并（用户改过的字段标 user，其余保留原 source），不再整篇压成单一 `source:"user"`。
- **RD-01 忠实度差异化**：`faithful/outline/free` 三档差异化 prompt 策略（原著约束/大纲约束/自由发挥）注入 `character_planner/episode_planner/episode_writer`。

### E. P1 小说改编链路
- **txt/epub 解析 + 章节切分**：上传小说 → 章节切分（准确率目标 ≥90%，PRD S-01）→ 章节列表展示；
- **章节 → 分集映射**：章节内容作为分集大纲阶段的输入（按章映射分集），消除 `raw_text[:4000]` 硬截断对长篇的影响。

### F. P2 决议与交互收尾
- **RD-08 影响面预览**：修改/删除实体前提示"影响 N 个资产 / M 个分镜"。
- **交互收尾**：生成完成后不再展示原始 JSON 残留；体检 issue 一键定位到对应场次编辑；剧本导出（TXT / Fountain）。

### G. 测试、文档与质量
- 单元测试（pytest）：抽取源、Schema 校验重试、三个新 Agent 解析容错、阶段状态机流转、diff 生成/应用、章节切分、体检新规则、字段级 source 合并等。
- 集成测试：14 个新接口的鉴权/归属越权/幂等/阶段校验（TestClient 端到端）。
- 系统测试：四段式流水线全链路冒烟脚本（mock LLM）；既有 41 个测试文件全量回归；双前端 vue-tsc type-check + build。
- API 文档：`backend/docs/api-script-pipeline.md` 记录 14 个新接口 + 修改接口（方法/路径/参数/响应/错误码/SSE 事件协议/幂等口径/阶段校验），并记录核心接口响应时间基线。

## Impact

- **Affected specs**: 无既有 spec 文档（本 spec 独立成立）；关联既有已完成 spec：`complete-creation-chain-gaps`（剧集 CRUD）、`harden-capabilities-and-real-models`（llm_adapter/model_router/PostgresSaver/HITL）、`fix-critical-defects-and-architecture-alignment`（Alembic 基线）。
- **Affected code**:
  - 后端：`backend/app/models/project.py`（Project/Episode）、`backend/app/models/asset.py`（Character）、`backend/app/models/script.py`（不变，复用）、`backend/app/services/script_service.py`、`backend/app/api/script.py`、`backend/app/agents/`（新增 `script_pipeline.py` 子图 + 3 个 planner/writer 节点）、`backend/app/schemas/script.py`、`backend/app/core/init_db.py`、`backend/alembic/versions/`（新增迁移）；
  - 前端：`mangaflow-web/src/views/studio/ScriptView.vue`（重构为四步工作台）、`mangaflow-web/src/api/`（新增剧本流水线 API 封装）、`EntityExtractView.vue`（降级为补充抽取入口）；
  - 测试：`backend/tests/` 新增 4~6 个测试文件 + 既有用例回归。

## ADDED Requirements

### Requirement: 角色设定阶段（前置人设卡）
系统 SHALL 在剧本写作之前提供角色设定阶段：由 `character_planner` Agent 基于创意 + 体裁/风格 + 忠实度档位生成角色清单（name/role_type/age/personality/appearance/motivation/arc/relations[]），用户可编辑、新增、删除、单角色重生成，确认后写入 `characters` 表成为全链路角色事实源，`script_phase` 推进为 `outline`。

#### Scenario: 生成并确认角色
- **WHEN** 用户在角色设定阶段点击"生成角色"并随后点击"确认角色，进入分集大纲"
- **THEN** 角色以人设卡形式入库（含 relations JSONB），写 `version_snapshots(stage='characters_confirmed')`，`projects.script_phase='outline'`，角色名在后续分集与正文生成中作为强约束。

#### Scenario: 单角色重生成
- **WHEN** 用户对某个角色不满意并点击"重新生成"
- **THEN** 仅该角色的人设卡被重新生成，其余角色不受影响。

### Requirement: 分集大纲阶段
系统 SHALL 基于已确认角色 + 创意生成 N 集大纲（每集：title/logline/core_event/characters[]/emotion_curve/duration_sec），支持编辑、增删集、重排，确认后 `script_phase='body'`。

#### Scenario: 生成并确认分集大纲
- **WHEN** 用户在分集大纲阶段点击"生成分集大纲"并确认
- **THEN** `episodes` 行携带 `logline/outline(JSONB)` 落库，`confirmed_at` 写入，`projects.script_phase='body'`。

### Requirement: 逐集正文生成
系统 SHALL 按"本集大纲 + 角色设定（姓名与人设不得漂移）"逐集生成文章体正文（`scripts.raw_text`）+ 结构化 `scenes[]`（`scripts.content`，含 action/dialogue type/emotion/location/time_of_day/int_ext/sfx/character_id），支持 SSE 流式输出与单集重生成。

#### Scenario: 流式生成单集正文
- **WHEN** 用户对某一集点击"生成本集正文"
- **THEN** SSE 逐增量输出文章体正文，完成后落库 `scripts`（episode_id 绑定）并产出结构化场次 JSON；重生成仅影响该集。

#### Scenario: 断线续跑
- **WHEN** 生成期间用户刷新页面或网络中断
- **THEN** 通过剧本子图检查点（PostgresSaver）可恢复续跑，已生成内容不丢失。

### Requirement: AI 优化四粒度 + diff 接受/拒绝（RD-07）
系统 SHALL 提供剧本优化接口，支持全局/单集/单段/单字段四种粒度，以 diff 预览（不落库）返回建议，用户逐片段接受或拒绝后应用落库；禁止一键整篇覆盖。

#### Scenario: 预览并部分接受
- **WHEN** 用户选择"单场次"粒度发起优化并预览 diff
- **THEN** 系统返回变更片段清单（原文/建议文），用户勾选部分片段点击"应用"后仅选中片段落库且对应字段 source 标记为 `user`。

### Requirement: 剧本 Schema 校验与重试（S-03）
系统 SHALL 定义 `ScriptContent` Pydantic 模型并强制校验 LLM 生成的剧本 JSON；校验失败携带错误信息重试最多 3 次；重试耗尽后向用户显式报错，禁止静默降级为 `{"raw": 全文}`。

#### Scenario: 输出跑偏自动重试
- **WHEN** LLM 返回的 JSON 缺少必需字段（如 scenes 非数组）
- **THEN** 系统将校验错误回传模型重试（≤3 次）；3 次后仍失败则 SSE 发出 `error` 事件并提示重试，不落库半成品。

### Requirement: 阶段状态机与快速模式
系统 SHALL 维护 `projects.script_phase` 阶段状态机（`idea → characters → outline → body`）：精细模式按阶段推进且各阶段提供人工确认点；`script_phase=NULL` 表示快速模式（保留既有"一句话→直接出稿"链路）；阶段校验拒绝跳步调用（如未确认角色直接请求分集大纲返回 409）。

#### Scenario: 跳步拦截
- **WHEN** 项目尚处于 `characters` 阶段时调用分集大纲生成接口
- **THEN** 返回 409 并提示"请先确认角色设定"。

### Requirement: 小说章节切分与分集映射
系统 SHALL 支持上传 .txt/.epub 小说并完成章节切分（目标准确率 ≥90%），章节列表可作为分集大纲阶段的输入（按章映射分集），长文本不再受 4000 字硬截断。

#### Scenario: 上传小说切分章节
- **WHEN** 用户以 novel 模式上传 .epub 文件
- **THEN** 系统解析出章节列表（标题 + 正文范围）并展示，用户可基于章节生成分集大纲。

### Requirement: 影响面预览（RD-08）
系统 SHALL 在用户修改/删除实体（角色/场景/道具）前提供影响面预览（引用该实体的资产数、分镜数）。

#### Scenario: 删除角色前预览
- **WHEN** 用户请求删除一个已被分镜引用的角色
- **THEN** 响应包含"影响 N 个分镜"提示数据，前端展示确认。

## MODIFIED Requirements

### Requirement: 实体抽取（S-10，降级为补充抽取）
实体抽取的数据源 SHALL 改为"生成后的结构化剧本 `scripts.content`"（novel 模式下以 `raw_text` 兜底）；在精细模式下抽取职责降级为"补充道具/场景/新登场角色"（角色已在前置设定阶段入库），人工确认（RD-04）流程保持不变。

#### Scenario: 一句话模式抽取
- **WHEN** 用户在生成剧本后点击"实体抽取"
- **THEN** 抽取输入为 `content` 中的结构化剧本（标题/梗概/大纲/场次/对白拼接），抽取结果覆盖剧本中真实出现的角色/场景/道具，而非仅创意一句话。

### Requirement: 结构化体检（RD-02 对齐）
体检规则 SHALL 替换为决议四条，且升级为按集体检：
1. **角色出场断层**——已确认角色在某集大纲/正文缺失引用或中途消失（warning，定位到集）；
2. **对白过长**——单条对白超过阈值字数（warning，定位到场次）；
3. **缺动作描写**——场次无 `action` 字段或为空（warning，定位到场次；快速模式旧 Schema 下自动跳过）；
4. **缺高潮点**——末集/末场缺少情绪峰值标记（info，定位到集）。
阈值与计分保持 100 分制；时长校验阈值收紧至 ±10%。

#### Scenario: 体检命中新规则
- **WHEN** 某场次 `action` 为空且某条对白超过 120 字
- THEN 体检报告分别输出"缺动作描写"与"对白过长"两条 issue，均带可定位的场次坐标。

### Requirement: 字段级来源标记（RD-03 落地）
来源标记 SHALL 下沉到场次级：`scenes[]` 每个元素携带 `source`；手动保存时按场次比对合并——用户改动的场次标 `source:"user"`，未改动场次保留原 `source:"ai"`；AI 优化接受的片段标 `source:"ai_optimized"`。

#### Scenario: 部分编辑保留来源
- **WHEN** AI 生成 6 个场次后用户仅修改第 2 场并保存
- THEN 落库后第 2 场 `source="user"`，其余 5 场仍为 `source="ai"`。

### Requirement: 忠实度档位差异化（RD-01）
忠实度档位 SHALL 实质影响生成策略：`faithful` 在 planner/writer 注入原著约束与一致性校验要求；`outline` 注入"遵循给定大纲"约束；`free` 放开创意发挥（更高温度）。三档均有差异化提示词与校验规则，而非仅文本拼接。

#### Scenario: faithful 模式约束生效
- **WHEN** 项目忠实度为 `faithful` 且上传了小说原文
- THEN `episode_writer` 的提示词携带"不得偏离原著情节/人物设定"约束段落，且生成后校验角色名与原著章节出场名单的一致性。

### Requirement: 生成接口统一 LLM 出入口与并发保护
剧本生成（快速模式与逐集正文）SHALL 统一经 `agents/llm_adapter.py` 调用（`agent_name` 区分），获得渠道路由降级与用量归因；生成接口 SHALL 维护"生成中"状态位并采用版本号乐观锁（`WHERE version = ?`）提交，冲突时返回 409 提示刷新。

#### Scenario: 重复提交拦截
- **WHEN** 用户在生成进行中再次点击"生成"
- THEN 第二次请求返回 409"正在生成中，请稍候"。

## REMOVED Requirements

### Requirement: 抽取源使用 raw_text 的旧行为
**Reason**: 一句话创意模式下从"用户原始输入"抽取必然失真（UI 测试 C-BUG-5 根因），属最严重设计缺陷。
**Migration**: 抽取源改判为 `scripts.content` 结构化剧本（novel 模式 raw_text 兜底）；角色来源切换为前置设定阶段后，抽取降级为补充职责。

### Requirement: 手动保存整篇压成单一 source:"user" 的行为
**Reason**: 使"用户改过的字段不被 AI 覆盖"（RD-03）无法实现。
**Migration**: 改为场次级比对合并保存（见 MODIFIED Requirements）；`_content_plain` 解包逻辑保持兼容三种落库形态。

### Requirement: 体检旧四条规则（无场景/无对白/对白缺角色名/时长偏差>20%）
**Reason**: 与 RD-02 决议完全不符（无一条相同）。
**Migration**: 替换为决议四条（角色出场断层/对白过长/缺动作描写/缺高潮点）；"无场景数据""全剧无对白"作为 Schema 校验阶段的前置防御保留在生成闸门中，不再出现在体检报告。
