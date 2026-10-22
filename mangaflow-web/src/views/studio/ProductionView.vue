<!--
  内容生成页（文件级注释）。

  对应原型 S-07（内容生成）+ S-08（配音与合成），路由 /projects/:id/production。
  功能：
  - 顶部积分消耗条：billing.wallet 的可用 / 冻结积分；
  - 四个 Tab（el-tabs，音频先行流程顺序：关键帧 → 配音 → 视频 → 合成）：
    ① 关键帧：分镜网格卡（keyframe_url 图或占位 + 状态角标 + D3「演示占位」角标）；
      每张卡片提供「生成 / 重新生成」按钮 keyframes(phase=trial) 单镜生成
      （已有关键帧时先确认覆盖）；「全量生成」确认框（将生成 N 张、
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
import { Loading as LoadingIcon, Upload as UploadIcon, VideoPause, VideoPlay } from '@element-plus/icons-vue'
import * as productionApi from '@/api/production'
import * as billingApi from '@/api/billing'
import * as bgmApi from '@/api/bgm'
import * as projectApi from '@/api/projects'
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
/** 项目状态（生成阶段门禁：仅 producing 允许发起新生成任务） */
const projectStatus = ref('')

/** 分镜是否已确认进入生成阶段（未确认时禁发新任务，仅展示历史产物） */
const productionConfirmed = computed(() => projectStatus.value === 'producing')

/**
 * 生成动作守卫：未确认分镜（项目状态非 production）时拦截并提示。
 * @returns 是否允许继续执行生成动作
 */
function ensureProductionReady(): boolean {
  if (productionConfirmed.value) return true
  ElMessage.warning('请先在分镜设计页点击「确认分镜，进入生成」')
  return false
}

/**
 * 拉取分镜与角色资产。
 * S-06：分镜按当前选中剧集过滤（默认剧集 = 不传 episode_id，返回全部）。
 * @returns 无
 */
async function loadBase(): Promise<void> {
  loading.value = true
  try {
    const [sb, assets, walletRes, proj] = await Promise.all([
      studioApi.listStoryboard(projectId.value, currentEpisodeId.value ?? undefined),
      studioApi.listAssets(projectId.value),
      billingApi.wallet().catch(() => null),
      // 项目详情：取 status 做生成阶段门禁（失败不阻塞首屏，门禁按未确认处理）
      projectApi.get(projectId.value).catch(() => null),
    ])
    boards.value = sb
    characters.value = assets.characters
    projectStatus.value = proj?.status ?? ''
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
              // 清理已完成段配音的「生成中」状态（批量/单段提交统一在此收敛；
              // ttsRows 依赖 boards 自动重算，按行 key 判定该段是否已有产物）
              const rowsByKey = new Map(ttsRows.value.map((r) => [r.key, r]))
              ttsSegGenerating.value = ttsSegGenerating.value.filter((k) => {
                const r = rowsByKey.get(k)
                return r ? !r.audioUrl : false
              })
              // 视频生成中状态收敛：已回写 video_url 的镜恢复正常展示
              videoGeneratingIds.value = videoGeneratingIds.value.filter((id) => {
                const b = sb.find((x) => x.id === id)
                return b ? !b.video_url : false
              })
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

/** 单镜「生成」进行中的分镜 ID 集合（防重复点击） */
const genBusyIds = ref<number[]>([])

/**
 * 判断某分镜关键帧是否正在生成（本地提交中或任务面板存在排队/进行中任务）。
 * @param b 目标分镜
 * @returns 是否生成中
 */
function isKeyframeBusy(b: Storyboard): boolean {
  if (genBusyIds.value.includes(b.id)) return true
  return tasks.value.some(
    (t) => t.type === 'image' && t.storyboard_id === b.id
      && (t.status === 'queued' || t.status === 'running'),
  )
}

/**
 * 单镜生成关键帧（trial：单分镜 1 张，替代原「打样第一镜」入口）。
 * 已有关键帧的镜重新生成前确认覆盖；进行中任务由右侧任务面板跟踪。
 * @param b 目标分镜
 * @returns 无
 */
async function genKeyframe(b: Storyboard): Promise<void> {
  if (!ensureProductionReady()) return
  if (isKeyframeBusy(b)) return
  if (b.keyframe_url) {
    try {
      await ElMessageBox.confirm(
        `#${String(b.sequence).padStart(2, '0')} 镜已有关键帧，重新生成将覆盖原图（10 积分/张）。是否继续？`,
        '重新生成',
        { type: 'warning', confirmButtonText: '重新生成' },
      )
    } catch {
      return
    }
  }
  genBusyIds.value = [...genBusyIds.value, b.id]
  try {
    const res = await productionApi.keyframes(projectId.value, b.id, { phase: 'trial' })
    ElMessage.success(`#${String(b.sequence).padStart(2, '0')} 镜生成任务已提交，冻结 ${res.total_cost} 积分`)
    panelVisible.value = true
    startTaskStream()
    void refreshTasks()
    void refreshWallet()
  } finally {
    genBusyIds.value = genBusyIds.value.filter((i) => i !== b.id)
  }
}

/**
 * 全量生成关键帧（full：项目全部分镜；确认框提示张数与积分）。
 * @returns 无
 */
async function fullKeyframes(): Promise<void> {
  if (!ensureProductionReady()) return
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
      // S-06 按集工作流：选中具体集时全量只生成该集镜头（None=全部）
      episode_id: currentEpisodeId.value ?? undefined,
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

/** 视频任务执行中的分镜 ID 集合（提交后到任务终态，驱动行内动态加载样式） */
const videoGeneratingIds = ref<number[]>([])

/**
 * 判断某镜视频是否在生成中（提交请求或任务轮询阶段）。
 * @param b 目标分镜
 * @returns 是否生成中
 */
function isVideoBusy(b: Storyboard): boolean {
  return videoBusyIds.value.includes(b.id) || videoGeneratingIds.value.includes(b.id)
}

/** 尾帧任务生成中的分镜 ID 集合（防重复点击 + 按钮 loading） */
const endFrameBusyIds = ref<number[]>([])

// ---------- 视频 Tab 行内配音同步播放（视频 agent 产物无声，配音按时刻对齐驱动） ----------

/**
 * 配音同步开关（默认关闭 = 配音与视频隔离，纯视频预览验证画面内容；
 * 打开后播放视频时按时刻同步接入该镜 TTS 配音轨）。
 */
const dubSyncEnabled = ref(false)

/** 分镜 ID → 行内 video 元素引用（模板 ref 回调登记） */
const sbVideoRefs = new Map<number, HTMLVideoElement>()

/** 分镜 ID → 配音 Audio 实例（懒建缓存，随 video 播放/暂停/seek 联动） */
const sbAudioEls = new Map<number, HTMLAudioElement>()

/**
 * 模板 ref 回调：登记/注销行内 video 元素引用。
 * @param sbId 分镜 ID
 * @param el video 元素（v-if 卸载时为 null）
 * @returns 无
 */
function setVideoRef(sbId: number, el: unknown): void {
  if (el instanceof HTMLVideoElement) sbVideoRefs.set(sbId, el)
  else sbVideoRefs.delete(sbId)
}

/**
 * 取（或懒建）该镜配音 Audio 实例（src 变化时重建）。
 * 无对白分镜不参与配音同步（防存量「画面描述旁白」类脏数据被播出）。
 * @param b 目标分镜（须有对白与 audio_url）
 * @returns Audio 实例（无对白/无配音返回 null）
 */
function getDubAudio(b: Storyboard): HTMLAudioElement | null {
  if (!b.audio_url || !(b.dialogue || '').trim()) return null
  let el = sbAudioEls.get(b.id)
  if (!el || el.src !== new URL(b.audio_url, window.location.href).href) {
    el?.pause()
    el = new Audio(b.audio_url)
    el.preload = 'auto'
    sbAudioEls.set(b.id, el)
  }
  return el
}

/**
 * video 播放联动：配音从视频当前时刻同步播放（音画对齐——视频时长与
 * 配音时长在任务创建时已按 ceil 对齐）。
 * @param b 目标分镜
 * @returns 无
 */
function onVideoPlay(b: Storyboard): void {
  const v = sbVideoRefs.get(b.id)
  // 配音隔离开关关闭时纯视频预览（不叠加 TTS 音轨）
  if (!dubSyncEnabled.value) return
  // 视频已内嵌当前配音（混流产物）：播放器自带音轨，再叠加会双声
  if (b.audio?.video_dubbed) return
  const a = getDubAudio(b)
  if (!v || !a) return
  try {
    a.currentTime = Math.min(v.currentTime, a.duration || v.currentTime)
  } catch {
    /* 元数据未就绪时由浏览器按 0 起播 */
  }
  void a.play().catch(() => {
    /* 自动播放策略拦截时静默：用户再点播放可恢复 */
  })
}

/**
 * video 暂停/结束联动：配音同步暂停。
 * @param b 目标分镜
 * @returns 无
 */
function onVideoPause(b: Storyboard): void {
  sbAudioEls.get(b.id)?.pause()
}

/**
 * video 进度条 seek 联动：配音跳转到同一时刻。
 * @param b 目标分镜
 * @returns 无
 */
function onVideoSeek(b: Storyboard): void {
  const v = sbVideoRefs.get(b.id)
  const a = sbAudioEls.get(b.id)
  if (!v || !a) return
  try {
    a.currentTime = Math.min(v.currentTime, a.duration || v.currentTime)
  } catch {
    /* seek 目标超出音频时长时忽略 */
  }
}

/**
 * 提交单镜视频任务并启动终态轮询（genVideo 与批量生成的公共主体）。
 * 携带分辨率/帧率/运动幅度（P-12）与该镜尾帧开关（P-03）；提交后由
 * 右侧任务面板 + SSE 跟踪，终态后刷新分镜取回 video_url。
 * @param b 目标分镜
 * @returns 是否提交成功（提交失败/被防重拦截返回 false）
 */
async function submitVideoTask(b: Storyboard): Promise<boolean> {
  try {
    const task = await productionApi.video(projectId.value, b.id, {
      resolution: videoConfig.resolution,
      fps: videoConfig.fps,
      motion_strength: videoConfig.motion_strength,
      use_end_frame: !!useEndFrameMap[b.id],
    })
    videoGeneratingIds.value = [...videoGeneratingIds.value, b.id]
    startTaskStream()
    void refreshTasks()
    void refreshWallet()
    // 轮询任务终态（视频生成 1~5 分钟，5s 间隔）；完成后刷新分镜取回 video_url
    const timer = setInterval(async () => {
      try {
        const t = await productionApi.taskDetail(task.id)
        if (t.status === 'done') {
          clearInterval(timer)
          boards.value = await studioApi.listStoryboard(
            projectId.value,
            currentEpisodeId.value ?? undefined,
          )
          ElMessage.success(`#${String(b.sequence).padStart(2, '0')} 镜视频生成完成`)
        } else if (t.status === 'failed' || t.status === 'canceled') {
          clearInterval(timer)
        } else {
          return
        }
        videoGeneratingIds.value = videoGeneratingIds.value.filter((i) => i !== b.id)
      } catch {
        clearInterval(timer)
        videoGeneratingIds.value = videoGeneratingIds.value.filter((i) => i !== b.id)
      }
    }, 5000)
    return true
  } catch {
    return false
  }
}

/**
 * 生成单镜视频片段（P-12：携带分辨率/帧率/运动幅度；P-03：按开关携带尾帧控制）。
 * @param b 目标分镜
 * @returns 无
 */
async function genVideo(b: Storyboard): Promise<void> {
  if (!ensureProductionReady()) return
  if (isVideoBusy(b)) return
  videoBusyIds.value = [...videoBusyIds.value, b.id]
  try {
    const ok = await submitVideoTask(b)
    if (ok) ElMessage.success('视频任务已提交')
  } finally {
    videoBusyIds.value = videoBusyIds.value.filter((i) => i !== b.id)
  }
}

/** 批量生成全部视频进行中（防重复点击） */
const batchVideoBusy = ref(false)

/**
 * 一键生成全部视频：对所有「有关键帧且尚无视频产物」的镜逐个提交视频任务
 * （不等终态，任务在右侧任务面板跟踪；无关键帧的镜自动跳过并提示；
 * 单个提交失败不影响其余镜头）。视频参数取当前参数区配置，尾帧开关
 * 按每镜自身开关状态携带。
 * @returns 无
 */
async function batchGenVideos(): Promise<void> {
  if (!ensureProductionReady()) return
  if (batchVideoBusy.value) return
  const pending = boards.value.filter((b) => b.keyframe_url && !b.video_url && !isVideoBusy(b))
  const noKeyframe = boards.value.filter((b) => !b.keyframe_url)
  if (!pending.length) {
    ElMessage.info(noKeyframe.length ? '无待生成视频的镜头（无关键帧的镜请先生成关键帧）' : '所有镜头均已生成视频')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将为 ${pending.length} 个未生成视频的镜头逐个提交视频任务（按镜头时长计费，视频时长自动对齐配音）。是否继续？`,
      '一键生成全部视频',
      { type: 'info', confirmButtonText: '开始生成' },
    )
  } catch {
    return
  }
  batchVideoBusy.value = true
  try {
    let submitted = 0
    const skipped: string[] = []
    for (const b of pending) {
      if (isVideoBusy(b)) {
        skipped.push(`#${String(b.sequence).padStart(2, '0')}（任务进行中）`)
        continue
      }
      videoBusyIds.value = [...videoBusyIds.value, b.id]
      try {
        const ok = await submitVideoTask(b)
        if (ok) submitted += 1
        else skipped.push(`#${String(b.sequence).padStart(2, '0')}（提交失败）`)
      } catch {
        skipped.push(`#${String(b.sequence).padStart(2, '0')}（提交失败）`)
      } finally {
        videoBusyIds.value = videoBusyIds.value.filter((i) => i !== b.id)
      }
    }
    if (submitted) {
      ElMessage.success(`已提交 ${submitted} 个视频生成任务`)
      panelVisible.value = true
      startTaskStream()
      void refreshTasks()
      void refreshWallet()
    }
    if (noKeyframe.length) {
      ElMessage.warning(`以下镜头无关键帧已跳过（请先生成关键帧）：${noKeyframe.map((b) => `#${String(b.sequence).padStart(2, '0')}`).join('、')}`)
    }
    if (skipped.length) {
      ElMessage.warning(`以下镜头提交被跳过：${skipped.join('、')}`)
    }
  } finally {
    batchVideoBusy.value = false
  }
}

/**
 * 生成单镜尾帧图像（P-03/P-08）：创建尾帧任务并轮询至终态，完成后刷新分镜
 * 列表（取回 end_frame_url，解锁该镜「使用尾帧」开关）。
 * @param b 目标分镜
 * @returns 无
 */
async function genEndFrame(b: Storyboard): Promise<void> {
  if (!ensureProductionReady()) return
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

/** 有对白的分镜列表（分镜配音表仅展示有角色对话的镜；无对白镜无需配音） */
const dialogueBoards = computed(() => boards.value.filter((x) => (x.dialogue || '').trim()))

// ---------- 行内配音试听（单例播放器：同时只播一条） ----------

/** 正在播放的音频行 key（配音表行唯一标识，null = 无播放） */
const playingAudioKey = ref<string | null>(null)
/** 单例音频播放器（模块级复用，避免每行各建一份） */
const audioEl = new Audio()

/**
 * 播放/暂停指定音频 URL（行 key 驱动图标切换；播放新行前先停止当前行）。
 * @param url 音频地址（空不处理）
 * @param key 行唯一 key
 * @returns 无
 */
function togglePlayUrl(url: string | null | undefined, key: string): void {
  if (!url) return
  if (playingAudioKey.value === key) {
    audioEl.pause()
    playingAudioKey.value = null
    return
  }
  audioEl.pause()
  audioEl.src = url
  playingAudioKey.value = key
  audioEl
    .play()
    .then(() => {
      /* 播放成功，图标由 playingAudioKey 驱动 */
    })
    .catch(() => {
      playingAudioKey.value = null
      ElMessage.warning('音频播放失败，可能该试听资源不可用')
    })
}

// 播放结束/异常时复位图标（分镜试听与角色试音共用单例播放器）
audioEl.addEventListener('ended', () => {
  playingAudioKey.value = null
  playingVoiceId.value = null
})
audioEl.addEventListener('error', () => {
  playingAudioKey.value = null
  playingVoiceId.value = null
})

// ---------- 音色确认闭环（试音 → 锁定 → 门禁分镜配音） ----------

/** 音色试音任务提交中的角色 ID 集合（防重复点击） */
const voicePreviewBusyIds = ref<number[]>([])
/** 音色锁定/解锁提交中的角色 ID 集合（防重复点击） */
const voiceLockBusyIds = ref<number[]>([])
/** 正在播放试音的角色 ID（null = 无播放，与分镜试听共用单例播放器） */
const playingVoiceId = ref<number | null>(null)

interface DialogueSeg {
  /** 说话人名（无「XXX：」前缀的行 为 null） */
  speaker: string | null
  /** 台词文本 */
  text: string
}

/**
 * 解析分镜对白为「说话人 + 台词」分段（对白列角色名高亮渲染用）。
 * @param b 目标分镜
 * @returns 分段列表（保持行序）
 */
function dialogueSegments(b: Storyboard): DialogueSeg[] {
  const segs: DialogueSeg[] = []
  for (const line of String(b.dialogue || '').split('\n')) {
    const t = line.trim()
    if (!t) continue
    const m = t.match(/^([^：:]{1,20})[：:](.+)$/)
    if (m && m[1] && m[2]) segs.push({ speaker: m[1].trim(), text: m[2].trim() })
    else segs.push({ speaker: null, text: t })
  }
  return segs
}

/**
 * 播放/暂停角色试音音频（复用单例播放器，与分镜试听互斥）。
 * @param row 目标角色（voice_sample_url 为空时不处理）
 * @returns 无
 */
function playVoiceSample(row: CharacterAsset): void {
  if (!row.voice_sample_url) return
  if (playingVoiceId.value === row.id) {
    audioEl.pause()
    playingVoiceId.value = null
    return
  }
  audioEl.pause()
  playingAudioKey.value = null
  audioEl.src = row.voice_sample_url
  playingVoiceId.value = row.id
  audioEl
    .play()
    .catch(() => {
      playingVoiceId.value = null
      ElMessage.warning('试音播放失败，请重新生成试音')
    })
}

/**
 * 重新拉取资产列表（试音回写 voice_sample_url 后刷新行内状态）。
 * @returns 无
 */
async function refreshCharacters(): Promise<void> {
  try {
    const assets = await studioApi.listAssets(projectId.value)
    characters.value = assets.characters
  } catch {
    /* 静默失败：行内状态以下次刷新兜底 */
  }
}

/**
 * 生成角色试音（「我是XX，这是我的配音」）：提交任务并轮询至终态，
 * 完成后刷新角色（取回 voice_sample_url）并自动播放。
 * @param row 目标角色（音色取映射表当前选择，未选走自动路由）
 * @returns 无
 */
async function previewVoice(row: CharacterAsset): Promise<void> {
  if (voicePreviewBusyIds.value.includes(row.id)) return
  voicePreviewBusyIds.value = [...voicePreviewBusyIds.value, row.id]
  try {
    const task = await productionApi.voicePreview(
      projectId.value, row.id,
      row.voice_selected ? { voice: row.voice_selected as 'male_01' | 'male_02' | 'female_01' | 'female_02' } : {},
    )
    startTaskStream()
    void refreshTasks()
    void refreshWallet()
    const timer = setInterval(async () => {
      try {
        const t = await productionApi.taskDetail(task.id)
        if (t.status === 'done') {
          clearInterval(timer)
          await refreshCharacters()
          const fresh = characters.value.find((c) => c.id === row.id)
          if (fresh?.voice_sample_url) playVoiceSample(fresh)
        } else if (t.status === 'failed' || t.status === 'canceled') {
          clearInterval(timer)
        }
      } catch {
        /* 轮询失败下一轮重试 */
      }
    }, 2000)
  } catch {
    /* 错误已由拦截器提示（409 积分不足等） */
  } finally {
    voicePreviewBusyIds.value = voicePreviewBusyIds.value.filter((i) => i !== row.id)
  }
}

/**
 * 锁定角色音色（按角色音色设定自动路由固化，放行该角色所在镜头配音）。
 * @param row 目标角色
 * @returns 无
 */
async function lockVoice(row: CharacterAsset): Promise<void> {
  if (voiceLockBusyIds.value.includes(row.id)) return
  voiceLockBusyIds.value = [...voiceLockBusyIds.value, row.id]
  try {
    const updated = await studioApi.voiceLock(projectId.value, row.id)
    const idx = characters.value.findIndex((c) => c.id === row.id)
    if (idx >= 0) characters.value[idx] = updated
    ElMessage.success(`角色「${row.name}」音色已锁定（${updated.voice_selected ?? '自动'}）`)
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    voiceLockBusyIds.value = voiceLockBusyIds.value.filter((i) => i !== row.id)
  }
}

/**
 * 解锁角色音色（误锁纠正；解锁后该角色所在镜头配音重新被门禁拦截）。
 * @param row 目标角色
 * @returns 无
 */
async function unlockVoice(row: CharacterAsset): Promise<void> {
  if (voiceLockBusyIds.value.includes(row.id)) return
  voiceLockBusyIds.value = [...voiceLockBusyIds.value, row.id]
  try {
    const updated = await studioApi.voiceUnlock(projectId.value, row.id)
    const idx = characters.value.findIndex((c) => c.id === row.id)
    if (idx >= 0) characters.value[idx] = updated
    ElMessage.success(`角色「${row.name}」音色已解锁`)
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    voiceLockBusyIds.value = voiceLockBusyIds.value.filter((i) => i !== row.id)
  }
}

/** 配音表行（一段对白一行独立生成；整镜预览行 segIdx=null） */
interface TtsRow {
  /** 行唯一 key（段：`${sb.id}:${segIdx}`；整镜预览：`${sb.id}:full`） */
  key: string
  /** 所属分镜 */
  sb: Storyboard
  /** 对白段下标（null = 整镜预览行，仅试听拼接产物） */
  segIdx: number | null
  /** 说话人名（null = 旁白/整镜行） */
  speaker: string | null
  /** 台词/说明文本 */
  text: string
  /** 该行音频产物（段产物缺失或对白已变更时为 null） */
  audioUrl: string | null
  /** 实际时长（秒，段产物回写值） */
  durationSec: number | null
}

/** 配音表数据：有对白分镜逐段拆行（一个角色一句话一行）+ 每镜末尾整镜预览行 */
const ttsRows = computed<TtsRow[]>(() => {
  const rows: TtsRow[] = []
  for (const b of dialogueBoards.value) {
    const products = b.audio?.dialogue ?? []
    dialogueSegments(b).forEach((seg, i) => {
      const p = products[i]
      // 产物需与当前对白行文本匹配才展示（对白编辑后旧段产物作废）
      const matched = !!p && p.text === seg.text
      rows.push({
        key: `${b.id}:${i}`,
        sb: b,
        segIdx: i,
        speaker: seg.speaker,
        text: seg.text,
        audioUrl: matched ? p!.audio_url : null,
        durationSec: matched ? (p!.duration_sec ?? null) : null,
      })
    })
    rows.push({
      key: `${b.id}:full`,
      sb: b,
      segIdx: null,
      speaker: null,
      text: '整镜配音（全部台词段生成后自动拼接，供视频/合成阶段使用）',
      audioUrl: b.audio_url ?? null,
      durationSec: b.actual_duration ?? null,
    })
  }
  return rows
})

/** 段级配音提交请求中的行 key 集合（防重复点击） */
const ttsSegBusy = ref<string[]>([])
/** 段级配音任务执行中的行 key 集合（提交后到任务终态，驱动行内动态加载样式） */
const ttsSegGenerating = ref<string[]>([])

/**
 * 判断某配音表行是否在生成中（提交请求或任务轮询阶段）。
 * @param r 目标行
 * @returns 是否生成中
 */
function isRowBusy(r: TtsRow): boolean {
  return r.segIdx !== null
    && (ttsSegBusy.value.includes(r.key) || ttsSegGenerating.value.includes(r.key))
}

/**
 * 判断某段说话人音色是否未锁定（行级门禁，与后端段级门禁同口径）。
 * @param speaker 说话人名（null = 旁白段，走回退音色不拦截）
 * @returns 是否未锁定
 */
function isSpeakerUnlocked(speaker: string | null): boolean {
  if (!speaker) return false
  const lockedByName = new Map(characters.value.map((c) => [c.name, c.voice_locked]))
  return lockedByName.get(speaker) === false
}

/**
 * 生成单段配音（一句一行）：仅提交该对白段（segment_index），任务完成后
 * 刷新分镜（取回后端回写的段产物；全段齐时后端自动拼接整镜音轨）。
 * @param r 目标配音表行（段行）
 * @returns 无
 */
async function genTtsSeg(r: TtsRow): Promise<void> {
  if (!ensureProductionReady()) return
  if (r.segIdx === null || isRowBusy(r)) return
  if (isSpeakerUnlocked(r.speaker)) {
    ElMessage.warning(`角色「${r.speaker}」音色未确认，请先在音色映射中试音并锁定音色`)
    return
  }
  ttsSegBusy.value = [...ttsSegBusy.value, r.key]
  try {
    // 音色不显式指定——后端按该段说话人角色的锁定音色自动路由
    const task = await productionApi.tts(projectId.value, r.sb.id, { segment_index: r.segIdx })
    ElMessage.success(`配音任务 #${task.id} 已提交`)
    ttsSegBusy.value = ttsSegBusy.value.filter((k) => k !== r.key)
    ttsSegGenerating.value = [...ttsSegGenerating.value, r.key]
    startTaskStream()
    void refreshTasks()
    void refreshWallet()
    // 轮询该任务直到终态；完成后刷新分镜列表以取回段级产物
    const timer = setInterval(async () => {
      try {
        const t = await productionApi.taskDetail(task.id)
        if (t.status === 'done') {
          clearInterval(timer)
          boards.value = await studioApi.listStoryboard(
            projectId.value,
            currentEpisodeId.value ?? undefined,
          )
          ElMessage.success(`#${String(r.sb.sequence).padStart(2, '0')} 镜「${r.speaker || '旁白'}」配音完成`)
        } else if (t.status === 'failed' || t.status === 'canceled') {
          clearInterval(timer)
        } else {
          return
        }
        ttsSegGenerating.value = ttsSegGenerating.value.filter((k) => k !== r.key)
      } catch {
        clearInterval(timer)
        ttsSegGenerating.value = ttsSegGenerating.value.filter((k) => k !== r.key)
      }
    }, 2000)
  } finally {
    ttsSegBusy.value = ttsSegBusy.value.filter((k) => k !== r.key)
  }
}

/** 批量生成全部配音进行中（防重复点击） */
const batchTtsBusy = ref(false)

/**
 * 批量生成全部配音：对所有「未生成音频」的对白段逐段提交 TTS 任务
 * （不等终态，任务在右侧任务面板跟踪；说话人音色未锁定的段跳过并提示）。
 * @returns 无
 */
async function batchGenTts(): Promise<void> {
  if (!ensureProductionReady()) return
  if (batchTtsBusy.value) return
  const pending = ttsRows.value.filter((r) => r.segIdx !== null && !r.audioUrl)
  if (!pending.length) {
    ElMessage.info('所有对白段均已配音')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将为 ${pending.length} 个未配音台词段逐段生成配音（每段独立音色，按字数计费），全部段完成后自动拼接整镜音轨。是否继续？`,
      '生成全部配音',
      { type: 'info', confirmButtonText: '开始生成' },
    )
  } catch {
    return
  }
  batchTtsBusy.value = true
  try {
    let submitted = 0
    const skipped: string[] = []
    for (const r of pending) {
      if (isSpeakerUnlocked(r.speaker)) {
        skipped.push(`#${String(r.sb.sequence).padStart(2, '0')}（${r.speaker || '旁白'}）`)
        continue
      }
      try {
        await productionApi.tts(projectId.value, r.sb.id, { segment_index: r.segIdx! })
        ttsSegGenerating.value = [...ttsSegGenerating.value, r.key]
        submitted += 1
      } catch {
        /* 单段失败（如 409 防重）不阻断其余段 */
      }
    }
    if (submitted) {
      ElMessage.success(`已提交 ${submitted} 个段级配音任务`)
      panelVisible.value = true
      startTaskStream()
      void refreshTasks()
      void refreshWallet()
    }
    if (skipped.length) {
      ElMessage.warning(`以下台词段因音色未锁定跳过：${skipped.join('、')}`)
    }
  } finally {
    batchTtsBusy.value = false
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
/** 字幕样式（字号/颜色/位置/双语，颜色为 #RRGGBB，提交时转 ASS 格式；字号为画面真实像素） */
const subtitleStyle = reactive({
  font_size: 70,
  color: '#FFFFFF',
  position: 'bottom' as 'bottom' | 'middle' | 'top',
  bilingual: false,
})

/**
 * 项目画幅是否竖屏（内部计算属性）。
 *
 * 依据项目 aspect_ratio：仅 '9:16' 视为竖屏（60~80px），'16:9'/'1:1'
 * 按横屏规格（40~55px）。
 *
 * Returns:
 *   boolean: 竖屏 true。
 */
const isPortraitProject = computed<boolean>(() => {
  const ar = (projectStore.currentProject as { aspect_ratio?: string } | null)?.aspect_ratio
  return ar === '9:16'
})

/**
 * 当前画幅的字号可调范围与默认值（内部计算属性，2026-10-20 用户规格）。
 *
 * Returns:
 *   { min, max, def }: 竖屏 60~80 默认 70；横屏 40~55 默认 48。
 */
const subtitleFontRange = computed<{ min: number; max: number; def: number }>(() =>
  isPortraitProject.value
    ? { min: 60, max: 80, def: 70 }
    : { min: 40, max: 55, def: 48 },
)

// 画幅切换/加载后字号越界时自动归位默认值（项目画幅在页面加载后异步到达）
watch(subtitleFontRange, (r) => {
  if (subtitleStyle.font_size < r.min || subtitleStyle.font_size > r.max) {
    subtitleStyle.font_size = r.def
  }
}, { immediate: true })

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
 * 组装本次合成的 BGM / 字幕 / 转场 / 片头片尾配置（对齐 ComposeCreateRequest 字段）。
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
  // 片头片尾卡（2026-10-21 成片整合）：片头=剧名+集数标题卡 / 片尾=剧终卡
  if (introCardEnabled.value) {
    payload.intro_card = true
    payload.outro_card = true
  }
  return payload
}

// ============================================================
// 合成（进入发布）
// ============================================================

/** 片头片尾卡开关（2026-10-21 成片整合：片头=剧名+集数标题卡，片尾=剧终卡，缺省开启） */
const introCardEnabled = ref(true)

/** 合成进行中 */
const composing = ref(false)
/** 合成任务进度（轮询 taskDetail） */
const composeProgress = ref(0)
/** 合成已完成镜数（后端按镜进度回传，成片整合二期） */
const composeDone = ref(0)
/** 合成参与合成的总镜数 */
const composeTotal = ref(0)

/**
 * 合成阶段文案（computed）：按后端进度区间映射当前环节，
 * 音画对齐阶段展示「第 X/Y 镜」按镜计数。
 */
const composeStage = computed(() => {
  const p = composeProgress.value
  if (p < 5) return '准备合成…'
  if (p < 10) return '分析分镜清单'
  if (p < 60) return `音画对齐（第 ${composeDone.value}/${composeTotal.value || '…'} 镜）`
  if (p < 63) return '生成片头片尾卡'
  if (p < 70) return '拼接片段'
  if (p < 76) return '转场合成'
  if (p < 82) return '准备字幕'
  if (p < 90) return '字幕 / BGM 后处理'
  if (p < 100) return '写入成片版本'
  return '即将完成'
})

/** 当前集缺视频产物的分镜（合成完整性门禁，2026-10-21） */
const missingVideoBoards = computed(() => boards.value.filter((b) => !b.video_url))

/**
 * 发起成片合成并轮询任务直到终态，完成后确认跳转发布页。
 * @returns 无
 */
async function composeFilm(): Promise<void> {
  if (!ensureProductionReady()) return
  if (composing.value) return
  // 完整性门禁（前端预防）：缺镜时弹窗引导前往视频 Tab 补齐（后端 400 兜底）
  if (missingVideoBoards.value.length) {
    const seqs = missingVideoBoards.value
      .map((b) => `#${String(b.sequence).padStart(2, '0')}`)
      .join('、')
    try {
      await ElMessageBox.confirm(
        `还有 ${missingVideoBoards.value.length} 个镜头未生成视频（${seqs}），成片会缺镜头。是否前往视频 Tab 补齐？`,
        '镜头未补齐',
        { type: 'warning', confirmButtonText: '去生成视频', cancelButtonText: '留在本页' },
      )
      activeTab.value = 'videos'
    } catch {
      /* 留在合成 Tab */
    }
    return
  }
  composing.value = true
  composeProgress.value = 0
  composeDone.value = 0
  composeTotal.value = 0
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
    // 轮询合成任务详情（进度条 + 阶段文案展示），终态后引导跳转
    for (;;) {
      await new Promise((r) => setTimeout(r, 2000))
      const t = await productionApi.taskDetail(task.id)
      composeProgress.value = t.progress
      composeDone.value = t.compose_done ?? composeDone.value
      composeTotal.value = t.compose_total ?? composeTotal.value
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
  } catch (e: unknown) {
    // 后端完整性门禁兜底（并发场景下镜头被删/预检后视频失效）：展示缺镜清单
    const detail = (e as { response?: { data?: { detail?: { code?: string; message?: string; missing?: Array<{ sequence: number }> } } } })
      ?.response?.data?.detail
    if (detail && typeof detail === 'object' && detail.code === 'videos_incomplete') {
      const seqs = (detail.missing || []).map((m) => `#${String(m.sequence).padStart(2, '0')}`).join('、')
      ElMessage.warning(`${detail.message || '镜头视频未补齐'}：${seqs}`)
      activeTab.value = 'videos'
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
  // 离开页面时停止行内试听与视频联动配音
  audioEl.pause()
  playingAudioKey.value = null
  sbAudioEls.forEach((a) => a.pause())
  sbAudioEls.clear()
  sbVideoRefs.clear()
})
</script>

<template>
  <div class="page-container prod-view">
    <!-- 生成阶段门禁：未确认分镜时提示（历史产物仍展示，新生成动作被拦截） -->
    <el-alert
      v-if="!productionConfirmed"
      type="warning"
      :closable="false"
      show-icon
      title="分镜尚未确认"
      description="请先在「分镜设计」页点击「确认分镜，进入生成」；确认前仅可查看历史生成产物，不能发起新的生成任务。"
      class="gate-alert"
    />
    <!-- 顶部：积分消耗条 + 任务面板入口 -->
    <div class="top-bar">
      <el-tabs v-model="activeTab" class="prod-tabs">
        <el-tab-pane label="关键帧" name="keyframes" />
        <el-tab-pane label="配音" name="tts" />
        <el-tab-pane label="视频" name="videos" />
        <el-tab-pane label="合成" name="compose" />
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
          <el-button type="primary" :loading="kfBusy" @click="fullKeyframes">
            全量生成（{{ boards.length }} 张 ≈ {{ boards.length * 10 }} 积分）
          </el-button>
          <span class="kf-tip">提示：可先单镜点击卡片上的「生成」确认风格，再全量批量（任务在右侧任务面板跟踪）</span>
        </div>
        <div class="kf-grid">
          <el-card
            v-for="b in boards"
            :id="`kf-card-${b.id}`"
            :key="b.id"
            shadow="hover"
            class="kf-card"
            :class="{ 'kf-card--target': highlightSbId === b.id, 'kf-card--generating': isKeyframeBusy(b) }"
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
              <!-- hover 浮层：滑过卡片浮现生成/重新生成按钮；生成中常驻显示 spinner（遮罩不拦截点击，可继续预览大图） -->
              <div class="kf-gen-overlay" :class="{ 'is-generating': isKeyframeBusy(b) }">
                <template v-if="isKeyframeBusy(b)">
                  <el-icon class="kf-gen-spin"><LoadingIcon /></el-icon>
                  <span class="kf-gen-text">生成中…</span>
                </template>
                <el-button v-else type="primary" size="small" @click="genKeyframe(b)">
                  {{ b.keyframe_url ? '重新生成' : '生成' }}
                </el-button>
              </div>
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
            <!-- 配音隔离开关：关闭时纯视频预览（不叠加配音轨，验证视频内容用） -->
            <span class="vc-label">播放时同步配音</span>
            <el-switch v-model="dubSyncEnabled" size="small" />
            <span class="vc-tip">以上参数随每次「生成视频片段」提交生效</span>
            <!-- 一键生成全部视频：逐镜提交缺视频产物且有关键帧的镜头 -->
            <el-button
              type="primary"
              size="small"
              :loading="batchVideoBusy"
              :disabled="batchVideoBusy"
              @click="batchGenVideos"
            >
              一键生成全部视频
            </el-button>
          </div>
        </el-card>
        <div class="video-rows">
          <el-card
            v-for="b in boards"
            :key="b.id"
            shadow="never"
            class="video-row"
            :class="{ 'video-row-generating': isVideoBusy(b) }"
          >
            <div class="video-row-body">
              <div class="video-play">
                <video
                  v-if="b.video_url && !b.video_url.endsWith('.svg')"
                  :ref="(el) => setVideoRef(b.id, el)"
                  :src="b.video_url"
                  controls
                  class="video-player"
                  @play="onVideoPlay(b)"
                  @pause="onVideoPause(b)"
                  @ended="onVideoPause(b)"
                  @seeked="onVideoSeek(b)"
                />
                <div v-else class="video-player video-player--empty">
                  <span>视频片段{{ b.video_url ? '为占位图（合成后可播）' : '待生成' }}</span>
                </div>
              </div>
              <div class="video-side">
                <div class="video-title">#{{ String(b.sequence).padStart(2, '0') }} {{ b.shot_type || '' }} · {{ b.duration }}s</div>
                <div class="video-desc">{{ b.visual_description || '（无画面描述）' }}</div>
                <!-- 音频先行引导：视频内嵌配音 / 未配音提示（视频时长将跟随配音实际时长） -->
                <div class="dub-line">
                  <!-- 视频已内嵌当前配音（混流产物）：播放器直接出声 -->
                  <el-tag v-if="b.video_url && b.audio?.video_dubbed" size="small" type="success" effect="plain">
                    视频已接入配音（环境音+配音）
                  </el-tag>
                  <!-- 有配音但视频未接入 / 内嵌配音已过期：提示重新生成视频 -->
                  <el-tag
                    v-else-if="b.audio_url && (b.dialogue || '').trim() && b.video_url"
                    size="small" type="warning" effect="plain"
                  >
                    配音已更新 · 重新生成视频可接入
                  </el-tag>
                  <el-tag v-else-if="b.audio_url && (b.dialogue || '').trim()" size="small" :type="dubSyncEnabled ? 'success' : 'info'" effect="plain">
                    已配音{{ dubSyncEnabled ? ' · 播放时同步接入' : ' · 纯视频预览（同步配音已关）' }}
                  </el-tag>
                  <el-tag v-else size="small" type="warning" effect="plain">未配音</el-tag>
                  <!-- 分镜级字幕：对白已按配音段时间轴烧进画面（合成阶段自动跳过防双字幕） -->
                  <el-tag v-if="b.audio?.burned_subtitles" size="small" type="info" effect="plain">已烧字幕</el-tag>
                  <span v-if="!(b.audio_url && (b.dialogue || '').trim())" class="dub-tip">建议先到「配音」Tab 生成配音，视频时长将自动对齐配音</span>
                </div>
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
                <!-- 生成视频：动态加载（按钮 loading + 合成中 tag + 行卡脉冲） -->
                <div class="video-btn-line">
                  <el-button
                    type="primary"
                    plain
                    size="small"
                    :loading="isVideoBusy(b)"
                    :disabled="isVideoBusy(b)"
                    @click="genVideo(b)"
                  >
                    {{ isVideoBusy(b) ? '生成中…' : (b.video_url ? '重新生成视频片段' : '生成视频片段') }}
                  </el-button>
                  <el-tag v-if="isVideoBusy(b)" size="small" type="warning" effect="light" class="tts-none-tag">
                    <el-icon :size="12" class="tts-spin"><LoadingIcon /></el-icon>
                    合成中
                  </el-tag>
                </div>
              </div>
            </div>
          </el-card>
        </div>
      </template>

      <!-- ============ 配音 Tab（S-08 前半：音色映射 + 分镜配音） ============ -->
      <template v-else-if="activeTab === 'tts'">
        <!-- 角色音色映射（试音 → 锁定 → 门禁分镜配音） -->
        <el-card shadow="never" class="block-card">
          <template #header>
            <div class="voice-card-head">
              <span>角色音色映射</span>
              <span class="voice-card-tip">按角色设定试音 → 确认音色并锁定后，方可生成该角色的分镜配音</span>
            </div>
          </template>
          <el-empty
            v-if="!characters.length"
            description="暂无角色资产"
            :image-size="60"
          />
          <el-table v-else :data="characters" size="small">
            <el-table-column prop="name" label="角色" width="120" />
            <el-table-column label="音色设定" min-width="180">
              <template #default="{ row }">
                <span class="voice-note-text">{{ row.voice_note || '（角色设定未提供音色描述）' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="试音" width="200">
              <template #default="{ row }">
                <el-button
                  size="small"
                  :loading="voicePreviewBusyIds.includes(row.id)"
                  :disabled="row.voice_locked"
                  @click="previewVoice(row)"
                >
                  {{ row.voice_sample_url ? '重新试音' : '生成试音' }}
                </el-button>
                <el-button
                  v-if="row.voice_sample_url"
                  link
                  type="primary"
                  :title="playingVoiceId === row.id ? '暂停试音' : '播放试音'"
                  @click="playVoiceSample(row)"
                >
                  <el-icon :size="18">
                    <VideoPause v-if="playingVoiceId === row.id" />
                    <VideoPlay v-else />
                  </el-icon>
                </el-button>
              </template>
            </el-table-column>
            <el-table-column label="确认" width="150">
              <template #default="{ row }">
                <template v-if="row.voice_locked">
                  <el-tag size="small" type="success" effect="plain">已锁定</el-tag>
                  <el-button
                    link
                    size="small"
                    :loading="voiceLockBusyIds.includes(row.id)"
                    @click="unlockVoice(row)"
                  >
                    解锁
                  </el-button>
                </template>
                <el-button
                  v-else
                  size="small"
                  type="primary"
                  plain
                  :loading="voiceLockBusyIds.includes(row.id)"
                  @click="lockVoice(row)"
                >
                  锁定音色
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <!-- 分镜配音（一段对白一行：一个角色一句话独立生成与试听，全段齐后自动拼接整镜） -->
        <el-card shadow="never" class="block-card">
          <template #header>
            <div class="voice-card-head">
              <span>分镜配音</span>
              <span class="voice-card-tip">
                一句一行独立配音（共 {{ ttsRows.filter((r) => r.segIdx !== null).length }} 段台词，
                {{ dialogueBoards.length }}/{{ boards.length }} 镜有对白）；全段生成后自动拼接整镜音轨
              </span>
              <el-button
                size="small"
                type="primary"
                class="batch-tts-btn"
                :loading="batchTtsBusy"
                :disabled="!ttsRows.some((r) => r.segIdx !== null && !r.audioUrl)"
                @click="batchGenTts"
              >
                生成全部配音
              </el-button>
            </div>
          </template>
          <el-table
            :data="ttsRows"
            size="small"
            :row-class-name="(ctx: { row: TtsRow }) => (isRowBusy(ctx.row) ? 'tts-row-generating' : '')"
          >
            <el-table-column label="镜号" width="80">
              <template #default="{ row }">#{{ String(row.sb.sequence).padStart(2, '0') }}</template>
            </el-table-column>
            <el-table-column label="说话人" width="110">
              <template #default="{ row }">
                <span v-if="row.segIdx === null" class="tts-full-label">整镜</span>
                <span v-else-if="row.speaker" class="dlg-speaker">{{ row.speaker }}</span>
                <span v-else class="tts-narrator-label">旁白</span>
              </template>
            </el-table-column>
            <el-table-column label="台词" show-overflow-tooltip>
              <template #default="{ row }">
                <span :class="{ 'tts-full-text': row.segIdx === null }">{{ row.text }}</span>
              </template>
            </el-table-column>
            <el-table-column label="时长" width="90">
              <template #default="{ row }">
                {{ row.durationSec ? `${row.durationSec}s` : (row.segIdx === null ? `${row.sb.duration}s` : '—') }}
              </template>
            </el-table-column>
            <el-table-column label="操作" width="230">
              <template #default="{ row }">
                <!-- 整镜预览行：仅试听拼接产物（各段生成后由后端自动拼接） -->
                <template v-if="row.segIdx === null">
                  <el-button
                    v-if="row.audioUrl"
                    link
                    type="primary"
                    class="tts-play-btn"
                    :title="playingAudioKey === row.key ? '暂停试听' : '播放整镜配音'"
                    @click="togglePlayUrl(row.audioUrl, row.key)"
                  >
                    <el-icon :size="18">
                      <VideoPause v-if="playingAudioKey === row.key" />
                      <VideoPlay v-else />
                    </el-icon>
                  </el-button>
                  <el-tag v-else size="small" type="info" effect="plain" class="tts-none-tag">
                    各段齐后自动拼接
                  </el-tag>
                </template>
                <!-- 台词段行：独立生成/重新配音 + 段级试听 -->
                <template v-else>
                  <el-button
                    size="small"
                    type="primary"
                    plain
                    :loading="isRowBusy(row)"
                    :disabled="isRowBusy(row) || isSpeakerUnlocked(row.speaker)"
                    :title="isSpeakerUnlocked(row.speaker)
                      ? `角色「${row.speaker}」音色未锁定，请先在上方映射中确认`
                      : '生成该段配音'"
                    @click="genTtsSeg(row)"
                  >
                    {{ isRowBusy(row) ? '生成中…' : (row.audioUrl ? '重新配音' : '生成配音') }}
                  </el-button>
                  <!-- 生成中：动态加载 tag；已配音：段级播放/暂停试听图标；未配音：未生成占位 -->
                  <el-tag v-if="isRowBusy(row)" size="small" type="warning" effect="light" class="tts-none-tag">
                    <el-icon :size="12" class="tts-spin"><LoadingIcon /></el-icon>
                    合成中
                  </el-tag>
                  <el-button
                    v-else-if="row.audioUrl"
                    link
                    type="primary"
                    class="tts-play-btn"
                    :title="playingAudioKey === row.key ? '暂停试听' : `播放「${row.speaker || '旁白'}」的配音`"
                    @click="togglePlayUrl(row.audioUrl, row.key)"
                  >
                    <el-icon :size="18">
                      <VideoPause v-if="playingAudioKey === row.key" />
                      <VideoPlay v-else />
                    </el-icon>
                  </el-button>
                  <el-tag v-else size="small" type="info" effect="plain" class="tts-none-tag">
                    未生成
                  </el-tag>
                </template>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </template>

      <!-- ============ 合成 Tab（S-08 后半：BGM / 字幕 / 转场） ============ -->
      <template v-else-if="activeTab === 'compose'">
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
                <span class="mix-inline-label">字号(px)</span>
                <el-input-number
                  v-model="subtitleStyle.font_size"
                  :min="subtitleFontRange.min"
                  :max="subtitleFontRange.max"
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
                <!-- 分镜级字幕说明：已烧字幕的镜不重复烧录 -->
                <span class="mix-inline-label" style="margin-left: 12px; color: var(--el-text-color-secondary)">
                  已烧字幕的分镜不再重复烧录
                </span>
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
            <!-- 片头片尾卡（2026-10-21 成片整合）：剧名+集数标题卡 / 剧终卡 -->
            <el-form-item label="片头片尾">
              <el-switch v-model="introCardEnabled" />
              <span class="mix-inline-label" style="margin-left: 12px; color: var(--el-text-color-secondary)">
                片头「剧名 + 第 N 集」标题卡（2.5s）· 片尾「剧终」卡（2s），自动淡入淡出
              </span>
            </el-form-item>
          </el-form>
          <div class="mix-note">以上配置在点击「全部完成，进入合成」时随合成任务提交生效</div>
        </el-card>
        <!-- 合成完整性门禁：缺视频的镜清单（补齐后才能合成完整一集） -->
        <el-card v-if="missingVideoBoards.length" shadow="never" class="block-card missing-card">
          <template #header>
            <div class="voice-card-head">
              <span>镜头完成度 {{ boards.length - missingVideoBoards.length }}/{{ boards.length }}</span>
              <span class="voice-card-tip">以下镜头尚未生成视频，补齐后才能合成本集成片</span>
            </div>
          </template>
          <div class="missing-tags">
            <el-tag
              v-for="b in missingVideoBoards"
              :key="b.id"
              type="warning"
              effect="plain"
              class="missing-tag"
            >
              #{{ String(b.sequence).padStart(2, '0') }} {{ (b.visual_description || '').slice(0, 14) }}
            </el-tag>
          </div>
        </el-card>
      </template>

      <!-- 合成进度 + 底部按钮 -->
      <div class="footer-bar">
        <el-progress
          v-if="composing"
          :percentage="composeProgress"
          class="compose-progress"
        />
        <span v-if="composing" class="compose-stage">{{ composeStage }}</span>
        <span v-else class="footer-tip">全部镜头关键帧 / 配音 / 视频完成后即可合成成片</span>
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
.gate-alert {
  border-radius: var(--mf-r-ctl);
}
.top-bar {
  /* 吸顶：Tab 行固定在滚动容器顶部，不随页面滚动（2026-10-21 用户确认）；
     负水平 margin 抵消 page-container 左右内边距使背景铺满整行，
     studio-layout header 在滚动容器外不会被遮挡 */
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-4);
  background: var(--mf-bg);
  padding: var(--mf-sp-2) var(--mf-sp-8);
  margin: 0 calc(-1 * var(--mf-sp-8));
  border-bottom: 1px solid var(--mf-border);
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
  cursor: default;
  user-select: none;
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
/* 单镜生成 hover 浮层：滑过卡片浮现，遮罩不拦截点击（保留图片预览） */
.kf-gen-overlay {
  position: absolute;
  inset: 0;
  z-index: 2;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--mf-sp-2);
  background: rgba(0, 0, 0, 0.32);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.12s ease;
}
.kf-card:hover .kf-gen-overlay {
  opacity: 1;
}
/* 浮层整体穿透（保留图片预览），仅按钮恢复可点击 */
.kf-gen-overlay .el-button {
  pointer-events: auto;
}
/* 生成中：浮层常驻，spinner + 文案替代按钮 */
.kf-gen-overlay.is-generating {
  opacity: 1;
}
.kf-gen-spin {
  color: var(--mf-brand);
  font-size: 28px;
  animation: kf-spin 1s linear infinite;
}
.kf-gen-text {
  color: #fff;
  font-size: var(--mf-fs-sm);
  text-shadow: 0 1px 4px rgba(0, 0, 0, 0.5);
}
@keyframes kf-spin {
  to {
    transform: rotate(360deg);
  }
}
/* 生成中扫光：斜向高光带循环扫过图片区域（不拦截点击） */
.kf-card--generating .kf-img-wrap::after {
  content: '';
  position: absolute;
  inset: 0;
  z-index: 1;
  pointer-events: none;
  background: linear-gradient(
    105deg,
    transparent 38%,
    rgba(255, 255, 255, 0.16) 50%,
    transparent 62%
  );
  transform: translateX(-100%);
  animation: kf-shimmer 1.6s ease-in-out infinite;
}
@keyframes kf-shimmer {
  to {
    transform: translateX(100%);
  }
}
@media (prefers-reduced-motion: reduce) {
  .kf-card--generating .kf-img-wrap::after {
    animation: none;
  }
  .kf-gen-spin {
    animation-duration: 2.4s;
  }
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
/* 音频先行引导行（视频 Tab 每镜配音状态） */
.dub-line {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  flex-wrap: wrap;
}
.dub-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
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
/* 合成完整性门禁：缺视频镜头清单卡（2026-10-21 成片整合） */
.missing-tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--mf-sp-2);
}
.missing-tag {
  max-width: 100%;
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
  /* 关键帧缩略图已移除（2026-10-21 用户确认）：播放器 + 信息区两列布局 */
  grid-template-columns: 1fr 240px;
  gap: var(--mf-sp-4);
  align-items: center;
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
/* 音色映射卡头部说明与音色设定文本 */
.voice-card-head {
  display: flex;
  align-items: baseline;
  gap: var(--mf-sp-3);
  flex-wrap: wrap;
}
.batch-tts-btn {
  margin-left: auto;
}
.voice-card-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.voice-note-text {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-xs);
}
/* 对白列角色名高亮（说话人前缀） */
.dlg-speaker {
  color: var(--mf-brand);
  font-weight: 500;
}
/* 配音生成中：行背景呼吸脉冲 + 合成中 tag 旋转图标 */
.el-table .tts-row-generating td {
  animation: tts-row-pulse 1.6s ease-in-out infinite;
}
@keyframes tts-row-pulse {
  0%, 100% {
    background: transparent;
  }
  50% {
    background: rgba(64, 158, 255, 0.09);
  }
}
.tts-spin {
  animation: tts-spin 1s linear infinite;
  margin-right: 4px;
  vertical-align: -2px;
}
@keyframes tts-spin {
  to {
    transform: rotate(360deg);
  }
}
@media (prefers-reduced-motion: reduce) {
  .el-table .tts-row-generating td,
  .el-card.video-row-generating {
    animation: none;
  }
}
/* 视频行生成中：卡片背景呼吸脉冲（复用配音行同款节奏）+ 按钮行横排 */
.el-card.video-row-generating {
  animation: tts-row-pulse 1.6s ease-in-out infinite;
}
.video-btn-line {
  display: flex;
  align-items: center;
}
.tts-none-tag {
  margin-left: 8px;
}
/* 行内配音试听图标（播放/暂停切换） */
.tts-play-btn {
  margin-left: 8px;
  vertical-align: middle;
}
/* 整镜预览行（拼接产物）：说明文字弱化展示 */
.tts-full-label,
.tts-narrator-label {
  color: var(--mf-text-3, #909399);
  font-size: var(--mf-fs-xs, 12px);
}
.tts-full-text {
  color: var(--mf-text-3, #909399);
  font-style: italic;
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
/* 合成阶段文案（成片整合二期）：footer-bar 行内展示当前环节与按镜计数 */
.compose-stage {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
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
