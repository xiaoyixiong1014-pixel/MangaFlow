/**
 * 剧本创作流水线 API（文件级注释，P-1 范式级重构 Task 10）。
 *
 * 对应后端 backend/app/api/script_pipeline.py（Task 3/4 定稿路由）：
 * - 角色域：generate（LLM 生成候选）/ list（候选+已入池双列表）/
 *   批量编辑（upsert+remove，仅动候选）/ 单角色 regenerate / confirm（入池并推进阶段）
 * - 大纲域：plan 生成（LLM，多退少补落 episodes 表）/ plan 列表 /
 *   单集 outline 编辑（含 sequence 互换重排）/ plan confirm（全量确认并推进到 body）
 * - 正文域（Task 5）：单集正文 SSE 生成/重生成、PUT 保存（场次 source 合并）
 * - 优化域（Task 7）：四粒度 AI 优化预览（只读）与片段化应用（禁止整篇覆盖）
 * - 辅助域（Task 6/8/11A）：体检（可按集）、断线恢复状态、影响面预览、TXT/Fountain 导出
 * - 小说域（Task 9）：小说解析（txt/epub）与章节列表（from_chapters 分集依据）
 *
 * 阶段约定（project.script_phase）：idea/characters/outline/body；NULL=快速模式。
 *
 * 错误约定：
 * - 所有非 2xx 响应由 client.ts 响应拦截器统一 ElMessage.error(后端 detail) 并
 *   原样 reject axios error（detail 同时保留在 error.response.data.detail），
 *   调用方 catch 中可通过 status 区分语义（409=阶段/状态冲突、502=LLM 生成失败）
 *   做状态回退等补充逻辑，勿重复弹窗。
 */
import client from './client'
import { ssePost } from './sse'
import { downloadBlob, filenameFromDisposition } from '@/utils/download'

/** 剧本创作阶段：idea 创意 / characters 角色设定 / outline 分集大纲 / body 逐集正文 */
export type ScriptPhase = 'idea' | 'characters' | 'outline' | 'body'

/** 角色定位（与后端 characters.role_type CHECK 约束一致） */
export type RoleType = 'lead' | 'support' | 'villain' | 'extra'

/** 人物关系条目（characters.relations JSONB 数组元素） */
export interface CharacterRelation {
  /** 关系目标角色名 */
  target: string
  /** 关系描述（如「青梅竹马」「宿敌」） */
  relation: string
}

/** 角色档案（候选 planned / 已入池 confirmed 通用结构） */
export interface CharacterProfile {
  /** 角色 ID（候选未落库为 null） */
  id: number | null
  /** 候选临时 ID（8 位 hex；仅候选携带，已入池为 null） */
  candidate_id: string | null
  /** 角色名 */
  name: string
  /** 角色定位 */
  role_type: RoleType | null
  /** 年龄设定（弹性表述，如「约30岁」「少年」） */
  age: string | null
  /** 性格设定 */
  personality: string | null
  /** 形象特征（角色圣经视觉段一：发型/五官/体型/肤色） */
  appearance: string | null
  /** 服装配饰完整拆解（角色圣经视觉段二：上装/下装/鞋履/配饰） */
  outfit: string | null
  /** 风格与画质特点（角色圣经视觉段三：画风/光线/画质关键词） */
  style_note: string | null
  /** 行为动机 */
  motivation: string | null
  /** 人物弧光（角色成长线） */
  arc: string | null
  /** 人物关系数组 */
  relations: CharacterRelation[]
  /** 确认时间（候选为 null） */
  confirmed_at: string | null
  /** 来源：planned 候选 / confirmed 已入池 */
  source: 'planned' | 'confirmed'
}

/** 候选角色批量编辑 upsert 条目（按 id 优先、其次 name 匹配更新，均未命中则新增） */
export interface CharacterUpsertPayload {
  /** 候选临时 ID（优先匹配键；新增时不传） */
  id?: string
  /** 角色名（name 匹配键/更新值，1~50 字符） */
  name: string
  /** 角色定位（不传=不修改） */
  role_type?: RoleType | null
  /** 年龄设定（不传=不修改，≤20 字符） */
  age?: string | null
  /** 性格设定（不传=不修改） */
  personality?: string | null
  /** 形象特征（不传=不修改） */
  appearance?: string | null
  /** 服装配饰完整拆解（不传=不修改） */
  outfit?: string | null
  /** 风格与画质特点（不传=不修改） */
  style_note?: string | null
  /** 行为动机（不传=不修改） */
  motivation?: string | null
  /** 人物弧光（不传=不修改） */
  arc?: string | null
  /** 人物关系数组（不传=不修改） */
  relations?: CharacterRelation[] | null
}

/** 角色确认入池条目（缺省字段沿用候选值） */
export interface CharacterConfirmItemPayload {
  /** 确认入池的角色名（必填） */
  name: string
  /** 角色定位（不传=沿用候选值） */
  role_type?: RoleType | null
  /** 年龄设定（不传=沿用候选值） */
  age?: string | null
  /** 性格设定（不传=沿用候选值） */
  personality?: string | null
  /** 形象特征（不传=沿用候选值） */
  appearance?: string | null
  /** 服装配饰完整拆解（不传=沿用候选值） */
  outfit?: string | null
  /** 风格与画质特点（不传=沿用候选值） */
  style_note?: string | null
  /** 行为动机（不传=沿用候选值） */
  motivation?: string | null
  /** 人物弧光（不传=沿用候选值） */
  arc?: string | null
  /** 人物关系数组（不传=沿用候选值） */
  relations?: CharacterRelation[] | null
}

/** 角色双列表输出（GET characters 响应） */
export interface CharacterListOut {
  /** 候选角色档案（未生成过为空数组） */
  planned: CharacterProfile[]
  /** 已入池角色档案（confirmed_at 非空） */
  confirmed: CharacterProfile[]
  /** 当前剧本创作阶段（NULL=快速模式） */
  phase: ScriptPhase | null
}

/** 候选角色列表输出（generate / 批量编辑接口通用回执） */
export interface CharacterCandidatesOut {
  /** 候选角色档案列表 */
  characters: CharacterProfile[]
  /** 当前剧本创作阶段 */
  phase: ScriptPhase | null
}

/** 单角色重新生成输出 */
export interface CharacterRegenerateOut {
  /** 重新生成后的角色档案（候选或已入池） */
  character: CharacterProfile
  /** 当前剧本创作阶段 */
  phase: ScriptPhase | null
}

/** 角色确认输出 */
export interface CharacterConfirmOut {
  /** 本次入池角色数量 */
  confirmed_count: number
  /** 确认后阶段（正常推进到 outline） */
  phase: ScriptPhase | null
}

/** 单集大纲条目（含落库后的剧集 ID 与确认状态） */
export interface EpisodePlanItem {
  /** 剧集 ID */
  episode_id: number
  /** 剧集序号（项目内从 1 起） */
  sequence: number
  /** 单集标题 */
  title: string
  /** 一句话梗概 */
  logline: string | null
  /** 本集核心事件 */
  core_event: string | null
  /** 本集出场角色名列表 */
  characters: string[]
  /** 情绪曲线（如「平静 → 冲突 → 释然」） */
  emotion_curve: string | null
  /** 目标时长（秒） */
  duration_sec: number | null
  /** 单集大纲确认时间（未确认为 null） */
  confirmed_at: string | null
  /** 项目当前剧本创作阶段（保存大纲自动推进 outline→body 时携带，其余接口可缺省） */
  phase?: ScriptPhase | null
}

/** 分集大纲整体输出（plan 生成/列表响应） */
export interface EpisodePlanOut {
  /** 单集大纲列表（按 sequence 升序） */
  episodes: EpisodePlanItem[]
  /** 当前剧本创作阶段 */
  phase: ScriptPhase | null
}

/** 单集大纲编辑请求（字段平铺，不传=不修改） */
export interface EpisodeOutlineEditPayload {
  /** 单集标题（1~100 字符） */
  title?: string
  /** 一句话梗概（1~200 字符） */
  logline?: string
  /** 本集核心事件 */
  core_event?: string
  /** 出场角色名列表 */
  characters?: string[]
  /** 情绪曲线 */
  emotion_curve?: string
  /** 目标时长（秒） */
  duration_sec?: number
  /** 目标序号（与目标序号剧集互换 sequence；body 阶段禁止，409） */
  sequence?: number
}

/** 分集大纲确认输出 */
export interface EpisodePlanConfirmOut {
  /** 本次确认的单集数量 */
  confirmed_count: number
  /** 确认后阶段（body） */
  phase: ScriptPhase | null
}

// ============================================================
// 角色域
// ============================================================

/**
 * LLM 生成角色班底候选（结果整体覆盖 config 候选列表）。
 * @param projectId 项目 ID
 * @param payload 可选参数（instruction 对角色班底的自然语言要求）
 * @returns 候选列表与当前阶段
 * @throws 409 剧本尚未保存输入（先保存剧本输入）；502 LLM 生成失败（detail 提示重试）
 */
export function generateCharacters(
  projectId: number,
  payload?: { instruction?: string },
): Promise<CharacterCandidatesOut> {
  return client
    .post(`/projects/${projectId}/script/characters/generate`, payload ?? {})
    .then((r) => r.data)
}

/**
 * 读取角色设定双列表（候选 + 已入池 + 当前阶段）。
 * @param projectId 项目 ID
 * @returns 双列表与阶段
 * @throws 404 项目不存在/非本人项目
 */
export function listCharacters(projectId: number): Promise<CharacterListOut> {
  return client.get(`/projects/${projectId}/script/characters`).then((r) => r.data)
}

/**
 * 批量编辑候选角色（upsert + remove，仅动 config 候选，不碰已入池资产）。
 * @param projectId 项目 ID
 * @param payload upsert 条目列表与移除的候选 ID/名称列表
 * @returns 编辑后的候选列表与阶段
 * @throws 404 项目不存在/非本人项目
 */
export function editCharacters(
  projectId: number,
  payload: {
    upsert: CharacterUpsertPayload[]
    remove_ids: string[]
    remove_names: string[]
  },
): Promise<CharacterCandidatesOut> {
  return client.put(`/projects/${projectId}/script/characters`, payload).then((r) => r.data)
}

/**
 * 单角色重新生成（候选按临时 ID、已入池按数字 ID 匹配，替换其人设设定）。
 * @param projectId 项目 ID
 * @param cid 候选临时 ID（8 位 hex）或已入池角色数字 ID 字符串
 * @param payload 可选参数（instruction 本轮重生成方向描述）
 * @returns 替换后的角色档案与阶段
 * @throws 404 角色不存在；502 LLM 生成失败（detail 提示重试）
 */
export function regenerateCharacter(
  projectId: number,
  cid: string,
  payload?: { instruction?: string },
): Promise<CharacterRegenerateOut> {
  return client
    .post(`/projects/${projectId}/script/characters/${cid}/regenerate`, payload ?? {})
    .then((r) => r.data)
}

/**
 * 角色设定确认入池（幂等写入 characters 表并把阶段推进到 outline）。
 * @param projectId 项目 ID
 * @param payload 确认条目列表（至少 1 条，缺省字段沿用候选值）
 * @returns 入池数量与确认后阶段
 * @throws 400 尚无角色候选（先生成角色）；404 项目不存在/非本人项目
 */
export function confirmCharacters(
  projectId: number,
  payload: { characters: CharacterConfirmItemPayload[] },
): Promise<CharacterConfirmOut> {
  return client
    .post(`/projects/${projectId}/script/characters/confirm`, payload)
    .then((r) => r.data)
}

// ============================================================
// 故事设定域（redesign-script-workspace Task 3，P2 故事设定产物）
// ============================================================

/** 故事设定内容块（projects.story_setting JSONB 的结构化形态） */
export interface StorySetting {
  /** 一句话故事 */
  logline: string | null
  /** 核心冲突描述 */
  core_conflict: string | null
  /** 世界观设定 */
  worldview: string | null
  /** 来源标记：ai 模型生成 / user 用户编辑 */
  source: string
  /** 最近更新时间（ISO 8601 字符串） */
  updated_at: string | null
}

/** 故事设定生成/重生成输出（generate / regenerate 通用回执） */
export interface StorySettingGenerateOut {
  /** 生成后的故事设定内容块（source=ai） */
  setting: StorySetting
  /** 人物候选列表（generate 为全新候选；regenerate 为已确认保留 + 新候选合并） */
  characters: CharacterProfile[]
  /** 生成后阶段（NULL/idea 起点推进到 characters，其余阶段不回退） */
  phase: ScriptPhase | null
}

/** 故事设定详情输出（GET setting；未生成时 setting 为 null，不 404） */
export interface StorySettingDetailOut {
  /** 故事设定内容块（未生成时为 null） */
  setting: StorySetting | null
  /** 人物候选档案（config.script_characters 全量） */
  planned: CharacterProfile[]
  /** 已入池角色档案（characters 表 confirmed_at 非空） */
  confirmed: CharacterProfile[]
  /** 当前剧本创作阶段 */
  phase: ScriptPhase | null
}

/** 故事设定编辑请求（PUT setting；至少传一个字段，None=不修改该字段） */
export interface StorySettingEditPayload {
  /** 一句话故事（1~200 字符） */
  logline?: string
  /** 核心冲突描述（1~1000 字符） */
  core_conflict?: string
  /** 世界观设定（1~2000 字符） */
  worldview?: string
}

/** 故事设定编辑输出（PUT setting 回执；source 强制置 user，不改变阶段） */
export interface StorySettingEditOut {
  /** 保存后的故事设定内容块（source=user） */
  setting: StorySetting
  /** 当前剧本创作阶段（编辑不改变） */
  phase: ScriptPhase | null
}

/**
 * LLM 生成故事设定（设定三段 + 人物候选；阶段推进到 characters）。
 * @param projectId 项目 ID
 * @returns 设定内容块、人物候选与当前阶段
 * @throws 409 剧本尚未保存输入（先在录入区保存创意）；502 LLM 生成失败（detail 提示重试）
 */
export function generateStorySetting(projectId: number): Promise<StorySettingGenerateOut> {
  return client
    .post(`/projects/${projectId}/script/setting/generate`, {})
    .then((r) => r.data)
}

/**
 * 重出故事设定段（source=ai；已确认角色不回退，未入池候选随重生成刷新）。
 * @param projectId 项目 ID
 * @param payload 可选参数（instruction 本轮重生成的调整方向描述，≤2000 字符）
 * @returns 重生成后的设定内容块、合并后的人物候选与当前阶段
 * @throws 409 故事设定已锁定（body 阶段拒绝重生成）；502 LLM 生成失败
 */
export function regenerateStorySetting(
  projectId: number,
  payload?: { instruction?: string },
): Promise<StorySettingGenerateOut> {
  return client
    .post(`/projects/${projectId}/script/setting/regenerate`, payload ?? {})
    .then((r) => r.data)
}

/**
 * 读取故事设定详情（设定内容 + 人物候选/已确认双组；未生成时 setting 为 null，不 404）。
 * @param projectId 项目 ID
 * @returns 设定内容块（可空）、双组人物与当前阶段
 * @throws 404 项目不存在/非本人项目
 */
export function getStorySetting(projectId: number): Promise<StorySettingDetailOut> {
  return client.get(`/projects/${projectId}/script/setting`).then((r) => r.data)
}

/**
 * 合并编辑故事设定三段（source 强制置 user，写版本快照，不改变阶段）。
 * @param projectId 项目 ID
 * @param payload 待编辑字段（至少一个非空字段，空串/全空 422）
 * @returns 保存后的设定内容块与当前阶段
 * @throws 422 请求体校验失败（空串 / 三字段全空 / 超长：logline≤200、core_conflict≤1000、worldview≤2000）
 */
export function editStorySetting(
  projectId: number,
  payload: StorySettingEditPayload,
): Promise<StorySettingEditOut> {
  return client
    .put(`/projects/${projectId}/script/setting`, payload)
    .then((r) => r.data)
}

// ============================================================
// 大纲域
// ============================================================

/**
 * LLM 生成分集大纲（既有行复用更新、不足新建、多余硬删）。
 * @param projectId 项目 ID
 * @param payload 可选参数（target_episodes 目标集数 1~100，不传由服务端启发式决定；
 *   from_chapters 是否按小说章节切分为集，需先解析小说章节）
 * @returns 全量分集列表与阶段
 * @throws 409 大纲已锁定（body 阶段）或尚未确认角色设定（from_chapters 时未解析小说章节）；502 LLM 生成失败
 */
export function generateEpisodePlan(
  projectId: number,
  payload?: { target_episodes?: number; from_chapters?: boolean },
): Promise<EpisodePlanOut> {
  return client
    .post(`/projects/${projectId}/script/episodes/plan`, payload ?? {})
    .then((r) => r.data)
}

/**
 * 读取分集大纲列表（按 sequence 升序，无剧集时返回空列表）。
 * @param projectId 项目 ID
 * @returns 分集列表与阶段
 * @throws 404 项目不存在/非本人项目
 */
export function getEpisodePlan(projectId: number): Promise<EpisodePlanOut> {
  return client.get(`/projects/${projectId}/script/episodes/plan`).then((r) => r.data)
}

/**
 * 编辑单集大纲（文案字段微调 / sequence 互换重排）。
 * @param projectId 项目 ID
 * @param episodeId 剧集 ID
 * @param payload 编辑字段（不传=不修改）
 * @returns 编辑后的单集详情
 * @throws 400 sequence 越界；404 剧集不存在；409 body 阶段禁止 sequence 重排
 */
export function editEpisodeOutline(
  projectId: number,
  episodeId: number,
  payload: EpisodeOutlineEditPayload,
): Promise<EpisodePlanItem> {
  return client
    .put(`/projects/${projectId}/script/episodes/${episodeId}/outline`, payload)
    .then((r) => r.data)
}

/** 单集大纲重生成请求（instruction 可选调整方向） */
export interface EpisodeOutlineRegeneratePayload {
  /** 可选调整方向描述（如「冲突提前到开场」；留空=按现状重写） */
  instruction?: string
}

/** 单集大纲重生成输出（单集大纲字段 + 正文存在提醒） */
export interface EpisodeOutlineRegenerateOut extends EpisodePlanItem {
  /** 该集是否已有正文（true 时提示按需重生成正文，后端不自动删正文） */
  body_exists: boolean
}

/**
 * 确认全部分集大纲（全部置 confirmed_at 并把阶段推进到 body）。
 * @param projectId 项目 ID
 * @returns 确认单集数量与确认后阶段
 * @throws 400 尚无分集大纲（先生成）；404 项目不存在/非本人项目
 */
export function confirmEpisodePlan(projectId: number): Promise<EpisodePlanConfirmOut> {
  return client
    .post(`/projects/${projectId}/script/episodes/plan/confirm`)
    .then((r) => r.data)
}

/**
 * 单集大纲 AI 重生成（作用域收窄为 1 集，复用分集大纲提示词与校验）。
 * @param projectId 项目 ID
 * @param episodeId 剧集 ID
 * @param instruction 可选调整方向描述（留空=按现状与前后集衔接重写）
 * @returns 重生成后的单集大纲与 body_exists 提醒字段
 * @throws 404 项目/剧集不存在或不归属；409 阶段未达 outline / 该集大纲未生成 /
 *   无已确认角色；502 LLM 生成失败
 */
export function regenerateEpisodeOutline(
  projectId: number,
  episodeId: number,
  instruction?: string,
): Promise<EpisodeOutlineRegenerateOut> {
  const payload: EpisodeOutlineRegeneratePayload = instruction ? { instruction } : {}
  return client
    .post(`/projects/${projectId}/script/episodes/${episodeId}/outline/regenerate`, payload)
    .then((r) => r.data)
}

/** 单集大纲 AI 一键填充输出（建议值，不落库；已填字段原样返回） */
export interface EpisodeOutlineAutofillOut {
  /** 目标集序号 */
  sequence: number
  /** 单集标题（建议值） */
  title: string
  /** 一句话梗概（建议值） */
  logline: string | null
  /** 核心事件（建议值） */
  core_event: string | null
  /** 出场角色名（建议值） */
  characters: string[]
  /** 情绪走向（建议值） */
  emotion_curve: string | null
  /** 单集时长秒（建议值） */
  duration_sec: number | null
  /** 本次被 AI 填充的字段名（原为空的字段，供 toast 提示） */
  filled_fields: string[]
}

/**
 * 单集大纲 AI 一键填充（建议值不落库）：依据故事设定/角色设定/本集已填
 * 内容补全缺失字段；已填字段原样返回。前端回填表单后由用户确认再保存
 * （PUT outline 承接留痕）。
 * @param projectId 项目 ID
 * @param episodeId 剧集 ID
 * @returns 补全后的单集大纲建议值与本次填充字段清单
 * @throws 404 项目/剧集不存在或不归属；409 阶段未达 outline / 该集大纲未生成 /
 *   无已确认角色；502 LLM 生成失败
 */
export function autofillEpisodeOutline(
  projectId: number,
  episodeId: number,
): Promise<EpisodeOutlineAutofillOut> {
  return client
    .post(`/projects/${projectId}/script/episodes/${episodeId}/outline/autofill`)
    .then((r) => r.data)
}

// ============================================================
// 正文域（Task 5）
// ============================================================

/** 正文对白条目（ScriptBodyContent.scenes[].dialogue 元素，与后端 DialogueItem 对齐） */
export interface EpisodeBodyDialogue {
  /** 角色名（空串表示旁白/无角色对白） */
  character: string
  /** 台词内容 */
  line: string
  /** 台词类型：对白/旁白/独白/画外音（缺省「对白」，P-4 扩展） */
  type?: string | null
  /** 台词情绪标注（P-4 扩展） */
  emotion?: string | null
  /** 角色资产 ID 引用（P-4 扩展） */
  character_id?: number | null
  /** 字段级来源标记（RD-03：ai/user） */
  source?: string
}

/** 正文结构化场次（ScriptBodyContent.scenes 元素，与后端 SceneItem 对齐） */
export interface EpisodeBodyScene {
  /** 场景序号（≥1） */
  scene_no: number
  /** 场景标题 */
  title: string
  /** 场景剧情描述 */
  plot: string
  /** 动作/画面描述（P-4 扩展） */
  action?: string | null
  /** 地点名（引用顶层 locations.name） */
  location?: string | null
  /** 时段：day/night/dawn/dusk */
  time_of_day?: string | null
  /** 内外景：int/ext */
  int_ext?: string | null
  /** 音效清单 */
  sfx?: string[] | null
  /** 场次主导情绪 */
  emotion?: string | null
  /** 对白列表 */
  dialogue: EpisodeBodyDialogue[]
  /** 场景时长（秒，正整数） */
  duration_sec?: number | null
  /** 场次级来源标记（RD-03：ai/user） */
  source?: string
}

/** 正文地点条目（locations 元素，与后端 LocationItem 对齐） */
export interface EpisodeBodyLocation {
  /** 地点名（场景 location 引用键） */
  name: string
  /** 地点描述 */
  description?: string | null
  /** 缺省时段 */
  time_of_day?: string | null
}

/** 单集正文纯结构（content 解包后的可编辑结构） */
export interface EpisodeBodyContent {
  /** 单集标题 */
  title?: string
  /** 单集一句话梗概 */
  logline?: string
  /** 去重地点清单（可空） */
  locations?: EpisodeBodyLocation[] | null
  /** 结构化场次列表（≥1） */
  scenes: EpisodeBodyScene[]
}

/** 单集正文输出（GET script / PUT body / SSE done 通用回执） */
export interface EpisodeBodyOut {
  /** 剧集 ID */
  episode_id: number
  /** 关联剧本行 ID */
  script_id: number
  /** 结构化正文（scenes 场景数组，落库包装形态，需前端解包） */
  content: unknown
  /** 文章体正文文本（可空） */
  raw_text: string | null
  /** 正文版本号 */
  version: number
  /** 最近更新时间 */
  updated_at?: string | null
}

/** 单集正文保存请求（PUT episodes/{id}/body） */
export interface EpisodeBodySavePayload {
  /** 文章体正文文本（必填，minLength=1） */
  raw_text: string
  /** 结构化正文（scenes 场景数组） */
  content: EpisodeBodyContent
  /** 可选乐观锁基准版本号（不匹配 409） */
  base_version?: number
}

/**
 * SSE 流式生成单集正文（首次生成，POST 流式）。
 * 事件：delta{text} 正文增量 / status{message} 进入结构化场次阶段 /
 * error{message} 失败（不落库）/ done{episode_id,script_id,version,raw_text,content,usage}。
 * @param projectId 项目 ID
 * @param episodeId 剧集 ID
 * @param onEvent 事件回调（event/data）
 * @param signal 可选中止信号
 * @returns 无
 * @throws Error 响应非 2xx（携带后端 detail）
 */
export async function generateEpisodeBodyStream(
  projectId: number,
  episodeId: number,
  onEvent: (event: string, data: unknown) => void,
  signal?: AbortSignal,
): Promise<void> {
  await ssePost(
    `/api/projects/${projectId}/script/episodes/${episodeId}/body/generate/stream`,
    {},
    onEvent,
    signal,
  )
}

/**
 * SSE 单集正文重生成（覆盖语义，事件序列同首次生成）。
 * @param projectId 项目 ID
 * @param episodeId 剧集 ID
 * @param onEvent 事件回调（event/data）
 * @param signal 可选中止信号
 * @returns 无
 * @throws Error 响应非 2xx（400 该集尚未生成正文；409 生成中/阶段不符）
 */
export async function regenerateEpisodeBodyStream(
  projectId: number,
  episodeId: number,
  onEvent: (event: string, data: unknown) => void,
  signal?: AbortSignal,
): Promise<void> {
  await ssePost(
    `/api/projects/${projectId}/script/episodes/${episodeId}/body/regenerate`,
    {},
    onEvent,
    signal,
  )
}

/**
 * 保存用户修改的单集正文（场次级 source 合并，RD-03）。
 * @param projectId 项目 ID
 * @param episodeId 剧集 ID
 * @param payload 文章体文本 + 结构化内容 + 可选基准版本
 * @returns 保存后的单集正文（含新 version）
 * @throws 409 阶段非 body 或 base_version 不匹配
 */
export function saveEpisodeBody(
  projectId: number,
  episodeId: number,
  payload: EpisodeBodySavePayload,
): Promise<EpisodeBodyOut> {
  return client
    .put(`/projects/${projectId}/script/episodes/${episodeId}/body`, payload)
    .then((r) => r.data)
}

// ============================================================
// AI 优化（Task 7）：四粒度预览 + 片段化应用（禁止整篇覆盖）
// ============================================================

/** 优化粒度：global 全篇 / episode 整集 / segment 单场次 / field 单字段 */
export type OptimizeGrain = 'global' | 'episode' | 'segment' | 'field'

/** diff 单行单元（行/句级） */
export interface OptimizeDiffLine {
  /** 操作类型：equal 未变 / delete 删除 / insert 新增 */
  op: 'equal' | 'delete' | 'insert'
  /** 单元文本 */
  text: string
}

/** 单条优化片段（片段化 diff） */
export interface OptimizeChange {
  /** 片段稳定 ID（预览与 apply 双侧一致） */
  id: string
  /** 字段路径（如 scenes[0].plot） */
  path: string
  /** 片段类型（当前固定 field） */
  kind: string
  /** 变更前原文 */
  original: string | null
  /** 优化后建议值 */
  proposed: string | null
  /** 优化理由 */
  reason: string
  /** 行级/句级 diff 单元序列 */
  diff_lines: OptimizeDiffLine[]
}

/** 优化预览请求 */
export interface EpisodeOptimizeRequest {
  /** 优化粒度 */
  grain: OptimizeGrain
  /** 粒度目标：segment=场次序号；field=字段路径（如 scenes[1].plot） */
  target?: number | string | null
  /** 可选补充指令（优化方向） */
  instruction?: string | null
}

/** 优化预览输出（只读不落库） */
export interface EpisodeOptimizeOut {
  /** 剧集 ID */
  episode_id: number
  /** 本次优化粒度 */
  grain: string
  /** 片段化 diff 列表 */
  changes: OptimizeChange[]
  /** 片段数量 */
  count: number
}

/** apply 提交的单条片段（回传预览片段） */
export interface OptimizeApplyChangeItem {
  /** 片段稳定 ID（须与 accept_ids 匹配） */
  id: string
  /** 字段路径 */
  path: string
  /** 预览时原文（服务端并发比对用） */
  original: string | null
  /** 待应用的建议值 */
  proposed: string
}

/** 应用选中修改输出 */
export interface EpisodeOptimizeApplyOut {
  /** 实际应用片段数 */
  applied: number
  /** 跳过片段数 */
  skipped: number
  /** 应用后的剧本版本号（未应用不递增） */
  version: number
  /** 应用后的结构化 content */
  content: unknown
}

/**
 * 生成 AI 优化建议（四粒度，只读不落库）。
 * @param projectId 项目 ID
 * @param episodeId 剧集 ID
 * @param payload 粒度/目标/补充指令
 * @returns 片段化 diff 列表
 * @throws 400 该集无正文或 segment/field 的 target 非法；409 阶段非 body；502 优化 LLM 失败
 */
export function optimizeEpisode(
  projectId: number,
  episodeId: number,
  payload: EpisodeOptimizeRequest,
): Promise<EpisodeOptimizeOut> {
  return client
    .post(`/projects/${projectId}/script/episodes/${episodeId}/optimize`, payload)
    .then((r) => r.data)
}

/**
 * 应用选中的优化片段（片段级并发安全，禁止整篇覆盖）。
 * @param projectId 项目 ID
 * @param episodeId 剧集 ID
 * @param payload 接受片段 ID + 预览片段列表 + 可选基准版本
 * @returns { applied, skipped, version, content }
 * @throws 400 该集无正文；409 阶段非 body / base_version 不匹配 / 并发写入冲突
 */
export function applyEpisodeOptimize(
  projectId: number,
  episodeId: number,
  payload: {
    accept_ids: string[]
    changes: OptimizeApplyChangeItem[]
    base_version?: number
  },
): Promise<EpisodeOptimizeApplyOut> {
  return client
    .post(`/projects/${projectId}/script/episodes/${episodeId}/optimize/apply`, payload)
    .then((r) => r.data)
}

// ============================================================
// 对话式改写（redesign-script-workspace Task 6，P4 对话式共创 B 档）
// ============================================================

/** 对话式改写作用域：setting 故事设定 / episode_outline 单集大纲 / episode_body 单集正文 */
export type AssistScope = 'setting' | 'episode_outline' | 'episode_body'

/**
 * 助手面板聚焦对象（ScriptView 按中部卡片选中态计算后下发 AssistantPanel）。
 * scope=setting 时 target 省略（后端忽略）；episode 两作用域 target 必须为剧集 ID。
 */
export interface AssistantFocus {
  /** 聚焦作用域 */
  scope: AssistScope
  /** 聚焦目标剧集 ID（scope=setting 或未选中集时为 null/缺省） */
  target?: number | null
  /** 聚焦对象展示文案（如「故事设定」「第 2 集 · 分集大纲」「未选择集」） */
  label: string
}

/** 助手面板采纳回执事件 payload（AssistantPanel emit applied，ScriptView 据此刷新卡片） */
export interface AssistantAppliedPayload {
  /** 本次采纳作用域；缺省（回退快照等全项目动作）表示需刷新全部卡片 */
  scope?: AssistScope
  /** 本次采纳目标剧集 ID（scope=episode_* 时携带） */
  target?: number | null
  /** 采纳后项目剧本创作阶段（接口回执原样返回，不改变） */
  phase?: ScriptPhase | null
}

/** 对话式改写预览请求（POST /script/assist，只读不落库） */
export interface AssistPreviewRequest {
  /** 改写作用域 */
  scope: AssistScope
  /** 作用目标：setting 可省略；episode 两作用域必须为剧集 ID（整数） */
  target?: number | string | null
  /** 用户自然语言改写指令（必填，1~500 字） */
  instruction: string
}

/** 对话式改写预览输出（片段结构复用 optimize 的 OptimizeChange，id 口径一致） */
export interface AssistPreviewOut {
  /** 本次改写作用域 */
  scope: AssistScope
  /** 规范化后的目标标识（setting="all"；episode 两作用域为剧集 ID 字符串） */
  target: string | null
  /** 片段化 diff 列表（id/path/kind/original/proposed/reason/diff_lines） */
  changes: OptimizeChange[]
  /** 片段数量（非法 path 已在服务端丢弃，可能少于 LLM 输出条数） */
  count: number
  /** LLM 对本轮改动的一句话概括（可空串） */
  summary: string
}

/** 对话式改写采纳请求（POST /script/assist/apply，片段回传校验防并发覆盖） */
export interface AssistApplyRequest {
  /** 改写作用域（与预览一致） */
  scope: AssistScope
  /** 作用目标：语义同预览请求 */
  target?: number | string | null
  /** 采纳的片段列表（id/path/original/proposed，回传预览片段整对象） */
  accepts: OptimizeApplyChangeItem[]
}

/** 对话式改写采纳输出 */
export interface AssistApplyOut {
  /** 实际应用片段数（applied=0 不递增版本、不落库、不写快照） */
  applied: number
  /** 跳过片段数（path 不在白名单 / 原文与当前库值不符 / proposed 为空） */
  skipped: number
  /** 本次采纳写入的版本快照 ID（stage=assist_applied；applied=0 时为 null） */
  snapshot_id: number | null
  /** 应用后的剧本版本号（仅 episode_body 作用域携带） */
  version: number | null
  /** 当前项目剧本创作阶段（assist 采纳不改变阶段，原样返回） */
  phase: ScriptPhase | null
  /** 应用后的结构化 content（仅 episode_body 作用域携带） */
  content: unknown
}

/**
 * 对话式改写预览（作用 scope 的 diff 片段，不落库）。
 * @param projectId 项目 ID
 * @param payload 作用域 / 目标 / 改写指令（1~500 字）
 * @returns 片段化 diff 预览
 * @throws 409 阶段守卫（如 body 锁定故事设定、大纲未生成）；422 指令为空或超 500 字；502 LLM 失败
 */
export function assistPreview(
  projectId: number,
  payload: AssistPreviewRequest,
): Promise<AssistPreviewOut> {
  return client
    .post(`/projects/${projectId}/script/assist`, payload)
    .then((r) => r.data)
}

/**
 * 采纳对话式改写片段并落库（服务端按 path 白名单 + original 一致性双重校验，
 * 不一致计 skipped；applied>0 自动写一条 stage=assist_applied 版本快照）。
 * @param projectId 项目 ID
 * @param payload 作用域 / 目标 / 采纳片段列表
 * @returns 应用统计与最新阶段
 * @throws 409 片段全部失效或阶段冲突（detail 中文，client 拦截器统一提示）
 */
export function assistApply(
  projectId: number,
  payload: AssistApplyRequest,
): Promise<AssistApplyOut> {
  return client
    .post(`/projects/${projectId}/script/assist/apply`, payload)
    .then((r) => r.data)
}

// ============================================================
// 选中文本优化（quote_rewrite）：工作台「添加对话中」→ 引用 + 优化方向 →
// 可复制的优化文本（纯文本建议，不落库不写快照）
// ============================================================

/** 选中文本作用域：setting 故事设定 / characters 角色设定 / episode_outline 单集大纲 / episode_body 单集正文 */
export type QuoteScope = 'setting' | 'characters' | 'episode_outline' | 'episode_body'

/** 选中文本优化请求（POST /script/assist/quote-rewrite，纯文本建议不落库） */
export interface QuoteRewriteRequest {
  /** 选中文本作用域（仅用于 LLM 语境标注，不校验阶段） */
  scope: QuoteScope
  /** 目标剧集 ID（大纲/正文作用域下用于「第 N 集」语境标注，可选） */
  target_episode_id?: number | null
  /** 选中的原文（1~8000 字；多段引用由前端以空行拼接） */
  quote_text: string
  /** 优化方向描述（1~1000 字） */
  instruction: string
}

/** 选中文本优化输出（前端展示并提供复制，用户自行粘贴回对应位置） */
export interface QuoteRewriteOut {
  /** 优化后的文本（纯文本） */
  optimized_text: string
}

/**
 * 选中文本优化（不落库、不写快照、不改阶段——锁定阶段的既有内容同样可请求）。
 * @param projectId 项目 ID
 * @param payload 引用作用域 / 目标剧集 / 原文 / 优化方向
 * @returns 优化后的文本
 * @throws 404 项目不存在；422 参数校验失败；502 优化 LLM 失败
 */
export function quoteRewrite(
  projectId: number,
  payload: QuoteRewriteRequest,
): Promise<QuoteRewriteOut> {
  return client
    .post(`/projects/${projectId}/script/assist/quote-rewrite`, payload)
    .then((r) => r.data)
}

// ============================================================
// 体检 / 断线恢复 / 影响面预览 / 导出（Task 6/8/11A）
// ============================================================

/** 体检问题条目 */
export interface ScriptHealthIssue {
  /** 级别：error / warning / info */
  level: string
  /** 问题描述 */
  message: string
  /** 定位描述（如「场景2」「第1集 · 场景3」） */
  location: string
}

/** 剧本体检输出 */
export interface ScriptHealthOut {
  /** 体检得分（0~100） */
  score: number
  /** 问题列表 */
  issues: ScriptHealthIssue[]
}

/**
 * 剧本规则体检（可按集，结果落库 health_report 并返回）。
 * @param projectId 项目 ID
 * @param episodeId 可选剧集 ID（传入仅体检该集；缺省聚合体检全部集）
 * @returns 体检得分与问题列表
 * @throws 404 项目或剧本不存在
 */
export function checkEpisodeHealth(
  projectId: number,
  episodeId?: number,
): Promise<ScriptHealthOut> {
  return client
    .post(`/projects/${projectId}/script/health`, undefined, {
      params: episodeId ? { episode_id: episodeId } : undefined,
    })
    .then((r) => r.data)
}

/** 子图挂起的 interrupt 信息 */
export interface PipelineInterrupt {
  /** interrupt 类型：confirm_characters / confirm_episodes / confirm_body */
  type: string
  /** 负载（候选数据摘要） */
  payload?: unknown
}

/** 剧本子图断线重连状态（GET pipeline/status） */
export interface ScriptPipelineStatusOut {
  /** 子图检查点线程 ID */
  thread_id: string
  /** 项目 ID */
  project_id: number
  /** 项目当前剧本阶段 */
  script_phase: ScriptPhase | null
  /** 断点处待执行节点名（无断点为空串） */
  current_stage: string
  /** 断点处全部待执行节点名 */
  next_nodes: string[]
  /** 挂起确认闸口对应阶段（characters/outline/body；无为空串） */
  pending_stage: string
  /** 是否存在待人工确认的闸口 */
  has_pending_confirm: boolean
  /** 挂起 interrupt 的完整信息（无则为 null） */
  pending_confirm: PipelineInterrupt | null
  /** 进度摘要（character_count / episode_count / body_count） */
  summary: Record<string, number>
}

/**
 * 读取剧本子图断线重连状态（只读展示摘要，不做轮询检查点）。
 * @param projectId 项目 ID
 * @returns 当前阶段、断点阶段与待确认摘要
 * @throws 404 项目不存在/非本人或检查点线程归属不符
 */
export function getPipelineStatus(projectId: number): Promise<ScriptPipelineStatusOut> {
  return client.get(`/projects/${projectId}/script/pipeline/status`).then((r) => r.data)
}

/** 影响面预览实体类型 */
export type ImpactEntityType = 'character' | 'scene' | 'prop'

/** 实体删除影响面预览输出（RD-08） */
export interface EntityImpactOut {
  /** 实体类型 */
  entity_type: string
  /** 实体资产 ID */
  entity_id: number
  /** 实体名称 */
  entity_name: string
  /** 剧本内容中实体名出现次数 */
  script_refs: number
  /** 匹配到的分镜条数 */
  storyboard_refs: number
  /** 剧本引用所在场次标签（上限 10 条） */
  script_locations: string[]
  /** 匹配到的分镜 ID 列表（上限 20 条） */
  storyboard_ids: number[]
  /** 中文影响面摘要文案 */
  summary: string
}

/**
 * 实体删除影响面预览（修改前只读，不落库）。
 * @param projectId 项目 ID
 * @param params 实体类型 / 实体 ID / 可选剧集 ID
 * @returns 剧本与分镜引用统计及摘要文案
 * @throws 400 entity_type 非法；404 项目/实体/剧集不存在
 */
export function entityImpact(
  projectId: number,
  params: { entity_type: ImpactEntityType; entity_id: number; episode_id?: number },
): Promise<EntityImpactOut> {
  return client
    .get(`/projects/${projectId}/script/entities/impact`, { params })
    .then((r) => r.data)
}

/** 剧本导出格式：txt 纯文本 / fountain 结构化剧本 */
export type ScriptExportFormat = 'txt' | 'fountain'

/**
 * 导出剧本（TXT / Fountain），触发浏览器下载。
 * @param projectId 项目 ID
 * @param format 导出格式（txt / fountain）
 * @param episodeId 可选剧集 ID（缺省导出全部分集）
 * @returns 无（触发下载）
 * @throws 400 format 非法；404 项目/剧集不存在或暂无剧本内容
 */
export async function exportScript(
  projectId: number,
  format: ScriptExportFormat,
  episodeId?: number,
): Promise<void> {
  const resp = await client.get(`/projects/${projectId}/script/export`, {
    params: { format, episode_id: episodeId ?? undefined },
    responseType: 'blob',
  })
  const filename = filenameFromDisposition(
    resp.headers['content-disposition'] as string | undefined,
    `script.${format}`,
  )
  downloadBlob(resp.data as Blob, filename)
}

// ============================================================
// 小说域（Task 9）：章节解析与列表
// ============================================================

/** 小说章节条目（不含完整正文） */
export interface NovelChapterOut {
  /** 章节序号（1 起） */
  index: number
  /** 章节标题 */
  title: string
  /** 章节正文字数 */
  char_count: number
  /** 章节摘要（正文首 200 字） */
  summary: string
}

/** 小说章节解析/列表输出 */
export interface NovelParseOut {
  /** 项目 ID */
  project_id: number
  /** 章节数 */
  chapter_count: number
  /** 章节条目列表 */
  chapters: NovelChapterOut[]
  /** 项目当前剧本阶段 */
  phase: ScriptPhase | null
}

/**
 * 解析上传的小说文件（.txt/.epub，multipart 表单字段名 file，20MB 上限）。
 * 成功后章节暂存到项目 config，回写剧本 raw_text 为拼接全文。
 * @param projectId 项目 ID
 * @param file 待上传的小说文件
 * @returns 章节列表与阶段
 * @throws 400 格式不支持/无章节；413 文件超过 20MB
 */
export function parseNovel(projectId: number, file: File): Promise<NovelParseOut> {
  const form = new FormData()
  form.append('file', file)
  return client
    .post(`/projects/${projectId}/script/input/novel/parse`, form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    .then((r) => r.data)
}

/**
 * 读取暂存的小说章节列表（未解析时返回空列表，不 404）。
 * @param projectId 项目 ID
 * @returns 章节列表与阶段
 * @throws 404 项目不存在/非本人
 */
export function getNovelChapters(projectId: number): Promise<NovelParseOut> {
  return client.get(`/projects/${projectId}/script/input/novel/chapters`).then((r) => r.data)
}
