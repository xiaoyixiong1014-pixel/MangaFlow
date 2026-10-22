<!--
  分镜设计页（文件级注释）。

  对应原型 S-06，路由 /projects/:id/storyboard。
  功能：
  - 空态：无分镜时展示「AI 拆解分镜」大按钮（decompose → loading → 刷新列表）；
  - 顶部工具条：总时长 / 预计成本（分镜数 × 10 积分文案，RD-20 拆解阶段成本可见）、
    节奏预警徽标（点开抽屉：storyboardWarnings → 告警 el-alert 列表，RD-22）、
    连续性检查（SB-06：suspect 红点 / info 灰点分组弹窗，点击项滚动定位分镜卡）、
    自动时长（SB-07：输入目标秒数等比缩放落库 + 前后对比摘要）、
    导出分镜表（xlsx / csv / pdf，SB-08）、
    「投递画布」按钮（deliverToCanvas → 成功后确认打开画布）；
  - 分镜卡片列表：vuedraggable 未安装（且禁止新增依赖），改用「上移 / 下移」按钮 +
    reorder(ordered_ids) 实现重排，注释说明为拖拽的替代实现；
  - 每卡展示：镜号 / 景别 / 机位角度 / 运镜 / 时长 / 情绪音效 Tag / 画面描述 /
    光影 / 对白 / 绑定资产 Tag（角色 + 场景 + 道具；link_confidence < 0.7 标黄
    提示确认，RD-24）；stale 引用资产过期标黄 + 「需重渲染」（P0-3）；
  - 每卡操作：编辑（el-dialog 表单：景别/机位角度/运镜/时长/画面/光影/负面提示词/
    对白/音效/情绪/prompt_editable/强绑场景 + 角色多选）、拆分（按对白行号）、
    合并（相邻两镜）、投递画布；
  - 底部「确认分镜，进入生成」→ 跳 /projects/:id/production。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Warning } from '@element-plus/icons-vue'
import * as studioApi from '@/api/studio'
import { useProjectStore } from '@/stores/project'
import type { AssetsGroup, Storyboard, StoryboardWarning } from '@/types'
import type { AutoFitDurationResult, ContinuityItem } from '@/api/studio'

const route = useRoute()
const router = useRouter()
const projectStore = useProjectStore()

/** 当前项目 ID（取路由参数） */
const projectId = computed(() => Number(route.params.id))

/** 当前选中剧集 ID（S-06 剧集切换器；null = 默认剧集，列表不传 episode_id） */
const currentEpisodeId = computed(() => projectStore.currentEpisodeId)

// ============================================================
// 分镜列表
// ============================================================

/** 首屏加载中 */
const loading = ref(true)
/** 分镜列表（按 sequence 升序） */
const boards = ref<Storyboard[]>([])

/**
 * 拉取分镜列表（quiet=true 静默刷新）。
 * S-06：按当前选中剧集过滤（默认剧集 = 不传 episode_id，返回全部，向后兼容）。
 * @param quiet 是否静默
 * @returns 无
 */
async function loadBoards(quiet = false): Promise<void> {
  if (!quiet) loading.value = true
  try {
    boards.value = await studioApi.listStoryboard(
      projectId.value,
      currentEpisodeId.value ?? undefined,
    )
  } finally {
    if (!quiet) loading.value = false
  }
}

// 切换剧集（S-06）后按新剧集重新加载分镜
watch(currentEpisodeId, () => {
  void loadBoards()
})

// ============================================================
// AI 拆解
// ============================================================

/** 拆解进行中（防重复点击） */
const decomposing = ref(false)

// ============================================================
// P0-1 锁定门禁拦截弹窗（主要角色定妆照未就绪）
// ============================================================

/** 未就绪角色条目（decompose 409 响应的 unready 结构） */
interface UnreadyCharacter {
  /** 角色 ID */
  character_id: number
  /** 角色名 */
  name: string
  /** 角色定位（lead/support/villain/extra，可能为 null） */
  role_type: string | null
  /** 是否已锁定面部 */
  face_locked: boolean
  /** 生成状态 pending/generating/done/failed */
  generation_status: string
  /** 是否已有定妆照 */
  has_image: boolean
  /** 未就绪原因文案（后端给出） */
  reason: string
}

/** 门禁拦截弹窗可见性 */
const gateVisible = ref(false)
/** 未就绪角色清单（弹窗表格数据） */
const gateUnready = ref<UnreadyCharacter[]>([])
/** 快速模式勾选（跳过锁定检查继续生成） */
const gateSkipCheck = ref(false)

/** 角色定位中文文案（弹窗表格展示用） */
const ROLE_LABELS: Record<string, string> = {
  lead: '主角',
  support: '配角',
  villain: '反派',
  extra: '群像',
}

/**
 * AI 拆解分镜（LLM 依据剧本拆出镜头序列）。
 * P0-1：默认走后端锁定门禁，409（asset_lock_not_ready）时打开未就绪弹窗；
 * 快速模式勾选后携带 skip_lock_check=true 重发。
 * S-06 按集工作流：跟随全局剧集切换器——选中具体集时只拆解并重建该集分镜。
 * @param skipLockCheck 是否跳过锁定门禁（快速模式）
 * @returns 无
 */
async function runDecompose(skipLockCheck = false): Promise<void> {
  if (decomposing.value) return
  decomposing.value = true
  try {
    const res = await studioApi.decompose(
      projectId.value,
      skipLockCheck ? { skip_lock_check: true } : undefined,
      currentEpisodeId.value ?? undefined,
    )
    const total = Number(res.total ?? 0)
    const scope = currentEpisodeId.value != null ? '当前集' : '全部剧集'
    ElMessage.success(
      `拆解完成（${scope}），共生成 ${total} 个分镜${res.degraded ? '（部分场景走了规则降级）' : ''}`,
    )
    gateVisible.value = false
    await loadBoards(true)
  } catch (e) {
    // P0-1 锁定门禁拦截：打开未就绪清单弹窗（其余错误由拦截器统一提示）
    const err = e as { response?: { status?: number; data?: { detail?: { code?: string; unready?: UnreadyCharacter[] } } } }
    if (
      err.response?.status === 409
      && err.response.data?.detail?.code === 'asset_lock_not_ready'
      && Array.isArray(err.response.data.detail.unready)
    ) {
      gateUnready.value = err.response.data.detail.unready
      gateSkipCheck.value = false
      gateVisible.value = true
    }
  } finally {
    decomposing.value = false
  }
}

/**
 * 「去资产页处理」：关闭弹窗并跳转资产管理页。
 * @returns 无
 */
function gotoAssets(): void {
  gateVisible.value = false
  void router.push(`/projects/${projectId.value}/assets`)
}

// ============================================================
// 顶部工具条统计
// ============================================================

/** 分镜总时长（秒） */
const totalDuration = computed(() => boards.value.reduce((sum, b) => sum + (b.duration || 0), 0))
/** 预估成本（每镜 10 积分文案，RD-20） */
const estimatedCost = computed(() => boards.value.length * 10)

/**
 * 总时长展示格式化：不足 1 分钟时省略"0分"，统一"X分X秒"（不再叠加括号秒数，避免单位混排）。
 * @param seconds 总秒数
 * @returns 形如"31秒"或"1分31秒"的文案
 */
function durationText(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return m > 0 ? `${m}分${s}秒` : `${s}秒`
}

// ============================================================
// P0-3 过期镜头（资产更新后引用过期：标黄 + 批量重渲染）
// ============================================================

/** 过期镜头计数 */
const staleCount = computed(() => boards.value.filter((b) => b.stale).length)
/** 批量重渲染进行中 */
const renderStaleBusy = ref(false)

/**
 * 批量重渲染全部过期镜头（render-stale 端点，10 积分/帧统一冻结）。
 * @returns 无
 */
async function renderStaleBoards(): Promise<void> {
  if (renderStaleBusy.value || !staleCount.value) return
  renderStaleBusy.value = true
  try {
    const res = await studioApi.renderStale(projectId.value, currentEpisodeId.value ?? undefined)
    if (res.queued > 0) {
      ElMessage.success(`已加入 ${res.queued} 个重渲染任务（${res.total_cost} 积分）`)
    } else {
      ElMessage.info('没有可重渲染的镜头（受影响镜头可能已在生成队列中）')
    }
    await loadBoards(true)
  } finally {
    renderStaleBusy.value = false
  }
}

// ============================================================
// 节奏预警抽屉（RD-22 文字化时长预警）
// ============================================================

/** 预警抽屉可见性 */
const warnVisible = ref(false)
/** 预警加载中 */
const warnLoading = ref(false)
/** 告警列表 */
const warnList = ref<StoryboardWarning[]>([])
/** 总时长与目标时长（展示用） */
const warnSummary = ref<{ total_duration: number; target_duration: number | null }>({
  total_duration: 0,
  target_duration: null,
})
/** 未读预警数徽标（拉取过一次后显示） */
const warnCount = ref<number | null>(null)

/**
 * 打开节奏预警抽屉并拉取告警。
 * @returns 无
 */
async function openWarnings(): Promise<void> {
  warnVisible.value = true
  warnLoading.value = true
  try {
    const res = await studioApi.storyboardWarnings(projectId.value)
    warnList.value = res.warnings ?? []
    warnSummary.value = {
      total_duration: res.total_duration,
      target_duration: res.target_duration ?? null,
    }
    warnCount.value = warnList.value.length
  } finally {
    warnLoading.value = false
  }
}

// ============================================================
// 分镜表导出（Task 10.4）
// ============================================================

/** 导出进行中（防重复点击） */
const exporting = ref(false)
/** 当前导出格式（按钮 loading 依据） */
const exportFormat = ref<'xlsx' | 'csv' | 'pdf' | null>(null)

/**
 * 导出分镜表（xlsx / csv / pdf）。
 * S-06：选中具体剧集时按该剧集导出（episode_id 走路径参数）；
 * 默认剧集回退 episode_id=0 全量导出（兼容语义）。
 * @param format 导出格式
 * @returns 无
 */
async function exportSheet(format: 'xlsx' | 'csv' | 'pdf'): Promise<void> {
  if (exporting.value) return
  exporting.value = true
  exportFormat.value = format
  try {
    await studioApi.exportStoryboardSheet(
      projectId.value,
      format,
      currentEpisodeId.value ?? undefined,
    )
    ElMessage.success('分镜表已开始下载')
  } catch {
    // 错误已由拦截器提示
  } finally {
    exporting.value = false
    exportFormat.value = null
  }
}

// ============================================================
// 连续性检查（SB-06，RD-23 宁可漏报不误报）
// ============================================================

/** 连续性检查弹窗可见性 */
const contVisible = ref(false)
/** 连续性检查加载中 */
const contLoading = ref(false)
/** 问题列表（无问题为空） */
const contItems = ref<ContinuityItem[]>([])
/** 检查的相邻镜头对数量 */
const contPairs = ref(0)
/** 疑似冲突分组（suspect，红点展示） */
const contSuspects = computed(() => contItems.value.filter((i) => i.confidence === 'suspect'))
/** 信息提示分组（info，灰点展示） */
const contInfos = computed(() => contItems.value.filter((i) => i.confidence === 'info'))
/** 当前高亮定位的分镜 ID（点击问题项临时高亮对应卡片） */
const flashBoardId = ref<number | null>(null)
/** 高亮复位定时器句柄（重复定位时重置） */
let flashTimer: ReturnType<typeof setTimeout> | null = null

/**
 * 打开连续性检查弹窗并拉取检查结果。
 * @returns 无
 */
async function openContinuity(): Promise<void> {
  contVisible.value = true
  contLoading.value = true
  try {
    const res = await studioApi.continuityCheck(
      projectId.value,
      currentEpisodeId.value ?? undefined,
    )
    contItems.value = res.items ?? []
    contPairs.value = res.checked_pairs ?? 0
  } finally {
    contLoading.value = false
  }
}

/**
 * 定位问题到对应分镜卡：平滑滚动至卡片并临时高亮 2 秒。
 * @param item 目标问题项
 * @returns 无
 */
function locateContinuityItem(item: ContinuityItem): void {
  if (!item.storyboard_id) return
  document.getElementById(`sb-card-${item.storyboard_id}`)?.scrollIntoView({
    behavior: 'smooth',
    block: 'center',
  })
  flashBoardId.value = item.storyboard_id
  if (flashTimer) clearTimeout(flashTimer)
  flashTimer = setTimeout(() => {
    flashBoardId.value = null
  }, 2000)
}

// ============================================================
// 目标时长自动调整（SB-07，等比缩放落库）
// ============================================================

/** 自动调整弹窗可见性 */
const fitVisible = ref(false)
/** 自动调整提交中 */
const fitLoading = ref(false)
/** 目标总时长输入（秒） */
const fitTarget = ref(60)
/** 调整摘要（确认后回填展示，前后对比） */
const fitResult = ref<AutoFitDurationResult | null>(null)
/** 每镜调整对比列表（空安全） */
const fitShots = computed(() => fitResult.value?.per_shot ?? [])

/**
 * 打开自动调整弹窗（默认目标 = 当前总时长，便于微调）。
 * @returns 无
 */
function openAutoFit(): void {
  fitTarget.value = totalDuration.value || 60
  fitResult.value = null
  fitVisible.value = true
}

/**
 * 确认按时长自动调整：等比缩放落库后静默刷新列表并展示摘要。
 * @returns 无
 */
async function confirmAutoFit(): Promise<void> {
  if (fitLoading.value) return
  const target = Math.round(fitTarget.value)
  if (!Number.isFinite(target) || target < 1 || target > 3600) {
    ElMessage.warning('目标总时长须在 1~3600 秒之间')
    return
  }
  fitLoading.value = true
  try {
    fitResult.value = await studioApi.autoFitDuration(
      projectId.value,
      target,
      currentEpisodeId.value ?? undefined,
    )
    await loadBoards(true)
  } catch {
    // 错误已由拦截器提示
  } finally {
    fitLoading.value = false
  }
}

// ============================================================
// 重排（上移 / 下移按钮替代拖拽）
// ============================================================

/** 重排进行中（防连点） */
const reorderBusy = ref(false)

/**
 * 上移 / 下移某镜并调 reorder 落库。
 * 说明：vuedraggable 未安装（禁止新增依赖），此处用相邻交换按钮替代拖拽，
 * 交互等价：每次交换后把最新顺序整体提交 reorder(ordered_ids)。
 * @param index 当前下标
 * @param dir -1 上移 / 1 下移
 * @returns 无
 */
async function moveBoard(index: number, dir: -1 | 1): Promise<void> {
  const target = index + dir
  if (target < 0 || target >= boards.value.length || reorderBusy.value) return
  reorderBusy.value = true
  try {
    const ids = boards.value.map((b) => b.id)
    // 相邻交换
    const tmp = ids[index]!
    ids[index] = ids[target]!
    ids[target] = tmp
    await studioApi.reorder(projectId.value, ids)
    await loadBoards(true)
  } finally {
    reorderBusy.value = false
  }
}

// ============================================================
// 编辑分镜
// ============================================================

/** 编辑弹窗可见性 */
const editVisible = ref(false)
/** 编辑表单（对应 StoryboardUpdatePayload，含方案 B 新字段） */
const editForm = ref({
  shot_type: '',
  camera_movement: '',
  duration: 5,
  visual_description: '',
  dialogue: '',
  prompt_editable: '',
  scene_id: undefined as number | undefined,
  character_ids: [] as number[],
  camera_angle: '',
  lighting: '',
  negative_prompt: '',
  sfx_text: '',
  emotion: '',
})
/** 正在编辑的分镜 ID */
let editId = 0
/** 场景资产选项 / 角色资产选项（表单下拉用） */
const sceneOptions = ref<{ id: number; name: string }[]>([])
const charOptions = ref<{ id: number; name: string }[]>([])
/** 保存中（防重复提交） */
const editSaving = ref(false)

/** 景别选项 */
const shotTypeOptions = ['全景', '远景', '中景', '近景', '特写']
/** 运镜选项 */
const cameraOptions = ['固定', '推镜', '拉镜', '摇镜', '移镜', '跟随']
/** 机位角度选项（方案 B 镜头语言扩展） */
const cameraAngleOptions = ['平视', '俯拍', '仰拍', '斜角', '过肩', '顶摄']

/**
 * 打开编辑弹窗（预填当前分镜；同时拉取资产选项）。
 * @param b 目标分镜
 * @returns 无
 */
async function openEdit(b: Storyboard): Promise<void> {
  editId = b.id
  editForm.value = {
    shot_type: b.shot_type ?? '',
    camera_movement: b.camera_movement ?? '',
    duration: b.duration,
    visual_description: b.visual_description ?? '',
    dialogue: b.dialogue ?? '',
    prompt_editable: b.prompt_editable ?? '',
    scene_id: b.scene_id ?? undefined,
    character_ids: b.characters.map((c) => c.character_id),
    camera_angle: b.camera_angle ?? '',
    lighting: b.lighting ?? '',
    negative_prompt: b.negative_prompt ?? '',
    sfx_text: (b.audio?.sfx ?? []).join('、'),
    emotion: b.audio?.emotion ?? '',
  }
  editVisible.value = true
  // 拉取场景/角色资产作为下拉与多选选项
  if (!sceneOptions.value.length || !charOptions.value.length) {
    const group: AssetsGroup = await studioApi.listAssets(projectId.value)
    sceneOptions.value = group.scenes.map((s) => ({ id: s.id, name: s.name }))
    charOptions.value = group.characters.map((c) => ({ id: c.id, name: c.name }))
  }
}

/**
 * 保存分镜编辑（character_links 整体替换；方案 B 新字段随表单提交）。
 * @returns 无
 */
async function saveEdit(): Promise<void> {
  if (editSaving.value) return
  editSaving.value = true
  try {
    // 音效按「、」或逗号/空格切分为数组（空串落空数组）
    const sfxList = editForm.value.sfx_text
      .split(/[、,，\s]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    const hasAudio = sfxList.length > 0 || editForm.value.emotion.trim()
    await studioApi.updateStoryboard(projectId.value, editId, {
      shot_type: editForm.value.shot_type || undefined,
      camera_movement: editForm.value.camera_movement || undefined,
      duration: editForm.value.duration,
      visual_description: editForm.value.visual_description,
      dialogue: editForm.value.dialogue,
      prompt_editable: editForm.value.prompt_editable,
      scene_id: editForm.value.scene_id,
      character_links: editForm.value.character_ids.map((cid) => ({ character_id: cid })),
      camera_angle: editForm.value.camera_angle || undefined,
      lighting: editForm.value.lighting,
      negative_prompt: editForm.value.negative_prompt,
      audio: hasAudio ? { sfx: sfxList, emotion: editForm.value.emotion.trim() || null } : { sfx: [], emotion: null },
    })
    editVisible.value = false
    ElMessage.success('分镜已更新')
    await loadBoards(true)
  } finally {
    editSaving.value = false
  }
}

// ============================================================
// 拆分（RD-21 文字级操作：按对白行号一分为二）
// ============================================================

/** 拆分弹窗可见性 */
const splitVisible = ref(false)
/** 拆分目标分镜 */
const splitBoard = ref<Storyboard | null>(null)
/** 拆分行号（1 起，前 at_line 行留本镜） */
const splitAtLine = ref(1)
/** 拆分对白行列表（展示用） */
const splitLines = computed(() =>
  (splitBoard.value?.dialogue ?? '').split('\n').filter((l) => l.trim()),
)
/** 拆分进行中 */
const splitSaving = ref(false)

/**
 * 打开拆分弹窗（无对白时提示不可拆）。
 * @param b 目标分镜
 * @returns 无
 */
function openSplit(b: Storyboard): void {
  const lines = (b.dialogue ?? '').split('\n').filter((l) => l.trim())
  if (lines.length < 2) {
    ElMessage.warning('本镜对白不足两行，无法拆分')
    return
  }
  splitBoard.value = b
  splitAtLine.value = 1
  splitVisible.value = true
}

/**
 * 确认拆分。
 * @returns 无
 */
async function confirmSplit(): Promise<void> {
  if (!splitBoard.value || splitSaving.value) return
  splitSaving.value = true
  try {
    await studioApi.split(projectId.value, splitBoard.value.id, splitAtLine.value)
    splitVisible.value = false
    ElMessage.success('拆分完成')
    await loadBoards(true)
  } finally {
    splitSaving.value = false
  }
}

// ============================================================
// 合并（选择相邻镜）
// ============================================================

/**
 * 与下一镜合并（保留当前镜，删除下一镜）。
 * @param b 当前分镜
 * @returns 无
 */
async function mergeWithNext(b: Storyboard): Promise<void> {
  const idx = boards.value.findIndex((x) => x.id === b.id)
  const next = boards.value[idx + 1]
  if (!next) {
    ElMessage.warning('已是最后一镜，无下一镜可合并')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将把 #${next.sequence} 合并进 #${b.sequence}（对白拼接、时长相加，#${next.sequence} 删除）。是否继续？`,
      '合并分镜',
      { type: 'warning' },
    )
  } catch {
    return
  }
  await studioApi.merge(projectId.value, b.id, next.id)
  ElMessage.success('合并完成')
  await loadBoards(true)
}

// ============================================================
// 投递画布
// ============================================================

/** 投递进行中 */
const delivering = ref(false)

/**
 * 投递当前分镜到 AI 画布（建 studio_linked 画布），成功后确认打开。
 * @param b 来源分镜
 * @returns 无
 */
async function deliver(b: Storyboard): Promise<void> {
  if (delivering.value) return
  delivering.value = true
  try {
    const res = await studioApi.deliverToCanvas(projectId.value, b.id)
    try {
      await ElMessageBox.confirm('已创建关联画布，是否立即打开精修？', '投递画布', {
        type: 'success',
        confirmButtonText: '打开画布',
        cancelButtonText: '稍后再说',
      })
      await router.push(`/canvas/${res.canvas_id}`)
    } catch {
      /* 用户选择稍后打开 */
    }
  } finally {
    delivering.value = false
  }
}

/** 确认分镜进行中（防重复点击） */
const confirmBusy = ref(false)

/**
 * 确认分镜并进入生成（生成阶段门禁）：调 confirm 推进项目状态
 * storyboard → production 后跳转；确认失败（如未拆解）不跳转，提示已由
 * 拦截器展示。幂等：已确认状态直接跳转。
 * @returns 无
 */
async function gotoProduction(): Promise<void> {
  if (confirmBusy.value) return
  confirmBusy.value = true
  try {
    await studioApi.confirmStoryboards(projectId.value)
    await router.push(`/projects/${projectId.value}/production`)
  } catch {
    // 确认失败（未拆解/状态不允许）：拦截器已提示，停留分镜页
  } finally {
    confirmBusy.value = false
  }
}

onMounted(loadBoards)
</script>

<template>
  <div class="page-container sb-view">
    <div class="view-head">
      <h2 class="page-title">分镜设计</h2>
      <!-- 节奏预警徽标（RD-22） -->
      <el-badge
        :value="warnCount ?? undefined"
        :hidden="!warnCount"
        type="warning"
        class="warn-badge"
      >
        <el-button :icon="Warning" @click="openWarnings">节奏预警</el-button>
      </el-badge>
    </div>

    <!-- 首屏骨架 -->
    <el-skeleton v-if="loading" :rows="7" animated />

    <!-- 空态：AI 拆解引导 -->
    <el-card v-else-if="!boards.length" shadow="never">
      <el-empty description="还没有分镜。AI 将依据剧本逐场景拆解为镜头序列（每镜 1~3 个）">
        <el-button type="primary" size="large" :loading="decomposing" @click="runDecompose()">
          AI 拆解分镜
        </el-button>
      </el-empty>
    </el-card>

    <template v-else>
      <!-- 顶部工具条：成本可见（RD-20） -->
      <div class="toolbar">
        <span>共 <b>{{ boards.length }}</b> 镜</span>
        <el-divider direction="vertical" />
        <span>总时长 <b>{{ durationText(totalDuration) }}</b></span>
        <el-divider direction="vertical" />
        <span class="toolbar-cost">预估生成消耗：{{ boards.length }} 镜 ≈ {{ estimatedCost }} 积分</span>
        <!-- 连续性检查（SB-06：窄口径规则，suspect/info 分组展示） -->
        <el-button class="toolbar-btn" :loading="contLoading && contVisible" @click="openContinuity">
          连续性检查
        </el-button>
        <!-- 目标时长自动调整（SB-07：等比缩放落库） -->
        <el-button @click="openAutoFit">自动时长</el-button>
        <!-- 导出分镜表（Task 10.4 + SB-08：episode_id=0 全量导出） -->
        <el-button
          :loading="exportFormat === 'xlsx'"
          :disabled="exporting"
          @click="exportSheet('xlsx')"
        >
          导出 Excel
        </el-button>
        <el-button
          :loading="exportFormat === 'csv'"
          :disabled="exporting"
          @click="exportSheet('csv')"
        >
          导出 CSV
        </el-button>
        <el-button
          :loading="exportFormat === 'pdf'"
          :disabled="exporting"
          @click="exportSheet('pdf')"
        >
          导出 PDF
        </el-button>
        <!-- P0-3 过期镜头批量重渲染（资产更新后引用过期） -->
        <el-button
          v-if="staleCount > 0"
          type="warning"
          :loading="renderStaleBusy"
          @click="renderStaleBoards"
        >
          {{ staleCount }} 个镜头需重渲染
        </el-button>
        <el-button type="primary" plain @click="runDecompose()" :loading="decomposing">
          重新拆解
        </el-button>
      </div>

      <!-- 分镜卡片列表（上移/下移按钮替代拖拽；卡片 id 供连续性问题定位） -->
      <div class="board-list">
        <el-card
          v-for="(b, i) in boards"
          :id="`sb-card-${b.id}`"
          :key="b.id"
          shadow="hover"
          :class="['board-card', {
            'board-card-flash': flashBoardId === b.id,
            'board-card-stale': b.stale,
          }]"
        >
          <template #header>
            <div class="board-head">
              <span class="board-no">#{{ String(b.sequence).padStart(2, '0') }}</span>
              <el-tag size="small" effect="plain">{{ b.shot_type || '未定景别' }}</el-tag>
              <!-- 方案 B：机位角度（镜头语言三要素之一） -->
              <el-tag v-if="b.camera_angle" size="small" effect="plain" type="info">{{ b.camera_angle }}</el-tag>
              <el-tag size="small" effect="plain" type="info">{{ b.camera_movement || '固定' }}</el-tag>
              <el-tag size="small" effect="plain" type="info">{{ b.duration }}s</el-tag>
              <!-- 方案 B：声音结构化（情绪标签 + 音效） -->
              <el-tooltip
                v-if="b.audio && ((b.audio.sfx?.length ?? 0) || b.audio.emotion)"
                :content="(b.audio.sfx ?? []).join('、')"
                placement="top"
                :disabled="!(b.audio.sfx?.length ?? 0)"
              >
                <el-tag size="small" effect="light" type="warning">
                  {{ b.audio.emotion || '音效' }}{{ (b.audio.sfx?.length ?? 0) ? ` ·♪${b.audio.sfx!.length}` : '' }}
                </el-tag>
              </el-tooltip>
              <!-- P0-3：引用资产已过期（角色定妆照/场景图更新后） -->
              <el-tooltip
                v-if="b.stale"
                :content="(b.stale_reasons ?? []).join('；') || '引用资产已更新'"
                placement="top"
              >
                <el-tag size="small" type="warning" effect="dark">需重渲染</el-tag>
              </el-tooltip>
              <div class="board-head-ops">
                <el-button
                  size="small"
                  text
                  :disabled="i === 0 || reorderBusy"
                  @click="moveBoard(i, -1)"
                >
                  ↑ 上移
                </el-button>
                <el-button
                  size="small"
                  text
                  :disabled="i === boards.length - 1 || reorderBusy"
                  @click="moveBoard(i, 1)"
                >
                  ↓ 下移
                </el-button>
              </div>
            </div>
          </template>
          <p class="board-visual">{{ b.visual_description || '（无画面描述）' }}</p>
          <!-- 方案 B：环境光影独立展示 -->
          <p v-if="b.lighting" class="board-lighting">光影：{{ b.lighting }}</p>
          <div v-if="b.dialogue" class="board-dialogue">
            <div v-for="(line, li) in b.dialogue.split('\n').filter((l) => l.trim())" :key="li" class="dlg-line">
              {{ line }}
            </div>
          </div>
          <!-- 绑定资产 Tag（低置信标黄，RD-24；方案 B 扩展道具绑定） -->
          <div class="board-assets">
            <span class="assets-label">绑定资产：</span>
            <el-tag
              v-for="c in b.characters"
              :key="c.character_id"
              size="small"
              :type="(c.link_confidence ?? 1) < 0.7 ? 'warning' : 'success'"
              :title="c.link_confidence != null ? `链接置信度 ${(c.link_confidence * 100).toFixed(0)}%` : ''"
            >
              角色·{{ c.name }}{{ (c.link_confidence ?? 1) < 0.7 ? '（低置信）' : '' }}
            </el-tag>
            <el-tag
              v-if="b.scene_id"
              size="small"
              type="success"
              effect="light"
            >
              场景·{{ b.scene_name || b.scene_id }}
            </el-tag>
            <el-tag
              v-for="p in b.props ?? []"
              :key="p.prop_id"
              size="small"
              :type="(p.link_confidence ?? 1) < 0.7 ? 'warning' : 'success'"
              :title="p.link_confidence != null ? `链接置信度 ${(p.link_confidence * 100).toFixed(0)}%` : ''"
            >
              道具·{{ p.name }}
            </el-tag>
            <span v-if="!b.characters.length && !b.scene_id && !(b.props ?? []).length" class="assets-none">未绑定</span>
          </div>
          <div class="board-ops">
            <el-button size="small" type="primary" plain @click="openEdit(b)">编辑</el-button>
            <el-button size="small" @click="openSplit(b)">拆分</el-button>
            <el-button size="small" @click="mergeWithNext(b)">合并下一镜</el-button>
            <el-button size="small" type="warning" plain :loading="delivering" @click="deliver(b)">
              投递画布
            </el-button>
          </div>
        </el-card>
      </div>

      <!-- 底部操作条 -->
      <div class="footer-bar">
        <span class="footer-tip">提示：顺序调整后镜号自动重排；低置信绑定建议先到资产页确认</span>
        <el-button size="large" type="primary" class="confirm-btn" :loading="confirmBusy" @click="gotoProduction">
          确认分镜，进入生成
        </el-button>
      </div>
    </template>

    <!-- 编辑弹窗 -->
    <el-dialog v-model="editVisible" title="编辑分镜" width="640px" append-to-body>
      <el-form label-width="90px">
        <el-form-item label="景别">
          <el-select v-model="editForm.shot_type" placeholder="选择景别" clearable style="width: 200px">
            <el-option v-for="s in shotTypeOptions" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
        <el-form-item label="机位角度">
          <el-select v-model="editForm.camera_angle" placeholder="选择机位角度" clearable style="width: 200px">
            <el-option v-for="a in cameraAngleOptions" :key="a" :label="a" :value="a" />
          </el-select>
        </el-form-item>
        <el-form-item label="运镜">
          <el-select v-model="editForm.camera_movement" placeholder="选择运镜" clearable style="width: 200px">
            <el-option v-for="c in cameraOptions" :key="c" :label="c" :value="c" />
          </el-select>
        </el-form-item>
        <el-form-item label="时长（秒）">
          <el-input-number v-model="editForm.duration" :min="1" :max="30" />
        </el-form-item>
        <el-form-item label="画面描述">
          <el-input v-model="editForm.visual_description" type="textarea" :rows="3" placeholder="主体动作描述" />
        </el-form-item>
        <el-form-item label="环境光影">
          <el-input
            v-model="editForm.lighting"
            type="textarea"
            :rows="2"
            placeholder="时段光线 / 光源方向 / 明暗氛围"
          />
        </el-form-item>
        <el-form-item label="负面提示词">
          <el-input
            v-model="editForm.negative_prompt"
            type="textarea"
            :rows="2"
            placeholder="生图需排除的元素，如：多余手指、文字水印、模糊变形"
          />
        </el-form-item>
        <el-form-item label="对白">
          <el-input
            v-model="editForm.dialogue"
            type="textarea"
            :rows="3"
            placeholder="每行一条，格式：角色名：台词"
          />
        </el-form-item>
        <el-form-item label="音效">
          <el-input
            v-model="editForm.sfx_text"
            placeholder="多个音效用「、」分隔，如：键盘声、雨声"
          />
        </el-form-item>
        <el-form-item label="情绪标签">
          <el-input v-model="editForm.emotion" placeholder="本镜整体情绪，如：压抑 / 爽感 / 紧张" />
        </el-form-item>
        <el-form-item label="提示词">
          <el-input
            v-model="editForm.prompt_editable"
            type="textarea"
            :rows="2"
            placeholder="用户可编辑层提示词（不影响一致性约束）"
          />
        </el-form-item>
        <el-form-item label="强绑场景">
          <el-select v-model="editForm.scene_id" placeholder="选择场景资产" clearable style="width: 260px">
            <el-option v-for="s in sceneOptions" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="涉及角色">
          <el-select v-model="editForm.character_ids" multiple style="width: 100%" placeholder="选择角色资产">
            <el-option v-for="c in charOptions" :key="c.id" :label="c.name" :value="c.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editSaving" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 拆分弹窗 -->
    <el-dialog v-model="splitVisible" title="拆分分镜（按对白行）" width="480px" append-to-body>
      <div class="split-lines">
        <el-radio-group v-model="splitAtLine">
          <div v-for="(line, li) in splitLines" :key="li" class="split-line">
            <el-radio :value="li + 1">前 {{ li + 1 }} 行留本镜</el-radio>
            <span class="split-line-text">{{ line }}</span>
          </div>
        </el-radio-group>
      </div>
      <template #footer>
        <el-button @click="splitVisible = false">取消</el-button>
        <el-button type="primary" :loading="splitSaving" @click="confirmSplit">拆分</el-button>
      </template>
    </el-dialog>

    <!-- 节奏预警抽屉（RD-22） -->
    <el-drawer v-model="warnVisible" title="节奏与连续性预警" size="420px" append-to-body>
      <div v-loading="warnLoading">
        <div class="warn-summary">
          总时长 {{ warnSummary.total_duration }}s
          <template v-if="warnSummary.target_duration">
            / 目标 {{ warnSummary.target_duration }}s
          </template>
        </div>
        <el-empty v-if="!warnList.length" description="未检测到节奏或绑定问题" />
        <el-alert
          v-for="(w, i) in warnList"
          :key="i"
          :title="w.message"
          :type="w.level === 'error' ? 'error' : 'warning'"
          show-icon
          :closable="false"
          class="warn-item"
        />
      </div>
    </el-drawer>

    <!-- 连续性检查弹窗（SB-06：suspect 红点 / info 灰点分组，点击项定位分镜卡） -->
    <el-dialog v-model="contVisible" title="连续性检查" width="640px" append-to-body>
      <div v-loading="contLoading">
        <div class="cont-summary">共检查 {{ contPairs }} 组相邻镜头</div>
        <el-empty v-if="!contItems.length" description="未发现连续性问题" />
        <template v-else>
          <template v-if="contSuspects.length">
            <div class="cont-group-title">疑似冲突</div>
            <div
              v-for="(item, i) in contSuspects"
              :key="`s-${i}`"
              class="cont-item"
              title="点击定位到对应分镜"
              @click="locateContinuityItem(item)"
            >
              <span class="cont-dot cont-dot-suspect" />
              <span class="cont-msg">{{ item.message }}</span>
            </div>
          </template>
          <template v-if="contInfos.length">
            <div class="cont-group-title">信息提示</div>
            <div
              v-for="(item, i) in contInfos"
              :key="`i-${i}`"
              class="cont-item"
              title="点击定位到对应分镜"
              @click="locateContinuityItem(item)"
            >
              <span class="cont-dot cont-dot-info" />
              <span class="cont-msg">{{ item.message }}</span>
            </div>
          </template>
        </template>
      </div>
    </el-dialog>

    <!-- 目标时长自动调整弹窗（SB-07：输入目标秒数 → 等比缩放 → 前后对比摘要） -->
    <el-dialog v-model="fitVisible" title="按时长自动调整" width="520px" append-to-body>
      <el-form label-width="100px">
        <el-form-item label="目标总时长">
          <el-input-number v-model="fitTarget" :min="1" :max="3600" :step="5" step-strictly />
          <span class="fit-unit">秒</span>
        </el-form-item>
        <el-form-item label="当前总时长">
          <span class="fit-current"> {{ totalDuration }} 秒（{{ boards.length }} 镜）</span>
        </el-form-item>
      </el-form>
      <div class="fit-tip">各镜按时长占比等比缩放（单镜保底 1 秒），调整后总时长 ≈ 目标值</div>
      <template v-if="fitResult">
        <el-alert
          type="success"
          :closable="false"
          class="fit-result"
          :title="`调整完成：${fitResult.before_total}s → ${fitResult.after_total}s（目标 ${fitResult.target_seconds}s），变更 ${fitResult.changed} 镜`"
        />
        <div v-if="fitShots.length" class="fit-list">
          <div v-for="d in fitShots" :key="d.id" class="fit-line">
            {{ d.before }}s → <b>{{ d.after }}s</b>
          </div>
        </div>
      </template>
      <template #footer>
        <el-button @click="fitVisible = false">关闭</el-button>
        <el-button type="primary" :loading="fitLoading" @click="confirmAutoFit">开始调整</el-button>
      </template>
    </el-dialog>

    <!-- P0-1 锁定门禁拦截弹窗：主要角色定妆照未就绪清单 + 快速模式兜底 -->
    <el-dialog v-model="gateVisible" title="主要角色定妆照未就绪" width="560px" append-to-body>
      <el-alert
        type="warning"
        show-icon
        :closable="false"
        title="「不定妆，不进分镜」：以下主要角色的定妆照尚未生成/锁定，为保证角色形象一致性，已拦截分镜生成"
        class="gate-alert"
      />
      <el-table :data="gateUnready" size="small" class="gate-table">
        <el-table-column label="角色" prop="name" min-width="100" />
        <el-table-column label="定位" width="90">
          <template #default="{ row }">
            {{ ROLE_LABELS[row.role_type] ?? '未标注' }}
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.reason === '生成失败' ? 'danger' : row.reason === '生成中' ? 'info' : 'warning'"
            >
              {{ row.reason }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
      <el-checkbox v-model="gateSkipCheck" class="gate-skip">
        快速模式：本次生成跳过锁定检查（可能造成角色形象不一致）
      </el-checkbox>
      <template #footer>
        <el-button @click="gotoAssets">去资产页处理</el-button>
        <el-button
          type="primary"
          :disabled="!gateSkipCheck"
          :loading="decomposing"
          @click="runDecompose(true)"
        >
          跳过并继续生成
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.sb-view {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-5);
}
.view-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-4);
}
.view-head .page-title {
  margin: 0;
}
.warn-badge {
  margin-top: 6px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  padding: var(--mf-sp-3) var(--mf-sp-4);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
}
.toolbar-cost {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.toolbar-btn {
  margin-left: auto;
}
.board-list {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-4);
}
.board-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  flex-wrap: wrap;
}
.board-no {
  font-weight: var(--mf-fw-bold);
  color: var(--mf-brand);
  font-size: var(--mf-fs-lg);
}
.board-head-ops {
  margin-left: auto;
}
.board-visual {
  margin: 0 0 var(--mf-sp-2);
  line-height: 1.8;
  color: var(--mf-text);
}
.board-lighting {
  margin: 0 0 var(--mf-sp-2);
  font-size: 12px;
  line-height: 1.7;
  color: var(--mf-text-secondary, #8b93a7);
}
.board-dialogue {
  background: var(--mf-surface-2);
  border-radius: var(--mf-r-ctl);
  padding: var(--mf-sp-2) var(--mf-sp-3);
  margin-bottom: var(--mf-sp-2);
}
.dlg-line {
  font-size: var(--mf-fs-sm);
  color: var(--mf-text-2);
  line-height: 1.8;
}
.board-assets {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: var(--mf-sp-3);
}
.assets-label {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.assets-none {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.board-ops {
  display: flex;
  flex-wrap: wrap;
  gap: var(--mf-sp-2);
}
.footer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-4);
  position: sticky;
  bottom: 0;
  /* sticky 吸底时浮于 positioned 内容之上，避免滚动穿透重叠 */
  z-index: 10;
  padding: var(--mf-sp-3);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
}
.footer-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.confirm-btn {
  background: var(--mf-brand);
  border-color: var(--mf-brand);
  color: var(--mf-bg);
}
.split-lines {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-2);
}
.split-line {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.split-line-text {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.warn-summary {
  color: var(--mf-text-2);
  margin-bottom: var(--mf-sp-3);
}
.warn-item {
  margin-bottom: var(--mf-sp-2);
}
/* 连续性问题定位时的高亮（2 秒后由脚本复位） */
.board-card-flash {
  box-shadow: 0 0 0 2px var(--mf-brand);
  transition: box-shadow 0.3s;
}
/* P0-3 过期镜头标黄：引用的角色定妆照/场景图已更新 */
.board-card-stale {
  border-color: var(--el-color-warning, #e6a23c);
  box-shadow: 0 0 0 1px var(--el-color-warning, #e6a23c);
}
.cont-summary {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  margin-bottom: var(--mf-sp-3);
}
.cont-group-title {
  font-weight: var(--mf-fw-bold);
  margin: var(--mf-sp-2) 0;
}
.cont-item {
  display: flex;
  align-items: flex-start;
  gap: var(--mf-sp-2);
  padding: var(--mf-sp-2);
  border-radius: var(--mf-r-ctl);
  cursor: pointer;
}
.cont-item:hover {
  background: var(--mf-surface-2);
}
.cont-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-top: 7px;
  flex: none;
}
.cont-dot-suspect {
  background: var(--el-color-danger, #f56c6c);
}
.cont-dot-info {
  background: var(--el-text-color-placeholder, #c0c4cc);
}
.cont-msg {
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
}
.fit-unit {
  margin-left: var(--mf-sp-2);
  color: var(--mf-text-3);
}
.fit-current {
  color: var(--mf-text-2);
}
.fit-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
  margin-bottom: var(--mf-sp-2);
}
.fit-result {
  margin-bottom: var(--mf-sp-2);
}
.fit-list {
  max-height: 200px;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.fit-line {
  font-size: var(--mf-fs-sm);
  color: var(--mf-text-2);
}
/* P0-1 锁定门禁拦截弹窗 */
.gate-alert {
  margin-bottom: var(--mf-sp-3);
}
.gate-table {
  margin-bottom: var(--mf-sp-3);
}
.gate-skip {
  margin-top: var(--mf-sp-2);
}
</style>
