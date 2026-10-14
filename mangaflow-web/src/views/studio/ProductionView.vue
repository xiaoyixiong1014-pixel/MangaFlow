<!--
  内容生成页（文件级注释）。

  对应原型 S-07（内容生成）+ S-08（配音与合成），路由 /projects/:id/production。
  功能：
  - 顶部积分消耗条：billing.wallet 的可用 / 冻结积分；
  - 三个 Tab（el-tabs）：
    ① 关键帧：分镜网格卡（keyframe_url 图或占位 + 状态角标 + D3「演示占位」角标）；
      「打样第一镜」keyframes(phase=trial)；「全量生成」确认框（将生成 N 张、
      预计 N×10 积分）后 keyframes(phase=full)；已完成关键帧卡片提供
      「上传替换」入口（P-06：multipart 一步上传调 /tasks/{id}/replace，
      成功后刷新分镜与任务列表）；任务提交后不阻塞，由任务面板跟踪；
    ② 视频：参数配置区（P-12：分辨率 480p/720p/1080p、帧率 24/30、运动幅度
      低/中/高，随每次提交携带）；每镜行（首帧缩略 + video_url 播放器或占位 +
      「使用尾帧」开关（P-03/P-08：仅 end_frame_url 已回写的分镜可开启，
      未生成时提供「先生成尾帧」按钮调 end-frame 接口并轮询回写）+
      「生成视频片段」video()）；
    ③ 配音与合成（S-08）：角色音色映射表（音色 Select + 试听按钮 disabled 占位）、
      每镜「生成配音」tts()；已配音行（后端回写 audio_url）在操作列展示行内
      播放/暂停试听图标，未配音行显示「未生成」占位；时长差异处理三选一
      （缩短台词 / 延长镜头 / 重新配音）——
      仅「重新配音」真实调用 tts，其余为前端演示提示（说明见注释）；
      BGM（曲库下拉 + 音量 + 上传自定义）/ 字幕（开关 + 字号/颜色/位置 +
      P-16 双语字幕开关）/ 转场（开关 + P-17 四类转场类型 + 时长）——
      配置随 compose 请求体提交（Task 10.4）；
  - R-03 重生成直连：发布页分镜条「重生成」携带 query.storyboard_id 跳入时，
    onMounted 预选该分镜（关键帧卡片高亮 + 滚动到可见 + 定位提示）；
  - 全局任务面板：右侧常驻抽屉；初始 taskList 拉取最近任务，taskStream（GET SSE）
    实时更新类型/状态/进度/结果链接，支持单任务重试（taskRetry）/取消（taskCancel），
    任务命中演示占位标记时展示「演示占位」角标；
  - 底部「全部完成，进入合成」：compose() 提交合成任务并轮询 taskDetail，
    完成后确认跳转发布页。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadRequestOptions } from 'element-plus'
import { Upload as UploadIcon, VideoPause, VideoPlay } from '@element-plus/icons-vue'
import * as productionApi from '@/api/production'
import * as billingApi from '@/api/billing'
import * as bgmApi from '@/api/bgm'
import * as studioApi from '@/api/studio'
import { useProjectStore } from '@/stores/project'
import type { BgmItem, ComposePayload, GenTask, Storyboard, Wallet, CharacterAsset } from '@/types'

const route = useRoute()
const router = useRouter()
const projectStore = useProjectStore()

/** 当前项目 ID（取路由参数） */
const projectId = computed(() => Number(route.params.id))

/** 当前选中剧集 ID（S-06 剧集切换器；null = 默认剧集，列表不传 episode_id） */
const currentEpisodeId = computed(() => projectStore.currentEpisodeId)

/**
 * R-03 重生成直连：发布页分镜条「重生成」跳转携带的 query.storyboard_id
 * 命中的目标分镜 ID（命中时该镜关键帧卡片高亮并滚动到可见）
 */
const highlightSbId = ref<number | null>(null)

// ============================================================
// 基础数据
// ============================================================

/** 首屏加载中 */
const loading = ref(true)
/** 分镜列表 */
const boards = ref<Storyboard[]>([])
/** 角色资产（音色映射表用） */
const characters = ref<CharacterAsset[]>([])
/** 当前 Tab */
const activeTab = ref('keyframes')

/**
 * 拉取分镜与角色资产。
 * S-06：分镜按当前选中剧集过滤（默认剧集 = 不传 episode_id，返回全部）。
 * @returns 无
 */
async function loadBase(): Promise<void> {
  loading.value = true
  try {
    const [sb, assets, walletRes] = await Promise.all([
      studioApi.listStoryboard(projectId.value, currentEpisodeId.value ?? undefined),
      studioApi.listAssets(projectId.value),
      billingApi.wallet().catch(() => null),
    ])
    boards.value = sb
    characters.value = assets.characters
    if (walletRes) {
      wallet.balance = walletRes.points_balance
      wallet.frozen = walletRes.frozen_points
    }
  } finally {
    loading.value = false
  }
}

// 切换剧集（S-06）后按新剧集重新加载分镜
watch(currentEpisodeId, () => {
  void loadBase()
})

// ============================================================
// 积分消耗条
// ============================================================

/** 钱包展示状态 */
const wallet = reactive({ balance: 0, frozen: 0 })

/**
 * 刷新钱包（任务状态变化后调用）。
 * @returns 无
 */
async function refreshWallet(): Promise<void> {
  try {
    const w = await billingApi.wallet()
    wallet.balance = w.points_balance
    wallet.frozen = w.frozen_points
  } catch {
    /* 静默失败：积分条非关键路径 */
  }
}

// ============================================================
// 全局任务面板（右侧常驻抽屉 + SSE 进度流）
// ============================================================

/** 任务面板可见性 */
const panelVisible = ref(false)
/** 任务列表（taskList 详情 + SSE 摘要合并） */
const tasks = ref<GenTask[]>([])
/** 非终态任务数（SSE progress 摘要） */
const runningCount = ref(0)
/** 分镜占位标记映射（分镜 ID → 最新已完成关键帧任务是否为演示占位，D3 角标） */
const sbPlaceholderMap = ref<Record<number, boolean>>({})
/** SSE 中止控制器 */
let taskAbort: AbortController | null = null

/**
 * 依据任务列表构建「分镜 → 演示占位」映射（D3 打标：mock 兜底产物角标）。
 * 取每个分镜最新一条已完成 image 任务的 placeholder 标记。
 * @param items 最近任务列表
 * @returns 无
 */
function buildPlaceholderMap(items: GenTask[]): void {
  const map: Record<number, boolean> = {}
  for (const t of items) {
    if (t.type !== 'image' || !t.storyboard_id || t.status !== 'done') continue
    // 列表按创建时间倒序，首个命中即最新
    if (!(t.storyboard_id in map)) map[t.storyboard_id] = !!t.placeholder
  }
  sbPlaceholderMap.value = map
}

/**
 * 拉取最近任务列表（面板初始与 SSE done 后刷新）。
 * @returns 无
 */
async function refreshTasks(): Promise<void> {
  try {
    const page = await productionApi.taskList(projectId.value, { page: 1, page_size: 20 })
    tasks.value = page.items
    buildPlaceholderMap(page.items)
  } catch {
    /* 拉取失败不打断主流程 */
  }
}

/**
 * 建立 SSE 进度流：progress 事件合并进任务列表；done 后刷新任务与分镜（回写结果）。
 * @returns 无
 */
function startTaskStream(): void {
  stopTaskStream()
  taskAbort = new AbortController()
  void productionApi
    .taskStream(
      projectId.value,
      (event, data) => {
        if (event === 'progress') {
          const d = (data ?? {}) as { tasks?: { id: number; type: string; status: string; progress: number }[]; running?: number }
          runningCount.value = d.running ?? 0
          for (const t of d.tasks ?? []) {
            const exist = tasks.value.find((x) => x.id === t.id)
            if (exist) {
              exist.status = t.status
              exist.progress = t.progress
              exist.type = t.type
            } else {
              // 新任务先以摘要行插入，详情在 done 后刷新补齐
              tasks.value.unshift({
                id: t.id,
                type: t.type,
                status: t.status,
                progress: t.progress,
              } as GenTask)
            }
          }
        } else if (event === 'done') {
          runningCount.value = 0
          void refreshTasks()
          void studioApi
            .listStoryboard(projectId.value, currentEpisodeId.value ?? undefined)
            .then((sb) => {
              boards.value = sb
            })
          void refreshWallet()
        }
      },
      taskAbort.signal,
    )
    .catch(() => {
      /* SSE 断开静默：任务状态以手动刷新兜底 */
    })
}

/**
 * 关闭 SSE 流。
 * @returns 无
 */
function stopTaskStream(): void {
  taskAbort?.abort()
  taskAbort = null
}

/**
 * 打开任务面板（同时刷新任务列表）。
 * @returns 无
 */
function openPanel(): void {
  panelVisible.value = true
  void refreshTasks()
}

/**
 * 重试失败任务（创建新任务，重新冻结积分）。
 * @param t 失败任务
 * @returns 无
 */
async function retryTask(t: GenTask): Promise<void> {
  const newTask = await productionApi.taskRetry(t.id)
  ElMessage.success(`已创建重试任务 #${newTask.id}`)
  startTaskStream()
  void refreshTasks()
}

/**
 * 取消任务（queued/running 可取消，退还冻结积分）。
 * @param t 目标任务
 * @returns 无
 */
async function cancelTask(t: GenTask): Promise<void> {
  const res = await productionApi.taskCancel(t.id)
  ElMessage.success(res.message ?? '任务已取消')
  void refreshTasks()
  void refreshWallet()
}

/** 任务状态 → Tag 类型 */
const taskStatusType: Record<string, 'info' | 'warning' | 'success' | 'danger'> = {
  queued: 'info',
  running: 'warning',
  done: 'success',
  failed: 'danger',
  canceled: 'info',
}
/** 任务类型 → 中文 */
const taskTypeLabel: Record<string, string> = {
  image: '关键帧',
  video: '视频',
  tts: '配音',
  compose: '合成',
  llm: '文本',
  extract: '抽取',
}

// ============================================================
// 关键帧 Tab（S-07）
// ============================================================

/** 提交关键帧任务进行中（防重复点击） */
const kfBusy = ref(false)

/**
 * 打样第一镜（trial：单分镜 1 张，确认风格后再放开全量，RD-27）。
 * @returns 无
 */
async function trialKeyframe(): Promise<void> {
  const first = boards.value[0]
  if (!first) {
    ElMessage.warning('暂无分镜，请先完成分镜拆解')
    return
  }
  if (kfBusy.value) return
  kfBusy.value = true
  try {
    const res = await productionApi.keyframes(projectId.value, first.id, { phase: 'trial' })
    ElMessage.success(`打样任务已提交，冻结 ${res.total_cost} 积分`)
    panelVisible.value = true
    startTaskStream()
    void refreshTasks()
    void refreshWallet()
  } finally {
    kfBusy.value = false
  }
}

/**
 * 全量生成关键帧（full：项目全部分镜；确认框提示张数与积分）。
 * @returns 无
 */
async function fullKeyframes(): Promise<void> {
  if (!boards.value.length) {
    ElMessage.warning('暂无分镜，请先完成分镜拆解')
    return
  }
  const n = boards.value.length
  try {
    await ElMessageBox.confirm(
      `将生成 ${n} 张关键帧，预计消耗 ${n * 10} 积分。是否继续？`,
      '全量生成',
      { type: 'info', confirmButtonText: '开始生成' },
    )
  } catch {
    return
  }
  if (kfBusy.value) return
  kfBusy.value = true
  try {
    const res = await productionApi.keyframes(projectId.value, boards.value[0]!.id, {
      phase: 'full',
    })
    ElMessage.success(`全量任务已提交（${res.tasks.length} 个任务，冻结 ${res.total_cost} 积分）`)
    panelVisible.value = true
    startTaskStream()
    void refreshTasks()
    void refreshWallet()
  } finally {
    kfBusy.value = false
  }
}

/**
 * 查询某分镜最新一条已完成的关键帧任务 ID（P-06 上传替换的目标任务）。
 * @param b 目标分镜
 * @returns 任务 ID（查不到返回 null）
 */
async function findKeyframeTaskId(b: Storyboard): Promise<number | null> {
  try {
    const page = await productionApi.taskList(projectId.value, {
      page: 1,
      page_size: 50,
      type: 'image',
      status: 'done',
    })
    const hit = page.items.find((t) => t.storyboard_id === b.id)
    return hit?.id ?? null
  } catch {
    return null
  }
}

/** 正在上传替换的分镜 ID 集合（防重复点击） */
const replaceBusyIds = ref<number[]>([])

/**
 * el-upload 自定义上传：替换该分镜关键帧（P-06 一步 multipart 上传）。
 * 先定位该镜最新已完成关键帧任务，再调 replace 接口；成功后刷新分镜列表
 * （关键帧卡片立即显示新图）并提示。
 * @param options el-upload 上传上下文（file 为所选图片）
 * @param b 目标分镜
 * @returns 无
 */
async function replaceKeyframe(options: UploadRequestOptions, b: Storyboard): Promise<void> {
  const file = options.file as File
  if (!file || replaceBusyIds.value.includes(b.id)) return
  replaceBusyIds.value = [...replaceBusyIds.value, b.id]
  try {
    const taskId = await findKeyframeTaskId(b)
    if (!taskId) {
      ElMessage.warning('未找到该分镜的关键帧任务，无法替换')
      return
    }
    await productionApi.replaceResult(taskId, file)
    ElMessage.success(`#${String(b.sequence).padStart(2, '0')} 镜关键帧已替换`)
    // 刷新分镜列表（后端已联动回写 keyframe_url）与任务列表（result_url 变更）
    const sb = await studioApi.listStoryboard(projectId.value, currentEpisodeId.value ?? undefined)
    boards.value = sb
    void refreshTasks()
  } catch {
    /* 错误已由拦截器提示（400 类型/状态/文件非法、403 越权） */
  } finally {
    replaceBusyIds.value = replaceBusyIds.value.filter((i) => i !== b.id)
  }
}

// ============================================================
// 视频 Tab
// ============================================================

/** 视频参数配置（P-12：分辨率 / 帧率 / 运动幅度，随每次视频任务提交携带） */
const videoConfig = reactive<{
  resolution: '480p' | '720p' | '1080p'
  fps: 24 | 30
  motion_strength: 'low' | 'medium' | 'high'
}>({
  resolution: '720p',
  fps: 24,
  motion_strength: 'medium',
})

/** 每镜「使用尾帧」开关状态（分镜 ID → 是否启用；仅尾帧已生成的分镜可用） */
const useEndFrameMap = reactive<Record<number, boolean>>({})

/** 视频任务提交中的分镜 ID 集合（防重复点击） */
const videoBusyIds = ref<number[]>([])

/** 尾帧任务生成中的分镜 ID 集合（防重复点击 + 按钮 loading） */
const endFrameBusyIds = ref<number[]>([])

/**
 * 生成单镜视频片段（P-12：携带分辨率/帧率/运动幅度；P-03：按开关携带尾帧控制）。
 * @param b 目标分镜
 * @returns 无
 */
async function genVideo(b: Storyboard): Promise<void> {
  if (videoBusyIds.value.includes(b.id)) return
  videoBusyIds.value = [...videoBusyIds.value, b.id]
  try {
    const task = await productionApi.video(projectId.value, b.id, {
      resolution: videoConfig.resolution,
      fps: videoConfig.fps,
      motion_strength: videoConfig.motion_strength,
      use_end_frame: !!useEndFrameMap[b.id],
    })
    ElMessage.success(`视频任务 #${task.id} 已提交`)
    startTaskStream()
    void refreshTasks()
    void refreshWallet()
  } finally {
    videoBusyIds.value = videoBusyIds.value.filter((i) => i !== b.id)
  }
}

/**
 * 生成单镜尾帧图像（P-03/P-08）：创建尾帧任务并轮询至终态，完成后刷新分镜
 * 列表（取回 end_frame_url，解锁该镜「使用尾帧」开关）。
 * @param b 目标分镜
 * @returns 无
 */
async function genEndFrame(b: Storyboard): Promise<void> {
  if (endFrameBusyIds.value.includes(b.id)) return
  endFrameBusyIds.value = [...endFrameBusyIds.value, b.id]
  try {
    const task = await productionApi.endFrame(projectId.value, b.id)
    ElMessage.success(`尾帧任务 #${task.id} 已提交`)
    startTaskStream()
    void refreshTasks()
    void refreshWallet()
    // 轮询该任务直到终态；完成后刷新分镜列表以取回后端回写的 end_frame_url
    const timer = setInterval(async () => {
      try {
        const t = await productionApi.taskDetail(task.id)
        if (t.status === 'done') {
          clearInterval(timer)
          const sb = await studioApi.listStoryboard(
            projectId.value,
            currentEpisodeId.value ?? undefined,
          )
          boards.value = sb
          ElMessage.success(`#${String(b.sequence).padStart(2, '0')} 镜尾帧生成完成`)
        } else if (t.status !== 'failed' && t.status !== 'canceled') {
          return // 仍在排队/生成中，保持 loading，下一轮继续轮询
        } else {
          ElMessage.warning(`#${String(b.sequence).padStart(2, '0')} 镜尾帧任务未成功（${t.status}）`)
        }
        endFrameBusyIds.value = endFrameBusyIds.value.filter((i) => i !== b.id)
      } catch {
        clearInterval(timer)
        endFrameBusyIds.value = endFrameBusyIds.value.filter((i) => i !== b.id)
      }
    }, 2000)
  } catch {
    /* 错误已由拦截器提示（400 无关键帧 / 409 已有尾帧任务） */
    endFrameBusyIds.value = endFrameBusyIds.value.filter((i) => i !== b.id)
  }
}

// ============================================================
// 配音与合成 Tab（S-08）
// ============================================================

/** 角色音色映射（角色 ID → 音色） */
const voiceMap = reactive<Record<number, 'male_01' | 'female_01'>>({})

/**
 * 取某镜使用的音色：取该镜第一个绑定角色的映射，缺省 female_01。
 * @param b 目标分镜
 * @returns 音色标识
 */
function voiceOf(b: Storyboard): 'male_01' | 'female_01' {
  const first = b.characters[0]
  if (first) return voiceMap[first.character_id] ?? 'female_01'
  return 'female_01'
}

/** 配音任务提交中的分镜 ID 集合（防重复点击） */
const ttsBusyIds = ref<number[]>([])

/** 已完成配音的分镜列表（依据后端回写的 audio_url，刷新页面后依然准确） */
const ttsDoneBoards = computed(() => boards.value.filter((x) => x.audio_url))

// ---------- 行内配音试听（单例播放器：同时只播一条） ----------

/** 正在播放配音的分镜 ID（null = 无播放） */
const playingSbId = ref<number | null>(null)
/** 单例音频播放器（模块级复用，避免每行各建一份） */
const audioEl = new Audio()

/**
 * 切换某行配音的播放/暂停：播放新行前先停止当前行。
 * @param b 目标分镜（audio_url 为空时不处理）
 * @returns 无
 */
function togglePlay(b: Storyboard): void {
  if (!b.audio_url) return
  if (playingSbId.value === b.id) {
    audioEl.pause()
    playingSbId.value = null
    return
  }
  audioEl.pause()
  audioEl.src = b.audio_url
  playingSbId.value = b.id
  audioEl
    .play()
    .then(() => {
      /* 播放成功，图标由 playingSbId 驱动 */
    })
    .catch(() => {
      playingSbId.value = null
      ElMessage.warning('音频播放失败，可能该试听资源不可用')
    })
}

// 播放结束/异常时复位图标
audioEl.addEventListener('ended', () => {
  playingSbId.value = null
})
audioEl.addEventListener('error', () => {
  playingSbId.value = null
})

/**
 * 生成单镜配音（TTS）。
 * @param b 目标分镜
 * @returns 无
 */
async function genTts(b: Storyboard): Promise<void> {
  if (ttsBusyIds.value.includes(b.id)) return
  ttsBusyIds.value = [...ttsBusyIds.value, b.id]
  try {
    const task = await productionApi.tts(projectId.value, b.id, { voice: voiceOf(b) })
    ElMessage.success(`配音任务 #${task.id} 已提交`)
    startTaskStream()
    void refreshTasks()
    void refreshWallet()
    // 轮询该任务直到终态；完成后刷新分镜列表以取回后端回写的 audio_url
    const timer = setInterval(async () => {
      try {
        const t = await productionApi.taskDetail(task.id)
        if (t.status === 'done') {
          clearInterval(timer)
          const sb = await studioApi.listStoryboard(
            projectId.value,
            currentEpisodeId.value ?? undefined,
          )
          boards.value = sb
          ElMessage.success(`#${b.sequence} 镜配音完成`)
        } else if (t.status === 'failed' || t.status === 'canceled') {
          clearInterval(timer)
        }
      } catch {
        clearInterval(timer)
      }
    }, 2000)
  } finally {
    ttsBusyIds.value = ttsBusyIds.value.filter((i) => i !== b.id)
  }
}

/**
 * 时长差异处理（RD-31 演示范围说明）：
 * 后端 TTS 完成后会把实际时长回写分镜 actual_duration，但列表接口（StoryboardOut）
 * 未暴露该字段，前端无法读取真实差异；因此本区块以「预计时长」展示，三选一中
 * 仅「重新配音」做真实调用（重发 tts 任务），其余两项为前端演示提示。
 */
const overHandle = ref<'shorten' | 'extend' | 'retts'>('retts')
/** 正在应用时长处理的目标镜 */
const overBoard = ref<Storyboard | null>(null)

/**
 * 应用时长差异处理方式。
 * @returns 无
 */
async function applyOverHandle(): Promise<void> {
  if (!overBoard.value) return
  if (overHandle.value === 'retts') {
    await genTts(overBoard.value)
  } else if (overHandle.value === 'extend') {
    // 演示范围：延长镜头需改分镜时长并重合成，此处仅前端提示
    ElMessage.info('演示范围：延长镜头将在合成阶段按实际音频时长自动适配')
  } else {
    ElMessage.info('演示范围：缩短台词请到分镜页编辑对白后重新配音')
  }
}

// BGM / 字幕 / 转场配置（Task 10.4 / S-08：并入 compose 请求体，字段对齐后端 schema）
// —— BGM：曲库下拉 + 音量滑杆 + 上传自定义（复用 /api/bgm 上传）
// —— 字幕：开关 + 样式（字号/颜色/位置）
// —— 转场：开关 + 类型 + 时长

/** BGM 曲库列表 */
const bgmList = ref<BgmItem[]>([])
/** 是否启用 BGM */
const bgmEnabled = ref(false)
/** 选中的 BGM 地址（/storage/bgm/... 或 /storage/uploads/...） */
const bgmUrl = ref('')
/** BGM 音量系数（0~1，仅作用于 BGM 轨） */
const bgmVolume = ref(0.3)
/** 自定义 BGM 上传中 */
const bgmUploading = ref(false)
/** 隐藏的 BGM 文件选择 input 引用 */
const bgmFileRef = ref<HTMLInputElement>()

/** 是否烧录字幕 */
const subtitleEnabled = ref(false)
/** 字幕样式（字号/颜色/位置/双语，颜色为 #RRGGBB，提交时转 ASS 格式） */
const subtitleStyle = reactive({
  font_size: 24,
  color: '#FFFFFF',
  position: 'bottom' as 'bottom' | 'middle' | 'top',
  bilingual: false,
})

/** 是否启用转场 */
const transitionEnabled = ref(false)
/** 转场类型（P-17：fade/dissolve/fadeblack/fadewhite，后端白名单映射） */
const transitionType = ref('fade')
/** 转场时长（秒，0.1~3.0） */
const transitionDuration = ref(0.5)

/**
 * #RRGGBB 颜色转 ASS PrimaryColour 格式（&H00BBGGRR，内部函数）。
 * @param hex 十六进制颜色（#RRGGBB）
 * @returns ASS 格式颜色串（如 &H00FFFFFF）
 */
function hexToAssColor(hex: string): string {
  const m = /^#?([0-9a-fA-F]{6})$/.exec(hex.trim())
  if (!m) return '&H00FFFFFF'
  const r = m[1]!.slice(0, 2)
  const g = m[1]!.slice(2, 4)
  const b = m[1]!.slice(4, 6)
  return `&H00${b}${g}${r}`.toUpperCase()
}

/**
 * 拉取 BGM 曲库（静默失败不影响主流程）。
 * @returns 无
 */
async function fetchBgmList(): Promise<void> {
  try {
    bgmList.value = await bgmApi.list()
    if (!bgmUrl.value && bgmList.value.length) bgmUrl.value = bgmList.value[0]!.url
  } catch {
    /* 曲库不可用时仍可选择不上传 */
  }
}

/**
 * 选择自定义 BGM 文件并上传，成功后刷新曲库并自动选中。
 * @param e 文件选择事件
 * @returns 无
 */
async function onBgmFileChosen(e: Event): Promise<void> {
  const inputEl = e.target as HTMLInputElement
  const file = inputEl.files?.[0]
  inputEl.value = ''
  if (!file || bgmUploading.value) return
  bgmUploading.value = true
  try {
    const uploaded = await bgmApi.upload(file)
    ElMessage.success('自定义 BGM 已上传')
    await fetchBgmList()
    bgmEnabled.value = true
    bgmUrl.value = uploaded.url
  } catch {
    // 错误已由拦截器提示
  } finally {
    bgmUploading.value = false
  }
}

/**
 * 组装本次合成的 BGM / 字幕 / 转场配置（对齐 ComposeCreateRequest 字段）。
 * @returns ComposePayload 增量字段
 */
function buildMixPayload(): ComposePayload {
  const payload: ComposePayload = {}
  if (bgmEnabled.value && bgmUrl.value) {
    payload.bgm = { url: bgmUrl.value, volume: bgmVolume.value }
  }
  if (subtitleEnabled.value) {
    payload.subtitles = true
    payload.subtitle_style = {
      font_size: subtitleStyle.font_size,
      color: hexToAssColor(subtitleStyle.color),
      position: subtitleStyle.position,
      // P-16 双语字幕：原文在上 + LLM 英文译文在下（翻译失败后端自动降级单行）
      bilingual: subtitleStyle.bilingual,
    }
  }
  if (transitionEnabled.value) {
    payload.transition = { type: transitionType.value, duration: transitionDuration.value }
  }
  return payload
}

// ============================================================
// 合成（进入发布）
// ============================================================

/** 合成进行中 */
const composing = ref(false)
/** 合成任务进度（轮询 taskDetail） */
const composeProgress = ref(0)

/**
 * 发起成片合成并轮询任务直到终态，完成后确认跳转发布页。
 * @returns 无
 */
async function composeFilm(): Promise<void> {
  if (composing.value) return
  composing.value = true
  composeProgress.value = 0
  try {
    // 并入 BGM / 字幕 / 转场配置（Task 10.4，字段对齐后端 ComposeCreateRequest）；
    // S-06：携带当前剧集 ID，仅合成该剧集分镜（默认剧集 = 不传，合成全部）
    const task = await productionApi.compose(projectId.value, {
      mode: 'full',
      ...buildMixPayload(),
      episode_id: currentEpisodeId.value ?? undefined,
    })
    ElMessage.success(`合成任务 #${task.id} 已提交`)
    panelVisible.value = true
    startTaskStream()
    void refreshTasks()
    void refreshWallet()
    // 轮询合成任务详情（进度条展示），终态后引导跳转
    for (;;) {
      await new Promise((r) => setTimeout(r, 2000))
      const t = await productionApi.taskDetail(task.id)
      composeProgress.value = t.progress
      if (t.status === 'done') {
        ElMessage.success('合成完成')
        try {
          await ElMessageBox.confirm('成片已生成，是否前往发布页查看与分享？', '合成完成', {
            type: 'success',
            confirmButtonText: '前往发布',
            cancelButtonText: '留在本页',
          })
          await router.push(`/projects/${projectId.value}/publish`)
        } catch {
          /* 留在本页 */
        }
        break
      }
      if (t.status === 'failed' || t.status === 'canceled') {
        ElMessage.error(t.error || '合成失败，可在任务面板重试')
        break
      }
    }
  } finally {
    composing.value = false
  }
}

onMounted(async () => {
  await loadBase()
  void refreshTasks()
  void fetchBgmList()
  // 进入页面即建立一次 SSE 流：无任务时服务端立即 done 结束，有任务时实时跟踪
  startTaskStream()
  // R-03 重生成直连：发布页分镜条携带 query.storyboard_id 跳入时定位该分镜
  const targetSbId = Number(route.query.storyboard_id)
  if (Number.isInteger(targetSbId) && targetSbId > 0) {
    const hit = boards.value.find((b) => b.id === targetSbId)
    if (hit) {
      highlightSbId.value = targetSbId
      activeTab.value = 'keyframes'
      void nextTick(() => {
        document
          .getElementById(`kf-card-${targetSbId}`)
          ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      })
      ElMessage.info(`已定位到分镜 #${String(hit.sequence).padStart(2, '0')}，可重新发起生成`)
    } else {
      ElMessage.warning('未在当前剧集范围找到该分镜，请检查剧集切换器后重试')
    }
  }
})

onBeforeUnmount(() => {
  stopTaskStream()
  // 离开页面时停止行内试听
  audioEl.pause()
  playingSbId.value = null
})
</script>

<template>
  <div class="page-container prod-view">
    <!-- 顶部：积分消耗条 + 任务面板入口 -->
    <div class="top-bar">
      <el-tabs v-model="activeTab" class="prod-tabs">
        <el-tab-pane label="关键帧" name="keyframes" />
        <el-tab-pane label="视频" name="videos" />
        <el-tab-pane label="配音与合成" name="audio" />
      </el-tabs>
      <div class="top-right">
        <div class="wallet-line">
          <span>可用积分 <b>{{ wallet.balance }}</b></span>
          <el-divider direction="vertical" />
          <span>冻结 <b>{{ wallet.frozen }}</b></span>
        </div>
        <el-badge :value="runningCount" :hidden="!runningCount">
          <el-button @click="openPanel">任务面板</el-button>
        </el-badge>
      </div>
    </div>

    <!-- 首屏骨架 -->
    <el-skeleton v-if="loading" :rows="7" animated />

    <el-empty
      v-else-if="!boards.length"
      description="暂无分镜，请先回分镜页完成拆解"
    >
      <el-button type="primary" @click="$router.push(`/projects/${projectId}/storyboard`)">
        去分镜设计
      </el-button>
    </el-empty>

    <template v-else>
      <!-- ============ 关键帧 Tab ============ -->
      <template v-if="activeTab === 'keyframes'">
        <div class="kf-ops">
          <el-button type="primary" plain :loading="kfBusy" @click="trialKeyframe">
            打样第一镜
          </el-button>
          <el-button type="primary" :loading="kfBusy" @click="fullKeyframes">
            全量生成（{{ boards.length }} 张 ≈ {{ boards.length * 10 }} 积分）
          </el-button>
          <span class="kf-tip">提示：先打样确认风格，再放开全量批量（打样任务在右侧任务面板跟踪）</span>
        </div>
        <div class="kf-grid">
          <el-card
            v-for="b in boards"
            :id="`kf-card-${b.id}`"
            :key="b.id"
            shadow="hover"
            class="kf-card"
            :class="{ 'kf-card--target': highlightSbId === b.id }"
          >
            <div class="kf-img-wrap">
              <el-image
                v-if="b.keyframe_url"
                :src="b.keyframe_url"
                :preview-src-list="[b.keyframe_url]"
                fit="cover"
                class="kf-img"
                preview-teleported
              />
              <div v-else class="kf-img kf-img--empty">
                <span>#{{ String(b.sequence).padStart(2, '0') }}</span>
              </div>
              <!-- 状态角标 -->
              <el-tag
                class="kf-badge"
                size="small"
                effect="dark"
                :type="b.keyframe_url ? 'success' : b.status === 'failed' ? 'danger' : 'info'"
              >
                {{ b.keyframe_url ? '已完成' : b.status === 'failed' ? '失败' : '待生成' }}
              </el-tag>
            </div>
            <div class="kf-caption">
              <span>#{{ String(b.sequence).padStart(2, '0') }} {{ b.shot_type || '' }} · {{ b.duration }}s</span>
              <!-- D3 演示占位角标：该镜最新关键帧任务命中 mock 兜底渠道 -->
              <el-tag
                v-if="sbPlaceholderMap[b.id]"
                size="small"
                type="warning"
                effect="light"
                class="ph-tag"
              >
                演示占位
              </el-tag>
              <!-- P-06 上传替换入口：仅已完成的关键帧可替换 -->
              <el-upload
                v-if="b.keyframe_url"
                :show-file-list="false"
                accept=".png,.jpg,.jpeg,.webp"
                :http-request="(opt: UploadRequestOptions) => replaceKeyframe(opt, b)"
                class="kf-replace"
              >
                <el-button
                  link
                  type="primary"
                  size="small"
                  :loading="replaceBusyIds.includes(b.id)"
                  title="上传替换关键帧（png/jpg/webp，≤10MB）"
                >
                  <el-icon v-if="!replaceBusyIds.includes(b.id)"><UploadIcon /></el-icon>
                  替换
                </el-button>
              </el-upload>
            </div>
          </el-card>
        </div>
      </template>

      <!-- ============ 视频 Tab ============ -->
      <template v-else-if="activeTab === 'videos'">
        <!-- P-12 视频参数配置区（随每次「生成视频片段」提交携带） -->
        <el-card shadow="never" class="video-config-card">
          <div class="video-config">
            <span class="vc-label">分辨率</span>
            <el-select v-model="videoConfig.resolution" size="small" style="width: 96px">
              <el-option label="480p" value="480p" />
              <el-option label="720p" value="720p" />
              <el-option label="1080p" value="1080p" />
            </el-select>
            <span class="vc-label">帧率</span>
            <el-select v-model="videoConfig.fps" size="small" style="width: 76px">
              <el-option label="24" :value="24" />
              <el-option label="30" :value="30" />
            </el-select>
            <span class="vc-label">运动幅度</span>
            <el-select v-model="videoConfig.motion_strength" size="small" style="width: 88px">
              <el-option label="低" value="low" />
              <el-option label="中" value="medium" />
              <el-option label="高" value="high" />
            </el-select>
            <span class="vc-tip">以上参数随每次「生成视频片段」提交生效</span>
          </div>
        </el-card>
        <div class="video-rows">
          <el-card v-for="b in boards" :key="b.id" shadow="never" class="video-row">
            <div class="video-row-body">
              <el-image
                v-if="b.keyframe_url"
                :src="b.keyframe_url"
                fit="cover"
                class="video-thumb"
                :preview-src-list="[b.keyframe_url]"
                preview-teleported
              />
              <div v-else class="video-thumb video-thumb--empty">无首帧</div>
              <div class="video-play">
                <video
                  v-if="b.video_url && !b.video_url.endsWith('.svg')"
                  :src="b.video_url"
                  controls
                  class="video-player"
                />
                <div v-else class="video-player video-player--empty">
                  <span>视频片段{{ b.video_url ? '为占位图（合成后可播）' : '待生成' }}</span>
                </div>
              </div>
              <div class="video-side">
                <div class="video-title">#{{ String(b.sequence).padStart(2, '0') }} {{ b.shot_type || '' }} · {{ b.duration }}s</div>
                <div class="video-desc">{{ b.visual_description || '（无画面描述）' }}</div>
                <!-- P-03/P-08 尾帧控制：仅尾帧已生成的分镜可开启 -->
                <div class="endframe-line">
                  <el-switch
                    v-model="useEndFrameMap[b.id]"
                    size="small"
                    :disabled="!b.end_frame_url"
                  />
                  <span class="endframe-label">使用尾帧</span>
                  <el-tag v-if="b.end_frame_url" size="small" type="success" effect="plain">
                    尾帧就绪
                  </el-tag>
                  <el-button
                    v-else
                    size="small"
                    text
                    type="primary"
                    :loading="endFrameBusyIds.includes(b.id)"
                    :disabled="!b.keyframe_url"
                    title="先生成关键帧，再生成尾帧"
                    @click="genEndFrame(b)"
                  >
                    先生成尾帧
                  </el-button>
                </div>
                <el-button
                  type="primary"
                  plain
                  size="small"
                  :loading="videoBusyIds.includes(b.id)"
                  @click="genVideo(b)"
                >
                  {{ b.video_url ? '重新生成视频片段' : '生成视频片段' }}
                </el-button>
              </div>
            </div>
          </el-card>
        </div>
      </template>

      <!-- ============ 配音与合成 Tab（S-08） ============ -->
      <template v-else>
        <!-- 角色音色映射 -->
        <el-card shadow="never" class="block-card">
          <template #header><span>角色音色映射</span></template>
          <el-empty
            v-if="!characters.length"
            description="暂无角色资产"
            :image-size="60"
          />
          <el-table v-else :data="characters" size="small">
            <el-table-column prop="name" label="角色" width="160" />
            <el-table-column label="音色" width="220">
              <template #default="{ row }">
                <el-select v-model="voiceMap[row.id]" placeholder="female_01（默认）" style="width: 180px">
                  <el-option label="男声 male_01" value="male_01" />
                  <el-option label="女声 female_01" value="female_01" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="试听">
              <template #default>
                <!-- 试听占位：TTS 试听接口未提供 -->
                <el-button size="small" disabled>试听 ▶（暂未开放）</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <!-- 每镜配音 -->
        <el-card shadow="never" class="block-card">
          <template #header><span>分镜配音</span></template>
          <el-table :data="boards" size="small">
            <el-table-column label="镜号" width="80">
              <template #default="{ row }">#{{ String(row.sequence).padStart(2, '0') }}</template>
            </el-table-column>
            <el-table-column prop="dialogue" label="对白" show-overflow-tooltip />
            <el-table-column label="预计时长" width="90">
              <template #default="{ row }">{{ row.duration }}s</template>
            </el-table-column>
            <el-table-column label="音色" width="110">
              <template #default="{ row }">{{ voiceOf(row) === 'male_01' ? '男声' : '女声' }}</template>
            </el-table-column>
            <el-table-column label="操作" width="190">
              <template #default="{ row }">
                <el-button
                  size="small"
                  type="primary"
                  plain
                  :loading="ttsBusyIds.includes(row.id)"
                  @click="genTts(row)"
                >
                  {{ row.audio_url ? '重新配音' : '生成配音' }}
                </el-button>
                <!-- 已配音：行内播放/暂停试听图标（替代原"已配音"标签）；未配音：未生成占位 -->
                <el-button
                  v-if="row.audio_url"
                  link
                  type="primary"
                  class="tts-play-btn"
                  :title="playingSbId === row.id ? '暂停试听' : '播放配音'"
                  @click="togglePlay(row)"
                >
                  <el-icon :size="18">
                    <VideoPause v-if="playingSbId === row.id" />
                    <VideoPlay v-else />
                  </el-icon>
                </el-button>
                <el-tag v-else size="small" type="info" effect="plain" class="tts-none-tag">
                  未生成
                </el-tag>
              </template>
            </el-table-column>
          </el-table>

          <!-- 时长差异处理（三选一；演示范围见脚本注释） -->
          <div v-if="ttsDoneBoards.length" class="over-box">
            <div class="over-title">
              时长差异处理（已完成配音 {{ ttsDoneBoards.length }} 镜；实际时长由后端在合成阶段自动对齐）
            </div>
            <el-select v-model="overBoard" value-key="id" placeholder="选择分镜" style="width: 200px" size="small">
              <el-option
                v-for="b in ttsDoneBoards"
                :key="b.id"
                :label="`#${String(b.sequence).padStart(2, '0')}（预计 ${b.duration}s）`"
                :value="b"
              />
            </el-select>
            <el-radio-group v-model="overHandle" class="over-radios">
              <el-radio value="shorten">缩短台词</el-radio>
              <el-radio value="extend">延长镜头</el-radio>
              <el-radio value="retts">重新配音</el-radio>
            </el-radio-group>
            <el-button
              size="small"
              type="primary"
              :disabled="!overBoard"
              :loading="overBoard ? ttsBusyIds.includes(overBoard.id) : false"
              @click="applyOverHandle"
            >
              应用
            </el-button>
          </div>
        </el-card>

        <!-- BGM / 字幕 / 转场（Task 10.4：配置并入 compose 请求体） -->
        <el-card shadow="never" class="block-card">
          <template #header><span>BGM / 字幕 / 转场</span></template>
          <el-form label-width="90px" class="mix-form">
            <el-form-item label="背景音乐">
              <el-switch v-model="bgmEnabled" />
              <template v-if="bgmEnabled">
                <el-select v-model="bgmUrl" placeholder="选择曲库曲目" style="width: 260px; margin-left: 12px">
                  <el-option
                    v-for="b in bgmList"
                    :key="b.url"
                    :label="`${b.name}${b.duration ? `（${Math.round(b.duration)}s）` : ''}`"
                    :value="b.url"
                  />
                </el-select>
                <el-button
                  size="small"
                  style="margin-left: 8px"
                  :loading="bgmUploading"
                  @click="bgmFileRef?.click()"
                >
                  上传自定义
                </el-button>
                <input
                  ref="bgmFileRef"
                  type="file"
                  accept="audio/*"
                  style="display: none"
                  @change="onBgmFileChosen"
                />
              </template>
            </el-form-item>
            <el-form-item v-if="bgmEnabled" label="BGM 音量">
              <el-slider
                v-model="bgmVolume"
                :min="0"
                :max="1"
                :step="0.05"
                show-input
                :show-input-controls="false"
                style="max-width: 320px"
              />
            </el-form-item>
            <el-form-item label="字幕">
              <el-switch v-model="subtitleEnabled" />
              <template v-if="subtitleEnabled">
                <span class="mix-inline-label">字号</span>
                <el-input-number
                  v-model="subtitleStyle.font_size"
                  :min="10"
                  :max="72"
                  size="small"
                  style="width: 100px; margin-left: 8px"
                />
                <span class="mix-inline-label">颜色</span>
                <el-color-picker v-model="subtitleStyle.color" size="small" />
                <el-select
                  v-model="subtitleStyle.position"
                  size="small"
                  style="width: 100px; margin-left: 8px"
                >
                  <el-option label="底部" value="bottom" />
                  <el-option label="居中" value="middle" />
                  <el-option label="顶部" value="top" />
                </el-select>
                <!-- P-16 双语字幕开关：原文在上 + 英文译文在下 -->
                <el-checkbox
                  v-model="subtitleStyle.bilingual"
                  size="small"
                  style="margin-left: 12px"
                >
                  双语字幕（中英）
                </el-checkbox>
              </template>
            </el-form-item>
            <el-form-item label="转场">
              <el-switch v-model="transitionEnabled" />
              <template v-if="transitionEnabled">
                <!-- P-17：转场类型对齐后端 xfade 白名单（非法值后端回退 fade） -->
                <el-select v-model="transitionType" size="small" style="width: 130px; margin-left: 8px">
                  <el-option label="淡入淡出" value="fade" />
                  <el-option label="叠化" value="dissolve" />
                  <el-option label="闪黑" value="fadeblack" />
                  <el-option label="闪白" value="fadewhite" />
                </el-select>
                <span class="mix-inline-label">时长 {{ transitionDuration.toFixed(1) }}s</span>
                <el-slider
                  v-model="transitionDuration"
                  :min="0.1"
                  :max="3"
                  :step="0.1"
                  style="width: 160px; margin-left: 8px"
                />
              </template>
            </el-form-item>
          </el-form>
          <div class="mix-note">以上配置在点击「全部完成，进入合成」时随合成任务提交生效</div>
        </el-card>
      </template>

      <!-- 合成进度 + 底部按钮 -->
      <div class="footer-bar">
        <el-progress
          v-if="composing"
          :percentage="composeProgress"
          class="compose-progress"
        />
        <span v-else class="footer-tip">全部镜头关键帧 / 视频 / 配音完成后即可合成成片</span>
        <el-button size="large" type="primary" class="confirm-btn" :loading="composing" @click="composeFilm">
          全部完成，进入合成
        </el-button>
      </div>
    </template>

    <!-- ============ 全局任务面板抽屉 ============ -->
    <el-drawer v-model="panelVisible" title="全局任务面板" size="480px" append-to-body>
      <el-empty v-if="!tasks.length" description="暂无生成任务" />
      <div v-else class="task-list">
        <div v-for="t in tasks" :key="t.id" class="task-item">
          <div class="task-line">
            <span class="task-id">#{{ t.id }}</span>
            <el-tag size="small" effect="plain">{{ taskTypeLabel[t.type] ?? t.type }}</el-tag>
            <el-tag size="small" :type="taskStatusType[t.status] ?? 'info'">
              {{ { queued: '排队中', running: '生成中', done: '已完成', failed: '失败', canceled: '已取消' }[t.status] ?? t.status }}
            </el-tag>
            <!-- D3 演示占位角标：mock 兜底渠道产物（已计费豁免） -->
            <el-tag v-if="t.placeholder" size="small" type="warning" effect="light">
              演示占位
            </el-tag>
            <span v-if="t.storyboard_id" class="task-sb">镜 #{{ t.storyboard_id }}</span>
            <div class="task-ops">
              <el-button v-if="t.status === 'failed'" size="small" text type="primary" @click="retryTask(t)">
                重试
              </el-button>
              <el-button
                v-if="t.status === 'queued' || t.status === 'running'"
                size="small"
                text
                type="danger"
                @click="cancelTask(t)"
              >
                取消
              </el-button>
            </div>
          </div>
          <el-progress
            v-if="t.status === 'running' || t.status === 'queued'"
            :percentage="t.progress"
            :stroke-width="8"
            class="task-progress"
          />
          <div v-if="t.result_url" class="task-result">
            结果：
            <a :href="t.result_url" target="_blank" rel="noopener">{{ t.result_url }}</a>
          </div>
          <div v-if="t.error" class="task-error">{{ t.error }}</div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.prod-view {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-4);
}
.top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-4);
}
.prod-tabs {
  flex: 1;
}
.top-right {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-4);
}
.wallet-line {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  white-space: nowrap;
}
.wallet-line b {
  color: var(--mf-brand);
}
.kf-ops {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.kf-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.kf-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: var(--mf-sp-4);
}
.kf-card :deep(.el-card__body) {
  padding: 0;
}
/* R-03 重生成直连：发布页跳入时目标分镜卡片高亮边框 */
.kf-card--target {
  border: 2px solid var(--mf-brand);
  box-shadow: 0 0 0 3px rgba(64, 158, 255, 0.18);
}
.kf-img-wrap {
  position: relative;
  height: 220px;
  border-radius: var(--mf-r-ctl) var(--mf-r-ctl) 0 0;
  overflow: hidden;
}
.kf-img {
  width: 100%;
  height: 100%;
  display: block;
}
.kf-img--empty {
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, var(--mf-surface-2), var(--mf-border));
  color: var(--mf-text-3);
  font-size: var(--mf-fs-2xl);
}
.kf-badge {
  position: absolute;
  top: 8px;
  right: 8px;
}
.kf-caption {
  padding: var(--mf-sp-2) var(--mf-sp-3);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  display: flex;
  align-items: center;
  gap: 6px;
}
/* P-06 上传替换入口（关键帧卡片下方，右对齐） */
.kf-replace {
  margin-left: auto;
  display: inline-flex;
}
/* D3 演示占位角标通用间距 */
.ph-tag {
  flex-shrink: 0;
}
/* P-12 视频参数配置区 */
.video-config-card {
  margin-bottom: var(--mf-sp-3);
}
.video-config {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  flex-wrap: wrap;
}
.vc-label {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.vc-tip {
  margin-left: auto;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
/* P-03/P-08 尾帧控制行（视频行操作区） */
.endframe-line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: var(--mf-sp-2);
}
.endframe-label {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.video-rows {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-4);
}
.video-row-body {
  display: grid;
  grid-template-columns: 140px 1fr 240px;
  gap: var(--mf-sp-4);
  align-items: center;
}
.video-thumb {
  width: 140px;
  height: 100px;
  border-radius: var(--mf-r-ctl);
}
.video-thumb--empty {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--mf-surface-2);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.video-player {
  width: 100%;
  max-height: 220px;
  border-radius: var(--mf-r-ctl);
  background: #000;
}
.video-player--empty {
  height: 120px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--mf-surface-2);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.video-title {
  font-weight: var(--mf-fw-semibold);
  margin-bottom: 4px;
}
.video-desc {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  margin-bottom: var(--mf-sp-2);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.block-card {
  margin-bottom: 0;
}
.tts-none-tag {
  margin-left: 8px;
}
/* 行内配音试听图标（播放/暂停切换） */
.tts-play-btn {
  margin-left: 8px;
  vertical-align: middle;
}
.over-box {
  margin-top: var(--mf-sp-4);
  padding: var(--mf-sp-3);
  border: 1px dashed var(--mf-border-strong);
  border-radius: var(--mf-r-ctl);
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  flex-wrap: wrap;
}
.over-title {
  width: 100%;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.over-radios {
  margin: 0 var(--mf-sp-2);
}
.mix-form {
  max-width: 640px;
}
.mix-inline-label {
  margin-left: 12px;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.mix-note {
  margin-top: var(--mf-sp-2);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.footer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-4);
  position: sticky;
  bottom: 0;
  /* sticky 吸底时浮于 positioned 内容（如 el-table 单元格）之上，避免滚动穿透重叠 */
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
.compose-progress {
  flex: 1;
}
.confirm-btn {
  background: var(--mf-brand);
  border-color: var(--mf-brand);
  color: var(--mf-bg);
}
.task-list {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
}
.task-item {
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
  padding: var(--mf-sp-3);
}
.task-line {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.task-id {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.task-sb {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.task-ops {
  margin-left: auto;
}
.task-progress {
  margin-top: var(--mf-sp-2);
}
.task-result {
  margin-top: var(--mf-sp-2);
  font-size: var(--mf-fs-sm);
  color: var(--mf-text-2);
  word-break: break-all;
}
.task-result a {
  color: var(--mf-brand);
}
.task-error {
  margin-top: var(--mf-sp-2);
  font-size: var(--mf-fs-sm);
  color: var(--el-color-danger);
}
</style>
