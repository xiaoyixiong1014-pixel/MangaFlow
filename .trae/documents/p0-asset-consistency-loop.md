# 资产一致性流程闭环实施计划（P0-1/P0-2/P0-3/P0-4 + P2 快赢）

## Context

资产管理数据模型已到位（四类资产表 + 变体表 + embeddings），但缺三件事的流程闭环：
1. **P0-1 锁定门禁**：`face_locked` 只是字段，分镜生成入口不校验"不定妆不准进分镜"。
2. **P0-2 抽取静默失败**：实体抽取场景/道具为 0 无任何反馈（规则兜底无场景规则；LLM 键名漂移被静默清空）。
3. **P0-3 版本透传**：资产图替换/重生成后，已引用该资产的分镜无感知，做不到"一处更新全链路生效"。
4. **P0-4 视觉一致性**：现有 consistency_check 是纯文本相似度，防不了"生成的脸不像"。

附带两个 P2 快赢（真实缺陷）：generate 幂等键含随机段，双击重复扣 10 积分；styles 无软删与其他三类不一致。

## 已确认的代码事实

- 迁移机制三处同步：ORM 模型 + `backend/app/core/init_db.py` `_EXTRA_COLUMNS`（L428 起，单一事实来源）+ alembic revision（最新 `a3f7c2e9d4b1`）。
- 分镜生成入口：`backend/app/api/storyboards.py` L71 `POST /storyboards/decompose` → `storyboard_service.decompose_storyboards`（L378，L451 删除既有分镜——**门禁必须在删除之前**）。
- 分镜引用：`StoryboardCharacter(character_id/variant_id, RESTRICT)`、`Storyboard.scene_id`，均无版本概念。
- 版本递增点：`backend/app/tasks/runner.py` L916-956 图像任务成功回写 `asset_url` 处（character/scene/prop/variant 四分支）。
- 角色分级：`characters.role_type`（lead/support/villain=主要，extra=次要，NULL 保守视为主要）。
- 视觉模型：`backend/app/providers/vision_review.py` 已接 Agnes（OpenAI 视觉协议），可复用。
- `asset_embeddings`（`backend/app/models/agent.py` L88-110）：asset_type/asset_id/embedding(JSONB)/model。
- 抽取链路：`script_service.py` L175-184 提示词已含三段；`rule_extract`（L1533）硬编码 scenes=[]；`_normalize_entities`（L1502）只认精确键名；`llm_extract` 失败静默降级。
- generate 幂等缺陷：`asset_service.create_character_image_task` L166 `img_{id}_{uuid4().hex[:8]}`。
- 前端：`StoryboardView.vue`（分镜）、`AssetsView.vue`（资产）、`EntityExtractView.vue`（抽取）、`api/studio.ts`。

## 决策点（默认采用推荐项，可改）

1. **P0-4 路线**：采用 **Agnes 视觉档案路线**（生成后提结构化面部档案 JSON → 档案文本 embedding 入库 → 余弦比对）。理由：零新增依赖、动漫图适配优于 InsightFace 类方案、档案人可读。默认**影子模式**（只记录 score 不拦截），阈值 0.70，积累样本后经回测再开强拦截。
2. **P2 纳入范围**：纳入 generate 防重复 + styles 软删统一；不纳入队列位置展示、pgvector 索引迁移（`main.py` 已有 ensure_pgvector_schema 覆盖）。

## 批次 A：数据模型与迁移

**ORM 模型**（`backend/app/models/asset.py`、`storyboard.py`、`agent.py`）：
- `characters/scenes/props/asset_variants` 各加 `asset_version: Integer, server_default="1"`（props 一并加，runner 同样回写 prop 图）。
- `StoryboardCharacter` 加 `character_version/variant_version: Optional[int]`；`Storyboard` 加 `scene_version: Optional[int]`。NULL=旧数据视为一致，防存量全量误报。
- `AssetEmbedding` 加 `kind: String(20), server_default="text"`（text/face），CHECK 约束 + 索引 `idx_ae_asset_kind(asset_type, asset_id, kind)`。
- `characters` 加 `face_profile: JSONB, nullable=True`。
- `styles` 加 `deleted_at: TIMESTAMPTZ, nullable=True`。

**迁移**：`init_db.py` `_EXTRA_COLUMNS` 追加全部补列 + 索引 + CHECK（仿 L631 先 DROP 后 ADD 模式）；新增 alembic revision（down_revision=`a3f7c2e9d4b1`）。三处一次提交。

**config**（`backend/app/core/config.py`）：`face_consistency_threshold: float = 0.70`、`face_consistency_enforce: bool = False`。

验证：全量 pytest 回归；alembic upgrade/downgrade 幂等。

## 批次 B：P0-2 抽取修复

**`backend/app/services/script_service.py`**：
- `rule_extract` 补场景规则：标记行正则（`内景/外景`、`场景：`、`第X场`）+ 场景后缀词表兜底（咖啡馆/教室/办公室等约 40 词，命中≥2 次取 Top8，confidence=0.4 自动带 low_confidence），与角色名/道具重叠的丢弃。
- `_normalize_entities`：键名别名归并（locations/places→scenes，items→props 等）+ 探测 `entities` 嵌套层。
- `llm_extract` 失败与三组全空时 `logger.warning`。
- `extract_entities` 返回增加 `stats: {characters, scenes, props, source: "llm"|"rule", source_text_chars}`。

**前端 `EntityExtractView.vue`**：Tab 上方加 `el-alert` 统计行「解析出角色 X · 场景 Y · 道具 Z（来源：AI 模型/规则兜底）」，scenes==0 时 warning 态提示"未识别到场景实体，分镜将无法强绑场景资产"。

验证：新增 `tests/test_script_extract.py`（规则抽场景、键名归并、stats 字段）；前端 build。

## 批次 C：P0-1 锁定门禁 + generate 防重复

**`backend/app/services/storyboard_service.py`** 新函数：
```
check_lock_readiness(db, project) -> List[Dict]
# 主要角色 = role_type IN (lead,support,villain) OR NULL；extra 放行
# 未就绪按序判定：无图且pending→"未生成定妆照"；failed→"生成失败"；
#                 generating→"生成中"；face_locked=False→"未锁定"
```

**`decompose_storyboards`** 签名加 `skip_lock_check: bool = False`；在无剧本 400 之后、删除既有分镜（L451）之前插入门禁：未就绪非空 → 409，body `{code:"asset_lock_not_ready", message, unready:[...]}`。

**`backend/app/api/storyboards.py`** decompose 端点加可选 body `DecomposeRequest {skip_lock_check: bool=False}`（schemas/storyboard.py），向后兼容空 body。

**前端 `StoryboardView.vue`**：decompose 409 时弹 Dialog——未就绪角色表格（名/定位/状态标签）、「去资产页处理」按钮、快速模式勾选框 + 「跳过并继续生成」（重发 `skip_lock_check:true`）。

**防重复扣积分**（`asset_service.create_character_image_task` 开头）：查同 character 存在 `queued/running` 的 image 任务 → 409「该角色已有进行中的生成任务」。

验证：新增 `tests/test_storyboard_gate.py`（未锁定 409+清单、全锁定 200、skip 绕过、二次 generate 409）。

## 批次 D：P0-3 版本透传

**版本递增**：
- `runner.py` 回写块四分支：`if obj.asset_url: obj.asset_version = (obj.asset_version or 1) + 1`（首次生成保持 1，替换/重生成才 +1）。
- `assets.py` 三个 PUT 端点检测 `asset_url` 变更分支递增。

**快照写入**：`decompose_storyboards` 创建 `StoryboardCharacter` 时写 `character_version=ch.asset_version or 1`、命中场景时写 `scene_version`（`_match_scene` 需改为同时返回资产对象）；`update_storyboard` 的 character_links 替换分支同步写。

**过期判定**：`list_storyboards` 序列化内联计算（零额外查询，优于独立 stale 接口）：每镜追加 `stale: bool` + `stale_reasons: ["角色「X」定妆照已更新", ...]`。判定逻辑抽模块级纯函数 `compute_stale_ids` 共用。

**批量重渲染**：新增 `POST /storyboards/render-stale`（注册在 `{storyboard_id}` 静态段之前）——找 stale 镜头逐镜复用 generation_service 关键帧生成函数（**不复用 /regen**，它只是计数端点），跳过已有 queued/running 关键帧任务的镜头，返回 `{queued, skipped, storyboard_ids}`。

**前端 `StoryboardView.vue`**：stale 卡片黄色边框 + 「需重渲染」tag + reasons tooltip；工具栏 stale 计数>0 时显示「N 个镜头需重渲染」按钮 → renderStale → 刷新列表。

验证：新增 `tests/test_asset_version.py`（首次=1、重生成=2、快照写入、改图后 stale=True、render-stale 入队数）。

## 批次 E：P0-4 视觉一致性

**新文件 `backend/app/services/face_profile.py`**：
- `build_face_profile(db, character, image_url)`：调 Agnes 提档案 JSON（性别/年龄感/脸型/发色/瞳色/眉/唇/肤色/特征标记等固定键）→ 写 `characters.face_profile` + `upsert_asset_embedding(..., kind='face')`；全程吞异常不阻塞主链路。
- `compare_face_profile(db, character, image_url) -> Optional[float]`：新图提档案 → 文本 embedding → 与库中 kind='face' 主档案余弦；无主档案返回 None（首次生成直接建档放行）。
- `profile_to_text(profile)`：固定键序拼接，保证同特征同文本。

**`vision_review.py`** 加 `review_face_profile(image_url)` 方法（复用现有 Agnes 客户端）。

**`upsert_asset_embedding`**（asset_service.py）加 `kind: str = "text"` 参数，按 `(asset_type, asset_id, kind)` 定位。

**`runner.py`** 图像回写 character 分支后追加 `_face_consistency_gate`：
- 影子模式（默认）：仅写 `task.meta["face_score"]` 供回测；
- enforce=True 且 score<阈值：未重试过 → 自动重抽一次（meta["consistency_retry"]=1，重新冻结积分，余额不足走 `_fail_task` 退款）；已重试 → 任务 failed（consistency_failed）。
- 变体回写同样与 owner 主形象比对。

阈值回测：影子模式积累 `task.meta.face_score` + 离线构造同角色/跨角色档案各 20+ 正负样本出分布，累计 50+ 样本后校准再开 enforce。

验证：新增 `tests/test_face_profile.py`（mock Agnes 建档 kind='face'、影子模式记录不拦截、enforce 时 failed）。

## 批次 F：styles 软删统一

- 模型已加列（批次 A）；`assets.py` batch-delete（L945）删掉 styles 物删特判、styles 列表查询补 `deleted_at IS NULL` 过滤、`ensure_unique_name` 覆盖 styles。
- 补 styles 软删测试用例。

## 实施顺序与通用验证

A → B（可与 A 并行）→ C → D → E → F。每批完成：
- `cd backend && .venv/Scripts/python -m pytest`（现有 test_assets.py、test_asset_tags.py 全绿）
- `cd mangaflow-web && npm run build`（vue-tsc 类型检查）

## 关键风险

1. 三处迁移同步遗漏 → 批次 A 一次提交全部。
2. decompose 409 必须在删除既有分镜之前。
3. NULL role_type 全量拦截 → 弹窗文案说明 + 快速模式兜底；反馈过强可一行降级为仅提示。
4. Agnes 调用时延 → 钩子吞异常；实测 >5s 则后台化（asyncio.create_task）。
5. 重抽连环失败 → consistency_retry 上限 1 次；强拦截默认关闭。
