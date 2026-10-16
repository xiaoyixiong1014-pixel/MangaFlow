# Tasks

> 编码规范（全部任务强制）：后端代码遵循项目既有注释规范——文件级/类级/函数级 docstring，含 Args/Returns/Raises 中文说明；前端关键逻辑加中文注释；RESTful 路由语义清晰、错误码规范（404 归属保护、409 冲突、400 参数、422 校验）。
> 前端设计纪律（design-taste-frontend）：延续项目既有 Element Plus 设计体系，圆角/主色/间距一致性锁定；每个交互区块必须补齐 加载 / 空 / 错误 三态；diff 卡片红删绿增；不引入新组件库、不做 AI-slop 装饰。
> **运行 pytest 的任务必须串行执行**（共享测试库 TRUNCATE 互锁教训）；纯编码任务可并行。

- [x] Task 1: P1 三栏布局与减负（纯前端，不依赖后端改动）
  - [x] 1.1 `ScriptView.vue` 重构：三栏栅格布局（左 260px 参数栏 / 中自适应工作区 / 右 360px 助手面板），顶部通栏（标题 + 版本历史抽屉入口 + 导出下拉 + 项目设置入口）；`el-steps` 移除，改为三卡片当前步高亮（`script_phase` 映射：NULL/idea+characters→①、outline→②、body→③）；小屏 <1200px 折叠单栏
  - [x] 1.2 `ParamSidebar.vue`：只读展示 `ProjectOut` 的 genre/style/fidelity/aspect_ratio/target_duration + 忠实度三档语义提示文案 + 预估消耗（复用/静态按集数×单价估算）+ 阶段集数（`pipeline/status`）；「修改」跳项目设置（路由表无独立设置页，暂 ElMessage 占位）
  - [x] 1.3 `AssistantPanel.vue` 骨架：常驻面板壳（标题、聚焦对象显示、对话滚动区占位、输入框禁用态），Task 6 填充完整能力
  - [x] 1.4 StepIdea 减负：双栏合并为单一可折叠输入区；大纲导航块迁出（暂存待 Task 4 并入大纲卡片）；场景卡块迁出（待 Task 4 并入正文卡片）；工具行上移顶部通栏（版本历史抽屉封装为 VersionHistoryDrawer.vue）；快速模式开关折叠为次要入口；AI 助手抽屉代码移除（面板替代）
  - [x] 1.5 vue-tsc type-check 0 错误 + build 成功；三态（加载骨架/占位空态/请求错误提示）已实现
- [x] Task 2: P2 后端故事设定（模型 + Agent + 4 接口 + 测试）
  - [x] 2.1 模型与迁移：`projects.story_setting` JSONB（nullable，结构 `{logline, core_conflict, worldview, source, updated_at}`）+ Alembic 迁移 a3f7c2e9d4b1（down_revision=c7d9e1f3a5b7，upgrade/downgrade 往返实测通过）+ `init_db` 同步
  - [x] 2.2 `setting_planner` Agent：build_setting_planner_messages（体裁/风格/画幅 + RD-01 忠实度注入 + 创意截 6000 字）+ `plan_story_setting`（agent_name="setting_planner"，重试 2 次温度 0.8→1.0→1.0，失败 502；人物复用 `_normalize_character_candidates`）
  - [x] 2.3 接口：POST setting/generate（设定段落 story_setting source=ai + 人物候选入 config + NULL/idea→characters 特例）、GET setting（未生成返回空对象）、PUT setting（source=user + setting_edited 快照）、POST setting/regenerate（已确认角色不回退）；全部 get_owned_project 404 归属；phase=body 409 锁定
  - [x] 2.4 测试：test_script_setting.py 7 用例 + 回归 test_script_characters/test_script_phase 共 30 passed
- [x] Task 3: P2 前端故事设定卡 + 分流自动加载
  - [x] 3.1 `StorySettingCard.vue`：设定三段展示/段级行内编辑（PUT 单字段，source=user 角标）+ 人物小传区（移植 StepCharacters 网格/编辑弹窗/单角色重生成/删除/手动新增）+「重新生成」（prompt 收集 instruction）+「确认故事设定，进入分集大纲」（flush 编辑 → confirmCharacters → phase→outline）+「下一步」
  - [x] 3.2 分流自动加载：`?auto=1` 且无 setting 且 phase NULL/idea 时 router.replace 去参后自动 generate + "AI 正在为您构建故事宇宙…"骨架；路径 B 在 StepEpisodesPlan（EPISODE_CONFIRM_THRESHOLD=10，按章节数推定集数超阈值弹窗确认）；NewProjectDialog 按 inputTab 分流跳转（idea→?auto=1）
  - [x] 3.3 ScriptView 装配：卡片①渲染 StorySettingCard，标题栏 popconfirm「快速模式」开启切回 StepIdea 原全链路；StepCharacters 退役不渲染（文件保留）；keep-alive/流式中止保持
  - [x] 3.4 type-check 0 错误 + build 成功
- [x] Task 4: P3 单集大纲重生成 + 漫剧格式渲染 + 卡片化改造
  - [x] 4.1 后端 `POST /script/episodes/{eid}/outline/regenerate`（build_episode_outline_regen_messages 复用 plan_episodes 骨架 + 前后集钩子 + instruction + 角色强约束 + 忠实度挂点；plan_episode_outline_regen 3 次重试 502；仅更新该集 + 快照 episode_outline_regenerated 留痕；body_exists 提醒）+ test_script_outline_regen.py 6 用例
  - [x] 4.2 `utils/manga-format.ts`：renderMangaScene/renderMangaScript 纯函数（【场景】/action+sfx/对白四类标注/character_id 回填角色名/空值跳过）；无 vitest 不引新依赖，`.run/test_manga_format.ts` 经 npx tsx 断言 4 组场景全过
  - [x] 4.3 `EpisodeOutlineCard.vue`：保留集列表/编辑/增删/确认推进/body 锁定 + 每集「AI 重生成」+ 大纲导航迁入 + scriptPipeline.ts 增 regenerateEpisodeOutline
  - [x] 4.4 `ScriptBodyCard.vue`：保留 SSE/双视图/保存/单集重生成/优化 diff/体检 + 扩三视图（文章体/场次结构/漫剧格式只读+复制）+ 场景卡内容并入场次结构视图 + 体检 issue 定位锚点恢复（body-scene-{no}，v-if 互斥防重复 id）
  - [x] 4.5 type-check + build 成功；pytest（outline_regen+episodes_plan 串行）14 passed
- [x] Task 5: P4 对话式改写后端（assist 双接口）
  - [x] 5.1 `POST /script/assist`：`{scope, target?, instruction(1~500字必填)}` → `{scope,target,changes:[{id,path,kind,original,proposed,reason,diff_lines}],count,summary}` 不落库；build_assist_messages 复用 optimize「仅变更片段」约束 + ASSIST_PATH_WHITELIST 共用白名单；setting 守卫（空 409/body 409）、episode_outline 对齐单集重生成口径、episode_body 内部转发 optimize_episode_content
  - [x] 5.2 `POST /script/assist/apply`：accepts 片段 path 白名单 + original 一致性校验（不一致 skipped 防并发覆盖）→ 按 scope 落库（setting source=user / outline JSONB / body 复用 optimize 落库 + 乐观锁 409）→ applied>0 写 assist_applied 快照；applied=0 不落库不递增
  - [x] 5.3 测试：test_script_assist.py 8 用例 + 回归 test_script_optimize/test_script_setting，串行 23 passed
- [x] Task 7: P5 下游格式化拆解串联
  - [x] 7.1 StepEpisodeBody「确认剧本，生成资产」三段式弹窗：角色对账（正文 dialogue.character vs confirmed 角色，缺失红色 tag + 补建跳转不阻塞）+ 场景对账（locations vs 场景资产）+ 影响面预览（entityImpact 汇总 N 资产/M 分镜 + 明细）；确认 → studioApi.decompose 整剧拆解 → confirm 投递 deliverToCanvas；快速模式原出口未触碰
  - [x] 7.2 type-check 0 错误 + build 成功；弹窗三态（骨架/alert 重试/空态）实现
- [x] Task 6: P4 前端助手面板 + Chips + 版本对比
  - [x] 6.1 `AssistantPanel.vue` 完整实现：对话流（user/assistant/system）→ assistPreview → diff 回执卡片（红删绿增沿用 StepEpisodeBody 样式口径、复选框默认全选、reason 次文案）+「采纳/拒绝/全部采纳」→ assistApply → 系统消息 + emit applied → ScriptView 按 scope 调对应卡片 reload（正文卡生成中自动跳过防打断流式）；三态完整（内联 loading/空态引导/可重试错误）；聚焦联动（卡片 focus-changed → outlineFocus/bodyFocus → assistFocus computed，未选集发送前置拦截）
  - [x] 6.2 快捷 Chips 三组（方案 §6.2 原文文案）按 scope 过滤（setting→剧情 / outline→剧情+节奏 / body→全三组），点击直接发送
  - [x] 6.3 历史版本折叠区：最近 10 条快照（stage 中文化）+ 回退（confirm→rollback→emit applied 刷新）+ 对比弹窗（复用现有摘要数据不新增协议）；采纳后自动刷新
  - [x] 6.4 残留文案 grep 复核（仅文件头历史注释，UI 无残留）；type-check 0 错误 + build 成功
- [x] Task 8: 质量收尾（主控执行，pytest 串行）
  - [x] 8.1 `backend/docs/api-script-workspace.md`：§0 概述（story_setting 结构 + 快照 stage 表）→ §1~§3 七接口（参数表/响应示例/错误码真实文案/幂等与阶段口径/白名单表）→ §4 既有接口行为变更与前端改造说明 → §6 证据索引（文件:行号）；字段与错误文案逐一与源码比对
  - [x] 8.2 全量测试：后端 pytest `-k "not real"` **363 passed（872s）零失败**（基线 342 + 新增 21：setting 7 / outline_regen 6 / assist 8）
  - [x] 8.3 双前端：mangaflow-web type-check 0 错误 + build exit 0；mangaflow-admin-web type-check 0 错误 + build exit 0
  - [x] 8.4 冒烟：`.run/smoke_script_pipeline.py` 扩展（步骤 2.5 设定域 / 4.5 单集重生成 + 锁定负向断言 / 5.5 assist 正文 / 5.6 漫剧渲染契约）**exit 0**；期间修复桩数据 emotion_curve 含"→"导致正文分批翻倍的断言失配
  - [x] 8.5 性能基线：GET setting 8.6ms / assist 预览 8.0ms / SSE 首事件 12.9ms，全部达标并回填 API 文档 §5
  - [x] 8.6 checklist.md 逐项核验勾选

# Task Dependencies
- Task 1 纯前端可立即开工（P1 减负优先，直接回应用户冗余诉求）
- Task 2（后端故事设定）→ Task 3（前端卡片 + 分流）；Task 3 依赖 Task 1 的三栏骨架（卡片装配进中部工作区）
- Task 4.1（后端单集重生成）与 Task 2 可并行；Task 4.2~4.4 前端依赖 Task 1 骨架
- Task 5（assist 后端）依赖 Task 2 的 story_setting 落库形态（scope=setting 需要它）与既有 optimize 机制；Task 6 依赖 Task 5 + Task 1.3 骨架
- Task 7 依赖 Task 4.4（正文卡片）与既有拆解/画布接口
- Task 8 最后执行；**凡运行 pytest 的任务必须串行**
