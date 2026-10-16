# 漫序 MangaFlow · 剧本创作模块复核评估 V2.0

| 项目 | 内容 |
| ---- | ---- |
| 报告类型 | **复核评估（Re-audit）**：验证 V1.1 评估所列缺口在代码中是否已闭环 |
| 复核对象 | 剧本创作模块（PRD **S-01~S-10** + 架构 §4.2 + 决议 **RD-01~RD-08** + V1.1 §3.6 范式缺口） |
| 复核基线 | 《漫序 MangaFlow 剧本创作模块不足评估 V1.1》（2026-10-14） |
| 复核方式 | 逐文件代码走查（带文件:行号）+ **全量回归测试实跑** + 前端组件核对，**不采信任何文档结论** |
| 复核日期 | 2026-10-14 |
| 状态 | 待项目组确认 |

**变更记录**

| 版本 | 日期 | 变更内容 |
| ---- | ---- | -------- |
| V1.0 | 2026-10-14 | 初稿：剧本域 10 项不足 + 决议落地缺口 + 修复优先级 |
| V1.1 | 2026-10-14 | 新增 §3.6「范式级缺口」：按明示的期望范式（**角色设定 → 分集大纲 → 逐集正文 → 用户修改/AI 优化**）校准评估 |
| **V2.0** | **2026-10-14** | **复核版**：代码已按 V1.1 大范围改造，逐条复验并给出闭环结论；新增 §6 超出评估的增强项、§7 残余问题。**本版取代 V1.1** |

---

## 1. 复核结论（TL;DR）

> **结论：V1.1 所列缺口已实现闭环——范式 7 步全部落地，10 项不足 9 项完全闭环、1 项部分闭环；后端回归测试 342 例全绿。**

| 复核维度 | V1.1 判定 | V2.0 实测 | 结论 |
| -------- | --------- | --------- | ---- |
| **范式流水线**（§3.6，7 步） | 整体未实现 | **7/7 落地** | ✅ 闭环 |
| **10 项不足** | 10 项未闭环 | **9 项完全闭环 + 1 项部分** | ✅ 基本闭环（95%） |
| **决议落地**（RD-01~RD-08） | 3 条未落地 / 3 条半落地 | **8/8 落地** | ✅ 闭环 |
| **回归测试** | 342 例无最新证据 | **342 passed / 0 failed（728s）** | ✅ 全绿 |

**本次变更规模（10-14 单日）**

| 指标 | V1.1 时 | V2.0 实测 | 变化 |
| ---- | ------- | --------- | ---- |
| 后端接口（script 域） | 9 + 剧集 5 | **11（script.py）+ 18（script_pipeline.py）+ 5（episodes）** | 大幅新增 |
| `script_service.py` | 744 行 | **1807 行** | +1063 |
| `api/script.py` | 677 行 | **1182 行** | +505 |
| 新增后端文件 | — | `api/script_pipeline.py`(2155)、`agents/script_pipeline.py`(1435)、`agents/script_pipeline_graph.py`(1264)、`services/script_pipeline_service.py`(476)、`novel_parser.py`(534)、`script_export_service.py`(518)、`pdf_export_service.py`(249) | **+6631 行** |
| 新增前端文件 | — | `StepIdea`(1707)、`StepCharacters`(723)、`StepEpisodesPlan`(638)、`StepEpisodeBody`(1587)、`api/scriptPipeline.ts`(824) | **+5479 行** |
| 测试用例 | 324 函数 / 342 收集 | 324 函数 / **342 收集（15 个剧本专项测试文件）** | 全绿 |

---

## 2. 范式级缺口复核（V1.1 §3.6 —— 逐步骤）

| 步骤 | V1.1 判定 | V2.0 实测 | 证据 |
| ---- | --------- | --------- | ---- |
| ① 创意输入 | ✅ 已有 | ✅ 保留（旧链路原样保留在 `StepIdea`） | `ScriptView.vue:11`；`StepIdea.vue`(1707 行) |
| ② **角色设定** | ❌ 未实现 | ✅ **已实现** | `api/script_pipeline.py`：`/characters/generate`(337)、`GET /characters`(389)、`PUT /characters`(432)、`/characters/{cid}/regenerate`(502)、`/characters/confirm`(598) |
| ③ **分集大纲** | ❌ 未实现 | ✅ **已实现** | `/episodes/plan` 生成(820)/读取(927)、`PUT /episodes/{eid}/outline`(960)、`/episodes/plan/confirm`(1049)；`_map_chapters_to_episodes`(682) 章节→分集映射 |
| ④ **逐集正文** | ❌ 未实现 | ✅ **已实现** | `/episodes/{eid}/body/generate/stream`(1316)、`PUT .../body`(1363)、`.../body/regenerate`(1433)；前端长文编辑 `StepEpisodeBody.vue`(1587 行) |
| ⑤ 用户修改 | 🟡 部分 | ✅ **已实现** | `PUT .../body` + `merge_scene_sources`(script_service:528) **场次级 source 合并** |
| ⑥ **AI 优化（RD-07）** | ❌ 未实现 | ✅ **已实现** | `/optimize`(1546) 四粒度 + `build_field_diffs`(script_service:825) **不落库预览**；`/optimize/apply`(1595) 按 accept_ids 应用 |
| ⑦ 小说→分集映射 | ❌ 未实现 | ✅ **已实现** | `services/novel_parser.py`：`split_txt_chapters`(54) 章节正则 + `parse_epub`(**标准库 zipfile+XML，未引入 ebooklib**) + `summarize_chapters` |

**范式编排（V1.1 §3.6.4 的"三段式剧本子图"）已落地：**

| 编排要素 | V2.0 实测 | 证据 |
| -------- | --------- | ---- |
| `character_planner` + interrupt 1 | ✅ | `script_pipeline_graph.py:507` `sp_character_planner`；`:574` `interrupt({"type":"confirm_characters"})` |
| `episode_planner` + interrupt 2 | ✅ | `:651` `sp_episode_planner`；`:726` `interrupt({"type":"confirm_episodes"})` |
| `episode_writer × N` 并行 | ✅ | `:816/840` `Command(goto=[Send(NODE_EPISODE_WRITER, {...}), ...])` |
| 正文汇总 + interrupt 3 | ✅ | `:927` `sp_body_reduce`；`:1048` `interrupt({"type":"confirm_body"})` |
| 一键串跑（auto_confirm） | ✅ | `:496` 三处闸口可跳过 interrupt（供"一键写完"与串跑测试） |
| 断点续跑 / 状态查询 | ✅ | `GET /script/pipeline/state`(1907)、`/pipeline/status`(1965)、`/pipeline/run/stream`(2028)、`/pipeline/resume`(2097) |
| interrupt 恢复幂等 | ✅ | 节点内"恢复重跑 → 复用 config 候选"，避免重复消耗（`script_pipeline_graph.py:518`） |

> **V1.1 的判断被验证**："技术能力已全部就绪，缺的只是编排"——本次正是以 `interrupt`/`Send`/子图/`PostgresSaver` 组装出剧本子图。

---

## 3. 10 项不足复核

| # | V1.1 不足 | V2.0 判定 | 证据 |
| - | --------- | --------- | ---- |
| 1 | 实体抽取读错数据源（读 `raw_text`） | ✅ **已闭环** | `build_extract_source_text`（`script_service.py:1033`，注释"P0 断链修复：抽取源从 raw_text 改判为生成的完整剧本"）；`:1581` 复核确认；无结构化 payload 时兜底 `raw_text` |
| 2 | 无 Schema 校验、无重试 | ✅ **已闭环** | `schemas/script.py:186 ScriptContent` / `:204 ScriptBodyContent`；`validate_script_content`(259)/`validate_script_body_content`(232) 返回 `(dict, 错误摘要)`；`MAX_GENERATE_ATTEMPTS = 3`（`api/script.py:95`），`:281` 循环重试 |
| 3 | Studio 生成不走 LangGraph、SSE 中断即丢 | 🟡 **部分闭环** | **新分阶段路径 ✅**：`script_pipeline_graph.py` 走 `PostgresSaver` 检查点 + `/pipeline/state|status|resume` 支持断线重连；**旧快速路径（StepIdea 的 `/script/generate/stream`）仍为"done 时落库"**（`api/script.py` 流式仅 `:288` 透传、`:335` 汇总后 commit）→ 见 §7 残余 R1 |
| 4 | 小说改编链路缺失 + 长文截断 | ✅ **已闭环** | `novel_parser.py`（txt 章节正则 + **epub 标准库解析** + 章节摘要）；`/script/input/novel/parse`(1106)、`/input/novel/chapters`(1155)；`_map_chapters_to_episodes` 章节→分集 |
| 5 | Schema 表达力不足 | ✅ **已闭环** | `SceneItem` 新增 `action`/`location`/`time_of_day`/`int_ext`/`sfx`/`emotion`；`DialogueItem` 新增 `type`(对白/旁白/独白/画外音)/`emotion`/`character_id`（**角色资产 ID 引用**，RD-24 依据成立） |
| 6 | 场次/地点概念混淆 | ✅ **已闭环** | `LocationItem`(name/description/time_of_day) + `ScriptContent.locations` 去重地点清单；`SceneItem.location` 引用其 name |
| 7 | 默认时长口径不自洽 | ✅ **已闭环** | `DEFAULT_TARGET_DURATION = 180`（`script_service.py:50`，注释"P-1 Task 8 时长口径统一"），`build_generate_messages` 以 `{target}` 占位符注入（:322/330） |
| 8 | 绕过模型路由/适配层 | ✅ **已闭环** | `api/script.py:33` `from app.agents.llm_adapter import get_last_usage, llm`；`:285` 注释"统一 LLM 出入口：经 llm_adapter 单例调用（带用量观测）" |
| 9 | 并发/幂等保护不足 | ✅ **已闭环** | 同项目**进程内生成锁**（并发第二个请求 409）+ **乐观锁** `UPDATE ... WHERE version=旧值`（`script_pipeline.py:1288/1644`；`api/script.py:217`） |
| 10 | 架构一致性（两套实现漂移） | ✅ **已闭环** | 快速路径与分阶段路径**共用** `llm_adapter` 与 `script_service` 工具（`content_plain`/`unwrap_scenes`/`wrap_content_fields`/`merge_scene_sources` 已由路由层迁入服务层统一） |

**小计：✅ 完全闭环 9 项；🟡 部分闭环 1 项（不足 3 的旧快速路径）。**

---

## 4. 决议落地复核（V1.1 §3.3）

| 决议 | V1.1 判定 | V2.0 实测 | 证据 |
| ---- | --------- | --------- | ---- |
| **RD-01** 忠实度档位差异化 | 🟡 半落地 | ✅ **已落地** | `build_fidelity_directive`（`agents/script_pipeline.py:135`）按 free/outline/faithful 返回差异化约束段；**faithful 档位额外做"名单外角色"一致性校验并触发重试 1 次**（`:74/182/210`） |
| **RD-02** 体检四条 | ❌ 未落地 | ✅ **已落地** | `run_health_check`（`:1207`）四条与决议逐字对齐：①角色出场断层 ②对白过长（>120 字）③缺动作描写 ④缺高潮点（末 20% 场次 emotion 峰值词）；支持按集体检（`location_prefix`） |
| **RD-03** 字段级 source + 差异确认 | ❌ 未落地 | ✅ **已落地** | `wrap_content_fields`（`:400`）顶层包装 + **scenes 元素级 source**；`merge_scene_sources`（`:528`）场次级差异化合并（用户改过的场次保留 `user`） |
| RD-04 抽取须人工确认 | ✅ | ✅ 保持 | `extract/confirm` |
| RD-05 别名归一可视化 | 🟡 | ✅ 增强 | `detect_alias_conflicts` 保留；配合角色设定阶段的 `relations` 字段与确认页 |
| RD-06 描述冲突用户裁决 | ✅ | ✅ 保持 | conflicts 产出 |
| **RD-07** 四粒度 + diff 接受/拒绝 | ❌ 未落地 | ✅ **已落地** | `grain ∈ {global, episode, segment, field}`；`_resolve_optimize_target`(1499) 校验 target；`/optimize` **不落库**返回片段化 diff；`/optimize/apply` 按 `accept_ids` 应用 —— **完全符合"局部 + 预览 diff + 可接受/拒绝，禁止一键整篇覆盖"** |
| **RD-08** 影响面预览 | ❌ 未落地 | ✅ **已落地** | `GET /script/entities/impact`（`api/script.py:524`）**修改前只读预览**：`script_refs`（剧本引用）+ `storyboard_refs`（分镜文本匹配 ∪ 结构化引用去重），**不落任何库表** |

**小计：8/8 已落地。**

---

## 5. 超出评估的增强项（Bonus）

| # | 增强 | 说明 |
| - | ---- | ---- |
| 1 | **剧本导出（txt / Fountain / PDF）** | `script_export_service.py`（`render_txt:376`、`render_fountain:458`，遵循 fountain.io 语法）+ `pdf_export_service.py`；接口 `GET /script/export`(584) —— 补上了 V1.1 §3.5 提出的导出诉求 |
| 2 | **场次内外景 `int_ext`** | V1.1 仅建议"昼夜/内外景"，实现同时给了 `int_ext`（int/ext） |
| 3 | **faithful 档位一致性守卫** | 不仅差异化 prompt，还**统计名单外角色并触发重试**——超出 RD-01 原始要求 |
| 4 | **性能基线冒烟脚本** | `.run/smoke_script_pipeline.py`(843 行) 覆盖"注册→建项目→idea→角色 generate/编辑/confirm→分集大纲→…"，并输出**接口与 SSE 首字节性能基线**（3 次取中位数） |
| 5 | **interrupt 恢复幂等** | 节点重跑时复用 `project.config` 中已落候选，避免恢复后重复消耗 LLM |
| 6 | **epub 零依赖解析** | 用标准库 `zipfile + xml.etree + html.parser` 解析 OPF manifest/spine，**未引入 ebooklib**（降低供应链风险） |

---

## 6. 验证证据

### 6.1 全量回归测试（实跑）

```
.venv/Scripts/python.exe -m pytest tests/ -q
342 passed in 728.43s (0:12:08)
```

| 项 | 值 |
| -- | -- |
| 收集用例 | **342**（54 个测试文件） |
| 结果 | **342 passed / 0 failed** |
| 耗时 | 728s（12 分 08 秒） |
| 剧本专项测试文件 | **15 个**：`test_script_phase`、`test_script_pipeline_basics`、`test_script_characters`、`test_script_episodes_plan`、`test_script_episode_body`、`test_script_pipeline_graph`、`test_script_subgraph`、`test_script_optimize`、`test_script_health_rules`、`test_script_export_impact`、`test_novel_parser`、`test_novel_pipeline`、`test_script_sync`、`test_script`、`test_episodes` |

### 6.2 未执行的验证（建议补做）

| 项 | 说明 | 建议 |
| -- | ---- | ---- |
| `smoke_script_pipeline.py` | 需后端运行 + 真实渠道；本次仅做代码核对，未实跑 | 由项目组在渠道可用时执行一次，留存输出 |
| 前端四步目视验收 | `StepIdea/StepCharacters/StepEpisodesPlan/StepEpisodeBody` 已集成（`ScriptView.vue:19-22/121-123`，按 `script_phase` 映射步骤 1~4） | 按 UI 测试方法对四步主链路补一轮浏览器自动化验收 |
| 真实渠道下的正文质量 | 十集正文生成为 LLM 输出，质量依赖渠道健康度 | 渠道恢复后做一轮 3 集样例人工评审 |

---

## 7. 残余问题与建议（诚实记录）

> 以下不否定本次改造成果，但属"仍未完全闭环"或"新引入的观察项"，建议纳入台账跟踪。

| # | 残余问题 | 级别 | 影响 | 建议 | 建议负责人 | 建议截止 |
| - | -------- | ---- | ---- | ---- | ---------- | -------- |
| **R1** | **旧快速路径（`StepIdea` 的 `/script/generate/stream`）仍为"done 时落库"**，SSE 中断丢失已生成内容；与新分阶段路径（有检查点+`/pipeline/resume`）**行为不一致** | P1 | 快速模式下断网=白刷 token；两条路径体验割裂 | 快速路径改为每 N 秒增量落库，或直接复用 `script_pipeline_graph` 的 input 段（低成本统一） | 待确认（后端） | 2026-10-24 |
| **R2** | **分阶段模式下 S-10 抽取仍返回 characters**，可能与本阶段②已确认入库的角色重复（`extract_entities` 未见按已确认角色过滤） | P2 | 抽取确认页出现重复角色，易误建重复资产卡 | 抽取结果对已确认角色名做**去重/标注**（"已存在，跳过"）；或分阶段模式下抽取只处理 props/scenes | 待确认（后端） | 2026-10-24 |
| **R3** | **RD-03「差异确认」交互完整性待目视确认**：后端 `merge_scene_sources` 已具备场次级 source 能力，但前端是否呈现"正文与结构化字段不一致 → [同步][忽略]"的确认条，本次未取证 | P2 | 决议的关键交互可能只落了"合并"没落"确认" | 前端补一轮核对（对标 UI 原型 S-02 的字段同步提示条） | 待确认（前端） | 2026-10-28 |
| **R4** | **回归测试耗时 728s（12 分钟）**，单文件曾达 183s；且用例依赖真实 LLM/DB，存在外部不稳定风险 | P2 | 回归周期长，影响迭代节奏；CI 易 flaky | 拆分 `@pytest.mark.slow`；对 LLM 依赖用固定桩；目标全量 < 5 分钟 | 待确认（测试） | 2026-11-07 |
| **R5** | **阶段恢复依赖"角色列表接口回执的 phase"**（`ScriptView.vue:8` 自述"项目详情暂不吐 script_phase"） | P3 | 直接深链进入某阶段时可能定位不准 | 项目详情接口补吐 `script_phase`，统一恢复来源 | 待确认（后端+前端） | 2026-11-07 |
| **R6** | **非本模块但仍在台账**：① `config.py` 5 处 `sk-` 明文硬编码；② 隐形水印实为可见角标 | P0/P2 | 合规红线 / 口径偏差 | 见《创作链路功能核对报告》D1/D12 行动项 | 待确认 | 2026-10-17 |

---

## 8. 结论与下一步

### 8.1 结论

**剧本创作模块已完成从"单次成稿"到"四段式生产流水线"的范式升级**，V1.1 所列的范式缺口（7/7）、10 项不足（9 完全 + 1 部分）、8 条决议（8/8）均已落地，并以 **342 例全绿回归**作为验收证据。模块的**结构完整性已不再是短板**——剩余风险集中在**旧路径一致性（R1）、交互完整性（R3）与回归效率（R4）**三类工程细节。

### 8.2 建议立即做的 3 件事

1. **【本周】执行一次 `smoke_script_pipeline.py` 并留存输出**——这是当前唯一"能力已就绪但未取得运行证据"的主链路（需渠道可用）。同时补一轮前端四步浏览器验收，把"代码已闭环"升级为"可演示已闭环"。
2. **【本周】关闭 R1（旧快速路径增量落库）**——成本低、收益直接：消除两条路径的行为差异，避免"快速模式下断网丢内容"成为演示事故点。
3. **【本周】处理 R6 的密钥明文轮换**——与剧本模块无关但优先级最高（合规红线），建议一并纳入本轮收口。

### 8.3 建议下一份产出

- 待上述 3 件事完成后，把本报告 §1 的复核结论**回写进《项目进度统计分析报告》**（剧本域 S-01~S-10 由 95% → 可评"已闭环"，并同步《创作链路功能核对报告》中 S-06/S-08 的过期判定）。

---

## 9. 附录：复核证据索引

| 结论 | 证据位置 |
| ---- | -------- |
| 阶段状态机 | `backend/app/models/project.py:41`（`CHECK script_phase IN ('idea','characters','outline','body')`） |
| 角色人设字段 | `backend/app/models/asset.py:97-110`（role_type/age/personality/motivation/arc/relations/confirmed_at）+ `:65` CHECK |
| 分集大纲字段 | `backend/app/models/project.py` Episode：`logline`/`outline`/`confirmed_at` |
| 抽取源修复 | `backend/app/services/script_service.py:1033,1581` |
| Schema 校验与重试 | `backend/app/schemas/script.py:186,204,232,259`；`backend/app/api/script.py:95,281` |
| 体检四条（RD-02） | `backend/app/services/script_service.py:1109-1317`（四条规则 + 峰值词表） |
| 表达力字段 | `backend/app/schemas/script.py` SceneItem（action/location/time_of_day/int_ext/sfx/emotion）、DialogueItem（type/emotion/character_id）、LocationItem |
| 忠实度差异化（RD-01） | `backend/app/agents/script_pipeline.py:74,135,182,210,238-258` |
| 剧本子图 | `backend/app/agents/script_pipeline_graph.py:507,574,651,726,816,840,927,1048` |
| 管线接口全集 | `backend/app/api/script_pipeline.py:337,389,432,502,598,820,927,960,1049,1316,1363,1433,1546,1595,1907,1965,2028,2097` |
| 影响面预览（RD-08） | `backend/app/api/script.py:524` |
| 导出 | `backend/app/api/script.py:584`；`services/script_export_service.py:376,458`；`services/pdf_export_service.py` |
| 小说解析 | `backend/app/services/novel_parser.py:54,` + `/script/input/novel/parse`(`api/script.py:1106`) |
| 并发与乐观锁 | `backend/app/api/script_pipeline.py:1288,1644`；`backend/app/api/script.py:217` |
| 时长口径统一 | `backend/app/services/script_service.py:47-50,322` |
| 前端四步 | `mangaflow-web/src/views/studio/ScriptView.vue:19-22,121-123`；`components/script/{StepIdea,StepCharacters,StepEpisodesPlan,StepEpisodeBody}.vue` |
| 冒烟脚本 | `.run/smoke_script_pipeline.py`（843 行，含性能基线） |
| 回归测试 | `backend/tests/`（54 文件 / 342 用例，**342 passed in 728.43s**） |

---

*本报告为模块复核评估，具体修复排期请结合项目组资源与合规法务要求确认。*

*— 报告结束 —*
