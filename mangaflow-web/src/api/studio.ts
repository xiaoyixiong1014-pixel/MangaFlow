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
import { downloadBlob, filenameFromDisposition } from '@/utils/download'
import { ElMessage } from 'element-plus'
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
 * @param episodeId 可选剧集 ID（S-06；缺省=默认剧本行，不传 episode_id）
 * @returns 剧本输出
 */
export function getInput(projectId: number, episodeId?: number): Promise<ScriptOut> {
  return client
    .get(`/projects/${projectId}/script`, { params: episodeId ? { episode_id: episodeId } : undefined })
    .then((r) => r.data)
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
 * @param episodeId 可选剧集 ID（S-06；缺省=默认剧本行）
 */
export async function generateStream(
  projectId: number,
  body: Record<string, unknown>,
  onEvent: (event: string, data: unknown) => void,
  signal?: AbortSignal,
  episodeId?: number,
): Promise<void> {
  const query = episodeId ? `?episode_id=${episodeId}` : ''
  await ssePost(`/api/projects/${projectId}/script/generate/stream${query}`, body, onEvent, signal)
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
 * 按集工作流：episodeId 传值时只抽取该剧集绑定的剧本行（缺省聚合全部）。
 * @param projectId 项目 ID
 * @param episodeId 可选剧集 ID（全局剧集切换器上下文；缺省=全部）
 * @returns 抽取结果
 */
export function extract(projectId: number, episodeId?: number): Promise<ExtractResult> {
  return client
    .post(`/projects/${projectId}/script/extract`, null, {
      params: { episode_id: episodeId },
    })
    .then((r) => r.data)
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

/**
 * 剧本一键同步 Agent 会话（S-08，后端幂等：同剧本版本返回既有会话）。
 * @param projectId 项目 ID
 * @param payload 可选参数（thread_id 复用既有会话；episode_id 按剧集同步）
 * @returns 会话 ID 与是否命中幂等
 * @throws 400 剧本内容为空；404 项目/剧本/会话不存在
 */
export function sendScriptToAgent(
  projectId: number,
  payload?: { thread_id?: string; episode_id?: number },
): Promise<{ thread_id: string; duplicated: boolean }> {
  return client
    .post(`/projects/${projectId}/script/send-to-agent`, payload ?? {})
    .then((r) => r.data)
}

/**
 * 剧本一键投递画布（S-08，后端幂等：同剧本版本返回既有节点）。
 * @param projectId 项目 ID
 * @param payload 可选参数（canvas_id 指定目标画布；episode_id 按剧集投递）
 * @returns 画布 ID、text 节点 ID 与是否命中幂等
 * @throws 400 剧本内容为空；404 项目/剧本/画布不存在
 */
export function sendScriptToCanvas(
  projectId: number,
  payload?: { canvas_id?: number; episode_id?: number },
): Promise<{ canvas_id: number; node_id: number; duplicated: boolean }> {
  return client
    .post(`/projects/${projectId}/script/send-to-canvas`, payload ?? {})
    .then((r) => r.data)
}

// ============================================================
// assets 资产
// ============================================================

/**
 * 查询项目四类资产聚合。
 * @param projectId 项目 ID
 * @param params 可选查询参数（episode_id：按集工作流当前集 ID，
 *               传值时响应带 episode_enabled 该集启用名单）
 * @returns 角色/场景/道具/风格列表（及按集启用名单）
 */
export function listAssets(
  projectId: number,
  params?: { episode_id?: number },
): Promise<AssetsGroup> {
  return client
    .get(`/projects/${projectId}/assets`, { params })
    .then((r) => r.data)
}

/** 资产列表的类别键（对应批量删除路由 asset_type 复数形式） */
export type AssetKind = 'characters' | 'scenes' | 'props' | 'styles'

/**
 * AI 识别场景/道具提示词六要素（agent 从剧本正文提取）。
 * @param projectId 项目 ID
 * @param type 资产类型 scene/prop
 * @param id 资产 ID
 * @returns {type, elements: 六要素键值（anchors 为字符串数组）}
 * @throws 400 剧本正文无相关内容；502 AI 识别失败
 */
export function suggestPrompt(
  projectId: number,
  type: 'scene' | 'prop',
  id: number,
): Promise<{ type: string; elements: Record<string, unknown> }> {
  return client
    .post(`/projects/${projectId}/assets/${type}/${id}/suggest-prompt`)
    .then((r) => r.data)
}

/**
 * 批量软删除资产（A-05）：逐条复用单删语义，未命中的 ID 进入 skipped。
 * @param projectId 项目 ID
 * @param kind 资产类别（characters/scenes/props/styles）
 * @param ids 待删除资产 ID 列表（1~100 个）
 * @returns { deleted, skipped: [{id, reason}] }
 * @throws 400 asset_type 非法或 ids 数量越界；404 项目不存在/非本人
 */
export function batchDeleteAssets(
  projectId: number,
  kind: AssetKind,
  ids: number[],
): Promise<{ deleted: number; skipped: Array<{ id: number; reason: string }> }> {
  return client
    .post(`/projects/${projectId}/assets/${kind}/batch-delete`, { ids })
    .then((r) => r.data)
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

/** 跨项目资产总览查询参数 */
export interface CrossProjectAssetQuery {
  /** 名称/描述模糊搜索关键字 */
  search?: string
  /** 页码（从 1 开始） */
  page?: number
  /** 每页条数 */
  page_size?: number
}

/** 跨项目资产总览分页响应（GET /characters 实际返回结构） */
export interface CrossProjectAssetPage {
  /** 当前页行列表 */
  items: CrossProjectAsset[]
  /** 总数 */
  total: number
  /** 页码 */
  page: number
  /** 每页条数 */
  page_size: number
}

/**
 * 跨项目资产总览（分页 + 搜索，S-06「复用资产」对话框数据源）。
 * @param params 搜索/分页参数
 * @returns 分页总览响应
 */
export function crossProjectAssets(
  params?: CrossProjectAssetQuery,
): Promise<CrossProjectAssetPage> {
  return client.get('/characters', { params }).then((r) => r.data)
}

/**
 * 复用资产：把其他项目的资产复制到当前项目（S-06）。
 * 副本名称追加" (副本)"；角色生成状态重置为 pending、变体不继承；
 * 风格副本不抢占默认。
 * @param projectId 目标项目 ID
 * @param assetType 资产类型 character/scene/prop/style
 * @param assetId 源资产 ID
 * @returns 新建资产（结构随类型为 CharacterOut/SceneOut/PropOut/StyleOut）
 */
export function copyAsset(
  projectId: number,
  assetType: 'character' | 'scene' | 'prop' | 'style',
  assetId: number,
): Promise<Record<string, unknown>> {
  return client
    .post(`/projects/${projectId}/assets/${assetType}/${assetId}/copy`)
    .then((r) => r.data)
}

// ============================================================
// storyboard 分镜
// ============================================================

/**
 * 发起分镜拆解（LLM 依据剧本拆出镜头序列）。
 * 按集工作流：episodeId 传值时只拆解并重建该剧集分镜（其他集保留）；
 * 缺省全量重建全部。
 * @param projectId 项目 ID
 * @param payload 拆解参数（可空，透传后端）
 * @param episodeId 可选剧集 ID（全局剧集切换器上下文；缺省=全量重建）
 * @returns 拆解概要（宽松结构）
 */
export function decompose(
  projectId: number,
  payload?: Record<string, unknown>,
  episodeId?: number,
): Promise<Record<string, unknown>> {
  return client
    .post(`/projects/${projectId}/storyboards/decompose`, payload ?? {}, {
      params: { episode_id: episodeId },
    })
    .then((r) => r.data)
}

/**
 * 批量重渲染过期镜头（P0-3 版本透传）。
 * @param projectId 项目 ID
 * @param episodeId 可选剧集过滤
 * @returns {"queued","skipped","storyboard_ids","total_cost"}
 */
export function renderStale(projectId: number, episodeId?: number): Promise<{
  queued: number
  skipped: number
  storyboard_ids: number[]
  total_cost: number
}> {
  return client
    .post(`/projects/${projectId}/storyboards/render-stale`, null, {
      params: { episode_id: episodeId },
    })
    .then((r) => r.data)
}

/**
 * 查询分镜列表（按 sequence 升序）。
 * @param projectId 项目 ID
 * @param episodeId 可选剧集 ID（S-06；缺省=全部，向后兼容）
 * @returns 分镜列表
 */
export function listStoryboard(projectId: number, episodeId?: number): Promise<Storyboard[]> {
  return client
    .get(`/projects/${projectId}/storyboards`, {
      params: episodeId ? { episode_id: episodeId } : undefined,
    })
    .then((r) => r.data)
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

/**
 * 导出分镜表（xlsx / csv / pdf，Task 10.4 + SB-08）。
 * episodeId 传真实剧集 ID 时按该剧集导出（S-06 剧集切换器）；缺省/0 为
 * 「项目全部分镜」全量导出（普通创作流分镜不绑剧集，向后兼容）。
 * pdf 由后端 fpdf2 生成（A4 横向表格）；字体缺失时后端自动降级英文兜底版
 * 并在响应头 X-PDF-Font-Fallback 标记，前端仅提示不改下载行为。
 * @param projectId 项目 ID
 * @param format 导出格式：xlsx / csv / pdf
 * @param episodeId 可选剧集 ID（缺省导出全部分镜）
 * @returns 触发浏览器下载（无返回数据）
 * @throws 400 format 非法；404 项目/剧集不存在/非本人
 */
export async function exportStoryboardSheet(
  projectId: number,
  format: 'xlsx' | 'csv' | 'pdf',
  episodeId?: number,
): Promise<void> {
  const resp = await client.get(
    `/projects/${projectId}/storyboards/episodes/${episodeId ?? 0}/export`,
    { params: { format }, responseType: 'blob' },
  )
  const fallback = `storyboards_all.${format}`
  const filename = filenameFromDisposition(
    resp.headers['content-disposition'] as string | undefined,
    fallback,
  )
  // SB-08：字体缺失降级提示（不影响下载，仅告知 PDF 内中文以 ? 呈现）
  if (format === 'pdf' && resp.headers['x-pdf-font-fallback']) {
    ElMessage.warning('未找到可用中文字体，PDF 已按英文兜底版生成（中文以 ? 占位）')
  }
  downloadBlob(resp.data as Blob, filename)
}

// ============================================================
// 连续性检查（SB-06）/ 目标时长自动调整（SB-07）
// ============================================================

/** 单条连续性问题（后端 ContinuityItem） */
export interface ContinuityItem {
  /** 关联分镜 ID（取问题镜头，供前端定位卡片） */
  storyboard_id: number | null
  /** 问题类型：scene_jump / outfit_conflict / prompt_missing */
  type: string
  /** 问题描述 */
  message: string
  /** 置信级别：info 信息提示 / suspect 疑似冲突 */
  confidence: 'info' | 'suspect'
}

/** 连续性检查响应（RD-23 宁可漏报不误报，无问题 items 为空） */
export interface ContinuityCheckResult {
  /** 问题列表 */
  items: ContinuityItem[]
  /** 检查的相邻镜头对数量 */
  checked_pairs: number
}

/** 单镜时长调整前后对比（SB-07） */
export interface AutoFitShotDiff {
  /** 分镜 ID */
  id: number
  /** 调整前时长（秒） */
  before: number
  /** 调整后时长（秒） */
  after: number
}

/** 目标时长自动调整响应摘要（SB-07） */
export interface AutoFitDurationResult {
  /** 调整前总时长（秒） */
  before_total: number
  /** 调整后总时长（秒，受单镜保底 1s 影响 ≈ 目标值） */
  after_total: number
  /** 目标总时长（秒） */
  target_seconds: number
  /** 实际发生变化的镜头数 */
  changed: number
  /** 每镜调整前后对比 */
  per_shot: AutoFitShotDiff[]
}

/**
 * 连续性检查（SB-06）：相邻镜头的场景跳变 / 服装变体冲突 / 提示词缺失。
 * @param projectId 项目 ID
 * @param episodeId 可选剧集 ID（缺省检查全部分镜）
 * @returns 问题列表与检查的相邻镜头对数量
 * @throws 404 项目/剧集不存在/非本人
 */
export function continuityCheck(projectId: number, episodeId?: number): Promise<ContinuityCheckResult> {
  return client
    .post(`/projects/${projectId}/storyboards/continuity-check`, {
      episode_id: episodeId ?? null,
    })
    .then((r) => r.data)
}

/**
 * 目标时长自动调整（SB-07）：分镜时长等比缩放至目标总时长并落库。
 * @param projectId 项目 ID
 * @param targetSeconds 目标总时长（秒，1~3600，越界后端 400）
 * @param episodeId 可选剧集 ID（缺省调整全部分镜）
 * @returns 调整摘要（前后对比）
 * @throws 400 目标越界或无分镜；404 项目/剧集不存在/非本人
 */
export function autoFitDuration(
  projectId: number,
  targetSeconds: number,
  episodeId?: number,
): Promise<AutoFitDurationResult> {
  return client
    .post(`/projects/${projectId}/storyboards/auto-fit-duration`, {
      target_seconds: targetSeconds,
      episode_id: episodeId ?? null,
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
  return generateAssetImage(projectId, 'character', characterId, prompt)
}

/**
 * 发起资产图生成（角色定妆照 / 场景概念图 / 道具标准图，同构后端通用端点）。
 * @param projectId 项目 ID
 * @param type 资产类型 character/scene/prop
 * @param id 资产 ID
 * @param prompt 覆盖用提示词（缺省用资产 visual_prompt）
 * @returns 任务字典（id/type/phase/status/cost_points 等）
 */
export function generateAssetImage(
  projectId: number,
  type: 'character' | 'scene' | 'prop',
  id: number,
  prompt?: string,
): Promise<Record<string, unknown>> {
  return client
    .post(`/projects/${projectId}/assets/${type}s/${id}/generate`, {
      prompt: prompt ?? null,
    })
    .then((r) => r.data)
}

/**
 * 更新角色资产（仅传入字段生效；改名时后端校验项目内唯一）。
 * @param projectId 项目 ID
 * @param characterId 角色 ID
 * @param payload 名称/描述/提示词/别名/参考图（可空字段不更新）
 * @returns 更新后的角色
 */
export function updateCharacter(
  projectId: number,
  characterId: number,
  payload: {
    name?: string
    description?: string
    visual_prompt?: string
    aliases?: string[]
    reference_image_url?: string | null
    tags?: string[]
  },
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
 * 软删除场景（分镜强绑引用不受影响，scene_id 外键 SET NULL）。
 * @param projectId 项目 ID
 * @param sceneId 场景 ID
 * @returns 消息响应
 */
export function deleteScene(projectId: number, sceneId: number): Promise<MessageResponse> {
  return client.delete(`/projects/${projectId}/assets/scenes/${sceneId}`).then((r) => r.data)
}

/**
 * 软删除道具。
 * @param projectId 项目 ID
 * @param propId 道具 ID
 * @returns 消息响应
 */
export function deleteProp(projectId: number, propId: number): Promise<MessageResponse> {
  return client.delete(`/projects/${projectId}/assets/props/${propId}`).then((r) => r.data)
}

/**
 * 软删除风格（与四类资产单删语义一致）。
 * @param projectId 项目 ID
 * @param styleId 风格 ID
 * @returns 消息响应
 */
export function deleteStyle(projectId: number, styleId: number): Promise<MessageResponse> {
  return client.delete(`/projects/${projectId}/assets/styles/${styleId}`).then((r) => r.data)
}

/**
 * 更新场景资产（名称/描述/提示词/锁定/参考图）。
 * @param projectId 项目 ID
 * @param sceneId 场景 ID
 * @param payload 更新字段
 * @returns 更新后的场景
 */
export function updateScene(
  projectId: number,
  sceneId: number,
  payload: {
    name?: string
    description?: string
    visual_prompt?: string
    is_locked?: boolean
    reference_image_url?: string | null
    tags?: string[]
  },
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
    tags?: string[]
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
    tags?: string[]
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
