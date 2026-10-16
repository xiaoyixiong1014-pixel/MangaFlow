# 剧本创作工作台改造 API 文档（redesign-script-workspace）

> 本文档描述「剧本创作工作台改造」新增的 **7 个接口**与对既有接口 / 前端流程的
> 行为变更。字段名 / 必填性 / 默认值 / 错误码均以源码为准（核实基准：
> `app/api/script_pipeline.py`、`app/schemas/script_pipeline.py`、
> `app/agents/script_pipeline.py`、`app/services/script_service.py`，2026-10-14）。
>
> **阅读前提**：阶段状态机（`idea → characters → outline → body`）、推进规则
> （一格校验 / 起点特例 / 幂等保持）、统一错误码约定（401/404/409/400/422/502/500）
> 与鉴权方式（`Authorization: Bearer <access_token>` + 项目归属校验，他人项目
> 一律 404）**全部沿用基线文档**
> [`api-script-pipeline.md`](./api-script-pipeline.md) §0.2~0.4，本文不再重复。
> 快速模式既有链路见 [`api-creation-chain.md`](./api-creation-chain.md)。
> 本文 7 接口均为普通 REST（无 SSE 端点），SSE 事件协议见基线文档 §3.1 / §5.1。

---

## 0. 概述与阅读前提

### 0.1 改造范围

本次改造（D2 决策：四步合并为三步工作台）新增三块后端能力：

| 域 | 新增接口 | 作用 |
| --- | --- | --- |
| 故事设定 | `POST /script/setting/generate`、`GET /script/setting`、`PUT /script/setting`、`POST /script/setting/regenerate` | 「一句话故事 / 核心冲突 / 世界观」三段设定产物（人物小传复用角色域数据） |
| 分集大纲 | `POST /script/episodes/{eid}/outline/regenerate` | 单集大纲 AI 重生成（作用域收窄到 1 集），响应携带 `body_exists` 提醒 |
| 分集大纲 | `POST /script/episodes/{eid}/outline/autofill` | 单集大纲 AI 一键填充（依据故事设定/角色设定/已填内容补全空缺字段；**建议值不落库**），响应携带 `filled_fields` |
| 对话协作 | `POST /script/assist`、`POST /script/assist/apply` | 对话式改写预览（不落库）+ 片段采纳落库（快照留痕） |

### 0.2 story_setting 数据结构

故事设定存 `projects.story_setting` JSONB（nullable，存量项目不受影响），
落库结构（`_build_setting_payload`，`app/api/script_pipeline.py:729`）：

```json
{
  "logline": "两个少女约定一起成为漫画家",
  "core_conflict": "天赋与家庭反对之间的拉扯",
  "worldview": "现代都市，漫画新人奖体系",
  "source": "ai",
  "updated_at": "2026-10-14T08:00:00+00:00"
}
```

- `source`：`"ai"`（生成 / 重生成）或 `"user"`（编辑 / assist 采纳）；NULL（JSONB
  为空）= 尚未生成；
- `updated_at`：当前 UTC 时间的 ISO 8601 字符串，生成 / 编辑 / 采纳时刷新；
- 设定三段截长上限：logline 200 / core_conflict 1000 / worldview 2000 字符
  （`_normalize_story_setting`，`app/agents/script_pipeline.py:1675`）；
- 人物部分**不入 story_setting**：候选存 `config["script_characters"]`（与角色域
  同构），已入池存 `characters` 表，`GET /script/setting` 双组并列返回。

### 0.3 快照 stage 取值（版本历史留痕）

| stage | 触发接口 |
| --- | --- |
| `setting_generated` | `POST /script/setting/generate`、`POST /script/setting/regenerate` |
| `setting_edited` | `PUT /script/setting` |
| `episode_outline_regenerated` | `POST /script/episodes/{eid}/outline/regenerate` |
| `assist_applied` | `POST /script/assist/apply`（applied>0 时写一条） |

---

## 1. 故事设定域（4 接口）

阶段语义沿用基线文档 §0.3 的 `characters` 取值（D4 决策：`script_phase` 取值
不变，UI 改称「故事设定」）。

### 1.1 生成故事设定（三段 + 人物候选）

```
POST /api/projects/{project_id}/script/setting/generate
```

LLM 生成故事设定三段落 `story_setting`（`source="ai"`），人物候选写入
`config["script_characters"]`（与角色域 generate 的候选结构完全一致）。

**无请求体**。

前置校验（按序）：项目归属（404）→ `phase=body` 拒绝（409「正文已开始生成，
故事设定已锁定」）→ 剧本已有输入（`raw_text` 非空或结构化 content 有实质
payload，否则 409「请先保存剧本输入」）。

成功响应 `200`（`StorySettingGenerateOut`）：

```json
{
  "setting": {
    "logline": "两个少女约定一起成为漫画家",
    "core_conflict": "天赋与家庭反对之间的拉扯",
    "worldview": "现代都市，漫画新人奖体系",
    "source": "ai",
    "updated_at": "2026-10-14T08:00:00+00:00"
  },
  "characters": [
    {
      "id": null, "candidate_id": "1887b3ea", "name": "林小满",
      "role_type": "lead", "age": "约17岁",
      "personality": "倔强善良，遇挫不折", "appearance": "短发少女，戴红色发卡",
      "motivation": "和挚友一起成为漫画家", "arc": "从畏缩退让到坚定追梦",
      "relations": [{"target": "陈默", "relation": "漫画搭档"}],
      "confirmed_at": null, "source": "planned"
    }
  ],
  "phase": "characters"
}
```

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目 |
| 409 | `phase=body`（「正文已开始生成，故事设定已锁定」）；或项目尚未保存任何剧本输入（「请先保存剧本输入」） |
| 502 | LLM 生成与解析多次失败（「故事设定生成失败，请重试」） |

**幂等与阶段口径**：**覆盖式**（设定三段整体重写、候选**全量刷新**，非增量）。
源文本取 `script.raw_text` 优先，无则取结构化剧本纯文本（`build_extract_source_text`）。
`plan_story_setting` 内部最多 3 次尝试（温度 0.8 → 1.0 → 1.0），设定三段任一为
空视为本轮失败；人物候选可能为空（不作失败条件）。阶段为 `NULL`（快速模式）/
`idea` 时直接置 `characters`（流水线起点特例，不经一格校验）；`characters` 及
以后**不回退**。

### 1.2 故事设定详情（设定可空 + 人物双组）

```
GET /api/projects/{project_id}/script/setting
```

无请求参数。成功响应 `200`（`StorySettingDetailOut`）：

```json
{
  "setting": { "logline": "…", "core_conflict": "…", "worldview": "…", "source": "ai", "updated_at": "…" },
  "planned":   [ /* source=planned 的候选档案（未生成过为空） */ ],
  "confirmed": [ /* source=confirmed 的已入池档案 */ ],
  "phase": "characters"
}
```

**未生成过设定时 `setting` 为 `null`（不 404）**，便于前端渲染空态。
`confirmed` 仅返回 `deleted_at IS NULL` 且 `confirmed_at IS NOT NULL` 的角色，
按 `id` 升序（复用角色域 list 口径）。

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目 |

### 1.3 编辑故事设定（合并保存）

```
PUT /api/projects/{project_id}/script/setting
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| logline | body | string\|null | 否 | null | 一句话故事（1~200 字符；None=不修改） |
| core_conflict | body | string\|null | 否 | null | 核心冲突描述（1~1000 字符；None=不修改） |
| worldview | body | string\|null | 否 | null | 世界观设定（1~2000 字符；None=不修改） |

> 三字段**至少传入一个**（模型校验器，全 None → 422）；请求体必填（无整体
> 缺省）。从未生成过设定的项目亦允许直接编辑保存（用户手动撰写设定）。

成功响应 `200`（`StorySettingEditOut`）：

```json
{
  "setting": { "logline": "用户改写后的一句话故事", "core_conflict": "…", "worldview": "…", "source": "user", "updated_at": "…" },
  "phase": "characters"
}
```

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目 |
| 422 | 请求体校验失败（空 body / 空串 / 三字段全 None / 超长） |

**幂等与阶段口径**：合并保存——仅覆盖传入字段（`None` = 沿用当前值）；
`source` **强制置 `"user"`** 并刷新 `updated_at`；写 `setting_edited` 版本快照；
**不改变阶段**（任何阶段可编辑，含 `body`——body 阶段的锁定仅约束 AI 生成 /
重生成 / assist 改写，手动编辑保存不在此列）。

### 1.4 重生成故事设定（已确认角色不回退）

```
POST /api/projects/{project_id}/script/setting/regenerate
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| instruction | body | string\|null | 否 | null | 本轮重生成的调整方向（≤2000 字符） |

> 请求体整体可缺省（`req: Optional[...]`）。

成功响应 `200`：`StorySettingGenerateOut`（同 1.1 结构；`characters` 为合并后的
候选列表）。

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目 |
| 409 | `phase=body`（「正文已开始生成，故事设定已锁定」） |
| 502 | LLM 生成与解析多次失败（「故事设定生成失败，请重试」） |

**幂等与阶段口径**：

- **指令组装**：取当前 `logline` 拼「当前一句话故事：…」+ 传入 `instruction`
  拼「调整要求：…」（分号连接）注入 LLM；源文本允许为空（**不要求**剧本已有
  输入，与 1.1 的前置校验不同）；
- **候选合并**（非覆盖）：`config` 候选中 `confirmed=True` 的**原样保留**（临时
  id 不变）；新候选以新 8 位 hex 临时 id 追加（`confirmed=False`）；**与已确认
  同名的新候选丢弃**（避免重复）；已入池 `characters` 表**不回退**；
- 阶段**不回退**（含 NULL/idea 也不前移，仅生成设定与候选）；写
  `setting_generated` 快照（summary 携带 `confirmed_kept` / `fresh_candidates`）。

---

## 2. 单集大纲重生成

### 2.1 单集大纲 AI 重生成（含 body_exists 提醒）

```
POST /api/projects/{project_id}/script/episodes/{eid}/outline/regenerate
```

仅重写指定一集的大纲（标题 / 一句话梗概 / 核心事件 / 出场角色 / 情绪走向 /
时长），复用 `plan_episodes` 提示词骨架与 `_normalize_episode_candidates`
规整校验（角色名单按已确认角色过滤，LLM 新造名单外角色剔除），**其余集不受
影响、阶段不变**。

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| eid | path | int | 是 | - | 剧集 ID（须属于该项目） |
| instruction | body | string\|null | 否 | null | 本轮重生成的调整方向（≤500 字符） |

> 请求体整体可缺省（`req: Optional[...]`）。

成功响应 `200`（`EpisodeOutlineRegenerateOut`）：

```json
{
  "episode_id": 2, "sequence": 2, "title": "第二集：落选之后",
  "logline": "两人落选后互相打气，决定再战",
  "core_event": "落选夜的天台谈心",
  "characters": ["林小满", "陈默"],
  "emotion_curve": "低谷→回暖", "duration_sec": 180,
  "confirmed_at": null,
  "body_exists": false
}
```

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目；或剧集不存在 / 不属于该项目（「剧集不存在」） |
| 409 | 阶段未达 outline（「请先进入分集大纲阶段（确认角色设定并生成分集大纲）」）；或该集大纲未生成（「请先生成分集大纲」）；或无已确认角色（「请先确认角色设定」） |
| 502 | LLM 生成与解析多次失败（「单集大纲重生成失败，请重试」） |

**幂等与阶段口径**：

- **阶段守卫对齐 plan 锁定口径的例外**：`phase ∈ {outline, body}` 均可用
  （`body` 阶段整卷 `plan` 拒绝，但**单集保持可重写**）；未达 outline 一律 409；
- **落库范围**：仅更新该集 `title` / `logline` 列与 `outline` JSONB；LLM 返回的
  空值字段沿用旧值兜底（logline / core_event / emotion_curve / duration_sec）；
  前后集钩子上下文按 `sequence` 相邻集注入（第 1 集 / 最后一集对应侧为 None）；
- **`body_exists` 语义**：该集 `scripts` 行存在即 `true`——**仅提醒**「大纲变更
  后正文将不一致，请按需重生成正文」，**不自动删除正文**，也不阻断落库；
- 写 `episode_outline_regenerated` 版本快照（episodes 行无 version 列，版本历史
  以快照 +1 记账）。

### 2.2 单集大纲 AI 一键填充（建议值，不落库）

```
POST /api/projects/{project_id}/script/episodes/{eid}/outline/autofill
```

依据**故事设定**（`projects.story_setting`）、**已确认角色设定**、**本集已填
写内容**与前后集钩子，把该集大纲中**未填写的字段补全**；已填写字段原样返回。
与 2.1 的差异：本接口**只出建议不落库、不打版本快照**——前端把建议值回填
编辑表单，用户确认调整（或经 AI 助手改写）后走既有 `PUT outline` 保存，
留痕发生在保存侧。

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| eid | path | int | 是 | 剧集 ID（须属于该项目；该集大纲须已生成） |

请求体：无。

成功响应 `200`（`EpisodeOutlineAutofillOut`）：

```json
{
  "sequence": 1,
  "title": "摸鱼一分钟，战力九万九",
  "logline": "社畜王小满绑定摸鱼系统…",
  "core_event": "加班濒死时的系统绑定",
  "characters": ["王小满", "系统老头", "赵总"],
  "emotion_curve": "压抑→爆发→释然",
  "duration_sec": 180,
  "filled_fields": ["核心事件", "情绪走向"]
}
```

| 字段 | 说明 |
| --- | --- |
| sequence / title / logline / core_event / characters / emotion_curve / duration_sec | 补全后的单集大纲六字段（已填字段原样返回，缺失字段为 AI 建议） |
| filled_fields | 本次被 AI 填充的字段名清单（对比填充前为空的字段），供前端 toast 提示 |

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目；或剧集不存在 / 不属于该项目 |
| 409 | 阶段未达 outline；或该集大纲未生成（空集应走 plan 生成而非填充）；或无已确认角色 |
| 502 | LLM 生成与解析多次失败（「AI 一键填充失败，请重试」） |

**实现要点**：提示词逐字段标注「已填写/未填写」并要求已填字段一字不改；
复用 regen 的 3 次重试骨架（温度 0.8→1.0→1.0）与名单外角色过滤；
`agent_name=episode_planner` 计量。

---

## 3. 对话协作域（2 接口）

对话式改写通道：预览（不落库）→ 用户挑选 → 采纳落库（快照留痕）。正文域
（`episode_body` 作用域）**直接复用**优化域（基线文档 §4）的预览与落库机制，
不重造协议。

### 3.1 path 白名单（ASSIST_PATH_WHITELIST）

白名单按 scope 划分（`app/agents/script_pipeline.py:1785`，预览提示词约束与
apply 落库校验共用同一事实来源）：

| scope | 可改写字段（path） | 作用对象 |
| --- | --- | --- |
| `setting` | `logline` / `core_conflict` / `worldview` | `projects.story_setting` 三段文本 |
| `episode_outline` | `title` / `logline` / `core_event` / `characters` / `emotion_curve` | 该集 `title` / `logline` 列 + `outline` JSONB 文本字段 |
| `episode_body` | 不适用（转发 optimize 通道） | 结构化正文（复用 optimize 的 `scenes[i].xxx` 字段路径体系） |

`characters` 名单口径：payload 以**顿号（、）连接**为展示文本参与 diff；apply
时按顿号拆回名单（`duration_sec` 为整数字段，不参与文本 diff，不在白名单内）。

### 3.2 对话式改写预览（不落库）

```
POST /api/projects/{project_id}/script/assist
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| scope | body | string | 是 | - | 改写作用域：`setting` / `episode_outline` / `episode_body` |
| target | body | int\|string\|null | 视 scope | null | `setting` 忽略（可省略或传 `"all"`）；`episode_outline` / `episode_body` **必须为剧集 ID 整数**（否则 422；bool 亦拒绝） |
| instruction | body | string | 是 | - | 用户自然语言改写指令（1~500 字符） |

成功响应 `200`（`AssistPreviewOut`）：

```json
{
  "scope": "setting", "target": "all",
  "changes": [
    {
      "id": "f6b6f7240aed", "path": "logline", "kind": "field",
      "original": "两个少女约定一起成为漫画家",
      "proposed": "落选的少女与挚友约定，一年内拿下新人奖金奖。",
      "reason": "加入挫折起点，增强弧光",
      "diff_lines": [{"op": "equal", "text": "…"}, {"op": "delete", "text": "…"}, {"op": "insert", "text": "…"}]
    }
  ],
  "count": 1,
  "summary": "把 logline 改为带挫折起点的版本"
}
```

各 scope 的守卫（按代码分支顺序）：

| scope | 守卫链 | 错误 |
| --- | --- | --- |
| `setting` | 归属 404 → `phase=body` 拒绝 → 设定未生成（或三段全空）拒绝 | 409「正文已开始生成，故事设定已锁定」；409「请先生成故事设定」 |
| `episode_outline` | 归属 404 → 阶段 ∈ {outline, body} → 剧集归属 → 该集大纲已生成 | 409「请先进入分集大纲阶段（确认角色设定并生成分集大纲）」；404「剧集不存在」；409「请先生成分集大纲」 |
| `episode_body` | 归属 404 → 剧集归属 → `phase=body` → 该集已有正文 | 404「剧集不存在」；409「请先确认分集大纲」；**400**「该集尚未生成正文」 |

| 错误码 | 触发条件 |
| --- | --- |
| 400 | `episode_body` 作用域该集无正文 |
| 404 | 项目不存在 / 非本人项目；或剧集不存在 / 不归属 |
| 409 | 见上表（setting 已锁定或未生成 / 阶段未达大纲 / 该集大纲未生成 / 阶段未达 body） |
| 422 | scope / target / instruction 校验失败（scope 非法取值；episode 作用域 target 缺失或非整数；instruction 空 / 超 500 字） |
| 502 | 改写 LLM 多次失败（「对话改写失败，请重试」） |

**幂等与阶段口径**：

- **只读预览，不落库、不写快照、不改阶段**；
- `episode_body` 内部转发 `optimize_episode_content`（`grain=global` 全篇、由
  LLM 自定范围），行为与基线文档 §4.1 完全一致；`setting` / `episode_outline`
  走 `assist_rewrite`（`agent_name="script_assist"`、json_mode、temperature 0.5、
  最多 3 次尝试）；
- **片段 id 口径**（与 optimize 片段一致）：`id = sha1(f"{path}\x00{original}\x00{proposed}")`
  前 12 位 hex（`_change_id`，`app/services/script_service.py:807`；同
  path/original/proposed 幂等一致，供 apply 双侧匹配）；
- `original` 一律以**当前库值**覆盖（LLM 输出的原文字段不可信）；非法 path
  在服务端丢弃，故 `count` 可能少于 LLM 输出条数；`summary` 截 200 字符
  （`episode_body` 作用域固定空串）；
- 预览响应的 `target` 为规范化标识：`setting` 固定 `"all"`；episode 两作用域为
  剧集 ID 字符串（`str(episode.id)`）。

### 3.3 采纳改写片段（落库 + 快照）

```
POST /api/projects/{project_id}/script/assist/apply
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| scope | body | string | 是 | - | 改写作用域（与 3.2 一致） |
| target | body | int\|string\|null | 视 scope | null | 语义同 3.2 |
| accepts | body | OptimizeApplyChangeItem[] | 否 | [] | 采纳的片段列表（元素含 `id` / `path` / `original` / `proposed`） |
| base_version | body | int\|null | 否 | null | 乐观锁基准版本号（≥1；**仅 `episode_body` 作用域消费**，不匹配 409） |

成功响应 `200`（`AssistApplyOut`）：

```json
{
  "applied": 1, "skipped": 0,
  "snapshot_id": 42,
  "version": null,
  "phase": "characters",
  "content": null
}
```

> `version` / `content` 仅 `episode_body` 作用域携带（应用后的剧本版本号与结构化
> content）；`setting` / `episode_outline` 作用域固定为 `null`。`snapshot_id`
> 为本次写入的 `assist_applied` 快照 ID（`applied=0` 时为 `null`）。

**逐片段校验与 skipped 计数**（三个 scope 一致）：

1. `path` 不在该 scope 白名单 → `skipped += 1`；
2. `proposed` 为空（strip 后）→ `skipped += 1`；
3. `original` 与**当前库值**不一致（并发已改）→ `skipped += 1`，跳过防覆盖
   （列表值以顿号连接后比较）。

**各 scope 落库口径**：

- `setting`：更新 `story_setting` 对应字段（截长 logline 200 / core_conflict
  1000 / worldview 2000）；用户采纳 AI 建议语义上属「确认」动作，
  `source` 置 **`"user"`**（与手动编辑保存同口径）并刷新 `updated_at`；
- `episode_outline`：更新该集 `title` / `logline` 列与 `outline` JSONB
  （`characters` 按顿号拆回名单；文本截长 title 100 / logline 200 /
  core_event 2000 / emotion_curve 100，与列宽对齐）；
- `episode_body`：复用 `apply_optimize_changes`（path 白名单 + `original` 校验 +
  被改动场次元素 `source="ai_optimized"` 重写），乐观锁
  `UPDATE ... WHERE version=旧值`（冲突 rollback 后 409「剧本已被其他操作修改，
  请刷新」）。

**幂等与阶段口径**：

- **`applied=0` 时不落库、不写快照**（`episode_body` 亦不递增版本，返回当前
  `version` 与 `content`；setting / outline 作用域返回 `version=null`、
  `content=null`）——幂等重放安全；
- `applied>0` 时写**一条** `stage=assist_applied` 版本快照（summary 携带 scope /
  target / applied / paths）；
- **assist 采纳不改变项目阶段**（`phase` 原样返回）；
- `base_version` 可选：仅 `episode_body` 作用域校验，与当前 `scripts.version`
  不一致 → 409「剧本已被其他操作修改，请刷新」（未传兼容旧客户端）。

| 错误码 | 触发条件 |
| --- | --- |
| 400 | `episode_body` 作用域该集无正文 |
| 404 | 项目不存在 / 非本人项目；或剧集不存在 / 不归属 |
| 409 | setting 已锁定（「正文已开始生成，故事设定已锁定」）或未生成（「请先生成故事设定」）；阶段未达大纲；该集大纲未生成；阶段未达 body；`base_version` 不匹配；或落库乐观锁冲突（「剧本已被其他操作修改，请刷新」） |
| 422 | scope / target 校验失败（同 3.2） |

---

## 4. 既有接口行为变更与前端改造说明

本文档新增 7 接口**均不改动**基线 22 接口的出入参；以下为三步工作台（D2）
下的行为映射与前端改造口径。

### 4.1 三步合并映射（D2）

前端三卡片同屏排列（`el-steps` 移除），与后端阶段取值的映射（D4：`script_phase`
取值**保持 `characters` 不变**，零数据迁移，UI 改称「故事设定」）：

| 前端卡片（新） | 替代旧步骤 | 后端阶段值 | 主用接口 |
| --- | --- | --- | --- |
| StorySettingCard 故事设定 | StepIdea（输入区并入，减负）+ StepCharacters | `characters` | 本文档 §1 四接口 + 既有 `PUT /script/input`、角色域 5 接口 |
| EpisodeOutlineCard 分集大纲 | StepEpisodesPlan | `outline` | 基线 §2.1~2.4 + 本文 §2.1 |
| ScriptBodyCard 剧本正文 | StepEpisodeBody | `body` | 基线 §3 三接口 + §4 优化域 + 本文 §3（episode_body 作用域） |

- 输入区（`PUT /script/input`、`POST /script/input/novel/parse`）行为不变，
  位置移入故事设定卡片的可折叠输入区；
- 大纲导航 / 场景卡分别迁入分集大纲卡与剧本正文卡；工具行（版本历史 / 导出）
  迁至顶部通栏（纯前端布局，接口不变）。

### 4.2 `?auto=1` 分流（路由参数，纯前端行为）

新建项目跳转剧本页携带 `?auto=1`（路径 A：一句话创意）时，前端**自动调用**
`POST /script/setting/generate`（无需用户点「开始生成」），期间展示「AI 正在为
您构建故事宇宙…」文案，完成后停留故事设定卡等待确认 / 编辑。后端无新协议、
无新参数——分流仅由前端触发时机体现；后端侧该调用的幂等口径即 §1.1
（覆盖式重出设定 + 候选全量刷新）。

### 4.3 AssistantPanel 替代旧抽屉

AI 助手由抽屉式「仅出建议」升级为右侧常驻 `AssistantPanel`（三栏布局右栏），
抽屉交互移除，「本助手不会直接改写剧本」旧文案删除。聚焦对象（故事设定 /
某集大纲 / 正文）跟随中部卡片选中态切换，对应 `POST /script/assist` 的
`scope` 取值；修改回执以 diff 卡片呈现（红删绿增，`diff_lines` 渲染），采纳走
`POST /script/assist/apply`（每次采纳自动打版本点 = `assist_applied` 快照；
历史版本对比与回退复用既有 snapshots / rollback 接口）。预设快捷 Chips
（剧情 / 台词 / 节奏）为**预置 instruction**，走同一通道，无新增协议。

### 4.4 漫剧格式渲染（纯前端）

`utils/manga-format.ts` 将结构化 `scenes[]`（action / location / time_of_day /
int_ext / sfx / emotion + dialogue[].type / emotion / character_id）渲染为漫剧
规范文本视图（【场景：…】+ 动作行 + 角色（情绪）：台词），与「文章体 /
场次结构」视图并列。**渲染为纯前端行为**，不新增后端接口、不改动正文数据
结构。

---

## 5. 性能基线

> 测量方式沿用基线文档 §9.1（`httpx.ASGITransport` 进程内直连、LLM 全 mock、
> 每接口串行 3 次取中位数）；实测数据由 `.run/smoke_script_pipeline.py` 性能段
> 输出（2026-10-14 本机 dev 口径，1 项目 / 1 集 / 2 场次规模）。

| 接口 | 中位耗时 | 样本 | 达标线 | 结论 |
| --- | --- | --- | --- | --- |
| `GET /api/projects/{id}/script/setting` | **8.6ms** | [8.6, 8.4, 8.6] | <10ms | ✅（含 story_setting 解析 + 候选/已入池双组装配） |
| `POST /api/projects/{id}/script/assist`（预览） | **8.0ms** | [8.0, 8.1, 6.9] | <15ms | ✅（mock LLM，含 `assist_rewrite` + `build_field_diffs`；setting 作用域在 body 阶段被锁，测的是同通道 episode_outline 预览） |
| SSE 首事件（正文生成流） | **12.9ms** | [13.9, 12.3, 12.9] | <15ms | ✅（`POST .../episodes/{eid}/body/generate/stream` 首字节） |

对照参考（同轮实测的既有接口）：项目列表 8.1ms / 分集大纲列表 8.6ms / 按集体检 10.8ms / TXT 导出 7.7ms / 实体影响面 10.8ms——新增接口与既有接口处于同一量级，无性能退化。

---

## 6. 证据索引（关键实现文件:行号）

| 主题 | 文件:行号 |
| --- | --- |
| 故事设定域 4 接口 | `app/api/script_pipeline.py:756`（generate）/ `:824`（get）/ `:869`（edit）/ `:922`（regenerate） |
| 设定落库 / 输出组装 | `app/api/script_pipeline.py:704`（_setting_to_out）/ `:729`（_build_setting_payload） |
| 输入守卫 / 源文本 | `app/api/script_pipeline.py:357`（_require_script_input）/ `:326`（_get_source_text） |
| 单集大纲重生成 | `app/api/script_pipeline.py:1448` |
| assist 预览 / 采纳 | `app/api/script_pipeline.py:2184`（preview）/ `:2272`（apply） |
| assist 白名单与截长 | `app/api/script_pipeline.py:2101`（_ASSIST_SETTING_PATHS）/ `:2103`（_ASSIST_OUTLINE_PATHS）/ `:2108`（_ASSIST_OUTLINE_TEXT_LIMITS） |
| 设定 Agent | `app/agents/script_pipeline.py:1703`（plan_story_setting）/ `:1635`（build_setting_planner_messages）/ `:1675`（_normalize_story_setting）/ `:1612`（系统提示词）/ `:1630-1632`（三段截长）/ `:110`（温度序列） |
| assist Agent | `app/agents/script_pipeline.py:1785`（ASSIST_PATH_WHITELIST）/ `:1800`（build_assist_messages）/ `:1844`（_normalize_assist_changes）/ `:1904`（assist_rewrite） |
| 单集大纲重生成 Agent | `app/agents/script_pipeline.py:726`（plan_episode_outline_regen） |
| 片段 id / diff / 应用 | `app/services/script_service.py:807`（_change_id）/ `:825`（build_field_diffs）/ `:882`（apply_optimize_changes） |
| Schema（设定域） | `app/schemas/script_pipeline.py:696`（StorySettingOut）/ `:717`（StorySettingEditRequest）/ `:751`（StorySettingRegenerateRequest）/ `:762`（StorySettingEditOut）/ `:774`（StorySettingGenerateOut）/ `:791`（StorySettingDetailOut） |
| Schema（大纲重生成） | `app/schemas/script_pipeline.py:371`（EpisodeOutlineRegenerateRequest）/ `:383`（EpisodeOutlineRegenerateOut）/ `:294`（EpisodePlanOutItem） |
| Schema（assist 域） | `app/schemas/script_pipeline.py:816`（AssistScope）/ `:819`（AssistPreviewRequest）/ `:860`（AssistPreviewOut）/ `:883`（AssistApplyRequest）/ `:923`（AssistApplyOut）/ `:602`（OptimizeChangeOut）/ `:642`（OptimizeApplyChangeItem） |
| 基线文档 / 改造 spec | `backend/docs/api-script-pipeline.md` / `.trae/specs/redesign-script-workspace/spec.md` |
