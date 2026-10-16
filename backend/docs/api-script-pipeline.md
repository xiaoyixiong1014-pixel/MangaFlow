# MangaFlow 剧本创作范式流水线 API 文档（bend: rebuild-script-pipeline）

> 本文档描述「剧本创作模块范式级重构」新增的**四阶段范式流水线**接口与
> 相关既有接口的变更口径。字段名 / 必填性 / 默认值 / 错误码均以源码为准
> （核实基准：`app/api/script_pipeline.py`、`app/api/script.py`、
> `app/schemas/script_pipeline.py`、`app/schemas/script.py`、
> `app/services/script_service.py`、`app/services/script_export_service.py`、
> `app/agents/script_pipeline.py`、`app/agents/script_pipeline_graph.py`，
> 2026-10-14）。快速模式的既有链路（一键生成 / 抽取确认 / 同步）见
> 姊妹文档 [`api-creation-chain.md`](./api-creation-chain.md)。

---

## 0. 概述

### 0.1 四阶段范式流水线

范式流水线把「剧本创作」拆为四个可人工介入的阶段，按序推进：

```
idea ──► characters ──► outline ──► body
创意输入   角色班底设定    分集大纲      逐集正文（文章体 + 结构化场次）
```

| 阶段 | 语义 | 主要接口域 |
| --- | --- | --- |
| `idea` | 输入创意 / 原著素材（一句话创意、大纲、小说） | `PUT /script/input`、`POST /script/input/novel/parse` |
| `characters` | 生成 / 编辑 / 确认角色班底（候选 → 入池） | 角色域 5 接口 |
| `outline` | 生成 / 编辑 / 确认分集大纲 | 大纲域 4 接口 |
| `body` | 逐集生成 / 编辑 / 优化正文 | 正文域 3 接口 + 优化域 2 接口 |

同一个项目还可走**整跑子图**（`pipeline/run/stream` + `resume` + `status` /
`state`）——由 LangGraph 编排上述四阶段，在三处确认闸口（角色 / 大纲 / 正文）
暂停等待人工确认，支持断线恢复。

### 0.2 快速模式并存

`projects.script_phase` 为 **NULL** 表示「快速模式」（沿用旧链路：`PUT
/script/input` → `POST /script/generate/stream` 一键出剧本 → 抽取 → 同步），
不进入范式工作台。范式工作台的每一步在以 `phase` 字段回读阶段，NULL 即快速
模式。两类模式在同一项目上**互斥使用**：一旦进入范式（phase 非空），
`/script/generate/stream` 与 `/script` 仍可操作默认剧本行，但正文以「逐集行」
为准。

### 0.3 阶段状态机与推进规则

```
        (NULL=快速模式)
idea ──► characters ──► outline ──► body
```

- **正常推进**：每次确认接口（characters/confirm、episodes/plan/confirm）经
  `advance_phase` 做**一格校验**——只能从紧邻上一阶段前进一格，越格抛 409。
- **起点特例**：`phase ∈ {NULL, idea}` 视作流水线起点，允许直接置
  `characters`（角色 generate）或 `body`（大纲 confirm，快速模式 NULL→body 为
  跨两格起点特例）；`characters` 阶段也可直接置 `body`。
- **幂等保持**：重复确认时若阶段已处于或超过目标阶段，**不再推进**（保持原值），
  接口按幂等成功返回。

### 0.4 统一错误码约定

| 状态码 | 触发条件 |
| --- | --- |
| 401 | 未认证 / token 失效（`get_current_user`） |
| **404** | **归属保护**：项目 / 剧集 / 资产 / 检查点线程不存在、已软删、**或不属于当前用户**——统一 404，不泄露他人资源的存在性 |
| **409** | **阶段或并发冲突**：阶段守卫（如 non-body 调正文域）、并发生成锁、乐观锁 `base_version` 不匹配、幂等前置不满足 |
| **400** | **参数错误**：`format`/`entity_type`/`sequence` 越界、`segment`/`field` 的 `target` 非法、剧本尚无内容等 |
| 413 | 上传超限（小说文件 > 20MB） |
| 422 | Pydantic 请求体/查询参数校验失败（默认结构，`detail` 为数组） |
| **502** | **LLM 失败**：生成/优化经内部重试后仍失败（`plan_characters`/`plan_episodes`/`plan_episode_scenes` 重试 2 次、`optimize` 重试 2 次） |
| 500 | 未捕获异常（全局处理器，不泄露堆栈） |

错误响应统一为 FastAPI 默认结构（单一 `detail` 字段）：

```json
{ "detail": "中文错误描述" }
```

鉴权：全部接口要求 `Authorization: Bearer <access_token>`，并在处理前做
`project → user` 归属校验（他人项目一律 404）。

### 0.5 分域接口清单

| 域 | 方法 | 路径 | 说明 |
| --- | --- | --- | --- |
| 角色 | POST | `/api/projects/{id}/script/characters/generate` | LLM 生成角色班底候选 |
| 角色 | GET | `/api/projects/{id}/script/characters` | 候选 + 已入池角色双列表 |
| 角色 | PUT | `/api/projects/{id}/script/characters` | 批量编辑候选（upsert / remove） |
| 角色 | POST | `/api/projects/{id}/script/characters/{cid}/regenerate` | 单角色重新生成 |
| 角色 | POST | `/api/projects/{id}/script/characters/confirm` | 确认入池并推进阶段 |
| 大纲 | POST | `/api/projects/{id}/script/episodes/plan` | LLM 生成分集大纲 |
| 大纲 | GET | `/api/projects/{id}/script/episodes/plan` | 分集大纲列表 |
| 大纲 | PUT | `/api/projects/{id}/script/episodes/{eid}/outline` | 单集大纲编辑 |
| 大纲 | POST | `/api/projects/{id}/script/episodes/plan/confirm` | 全量确认大纲 |
| 正文 | POST | `/api/projects/{id}/script/episodes/{eid}/body/generate/stream` | SSE 流式生成单集正文 |
| 正文 | PUT | `/api/projects/{id}/script/episodes/{eid}/body` | 保存单集正文（场次级 source 合并） |
| 正文 | POST | `/api/projects/{id}/script/episodes/{eid}/body/regenerate` | SSE 重生成单集正文 |
| 优化 | POST | `/api/projects/{id}/script/episodes/{eid}/optimize` | AI 优化预览（不落库） |
| 优化 | POST | `/api/projects/{id}/script/episodes/{eid}/optimize/apply` | 应用选中优化片段 |
| 子图 | POST | `/api/projects/{id}/script/pipeline/run/stream` | SSE 启动/续跑四阶段子图 |
| 子图 | POST | `/api/projects/{id}/script/pipeline/resume` | SSE 确认/退回续跑 |
| 子图 | GET | `/api/projects/{id}/script/pipeline/status` | 断线重连状态 |
| 子图 | GET | `/api/projects/{id}/script/pipeline/state?thread_id=` | 按线程回放子图状态 |
| 小说 | POST | `/api/projects/{id}/script/input/novel/parse` | 解析小说章节并暂存 |
| 小说 | GET | `/api/projects/{id}/script/input/novel/chapters` | 读取暂存章节列表 |
| 影响面 | GET | `/api/projects/{id}/script/entities/impact` | 实体删除影响面预览 |
| 导出 | GET | `/api/projects/{id}/script/export` | 剧本导出（txt / fountain） |

---

## 1. 角色域（Characters）

角色存在两种形态：**候选**（planned，存于 `project.config["script_characters"]`，
带 8 位 hex 临时 `candidate_id`，未落库）与**已入池**（confirmed，`characters`
表行，`confirmed_at` 非空）。`CharacterProfileOut.source` 区分二者
（`planned` / `confirmed`），候选的 `id` 为 null、`candidate_id` 有值；
已入池的 `candidate_id` 为 null、`id` 有值，`appearance` 回读映射自
`characters.description`。

### 1.1 生成角色班底候选

```
POST /api/projects/{project_id}/script/characters/generate
```

鉴权：Bearer（项目归属校验）。

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| project_id | path | int | 是 | - | 项目 ID |
| instruction | body | string\|null | 否 | null | 对角色班底的自然语言要求，≤2000 字符 |
| episode_count_hint | body | int\|null | 否 | null | 目标集数提示（1~100），供 LLM 控制班底规模 |

> 请求体整体可缺省（`req: Optional[...]`）。

成功响应 `200`：

```json
{
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
| 409 | 项目尚未保存任何剧本输入（`raw_text` 与结构化 content 皆空） |
| 502 | LLM 生成与解析多次失败 |

**幂等与阶段**：**覆盖式**（每次调用以新结果整体替换 `config` 候选，
非增量）。`phase ∈ {NULL, idea}` 时置 `characters`（流水线起点特例）；
其余阶段保持不变。

### 1.2 角色列表（候选 + 已入池）

```
GET /api/projects/{project_id}/script/characters
```

无请求参数。成功响应 `200`：

```json
{
  "planned": [ /* source=planned 的候选档案（未生成过为空） */ ],
  "confirmed": [ /* source=confirmed 的已入池档案 */ ],
  "phase": "outline"
}
```

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目 |

`confirmed` 仅返回 `deleted_at IS NULL` 且 `confirmed_at IS NOT NULL` 的角色，
按 `id` 升序。

### 1.3 批量编辑候选

```
PUT /api/projects/{project_id}/script/characters
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| upsert | body | CharacterProfileUpsert[] | 否 | [] | 新增/更新条目 |
| remove_ids | body | string[] | 否 | [] | 按候选临时 ID 移除 |
| remove_names | body | string[] | 否 | [] | 按候选角色名移除 |

`CharacterProfileUpsert` 字段：`id`（候选临时 ID，优先匹配键）、`name`
（必填，1~50 字符，name 匹配键 / 更新值）、`role_type`、`age`（≤20）、
`personality`、`appearance`、`motivation`、`arc`、`relations`
（`{target, relation}[]`；`None` = 不修改）。

成功响应 `200`：`CharacterCandidatesOut`（`{"characters": [...], "phase": ...}`，
同 1.1 结构）。

**匹配与幂等口径**（仅作用于候选，**不碰已入池资产**——已入池角色走既有资产
接口编辑）：

1. 先执行删除（按 `remove_ids` 或 `remove_names`）；
2. 再执行 upsert：`id` 命中 → 更新；否则 `name` 命中 → 更新；均未命中 →
   新增候选（自动分配 8 位 hex 临时 id、`confirmed=False`）；
3. 传入字段值为 `None` 时不覆盖原值。

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目 |
| 422 | upsert 条目结构非法（如 name 为空 / 超长） |

### 1.4 单角色重新生成

```
POST /api/projects/{project_id}/script/characters/{cid}/regenerate
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| cid | path | string | 是 | - | 候选临时 ID（8 位 hex）或已入池角色数字 ID 的字符串形式 |
| instruction | body | string\|null | 否 | null | 本轮重生成方向，≤2000 字符 |

成功响应 `200`：

```json
{ "character": { /* 重生成后的角色档案 */ }, "phase": "characters" }
```

**两条匹配路径**：

- **候选路径**（`cid` 命中 config 候选 id）：构造「重新设计角色「name」（当前
  设定：…）+ instruction」单角色指令，取返回的第一个角色**替换该候选设定**，
  保留原候选 id 与 `confirmed` 标记，其余候选不动；
- **已入池路径**（`cid` 转 int 命中 `characters` 主键且属于本项目、未软删）：
  以角色 name 为指令重生成，仅更新其人设字段（**name 保持不动**，避免撞唯一
  索引），`description` 更新为人设四要素摘要。

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目；或 `cid` 无法匹配任何候选 / 已入池角色 |
| 502 | LLM 生成与解析多次失败 |

### 1.5 确认角色入池（推进阶段）

```
POST /api/projects/{project_id}/script/characters/confirm
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| characters | body | CharacterConfirmItem[] | 是 | - | 确认入池条目列表，**至少 1 条** |

`CharacterConfirmItem` 字段：`name`（必填，1~50）、`role_type`、`age`（≤20）、
`personality`、`appearance`、`motivation`、`arc`、`relations`（`None` = 沿用
候选值）。

成功响应 `200`：

```json
{ "confirmed_count": 2, "phase": "outline" }
```

**幂等与阶段**：

- **同名幂等入池**：同名角色更新人设字段、否则新建；`visual_prompt` 缺省用
  默认模板、`description` 为人设四要素摘要、`relations` 存 JSONB、
  `confirmed_at=now`、`source="script_planner"`；
- 确认结果回写 config 候选（`confirmed=True`），并写
  `characters_confirmed` 里程碑快照（V-01 留痕）；
- 阶段推进：`characters → outline`（一格校验）；`NULL/idea` 特例直接置
  `outline`；`outline` 及之后**重复确认幂等保持**，不再推进。

| 错误码 | 触发条件 |
| --- | --- |
| 400 | 项目尚无角色候选（提示先生成角色） |
| 404 | 项目不存在 / 非本人项目 |

---

## 2. 大纲域（Episodes Plan）

### 2.1 生成分集大纲

```
POST /api/projects/{project_id}/script/episodes/plan
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| target_episodes | body | int\|null | 否 | null | 目标集数（1~100）；缺省由服务端启发式决定（`target_duration ≥ 600s` 或小说切分模式建议 3，否则 1） |
| from_chapters | body | bool | 否 | false | 是否按小说章节切分（须先解析章节） |

> 请求体整体可缺省（`req: Optional[...]`）。

成功响应 `200`：

```json
{
  "episodes": [
    {
      "episode_id": 1, "sequence": 1, "title": "第一集：天台之约",
      "logline": "两个少女在天台相遇并约定一起成为漫画家",
      "core_event": "天台立约，共同报名新人奖",
      "characters": ["林小满", "陈默"],
      "emotion_curve": "热血", "duration_sec": 180, "confirmed_at": null
    }
  ],
  "phase": "outline"
}
```

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目 |
| 409 | `phase=body`（大纲已锁定）；或尚未确认角色设定；或 `from_chapters=true` 但尚未解析小说章节 |
| 502 | LLM 生成与解析多次失败 |

**落库口径（多退少补）**：既有 `episodes` 行按 `sequence` 复用更新
（title/logline/outline）并重写 sequence 为 `1..n`；不足新建；多余**硬删**
（删除前解绑 `scripts` / `storyboards` / `film_outputs` 引用）。

**阶段**：`NULL/idea/characters` 时置 `outline`（NULL→outline 为快速模式
跨格起点特例，不经一格校验）。

**`from_chapters` 分支（小说改编）**：校验 `config["novel_chapters"]` 非空；
目标集数缺省为章节数（超上限则合并）、显式传值优先；按章节摘要合并/拆分出
恰好 target 段，连同章节上下文（`build_novel_context`）注入 episode_planner。

**阶段守卫**：`phase=body` 时拒绝整卷重生成（409），单集微调走 2.3。

### 2.2 分集大纲列表

```
GET /api/projects/{project_id}/script/episodes/plan
```

无请求参数。成功响应 `200`：`EpisodePlanOut`（同 2.1 结构）。**无任何剧集时
返回空列表（不 404）**，便于前端渲染空态。

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目 |

### 2.3 单集大纲编辑

```
PUT /api/projects/{project_id}/script/episodes/{eid}/outline
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| eid | path | int | 是 | - | 剧集 ID（须属于该项目） |
| title | body | string\|null | 否 | null | 单集标题（1~100） |
| logline | body | string\|null | 否 | null | 一句话梗概（1~200） |
| core_event | body | string\|null | 否 | null | 核心事件 |
| characters | body | string[]\|null | 否 | null | 出场角色名列表 |
| emotion_curve | body | string\|null | 否 | null | 情绪曲线 |
| duration_sec | body | int\|null | 否 | null | 目标时长（秒，≥1） |
| sequence | body | int\|null | 否 | null | 剧集序号（互换重排，1~1000） |

成功响应 `200`：`EpisodePlanOutItem`（编辑后的单集）。

**幂等口径**：全部字段可空，`None` = 不修改（仅覆盖传入字段）；`title` 自动
strip。

**`sequence` 重排**：与目标序号的**同项目剧集互换** sequence（无同序号行时
直接占用）；写入采用**两阶段**（先置负值再回写）避开 `uk_episodes_seq`
唯一索引。

| 错误码 | 触发条件 |
| --- | --- |
| 400 | `sequence` 超出 1~1000 |
| 404 | 项目 / 剧集不存在或不属于该项目 |
| 409 | `phase=body` 时禁止 `sequence` 重排（仅允许文案微调） |

### 2.4 全量确认大纲（推进阶段）

```
POST /api/projects/{project_id}/script/episodes/plan/confirm
```

无请求参数。成功响应 `200`：

```json
{ "confirmed_count": 1, "phase": "body" }
```

**幂等与阶段**：

- 全部剧集置 `confirmed_at`（**已确认的保持原值不覆盖**）；
- 写 `outline_confirmed` 里程碑快照；
- 阶段推进：`outline → body`（一格校验）；`NULL/idea/characters` 特例直接置
  `body`（快速模式 NULL→body 为跨两格起点特例）；`body` 及之后幂等保持。

| 错误码 | 触发条件 |
| --- | --- |
| 400 | 项目尚无任何剧集（提示先生成分集大纲） |
| 404 | 项目不存在 / 非本人项目 |

---

## 3. 正文域（Episode Body，精细流水线专属）

**阶段守卫**：正文域三接口要求 `phase == "body"`，否则 409「请先确认分集
大纲」（**NULL 快速模式不放行正文域**，与大纲域对 NULL 的放行不同）。

### 3.1 SSE 流式生成单集正文

```
POST /api/projects/{project_id}/script/episodes/{eid}/body/generate/stream
```

无请求体。前置校验（按序）：项目归属 → 剧集归属（404）→ `phase==body`
（409）→ 该集大纲已生成（409）→ 已确认角色 ≥1（409）→ 进程内生成锁
（409 正在生成中）。

**SSE 事件协议**（`text/event-stream`）：

| 事件名 | 数据字段 | 触发时机 |
| --- | --- | --- |
| `delta` | `{text}` | 两段式第一段：文章体正文分块流式透传（可持续多帧） |
| `status` | `{message}` | 进入第二段前发出（`"正在生成结构化场次"`） |
| `error` | `{message}` | LLM 异常 / 结构化 3 轮耗尽 / 乐观锁冲突（**不落库、不发 done**） |
| `done` | `{episode_id, script_id, version, raw_text, content, usage}` | 成功收尾（content 为 RD-03 字段级包装，`source="ai"`） |

`done` 示例：

```json
{
  "episode_id": 2, "script_id": 4, "version": 1,
  "raw_text": "天台的风吹过，林小满握紧了拳头。",
  "content": {
    "title": {"value": "第一集：天台之约", "source": "ai"},
    "logline": {"value": "两个少女在天台相遇并约定一起成为漫画家", "source": "ai"},
    "locations": {"value": [{"name": "学校天台", "description": "黄昏的学校天台", "time_of_day": "dusk"}], "source": "ai"},
    "scenes": {"value": [ /* 场次数组，含 action/sfx/dialogue 等 */ ], "source": "ai"}
  },
  "usage": null
}
```

**落库口径**：`scripts` 行按 `(project_id, episode_id)` upsert——首次新建
`version=1`；已存在行走**乐观锁** `UPDATE ... WHERE version=旧值`（冲突发
`error` 不落库）。`episode.status` 置 `scripted`。`usage` 为本次生成 LLM 用量
归因记录（mock / 离线路径为 null）。

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目 / 剧集不存在或不归属 |
| 409 | 阶段未达 body / 该集大纲未生成 / 无已确认角色 / 同项目已有生成流进行中 |

### 3.2 保存单集正文

```
PUT /api/projects/{project_id}/script/episodes/{eid}/body
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| eid | path | int | 是 | - | 剧集 ID |
| raw_text | body | string | 是 | - | 文章体正文文本（≥1 字符） |
| content | body | object | 是 | - | 结构化正文（`scenes` 场次数组；保存时按场次合并 source） |
| base_version | body | int\|null | 否 | null | 乐观锁基准版本号（≥1；不匹配 409） |

成功响应 `200`：

```json
{
  "episode_id": 2, "script_id": 4, "raw_text": "用户修改后的文章体正文。",
  "content": { "value": { /* 合并后的结构化正文 */ }, "source": "user" },
  "version": 2, "updated_at": "2026-10-14T15:42:49"
}
```

**幂等与合并口径**：

- **场次级 source 合并**（`merge_scene_sources`，RD-03 场次粒度）：逐场比对
  旧 content 与新 content（`scene_no + title` 匹配 + 内容指纹）——**改动/新增
  场次标 `"user"`、未改动场次保留旧 source**；首次保存（无旧数据）全部标
  `"ai"`；
- 合并结果整体包装为 `{"value": merged, "source": "user"}` 落库，`version += 1`；
- `base_version` **可选**：传入且与当前 `version` 不一致 → 409（兼容未传的
  旧客户端）。
- 首次保存（该集无剧本行）新建 `version=1`。

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目 / 剧集不存在或不归属 |
| 409 | 阶段未达 body；或 `base_version` 不匹配（剧本已被其他操作修改） |

### 3.3 SSE 重生成单集正文

```
POST /api/projects/{project_id}/script/episodes/{eid}/body/regenerate
```

无请求体。**事件协议与 3.1 完全一致**（复用同一生成器），语义差异：要求该集
**已有正文**（`scripts` 行存在且 `raw_text` 非空，否则 **400**「该集尚未生成
正文」），重生成 = 覆盖 `raw_text/content`、`version+1`（乐观锁）。

| 错误码 | 触发条件 |
| --- | --- |
| 400 | 该集尚未生成正文 |
| 404 | 项目 / 剧集不存在或不归属 |
| 409 | 阶段未达 body / 大纲未生成 / 无已确认角色 / 已有生成流进行中 |

---

## 4. 优化域（AI Optimize）

### 4.1 优化预览（不落库）

```
POST /api/projects/{project_id}/script/episodes/{eid}/optimize
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| eid | path | int | 是 | - | 剧集 ID |
| grain | body | Literal | 是 | - | 粒度：`global` / `episode` / `segment` / `field` |
| target | body | int\|string\|null | 否 | null | `segment`=场次序号（int 或数字串）；`field`=字段路径（如 `scenes[1].plot`）；`global`/`episode` 可省略 |
| instruction | body | string\|null | 否 | null | 优化方向补充，≤2000 字符 |

成功响应 `200`：

```json
{
  "episode_id": 2, "grain": "segment", "count": 1,
  "changes": [
    {
      "id": "f6b6f7240aed", "path": "scenes[0].action", "kind": "field",
      "original": "用户改过的动作：小满攥紧栏杆俯瞰操场。",
      "proposed": "夜风呼啸，两人并肩站上天台边缘远眺城市灯火。",
      "reason": "增强画面感与情绪张力",
      "diff_lines": [{"op": "equal", "text": "…"}, {"op": "delete", "text": "…"}, {"op": "insert", "text": "…"}]
    }
  ]
}
```

**幂等口径**：**只读预览，不落库**。`original` 一律以**当前库值**覆盖（LLM
输出的原文字段不可信）；`path` 经白名单 + 真实性校验，非法路径丢弃（因此
`count` 可能少于 LLM 输出条数）；每片段带稳定 `id`（供 apply 匹配）。

| 错误码 | 触发条件 |
| --- | --- |
| 400 | 该集无正文；或 `segment`/`field` 缺 `target` / `target` 非法（越界 / 非白名单 / 字段不存在） |
| 404 | 项目 / 剧集不存在或不归属 |
| 409 | 阶段未达 body |
| 502 | 优化 LLM 多次失败 |

### 4.2 应用选中优化片段

```
POST /api/projects/{project_id}/script/episodes/{eid}/optimize/apply
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| accept_ids | body | string[] | 否 | [] | 接受应用的片段 ID 列表 |
| changes | body | object[] | 否 | [] | 预览时的片段（`{id, path, original, proposed}`，服务端据此匹配 id→变更） |
| base_version | body | int\|null | 否 | null | 乐观锁基准版本号（≥1；不匹配 409） |

成功响应 `200`：

```json
{
  "applied": 1, "skipped": 0, "version": 3,
  "content": { "title": {"value": "…", "source": "user"}, "scenes": {"value": [ /* source=ai_optimized */ ], "source": "user"} }
}
```

**幂等与并发口径**：

- 服务端按 `accept_ids` 从 `changes` 中匹配片段，经 `apply_optimize_changes`
  做 **path 白名单 + 越界 + `original` 与当前库值一致**校验，**不一致的片段
  跳过计入 `skipped`**（防并发覆盖）；
- **`applied=0` 时不递增版本、不落库**（直接返回当前 content）；
- 应用后 `version+1`，以乐观锁 `UPDATE ... WHERE version=旧值` 落库（冲突
  409）；被改动场次元素 `source="ai_optimized"`、未改动场次保持原 source。

| 错误码 | 触发条件 |
| --- | --- |
| 400 | 该集无正文 |
| 404 | 项目 / 剧集不存在或不归属 |
| 409 | 阶段未达 body；`base_version` 不匹配；或落库乐观锁冲突 |

---

## 5. 子图编排（Pipeline，整跑 + 断线恢复）

子图线程 ID 由服务端按 **`script_{project_id}_{user_id}`** 固定命名（同一用户
在同一项目上多次调用幂等续跑同一线程）。检查点状态内 `user_id` / `project_id`
双重归属校验，他人线程 / 跨项目一律 404。

### 5.1 SSE 启动/续跑子图

```
POST /api/projects/{project_id}/script/pipeline/run/stream
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| instruction | body | string\|null | 否 | null | 入口补充指令（透传给生成节点），≤2000 |
| auto_confirm | body | bool | 否 | false | 一键串跑：三处确认闸口自动放行，不在 `pending_confirm` 暂停 |

**幂等语义**：

- **无检查点** → 以 `{project_id, user_id, instruction, auto_confirm}` 启动
  子图，跑到首个确认闸口（`pending_confirm`）或全流程完成（`done`）；
- **已有挂起闸口** → **不重跑图**（避免重复 LLM 调用），直接回放
  `pending_confirm` + `done`；`auto_confirm=true` 时以自动确认续跑至下一闸口 /
  完成；
- **已完成（body 确认）** → 直接回放 `done`。

**SSE 事件协议**：

| 事件名 | 数据字段 | 触发时机 |
| --- | --- | --- |
| `node` | `{node}` | 每完成一个子图节点推送一次（进度可视）；被中断节点从检查点快照补推 |
| `pending_confirm` | `{thread_id, stage, confirm_type, payload}` | 触达三处人工确认闸口（interrupt）时推送，本次流结束，等 `resume` 续跑 |
| `error` | `{message}` | 节点 error 通道非空（前置不满足 / 退回重生）或图执行异常 |
| `done` | `{thread_id, script_phase, interrupted, error}` | 流收尾（暂停 / 完成 / 报错均发出，便于前端收敛 UI） |

`stage` 取值：`characters` / `outline` / `body`；`confirm_type` 取值：
`confirm_characters` / `confirm_episodes` / `confirm_body`。

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目；或检查点线程归属不符 |

### 5.2 SSE 确认/退回续跑

```
POST /api/projects/{project_id}/script/pipeline/resume
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| decision | body | Literal | 否 | `approved` | `approved`（确认通过）/ `rejected`（退回重生） |
| reason | body | string\|null | 否 | null | 退回意见（`rejected` 时作为重生指令 / 播报文案），≤2000 |
| edited_payload | body | object\|null | 否 | null | 人工编辑稿（原样并入 resume 值，如 `{"characters": [...]}` / `{"episodes": [...]}`） |

**事件协议与 5.1 相同**。服务端组装 resume 值
`{"decision", "approved", "reason"?, **edited_payload}` 经 `Command(resume=...)`
续跑被中断节点：

- `approved`：按当前候选（或 `edited_payload` 人工编辑稿）落库并推进阶段，
  继续跑到下一闸口或完成；
- `rejected`：回退上游生成节点按 `reason` 重新生成，随后再次停在确认闸口。

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目；**或该线程无检查点**（首未启动） |
| 409 | 有检查点但**当前无挂起闸口**（无可续跑断点） |

### 5.3 断线重连状态

```
GET /api/projects/{project_id}/script/pipeline/status
```

无请求参数（`thread_id` 服务端推导）。成功响应 `200`：

```json
{
  "thread_id": "script_2_2", "project_id": 2,
  "script_phase": "body",
  "current_stage": "", "next_nodes": [],
  "pending_stage": "", "has_pending_confirm": false, "pending_confirm": null,
  "summary": {"character_count": 2, "episode_count": 1, "body_count": 0}
}
```

字段：`script_phase`（DB 侧当前阶段）、`current_stage`（断点处待执行节点名，
多个取首个，无断点为空串）、`next_nodes`、`pending_stage`（挂起闸口对应阶段
`characters`/`outline`/`body`，无为空串）、`has_pending_confirm`、
`pending_confirm`（`{type, payload}` 或 null）、`summary`。

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目；或检查点线程归属不符 |

### 5.4 按线程回放状态

```
GET /api/projects/{project_id}/script/pipeline/state?thread_id={thread_id}
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| thread_id | query | string | 是 | - | 子图检查点线程 ID（1~80 字符） |

成功响应 `200`：

```json
{
  "thread_id": "script_2_2", "project_id": 2, "phase": "characters",
  "next_nodes": ["sp_character_planner"],
  "pending_interrupt": {
    "type": "confirm_characters",
    "payload": {"message": "…", "count": 2, "characters": [ /* 候选摘要 */ ]}
  },
  "summary": {"character_count": 2, "episode_count": 0, "body_count": 0}
}
```

与 5.3 的差异：本端点**只读回放**（供前端渲染「继续 / 重试」卡片），续跑仍走
5.2；`summary.character_count` 取 config 候选数（DB 侧）。

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目；或检查点线程归属不符 |
| 401 | 未登录 |

---

## 6. 小说导入（Novel，改编链路前置）

### 6.1 解析小说章节

```
POST /api/projects/{project_id}/script/input/novel/parse
Content-Type: multipart/form-data（文件）或 application/json（纯文本）
```

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| file | form-data | file | 二选一 | `.txt` / `.epub`，≤ 20MB |
| text | body(JSON) | string | 二选一 | 纯文本正文（JSON 模式） |

成功响应 `200`：

```json
{
  "project_id": 2, "chapter_count": 12,
  "chapters": [
    {"index": 1, "title": "第一章 旧书店", "char_count": 3210, "summary": "正文首 200 字摘要"}
  ],
  "phase": null
}
```

**口径**：`parse_novel` 分派解析（`.txt` 正则切分 / `.epub` 纯标准库解析）→
`summarize_chapters` 生成摘要 → 章节元信息 + 摘要 + 正文前 2000 字预览暂存
`project.config["novel_chapters"]`（**完整正文不入库**）→ 回写剧本行
`raw_text` 为拼接全文（超 `_NOVEL_RAW_TEXT_MAX=20000` 字改用章节摘要拼接）
+ `input_mode=novel`。

| 错误码 | 触发条件 |
| --- | --- |
| 400 | 扩展名不支持 / epub 损坏 / 文本解码失败 / 无章节 / 请求体非法 |
| 404 | 项目不存在 / 非本人项目 |
| 413 | 文件超过 20MB |

### 6.2 读取暂存章节列表

```
GET /api/projects/{project_id}/script/input/novel/chapters
```

无请求参数。成功响应 `200`：`NovelChaptersOut`（`{project_id,
chapter_count, chapters, phase}`，同 6.1 结构）。**未解析时返回空列表
（不 404）**。

| 错误码 | 触发条件 |
| --- | --- |
| 404 | 项目不存在 / 非本人项目 |

---

## 7. 影响面与导出（RD-08）

### 7.1 实体删除影响面预览

```
GET /api/projects/{project_id}/script/entities/impact?entity_type=character&entity_id=3
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| entity_type | query | string | 是 | - | `character` / `scene` / `prop`（非法 400） |
| entity_id | query | int | 是 | - | 实体资产 ID（须属于该项目且未软删，否则 404） |
| episode_id | query | int\|null | 否 | null | 传入时剧本与分镜统计均限定该集 |

成功响应 `200`：

```json
{
  "entity_type": "character", "entity_id": 3, "entity_name": "林小满",
  "script_refs": 2, "storyboard_refs": 0,
  "script_locations": ["第1集 · 场景1", "场景1"], "storyboard_ids": [],
  "summary": "删除后影响 2 处剧本引用、0 个分镜"
}
```

**口径（只读，不落库）**：

- `script_refs`：剧本 content 的 `scenes` 中实体名出现次数（`title`/`plot`/
  `action` + `dialogue.character`/`dialogue.line`）；
- `storyboard_refs`：分镜「文本匹配（`dialogue`/`visual_description`/
  `prompt_locked`/`prompt_editable`）∪ 结构化引用（角色→关联表、场景→
  `scene_id`）」**去重后**的条数；
- `script_locations` 上限 10 条、`storyboard_ids` 上限 20 条。

| 错误码 | 触发条件 |
| --- | --- |
| 400 | `entity_type` 非 character/scene/prop |
| 404 | 项目不存在 / 非本人；实体不存在 / 已软删 / 不属于该项目；剧集不存在 |

### 7.2 剧本导出

```
GET /api/projects/{project_id}/script/export?format=fountain
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| format | query | string | 是 | - | `txt` 纯文本 / `fountain` 结构化剧本（**别名 `format`，非法 400**） |
| episode_id | query | int\|null | 否 | null | 缺省导出项目全部集 |

成功响应 `200`：文本响应体（`text/plain; charset=utf-8`），
`Content-Disposition: attachment; filename*=UTF-8''<URL编码文件名>`
（文件名 = 项目标题 + `.txt` / `.fountain`）。

**口径**：

- 数据来源：指定 `episode_id` 取该集剧本行；缺省取项目下**全部剧本行**，
  按 `episode.sequence` 升序（未绑定剧集的默认行作为「快速模式单篇」排在
  最后）；**无任何正文内容时 404**；
- **TXT**：多集以「===== 第N集 · 标题 =====」分隔；每集正文优先 `raw_text`，
  为空回退结构化 `scenes` 渲染；
- **Fountain**：遵循规范结构化渲染——场景标题大写
  （`INT./EXT. 地点 - 时间`，无 `int_ext` 时回退 `场景N 标题`）、角色行大写
  紧接对白、多集以 `# 第N集 · 标题` Section 分节。

示例（fountain）：

```
# 第1集 · 第一集：天台之约

EXT. 学校天台 - DUSK

立约

林小满
我们一起拿金奖吧。
```

| 错误码 | 触发条件 |
| --- | --- |
| 400 | `format` 非 txt/fountain |
| 404 | 项目不存在 / 非本人；剧集不存在；或暂无剧本内容可导出 |

---

## 8. 既有接口的变更点（backend/app/api/script.py）

本节说明范式重构对既有接口的行为变更（前后对比）。

### 8.1 `PUT /api/projects/{id}/script/input`

| 维度 | 变更 |
| --- | --- |
| 输入模式 | `content` 合并记录 `{"input_mode": mode}`（`idea`/`novel`/`outline`），保留既有字段不覆盖 |
| novel 上下文联动 | `mode=novel` 时，后续 `generate/stream`、`extract`、`episodes/plan` 检测到已解析章节（`config["novel_chapters"]` 非空）后，改用 `build_novel_context`（章节摘要 + 正文节选）拼接长上下文，取代既有 `raw_text[:4000]` 截断 |
| 剧集绑定 | 可选 `episode_id`（S-06）绑定该集剧本行（校验剧集归属） |

### 8.2 `POST /api/projects/{id}/script/generate/stream`（快速模式）

| 维度 | 变更 |
| --- | --- |
| 校验重试 | **最多 3 轮**流式尝试（`MAX_GENERATE_ATTEMPTS=3`）：每轮完整收集后经 `parse_script_json` 双重校验，失败则把「上一轮原始输出 + 校验错误摘要 + 纠错指令」追加进消息重试；3 轮耗尽发 `error`（不落库、不发 `done`、版本不加） |
| LLM 出入口 | 统一经 `llm_adapter` 单例调用（`agent_name="script"`，带用量观测） |
| 用量归因 | `done` 事件新增 `usage` 字段（`get_last_usage()`，无记录为 null） |
| 并发保护 | 同项目**进程内生成锁**：并发第二个请求 409；落库采乐观锁 `UPDATE ... WHERE version=快照值`，冲突发 `error` |
| 降级 | LLM 完全异常时走离线降级（`build_fallback_script`）并发 `error` + `done` |

### 8.3 `PUT /api/projects/{id}/script`（手动保存）

| 维度 | 变更 |
| --- | --- |
| 并发热点 | 新增**可选乐观锁** `base_version`：与当前 `version` 不一致 → 409（未传保持旧行为兼容） |
| source 合并 | **场次级** source 差异化合并（`merge_scene_sources`）：改动/新增场次标 `"user"`、未改动场次保留旧 source（首次全 `"ai"`）；顶层仍整体包装 `{"value":…, "source":"user"}` |
| 剧集绑定 | 可选 `episode_id` 按集定位剧本行 |

### 8.4 `POST /api/projects/{id}/script/health`

| 维度 | 变更 |
| --- | --- |
| 范围 | 支持 `episode_id` **按集体检**：传值仅体检该集（按 `(project_id, episode_id)` 定位剧本行，404 语义）；不传为**聚合体检**项目全部集级行（各集分别体检 + 汇总 issues，`location` 带「第N集 · 」前缀）；无集级行（快速模式）时按单剧本兜底 |
| 规则 | 替换为**四条新规则 + 时长/语速校验**：① 角色出场断层（warning）② 对白过长 >120 字（warning）③ 缺动作描写（warning，仅新 Schema）④ 缺高潮点（info）＋ 时长偏差 ±10%（error）＋ 对白语速交叉校验（warning）。旧「无场景 / 无对白 / 对白缺角色名」已移除 |
| 计分 | 起始 100，error 扣 25 / warning 扣 10 / info 扣 3，下限 0 |

### 8.5 `POST /api/projects/{id}/script/extract`

| 维度 | 变更 |
| --- | --- |
| 抽取源改判 | 由 `script.raw_text`（用户原始输入）**改判为 `build_extract_source_text(script)`**——优先生成后的完整结构化剧本 content（三幕摘要 + 场次 `title/plot` + 「角色名：台词」逐行对白），仅在无结构化 payload 时兜底 `raw_text`（修复「一句话创意抽不到角色」的 P0 断链） |
| novel 上下文 | `input_mode=novel` 且已解析章节时用 `build_novel_context` 作为抽取源 |
| 多剧本行兼容（本次修复） | `get_script_or_404` 由「按项目唯一行 `scalar_one_or_none`」改为「**优先默认剧本行（`episode_id IS NULL`），无默认行取 id 最小行**」，避免范式流水线（默认行 + 多集级行并存）下抛出 `MultipleResultsFound` → 500（详见 §10 缺陷修复说明） |

---

## 9. 性能基线（本地 dev 口径）

### 9.1 测量方式

- **环境**：Windows 开发机、单进程、PostgreSQL 本地实例、独立测试库
  `mangaflow_test`；
- **口径**：`httpx.ASGITransport` **进程内**直连 FastAPI 应用（不监听端口、
  不触发 lifespan），**LLM 全部 mock**（无真实模型网络调用）；
- **方法**：每接口串行请求 **3 次取中位数**；SSE 记录**首事件**耗时（完整
  消费响应体）；
- **数据量**：1 个项目 / 1 集大纲 / 1 集 2 场次正文 / 2 已确认角色 / 0 分镜。

> **口径声明**：以上为**本地单机进程内**口径，**非生产压测结论**（进程内
> 直连省去网络与网关开销，真实部署需以 `/api/metrics` 直方图在真实流量下标定）。

### 9.2 实测结果

| 接口 | 中位耗时 (ms) | 3 次样本 (ms) | 备注（数据量） |
| --- | --- | --- | --- |
| `GET /api/projects` | **6.9** | 12.6 / 6.6 / 6.9 | 本人 1 个项目 |
| `GET /script/episodes/plan` | **6.5** | 8.2 / 6.5 / 6.3 | 1 集大纲 |
| `POST /script/health?episode_id={eid}` | **8.3** | 8.4 / 8.3 / 7.7 | 1 集 2 场次（含 LLM 无关规则体检 + 落库） |
| `GET /script/export?format=txt` | **6.1** | 6.1 / 6.2 / 5.9 | 1 集 2 场次 |
| `GET /script/entities/impact` | **8.2** | 9.3 / 8.1 / 8.2 | 1 角色 0 分镜 |
| `POST /script/episodes/{eid}/body/generate/stream`（首事件） | **10.9** | 12.0 / 10.0 / 10.9 | mock LLM，两段式全流程首事件耗时 |

> 复现命令：
> `backend\.venv\Scripts\python.exe .run\smoke_script_pipeline.py`
> （脚本内置上述测量，结尾打印性能基线表）。

**说明**：全部接口均在**个位数~十余毫秒**量级，与既有文档
`api-creation-chain.md` §11.3 的列表类 <50ms 量级判断一致；SSE 首事件耗时主要
来自 ASGI 分派与事件编码，mock LLM 即时完成（真实渠道需另计模型首 token
延迟）。真实 LLM 链路（未 mock）的端到端耗时**需真实渠道，未测**。

---

## 10. 缺陷修复说明（本轮冒烟发现）

**缺陷**：范式流水线下，同一项目会同时存在①未绑定剧集的**默认剧本行**
（`episode_id IS NULL`，idea/novel 输入或手动保存）与②**多集级剧本行**
（`episode_id` 非空，逐集正文）。既有 `POST /script/extract` 经
`get_script_or_404` 以 `scalar_one_or_none()` 定位「项目唯一剧本行」，在多行
场景下抛出 `sqlalchemy.exc.MultipleResultsFound` → 500（冒烟脚本首轮复现）。

**修复**（`app/services/script_service.py` `get_script_or_404`）：改为
**优先返回默认剧本行（`episode_id IS NULL`），无默认行时取 `id` 最小首行**。
该函数**仅被 `/script/extract` 使用**，单行项目行为完全不变，仅消除多行场景
的 500。

**影响面**：无其他调用点；不动任何接口出入参；冒烟脚本修复后全流程通过
（`extract` 由 500 转为 200，正确从 content 抽到角色）。

---

## 11. 端到端冒烟与证据索引

### 11.1 冒烟脚本

`.run/smoke_script_pipeline.py`：`httpx.ASGITransport` + mock LLM 驱动完整范式
流水线（注册 → 建项目 → idea → characters → outline → body → optimize →
health → extract → impact → export → pipeline/status），并内联性能测量。
独立测试库 `mangaflow_test`，子图检查点用 `InMemorySaver`（主库零读写）。
运行输出（2026-10-14）全部 `[OK]`，退出码 0。

### 11.2 证据索引（关键实现文件:行号）

| 主题 | 文件:行号 |
| --- | --- |
| 角色域 5 接口 | `app/api/script_pipeline.py:337`（generate）/ `:389`（list）/ `:432`（edit）/ `:502`（regenerate）/ `:598`（confirm） |
| 大纲域 4 接口 | `app/api/script_pipeline.py:820`（plan）/ `:927`（list）/ `:960`（outline）/ `:1049`（confirm） |
| 正文域 3 接口 | `app/api/script_pipeline.py:1316`（generate SSE）/ `:1363`（save）/ `:1433`（regenerate SSE） |
| 正文 SSE 事件生成器 | `app/api/script_pipeline.py:1196` |
| 优化域 2 接口 | `app/api/script_pipeline.py:1546`（optimize）/ `:1595`（apply） |
| 子图 4 接口 | `app/api/script_pipeline.py:2028`（run/stream）/ `:2097`（resume）/ `:1965`（status）/ `:1907`（state） |
| 子图 SSE 事件生成器 | `app/api/script_pipeline.py:1816` |
| 小说 2 接口 | `app/api/script.py:1106`（parse）/ `:1155`（chapters） |
| 影响面 / 导出 | `app/api/script.py:524`（impact）/ `:584`（export） |
| 既有接口变更 | `app/api/script.py:138`（input）/ `:197`（generate/stream）/ `:351`（save）/ `:404`（health）/ `:456`（extract） |
| 角色/大纲/正文/优化 Schema | `app/schemas/script_pipeline.py:56`（CharacterProfileOut）/ `:278`（EpisodePlanGenerateRequest）/ `:373`（EpisodeBodySaveRequest）/ `:543`（EpisodeOptimizeRequest）/ `:632`（EpisodeOptimizeApplyRequest） |
| 子图 Schema | `app/schemas/script_pipeline.py:434`（PipelineStateOut）/ `:500`（ScriptPipelineStatusOut）/ `:462`（ScriptPipelineRunRequest）/ `:478`（ScriptPipelineResumeRequest） |
| 抽取源改判 / 体检 / 归属性检 | `app/services/script_service.py:1020`（build_extract_source_text）/ `:1195`（run_health_check）/ `:1375`（run_project_health_check）/ `:1566`（extract_entities） |
| 多行兼容修复 | `app/services/script_service.py:163`（get_script_or_404） |
| 场次级 source 合并 | `app/services/script_service.py:516`（merge_scene_sources） |
| 影响面 / 导出渲染 | `app/services/script_export_service.py:166`（collect_entity_impact）/ `:252`（collect_export_episodes）/ `:458`（render_fountain） |
| LLM 挂点与生成逻辑 | `app/agents/script_pipeline.py:102`（_llm_chat）/ `:666`（_llm_chat_stream）/ `:343`（plan_characters）/ `:537`（plan_episodes）/ `:776`（stream_episode_body）/ `:1046`（plan_episode_scenes）/ `:1386`（optimize_episode_content） |
| 子图编排 | `app/agents/script_pipeline_graph.py:1160`（build_script_pipeline_subgraph）/ `:1243`（get_compiled_script_pipeline） |
| 端到端冒烟脚本 | `.run/smoke_script_pipeline.py` |
| 既有链路文档 | `backend/docs/api-creation-chain.md` |
