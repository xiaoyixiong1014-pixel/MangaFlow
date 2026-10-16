# Checklist

## P1 三栏布局与减负
- [x] ScriptView.vue 为三栏布局（左参数栏/中三卡片/右助手面板），顶部通栏含版本历史与导出
- [x] el-steps 已移除，三卡片同屏且当前阶段卡片高亮（phase 映射 NULL/idea+characters→①、outline→②、body→③）
- [x] ParamSidebar 展示 genre/style/fidelity/aspect_ratio/target_duration、忠实度语义提示、预估消耗、阶段集数；「修改」暂 ElMessage 占位（路由表无独立项目设置页，既有结构限制，已在 tasks.md 记录）
- [x] StepIdea 双栏合并为单一可折叠输入区，大纲导航与场景卡已迁出，快速模式折叠为次要入口
- [x] 小屏 <1200px 三栏折叠单栏可用（媒体查询）
- [x] 所有交互区块具备 加载/空/错误 三态；mangaflow-web type-check 0 错误 + build 成功

## P2 故事设定
- [x] projects.story_setting JSONB 迁移 a3f7c2e9d4b1 可 upgrade/downgrade（独立库往返实测），init_db 同步，存量项目不受影响（nullable）
- [x] POST/GET/PUT setting + setting/regenerate 四接口全部带归属校验（越权 404）与阶段语义（body 409 锁定）
- [x] setting/generate 产出 一句话故事/核心冲突/世界观/人物小传，人物候选与现角色域数据一致（冒烟步骤 2.5 断言通过）
- [x] PUT setting 标记 source=user 并写 setting_edited 快照；regenerate 支持 instruction 且不回退已确认角色
- [x] StorySettingCard 四段展示/编辑 + 人物小传卡 + 确认推进（phase→outline）
- [x] 路径 A：?auto=1 自动触发生成并显示"AI 正在为您构建故事宇宙…"（router.replace 去参防重复触发）
- [x] 路径 B：解析进度展示；预计集数 > 阈值 10 时弹窗确认后才生成大纲
- [x] test_script_setting.py 7 用例全绿且既有回归零失败（全量 363 passed）

## P3 大纲与正文
- [x] POST episodes/{eid}/outline/regenerate 仅影响目标集（快照留痕、角色名单约束、body_exists 提醒、冒烟步骤 4.5 断言通过）
- [x] utils/manga-format.ts 将 scenes[] 渲染为漫剧规范文本（【场景】/动作/角色（情绪）：台词/type 标注）；`.run/test_manga_format.ts` 断言 4 组场景全过
- [x] EpisodeOutlineCard：集列表 + 编辑 + 单集重生成 + 增删集 + 确认推进 + 原大纲导航并入
- [x] ScriptBodyCard：SSE 流式 + 文章体/场次结构/漫剧格式三视图 + 按段编辑 + 体检面板 + 原场景卡并入
- [x] test_script_episodes_plan.py 回归零失败；type-check + build 成功

## P4 对话式共创（B 档）
- [x] POST script/assist 支持 scope=setting/episode_outline/episode_body，diff 预览不落库（冒烟断言预览后库值未变）
- [x] POST script/assist/apply 片段一致性校验防并发覆盖（不一致 skipped），applied=0 不递增版本不写快照
- [x] 每次采纳自动写版本快照（stage=assist_applied），版本列表可对比/回退（AssistantPanel 历史版本折叠区）
- [x] AssistantPanel 常驻对话区：diff 卡片红删绿增、采纳/拒绝/全部采纳、聚焦对象随中部卡片联动（focus-changed → assistFocus）
- [x] 三组快捷 Chips（剧情/台词/节奏）按聚焦对象过滤，点击即发送
- [x] 旧"本助手不会直接改写剧本"文案与抽屉代码完全清除（grep 复核仅剩文件头历史注释）
- [x] test_script_assist.py 8 用例全绿且 test_script_optimize.py 回归零失败

## P5 下游衔接
- [x] 确认正文 → 角色/场景对账 + 影响面预览 + 确认弹窗 → 一键分镜拆解（studioApi.decompose）→ 可选投递画布（deliverToCanvas），全链路 UI 串联完成（快速模式原出口未触碰）
- [x] 对账缺失项提供补建跳转（router.push studio-assets，不阻塞确认）

## 质量收尾
- [x] backend/docs/api-script-workspace.md 完整覆盖 7 个新接口（方法/路径/参数/响应/错误码/幂等口径）与既有接口变更点，字段与错误文案逐一与源码比对
- [x] 后端 pytest 全量回归零失败（363 passed / 872s）；双前端 type-check 0 错误 + build exit 0
- [x] 冒烟脚本扩展覆盖 故事设定→确认→单集重生成→assist 采纳→漫剧格式→拆解对账，**exit 0**
- [x] 性能基线记录达标：GET setting 8.6ms（<10ms）/ assist 预览 8.0ms（<15ms）/ SSE 首事件 12.9ms（<15ms），已回填 API 文档 §5
