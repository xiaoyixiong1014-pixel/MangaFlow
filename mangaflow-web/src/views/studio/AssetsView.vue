<!--
  资产管理页（文件级注释）。

  对应原型 S-04（资产管理）+ S-05（资产变体面板），路由 /projects/:id/assets。
  功能：
  - 四 Tab（角色/场景/道具/风格，各 pane 显式声明 name 与 activeTab 对齐，修复
    v-model 初值不匹配自动索引导致的内容区空白缺陷 C-BUG-1）+ 「生成全部主照」
    批量按钮（确认框提示张数与积分）；
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
  - A-05 标签：卡片展示 tags（el-tag）；工具栏标签下拉（数据源=当前项目资产 tags
    去重聚合）经后端 ?tag= JSONB 包含匹配过滤当前 Tab 列表；「标签」轻量弹窗
    （el-select multiple allow-create，≤10 个）编辑四类资产 tags；
  - A-05 批量删除：四类 Tab 卡片多选（选择框）+「批量删除」按钮（选中 >0 可用，
    confirm 二次确认后调 batch-delete 接口，跳过项提示原因）；
  - 场景卡/道具卡简化：编辑提示词 + 锁定切换；场景无图显示「空场景」占位（RD-19）；
  - 风格 Tab：卡列表 + 设为默认（updateStyle is_default）；
  - 一致性校验：consistencyCheck → el-dialog 并排对比（原图 + 变体图）+
    相似度进度条（passed 绿 / 未通过红）。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Lock } from '@element-plus/icons-vue'
import * as studioApi from '@/api/studio'
import * as mediaApi from '@/api/media'
import type { UploadFile } from 'element-plus'
import type {
  AssetVariant,
  AssetsGroup,
  CharacterAsset,
  ConsistencyCheckResult,
  CrossProjectAsset,
  PropAsset,
  SceneAsset,
  StyleAsset,
} from '@/types'

const route = useRoute()

/** 当前项目 ID（取路由参数） */
const projectId = computed(() => Number(route.params.id))

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

/** 请求序号（竞态保护：响应返回时若已有更新的请求发出，则丢弃本次过期响应） */
let loadSeq = 0
/** 组件是否已卸载（卸载后在途响应禁止回写状态） */
let disposed = false

/**
 * 拉取四类资产列表（quiet=true 时不切换骨架屏，供轮询与操作后刷新使用）。
 * 带请求序号竞态保护：仅允许最新一次请求回写状态，丢弃过期响应；
 * 失败时非静默进入错误兜底态、静默轮询保持旧数据不惊扰用户。
 * A-05：聚合拉取后若某 Tab 设置了标签筛选，再按 Tab 调后端 ?tag= 过滤结果覆盖
 * 该 Tab 列表（保持接口语义：过滤在后端 JSONB @> 完成）。
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
    const group = await studioApi.listAssets(projectId.value)
    // 过期响应或组件已卸载：直接丢弃，不回写状态
    if (disposed || seq !== loadSeq) return
    assets.value = group
    rebuildTagOptionsCache(group)
    // A-05：逐 Tab 应用标签筛选（有筛选的 Tab 用后端 ?tag= 过滤结果覆盖）
    for (const kind of TAB_KEYS) {
      const tag = tagFilters.value[kind]
      if (!tag) continue
      const page = await studioApi.listAssetsByType(projectId.value, kind, tag)
      if (disposed || seq !== loadSeq) return
      assets.value = { ...assets.value, [kind]: page.items } as AssetsGroup
    }
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
// 锁定 / 解锁面部层（RD-12）
// ============================================================

/**
 * 确认并锁定角色面部层（confirm 二次确认）。
 * @param c 目标角色
 * @returns 无
 */
async function lockFace(c: CharacterAsset): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `锁定后「${c.name}」的面部特征将在全项目保持一致，重生成不再影响面部。是否锁定？`,
      '锁定面部',
      { type: 'warning' },
    )
  } catch {
    return
  }
  const updated = await studioApi.lockCharacter(projectId.value, c.id)
  Object.assign(c, updated)
  ElMessage.success('面部已锁定')
}

/**
 * 解锁角色面部层。
 * @param c 目标角色
 * @returns 无
 */
async function unlockFace(c: CharacterAsset): Promise<void> {
  const updated = await studioApi.unlockCharacter(projectId.value, c.id)
  Object.assign(c, updated)
  ElMessage.success('面部已解锁')
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
    ElMessage.success('提示词已更新')
  } finally {
    promptSaving.value = false
  }
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
 * 角色参考图上传替换回调（A-04）：选择文件后先调 POST /api/media/upload
 * 落库素材，再用返回 URL PATCH 角色 reference_image_url（后端生成定妆照
 * 时将该参考图作为锚点透传图像渠道），成功后回写角色卡状态。
 * @param character 目标角色
 * @param uploadFile el-upload on-change 回调的文件对象（raw 为原始 File）
 * @returns 无
 */
async function onUploadReplace(character: CharacterAsset, uploadFile: UploadFile): Promise<void> {
  const raw = uploadFile.raw
  if (!raw) return
  if (uploadingRefIds.value.includes(character.id)) return
  uploadingRefIds.value = [...uploadingRefIds.value, character.id]
  try {
    const media = await mediaApi.upload(raw, { media_type: 'image', project_id: projectId.value })
    const updated = await studioApi.updateCharacter(projectId.value, character.id, {
      reference_image_url: media.url,
    })
    Object.assign(character, updated)
    ElMessage.success(`「${character.name}」参考图已上传并保存，下次生成定妆照将作为参考`)
  } finally {
    uploadingRefIds.value = uploadingRefIds.value.filter((i) => i !== character.id)
  }
}

// ============================================================
// 删除角色
// ============================================================

/**
 * 删除角色（confirm + 软删除）。
 * @param c 目标角色
 * @returns 无
 */
async function removeCharacter(c: CharacterAsset): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除角色「${c.name}」吗？删除后可联系管理员恢复。`, '删除角色', {
      type: 'warning',
    })
  } catch {
    return
  }
  await studioApi.deleteCharacter(projectId.value, c.id)
  ElMessage.success('角色已删除')
  await loadAssets(true)
}

// ============================================================
// 场景 / 道具锁定切换
// ============================================================

/**
 * 切换场景锁定状态。
 * @param s 目标场景
 * @returns 无
 */
async function toggleSceneLock(s: SceneAsset): Promise<void> {
  const updated = await studioApi.updateScene(projectId.value, s.id, { is_locked: !s.is_locked })
  Object.assign(s, updated)
}

/**
 * 切换道具锁定状态。
 * @param p 目标道具
 * @returns 无
 */
async function togglePropLock(p: PropAsset): Promise<void> {
  const updated = await studioApi.updateProp(projectId.value, p.id, { is_locked: !p.is_locked })
  Object.assign(p, updated)
}

// ============================================================
// 风格设为默认
// ============================================================

/**
 * 把风格设为项目默认（后端自动清除其他默认）。
 * @param st 目标风格
 * @returns 无
 */
async function setDefaultStyle(st: StyleAsset): Promise<void> {
  const updated = await studioApi.updateStyle(projectId.value, st.id, { is_default: true })
  Object.assign(st, updated)
  ElMessage.success(`已将「${st.name}」设为默认风格`)
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
// A-05 标签筛选 / 标签编辑 / 批量删除
// ============================================================

/** 资产类别键（复数形式，与后端 batch-delete 路由 asset_type 一致） */
type AssetKind = studioApi.AssetKind

/** 四类 Tab 的固定顺序（loadAssets 逐 Tab 应用标签筛选时遍历） */
const TAB_KEYS: AssetKind[] = ['characters', 'scenes', 'props', 'styles']

/** 类别 → 中文文案（批量删除确认文案用） */
const KIND_LABELS: Record<AssetKind, string> = {
  characters: '角色',
  scenes: '场景',
  props: '道具',
  styles: '风格',
}

/** 各 Tab 生效的标签筛选（undefined = 不过滤；过滤经后端 ?tag= 完成） */
const tagFilters = ref<Record<AssetKind, string | undefined>>({
  characters: undefined,
  scenes: undefined,
  props: undefined,
  styles: undefined,
})

/** 各 Tab 勾选待批量删除的资产 ID */
const selectedIds = ref<Record<AssetKind, number[]>>({
  characters: [],
  scenes: [],
  props: [],
  styles: [],
})

/** 标签选项缓存（每次未过滤聚合拉取后重建，过滤态下拉仍展示全量标签） */
const tagOptionsCache = ref<Record<AssetKind, string[]>>({
  characters: [],
  scenes: [],
  props: [],
  styles: [],
})

/** 批量删除进行中（防重复提交） */
const batchDeleting = ref(false)

/**
 * 从聚合数据重建各 Tab 的标签去重选项（中文序排序）。
 * @param group 四类资产聚合数据
 * @returns 无
 */
function rebuildTagOptionsCache(group: AssetsGroup): void {
  for (const kind of TAB_KEYS) {
    const set = new Set<string>()
    for (const a of group[kind] as Array<{ tags?: string[] }>) {
      for (const t of a.tags ?? []) set.add(t)
    }
    tagOptionsCache.value[kind] = [...set].sort((x, y) => x.localeCompare(y, 'zh'))
  }
}

/** 当前 Tab 的标签下拉选项 */
const activeTagOptions = computed(() => tagOptionsCache.value[activeTab.value])
/** 当前 Tab 勾选的资产 ID 列表 */
const activeSelectedIds = computed(() => selectedIds.value[activeTab.value])

/**
 * 标签筛选变更：清空当前 Tab 勾选并重新拉取（后端 ?tag= 过滤语义）。
 * @returns 无
 */
function onTagFilterChange(): void {
  selectedIds.value[activeTab.value] = []
  void loadAssets(true)
}

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

// ---------- 标签编辑轻量弹窗 ----------
/** 标签编辑弹窗可见性 */
const tagEditVisible = ref(false)
/** 编辑目标类别 */
const tagEditKind = ref<AssetKind>('characters')
/** 编辑目标资产 ID */
const tagEditId = ref(0)
/** 编辑目标资产名（仅展示） */
const tagEditName = ref('')
/** 编辑中的标签数组（el-select multiple allow-create） */
const tagEditValue = ref<string[]>([])
/** 标签保存中（防重复提交） */
const tagEditSaving = ref(false)

/** 标签编辑弹窗的候选项（当前编辑类别的全量标签） */
const tagEditOptions = computed(() => tagOptionsCache.value[tagEditKind.value])

/**
 * 打开标签编辑弹窗（四类资产通用，预填已有 tags）。
 * @param kind 资产类别
 * @param asset 目标资产（任意一类）
 * @returns 无
 */
function openTagEditor(
  kind: AssetKind,
  asset: CharacterAsset | SceneAsset | PropAsset | StyleAsset,
): void {
  tagEditKind.value = kind
  tagEditId.value = asset.id
  tagEditName.value = asset.name
  tagEditValue.value = [...(asset.tags ?? [])]
  tagEditVisible.value = true
}

/**
 * 保存标签编辑：按类别分发到对应更新接口（tags 数组整体替换），并回写本地卡片。
 * @returns 无
 */
async function saveTagEdit(): Promise<void> {
  if (tagEditSaving.value) return
  tagEditSaving.value = true
  try {
    // 前端先做与后端一致的规范化（去空白、去空项、最多 10 个）
    const tags = [...new Set(tagEditValue.value.map((t) => t.trim()).filter(Boolean))].slice(0, 10)
    const kind = tagEditKind.value
    const id = tagEditId.value
    let updated: { tags?: string[] }
    if (kind === 'characters') {
      updated = await studioApi.updateCharacter(projectId.value, id, { tags })
    } else if (kind === 'scenes') {
      updated = await studioApi.updateScene(projectId.value, id, { tags })
    } else if (kind === 'props') {
      updated = await studioApi.updateProp(projectId.value, id, { tags })
    } else {
      updated = await studioApi.updateStyle(projectId.value, id, { tags })
    }
    const list = assets.value[kind] as Array<CharacterAsset | SceneAsset | PropAsset | StyleAsset>
    const idx = list.findIndex((a) => a.id === id)
    if (idx >= 0 && list[idx]) Object.assign(list[idx]!, updated)
    rebuildTagOptionsCache(assets.value)
    tagEditVisible.value = false
    ElMessage.success('标签已保存')
  } finally {
    tagEditSaving.value = false
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

    <el-empty v-else-if="!assets.characters.length && !assets.scenes.length && !assets.props.length && !assets.styles.length"
      description="还没有资产，请先回到剧本页完成「实体提取确认」"
    >
      <el-button type="primary" @click="$router.push(`/projects/${projectId}/script/extract`)">
        去实体提取
      </el-button>
    </el-empty>

    <template v-else>
      <!-- A-05 工具行：当前 Tab 的标签筛选（后端 ?tag= 过滤）+ 批量删除 -->
      <div class="tab-toolbar">
        <el-select
          v-model="tagFilters[activeTab]"
          class="tag-filter"
          clearable
          filterable
          placeholder="按标签筛选当前分类"
          @change="onTagFilterChange"
          @clear="onTagFilterChange"
        >
          <el-option v-for="t in activeTagOptions" :key="t" :label="t" :value="t" />
        </el-select>
        <span class="toolbar-tip">已选 {{ activeSelectedIds.length }} 项</span>
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
      <el-tab-pane name="characters" :label="`角色 (${assets.characters.length})`">
        <div class="card-grid">
          <el-card v-for="c in assets.characters" :key="c.id" shadow="hover" class="asset-card">
            <div class="img-wrap">
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
              <!-- 面部锁定徽标 -->
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
              <div class="card-ops">
                <el-checkbox
                  class="pick-box"
                  :model-value="selectedIds.characters.includes(c.id)"
                  @change="(v: any) => toggleSelected('characters', c.id, !!v)"
                >
                  选择
                </el-checkbox>
                <el-button size="small" text @click="openTagEditor('characters', c)">标签</el-button>
                <el-button
                  v-if="c.asset_url && !c.face_locked"
                  size="small"
                  type="success"
                  plain
                  @click="lockFace(c)"
                >
                  确认并锁定
                </el-button>
                <el-button v-if="c.face_locked" size="small" text @click="unlockFace(c)">解锁</el-button>
                <el-button size="small" text type="primary" @click="openVariantDrawer(c)">变体</el-button>
                <el-dropdown trigger="click" @command="(cmd: string) => {
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
              </div>
            </div>
          </el-card>
          <el-empty
            v-if="!assets.characters.length"
            description="暂无角色资产"
            class="grid-empty"
          />
        </div>
      </el-tab-pane>

      <!-- ============ 场景 Tab ============ -->
      <el-tab-pane name="scenes" :label="`场景 (${assets.scenes.length})`">
        <div class="card-grid">
          <el-card v-for="s in assets.scenes" :key="s.id" shadow="hover" class="asset-card">
            <div class="img-wrap">
              <!-- 场景无图：空场景占位（RD-19） -->
              <div v-if="!s.asset_url" class="img-placeholder scene-empty">
                <span class="scene-empty-tag">空场景</span>
                <span>{{ s.name }}</span>
              </div>
              <el-image
                v-else
                :src="s.asset_url"
                :preview-src-list="[s.asset_url]"
                fit="cover"
                class="asset-img"
                preview-teleported
              />
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
              <div class="card-ops">
                <el-checkbox
                  class="pick-box"
                  :model-value="selectedIds.scenes.includes(s.id)"
                  @change="(v: any) => toggleSelected('scenes', s.id, !!v)"
                >
                  选择
                </el-checkbox>
                <el-button size="small" text @click="openTagEditor('scenes', s)">标签</el-button>
                <el-button size="small" text type="primary" @click="openPromptDrawer(s, 'scene')">
                  编辑提示词
                </el-button>
                <el-button size="small" text @click="toggleSceneLock(s)">
                  {{ s.is_locked ? '解锁' : '锁定' }}
                </el-button>
              </div>
            </div>
          </el-card>
          <el-empty v-if="!assets.scenes.length" description="暂无场景资产" class="grid-empty" />
        </div>
      </el-tab-pane>

      <!-- ============ 道具 Tab ============ -->
      <el-tab-pane name="props" :label="`道具 (${assets.props.length})`">
        <div class="card-grid">
          <el-card v-for="p in assets.props" :key="p.id" shadow="hover" class="asset-card">
            <div class="img-wrap">
              <div v-if="!p.asset_url" class="img-placeholder">
                <span>{{ p.name }}</span>
              </div>
              <el-image
                v-else
                :src="p.asset_url"
                :preview-src-list="[p.asset_url]"
                fit="cover"
                class="asset-img"
                preview-teleported
              />
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
              <div class="card-ops">
                <el-checkbox
                  class="pick-box"
                  :model-value="selectedIds.props.includes(p.id)"
                  @change="(v: any) => toggleSelected('props', p.id, !!v)"
                >
                  选择
                </el-checkbox>
                <el-button size="small" text @click="openTagEditor('props', p)">标签</el-button>
                <el-button size="small" text type="primary" @click="openPromptDrawer(p, 'prop')">
                  编辑提示词
                </el-button>
                <el-button size="small" text @click="togglePropLock(p)">
                  {{ p.is_locked ? '解锁' : '锁定' }}
                </el-button>
              </div>
            </div>
          </el-card>
          <el-empty v-if="!assets.props.length" description="暂无道具资产" class="grid-empty" />
        </div>
      </el-tab-pane>

      <!-- ============ 风格 Tab ============ -->
      <el-tab-pane name="styles" :label="`风格 (${assets.styles.length})`">
        <div class="card-grid">
          <el-card v-for="st in assets.styles" :key="st.id" shadow="hover" class="asset-card">
            <div class="img-wrap">
              <div v-if="!st.reference_url" class="img-placeholder">
                <span>{{ st.name }}</span>
              </div>
              <el-image
                v-else
                :src="st.reference_url"
                :preview-src-list="[st.reference_url]"
                fit="cover"
                class="asset-img"
                preview-teleported
              />
              <el-tag v-if="st.is_default" class="status-tag" size="small" effect="dark">
                默认
              </el-tag>
            </div>
            <div class="card-info">
              <div class="card-name">{{ st.name }}</div>
              <div class="card-desc">{{ st.preset_code || st.visual_prompt }}</div>
              <!-- A-05 标签展示 -->
              <div v-if="(st.tags ?? []).length" class="card-tags">
                <el-tag v-for="t in st.tags" :key="t" size="small" effect="plain">{{ t }}</el-tag>
              </div>
              <div class="card-ops">
                <el-checkbox
                  class="pick-box"
                  :model-value="selectedIds.styles.includes(st.id)"
                  @change="(v: any) => toggleSelected('styles', st.id, !!v)"
                >
                  选择
                </el-checkbox>
                <el-button size="small" text @click="openTagEditor('styles', st)">标签</el-button>
                <el-button
                  size="small"
                  type="primary"
                  plain
                  :disabled="st.is_default"
                  @click="setDefaultStyle(st)"
                >
                  设为默认
                </el-button>
              </div>
            </div>
          </el-card>
          <el-empty v-if="!assets.styles.length" description="暂无风格资产" class="grid-empty" />
        </div>
      </el-tab-pane>
      </el-tabs>

      <!-- ============ A-05 标签编辑轻量弹窗（四类资产通用） ============ -->
      <el-dialog
        v-model="tagEditVisible"
        :title="`编辑标签 · ${tagEditName}`"
        width="440px"
        append-to-body
      >
        <el-select
          v-model="tagEditValue"
          multiple
          filterable
          allow-create
          default-first-option
          :multiple-limit="10"
          placeholder="输入标签后回车创建，最多 10 个、每项 20 字以内"
          style="width: 100%"
        >
          <el-option v-for="t in tagEditOptions" :key="t" :label="t" :value="t" />
        </el-select>
        <template #footer>
          <el-button @click="tagEditVisible = false">取消</el-button>
          <el-button type="primary" :loading="tagEditSaving" @click="saveTagEdit">保存</el-button>
        </template>
      </el-dialog>
    </template>

    <!-- ============ 提示词编辑抽屉（两层：锁定层只读 + 可编辑层） ============ -->
    <el-drawer v-model="promptVisible" :title="`编辑提示词 · ${promptName}`" size="440px" append-to-body>
      <div class="layer-label">
        系统锁定层（只读，约束角色/场景一致性的标准化提示词）
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
/* A-05 工具行：标签筛选 + 批量删除 */
.tab-toolbar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  margin-bottom: var(--mf-sp-3);
}
.tag-filter {
  width: 220px;
}
.toolbar-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
  margin-right: auto;
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
  left: 8px;
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
/* A-04 参考图标识：位于锁定徽标下方（left 与 lock-badge 对齐） */
.ref-badge {
  position: absolute;
  top: 40px;
  left: 8px;
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
