# MangaFlow 主链路 API 文档（创作链路缺口补全轮）

> 本文档补 Swagger（`http://127.0.0.1:8300/docs` 的 OpenAPI）之外的**业务口径**：
> 幂等键语义、降级链、占位产物标记、字段联动与留痕约定。字段与错误码均以代码为准
> （核实基准：`app/api/episodes.py` / `script.py` / `assets.py` / `storyboards.py` /
> `generation.py` / `publish.py` / `canvases.py` 及对应 services / schemas，2026-10-12）。

---

## 0. 全局约定

### 0.1 认证

- 除公开端点外，全部接口要求 `Authorization: Bearer <access_token>`（JWT，注册登录接口
  `POST /api/auth/login` 签发；access 有效期默认 7 天，refresh 30 天）。
- 公开端点（免认证）仅两类：
  - `GET /api/share/{token}` 公开分享页数据；
  - `GET /api/metrics` Prometheus 指标（仅聚合数据）。

### 0.2 错误响应结构（统一）

全部错误为 FastAPI 默认 JSON 结构，**始终是单一 `detail` 字段**：

```json
{ "detail": "中文错误描述" }
```

| 状态码 | 来源 | 说明 |
| --- | --- | --- |
| 401 | `get_current_user` | 未认证 / token 失效 |
| 403 | 归属链校验 | 任务等资源存在但所属项目不属于当前用户 |
| 404 | 归属链校验 | 资源不存在、已软删或**不属于当前用户**（统一 404，不泄露存在性） |
| 409 | 业务冲突 | 重名资产、重复提交、积分不足、终态任务误操作等 |
| 422 | Pydantic | 请求体/查询参数类型或格式校验失败（FastAPI 默认结构，`detail` 为数组） |
| 500 | 全局异常处理器 | 未捕获异常，`{"detail": "服务器内部错误"}`，不泄漏堆栈 |

> 注意区分：少数接口把"取值非法"设计为 400（如分镜导出 `format`、剧集 `status`、
> 资产 `type`），而 Pydantic 的 `Literal` 枚举校验失败是 422。两套都以各接口错误码表为准。

### 0.3 归属校验口径

- 项目域接口（episodes/script/assets/storyboards/generation/publish）：先校验
  `project → user` 归属，他人或已删除项目**一律 404**。
- 剧集/分镜/资产等子资源：子资源存在但不属于该项目同样 404。
- 任务域接口（`/api/tasks/*`）：任务经项目归属链校验，跨用户 **403**（任务详情泄漏 ID 存在性）。
- 画布域接口：`canvas → user` 校验；节点级操作（沉淀）经 `node → canvas → user` 链校验。

### 0.4 分页约定

- 分页参数：`page`（从 1 起）、`page_size`；服务端 `normalize_pagination` 钳位
  `page ≥ 1`、`page_size ∈ [1, 100]`，非法值自动收敛不报错。
- 分页响应统一结构 `PageResult`：

```json
{ "total": 42, "page": 1, "page_size": 10, "items": [ /* ... */ ] }
```

- **不分页的列表接口**：剧集列表、分镜列表（数量天然有限，按序号升序全量返回）。

### 0.5 幂等键约定

| 场景 | 幂等判定 | 重复行为 |
| --- | --- | --- |
| 剧本同步 Agent / 画布 | `project_id + script_version`（落 `agent_threads.state.script_sync` / `canvas_nodes.meta.script_sync`） | 返回既有 ID + `duplicated=true`，不重复建 |
| 画布节点沉淀 | `canvas_nodes.meta.sinked_asset_type + sinked_asset_id` 且资产未删除 | 返回既有资产 ID + `duplicated=true` |
| 生成任务创建 | 同分镜同阶段已有 queued/running 任务 | 409 拒绝（防重复提交） |
| 计费流水 | `idempotency_key` 唯一：扣费 `task_{id}_consume`、失败退款 `task_{id}_refund`、取消退款 `task_{id}_refund`、占位豁免退款 `ph-refund-{task_id}` | 同键重复请求不重复记账 |

### 0.6 占位产物标记约定（演示降级链）

详见 [§9 占位治理口径](#9-占位治理口径全局)。一句话口径：真实模型渠道全部失败时
兜底走本地 mock 哨兵渠道，产物文件名统一带 `placeholder_` 前缀、任务
`meta.placeholder=true`、`TaskOut.placeholder=true` 透出，冻结积分**全额豁免退还**。

---

## 1. 剧集管理（episodes，S-06）

全部端点挂载于 `/api/projects/{project_id}/episodes`，认证：Bearer JWT。
剧集序号 `sequence` 项目内唯一（`uk_episodes_seq` 唯一索引），状态白名单：
`draft / scripting / storyboard / producing / published`。

### 1.1 创建剧集

```
POST /api/projects/{project_id}/episodes
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| project_id | path | int | 是 | - | 项目 ID |
| title | body | string | 是 | - | 1~100 字符（自动去首尾空白） |

成功响应 `200`：

```json
{
  "id": 3, "project_id": 1, "title": "第二集",
  "sequence": 2, "status": "draft",
  "created_at": "2026-10-12T08:00:00", "updated_at": "2026-10-12T08:00:00"
}
```

业务口径：`sequence` 自动取项目内现有最大序号 +1（空项目为 1）；初始状态恒为 `draft`。

| 错误码 | 场景 |
| --- | --- |
| 404 | 项目不存在 / 已删除 / 非本人项目 |
| 422 | title 缺失或超长 |

### 1.2 剧集列表

```
GET /api/projects/{project_id}/episodes
```

无查询参数，**不分页**（一项目剧集数量级有限）。按 `sequence` 升序、平局按 `id` 升序，
返回 `EpisodeOut` 数组（结构与 1.1 响应同构）。错误：404（同上）。

### 1.3 重排剧集

```
PUT /api/projects/{project_id}/episodes/reorder
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| ordered_ids | body | int[] | 是 | - | 目标顺序的剧集 ID 清单 |

成功响应 `200`：

```json
{ "total": 3 }
```

业务口径与校验规则（三种 400）：

1. `ordered_ids` 含重复 ID → 400「ordered_ids 含重复剧集 ID」；
2. 含不属于该项目的剧集 ID → 400（附前 5 个非法 ID）；
3. 未恰好覆盖项目全部剧集（多删均不可）→ 400「未恰好覆盖项目全部剧集」。

**写入实现口径**：因 `uk_episodes_seq` 对 `(project_id, sequence)` 唯一，采用两阶段
写入——先全部置临时负值（`-id`）flush，再按目标顺序回写 `1..n`，避免逐行 UPDATE
触发唯一冲突。部分失败整事务回滚，序号不变。

| 错误码 | 场景 |
| --- | --- |
| 400 | 上述三种清单校验失败 |
| 404 | 项目不存在 / 非本人 |

### 1.4 更新剧集（PATCH）

```
PATCH /api/projects/{project_id}/episodes/{episode_id}
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| episode_id | path | int | 是 | - | 剧集 ID（须属于该项目） |
| title | body | string | 否 | None | 1~100 字符；None 不修改 |
| status | body | string | 否 | None | 白名单值；None 不修改 |

成功响应 `200`：同 1.1 结构（更新后的剧集）。

| 错误码 | 场景 |
| --- | --- |
| 400 | `status` 非白名单值（detail 列出合法值） |
| 404 | 项目或剧集不存在 / 不属于该项目 |
| 422 | title 超长 |

### 1.5 删除剧集（硬删 + 引用解绑）

```
DELETE /api/projects/{project_id}/episodes/{episode_id}
```

成功响应 `200`：

```json
{ "message": "剧集已删除", "unbound": 4 }
```

业务口径：

- **硬删** episodes 行，删除后不可恢复；
- `scripts` / `storyboards` / `film_outputs` 中 `episode_id` 指向该剧集的行**保留数据本体**、
  统一置 `NULL`（与外键 `ondelete=SET NULL` 语义一致，显式置 NULL 以兼容未声明数据库级
  外键的存量环境），`unbound` 为解绑的引用行总数（三表合计）。

| 错误码 | 场景 |
| --- | --- |
| 404 | 项目或剧集不存在 / 不属于该项目 |

---

## 2. 剧本一键同步（script，S-08，均幂等）

两个端点共享前置校验：剧本内容为空（无 `raw_text` 且无结构化 content）→ 400。
同步标记的幂等键统一为 **`project_id + script_version`**（取当前剧本 `version` 字段）。

### 2.1 剧本同步 Agent 会话

```
POST /api/projects/{project_id}/script/send-to-agent
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| thread_id | body | string | 否 | None | 复用既有会话（须属于当前用户，否则 404） |
| episode_id | body | int | 否 | None | 绑定剧集的剧本行（S-06） |

> 请求体可整体缺省（`req: Optional[...]`）。

成功响应 `200`：

```json
{ "thread_id": "u1_3f9a2c8d", "duplicated": false }
```

业务口径：

- **幂等**：`agent_threads.state.script_sync` 已记录同 `{project_id, script_version}`
  标记时，直接返回既有会话 ID 且 `duplicated=true`，不重复建会话、不重复插消息。
- 新建会话：`thread_id` 命名规则 `u{user_id}_{uuid前8位}`（与智能体对话缺省一致），
  `state.messages` 插入首条 user 消息（剧本摘要：标题/集数/梗概/三幕大纲/场景清单/
  角色名/原文前 200 字），`state.script_sync = {"project_id": …, "script_version": …}`，
  `stage="script"`。
- 复用会话（传 `thread_id`）：校验归属（不属于当前用户 → 404「会话不存在」）后**追加**
  摘要消息与同步标记，不覆盖既有消息。
- 消息同时落 `agent_threads.state` JSONB，`GET /api/agent/threads/{id}/messages` 可回放；
  LangGraph 检查点侧上下文由用户在会话内继续对话时自然建立。

| 错误码 | 场景 |
| --- | --- |
| 400 | 剧本内容为空 |
| 404 | 项目/剧集/剧本不存在，或传入 thread_id 不属于当前用户 |

### 2.2 剧本投递画布

```
POST /api/projects/{project_id}/script/send-to-canvas
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| canvas_id | body | int | 否 | None | 目标画布（须属于本人+该项目+未软删，否则 404） |
| episode_id | body | int | 否 | None | 绑定剧集的剧本行 |

成功响应 `200`：

```json
{ "canvas_id": 7, "node_id": 42, "duplicated": false }
```

业务口径：

- **幂等**：项目内已有 `node_type="text"` 且 `meta.script_sync` 命中同
  `{project_id, script_version}` 的节点（联表排除软删画布）→ 返回既有
  `canvas_id/node_id`，`duplicated=true`，不新建。
- 节点内容：`text` 类型节点，`data = {"text": 剧本摘要, "label": "剧本 · v{version}"}`，
  位置 (100, 100)（与分镜投递的 text 节点排布一致）。
- 目标画布选择：指定 `canvas_id` 校验归属；缺省取该项目**最近创建**的画布；项目尚无
  画布时自动创建标题「剧本画布」的 `standalone` 画布。

| 错误码 | 场景 |
| --- | --- |
| 400 | 剧本内容为空 |
| 404 | 项目/剧集/剧本/画布不存在或归属不符 |

---

## 3. 参考图锚点（assets，A-04）

### 3.1 字段编辑（PATCH 口径，挂 PUT 更新接口）

参考图锚点字段 `reference_image_url` 存在于 **characters / scenes** 两类资产
（props/styles 无此字段），经既有更新接口写入，语义为 **PATCH（仅更新传入字段）**：

```
PUT /api/projects/{project_id}/assets/characters/{character_id}
PUT /api/projects/{project_id}/assets/scenes/{scene_id}
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| reference_image_url | body | string\|null | 否 | None | 参考图 URL，≤500 字符；**None = 不修改**（传 `null` 语义是"不传"，非"清空"） |
| tags | body | string[]\|null | 否 | None | 见 §4.2 |

```json
{ "id": 5, "name": "林小满", "reference_image_url": "/storage/uploads/ref_5.png",
  "tags": ["主角", "现代装"], "...": "其余字段见 CharacterOut" }
```

| 错误码 | 场景 |
| --- | --- |
| 404 | 项目/资产不存在（角色改名冲突另见 409） |
| 422 | URL 超过 500 字符 |

### 3.2 生成任务透传链（meta.reference_image_url）

角色定妆照生成（`POST /api/projects/{project_id}/assets/characters/{id}/generate`）
读取该字段并**透传给图像渠道**，完整链路：

```
characters.reference_image_url
  → 任务 meta.reference_image_url（asset_service.create_character_image_task 写入）
  → 执行器读取（tasks/runner.py: meta.get("reference_image_url")）
  → 图像 provider（真实渠道 / mock 均接收 reference_image_url 参数）
```

- 字段为空时不写入 meta，生成走纯提示词路径；
- 生成任务类型为 `image`、冻结 10 积分（RD-28），计费与占位豁免见 §9。

---

## 4. 资产标签与批量删除（assets，A-05）

### 4.1 标签过滤（?tag=）

```
GET /api/projects/{project_id}/assets?tag=主角
GET /api/projects/{project_id}/assets?type=character&tag=主角&page=1&page_size=10
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| type | query | string | 否 | None | character/scene/prop/style；缺省返回四类聚合 |
| tag | query | string | 否 | None | 标签精确匹配，≤20 字符 |
| page / page_size | query | int | 否 | 1 / 10 | 仅指定 type 时生效（钳位 1~100） |

业务口径：

- 匹配条件为 **JSONB 包含**：`tags @> '["tag"]'::jsonb`（SQLAlchemy `tags.contains([tag])`）；
- `type` 缺省（聚合模式）时 tag 对**四类同时生效**，返回
  `{"characters":[…], "scenes":[…], "props":[…], "styles":[…]}`；
- 指定 `type` 时返回分页结构 `{"items","total","page","page_size"}`，软删行被排除
  （style 无软删字段不排除）。

| 错误码 | 场景 |
| --- | --- |
| 400 | `type` 取值非法 |
| 404 | 项目不存在 / 非本人 |

### 4.2 标签编辑（tags 字段）

characters / scenes / props / styles 四类更新接口均支持 `tags` 字段，语义为
**数组整体替换**（非增量合并）。入参经 `_validate_tags` 规范化：

- 必须为字符串数组，单项非空（strip 后非空，否则 422）；
- **去重保序**（首个出现位置生效）；
- 最多 **10 个**标签（超出 422）；
- 出参统一兜底：ORM 列为 NULL（存量行）时透出 `[]`。

### 4.3 批量删除

```
POST /api/projects/{project_id}/assets/{asset_type}/batch-delete
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| asset_type | path | string | 是 | - | 复数形式：characters/scenes/props/styles |
| ids | body | int[] | 是 | - | 去重保序后 **1~100** 个，越界 400（空列表也 400，非 422） |

成功响应 `200`：

```json
{
  "deleted": 2,
  "skipped": [
    { "id": 99, "reason": "资产不存在、不属于当前项目或已删除" }
  ]
}
```

业务口径：

- **软删语义**（characters/scenes/props）：与单删完全一致，仅置 `deleted_at=now`，
  不做引用保护（分镜关联引用不受影响），可恢复；
- **styles 例外**：无软删字段（也无单删端点），命中时**物理删除**（styles 表无外键引用）；
- 未命中（不存在 / 不属于当前项目 / 已软删）**不中断整体操作**，逐条计入 `skipped`
  并附统一原因文案（等价于单删场景的 404，批删不抛错）；
- 幂等性：重复提交同一批 ID，首轮 `deleted` 后次轮全部进入 `skipped`，无副作用。

| 错误码 | 场景 |
| --- | --- |
| 400 | asset_type 非法；ids 去重后数量为 0 或超 100 |
| 404 | 项目不存在 / 非本人 |

---

## 5. 分镜增强（storyboards，SB-06/07/08）

### 5.1 分镜表导出（xlsx / csv / pdf）

```
GET /api/projects/{project_id}/storyboards/episodes/{episode_id}/export?format=pdf
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| episode_id | path | int | 是 | - | 剧集 ID；**特殊值 0 = 导出项目全部分镜**（兼容语义） |
| format | query | string | 否 | xlsx | xlsx / csv / pdf |

成功响应 `200`：文件字节流（`StreamingResponse`）。

| format | 生成器 | 特性 |
| --- | --- | --- |
| xlsx | openpyxl | 表头加粗、列宽自适应（钳位 ≤60）；MIME `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` |
| csv | 标准库 csv | **utf-8-sig 带 BOM**，Excel 直接打开中文不乱码 |
| pdf | fpdf2（懒导入） | A4 横向表格 |

响应头 `Content-Disposition` 同时携带 ASCII 文件名与 RFC 5987 UTF-8 文件名
（`storyboards_ep{id}.{fmt}` / 全量时 `storyboards_all.{fmt}`）。

**PDF 字体降级（SB-08）**：中文字体探测失败时自动降级生成英文兜底版（不 500），
并在响应头标记：

```
X-PDF-Font-Fallback: true
```

业务口径：归属校验链 `episode → project → user`（`episode_id=0` 除外，直接全量）；
数据源与分镜列表接口同构（含场景名联查）。fpdf2 未安装时仅影响 pdf 格式（懒导入），
xlsx/csv 导出不受影响。

| 错误码 | 场景 |
| --- | --- |
| 400 | format 非 xlsx/csv/pdf（手动校验，**非 422**） |
| 404 | 项目或剧集不存在 / 非本人（episode_id=0 不校验剧集） |

### 5.2 连续性检查（SB-06）

```
POST /api/projects/{project_id}/storyboards/continuity-check
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| episode_id | body | int\|null | 否 | None | 剧集过滤；None = 项目全部分镜 |

成功响应 `200`：

```json
{
  "items": [
    {
      "storyboard_id": 12,
      "type": "scene_jump",
      "message": "相邻镜头场景跳变：室内·咖啡馆 → 室外·街道，但画面描述含连续词，请确认是否误拆场景",
      "confidence": "suspect"
    }
  ],
  "checked_pairs": 11
}
```

**info / suspect 置信口径**（`confidence` 字段取值，非数值而是分级标签）：

| 检查规则 | 判定条件 | confidence |
| --- | --- | --- |
| 场景跳变 | 相邻两镜 `scene_id` 均非空且不同 | `info`；两镜画面描述含强连续词白名单（如"继续/接着/然后"类）时升级 `suspect` |
| 服装变体冲突 | 相邻两镜同一角色绑定的服装变体不同（**两侧均绑定变体才判**，缺变体不判） | `suspect` |
| 角色提示词缺失 | 镜 N 台词出现角色（行首"角色名："格式）但镜 N+1 提示词不包含该角色 | `info` |

设计原则（RD-23）：**宁可漏报不误报**——条件不充分一律不报；无问题返回空 `items`。

| 错误码 | 场景 |
| --- | --- |
| 404 | 项目或剧集不存在 / 非本人 |

### 5.3 目标时长自动调整（SB-07）

```
POST /api/projects/{project_id}/storyboards/auto-fit-duration
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| target_seconds | body | int | 是 | - | 目标总时长，**1~3600 秒** |
| episode_id | body | int\|null | 否 | None | 剧集过滤；None = 项目全部分镜 |

成功响应 `200`：

```json
{
  "before_total": 40,
  "after_total": 30,
  "target_seconds": 30,
  "changed": true,
  "per_shot": [
    { "id": 11, "before": 20, "after": 15 },
    { "id": 12, "before": 20, "after": 15 }
  ]
}
```

业务口径：

- **等比缩放**：`新时长 = max(1, round(原时长 × target / 当前合计))`，**单镜保底 1 秒**；
- 调整后落库（`storyboards.duration` 逐镜更新），`after_total` 为落库后实际合计
  （各镜保底取整后可能与 target 有 ±n 秒误差）；
- `per_shot` 返回逐镜前后对照，`changed=false` 表示无需调整（合计已等于目标）。

| 错误码 | 场景 |
| --- | --- |
| 400 | target_seconds 越界（<1 或 >3600）或无分镜可调整 |
| 404 | 项目或剧集不存在 / 非本人 |

---

## 6. 生成侧（generation）

### 6.1 视频参数（VideoCreateRequest，P-12）

```
POST /api/projects/{project_id}/storyboards/{storyboard_id}/video
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| use_end_frame | body | bool | 否 | false | 尾帧控制：true 时该分镜 `end_frame_url` 必须已生成，否则 **400** |
| resolution | body | string | 否 | "720p" | 枚举 480p / 720p / 1080p（Literal，非法 422） |
| fps | body | int | 否 | 24 | 枚举 24 / 30（Literal，非法 422） |
| motion_strength | body | string | 否 | "medium" | 枚举 low / medium / high（Literal，非法 422） |

成功响应 `200`：`TaskOut`（任务详情）。任务 `meta` 记录全部参数：

```json
{
  "id": 88, "type": "video", "status": "queued",
  "cost_points": 45, "placeholder": false,
  "meta_use": "meta 内含：use_end_frame / resolution / fps / motion_strength / first_frame / end_frame",
  "result_url": null, "error": null, "progress": 0
}
```

业务口径：

- 成本 = 每秒单价 × 分镜时长（详见计费模块）；
- `meta.first_frame` 恒记首帧地址；`meta.end_frame` 仅 `use_end_frame=true` 时记尾帧
  地址（false 时为 None）——首/尾帧地址供执行器透传视频渠道（图生视频/首尾帧控制）；
- 积分不足 409（余额不足）。

| 错误码 | 场景 |
| --- | --- |
| 400 | `use_end_frame=true` 但该分镜尾帧尚未生成 |
| 403 / 404 | 归属校验失败 |
| 409 | 积分余额不足 |
| 422 | resolution/fps/motion_strength 取值非法 |

### 6.2 尾帧图像生成（P-03/P-08）

```
POST /api/projects/{project_id}/storyboards/{storyboard_id}/end-frame
```

无请求体。成功响应 `200`：`TaskOut`（`type=image` 任务，`meta` 带
`end_frame=true` 与 `target_type=storyboard_end_frame`）。

业务口径：

- 成本 10 积分/张；
- **前置依赖**：分镜 `keyframe_url` 非空（尾帧以关键帧画面为基准延续生成），
  未生成 → 400「该分镜尚未生成关键帧…」；
- **独立防重**：仅检查 `meta.end_frame=true` 的 queued/running 图像任务（409），
  与关键帧任务互不阻塞；
- 任务成功后由执行器回写 `storyboards.end_frame_url`。

| 错误码 | 场景 |
| --- | --- |
| 400 | 分镜尚无关键帧 |
| 403 / 404 | 归属校验失败 |
| 409 | 同分镜已有进行中的尾帧任务，或积分不足 |

### 6.3 生成结果上传替换（P-06）

```
POST /api/tasks/{task_id}/replace
Content-Type: multipart/form-data
```

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| file | form-data | file | 是 | - | 图片文件：**png / jpg / jpeg / webp，≤10MB** |

成功响应 `200`：更新后的 `TaskOut`（`result_url` 为新文件地址）。

业务口径与约束：

- **准入**：仅 `type=image` 且 `status=done` 的任务可替换（否则 400）；
- 落盘 `storage/replaced/{yyyy-mm}/`，回写 `task.result_url`；
- **meta 留痕**（替换可追溯）：

```json
{
  "replaced_from": "/storage/generated/placeholder_keyframe_5.svg",
  "replaced_at": "2026-10-12T09:30:00+00:00",
  "replaced_by_upload": true
}
```

- **关键帧联动**（替换后同步更新分镜，前端缩略图取同字段）：
  - 尾帧任务（`meta.target_type=storyboard_end_frame` 或 `meta.end_frame`）→
    回写 `storyboards.end_frame_url`（不混用）；
  - 其余关联分镜的 image 任务 → 回写 `storyboards.keyframe_url` 并置
    `storyboards.status="done"`。

| 错误码 | 场景 |
| --- | --- |
| 400 | 非 image 任务；状态非 done；文件格式/大小非法 |
| 403 | 任务所属项目不属于当前用户 |
| 404 | 任务不存在 |
| 422 | 未携带 file 字段 |

---

## 7. 发布（publish / compose）

### 7.1 成片合成参数（ComposeCreateRequest）

```
POST /api/projects/{project_id}/compose
```

核心字段（完整见 `schemas/publish.py`）：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| resolution | Literal | "720P" | 720P / 1080P / 2K |
| aspect_ratio | Literal\|null | None | 9:16 / 16:9 / 1:1；缺省沿用项目配置 |
| mode | Literal | "full" | incremental（增量，须传 storyboard_ids）/ full |
| storyboard_ids | int[]\|null | None | 仅 incremental 生效（≤200 个）；含非法 ID → 400 |
| episode_id | int\|null | None | 传入时仅合成该剧集分镜，成片版本绑定该剧集（不属于该项目 → 404） |
| subtitles | bool | false | 是否烧录字幕 |
| subtitle_style | SubtitleStyle\|null | None | 字幕样式（见下） |
| bgm | BgmOption\|null | None | `{url, volume(0~1,默认0.3)}` |
| transition | TransitionOption\|null | None | `{type, duration(0.1~3.0,默认0.5)}` |

**字幕双语（P-16）**：`subtitle_style.bilingual=true` 时原文在上 + 英文译文在下。
**降级口径**：LLM 批量翻译超时 15s / 调用失败 → **自动降级为单行原文**（不阻断，
合成任务照常成功），字幕样式其余字段（font_size 10~72 / color ASS 格式 / position
bottom/middle/top）不受影响。

**转场类型映射（P-17）**：`transition.type` 经白名单映射为 ffmpeg `xfade` 滤镜名：

| 请求值 | xfade 滤镜 | 含义 |
| --- | --- | --- |
| fade | fade | 淡入淡出（**默认与回退值**） |
| dissolve | dissolve | 叠化 |
| fadeblack | fadeblack | 闪黑 |
| fadewhite | fadewhite | 闪白 |
| 其余任何取值 | **fade（回退）** | 大小写不敏感、去首尾空白后匹配 |

转场仅当请求携带 `transition` 且片段数 ≥2 时启用 xfade 链重编码，否则直接 concat 拼接。

### 7.2 闸门拦截口径（合成 / 发布）

- **闸门二（合成前）**：汇总本次涉及分镜的 generate 机审记录——任一 `violate` 且未
  人工 approved → 400 拦截；任一 `suspect` 且未处置（manual_status 为 NULL/pending）→
  400 拦截；通过后落 `compose` 通过记录（`method=gate`）并创建合成任务（成本 200 积分）。
- **闸门三（发布前）**：汇总项目**全部闸门**审核记录，判定口径同上；通过后落
  `publish` 通过记录并生成 `share_token`。

相关错误码：合成 400（无分镜/增量清单非法/闸门二拦截）、409（积分不足）；
发布 400（闸门三拦截）、409（积分/状态冲突透传）；分享撤销 409（项目无有效分享链接）；
分享页 `GET /api/share/{token}` 免认证：404（令牌无效/已撤销）、410（已过期）、
403（提取码错误，请求头 `X-Share-Password`）。

---

## 8. 画布节点沉淀入资产库（canvases，A-10/CV-24）

```
POST /api/canvas/nodes/{node_id}/sink
```

> 独立于 `/api/canvases` 前缀：沉淀以**节点**为操作主体，归属经
> `node → canvas → user` 链路校验，无需客户端传画布 ID。

| 参数 | 位置 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- | --- |
| asset_type | body | string | 是 | - | character / scene / prop（**image 节点不支持 style**） |
| name | body | string\|null | 否 | None | 资产名；缺省取节点 label，再缺省 `[画布] 节点{id}`（≤50 字符） |

成功响应 `201`：

```json
{ "asset_id": 15, "asset_type": "character", "duplicated": false }
```

业务口径：

- **准入**：仅 `image` 类型节点（非 image → 400）；节点须有产物 URL
  （`data.output` 优先、其次 `data.url`，仅接受 `http(s)://` 或 `/` 开头，无产物 → 400）；
  独立画布（未关联项目）→ 400「请先将画布关联到项目」；
- **幂等（meta 留痕）**：节点 `meta.sinked_asset_type` 与 `meta.sinked_asset_id`
  记录沉淀结果；已沉淀同类型且该资产仍存在（未软删）→ 直接返回既有资产 ID 且
  `duplicated=true`，不重复建资产；
- **source 标记**：新建资产行统一 `source="canvas"`（区别于抽取入池 `manual` 等），
  `asset_url` 指向画布产物 URL，`description` 取节点描述文本（可空）；
- **名称去重**：项目内同名自动追加 `_1/_2…` 后缀（characters/scenes 有
  `(project_id, name)` 唯一索引）；
- **visual_prompt 自动补全降级链（RD-47）**（列 NOT NULL，须保证始终有值）：

```
LLM 生成标准化视觉描述（80 字内，超时 15s / 调用失败 / 输出为空）
  → 降级 1：节点描述文本（data.description → data.text → data.prompt → data.label → config.label）
  → 降级 2（描述也为空）：默认模板「画布图像「{name}」：漫画风格，构图完整，光影统一。」
```

  降级**不阻断**沉淀主流程。

| 错误码 | 场景 |
| --- | --- |
| 400 | asset_type 非法 / 非 image 节点 / 节点无产物 URL / 独立画布未关联项目 |
| 404 | 节点不存在或画布不属于当前用户（含软删画布） |

---

## 9. 占位治理口径（全局）

> 演示占位治理（D3）：真实模型渠道**全部失败**时，降级链尾固定追加本地 mock 哨兵
> 渠道（channel_id=0）兜底出占位资源，业务不中断。占位产物不向用户收费。

### 9.1 标记链路

```
mock provider 返回 result.meta.placeholder=true
  → 执行器判定（_is_placeholder_result：provider meta 或 mock 哨兵渠道命中）
  → 任务 meta 平铺写入 {"placeholder": true, "mock": true, "provider": "mock", ...}
  → API 透出：TaskOut.placeholder = bool(meta.placeholder)
```

### 9.2 产物命名约定

三类 mock 产物文件名统一带 **`placeholder_` 前缀**，画面/音频内可感知占位
（图像/视频叠加「演示占位」标注）：

| 类型 | 文件名模式 | 说明 |
| --- | --- | --- |
| 图像 | `placeholder_keyframe_{seed}_{ts}.svg` | SVG 占位帧 |
| 视频 | `placeholder_clip_{seed}.mp4`（ffmpeg 可用）/ `placeholder_poster_{seed}_{ts}.svg` | 有 ffmpeg 生成真实容器视频并叠加占位字幕，否则 SVG 海报 |
| TTS | `placeholder_tts_{ts}_{hash}.wav` | 静音/提示音 |

### 9.3 计费豁免口径

任务成功落库时（`runner._succeed_task`）：

- `meta.placeholder=true` 且 `cost_points > 0` → **不扣费**，冻结积分**全额退回**可用余额：
  - 退款幂等键：`ph-refund-{task_id}`（独立前缀，与失败/取消退款 `task_{id}_refund`
    互不冲突）；
  - 承担方：`bear="platform"`（平台承担，非用户过失）；
  - **流水备注**：`演示占位豁免：任务 {id}（{type}）产物为演示占位，冻结积分退回`
    （流水类型为退款，钱包明细可按备注检索豁免记录）；
- 其余任务照常 `consume_frozen`（扣费幂等键 `task_{id}_consume`）。

### 9.4 渠道未配置启动告警

启动时逐渠道检查 API Key：为空仅记 warning（「模型渠道 X 未配置 API Key，对应能力
将走演示降级链」），**不阻断启动**；该渠道能力调用时沿降级链落到 mock 占位。

---

## 10. 密钥配置（backend/.env.example 变量清单）

> 模板文件 `backend/.env.example`：复制为 `backend/.env` 使用（pydantic-settings 自动
> 读取，cwd=backend）。**安全基线（D1）：任何真实密钥不得写入代码仓库**，模板中一律
> `your-key-here` 占位。环境变量名不区分大小写。未配置的渠道不阻断启动。

### 数据库

| 变量 | 用途 |
| --- | --- |
| `DATABASE_URL` | 业务库连接串（PostgreSQL + asyncpg） |
| `ADMIN_DATABASE_URL` | 管理库连接串（连 postgres 系统库，仅启动自动建业务库用） |

### 认证 / JWT

| 变量 | 用途 |
| --- | --- |
| `JWT_SECRET_KEY` | JWT 签名密钥（生产必换高强度随机串） |
| `JWT_ALGORITHM` | JWT 签名算法（默认 HS256） |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 访问令牌有效期（分钟，默认 10080 = 7 天） |
| `REFRESH_TOKEN_EXPIRE_DAYS` | 刷新令牌有效期（天，默认 30） |

### 模型渠道密钥（5 渠道）

| 渠道 | 变量 | 用途 |
| --- | --- | --- |
| DeepSeek（文本主渠道） | `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` | 文本 LLM（OpenAI 兼容协议） |
| Agnes（文本兜底） | `AGNES_LLM_BASE_URL` / `AGNES_LLM_API_KEY` / `AGNES_LLM_MODEL` | 兜底文本 LLM |
| Agnes 图像 | `AGNES_IMAGE_BASE_URL` / `AGNES_IMAGE_API_KEY` / `AGNES_IMAGE_MODEL` | 图像生成 |
| Agnes 视频 | `AGNES_VIDEO_BASE_URL` / `AGNES_VIDEO_API_KEY` / `AGNES_VIDEO_MODEL` / `AGNES_VIDEO_MODE` | 视频生成（MODE 官方枚举 text/keyframe/reference，带首帧图自动切 keyframe） |
| Agnes 视觉机审 | `AGNES_VISION_MODEL` | 图像/视频产物真实视觉机审（OpenAI 兼容图文混合协议） |
| 百炼 TTS | `BAILIAN_TTS_BASE_URL` / `BAILIAN_TTS_API_KEY` / `BAILIAN_TTS_MODEL` | 语音合成（WebSocket 网关；Key 与 wan 视频共用） |
| 百炼视频 | `BAILIAN_WAN_BASE_URL` / `BAILIAN_WAN_MODEL` | wan2.7-i2v 图生视频（DashScope 原生异步协议根域名，非 /compatible-mode） |

### 异步视频轮询

| 变量 | 用途 |
| --- | --- |
| `VIDEO_POLL_INTERVAL_SECONDS` | 异步视频任务轮询间隔（秒，默认 5） |
| `VIDEO_POLL_TIMEOUT_SECONDS` | 轮询总超时（秒，默认 300） |

### 安全与存储

| 变量 | 用途 |
| --- | --- |
| `AES_KEY` | 渠道 API Key 的 Fernet/AES 加密密钥（16/24/32 字节，生产必换） |
| `STORAGE_DIR` | 本地文件存储根目录（默认 backend/storage） |
| `CORS_ORIGINS` | 允许跨域来源列表（**JSON 数组格式**，`["*"]` 不限） |
| `PLUGIN_ALLOWED_HOSTS` | 画布插件 endpoint_url 信任域白名单（JSON 数组；空 = 仅强制内网黑名单） |

### 业务开关

| 变量 | 用途 |
| --- | --- |
| `PUNISHMENT_ENABLED` | 违规分级处罚准入拦截（默认 false；上线后置 true 启用） |
| `ALEMBIC_ON_STARTUP` | 启动时先执行 `alembic upgrade head` 再 create_all 兜底（默认 false） |

### LangSmith 追踪

| 变量 | 用途 |
| --- | --- |
| `LANGSMITH_TRACING` | 追踪开关（true/1 启用，默认 false 零开销） |
| `LANGSMITH_API_KEY` | LangSmith API Key（`lsv2_` 前缀，启用时必填） |
| `LANGSMITH_PROJECT` | trace 上报目标项目名（默认 mangaflow） |

---

## 11. 性能基线与索引评估（9.2 核查附录）

### 11.1 新列查询模式评估

| 列 / 查询 | 索引现状 | 结论 |
| --- | --- | --- |
| `episodes.project_id` 过滤（列表按序号排序） | `idx_episodes_project(project_id)` **已确认存在**；另有 `uk_episodes_seq(project_id, sequence)` 唯一索引同时覆盖过滤与排序 | 索引完备，无需动作 |
| `characters/scenes/props/styles.tags` JSONB `@>` 过滤（?tag=） | **无 GIN 索引**（tags 列为普通 JSONB） | 当前规模不需要；见下方阈值建议 |
| `characters/scenes.reference_image_url` | 无索引 | 仅单行 PATCH 读改写与生成时单点读取，不作为过滤条件，**不需要索引** |
| `canvas_nodes.meta` 幂等查重（script_sync / sinked_asset_*） | 无索引 | 查重按 node 主键单点读写，聚合模式仅在画布图加载后遍历，**不需要索引** |

**?tag= GIN 索引评估结论**：资产表当前数据量为万级以内（单项目资产数十至数百行，
标签查询在 `project_id` 索引收敛后顺序过滤的行数极少），JSONB `@>` 顺序扫描开销
可忽略——**当前规模不需要建 GIN 索引**。建议阈值：单表资产行数超过 **5 万**（或
tag 过滤出现在高频列表路径且 P95 超过 100ms）时，为四类资产的 `tags` 列追加 GIN
索引（`jsonb_path_ops` 操作符类更小更快，恰好匹配 `@>` contains 查询）：

```sql
-- DDL 示例（仅在数据量超阈值后评估执行，当前不执行）
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_characters_tags_gin
    ON characters USING gin (tags jsonb_path_ops);
-- scenes / props / styles 同理
```

### 11.2 列表接口分页 / 限参核查

| 接口 | 分页 | 说明 |
| --- | --- | --- |
| `GET /api/projects/{id}/episodes` | **不分页**（合理） | 一项目剧集数量天然有限（十级以内），全量返回按序号排序 |
| `GET /api/projects/{id}/storyboards` | **不分页**（合理） | 单项目分镜数十至数百，`episode_id` 可过滤 |
| `GET /api/projects/{id}/assets?type=…` | **分页**（沿用既有） | `normalize_pagination` 钳位 page_size∈[1,100]；聚合模式（type 缺省）为四类全量，与前端既有结构兼容 |
| `GET /api/projects/{id}/tasks` / `outputs` / `reviews` | **分页**（沿用既有） | 钳位 1~100，按创建时间/版本号倒序 |
| `GET /api/canvases` | **分页** | 节点/连线计数批量聚合查询，无 N+1 |

### 11.3 响应时间基线（本地 dev 口径）

**测量方式**（两条既有通道，均未新增埋点）：

1. **Prometheus 指标**：`GET /api/metrics`（免鉴权）输出的
   `mangaflow_http_request_duration_seconds` 直方图（分桶 0.05 / 0.1 / 0.5 / 1.0 / 3.0s，
   纯 ASGI 中间件采集），按接口聚合 P50/P95；
2. **pytest 内联计时**：本轮测试以异步 fixture 驱动真实接口调用，可在用例内
   `time.perf_counter()` 内联测量（本轮未开启 `--durations`，以下为按指标分桶与
   mock 链路耗时的**量级估算**）。

**典型耗时参考（本地单机，mock provider 即时完成，无真实模型网络调用）**：

| 接口类别 | 量级参考 |
| --- | --- |
| 列表类（episodes / assets / tasks / outputs） | **< 50ms**（归属校验 1~2 次点查 + 索引过滤） |
| 任务创建类（keyframes / video / tts / end-frame / replace） | **< 100ms**（含积分冻结事务 + 入队；replace 另含 ≤10MB 文件落盘，量级仍 <100ms） |
| 幂等同步类（send-to-agent / send-to-canvas / sink） | < 100ms（JSONB 查重 + 单行写入） |
| 导出类（xlsx / csv / pdf，百镜以内） | 100~500ms（内存构建 + 字节流） |
| 重操作（compose / decompose 任务本身） | 异步任务执行，API 创建 <100ms；执行耗时取决于 provider（mock 即时、真实渠道走轮询） |

> **口径声明**：以上为本地单机（Windows 开发机、PostgreSQL 16 本地实例、mock provider）
> 的估算量级，**非生产压测结论**；pytest 全量回归中单用例均值约 30s 属 fixture 环境
> （建库/种子/事件循环）开销，不代表接口响应时间。生产口径需以 `/api/metrics` 直方图
> 在真实流量下重新标定。

---

## 12. 变更记录

### 2026-10-12（本轮：创作链路缺口补全）

| 模块 | 新增接口 | 要点 |
| --- | --- | --- |
| 剧集管理（S-06） | `POST/GET /api/projects/{pid}/episodes`、`PUT …/reorder`、`PATCH/DELETE …/{episode_id}` | sequence 自动递增 + 两阶段重排避唯一冲突；硬删解绑引用（unbound 留痕） |
| 剧本同步（S-08） | `POST …/script/send-to-agent`、`POST …/script/send-to-canvas` | 幂等键 project_id+script_version；复用会话 / 缺省建「剧本画布」 |
| 参考图（A-04） | `PUT …/assets/characters/{id}`、`PUT …/assets/scenes/{id}` 增补 `reference_image_url` | 生成任务 `meta.reference_image_url` 全链透传图像渠道 |
| 标签/批删（A-05） | `GET …/assets?tag=`、四类更新接口 `tags` 字段、`POST …/assets/{type}/batch-delete` | JSONB @> 匹配；数组整体替换（≤10 去重保序）；软删 + skipped 结构（styles 物理删） |
| 分镜导出（SB-08） | `GET …/storyboards/episodes/{eid}/export?format=pdf` | fpdf2 A4 横向；中文字体降级 + `X-PDF-Font-Fallback: true`；episode_id=0 全量兼容 |
| 连续性检查（SB-06） | `POST …/storyboards/continuity-check` | info/suspect 分级口径；宁可漏报不误报；checked_pairs 留痕 |
| 时长调整（SB-07） | `POST …/storyboards/auto-fit-duration` | 等比缩放 + 单镜保底 1s；per_shot 前后对照 |
| 视频参数（P-12） | `POST …/storyboards/{sid}/video` 增补 `resolution/fps/motion_strength` | use_end_frame 尾帧控制（未生成 400）；meta 全参数记录 |
| 尾帧生成（P-03/08） | `POST …/storyboards/{sid}/end-frame` | 前置关键帧依赖（400）；独立防重（409）；成功回写 end_frame_url |
| 结果替换（P-06） | `POST /api/tasks/{id}/replace` | multipart 一步上传；meta.replaced_from/at/by_upload 留痕；关键帧/尾帧联动回写 |
| 双语字幕（P-16） | `POST …/compose` 增补 `subtitle_style.bilingual` | LLM 翻译 15s 失败降级单行原文，不阻断合成 |
| 转场映射（P-15/17） | `POST …/compose` 增补 `transition` | 白名单 fade/dissolve/fadeblack/fadewhite，其余回退 fade；≥2 片段走 xfade 链 |
| 画布沉淀（A-10/CV-24） | `POST /api/canvas/nodes/{node_id}/sink` | source=canvas；meta 幂等；visual_prompt 三级降级链（LLM→节点描述→默认模板） |
| 占位治理（D3） | 全局口径（无新端点） | meta.placeholder 打标 + placeholder_ 文件名前缀 + 计费豁免（ph-refund-{id} 全额退回、备注「演示占位豁免」） |
| 密钥治理（D1） | `backend/.env.example` | 全渠道变量清单模板化，真实密钥零落库（详见 §10） |
