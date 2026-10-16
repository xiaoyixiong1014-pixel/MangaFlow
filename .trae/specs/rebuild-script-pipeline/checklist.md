# Checklist

## A. P0 断链修复
- [x] 实体抽取数据源为 `scripts.content` 结构化剧本（novel 模式 raw_text 兜底），一句话模式下抽取结果覆盖剧本真实实体
- [x] `ScriptContent` Pydantic 模型存在且生成链路强制校验；校验失败携错重试 ≤3 次；耗尽后 SSE 显式 error、不落库 `{"raw"}` 半成品
- [x] 剧本生成全部经 `llm_adapter`（agent_name="script"），done 事件含用量归因（channel/model/tokens）；全仓无 `providers.llm.chat_stream` 直连残留
- [x] 生成中重复请求返回 409；版本乐观锁生效（并发保存冲突 409）

## B. P0 范式流水线
- [x] `projects.script_phase` 状态机（idea/characters/outline/body/NULL 快速模式）生效，跳步调用返回 409，存量数据（NULL）行为不回归
- [x] `characters` 表含 role_type/age/personality/motivation/arc/relations/confirmed_at，Alembic 迁移可 upgrade/downgrade，COMMENT 对齐
- [x] `episodes` 表含 logline/outline/confirmed_at
- [x] 角色设定 5 接口（generate/GET/PUT 批量编辑/{cid}/regenerate/confirm）全部可用；确认后写 characters + 快照 characters_confirmed + phase=outline
- [x] 分集大纲 4 接口（plan 生成/GET/{eid}/outline 编辑/confirm）全部可用；确认后写快照 outline_confirmed + phase=body
- [x] 逐集正文 3 接口（SSE 生成/保存/regenerate）全部可用；正文落 scripts（episode_id 绑定，uk 约束）；单集重生成不影响他集
- [x] 剧本子图三节点 + 三 interrupt 确认点 + Send 并行逐集 + PostgresSaver 检查点续跑；断线重连可恢复
- [x] AI 优化 `optimize` 返回片段化 diff 不落库；`optimize/apply` 按 accept_ids 部分应用、应用字段 source=ai_optimized；无整篇覆盖路径

## C. 表达力与口径
- [x] 场次 Schema 含 action/dialogue[].type/dialogue[].emotion/location/time_of_day/int_ext/sfx/dialogue[].character_id；顶层 locations[] 去重
- [x] 旧 Schema 数据向后兼容：storyboard 下游 unwrap_scenes 全量回归通过
- [x] 时长口径自洽（prompt 场次时长区间与默认目标匹配）；体检时长阈值 ±10%；对白字数÷语速交叉校验生效

## D. 决议对齐
- [x] 体检四条规则与 RD-02 一致（角色出场断层/对白过长/缺动作描写/缺高潮点），按集体检可定位，issue 点击跳转对应编辑位置
- [x] 场次级 source 落地：用户仅改部分场次保存后，改动场次 user、其余保持 ai；`_content_plain`/`unwrap_scenes` 兼容三种形态
- [x] 忠实度三档差异化提示词注入三个 Agent；faithful 模式有原著约束与角色名一致性校验

## E. 小说改编
- [x] txt/epub 上传解析出章节列表；章节可映射为分集大纲输入；生成/抽取输入不再 4000 字硬截断

## F. 交互收尾
- [x] 生成完成后无原始 JSON 残留展示（raw 收进"查看原文"抽屉）
- [x] RD-08 影响面预览：删除/修改实体前展示影响 N 资产/M 分镜
- [x] 剧本导出 TXT/Fountain 接口 + 前端按钮可用
- [x] 前端四步工作台：步骤条 + 角色卡页 + 分集大纲页 + 正文页（diff UI + 按集体检面板 + 单集重生成）；快速模式共存
- [x] vue-tsc type-check + build 通过（mangaflow-web）

## G. 文档与质量
- [x] `backend/docs/api-script-pipeline.md` 覆盖全部新/改接口（方法/路径/参数/响应/SSE 协议/错误码/幂等/阶段校验）+ 响应时间基线表
- [x] 新增测试文件全绿；既有 41 测试文件全量回归通过（pytest `-k "not real"`）
- [x] 四段式流水线端到端冒烟脚本（mock LLM）跑通并留存证据（.run/）
- [x] 新增/修改代码符合注释规范（文件级/类级/函数级/参数/返回值/异常中文注释）
- [x] 全部接口归属校验（非本人 404 不泄露存在性）、无越权路径
