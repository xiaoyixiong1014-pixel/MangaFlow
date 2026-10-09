/**
 * Studio 工作流 API（文件级注释）。
 *
 * 覆盖三组接口（路径以 /api/projects/{project_id} 为前缀）：
 * - script：剧本输入/保存/SSE 生成/保存剧本/体检/实体抽取与确认
 * - assets：四类资产、角色定妆生成/更新/删除、锁定、变体、一致性校验、跨项目总览
 * - storyboard：拆解/列表/更新/重排/拆分/合并/告警/重生成/投递画布
 *
 * 说明：路径与方法已与后端 backend/app/api/{script,assets,storyboards}.py 实际路由对齐：
 * - script/health 为 POST（规则体检会回写体检结果）
 * - storyboards/warnings 为 POST（告警检查会回写）
 * - storyboards/deliver-to-canvas 路径带 "-to-"
 */
import client from './client'
import { ssePost } from './sse'
import type {
  AssetsGroup,
  AssetVariant,
  CharacterAsset,
  ConsistencyCheckResult,
  CrossProjectAsset,
  ExtractConfirmPayload,
  ExtractConfirmResult,
  ExtractResult,
  MessageResponse,
  PropAsset,
  SceneAsset,
  ScriptInputPayload,
  ScriptOut,
  ScriptSavePayload,
  StyleAsset,
  Storyboard,
  StoryboardRegenResult,
  StoryboardUpdatePayload,
  StoryboardWarnings,
  VariantCreatePayload,
} from '@/types'

// ============================================================
// script 剧本
// ============================================================

/**
 * 获取剧本（当前内容与原始输入）。
 * @param projectId 项目 ID
 * @returns 剧本输出
 */
export function getInput(projectId: number): Promise<ScriptOut> {
  return client.get(`/projects/${projectId}/script`).then((r) => r.data)
}

/**
 * 保存剧本原始输入（mode + raw_text，触发别名归一）。
 * @param projectId 项目 ID
 * @param payload 输入模式与原文
 * @returns 剧本输出
 */
export function saveInput(projectId: number, payload: ScriptInputPayload): Promise<ScriptOut> {
  return client.put(`/projects/${projectId}/script/input`, payload).then((r) => r.data)
}

/**
 * 剧本生成（POST SSE 流式）。
 * 事件约定：'delta' 增量文本片段、'done' 生成完成（携带剧本内容）、'error' 失败。
 * @param projectId 项目 ID
 * @param body 生成参数（模式/原文等，透传后端）
 * @param onEvent 每个事件帧回调
 * @param signal 中止信号（用户停止生成）
 */
export async function generateStream(
  projectId: number,
  body: Record<string, unknown>,
  onEvent: (event: string, data: unknown) => void,
  signal?: AbortSignal,
): Promise<void> {
  await ssePost(`/api/projects/${projectId}/script/generate/stream`, body, onEvent, signal)
}

/**
 * 手动保存编辑后的剧本内容（整体包装 source=user 落库）。
 * @param projectId 项目 ID
 * @param payload 剧本内容与可选体检回写
 * @returns 剧本输出
 */
export function saveScript(projectId: number, payload: ScriptSavePayload): Promise<ScriptOut> {
  return client.put(`/projects/${projectId}/script`, payload).then((r) => r.data)
}

/**
 * 剧本体检（结构化健康报告；后端为 POST，体检结果会回写剧本）。
 * @param projectId 项目 ID
 * @returns 体检报告（JSONB，宽松结构）
 */
export function health(projectId: number): Promise<Record<string, unknown>> {
  return client.post(`/projects/${projectId}/script/health`).then((r) => r.data)
}

/** 剧本体检别名（与 health 等价，供页面按语义命名引用）。 */
export { health as scriptHealth }

/**
 * 实体抽取（角色/场景/道具 + 别名冲突，同时写入 scripts.fences）。
 * @param projectId 项目 ID
 * @returns 抽取结果
 */
export function extract(projectId: number): Promise<ExtractResult> {
  return client.post(`/projects/${projectId}/script/extract`).then((r) => r.data)
}

/**
 * 实体抽取确认入池。
 * @param projectId 项目 ID
 * @param payload 确认的角色/场景/道具列表
 * @returns 各类资产现有总数
 */
export function confirmExtract(
  projectId: number,
  payload: ExtractConfirmPayload,
): Promise<ExtractConfirmResult> {
  return client.post(`/projects/${projectId}/script/extract/confirm`, payload).then((r) => r.data)
}

// ============================================================
// assets 资产
// ============================================================

/**
 * 查询项目四类资产聚合。
 * @param projectId 项目 ID
 * @returns 角色/场景/道具/风格列表
 */
export function listAssets(projectId: number): Promise<AssetsGroup> {
  return client.get(`/projects/${projectId}/assets`).then((r) => r.data)
}

/**
 * 创建角色并发起定妆照生成。
 * @param projectId 项目 ID
 * @param payload 角色名/描述/提示词/别名
 * @returns 新建角色（generation_status=queued）
 */
export function genCharacter(
  projectId: number,
  payload: { name: string; description?: string; visual_prompt?: string; aliases?: string[] },
): Promise<CharacterAsset> {
  return client.post(`/projects/${projectId}/assets/characters`, payload).then((r) => r.data)
}

/**
 * 锁定角色面部层（锁定后不再随重生成漂移）。
 * @param projectId 项目 ID
 * @param characterId 角色 ID
 * @returns 更新后的角色
 */
export function lockCharacter(projectId: number, characterId: number): Promise<CharacterAsset> {
  return client
    .post(`/projects/${projectId}/assets/characters/${characterId}/lock`)
    .then((r) => r.data)
}

/**
 * 解锁角色面部层。
 * @param projectId 项目 ID
 * @param characterId 角色 ID
 * @returns 更新后的角色
 */
export function unlockCharacter(projectId: number, characterId: number): Promise<CharacterAsset> {
  return client
    .post(`/projects/${projectId}/assets/characters/${characterId}/unlock`)
    .then((r) => r.data)
}

/**
 * 查询角色造型变体列表。
 * @param projectId 项目 ID
 * @param characterId 角色 ID
 * @returns 变体列表
 */
export function variants(projectId: number, characterId: number): Promise<AssetVariant[]> {
  return client
    .get(`/projects/${projectId}/assets/characters/${characterId}/variants`)
    .then((r) => r.data)
}

/**
 * 新增角色造型变体（inherit_mask 必填，RD-14）。
 * @param projectId 项目 ID
 * @param characterId 角色 ID
 * @param payload 变体类型/名称/继承声明等
 * @returns 新建变体
 */
export function addVariant(
  projectId: number,
  characterId: number,
  payload: VariantCreatePayload,
): Promise<AssetVariant> {
  return client
    .post(`/projects/${projectId}/assets/characters/${characterId}/variants`, payload)
    .then((r) => r.data)
}

/**
 * 一致性校验（主形象与变体/新图比对，伪相似度）。
 * @param projectId 项目 ID
 * @param payload 角色 ID 与可选变体 ID
 * @returns 相似度/是否通过/报告
 */
export function consistencyCheck(
  projectId: number,
  payload: { character_id: number; variant_id?: number },
): Promise<ConsistencyCheckResult> {
  return client.post(`/projects/${projectId}/assets/consistency-check`, payload).then((r) => r.data)
}

/**
 * 跨项目资产总览（角色 + 场景联合，"角色与场景"页数据源）。
 * @returns 总览行列表
 */
export function overviewCharacters(): Promise<CrossProjectAsset[]> {
  return client.get('/characters').then((r) => r.data)
}

// ============================================================
// storyboard 分镜
// ============================================================

/**
 * 发起分镜拆解（LLM 依据剧本拆出镜头序列）。
 * @param projectId 项目 ID
 * @param payload 拆解参数（可空，透传后端）
 * @returns 拆解概要（宽松结构）
 */
export function decompose(
  projectId: number,
  payload?: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return client.post(`/projects/${projectId}/storyboards/decompose`, payload ?? {}).then((r) => r.data)
}

/**
 * 查询项目全部分镜（按 sequence 升序）。
 * @param projectId 项目 ID
 * @returns 分镜列表
 */
export function listStoryboard(projectId: number): Promise<Storyboard[]> {
  return client.get(`/projects/${projectId}/storyboards`).then((r) => r.data)
}

/**
 * 更新分镜（景别/运镜/时长/描述/对白/提示词可编辑层/角色关联整体替换）。
 * @param projectId 项目 ID
 * @param storyboardId 分镜 ID
 * @param payload 更新内容
 * @returns 更新后的分镜
 */
export function updateStoryboard(
  projectId: number,
  storyboardId: number,
  payload: StoryboardUpdatePayload,
): Promise<Storyboard> {
  return client
    .put(`/projects/${projectId}/storyboards/${storyboardId}`, payload)
    .then((r) => r.data)
}

/**
 * 分镜重排（ordered_ids 须覆盖全部镜头）。
 * @param projectId 项目 ID
 * @param orderedIds 目标顺序的分镜 ID 列表
 * @returns 重排后的分镜列表
 */
export function reorder(projectId: number, orderedIds: number[]): Promise<Storyboard[]> {
  return client
    .post(`/projects/${projectId}/storyboards/reorder`, { ordered_ids: orderedIds })
    .then((r) => r.data)
}

/**
 * 拆分分镜（按对白行号一分为二）。
 * @param projectId 项目 ID
 * @param storyboardId 分镜 ID
 * @param atLine 对白拆分行号（1 起）
 * @returns 拆分结果（宽松结构）
 */
export function split(
  projectId: number,
  storyboardId: number,
  atLine: number,
): Promise<Record<string, unknown>> {
  return client
    .post(`/projects/${projectId}/storyboards/${storyboardId}/split`, { at_line: atLine })
    .then((r) => r.data)
}

/**
 * 合并分镜（other_id 为被合并删除的另一镜）。
 * @param projectId 项目 ID
 * @param storyboardId 保留镜 ID
 * @param otherId 被合并镜 ID
 * @returns 合并后的分镜
 */
export function merge(projectId: number, storyboardId: number, otherId: number): Promise<Storyboard> {
  return client
    .post(`/projects/${projectId}/storyboards/${storyboardId}/merge`, { other_id: otherId })
    .then((r) => r.data)
}

/**
 * 查询分镜告警（时长偏差/缺场景/缺角色等；后端为 POST，检查结果会回写）。
 * @param projectId 项目 ID
 * @returns 告警汇总
 */
export function warnings(projectId: number): Promise<StoryboardWarnings> {
  return client.post(`/projects/${projectId}/storyboards/warnings`).then((r) => r.data)
}

/** 分镜告警别名（与 warnings 等价，供页面按语义命名引用）。 */
export { warnings as storyboardWarnings }

/**
 * 重生成分镜（LLM 依据最新剧本/资产重写本镜内容）。
 * @param projectId 项目 ID
 * @param storyboardId 分镜 ID
 * @returns 累计次数与是否建议转画布精修
 */
export function regen(projectId: number, storyboardId: number): Promise<StoryboardRegenResult> {
  return client
    .post(`/projects/${projectId}/storyboards/${storyboardId}/regen`)
    .then((r) => r.data)
}

/**
 * 分镜投递 AI 画布（建立 studio_linked 画布；后端路径为 deliver-to-canvas）。
 * @param projectId 项目 ID
 * @param storyboardId 来源分镜 ID（缺省取第一镜）
 * @returns 新建画布 ID
 */
export function deliverToCanvas(
  projectId: number,
  storyboardId?: number,
): Promise<{ canvas_id: number }> {
  return client
    .post(`/projects/${projectId}/storyboards/deliver-to-canvas`, {
      storyboard_id: storyboardId ?? null,
    })
    .then((r) => r.data)
}

// ============================================================
// assets 资产补充（角色定妆生成 / 资产更新与删除 / 风格默认 / 跨类变体）
// ============================================================

/**
 * 发起角色定妆照生成（冻结 10 积分 + image 任务入队，置 generating）。
 * @param projectId 项目 ID
 * @param characterId 角色 ID
 * @param prompt 覆盖用提示词（缺省用角色 visual_prompt）
 * @returns 任务字典（id/type/phase/status/cost_points 等）
 */
export function generateCharacterPortrait(
  projectId: number,
  characterId: number,
  prompt?: string,
): Promise<Record<string, unknown>> {
  return client
    .post(`/projects/${projectId}/assets/characters/${characterId}/generate`, {
      prompt: prompt ?? null,
    })
    .then((r) => r.data)
}

/**
 * 更新角色资产（仅传入字段生效；改名时后端校验项目内唯一）。
 * @param projectId 项目 ID
 * @param characterId 角色 ID
 * @param payload 名称/描述/提示词/别名（可空字段不更新）
 * @returns 更新后的角色
 */
export function updateCharacter(
  projectId: number,
  characterId: number,
  payload: { name?: string; description?: string; visual_prompt?: string; aliases?: string[] },
): Promise<CharacterAsset> {
  return client
    .put(`/projects/${projectId}/assets/characters/${characterId}`, payload)
    .then((r) => r.data)
}

/**
 * 软删除角色（分镜引用不受影响）。
 * @param projectId 项目 ID
 * @param characterId 角色 ID
 * @returns 消息响应
 */
export function deleteCharacter(projectId: number, characterId: number): Promise<MessageResponse> {
  return client.delete(`/projects/${projectId}/assets/characters/${characterId}`).then((r) => r.data)
}

/**
 * 更新场景资产（名称/描述/提示词/锁定）。
 * @param projectId 项目 ID
 * @param sceneId 场景 ID
 * @param payload 更新字段
 * @returns 更新后的场景
 */
export function updateScene(
  projectId: number,
  sceneId: number,
  payload: { name?: string; description?: string; visual_prompt?: string; is_locked?: boolean },
): Promise<SceneAsset> {
  return client
    .put(`/projects/${projectId}/assets/scenes/${sceneId}`, payload)
    .then((r) => r.data)
}

/**
 * 更新道具资产（名称/分类/描述/提示词/锁定）。
 * @param projectId 项目 ID
 * @param propId 道具 ID
 * @param payload 更新字段
 * @returns 更新后的道具
 */
export function updateProp(
  projectId: number,
  propId: number,
  payload: {
    name?: string
    category?: string
    description?: string
    visual_prompt?: string
    is_locked?: boolean
  },
): Promise<PropAsset> {
  return client
    .put(`/projects/${projectId}/assets/props/${propId}`, payload)
    .then((r) => r.data)
}

/**
 * 更新风格资产（含设为项目默认：is_default=true 时后端自动清除其他默认）。
 * @param projectId 项目 ID
 * @param styleId 风格 ID
 * @param payload 更新字段
 * @returns 更新后的风格
 */
export function updateStyle(
  projectId: number,
  styleId: number,
  payload: {
    name?: string
    preset_code?: string
    reference_url?: string
    visual_prompt?: string
    is_default?: boolean
  },
): Promise<StyleAsset> {
  return client
    .put(`/projects/${projectId}/assets/styles/${styleId}`, payload)
    .then((r) => r.data)
}

/**
 * 查询场景/道具的造型变体列表（owner_type 路径段区分：scenes/props）。
 * @param projectId 项目 ID
 * @param ownerType 归属类型（scenes / props）
 * @param ownerId 资产 ID
 * @returns 变体列表
 */
export function listVariantsOf(
  projectId: number,
  ownerType: 'scenes' | 'props',
  ownerId: number,
): Promise<AssetVariant[]> {
  return client
    .get(`/projects/${projectId}/assets/${ownerType}/${ownerId}/variants`)
    .then((r) => r.data)
}
