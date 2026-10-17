<!--
  资产管理页（文件级注释）。

  对应原型 S-04（资产管理）+ S-05（资产变体面板），路由 /projects/:id/assets。
  功能：
  - 三 Tab（角色/场景/道具，各 pane 显式声明 name 与 activeTab 对齐，修复
    v-model 初值不匹配自动索引导致的内容区空白缺陷 C-BUG-1）+ 「生成全部主照」
    批量按钮（确认框提示张数与积分）；风格不再作为资产 Tab 展示（画风由
    项目创建时选定，后端风格行仅作生成管线锚点，用户不可见）；
  - 卡片操作统一形态：批量删除选择圈浮于图片左上角（纯圆圈，选中态品牌绿
    填充+光晕+微放大）；底部操作行 = 上传替换（左）+ 更多（右，dropdown
    收拢提示词/锁定/删除等，道具无参考图字段故无上传替换）；
  - 角色卡网格：有图 el-image 预览 / 无图占位渐变 + 生成按钮；状态 Tag（pending/
    generating/done/failed，仅 generating（确有生成任务在跑）时每 3s 轮询刷新，
    直至全部终态或达到轮询上限自动停止；pending 为实体抽取后的初始态、无任务，
    不触发轮询——修复进入页面即无限循环请求缺陷 C-BUG-1）；
    face_locked 显示 el-icon Lock 徽标；
  - 角色操作：生成定妆照（generateCharacterPortrait→轮询）/确认并锁定（lock→confirm）/
    派生变体（S-05 抽屉，RD-14 继承声明：面部特征必须继承——默认勾选且禁用）/
    重生成 / 编辑提示词（双层抽屉：锁定层只读 visual_prompt + 可编辑层 textarea）/
    上传替换（A-04：el-upload → POST /api/media/upload → PATCH reference_image_url）/
    删除（confirm + 软删）；
  - A-05 批量删除：卡片多选（左上角选择圈）+「批量删除」按钮（选中 >0 可用，
    confirm 二次确认后调 batch-delete 接口，跳过项提示原因）；
  - 按集工作流（S-06）：顶部全局剧集切换器联动——按集视图仅显示该集经「启用资产」
    确认入池的资产（后端 episodes.enabled_assets 名单经 GET /assets?episode_id=
    响应的 episode_enabled 下发），未启用过的集显示空态引导；
  - 场景卡/道具卡：编辑提示词 + 锁定切换（收进更多菜单）；场景无图显示
    「空场景」占位（RD-19）；
  - 一致性校验：consistencyCheck → el-dialog 并排对比（原图 + 变体图）+
    相似度进度条（passed 绿 / 未通过红）。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Lock, Loading } from '@element-plus/icons-vue'
import * as studioApi from '@/api/studio'
import * as mediaApi from '@/api/media'
import * as pipelineApi from '@/api/scriptPipeline'
import * as productionApi from '@/api/production'
import { useProjectStore } from '@/stores/project'
import type { UploadFile } from 'element-plus'
import type {
  AssetVariant,
  AssetsGroup,
  CharacterAsset,
  ConsistencyCheckResult,
  CrossProjectAsset,
  EpisodeEnabledNames,
  PropAsset,
  SceneAsset,
} from '@/types'

const route = useRoute()
const router = useRouter()
const projectStore = useProjectStore()

/** 当前项目 ID（取路由参数） */
const projectId = computed(() => Number(route.params.id))

// ============================================================
// S-06 按集工作上下文（全局剧集切换器联动）
// ============================================================

/** 当前选中剧集 ID（顶部全局切换器写入；null = 默认剧集/全部，不过滤） */
const episodeScope = computed(() => projectStore.currentEpisodeId)

/** 当前选中剧集条目（工具行展示集名） */
const episodeScopeItem = computed(
  () => projectStore.currentProject?.episodes.find((e) => e.id === episodeScope.value) ?? null,
)

/** 视图过滤模式：episode 仅本集相关 / all 全部资产（无集上下文时强制 all） */
const filterMode = ref<'episode' | 'all'>('episode')

/**
 * 该集已启用资产名名单（GET /assets?episode_id= 响应的 episode_enabled，
 * 后端取 episodes.enabled_assets——该集经「启用资产」确认入池的资产名 union，
 * 规范化四键：characters/scenes/props/styles 各为名字数组）。
 * null = 请求未携带集上下文（无集上下文不过滤）；名单对象存在但键为空数组
 * = 该集未启用过该类资产（按集视图显示空态，引导用户点「启用资产」）。
 */
const episodeEnabled = ref<EpisodeEnabledNames | null>(null)

// 切换全局剧集：重置过滤模式为"仅本集相关"并重拉资产（带该集启用名单）
watch(episodeScope, () => {
  filterMode.value = 'episode'
  void loadAssets()
})

/** 按集过滤是否生效（选中集 + 过滤模式=episode） */
const episodeFilterActive = computed(
  () => episodeScope.value != null && filterMode.value === 'episode',
)

/**
 * 「启用资产」入口：按集工作流要求先选定集数，未选集时提示不跳转。
 * @returns 无
 */
function gotoExtract(): void {
  if (episodeScope.value == null) {
    ElMessage.warning('请在左上角选择好集数')
    return
  }
  void router.push(`/projects/${projectId.value}/script/extract`)
}

/**
 * 判定资产名是否在该集启用名单内（精确匹配）。
 * 名单对象存在但键缺数组（该集未启用过该类）→ 视为空数组 → 不命中 → 隐藏；
 * 仅当 episodeEnabled 为 null（请求未带集上下文）时不过滤。
 * @param kind 资产类别键
 * @param name 资产名
 * @returns 是否该集已启用
 */
function isEnabledInEpisode(kind: 'characters' | 'scenes' | 'props' | 'styles', name: string): boolean {
  if (episodeEnabled.value === null) return true // 无集上下文 → 不按集隐藏
  const names = episodeEnabled.value[kind] ?? []
  return names.includes(name.trim())
}

// ============================================================
// 资产列表
// ============================================================

/** 首屏加载中（骨架屏） */
const loading = ref(true)
/** 首屏加载失败（区分「加载失败」与「确实无资产」两种空内容场景，避免误导性空态） */
const loadError = ref(false)
/** 四类资产聚合 */
const assets = ref<AssetsGroup>({ characters: [], scenes: [], props: [], styles: [] })
/** 当前激活 Tab（与 el-tab-pane 显式 name 一一对应） */
const activeTab = ref<'characters' | 'scenes' | 'props' | 'styles'>('characters')

// ---------- 按集过滤视图（资产库项目级共享；按集视图=该集启用名单，未启用集为空态） ----------

/** 当前 Tab 可见角色（按集过滤生效时仅该集启用名单内的角色，否则全量） */
const visibleCharacters = computed(() =>
  episodeFilterActive.value
    ? assets.value.characters.filter((c) => isEnabledInEpisode('characters', c.name))
    : assets.value.characters,
)

/** 当前 Tab 可见场景（按集过滤生效时仅该集启用名单内的场景，否则全量） */
const visibleScenes = computed(() =>
  episodeFilterActive.value
    ? assets.value.scenes.filter((s) => isEnabledInEpisode('scenes', s.name))
    : assets.value.scenes,
)

/** 当前 Tab 可见道具（按集过滤生效时仅该集启用名单内的道具，否则全量） */
const visibleProps = computed(() =>
  episodeFilterActive.value
    ? assets.value.props.filter((p) => isEnabledInEpisode('props', p.name))
    : assets.value.props,
)

/** 请求序号（竞态保护：响应返回时若已有更新的请求发出，则丢弃本次过期响应） */
let loadSeq = 0
/** 组件是否已卸载（卸载后在途响应禁止回写状态） */
let disposed = false

/**
 * 拉取四类资产列表（quiet=true 时不切换骨架屏，供轮询与操作后刷新使用）。
 * 带请求序号竞态保护：仅允许最新一次请求回写状态，丢弃过期响应；
 * 失败时非静默进入错误兜底态、静默轮询保持旧数据不惊扰用户。
 * 按集工作流：选中集时带 episode_id 请求，响应携带该集启用名单 episode_enabled
 * （按集视图据此过滤；请求失败时名单置空对象 → 按集视图显示空态）。
 * @param quiet 是否静默刷新
 * @returns 无
 */
async function loadAssets(quiet = false): Promise<void> {
  const seq = ++loadSeq
  if (!quiet) {
    loading.value = true
    loadError.value = false
  }
  try {
    const group = await studioApi.listAssets(
      projectId.value,
      episodeScope.value != null ? { episode_id: episodeScope.value } : undefined,
    )
    // 过期响应或组件已卸载：直接丢弃，不回写状态
    if (disposed || seq !== loadSeq) return
    assets.value = group
    // 未带 episode_id 的响应无名单字段 → null（无集上下文不过滤）
    episodeEnabled.value = group.episode_enabled ?? null
  } catch {
    // 静默轮询失败保持旧数据；首屏失败进入错误兜底（不吞异常导致骨架屏卡死）
    if (!quiet && !disposed && seq === loadSeq) loadError.value = true
  } finally {
    if (!quiet && !disposed && seq === loadSeq) loading.value = false
  }
}

/** 轮询定时器句柄（有角色 generating 时每 3s 刷新） */
let pollTimer: ReturnType<typeof setInterval> | null = null
/** 轮询间隔（毫秒） */
const POLL_INTERVAL_MS = 3000
/** 轮询上限（3s × 100 = 5 分钟；防止后端任务卡死永不终态时无限循环请求） */
const POLL_MAX_TICKS = 100

/**
 * 停止轮询并清理定时器句柄。
 * @returns 无
 */
function stopPolling(): void {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

/**
 * 若存在确有生成任务在跑的角色（generating）则启动 3s 轮询，
 * 全部终态后自动停止；达到轮询上限自动停止并提示（防止任务卡死导致无限请求）。
 * 注意：pending 是实体抽取后的初始态（后端 server_default，无关联任务），
 * 不纳入轮询触发条件——否则刚进入页面就会无限轮询（C-BUG-1 循环请求根因）。
 * @returns 无
 */
function ensurePolling(): void {
  const busy = assets.value.characters.some((c) => c.generation_status === 'generating')
  if (!busy || pollTimer) return
  // 本次轮询周期已执行的 tick 计数（闭包内维护，重启轮询时重新计数）
  let ticks = 0
  pollTimer = setInterval(async () => {
    ticks += 1
    await loadAssets(true)
    const stillBusy = assets.value.characters.some((c) => c.generation_status === 'generating')
    if (!stillBusy && pollTimer) {
      stopPolling()
      ElMessage.success('全部定妆照生成完成')
    } else if (ticks >= POLL_MAX_TICKS && pollTimer) {
      stopPolling()
      ElMessage.warning('生成任务长时间未更新，已暂停自动刷新，可稍后手动刷新页面查看结果')
    }
  }, POLL_INTERVAL_MS)
}

onBeforeUnmount(() => {
  disposed = true
  stopPolling()
})

/**
 * 首屏加载失败后的手动重试（重新拉取并按需恢复轮询）。
 * @returns 无
 */
async function retryLoad(): Promise<void> {
  await loadAssets()
  ensurePolling()
}

// ============================================================
// 状态 Tag
// ============================================================

/** 生成状态 → Tag 文案 */
const statusLabel: Record<string, string> = {
  pending: '排队中',
  generating: '生成中',
  done: '已完成',
  failed: '失败',
}
/** 生成状态 → Tag 类型 */
const statusType: Record<string, 'info' | 'warning' | 'success' | 'danger'> = {
  pending: 'info',
  generating: 'warning',
  done: 'success',
  failed: 'danger',
}

// ============================================================
// 定妆照生成（单个 / 全部）
// ============================================================

/** 单张生成请求进行中的角色 ID 集合（防重复点击） */
const genBusyIds = ref<number[]>([])

/**
 * 发起单张定妆照生成并进入轮询。
 * @param c 目标角色
 * @returns 无
 */
async function generatePortrait(c: CharacterAsset): Promise<void> {
  if (genBusyIds.value.includes(c.id)) return
  genBusyIds.value = [...genBusyIds.value, c.id]
  try {
    await studioApi.generateCharacterPortrait(projectId.value, c.id)
    ElMessage.success(`已提交「${c.name}」定妆照生成任务（冻结 10 积分）`)
    await loadAssets(true)
    ensurePolling()
  } finally {
    genBusyIds.value = genBusyIds.value.filter((i) => i !== c.id)
  }
}

// ---------- 场景/道具图生成（与角色定妆照同构；无 generation_status 字段，走任务轮询） ----------

/** 场景图生成请求进行中的场景 ID 集合（防重复点击） */
const sceneGenBusyIds = ref<number[]>([])
/** 道具图生成请求进行中的道具 ID 集合（防重复点击） */
const propGenBusyIds = ref<number[]>([])

/**
 * 轮询任务至终态后刷新列表（场景/道具生成状态感知）。
 * 2s 间隔，上限 90s；done 提示成功，failed/canceled 提示失败；超时静默刷新。
 * @param taskId 任务 ID
 * @param name 资产名（提示文案用）
 * @returns 无
 */
async function pollAssetTask(taskId: number, name: string): Promise<void> {
  let finalStatus = ''
  for (let i = 0; i < 45; i++) {
    await new Promise((resolve) => setTimeout(resolve, 2000))
    const t = await productionApi.taskDetail(taskId)
    if (['done', 'failed', 'canceled'].includes(t.status)) {
      finalStatus = t.status
      break
    }
  }
  await loadAssets(true)
  if (finalStatus === 'done') {
    ElMessage.success(`「${name}」图片已生成`)
  } else if (finalStatus) {
    ElMessage.error(`「${name}」图片生成失败，请重试`)
  }
}

/**
 * 发起场景概念图生成并轮询至终态。
 * @param s 目标场景
 * @returns 无
 */
async function generateScene(s: SceneAsset): Promise<void> {
  if (sceneGenBusyIds.value.includes(s.id)) return
  sceneGenBusyIds.value = [...sceneGenBusyIds.value, s.id]
  try {
    const task = (await studioApi.generateAssetImage(projectId.value, 'scene', s.id)) as {
      id: number
    }
    ElMessage.success(`已提交「${s.name}」场景图生成任务（冻结 10 积分）`)
    await pollAssetTask(task.id, s.name)
  } finally {
    sceneGenBusyIds.value = sceneGenBusyIds.value.filter((i) => i !== s.id)
  }
}

/**
 * 发起道具标准图生成并轮询至终态。
 * @param p 目标道具
 * @returns 无
 */
async function generateProp(p: PropAsset): Promise<void> {
  if (propGenBusyIds.value.includes(p.id)) return
  propGenBusyIds.value = [...propGenBusyIds.value, p.id]
  try {
    const task = (await studioApi.generateAssetImage(projectId.value, 'prop', p.id)) as {
      id: number
    }
    ElMessage.success(`已提交「${p.name}」道具图生成任务（冻结 10 积分）`)
    await pollAssetTask(task.id, p.name)
  } finally {
    propGenBusyIds.value = propGenBusyIds.value.filter((i) => i !== p.id)
  }
}

/**
 * 生成全部主照：对所有缺图角色批量提交（确认框提示张数与预估积分）。
 * @returns 无
 */
async function generateAll(): Promise<void> {
  const targets = assets.value.characters.filter(
    (c) => !c.asset_url && c.generation_status !== 'generating',
  )
  if (!targets.length) {
    ElMessage.info('所有角色都已有主照')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将为 ${targets.length} 个角色生成主定妆照，预计消耗 ${targets.length * 10} 积分。是否继续？`,
      '生成全部主照',
      { type: 'info', confirmButtonText: '开始生成' },
    )
  } catch {
    return
  }
  for (const c of targets) {
    await studioApi.generateCharacterPortrait(projectId.value, c.id).catch(() => undefined)
  }
  ElMessage.success('批量任务已提交')
  await loadAssets(true)
  ensurePolling()
}

// ============================================================
// 一键锁定（当前 Tab 可见资产批量锁定）
// ============================================================

/** 一键锁定进行中（按钮 loading，防重复提交） */
const lockingAll = ref(false)

/**
 * 一键锁定当前 Tab 的可见资产：角色锁定面部层（face_locked，仅对已生成定妆照
 * 的角色生效），场景/道具置 is_locked（生成保护）。总确认后逐个调用既有单锁
 * 接口，完成后静默刷新列表；无可锁定项时提示不打断。
 * @returns 无
 */
async function lockAllVisible(): Promise<void> {
  const kind = activeTab.value
  if (lockingAll.value) return
  // 收集可锁定目标：角色=有定妆照且未锁；场景/道具=未锁定
  const targets: Array<{ id: number; name: string }> = []
  if (kind === 'characters') {
    for (const c of visibleCharacters.value) {
      if (c.asset_url && !c.face_locked) targets.push(c)
    }
  } else if (kind === 'scenes') {
    for (const s of visibleScenes.value) {
      if (!s.is_locked) targets.push(s)
    }
  } else if (kind === 'props') {
    for (const p of visibleProps.value) {
      if (!p.is_locked) targets.push(p)
    }
  } else {
    return
  }
  if (!targets.length) {
    ElMessage.info(`当前分类没有可锁定的${KIND_LABELS[kind]}`)
    return
  }
  try {
    await ElMessageBox.confirm(
      `将锁定 ${targets.length} 个${KIND_LABELS[kind]}，锁定后重生成不再影响已锁定内容。是否继续？`,
      '一键锁定',
      { type: 'warning', confirmButtonText: '锁定', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  lockingAll.value = true
  try {
    for (const t of targets) {
      if (kind === 'characters') {
        await studioApi.lockCharacter(projectId.value, t.id)
      } else if (kind === 'scenes') {
        await studioApi.updateScene(projectId.value, t.id, { is_locked: true })
      } else {
        await studioApi.updateProp(projectId.value, t.id, { is_locked: true })
      }
    }
    ElMessage.success(`已锁定 ${targets.length} 个${KIND_LABELS[kind]}`)
    await loadAssets(true)
  } finally {
    lockingAll.value = false
  }
}

// ============================================================
// 编辑提示词抽屉（RD-04 分层：锁定层只读 + 可编辑层）
// ============================================================

/** 提示词抽屉可见性 */
const promptVisible = ref(false)
/** 当前编辑对象类别 */
const promptKind = ref<'character' | 'scene' | 'prop'>('character')
/** 当前编辑对象名称（仅展示） */
const promptName = ref('')
/** 锁定层内容（系统标准化提示词，只读展示） */
const promptLockedText = ref('')
/** 可编辑层文本（用户补充/覆盖） */
const promptEditableText = ref('')
/** 保存中（防重复提交） */
const promptSaving = ref(false)
/** 当前编辑对象引用（保存后回写） */
let promptTarget: CharacterAsset | SceneAsset | PropAsset | null = null

/**
 * 打开提示词抽屉（角色/场景/道具通用）。
 * @param target 资产对象
 * @param kind 类别
 * @returns 无
 */
function openPromptDrawer(
  target: CharacterAsset | SceneAsset | PropAsset,
  kind: 'character' | 'scene' | 'prop',
): void {
  promptTarget = target
  promptKind.value = kind
  promptName.value = target.name
  promptLockedText.value = target.visual_prompt
  promptEditableText.value = target.visual_prompt
  suggestElements.value = null // 重置上次的六要素识别结果
  suggestAnchors.value = ''
  promptVisible.value = true
}

/**
 * 保存可编辑层提示词（按类别分发到对应更新接口）。
 * @returns 无
 */
async function savePrompt(): Promise<void> {
  if (!promptTarget || promptSaving.value) return
  promptSaving.value = true
  try {
    const text = promptEditableText.value.trim()
    if (!text) {
      ElMessage.warning('提示词不能为空')
      return
    }
    if (promptKind.value === 'character') {
      Object.assign(
        promptTarget,
        await studioApi.updateCharacter(projectId.value, promptTarget.id, { visual_prompt: text }),
      )
    } else if (promptKind.value === 'scene') {
      Object.assign(
        promptTarget,
        await studioApi.updateScene(projectId.value, promptTarget.id, { visual_prompt: text }),
      )
    } else {
      Object.assign(
        promptTarget,
        await studioApi.updateProp(projectId.value, promptTarget.id, { visual_prompt: text }),
      )
    }
    promptLockedText.value = text
    promptVisible.value = false
    ElMessage.success('提示词已更新，生成图片时将使用该提示词')
  } finally {
    promptSaving.value = false
  }
}

// ============================================================
// 场景/道具提示词六要素 AI 识别（agent 从剧本正文提取，确认后生成图片）
// ============================================================

/** 六要素 AI 识别进行中（按钮 loading） */
const suggestLoading = ref(false)
/** 识别出的六要素（五键字符串 + anchors 数组；null=未识别，抽屉展示手写可编辑层） */
const suggestElements = ref<Record<string, string> | null>(null)
/** 核心锚点编辑文本（顿号/逗号分隔，确认时拆分为数组） */
const suggestAnchors = ref('')

/**
 * 打开 AI 识别流（点击「AI 识别六要素」按钮）：调后端 LLM 从剧本正文提取
 * 该场景/道具的六要素，填充结构化编辑区。
 * @returns 无
 */
async function runSuggestPrompt(): Promise<void> {
  if (!promptTarget || suggestLoading.value) return
  const type = promptKind.value === 'scene' ? 'scene' : 'prop'
  suggestLoading.value = true
  try {
    const res = await studioApi.suggestPrompt(projectId.value, type, promptTarget.id)
    const el = res.elements as Record<string, unknown>
    suggestElements.value = {
      style_base: String(el.style_base ?? ''),
      space_type: String(el.space_type ?? ''),
      lighting: String(el.lighting ?? ''),
      composition: String(el.composition ?? ''),
      negative: String(el.negative ?? ''),
      scale_ref: String(el.scale_ref ?? ''),
      material: String(el.material ?? ''),
      marker: String(el.marker ?? ''),
      usage: String(el.usage ?? ''),
      text_policy: String(el.text_policy ?? ''),
    }
    suggestAnchors.value = Array.isArray(el.anchors) ? el.anchors.map(String).join('、') : ''
  } finally {
    suggestLoading.value = false
  }
}

/**
 * 按六要素合成完整视觉提示词（场景/道具模板各异）。
 * @returns 合成文本（未识别时返回空串）
 */
function composeElementsPrompt(): string {
  const e = suggestElements.value
  if (!e || !promptTarget) return ''
  const anchors = suggestAnchors.value
    .split(/[、,，]/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (promptKind.value === 'scene') {
    return (
      `统一${e.style_base}插画风格，场景：${promptTarget.name}（${e.space_type}）。` +
      `核心锚点：${anchors.join('、')}。光照氛围：${e.lighting}。` +
      `构图与视角：${e.composition}。负面约束：画面中不出现${e.negative}。`
    )
  }
  return (
    `道具：${promptTarget.name}。尺度参照：${e.scale_ref}。` +
    `核心锚点：${anchors.join('、')}。材质与表面工艺：${e.material}。` +
    `唯一标记：${e.marker}。功能与操作方式：${e.usage}。可读文字政策：${e.text_policy}。`
  )
}

/**
 * 确认六要素：合成提示词写入可编辑层并保存（保存后生成图片即用该提示词）。
 * @returns 无
 */
async function confirmSuggestSave(): Promise<void> {
  promptEditableText.value = composeElementsPrompt()
  await savePrompt()
}

// ============================================================
// S-05 变体抽屉（RD-14 继承声明）
// ============================================================

/** 变体抽屉可见性 */
const variantVisible = ref(false)
/** 当前变体抽屉的角色 */
const variantChar = ref<CharacterAsset | null>(null)
/** 变体列表 */
const variantList = ref<AssetVariant[]>([])
/** 变体列表加载中 */
const variantLoading = ref(false)
/** 变体类型选项（角色） */
const variantTypeOptions = [
  { label: '服饰', value: 'outfit' },
  { label: '年龄', value: 'age' },
  { label: '其他', value: 'other' },
]
/** 新增变体表单 */
const variantForm = ref({ variant_type: 'outfit', name: '' })
/** 面部特征继承（RD-14：换装必继承 → 默认勾选且禁用） */
const inheritFace = ref(true)
/** 造型独立声明（勾选后服饰/画风作为独立特征生成） */
const outfitIndependent = ref(false)
/** 提交中（防重复点击） */
const variantSubmitting = ref(false)

/**
 * 打开变体抽屉并加载变体列表。
 * @param c 目标角色
 * @returns 无
 */
async function openVariantDrawer(c: CharacterAsset): Promise<void> {
  variantChar.value = c
  variantVisible.value = true
  variantLoading.value = true
  try {
    variantList.value = await studioApi.variants(projectId.value, c.id)
  } finally {
    variantLoading.value = false
  }
}

/**
 * 新增造型变体：inherit_mask 必填（RD-14），面部继承强制 true。
 * @returns 无
 */
async function submitVariant(): Promise<void> {
  if (!variantChar.value || variantSubmitting.value) return
  if (!variantForm.value.name.trim()) {
    ElMessage.warning('请填写变体名称')
    return
  }
  if (!inheritFace.value) {
    // 理论上不可达（checkbox 禁用且默认勾选），双保险阻断
    ElMessage.warning('面部特征必须继承（换装不换脸）')
    return
  }
  variantSubmitting.value = true
  try {
    await studioApi.addVariant(projectId.value, variantChar.value.id, {
      variant_type: variantForm.value.variant_type,
      name: variantForm.value.name.trim(),
      // RD-14 继承声明：face 恒继承；style 独立性由用户勾选决定
      inherit_mask: {
        face: true,
        style: !outfitIndependent.value,
        outfit_independent: outfitIndependent.value,
      },
    })
    ElMessage.success('变体已创建')
    variantForm.value = { variant_type: 'outfit', name: '' }
    outfitIndependent.value = false
    variantList.value = await studioApi.variants(projectId.value, variantChar.value.id)
  } finally {
    variantSubmitting.value = false
  }
}

/** 变体类型 → 中文文案 */
const variantTypeLabel: Record<string, string> = {
  outfit: '服饰',
  age: '年龄',
  lighting: '光线',
  other: '其他',
}

// ============================================================
// 一致性校验（RD-13：并排对比 + 相似度内部判定）
// ============================================================

/** 校验弹窗可见性 */
const ccVisible = ref(false)
/** 校验目标角色 */
const ccChar = ref<CharacterAsset | null>(null)
/** 变体选项（供选择与主形象比对的变体） */
const ccVariants = ref<AssetVariant[]>([])
/** 选中的变体 ID（空 = 与主形象自身比对） */
const ccVariantId = ref<number | undefined>(undefined)
/** 校验进行中 */
const ccLoading = ref(false)
/** 校验结果 */
const ccResult = ref<ConsistencyCheckResult | null>(null)

/**
 * 打开一致性校验弹窗并加载该角色变体。
 * @param c 目标角色
 * @returns 无
 */
async function openConsistency(c: CharacterAsset): Promise<void> {
  ccChar.value = c
  ccResult.value = null
  ccVariantId.value = undefined
  ccVisible.value = true
  try {
    ccVariants.value = await studioApi.variants(projectId.value, c.id)
  } catch {
    ccVariants.value = []
  }
}

/**
 * 执行一致性校验。
 * @returns 无
 */
async function runConsistency(): Promise<void> {
  if (!ccChar.value || ccLoading.value) return
  ccLoading.value = true
  try {
    ccResult.value = await studioApi.consistencyCheck(projectId.value, {
      character_id: ccChar.value.id,
      variant_id: ccVariantId.value,
    })
  } finally {
    ccLoading.value = false
  }
}

/** 右侧对比图（选中变体图，无则回退主形象图） */
const ccRightUrl = computed(
  () =>
    ccVariants.value.find((v) => v.id === ccVariantId.value)?.asset_url
    ?? ccChar.value?.asset_url
    ?? '',
)

// ============================================================
// 上传替换（A-04 参考图锚点：真实上传 + 回写 reference_image_url）
// ============================================================

/** 正在上传参考图的角色 ID 集合（按钮 loading，防重复提交） */
const uploadingRefIds = ref<number[]>([])

/**
 * 参考图上传底层（A-04，角色/场景共用）：选择文件后先调 POST /api/media/upload
 * 落库素材，再用返回 URL PATCH 对应资产 reference_image_url（生成时将该参考图
 * 作为锚点透传图像渠道），成功后回写资产卡状态。
 * @param kind 资产类别（characters/scenes；道具无参考图字段不接入）
 * @param asset 目标资产（至少含 id/name）
 * @param raw 原始图片文件（el-upload on-change 回调 raw；空则忽略）
 * @returns 无
 */
async function uploadRefImage(
  kind: 'characters' | 'scenes',
  asset: { id: number; name: string },
  raw?: File,
): Promise<void> {
  if (!raw) return
  if (uploadingRefIds.value.includes(asset.id)) return
  uploadingRefIds.value = [...uploadingRefIds.value, asset.id]
  try {
    const media = await mediaApi.upload(raw, { media_type: 'image', project_id: projectId.value })
    const updated = kind === 'characters'
      ? await studioApi.updateCharacter(projectId.value, asset.id, { reference_image_url: media.url })
      : await studioApi.updateScene(projectId.value, asset.id, { reference_image_url: media.url })
    Object.assign(asset, updated)
    ElMessage.success(`「${asset.name}」参考图已上传并保存，下次生成时将作为参考`)
  } finally {
    uploadingRefIds.value = uploadingRefIds.value.filter((i) => i !== asset.id)
  }
}

/**
 * 角色参考图上传替换回调（A-04）。
 * @param character 目标角色
 * @param uploadFile el-upload on-change 回调的文件对象（raw 为原始 File）
 * @returns 无
 */
function onUploadReplace(character: CharacterAsset, uploadFile: UploadFile): void {
  void uploadRefImage('characters', character, uploadFile.raw)
}

/**
 * 场景参考图上传替换回调（A-04，与角色卡「上传替换」行为一致）。
 * @param scene 目标场景
 * @param uploadFile el-upload on-change 回调的文件对象（raw 为原始 File）
 * @returns 无
 */
function onSceneUploadReplace(scene: SceneAsset, uploadFile: UploadFile): void {
  void uploadRefImage('scenes', scene, uploadFile.raw)
}

// ============================================================
// 删除角色
// ============================================================

/**
 * 删除角色（RD-08 影响面预览 + confirm + 软删除）。
 * 删除前调 entities/impact 拉取剧本/分镜引用统计，把摘要拼入二次确认文案；
 * 预览失败时降级为通用确认文案，不阻断删除流程。
 * 注：批量删除涉及多实体，暂不逐条预览影响面（改动面控制）。
 * @param c 目标角色
 * @returns 无
 */
async function removeCharacter(c: CharacterAsset): Promise<void> {
  let impactSummary = ''
  try {
    const impact = await pipelineApi.entityImpact(projectId.value, {
      entity_type: 'character',
      entity_id: c.id,
    })
    impactSummary = impact.summary ? `${impact.summary}\n\n` : ''
  } catch {
    // 预览失败（网络/业务错误）：降级为通用确认文案
    impactSummary = ''
  }
  try {
    await ElMessageBox.confirm(
      `${impactSummary}确定删除角色「${c.name}」吗？删除后可联系管理员恢复。`,
      '删除角色',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await studioApi.deleteCharacter(projectId.value, c.id)
  ElMessage.success('角色已删除')
  await loadAssets(true)
}

// ============================================================
// 删除场景 / 道具（confirm + 软删除，与角色单删语义一致）
// ============================================================

/**
 * 删除场景（confirm 二次确认 + 软删除）。
 * 场景无影响面预览端点，使用通用确认文案；分镜强绑引用由后端
 * scene_id 外键 SET NULL 语义兜底，不会阻断删除。
 * @param s 目标场景
 * @returns 无
 */
async function removeScene(s: SceneAsset): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定删除场景「${s.name}」吗？删除后可联系管理员恢复。`,
      '删除场景',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await studioApi.deleteScene(projectId.value, s.id)
  ElMessage.success('场景已删除')
  await loadAssets(true)
}

/**
 * 删除道具（confirm 二次确认 + 软删除）。
 * @param p 目标道具
 * @returns 无
 */
async function removeProp(p: PropAsset): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定删除道具「${p.name}」吗？删除后可联系管理员恢复。`,
      '删除道具',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await studioApi.deleteProp(projectId.value, p.id)
  ElMessage.success('道具已删除')
  await loadAssets(true)
}

// ============================================================
// S-06 复用资产（跨项目总览 → 复制到当前项目）
// ============================================================

/** 复用资产弹窗可见性 */
const reuseVisible = ref(false)
/** 总览列表加载中 */
const reuseLoading = ref(false)
/** 搜索关键字（名称/描述模糊匹配） */
const reuseSearch = ref('')
/** 跨项目资产总览行（characters + scenes 联合） */
const reuseRows = ref<CrossProjectAsset[]>([])
/** 单行复制进行中（行 ID 集合，防重复点击） */
const copyingIds = ref<number[]>([])

/**
 * 打开复用资产弹窗并拉取跨项目总览（当前用户全部项目的角色 + 场景）。
 * @returns 无
 */
async function openReuse(): Promise<void> {
  reuseVisible.value = true
  await searchReuse()
}

/**
 * 按关键字拉取跨项目资产总览（分页取前 50 条）。
 * @returns 无
 */
async function searchReuse(): Promise<void> {
  reuseLoading.value = true
  try {
    const page = await studioApi.crossProjectAssets({
      search: reuseSearch.value.trim() || undefined,
      page: 1,
      page_size: 50,
    })
    reuseRows.value = page.items
  } finally {
    reuseLoading.value = false
  }
}

/**
 * 复制一条资产到当前项目：名称追加"(副本)"、角色生成状态重置 pending。
 * 成功后静默刷新当前项目资产列表。
 * @param row 总览行
 * @returns 无
 */
async function copyReuse(row: CrossProjectAsset): Promise<void> {
  if (copyingIds.value.includes(row.id)) return
  copyingIds.value = [...copyingIds.value, row.id]
  try {
    await studioApi.copyAsset(
      projectId.value,
      row.asset_type as 'character' | 'scene' | 'prop' | 'style',
      row.id,
    )
    ElMessage.success(`已将「${row.name}」复制到当前项目`)
    await loadAssets(true)
  } finally {
    copyingIds.value = copyingIds.value.filter((id) => id !== row.id)
  }
}

// ============================================================
// A-05 批量删除 / 标签编辑
// ============================================================

/** 资产类别键（复数形式，与后端 batch-delete 路由 asset_type 一致） */
type AssetKind = studioApi.AssetKind

/** 类别 → 中文文案（批量删除确认文案用） */
const KIND_LABELS: Record<AssetKind, string> = {
  characters: '角色',
  scenes: '场景',
  props: '道具',
  styles: '风格',
}

/** 各 Tab 勾选待批量删除的资产 ID */
const selectedIds = ref<Record<AssetKind, number[]>>({
  characters: [],
  scenes: [],
  props: [],
  styles: [],
})

/** 批量删除进行中（防重复提交） */
const batchDeleting = ref(false)

/** 当前 Tab 勾选的资产 ID 列表 */
const activeSelectedIds = computed(() => selectedIds.value[activeTab.value])

/**
 * 勾选/取消勾选一张资产卡（批量删除多选）。
 * @param kind 资产类别
 * @param id 资产 ID
 * @param checked 是否勾选
 * @returns 无
 */
function toggleSelected(kind: AssetKind, id: number, checked: boolean): void {
  const cur = selectedIds.value[kind]
  selectedIds.value[kind] = checked ? [...cur, id] : cur.filter((i) => i !== id)
}

/**
 * 批量删除当前 Tab 勾选的资产（confirm 二次确认 → batch-delete 接口 → 刷新）。
 * 跳过项（不属于本项目/已删除）汇总提示原因。
 * @returns 无
 */
async function batchDeleteSelected(): Promise<void> {
  const kind = activeTab.value
  const ids = selectedIds.value[kind]
  if (!ids.length || batchDeleting.value) return
  try {
    await ElMessageBox.confirm(
      `确定删除选中的 ${ids.length} 个${KIND_LABELS[kind]}吗？删除后可联系管理员恢复。`,
      '批量删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  batchDeleting.value = true
  try {
    const res = await studioApi.batchDeleteAssets(projectId.value, kind, ids)
    if (res.skipped.length) {
      ElMessage.warning(
        `已删除 ${res.deleted} 个，${res.skipped.length} 个跳过（${res.skipped[0]?.reason}）`,
      )
    } else {
      ElMessage.success(`已删除 ${res.deleted} 个${KIND_LABELS[kind]}`)
    }
    selectedIds.value[kind] = []
    await loadAssets(true)
  } finally {
    batchDeleting.value = false
  }
}

onMounted(() => {
  void loadAssets().then(ensurePolling)
})
</script>

<template>
  <div class="page-container assets-view">
    <div class="view-head">
      <h2 class="page-title">资产管理</h2>
      <div class="head-actions">
        <!-- 实体抽取入口（S-04）：按集工作流要求先在左上角选定集数，未选集拦截提示 -->
        <el-button type="primary" plain @click="gotoExtract">启用资产</el-button>
        <el-button @click="openReuse">复用资产</el-button>
        <el-button
          v-if="activeTab === 'characters' && assets.characters.length"
          type="primary"
          plain
          @click="generateAll"
        >
          生成全部主照
        </el-button>
      </div>
    </div>

    <!-- 首屏骨架 -->
    <el-skeleton v-if="loading" :rows="6" animated />

    <!-- 首屏加载失败兜底：给出明确错误态与重试入口，避免空白/误导性空态 -->
    <el-result
      v-else-if="loadError"
      icon="error"
      title="资产加载失败"
      sub-title="网络异常或服务暂不可用，请稍后重试"
    >
      <template #extra>
        <el-button type="primary" @click="retryLoad">重新加载</el-button>
      </template>
    </el-result>

    <el-empty v-else-if="!assets.characters.length && !assets.scenes.length && !assets.props.length"
      description="该项目还没有资产：在左上角选择集数后点击「启用资产」提取"
    >
      <el-button type="primary" @click="gotoExtract">启用资产</el-button>
    </el-empty>

    <template v-else>
      <!-- 工具行：当前集上下文（按集视图=该集启用名单）+ 批量删除 -->
      <div class="tab-toolbar">
        <!-- S-06 按集工作上下文：顶部切换器选集后，资产视图默认只看该集启用名单 -->
        <template v-if="episodeScope != null">
          <el-tag size="small" effect="dark" type="info" class="scope-tag">
            当前集：{{ episodeScopeItem ? `第${episodeScopeItem.sequence}集` : '未知' }}
          </el-tag>
          <el-radio-group v-model="filterMode" size="small">
            <el-radio-button value="episode">仅本集相关</el-radio-button>
            <el-radio-button value="all">全部资产</el-radio-button>
          </el-radio-group>
          <el-divider direction="vertical" />
        </template>
        <el-button type="success" plain :loading="lockingAll" @click="lockAllVisible">
          一键锁定
        </el-button>
        <el-button
          type="danger"
          plain
          :disabled="!activeSelectedIds.length"
          :loading="batchDeleting"
          @click="batchDeleteSelected"
        >
          批量删除
        </el-button>
      </div>

      <el-tabs v-model="activeTab" type="border-card">
      <!-- ============ 角色 Tab ============ -->
      <!-- 各 pane 显式声明 name 与 activeTab 对齐：Element Plus 对未命名 pane 自动按序
           赋 name（'0'/'1'/...），v-model 初值 'characters' 无法匹配会导致全部 pane
           v-show=false（内容区空白，C-BUG-1 空白根因） -->
      <el-tab-pane name="characters" :label="`角色 (${visibleCharacters.length})`">
        <div class="card-grid">
          <el-card v-for="c in visibleCharacters" :key="c.id" shadow="hover" class="asset-card">
            <div class="img-wrap">
              <!-- 批量删除选择圈：浮于图片左上角（纯圆圈无文字） -->
              <el-checkbox
                class="pick-circle"
                :model-value="selectedIds.characters.includes(c.id)"
                @change="(v: any) => toggleSelected('characters', c.id, !!v)"
              />
              <el-image
                v-if="c.asset_url"
                :src="c.asset_url"
                :preview-src-list="[c.asset_url]"
                fit="cover"
                class="asset-img"
                preview-teleported
              />
              <div v-else class="img-placeholder">
                <span>{{ c.name }}</span>
                <el-button
                  size="small"
                  type="primary"
                  :loading="genBusyIds.includes(c.id) || c.generation_status === 'generating'"
                  @click="generatePortrait(c)"
                >
                  生成定妆照
                </el-button>
              </div>
              <!-- 生成中遮罩：重生成/首次生成期间覆盖图片区 -->
              <div
                v-if="genBusyIds.includes(c.id) || c.generation_status === 'generating'"
                class="gen-overlay"
              >
                <el-icon class="is-loading"><Loading /></el-icon>
                <span>定妆照生成中…</span>
              </div>
              <!-- 面部锁定徽标（左上角选择圈右侧） -->
              <el-tooltip v-if="c.face_locked" content="面部已锁定" placement="top">
                <el-icon class="lock-badge"><Lock /></el-icon>
              </el-tooltip>
              <!-- A-04 参考图标识：已配置参考图（生成时作为锚点） -->
              <el-tooltip v-if="c.reference_image_url" content="已设置参考图，生成时作为参考" placement="top">
                <el-tag class="ref-badge" size="small" type="warning" effect="dark">参考图</el-tag>
              </el-tooltip>
              <el-tag
                class="status-tag"
                size="small"
                effect="dark"
                :type="statusType[c.generation_status] ?? 'info'"
              >
                {{ statusLabel[c.generation_status] ?? c.generation_status }}
              </el-tag>
            </div>
            <div class="card-info">
              <div class="card-name">{{ c.name }}</div>
              <div class="card-desc">{{ c.description || '暂无描述' }}</div>
              <!-- A-05 标签展示 -->
              <div v-if="(c.tags ?? []).length" class="card-tags">
                <el-tag v-for="t in c.tags" :key="t" size="small" effect="plain">{{ t }}</el-tag>
              </div>
              <!-- 底部操作行：上传替换（左）+ 更多（右，锁定/重生成/提示词/校验/删除收拢） -->
              <div class="card-ops">
                <!-- 上传替换（A-04：真实上传到 /api/media/upload 后回写参考图） -->
                <el-upload
                  :auto-upload="false"
                  :show-file-list="false"
                  accept="image/*"
                  :on-change="(f: UploadFile) => onUploadReplace(c, f)"
                >
                  <el-button size="small" text :loading="uploadingRefIds.includes(c.id)">
                    上传替换
                  </el-button>
                </el-upload>
                <el-dropdown class="more-dd" trigger="click" @command="(cmd: string) => {
                  if (cmd === 'regen') generatePortrait(c)
                  else if (cmd === 'prompt') openPromptDrawer(c, 'character')
                  else if (cmd === 'cc') openConsistency(c)
                  else if (cmd === 'del') removeCharacter(c)
                }">
                  <el-button size="small" text>更多</el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="regen">重生成</el-dropdown-item>
                      <el-dropdown-item command="prompt">编辑提示词</el-dropdown-item>
                      <el-dropdown-item command="cc">一致性校验</el-dropdown-item>
                      <el-dropdown-item command="del" divided>删除</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </div>
            </div>
          </el-card>
          <el-empty
            v-if="!visibleCharacters.length"
            :description="episodeFilterActive ? '本集尚未启用角色资产，点击右上角「启用资产」提取本集出场角色' : '暂无角色资产'"
            class="grid-empty"
          />
        </div>
      </el-tab-pane>

      <!-- ============ 场景 Tab ============ -->
      <el-tab-pane name="scenes" :label="`场景 (${visibleScenes.length})`">
        <div class="card-grid">
          <el-card v-for="s in visibleScenes" :key="s.id" shadow="hover" class="asset-card">
            <div class="img-wrap">
              <!-- 批量删除选择圈：浮于图片左上角（与角色卡一致） -->
              <el-checkbox
                class="pick-circle"
                :model-value="selectedIds.scenes.includes(s.id)"
                @change="(v: any) => toggleSelected('scenes', s.id, !!v)"
              />
              <!-- 场景无图：空场景占位（RD-19）+ 生成入口 -->
              <div v-if="!s.asset_url" class="img-placeholder scene-empty">
                <span class="scene-empty-tag">空场景</span>
                <span>{{ s.name }}</span>
                <el-button
                  size="small"
                  type="primary"
                  :loading="sceneGenBusyIds.includes(s.id)"
                  @click="generateScene(s)"
                >
                  生成场景图
                </el-button>
              </div>
              <el-image
                v-else
                :src="s.asset_url"
                :preview-src-list="[s.asset_url]"
                fit="cover"
                class="asset-img"
                preview-teleported
              />
              <!-- 生成中遮罩：重生成/首次生成期间覆盖图片区 -->
              <div v-if="sceneGenBusyIds.includes(s.id)" class="gen-overlay">
                <el-icon class="is-loading"><Loading /></el-icon>
                <span>场景图生成中…</span>
              </div>
              <el-tag v-if="s.is_locked" class="status-tag" size="small" effect="dark" type="success">
                已锁定
              </el-tag>
              <!-- A-04 参考图标识：已配置参考图（生成场景图时作为参考） -->
              <el-tooltip v-if="s.reference_image_url" content="已设置参考图，生成时作为参考" placement="top">
                <el-tag class="ref-badge" size="small" type="warning" effect="dark">参考图</el-tag>
              </el-tooltip>
            </div>
            <div class="card-info">
              <div class="card-name">{{ s.name }}</div>
              <div class="card-desc">{{ s.description || '暂无描述' }}</div>
              <!-- A-05 标签展示 -->
              <div v-if="(s.tags ?? []).length" class="card-tags">
                <el-tag v-for="t in s.tags" :key="t" size="small" effect="plain">{{ t }}</el-tag>
              </div>
              <!-- 底部操作行：上传替换（左）+ 更多（右，提示词/锁定/删除收拢，与角色卡一致） -->
              <div class="card-ops">
                <el-upload
                  :auto-upload="false"
                  :show-file-list="false"
                  accept="image/*"
                  :on-change="(f: UploadFile) => onSceneUploadReplace(s, f)"
                >
                  <el-button size="small" text :loading="uploadingRefIds.includes(s.id)">
                    上传替换
                  </el-button>
                </el-upload>
                <el-dropdown class="more-dd" trigger="click" @command="(cmd: string) => {
                  if (cmd === 'regen') generateScene(s)
                  else if (cmd === 'prompt') openPromptDrawer(s, 'scene')
                  else if (cmd === 'del') removeScene(s)
                }">
                  <el-button size="small" text>更多</el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item v-if="s.asset_url" command="regen">重生成</el-dropdown-item>
                      <el-dropdown-item command="prompt">编辑提示词</el-dropdown-item>
                      <el-dropdown-item command="del" divided>删除</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </div>
            </div>
          </el-card>
          <el-empty
            v-if="!visibleScenes.length"
            :description="episodeFilterActive ? '本集尚未启用场景资产，点击右上角「启用资产」从剧本正文提取' : '暂无场景资产'"
            class="grid-empty"
          />
        </div>
      </el-tab-pane>

      <!-- ============ 道具 Tab ============ -->
      <el-tab-pane name="props" :label="`道具 (${visibleProps.length})`">
        <div class="card-grid">
          <el-card v-for="p in visibleProps" :key="p.id" shadow="hover" class="asset-card">
            <div class="img-wrap">
              <!-- 批量删除选择圈：浮于图片左上角（与角色卡一致） -->
              <el-checkbox
                class="pick-circle"
                :model-value="selectedIds.props.includes(p.id)"
                @change="(v: any) => toggleSelected('props', p.id, !!v)"
              />
              <!-- 道具无图：占位 + 生成入口 -->
              <div v-if="!p.asset_url" class="img-placeholder">
                <span>{{ p.name }}</span>
                <el-button
                  size="small"
                  type="primary"
                  :loading="propGenBusyIds.includes(p.id)"
                  @click="generateProp(p)"
                >
                  生成道具图
                </el-button>
              </div>
              <el-image
                v-else
                :src="p.asset_url"
                :preview-src-list="[p.asset_url]"
                fit="cover"
                class="asset-img"
                preview-teleported
              />
              <!-- 生成中遮罩：重生成/首次生成期间覆盖图片区 -->
              <div v-if="propGenBusyIds.includes(p.id)" class="gen-overlay">
                <el-icon class="is-loading"><Loading /></el-icon>
                <span>道具图生成中…</span>
              </div>
              <el-tag v-if="p.is_locked" class="status-tag" size="small" effect="dark" type="success">
                已锁定
              </el-tag>
            </div>
            <div class="card-info">
              <div class="card-name">{{ p.name }}</div>
              <div class="card-desc">{{ p.description || '暂无描述' }}</div>
              <!-- A-05 标签展示 -->
              <div v-if="(p.tags ?? []).length" class="card-tags">
                <el-tag v-for="t in p.tags" :key="t" size="small" effect="plain">{{ t }}</el-tag>
              </div>
              <!-- 底部操作行：更多（右，提示词/锁定/删除收拢，与角色卡一致）；
                   道具无参考图字段（A-04 仅角色/场景），不提供上传替换 -->
              <div class="card-ops">
                <el-dropdown class="more-dd" trigger="click" @command="(cmd: string) => {
                  if (cmd === 'regen') generateProp(p)
                  else if (cmd === 'prompt') openPromptDrawer(p, 'prop')
                  else if (cmd === 'del') removeProp(p)
                }">
                  <el-button size="small" text>更多</el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item v-if="p.asset_url" command="regen">重生成</el-dropdown-item>
                      <el-dropdown-item command="prompt">编辑提示词</el-dropdown-item>
                      <el-dropdown-item command="del" divided>删除</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </div>
            </div>
          </el-card>
          <el-empty
            v-if="!visibleProps.length"
            :description="episodeFilterActive ? '本集尚未启用道具资产，点击右上角「启用资产」从剧本正文提取' : '暂无道具资产'"
            class="grid-empty"
          />
        </div>
      </el-tab-pane>

      </el-tabs>
    </template>

    <!-- ============ 提示词编辑抽屉（角色：锁定层只读 + 可编辑层；
         场景/道具：AI 六要素识别 → 编辑 → 确认合成保存） ============ -->
    <el-drawer v-model="promptVisible" :title="`编辑提示词 · ${promptName}`" size="440px" append-to-body>
      <!-- 角色：提示词直接取自角色设定（锁定层 + 可编辑覆盖层） -->
      <template v-if="promptKind === 'character'">
        <div class="layer-label">
          系统锁定层（只读，约束角色一致性的标准化提示词）
        </div>
        <div class="locked-layer">{{ promptLockedText || '（空）' }}</div>
        <div class="layer-label">用户可编辑层（保存后覆盖生效）</div>
        <el-input
          v-model="promptEditableText"
          type="textarea"
          :rows="6"
          placeholder="调整细节描述，例：侧光、胶片质感、唯美二次元"
        />
        <div class="drawer-ops">
          <el-button @click="promptVisible = false">取消</el-button>
          <el-button type="primary" :loading="promptSaving" @click="savePrompt">保存</el-button>
        </div>
      </template>

      <!-- 场景/道具：AI 识别六要素（剧本正文提取）→ 编辑 → 确认合成保存 -->
      <template v-else>
        <div class="layer-label">当前提示词（锁定层，只读）</div>
        <div class="locked-layer">{{ promptLockedText || '（空）' }}</div>

        <el-button
          type="primary"
          plain
          style="width: 100%"
          :loading="suggestLoading"
          @click="runSuggestPrompt"
        >
          {{ suggestLoading ? 'AI 正在从剧本正文识别…' : 'AI 识别六要素（从剧本正文提取）' }}
        </el-button>
        <p v-if="!suggestElements" class="suggest-tip">
          场景将识别：画风基底、空间类型、核心锚点（3–5 个）、光照氛围、构图/视角偏好、负面约束；
          道具将识别：尺度参照、核心锚点（3–5 个）、材质与表面工艺、唯一标记、功能与操作方式、可读文字政策。
        </p>

        <template v-if="suggestElements">
          <div class="layer-label">
            {{ promptKind === 'scene' ? '场景六要素' : '道具六要素' }}（可修改，确认后合成提示词）
          </div>
          <template v-if="promptKind === 'scene'">
            <div class="element-item">
              <span class="element-label">画风基底</span>
              <el-input v-model="suggestElements.style_base" placeholder="画风描述" />
            </div>
            <div class="element-item">
              <span class="element-label">空间类型</span>
              <el-input v-model="suggestElements.space_type" placeholder="功能性空间" />
            </div>
            <div class="element-item">
              <span class="element-label">核心锚点（3–5 个，顿号分隔）</span>
              <el-input v-model="suggestAnchors" placeholder="物体/结构，顿号分隔" />
            </div>
            <div class="element-item">
              <span class="element-label">光照氛围</span>
              <el-input v-model="suggestElements.lighting" placeholder="光照描述" />
            </div>
            <div class="element-item">
              <span class="element-label">构图/视角偏好</span>
              <el-input v-model="suggestElements.composition" placeholder="构图与视角" />
            </div>
            <div class="element-item">
              <span class="element-label">负面约束</span>
              <el-input v-model="suggestElements.negative" placeholder="画面中不应出现的元素" />
            </div>
          </template>
          <template v-else>
            <div class="element-item">
              <span class="element-label">尺度参照</span>
              <el-input v-model="suggestElements.scale_ref" placeholder="与常见物体的相对大小" />
            </div>
            <div class="element-item">
              <span class="element-label">核心锚点（3–5 个，顿号分隔）</span>
              <el-input v-model="suggestAnchors" placeholder="形状/部件特征，顿号分隔" />
            </div>
            <div class="element-item">
              <span class="element-label">材质与表面工艺</span>
              <el-input v-model="suggestElements.material" placeholder="材质与工艺" />
            </div>
            <div class="element-item">
              <span class="element-label">唯一标记</span>
              <el-input v-model="suggestElements.marker" placeholder="可辨识的独有特征" />
            </div>
            <div class="element-item">
              <span class="element-label">功能与操作方式</span>
              <el-input v-model="suggestElements.usage" placeholder="功能与操作" />
            </div>
            <div class="element-item">
              <span class="element-label">可读文字政策</span>
              <el-input v-model="suggestElements.text_policy" placeholder="表面文字处理方式" />
            </div>
          </template>

          <div class="layer-label">合成提示词预览</div>
          <div class="locked-layer">{{ composeElementsPrompt() || '（请完善要素）' }}</div>
        </template>

        <div class="drawer-ops">
          <el-button @click="promptVisible = false">取消</el-button>
          <el-button
            type="primary"
            :loading="promptSaving"
            :disabled="!suggestElements"
            @click="confirmSuggestSave"
          >
            确认并保存
          </el-button>
        </div>
      </template>
    </el-drawer>

    <!-- ============ S-05 变体抽屉 ============ -->
    <el-drawer
      v-model="variantVisible"
      :title="`${variantChar?.name ?? ''} · 造型与变体`"
      size="480px"
      append-to-body
    >
      <div v-loading="variantLoading">
        <div class="layer-label">已有变体</div>
        <el-empty
          v-if="!variantList.length"
          description="暂无变体，可新增造型 / 光线变体"
          :image-size="72"
        />
        <div v-else class="variant-list">
          <div v-for="v in variantList" :key="v.id" class="variant-item">
            <el-image
              v-if="v.asset_url"
              :src="v.asset_url"
              :preview-src-list="[v.asset_url]"
              fit="cover"
              class="variant-img"
              preview-teleported
            />
            <div v-else class="variant-img variant-img--empty">待生成</div>
            <div class="variant-meta">
              <span class="variant-name">{{ v.name }}</span>
              <el-tag size="small" type="info" effect="plain">
                {{ variantTypeLabel[v.variant_type] ?? v.variant_type }}
              </el-tag>
            </div>
          </div>
        </div>

        <el-divider>新增变体</el-divider>
        <el-form label-width="90px">
          <el-form-item label="变体名称" required>
            <el-input v-model="variantForm.name" maxlength="50" placeholder="例：战斗装" />
          </el-form-item>
          <el-form-item label="变体类型" required>
            <el-select v-model="variantForm.variant_type" style="width: 100%">
              <el-option
                v-for="opt in variantTypeOptions"
                :key="opt.value"
                :label="opt.label"
                :value="opt.value"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="继承声明">
            <div class="inherit-box">
              <!-- RD-14：面部特征必须继承（换装必继承），默认勾选且禁用 -->
              <el-checkbox v-model="inheritFace" disabled>面部特征（脸型 / 五官 / 发色）必须继承</el-checkbox>
              <el-checkbox v-model="outfitIndependent">造型独立（服饰 / 画风作为独立特征生成）</el-checkbox>
              <div class="inherit-tip">说明：不勾选的项将作为独立特征生成</div>
            </div>
          </el-form-item>
        </el-form>
        <div class="drawer-ops">
          <el-button
            type="primary"
            :disabled="!inheritFace"
            :loading="variantSubmitting"
            @click="submitVariant"
          >
            生成变体
          </el-button>
        </div>
      </div>
    </el-drawer>

    <!-- ============ 一致性校验弹窗 ============ -->
    <el-dialog v-model="ccVisible" :title="`一致性校验 · ${ccChar?.name ?? ''}`" width="680px" append-to-body>
      <div class="cc-select-row">
        <span>对比对象：</span>
        <el-select v-model="ccVariantId" placeholder="主形象" clearable style="width: 220px">
          <el-option
            v-for="v in ccVariants"
            :key="v.id"
            :label="`${v.name}（${variantTypeLabel[v.variant_type] ?? v.variant_type}）`"
            :value="v.id"
          />
        </el-select>
        <el-button type="primary" :loading="ccLoading" @click="runConsistency">开始校验</el-button>
      </div>
      <div class="cc-compare">
        <div class="cc-pane">
          <div class="cc-pane-title">主形象</div>
          <el-image
            v-if="ccChar?.asset_url"
            :src="ccChar.asset_url"
            fit="contain"
            class="cc-img"
            :preview-src-list="[ccChar.asset_url]"
            preview-teleported
          />
          <div v-else class="cc-img cc-img--empty">暂无主照</div>
        </div>
        <div class="cc-pane">
          <div class="cc-pane-title">对比图</div>
          <el-image
            v-if="ccRightUrl"
            :src="ccRightUrl"
            fit="contain"
            class="cc-img"
            :preview-src-list="[ccRightUrl]"
            preview-teleported
          />
          <div v-else class="cc-img cc-img--empty">暂无对比图</div>
        </div>
      </div>
      <template v-if="ccResult">
        <div class="cc-sim-row">
          <span>相似度：</span>
          <el-progress
            :percentage="Math.round(ccResult.similarity * 100)"
            :stroke-width="14"
            :color="ccResult.passed ? '#67c23a' : '#f56c6c'"
            class="cc-progress"
          />
          <el-tag :type="ccResult.passed ? 'success' : 'danger'" effect="dark">
            {{ ccResult.passed ? '通过' : '未通过' }}
          </el-tag>
        </div>
        <div class="cc-report">{{ ccResult.report }}</div>
      </template>
    </el-dialog>

    <!-- ============ S-06 复用资产弹窗（跨项目总览 → 复制到当前项目） ============ -->
    <el-dialog v-model="reuseVisible" title="复用其他项目的资产" width="720px" append-to-body>
      <div class="reuse-search-row">
        <el-input
          v-model="reuseSearch"
          placeholder="按名称/描述搜索角色与场景"
          clearable
          class="reuse-search-input"
          @keyup.enter="searchReuse"
        />
        <el-button type="primary" :loading="reuseLoading" @click="searchReuse">搜索</el-button>
      </div>
      <el-table v-loading="reuseLoading" :data="reuseRows" size="small" max-height="420">
        <el-table-column label="形象" width="72">
          <template #default="{ row }">
            <el-image
              v-if="row.asset_url"
              :src="row.asset_url"
              fit="cover"
              class="reuse-thumb"
              :preview-src-list="[row.asset_url]"
              preview-teleported
            />
            <div v-else class="reuse-thumb reuse-thumb--empty">{{ row.asset_type === 'scene' ? '景' : '角' }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="名称" min-width="120" show-overflow-tooltip />
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">
              {{ row.asset_type === 'scene' ? '场景' : '角色' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="project_title" label="所属项目" min-width="140" show-overflow-tooltip />
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button
              size="small"
              type="primary"
              plain
              :disabled="row.project_id === projectId"
              :loading="copyingIds.includes(row.id)"
              @click="copyReuse(row)"
            >
              {{ row.project_id === projectId ? '当前项目' : '复制' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="reuse-tip">复制后会生成名称带"(副本)"的新资产；角色副本需重新生成定妆照前可沿用原图。</div>
    </el-dialog>
  </div>
</template>

<style scoped>
.assets-view {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-5);
}
.view-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
/* S-06：标题右侧操作按钮组（复用资产 + 生成全部主照） */
.head-actions {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
/* S-06：复用资产弹窗 */
.reuse-search-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  margin-bottom: var(--mf-sp-4);
}
.reuse-search-input {
  flex: 1;
}
.reuse-thumb {
  width: 44px;
  height: 44px;
  border-radius: var(--mf-r-ctl, 6px);
  object-fit: cover;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--mf-surface-2, #f2f3f5);
  color: var(--mf-text-2, #909399);
  font-size: var(--mf-fs-sm, 12px);
}
.reuse-tip {
  margin-top: var(--mf-sp-3);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.view-head .page-title {
  margin: 0;
}
/* 工具行：当前集上下文 + 批量删除 */
.tab-toolbar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  margin-bottom: var(--mf-sp-3);
}
/* S-06 当前集标签（按集工作上下文） */
.scope-tag {
  flex-shrink: 0;
}
/* 批量删除按钮靠右（原标签筛选/已选计数控件已移除） */
.tab-toolbar > .el-button:last-child {
  margin-left: auto;
}
/* A-05 卡片标签展示行与多选框 */
.card-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: var(--mf-sp-2);
}
.pick-box {
  height: 24px;
  margin-right: 4px;
}
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: var(--mf-sp-4);
}
.grid-empty {
  grid-column: 1 / -1;
}
.asset-card :deep(.el-card__body) {
  padding: 0;
}
.img-wrap {
  position: relative;
  height: 180px;
  border-radius: var(--mf-r-ctl) var(--mf-r-ctl) 0 0;
  overflow: hidden;
}
.asset-img {
  width: 100%;
  height: 100%;
  display: block;
}
.img-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--mf-sp-2);
  background: linear-gradient(135deg, var(--mf-surface-2), var(--mf-border));
  color: var(--mf-text-2);
}
.scene-empty-tag {
  font-size: var(--mf-fs-sm);
  color: var(--mf-text-3);
  border: 1px dashed var(--mf-text-3);
  border-radius: var(--mf-r-pill);
  padding: 0 8px;
}
.lock-badge {
  position: absolute;
  top: 8px;
  /* 左上角让位于批量删除选择圈（pick-circle），右移一圈位 */
  left: 36px;
  background: rgba(0, 0, 0, 0.55);
  color: #ffd04b;
  border-radius: 50%;
  padding: 5px;
}
.status-tag {
  position: absolute;
  top: 8px;
  right: 8px;
}
/* 生成中遮罩：覆盖整张图片区（重生成/首次生成期间给出明确加载反馈） */
.gen-overlay {
  position: absolute;
  inset: 0;
  z-index: 2;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  font-size: var(--mf-fs-sm);
}
.gen-overlay .el-icon {
  font-size: 26px;
}
/* A-04 参考图标识：位于锁定徽标下方（left 与 lock-badge 对齐） */
.ref-badge {
  position: absolute;
  top: 40px;
  left: 36px;
}
/* 角色卡批量删除选择圈：浮于图片左上角，圆形半透明白底保证图片上可见 */
.pick-circle {
  position: absolute;
  top: 8px;
  left: 8px;
  z-index: 3;
  height: auto;
  margin-right: 0;
}
.pick-circle :deep(.el-checkbox__inner) {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.85);
  border-color: rgba(0, 0, 0, 0.35);
  transition: transform var(--mf-dur) var(--mf-ease), background var(--mf-dur) var(--mf-ease),
    box-shadow var(--mf-dur) var(--mf-ease);
}
/* 不显示对勾：选中反馈由填充色 + 光晕 + 微放大承担 */
.pick-circle :deep(.el-checkbox__inner::after) {
  display: none;
}
/* 选中态：品牌绿填充 + 浅绿光晕 + 微放大（无对勾），浮于图片上有明显反馈 */
.pick-circle :deep(.el-checkbox__input.is-checked .el-checkbox__inner) {
  background: var(--mf-brand);
  border-color: var(--mf-brand);
  box-shadow: 0 0 0 3px var(--mf-brand-soft);
  transform: scale(1.12);
}
.card-info {
  padding: var(--mf-sp-3);
}
.card-name {
  font-weight: var(--mf-fw-semibold);
}
.card-desc {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
  margin: 4px 0 var(--mf-sp-2);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 34px;
}
.card-ops {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 2px;
}
/* 角色卡「更多」下拉靠右（底部操作行仅剩上传替换 + 更多） */
.card-ops .more-dd {
  margin-left: auto;
}
.layer-label {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  margin: var(--mf-sp-2) 0;
}
.locked-layer {
  background: var(--mf-surface-2);
  border-radius: var(--mf-r-ctl);
  padding: var(--mf-sp-3);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
  margin-bottom: var(--mf-sp-4);
}
.drawer-ops {
  margin-top: var(--mf-sp-4);
  text-align: right;
}
/* 场景/道具六要素识别：说明文案与要素编辑行 */
.suggest-tip {
  margin: var(--mf-sp-2) 0;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
}
.element-item {
  margin-bottom: var(--mf-sp-2);
}
.element-label {
  display: block;
  margin-bottom: 4px;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.variant-list {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
}
.variant-item {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.variant-img {
  width: 72px;
  height: 72px;
  border-radius: var(--mf-r-ctl);
  flex-shrink: 0;
}
.variant-img--empty {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--mf-surface-2);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.variant-meta {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.variant-name {
  font-weight: var(--mf-fw-semibold);
}
.inherit-box {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.inherit-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.cc-select-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  margin-bottom: var(--mf-sp-4);
}
.cc-compare {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--mf-sp-4);
}
.cc-pane-title {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  margin-bottom: var(--mf-sp-2);
  text-align: center;
}
.cc-img {
  width: 100%;
  height: 220px;
  background: var(--mf-surface-2);
  border-radius: var(--mf-r-ctl);
}
.cc-img--empty {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--mf-text-3);
}
.cc-sim-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  margin-top: var(--mf-sp-4);
}
.cc-progress {
  flex: 1;
}
.cc-report {
  margin-top: var(--mf-sp-3);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
}
</style>
