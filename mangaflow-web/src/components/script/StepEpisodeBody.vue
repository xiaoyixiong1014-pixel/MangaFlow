<!--
  剧本工作台 · 卡片③ 剧本正文内容区（文件级注释，P-1 范式级重构 Task 11；
  redesign-script-workspace Task 4：漫剧格式视图 + 场景卡迁入，作为 ScriptBodyCard 内部实现）。

  布局：上方集卡横向滑动列表 + 下方长文编辑器（原右侧 AI 优化 / 体检面板已删除，
  其能力后续迁入容器右侧 AI 助手面板实现）。

  - 集卡列表：读 episodes/plan 渲染集（序号/标题/时长/正文状态标记），
    「生成本集正文 / 重新生成本集」按集触发 SSE（正文状态取自 episodes.status
    === 'scripted'，避免对未生成集请求剧本行触发 404 弹窗）；
  - 编辑区：未生成态空态 + 开始生成按钮；生成中 SSE 流式渲染（delta 追加 + 自动
    滚动 + status 进度提示）；已生成态三视图 Tab——「文章体」（raw_text 可编辑
    长文）、「场次结构」（scenes 卡片，可编辑 plot/action/dialogue.line；原
    StepIdea「场景卡列表」迁入即为此卡片化呈现，不另建重复展示区）与「漫剧格式」
    （Task 4 新增：utils/manga-format 渲染的漫剧规范文本只读视图 + 一键复制）；
    生成完成后默认切文章体视图，不残留原始 JSON；
  - 工具栏：保存（PUT body，base_version=当前 version；保存成功后自动触发整剧
    分镜拆解 SB-01 decompose，把场次结构与漫剧格式传给下游资产/分镜，失败不打断
    保存，可稍后在分镜页重拆）、重生成本集、导出 TXT / Fountain（blob 下载，
    文件名取 Content-Disposition）；原「确认剧本，生成资产」弹窗出口已删除——
    下游传递合并进保存动作（投递画布可到分镜页操作）；
  - 断线恢复：进入时调 pipeline/status，存在待确认闸口或断点阶段时提示
    「上次生成未完成」并提供流水线状态摘要（不自研轮询检查点）。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowDown, CopyDocument, Loading } from '@element-plus/icons-vue'
import * as pipelineApi from '@/api/scriptPipeline'
import type {
  EpisodeBodyContent,
  EpisodeBodySavePayload,
  EpisodePlanItem,
  ScriptExportFormat,
  ScriptPhase,
} from '@/api/scriptPipeline'
import { renderMangaScene, renderMangaScript } from '@/utils/manga-format'
import * as episodesApi from '@/api/episodes'
import * as studioApi from '@/api/studio'
import QuotableText from './QuotableText.vue'

/** 组件入参 */
const props = defineProps<{
  /** 当前项目 ID（正文/优化/体检等接口路径参数） */
  projectId: number
  /** 当前剧本创作阶段（body=大纲已确认；非 body 时给出引导提示） */
  phase?: ScriptPhase | null
}>()

/** 组件事件 */
const emit = defineEmits<{
  /** 选中集变化（redesign-script-workspace Task 6：容器据此计算助手面板聚焦对象） */
  'focus-changed': [info: { episodeId: number | null; sequence: number | null }]
}>()

// ============================================================
// 左集列表：分集大纲 + 各集正文状态
// ============================================================

/** 首屏加载中（骨架） */
const loading = ref(false)
/** 分集大纲列表（sequence 升序） */
const episodes = ref<EpisodePlanItem[]>([])
/** 各集执行状态映射（episode_id → episodes.status；'scripted' 表示已有正文） */
const statusMap = ref<Record<number, string>>({})
/** 当前选中集 ID（null=未选中） */
const selectedId = ref<number | null>(null)

/** 当前选中集条目 */
const selectedEpisode = computed(
  () => episodes.value.find((e) => e.episode_id === selectedId.value) ?? null,
)

/** 选中文本引用的来源标签前缀（正文编辑区「添加对话中」用） */
const bodyQuoteSource = computed(
  () => `剧本正文 · 第 ${selectedEpisode.value?.sequence ?? '?'} 集`)

/**
 * 判断某集是否已有正文（episodes.status 为 scripted）。
 * @param ep 目标集条目
 * @returns 是否已有正文
 */
function hasBody(ep: EpisodePlanItem): boolean {
  return statusMap.value[ep.episode_id] === 'scripted'
}

/**
 * 拉取分集大纲与各集状态（状态接口失败时降级为空，不影响大纲展示）。
 * @returns 无
 */
async function loadPlan(): Promise<void> {
  loading.value = true
  try {
    const plan = await pipelineApi.getEpisodePlan(props.projectId)
    episodes.value = plan.episodes
    try {
      const eps = await episodesApi.list(props.projectId)
      statusMap.value = Object.fromEntries(eps.map((e) => [e.id, e.status]))
    } catch {
      statusMap.value = {}
    }
    if (!episodes.value.some((e) => e.episode_id === selectedId.value)) {
      selectedId.value = episodes.value[0]?.episode_id ?? null
    }
  } finally {
    loading.value = false
  }
}

// ============================================================
// 正文加载与解包（兼容字段级包装 / 整体包装落库形态）
// ============================================================

/** 正文编辑中的场次草稿（字段统一归一为字符串，便于表单双向绑定） */
interface SceneDraft {
  /** 场景序号（≥1） */
  scene_no: number
  /** 场景标题 */
  title: string
  /** 场景剧情描述 */
  plot: string
  /** 动作/画面描述 */
  action: string
  /** 地点名 */
  location: string
  /** 时段 */
  time_of_day: string
  /** 内外景 */
  int_ext: string
  /** 场次主导情绪 */
  emotion: string
  /** 场景时长（秒，可空） */
  duration_sec: number | null
  /** 对白列表（type 缺省「对白」；emotion 供漫剧格式渲染情绪标注） */
  dialogue: Array<{ character: string; line: string; type: string; emotion: string }>
  /** 场次级来源标记（ai/user） */
  source?: string
}

/** 正文加载中 */
const bodyLoading = ref(false)
/** 当前集是否已有正文（决定空态/编辑态） */
const bodyLoaded = ref(false)
/** 文章体正文文本（编辑区双向绑定） */
const rawText = ref('')
/** 单集标题（结构化 content 顶层） */
const bodyTitle = ref('')
/** 单集一句话梗概（结构化 content 顶层） */
const bodyLogline = ref('')
/** 去重地点清单（结构化 content 顶层） */
const bodyLocations = ref<pipelineApi.EpisodeBodyLocation[]>([])
/** 结构化场次草稿列表 */
const scenes = ref<SceneDraft[]>([])
/** 当前正文版本号（保存/应用的乐观锁基准） */
const currentVersion = ref<number | null>(null)
/** 中间视图：article 文章体 / scenes 场次结构 */
const activeView = ref<string>('article')

/**
 * 字段级解包：{"value":x,"source":…} 包装 → x，其余原样返回。
 * @param v 任意字段值
 * @returns 解包后的值
 */
function unwrapField(v: unknown): unknown {
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    const rec = v as Record<string, unknown>
    if ('value' in rec && 'source' in rec) return rec.value
  }
  return v
}

/**
 * 把后端 content 统一解包为纯结构 dict。
 * 兼容整体包装（手动保存）/ 字段级包装（AI 生成）/ 自由结构。
 * @param content 单集正文 content JSONB
 * @returns 解包后的纯结构（非法输入返回空对象）
 */
function unpackContent(content: unknown): Record<string, unknown> {
  if (!content || typeof content !== 'object' || Array.isArray(content)) return {}
  const c = content as Record<string, unknown>
  if (typeof c.value === 'object' && c.value !== null && !Array.isArray(c.value) && 'source' in c) {
    return c.value as Record<string, unknown>
  }
  const plain: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(c)) plain[k] = unwrapField(v)
  return plain
}

/**
 * 任意值归一为字符串（null/undefined → ''）。
 * @param v 任意值
 * @returns 字符串
 */
function toStr(v: unknown): string {
  return v == null ? '' : String(v)
}

/**
 * 解析结构化 content 为可编辑的结构（标题/梗概/地点/场次草稿）。
 * @param content 单集正文 content JSONB
 * @returns 解析结果
 */
function normalizeBodyContent(content: unknown): {
  title: string
  logline: string
  locations: pipelineApi.EpisodeBodyLocation[]
  scenes: SceneDraft[]
} {
  const plain = unpackContent(content)
  const rawScenes = Array.isArray(plain.scenes) ? plain.scenes : []
  const rawLocations = Array.isArray(plain.locations) ? plain.locations : []
  return {
    title: toStr(plain.title),
    logline: toStr(plain.logline),
    locations: rawLocations.map((l) => {
      const r = (l ?? {}) as Record<string, unknown>
      return {
        name: toStr(r.name),
        description: r.description != null ? toStr(r.description) : null,
        time_of_day: r.time_of_day != null ? toStr(r.time_of_day) : null,
      }
    }),
    scenes: rawScenes.map((s, i) => {
      const r = (s ?? {}) as Record<string, unknown>
      const dl = Array.isArray(r.dialogue) ? r.dialogue : []
      const dur = Number(r.duration_sec)
      return {
        scene_no: Number(r.scene_no ?? i + 1) || i + 1,
        title: toStr(r.title) || `场景 ${i + 1}`,
        plot: toStr(r.plot),
        action: toStr(r.action),
        location: toStr(r.location),
        time_of_day: toStr(r.time_of_day),
        int_ext: toStr(r.int_ext),
        emotion: toStr(r.emotion),
        duration_sec: r.duration_sec != null && !Number.isNaN(dur) ? dur : null,
        dialogue: dl.map((d) => {
          const dr = (d ?? {}) as Record<string, unknown>
          return {
            character: toStr(dr.character),
            line: toStr(dr.line),
            type: toStr(dr.type) || '对白',
            emotion: toStr(dr.emotion),
          }
        }),
        source: typeof r.source === 'string' ? r.source : undefined,
      }
    }),
  }
}

/**
 * 把加载结果写入正文编辑状态（生成完成/保存/应用后统一调用）。
 * @param raw 文章体正文文本
 * @param content 结构化 content
 * @param version 版本号
 * @returns 无
 */
function applyBody(raw: string, content: unknown, version: number): void {
  const parsed = normalizeBodyContent(content)
  rawText.value = raw
  bodyTitle.value = parsed.title
  bodyLogline.value = parsed.logline
  bodyLocations.value = parsed.locations
  scenes.value = parsed.scenes
  currentVersion.value = version
  bodyLoaded.value = raw.trim() !== '' || parsed.scenes.length > 0
  activeView.value = 'article'
}

/**
 * 按当前 scenes 草稿构造提交给后端的结构化 content。
 * @returns 结构化正文（空字段省略）
 */
function buildContent(): EpisodeBodyContent {
  return {
    title: bodyTitle.value || undefined,
    logline: bodyLogline.value || undefined,
    locations: bodyLocations.value.length ? bodyLocations.value : undefined,
    scenes: scenes.value.map((s) => ({
      scene_no: s.scene_no,
      title: s.title,
      plot: s.plot,
      action: s.action || undefined,
      location: s.location || undefined,
      time_of_day: s.time_of_day || undefined,
      int_ext: s.int_ext || undefined,
      emotion: s.emotion || undefined,
      duration_sec: s.duration_sec ?? undefined,
      dialogue: s.dialogue.map((d) => ({
        character: d.character,
        line: d.line,
        type: d.type || undefined,
        emotion: d.emotion || undefined,
      })),
    })),
  }
}

// ============================================================
// 漫剧格式视图（Task 4：utils/manga-format 只读渲染 + 一键复制）
// ============================================================

/** 漫剧格式整集文本（只读视图与复制共用，随场次草稿实时刷新） */
const mangaText = computed(() => renderMangaScript(scenes.value))

/**
 * 复制漫剧格式整集文本到剪贴板（Clipboard API；无内容时提示不执行）。
 * @returns 无
 */
async function copyMangaText(): Promise<void> {
  if (!mangaText.value.trim()) {
    ElMessage.warning('暂无场次内容可复制')
    return
  }
  try {
    await navigator.clipboard.writeText(mangaText.value)
    ElMessage.success('漫剧格式文本已复制到剪贴板')
  } catch {
    ElMessage.error('复制失败，请手动选择文本复制')
  }
}

/** 重置正文编辑状态（切换集时清空，避免残留上一集内容） */
function resetBodyState(): void {
  rawText.value = ''
  bodyTitle.value = ''
  bodyLogline.value = ''
  bodyLocations.value = []
  scenes.value = []
  currentVersion.value = null
  bodyLoaded.value = false
  activeView.value = 'article'
  genError.value = ''
  streamText.value = ''
  statusMsg.value = ''
}

/**
 * 加载选中集的正文（未生成集直接置空态，不请求剧本行以免 404 弹窗）。
 * @param episodeId 剧集 ID
 * @returns 无
 */
async function loadBody(episodeId: number): Promise<void> {
  resetBodyState()
  if (statusMap.value[episodeId] !== 'scripted') return
  bodyLoading.value = true
  try {
    const script = await studioApi.getInput(props.projectId, episodeId)
    applyBody(script.raw_text ?? '', script.content, script.version)
  } catch {
    // 剧本行缺失（404）：保持空态，业务错误已由 client 拦截器统一提示
  } finally {
    bodyLoading.value = false
  }
}

// 切换选中集：生成中先中止当前流，再加载新集正文；同时上报选中集（Task 6 聚焦联动）
watch(selectedId, (id) => {
  emit('focus-changed', {
    episodeId: id,
    sequence: episodes.value.find((e) => e.episode_id === id)?.sequence ?? null,
  })
  if (id == null) return
  if (generating.value) abortStream()
  void loadBody(id)
})

/**
 * 重拉分集列表与当前选中集正文（Task 6：助手面板采纳片段后由容器调用刷新，
 * 保持当前选中集不变；生成中跳过，避免打断流式输出；选中集因刷新被改时
 * 交由 selectedId watch 加载正文，此处不重复请求）。
 * @returns 无
 */
async function reload(): Promise<void> {
  if (generating.value) return
  const prev = selectedId.value
  await loadPlan()
  await nextTick()
  if (selectedId.value === prev && selectedId.value != null) {
    await loadBody(selectedId.value)
  }
}

/** 组件暴露（Task 6：容器在助手面板采纳后调 reload 刷新正文卡内容） */
defineExpose({ reload })

// ============================================================
// SSE 正文生成 / 重生成
// ============================================================

/** 生成中（流式进行时禁用按钮、显示光标与中断按钮） */
const generating = ref(false)
/** 流式累积文本（生成中展示） */
const streamText = ref('')
/** 生成阶段提示（status 事件，如「正在生成结构化场次」） */
const statusMsg = ref('')
/** SSE error 事件文案（生成失败提示） */
const genError = ref('')
/** 生成用的 AbortController */
let abortCtrl: AbortController | null = null
/** 流式区滚动容器引用 */
const streamBox = ref<HTMLElement | null>(null)

// 流式文本变化时自动滚动到底部
watch(streamText, () => {
  void nextTick(() => {
    if (streamBox.value) streamBox.value.scrollTop = streamBox.value.scrollHeight
  })
})

/**
 * 中止当前正文流式请求（保留已落库内容，丢弃流式半成品）。
 * @returns 无
 */
function abortStream(): void {
  abortCtrl?.abort()
}

/**
 * 生成 / 重新生成选中集正文（SSE：delta→status→done/error）。
 * 生成完成后默认切回文章体视图，不展示原始 JSON。
 * @param regenerate 是否为重生成（覆盖语义，要求已有正文）
 * @returns 无
 */
async function startGenerate(regenerate: boolean): Promise<void> {
  if (generating.value || selectedId.value == null) return
  streamText.value = ''
  statusMsg.value = ''
  genError.value = ''
  generating.value = true
  activeView.value = 'article'
  abortCtrl = new AbortController()
  const episodeId = selectedId.value
  try {
    const handler = (event: string, data: unknown): void => {
      const d = (data ?? {}) as Record<string, unknown>
      if (event === 'delta' && typeof d.text === 'string') {
        streamText.value += d.text
      } else if (event === 'status' && typeof d.message === 'string') {
        statusMsg.value = d.message
      } else if (event === 'error') {
        // 失败：丢弃流式半成品，仅保留错误提示
        genError.value = typeof d.message === 'string' ? d.message : '正文生成失败，请重试'
        streamText.value = ''
      } else if (event === 'done') {
        statusMap.value = { ...statusMap.value, [episodeId]: 'scripted' }
        applyBody(toStr(d.raw_text), d.content, Number(d.version ?? 1))
        streamText.value = ''
        statusMsg.value = ''
        genError.value = ''
      }
    }
    if (regenerate) {
      await pipelineApi.regenerateEpisodeBodyStream(props.projectId, episodeId, handler, abortCtrl.signal)
    } else {
      await pipelineApi.generateEpisodeBodyStream(props.projectId, episodeId, handler, abortCtrl.signal)
    }
  } catch (e) {
    // 用户主动中断不提示错误；其余由 ssePost 抛出后端 detail
    if ((e as Error).name !== 'AbortError') {
      genError.value = (e as Error).message || '正文生成失败，请重试'
    }
    streamText.value = ''
  } finally {
    generating.value = false
    abortCtrl = null
  }
}

/** 生成失败重试时是否走重生成接口（原已有正文则重生成，否则首次生成） */
const retryRegenerate = computed(
  () => (selectedEpisode.value ? hasBody(selectedEpisode.value) : false),
)

// ============================================================
// 保存 / 导出
// ============================================================

/** 保存中（防重复提交） */
const saving = ref(false)

/**
 * 保存本集正文（PUT body，base_version 用当前 version 做乐观锁）。
 * 保存成功后自动把本集内容传给下游：触发整剧分镜拆解（SB-01），场次结构
 * 与漫剧格式随之同步到资产/分镜页——原「确认剧本，生成资产」出口已合并进
 * 保存动作，无需单独确认。
 * @returns 无
 */
async function saveBody(): Promise<void> {
  if (selectedId.value == null || saving.value) return
  if (!rawText.value.trim()) {
    ElMessage.warning('正文内容不能为空')
    return
  }
  saving.value = true
  try {
    const payload: EpisodeBodySavePayload = {
      raw_text: rawText.value,
      content: buildContent(),
      base_version: currentVersion.value ?? undefined,
    }
    const out = await pipelineApi.saveEpisodeBody(props.projectId, selectedId.value, payload)
    currentVersion.value = out.version
    scenes.value = normalizeBodyContent(out.content).scenes
    ElMessage.success(`本集正文已保存（v${out.version}）`)
    // 后台同步下游（不阻塞保存主流程）：分镜/资产页即可拿到最新拆解结果
    void syncDownstream()
  } finally {
    saving.value = false
  }
}

/** 下游同步进行中（保存后自动分镜拆解的防重入标记） */
const syncingDownstream = ref(false)

/**
 * 保存后同步下游（资产/分镜）：触发整剧分镜拆解（SB-01 decompose，与分镜页
 * 既有调用一致），依据全部已生成正文全量重建分镜序列并联动场景资产；失败时
 * 不打断保存主流程（业务 detail 已由 client 拦截器提示），可稍后在分镜页重拆。
 * 原出口的投递画布询问（SB-09）不再自动弹出，需要时到分镜页投递。
 * @returns 无
 */
async function syncDownstream(): Promise<void> {
  if (syncingDownstream.value) return
  syncingDownstream.value = true
  try {
    await studioApi.decompose(props.projectId)
    ElMessage.success('已同步分镜拆解，可前往「资产 / 分镜」查看')
  } catch {
    // 拆解失败不阻断保存：拦截器已提示，用户可稍后到分镜页重试
  } finally {
    syncingDownstream.value = false
  }
}

/** 导出中的格式（按钮 loading），null 表示空闲 */
const exporting = ref<ScriptExportFormat | null>(null)

/**
 * 导出剧本（TXT / Fountain，当前集）。
 * @param format 导出格式
 * @returns 无
 */
async function doExport(format: ScriptExportFormat): Promise<void> {
  if (exporting.value) return
  exporting.value = format
  try {
    await pipelineApi.exportScript(props.projectId, format, selectedId.value ?? undefined)
    ElMessage.success(`已导出 ${format === 'fountain' ? 'Fountain' : 'TXT'} 文件`)
  } finally {
    exporting.value = null
  }
}

/**
 * 导出下拉命令分发（el-dropdown command 值 → 导出格式）。
 * @param command 下拉项命令值
 * @returns 无
 */
function handleExportCommand(command: string | number | object): void {
  if (command === 'txt' || command === 'fountain') void doExport(command)
}

// ============================================================
// 断线恢复（只读展示 pipeline/status 摘要，不轮询）
// ============================================================

/** 流水线状态（null=未拉取/无检查点） */
const pipelineStatus = ref<pipelineApi.ScriptPipelineStatusOut | null>(null)
/** 流水线状态弹窗可见性 */
const statusDialogVisible = ref(false)

/** 挂起闸口阶段中文标签 */
const STAGE_LABELS: Record<string, string> = {
  characters: '角色设定',
  outline: '分集大纲',
  body: '逐集正文',
}

/** 断线恢复提示文案（无待确认/断点时为 ''） */
const recoveryTip = computed(() => {
  const st = pipelineStatus.value
  if (!st) return ''
  if (st.has_pending_confirm) {
    return `上次流水线在「${STAGE_LABELS[st.pending_stage] ?? st.pending_stage}」处等待确认，尚未完成`
  }
  if (st.current_stage) {
    return `上次流水线执行至节点「${st.current_stage}」处中断`
  }
  return ''
})

/** 进度摘要展示文案 */
const statusSummaryText = computed(() => {
  const s = pipelineStatus.value?.summary
  if (!s) return ''
  return `候选角色 ${s.character_count ?? 0} · 分集 ${s.episode_count ?? 0} · 已生成正文 ${s.body_count ?? 0} 集`
})

/**
 * 拉取流水线断线恢复状态（失败静默，不阻断页面）。
 * @returns 无
 */
async function loadPipelineStatus(): Promise<void> {
  try {
    pipelineStatus.value = await pipelineApi.getPipelineStatus(props.projectId)
  } catch {
    pipelineStatus.value = null
  }
}

// ============================================================
// 生命周期
// ============================================================

onMounted(async () => {
  await loadPlan()
  void loadPipelineStatus()
})
// keep-alive 切走/卸载时中止进行中的正文流
onDeactivated(abortStream)
onBeforeUnmount(abortStream)
</script>

<template>
  <div class="step-body" v-loading="loading">
    <!-- 断线恢复提示：上次生成未完成（仅展示 status 摘要，不做轮询） -->
    <el-alert
      v-if="recoveryTip"
      class="recover-alert"
      type="warning"
      show-icon
      :closable="false"
      :title="recoveryTip"
    >
      <template #default>
        <div class="recover-body">
          <span>{{ statusSummaryText }}</span>
          <el-button size="small" text type="primary" @click="statusDialogVisible = true">
            查看流水线状态
          </el-button>
        </div>
      </template>
    </el-alert>

    <!-- 阶段提示（未到大纲确认时引导） -->
    <el-alert
      v-if="phase !== 'body'"
      class="body-alert"
      type="info"
      show-icon
      :closable="false"
      title="大纲尚未确认：请回到「分集大纲」确认后进入正文创作"
    />

    <div class="body-layout">
      <!-- 左：集列表 -->
      <aside class="ep-list">
        <el-empty
          v-if="!loading && episodes.length === 0"
          description="暂无分集，请回「分集大纲」生成"
          :image-size="60"
        />
        <div
          v-for="ep in episodes"
          :key="ep.episode_id"
          class="ep-item"
          :class="{ 'ep-item--active': ep.episode_id === selectedId }"
          role="button"
          @click="selectedId = ep.episode_id"
        >
          <div class="ep-item__main">
            <span class="ep-seq">第{{ ep.sequence }}集</span>
            <span class="ep-title">{{ ep.title }}</span>
          </div>
          <div class="ep-item__meta">
            <el-tag v-if="hasBody(ep)" size="small" type="success" effect="plain">已有正文</el-tag>
            <el-tag v-else size="small" type="info" effect="plain">未生成</el-tag>
            <span v-if="ep.duration_sec" class="ep-duration">{{ ep.duration_sec }}s</span>
          </div>
          <div class="ep-item__ops" @click.stop>
            <el-button
              v-if="!hasBody(ep)"
              size="small"
              text
              type="primary"
              :disabled="generating"
              @click="selectedId = ep.episode_id; startGenerate(false)"
            >
              生成本集正文
            </el-button>
            <el-button
              v-else
              size="small"
              text
              type="primary"
              :disabled="generating"
              @click="selectedId = ep.episode_id; startGenerate(true)"
            >
              重新生成本集
            </el-button>
          </div>
        </div>
      </aside>

      <!-- 中：长文编辑器 / 流式 / 空态 -->
      <section class="body-main">
        <!-- 顶部工具栏（选中集且非生成中时可用；按钮靠右、无容器框——本集标题已由集卡展示） -->
        <div v-if="selectedEpisode" class="body-toolbar">
          <el-button
            size="small"
            type="primary"
            :loading="saving"
            :disabled="!bodyLoaded || generating"
            @click="saveBody"
          >
            保存
          </el-button>
          <el-button
            size="small"
            :disabled="!bodyLoaded || generating"
            @click="startGenerate(true)"
          >
            重生成本集
          </el-button>
          <el-dropdown trigger="click" @command="handleExportCommand">
            <el-button size="small" :loading="!!exporting">
              导出<el-icon class="el-icon--right"><ArrowDown /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="txt">导出 TXT</el-dropdown-item>
                <el-dropdown-item command="fountain">导出 Fountain</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>

        <!-- 生成中：流式输出区 + 进度提示 -->
        <el-card v-if="generating" shadow="never" class="stream-card">
          <div class="stream-head">
            <span class="stream-label">正在生成本集正文…</span>
            <el-button size="small" type="danger" plain @click="abortStream">中断</el-button>
          </div>
          <div ref="streamBox" class="stream-box">
            <span class="stream-text">{{ streamText || '正在连接模型…' }}</span><span class="cursor">▍</span>
          </div>
          <div v-if="statusMsg" class="stream-status">
            <el-icon class="is-loading"><Loading /></el-icon>
            <span>{{ statusMsg }}</span>
          </div>
        </el-card>

        <!-- 生成失败 -->
        <el-alert
          v-else-if="genError"
          class="gen-error"
          type="error"
          show-icon
          :closable="false"
          :title="genError"
        >
          <template #default>
            <el-button size="small" type="primary" @click="startGenerate(retryRegenerate)">
              重试
            </el-button>
          </template>
        </el-alert>

        <!-- 未生成态：空态 + 开始生成 -->
        <div v-else-if="selectedEpisode && !bodyLoaded" class="body-empty" v-loading="bodyLoading">
          <el-empty :image-size="120" description="该集尚未生成正文">
            <el-button type="primary" :disabled="generating" @click="startGenerate(false)">
              开始生成第 {{ selectedEpisode.sequence }} 集正文
            </el-button>
          </el-empty>
        </div>

        <!-- 已生成态：文章体 / 场次结构 / 漫剧格式 三视图 -->
        <div v-else-if="bodyLoaded" class="body-editor" v-loading="bodyLoading">
          <el-tabs v-model="activeView">
            <el-tab-pane label="文章体" name="article">
              <QuotableText :source="bodyQuoteSource" scope="episode_body" :target-episode-id="selectedEpisode?.episode_id ?? null">
                <el-input
                  v-model="rawText"
                  type="textarea"
                  :rows="24"
                  resize="vertical"
                  placeholder="本集正文（文章体）"
                  class="article-area"
                />
              </QuotableText>
            </el-tab-pane>
            <el-tab-pane label="场次结构" name="scenes">
              <!-- v-if 惰性挂载：与漫剧格式视图互斥渲染，避免 body-scene-{no} 锚点重复 -->
              <template v-if="activeView === 'scenes'">
                <el-empty v-if="!scenes.length" description="本集暂无结构化场次" :image-size="60" />
                <div
                  v-for="sc in scenes"
                  :key="sc.scene_no"
                  :id="`body-scene-${sc.scene_no}`"
                  class="scene-card"
                >
                  <div class="scene-head">
                    <span class="scene-no">场景 {{ sc.scene_no }}</span>
                    <span class="scene-title">{{ sc.title }}</span>
                    <el-tag v-if="sc.duration_sec" size="small" type="info" effect="plain">
                      约 {{ sc.duration_sec }}s
                    </el-tag>
                    <el-tag v-if="sc.source" size="small" :type="sc.source === 'user' ? 'warning' : 'success'" effect="plain">
                      {{ sc.source === 'user' ? '人工修改' : 'AI 生成' }}
                    </el-tag>
                    <el-tag v-if="sc.location" size="small" effect="plain">{{ sc.location }}</el-tag>
                  </div>
                  <div class="scene-field">
                    <span class="field-label">剧情</span>
                    <QuotableText :source="`${bodyQuoteSource} · 场景${sc.scene_no} · 剧情`" scope="episode_body" :target-episode-id="selectedEpisode?.episode_id ?? null" class="quote-field">
                      <el-input v-model="sc.plot" type="textarea" :rows="2" placeholder="场景剧情描述" />
                    </QuotableText>
                  </div>
                  <div class="scene-field">
                    <span class="field-label">动作</span>
                    <QuotableText :source="`${bodyQuoteSource} · 场景${sc.scene_no} · 动作`" scope="episode_body" :target-episode-id="selectedEpisode?.episode_id ?? null" class="quote-field">
                      <el-input v-model="sc.action" type="textarea" :rows="2" placeholder="动作/画面描述" />
                    </QuotableText>
                  </div>
                  <div v-if="sc.dialogue.length" class="dlg-list">
                    <div v-for="(dl, di) in sc.dialogue" :key="di" class="dlg-row">
                      <el-tag size="small" effect="plain" class="dlg-type">{{ dl.type }}</el-tag>
                      <span class="dlg-char">{{ dl.character || '旁白' }}</span>
                      <el-input v-model="dl.line" class="dlg-line" placeholder="台词" />
                    </div>
                  </div>
                  <el-empty v-else description="本场景暂无对白" :image-size="40" />
                </div>
              </template>
            </el-tab-pane>
            <el-tab-pane label="漫剧格式" name="manga">
              <!-- 惰性挂载（与场次结构互斥）：漫剧规范文本只读视图 + 一键复制 -->
              <template v-if="activeView === 'manga'">
                <div class="manga-toolbar">
                  <span class="manga-tip">
                    漫剧规范格式（只读）：【场景：地点，时段，内/外景】+ 动作行 +
                    角色（情绪）：台词；旁白 / 独白 / 画外音按 type 区分标注
                  </span>
                  <el-button size="small" :icon="CopyDocument" @click="copyMangaText">
                    复制全文
                  </el-button>
                </div>
                <el-empty v-if="!scenes.length" description="本集暂无结构化场次" :image-size="60" />
                <div
                  v-for="sc in scenes"
                  :key="sc.scene_no"
                  :id="`body-scene-${sc.scene_no}`"
                  class="manga-scene"
                >
                  <div class="manga-scene__head">
                    <span class="scene-no">场景 {{ sc.scene_no }}</span>
                    <span class="scene-title">{{ sc.title }}</span>
                  </div>
                  <pre class="manga-pre">{{ renderMangaScene(sc) }}</pre>
                </div>
              </template>
            </el-tab-pane>
          </el-tabs>
        </div>

        <!-- 无选中集 -->
        <div v-else class="body-empty">
          <el-empty description="从左侧选择一集开始创作" :image-size="100" />
        </div>
      </section>
    </div>

    <!-- 流水线状态弹窗（断线恢复摘要，只读） -->
    <el-dialog v-model="statusDialogVisible" title="流水线状态" width="480px" append-to-body>
      <div v-if="pipelineStatus" class="status-body">
        <div class="status-row">
          <span class="status-label">当前阶段</span>
          <span>{{ pipelineStatus.script_phase ?? '未进入流水线' }}</span>
        </div>
        <div class="status-row">
          <span class="status-label">待确认闸口</span>
          <span>{{ pipelineStatus.has_pending_confirm ? (STAGE_LABELS[pipelineStatus.pending_stage] ?? pipelineStatus.pending_stage) : '无' }}</span>
        </div>
        <div class="status-row">
          <span class="status-label">断点节点</span>
          <span>{{ pipelineStatus.current_stage || '无' }}</span>
        </div>
        <div class="status-row">
          <span class="status-label">进度摘要</span>
          <span>{{ statusSummaryText || '无' }}</span>
        </div>
        <el-alert
          type="info"
          :closable="false"
          class="status-tip"
          title="如需续跑，请回到「创意」步骤的流水线入口继续；本页仅展示状态摘要"
        />
      </div>
      <el-empty v-else description="暂无流水线状态（未使用流水线模式）" :image-size="60" />
      <template #footer>
        <el-button @click="statusDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.step-body {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-5);
}
.recover-alert,
.body-alert {
  border-radius: var(--mf-r-ctl);
}
.recover-body {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  margin-top: 4px;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.body-layout {
  display: grid;
  /* 两栏：集列表 / 正文（minmax(0,1fr) 防 1fr 被 auto 最小尺寸撑爆）。
     外层工作区（参数栏+助手面板）已占大量宽度，固定列收窄并配容器查询降级
     （见文件末尾 @container 块），避免正文列被挤压成竖排 */
  grid-template-columns: 240px minmax(0, 1fr);
  gap: var(--mf-sp-4);
  align-items: start;
}
/* 集卡单行流（与分集大纲 ep-cards 同款）：不换行、超出横向滑动选择 */
.ep-list {
  display: flex;
  flex-direction: row;
  flex-wrap: nowrap;
  align-items: stretch;
  gap: var(--mf-sp-2);
  overflow-x: auto;
  padding-bottom: var(--mf-sp-1);
}
.ep-list::-webkit-scrollbar {
  height: 6px;
}
.ep-list::-webkit-scrollbar-thumb {
  border-radius: 3px;
  background: var(--mf-border);
}
.ep-item {
  flex-shrink: 0;
  min-width: 200px;
  padding: var(--mf-sp-3);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-surface);
  cursor: pointer;
  transition: border-color var(--mf-dur) var(--mf-ease), background var(--mf-dur) var(--mf-ease);
}
.ep-item:hover {
  border-color: var(--mf-brand);
}
.ep-item--active {
  border-color: var(--mf-brand);
  background: var(--mf-brand-soft);
}
.ep-item__main {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.ep-seq {
  flex-shrink: 0;
  color: var(--mf-brand);
  font-weight: var(--mf-fw-semibold);
  font-size: var(--mf-fs-sm);
}
.ep-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: var(--mf-fw-semibold);
}
.ep-item__meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: var(--mf-sp-1);
}
.ep-duration {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.ep-item__ops {
  margin-top: var(--mf-sp-1);
  text-align: right;
}
.body-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
}
/* 选中文本引用包装：占满表单行宽度（场次卡/角色表单内使用） */
.quote-field {
  flex: 1;
  min-width: 0;
}
.body-toolbar {
  display: flex; /* 按钮靠右、无容器框（无边框/底色/内边距） */
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--mf-sp-2);
}
.stream-card {
  border-radius: var(--mf-r-ctl);
}
.stream-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--mf-sp-2);
}
.stream-label {
  font-weight: var(--mf-fw-semibold);
}
.stream-box {
  min-height: 200px;
  max-height: 460px;
  overflow-y: auto;
  padding: var(--mf-sp-3);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-surface-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.9;
  white-space: pre-wrap;
  word-break: break-word;
}
.cursor {
  display: inline-block;
  color: var(--mf-brand);
  animation: mf-blink 0.9s step-end infinite;
}
@keyframes mf-blink {
  50% {
    opacity: 0;
  }
}
.stream-status {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: var(--mf-sp-2);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.gen-error {
  border-radius: var(--mf-r-ctl);
}
.body-empty {
  min-height: 360px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--mf-border);
  border-radius: var(--mf-r-ctl);
}
.body-editor {
  min-height: 320px;
}
.article-area :deep(.el-textarea__inner) {
  font-size: var(--mf-fs-sm);
  line-height: 1.9;
}
.scene-card {
  padding: var(--mf-sp-3);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
  margin-bottom: var(--mf-sp-4);
}
.scene-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-3);
}
.scene-no {
  font-weight: var(--mf-fw-semibold);
  color: var(--mf-brand);
}
.scene-title {
  font-weight: var(--mf-fw-semibold);
}
.scene-field {
  display: flex;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-2);
}
.field-label {
  flex-shrink: 0;
  width: 36px;
  padding-top: 4px;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.dlg-list {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-2);
  margin-top: var(--mf-sp-2);
}
.dlg-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.dlg-type {
  flex-shrink: 0;
}
.dlg-char {
  flex-shrink: 0;
  width: 72px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--mf-fs-sm);
  color: var(--mf-text-2);
}
.dlg-line {
  flex: 1;
}
/* ===== 漫剧格式视图（Task 4）===== */
.manga-toolbar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  margin-bottom: var(--mf-sp-3);
}
.manga-tip {
  flex: 1;
  min-width: 0;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
  line-height: 1.6;
}
.manga-scene {
  padding: var(--mf-sp-3);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
  margin-bottom: var(--mf-sp-4);
}
.manga-scene__head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-2);
}
.manga-pre {
  margin: 0;
  font-family: inherit;
  font-size: var(--mf-fs-sm);
  line-height: 1.9;
  color: var(--mf-text-2);
  white-space: pre-wrap;
  word-break: break-word;
}
.status-body {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-2);
}
.status-row {
  display: flex;
  gap: var(--mf-sp-2);
  font-size: var(--mf-fs-sm);
}
.status-label {
  flex-shrink: 0;
  width: 80px;
  color: var(--mf-text-3);
}
.status-tip {
  margin-top: var(--mf-sp-2);
  border-radius: var(--mf-r-ctl);
}

/* ===== 容器查询降级（置于样式末尾确保覆盖原始规则；容器为 ScriptBodyCard
   根 .sb-card，container-type 已在外壳声明）：
   窄容器（<760px）→ 单列堆叠：集列表横向滑动，正文纵排 ===== */
@container sbcard (max-width: 760px) {
  .body-layout {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
