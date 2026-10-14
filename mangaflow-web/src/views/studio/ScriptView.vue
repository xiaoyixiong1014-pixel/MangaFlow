<!--
  剧本创作页（文件级注释）。

  对应原型 S-02，五阶段之「① 剧本」，路由 /projects/:id/script。
  功能：
  - 左栏「原始输入」：已有剧本时只读展示；无剧本时三 Tab（一句话/小说文本/大纲）快速录入
    并调 saveInput(mode, raw_text)；
  - 右栏「AI 生成」：generateStream（POST SSE）增量追加流式文本（打字机光标 + 自动滚动），
    支持 AbortController 中断；error 事件展示失败三路径面板（S-09：重试 / 换模型 /
    精简输入），genError 清空（成功/关闭）后面板自动隐藏；done 后刷新剧本详情；
  - 结构化展示：大纲导航（锚点定位）+ 场景卡列表（场景号/标题/剧情/对白表/时长）；
    content 兼容三种落库形态：字段级 {"value","source"} 包装（AI 生成）、
    整体 {"value","source"} 包装（手动保存）、{"raw"} 纯文本；
  - 场景卡「编辑」：el-dialog 内以 JSON 文本编辑该场景，保存走 saveScript；
  - AI 助手：选粒度 → agent.chatStream 流式给出建议（抽屉展示，标注需手动采纳）；
  - 剧本体检：scriptHealth（修正版 POST）→ 得分 + 问题列表，点击定位到对应场景卡；
  - S-08 一键同步：「同步到 Agent」调 send-to-agent（按剧本版本幂等）后跳转
    AI 工作台并携带 thread_id query 续聊；「投递画布」调 send-to-canvas
    （按剧本版本幂等）成功后 el-message 内提供画布编辑器跳转链接；
  - 底部主按钮「确认剧本，进入实体提取」→ 跳转 /projects/:id/script/extract。
-->
<script setup lang="ts">
import { computed, h, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Lock } from '@element-plus/icons-vue'
import * as studioApi from '@/api/studio'
import * as agentApi from '@/api/agent'
import { modelsCatalog } from '@/api/catalog'
import { rollbackSnapshot, snapshots as snapshotsApi } from '@/api/projects'
import { useProjectStore } from '@/stores/project'
import type { LogicalModel, ScriptContent, ScriptOut, VersionSnapshot } from '@/types'

const route = useRoute()
const router = useRouter()
const projectStore = useProjectStore()

/** 当前项目 ID（取路由参数） */
const projectId = computed(() => Number(route.params.id))

/** 当前选中剧集 ID（S-06 剧集切换器；null = 默认剧集，请求不携带 episode_id） */
const currentEpisodeId = computed(() => projectStore.currentEpisodeId)

// ============================================================
// 剧本加载状态
// ============================================================

/** 页面首屏加载中（骨架屏） */
const loading = ref(true)
/** 剧本详情（含 content/raw_text/version） */
const script = ref<ScriptOut | null>(null)
/** 项目尚无剧本行（404）时为 true，展示三 Tab 录入 */
const noScript = ref(false)

/** 解析后的场景条目（宽松结构，兼容 LLM 输出） */
interface ParsedScene {
  /** 场景序号 */
  scene_no: number
  /** 场景标题 */
  title: string
  /** 场景剧情描述 */
  plot: string
  /** 对白列表（角色名 + 台词） */
  dialogue: { character: string; line: string }[]
  /** 时长（秒） */
  duration_sec: number | null
}

/** 解析后的大纲条目 */
interface ParsedOutline {
  /** 幕名（第一幕…） */
  act: string
  /** 本幕概要 */
  summary: string
}

/** 剧本内容的统一解析结果 */
interface ParsedScript {
  /** 是否为纯文本形态（content.raw） */
  isRaw: boolean
  /** 纯文本内容（isRaw 时有效） */
  rawText: string
  /** 剧本标题 */
  title: string
  /** 一句话梗概 */
  logline: string
  /** 三幕大纲 */
  outline: ParsedOutline[]
  /** 场景列表 */
  scenes: ParsedScene[]
  /** 解包后的纯结构（编辑保存时整体提交） */
  plain: Record<string, unknown> | null
}

/** 当前解析结果（无剧本时为 null） */
const parsed = ref<ParsedScript | null>(null)

/**
 * 字段级解包：{"value": x, "source": ...} 包装 → x，其余原样返回。
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
 * 把后端 content 统一解析为可渲染结构。
 * 兼容三种形态：字段级包装 / 整体包装（手动保存） / {"raw": 文本} 纯文本。
 * @param content 剧本 content JSONB
 * @returns 解析结果；content 为空时返回 null
 */
function normalizeScript(content: ScriptContent | null | undefined): ParsedScript | null {
  if (!content) return null
  const c = content as Record<string, unknown>
  let plain: Record<string, unknown>
  if (typeof c.value === 'object' && c.value !== null && !Array.isArray(c.value) && 'source' in c) {
    // 手动保存后的整体包装：{"value": {...}, "source": "user"}
    plain = c.value as Record<string, unknown>
  } else {
    // AI 生成后的字段级包装（或首次录入仅 input_mode）
    plain = {}
    for (const [k, v] of Object.entries(c)) plain[k] = unwrapField(v)
  }
  const empty: ParsedScript = {
    isRaw: false, rawText: '', title: '', logline: '', outline: [], scenes: [], plain,
  }
  // 纯文本形态：JSON 解析失败时后端落库 {"raw": 全文}
  if (typeof plain.raw === 'string') {
    return { ...empty, isRaw: true, rawText: plain.raw }
  }
  const outlineRaw = Array.isArray(plain.outline) ? plain.outline : []
  const scenesRaw = Array.isArray(plain.scenes) ? plain.scenes : []
  return {
    ...empty,
    title: typeof plain.title === 'string' ? plain.title : '',
    logline: typeof plain.logline === 'string' ? plain.logline : '',
    outline: outlineRaw.map((o) => {
      const rec = (o ?? {}) as Record<string, unknown>
      return { act: String(rec.act ?? ''), summary: String(rec.summary ?? '') }
    }),
    scenes: scenesRaw.map((s, i) => {
      const rec = (s ?? {}) as Record<string, unknown>
      const dl = Array.isArray(rec.dialogue) ? rec.dialogue : []
      return {
        scene_no: Number(rec.scene_no ?? i + 1) || i + 1,
        title: String(rec.title ?? `场景 ${i + 1}`),
        plot: String(rec.plot ?? ''),
        dialogue: dl.map((d) => {
          const dr = (d ?? {}) as Record<string, unknown>
          return { character: String(dr.character ?? ''), line: String(dr.line ?? '') }
        }),
        duration_sec: rec.duration_sec != null ? Number(rec.duration_sec) : null,
      }
    }),
  }
}

/**
 * 拉取剧本详情并解析（无剧本行 404 时进入录入空态）。
 * S-06：按当前选中剧集过滤（默认剧集 = 不传 episode_id，兼容存量 NULL 行）。
 * @returns 无
 */
async function loadScript(): Promise<void> {
  loading.value = true
  try {
    script.value = await studioApi.getInput(projectId.value, currentEpisodeId.value ?? undefined)
    noScript.value = false
    parsed.value = normalizeScript(script.value.content)
  } catch (e) {
    // 404 = 当前范围尚无剧本行，属正常空态（全局拦截器已提示一次），切录入视图
    const status = (e as { response?: { status?: number } }).response?.status
    if (status === 404) {
      noScript.value = true
      script.value = null
      parsed.value = null
    }
  } finally {
    loading.value = false
  }
}

// 切换剧集（S-06）后按新剧集重新加载剧本
watch(currentEpisodeId, () => {
  void loadScript()
})

// ============================================================
// 原始输入（三 Tab 快速录入）
// ============================================================

/** 录入模式：idea 一句话 / novel 小说文本 / outline 大纲 */
const inputMode = ref<'idea' | 'novel' | 'outline'>('idea')
/** 录入文本 */
const inputText = ref('')
/** 录入保存中（防重复提交） */
const savingInput = ref(false)

/**
 * 保存原始输入，成功后进入生成视图。
 * @returns 无
 */
async function saveInput(): Promise<void> {
  if (!inputText.value.trim()) {
    ElMessage.warning('请先输入内容')
    return
  }
  savingInput.value = true
  try {
    script.value = await studioApi.saveInput(projectId.value, {
      mode: inputMode.value,
      raw_text: inputText.value.trim(),
      // S-06：绑定当前剧集（默认剧集 = 不传，后端落 NULL 行）
      episode_id: currentEpisodeId.value ?? undefined,
    })
    noScript.value = false
    parsed.value = normalizeScript(script.value.content)
    ElMessage.success('原始输入已保存，可开始 AI 生成')
  } finally {
    savingInput.value = false
  }
}

// ============================================================
// AI 流式生成
// ============================================================

/** 是否生成中（流式进行时禁用按钮、显示光标动画与中断按钮） */
const generating = ref(false)
/** 流式累积文本（打字机区域） */
const streamText = ref('')
/** SSE error 事件文案（警告条） */
const genError = ref('')
/** 生成用的 AbortController（「中断」按钮触发 abort） */
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
 * 开始 AI 流式生成剧本（POST SSE）。
 * 事件：delta 增量追加；error 展示警告条；done 刷新详情。
 * @returns 无
 */
async function startGenerate(): Promise<void> {
  if (generating.value) return // 防重复点击
  streamText.value = ''
  genError.value = ''
  generating.value = true
  abortCtrl = new AbortController()
  try {
    await studioApi.generateStream(
      projectId.value,
      {},
      (event, data) => {
        const d = (data ?? {}) as Record<string, unknown>
        if (event === 'delta' && typeof d.text === 'string') {
          streamText.value += d.text
        } else if (event === 'error') {
          genError.value = typeof d.message === 'string' ? d.message : '生成过程出现异常'
        } else if (event === 'done') {
          // S-09：成功后面板自动隐藏（genError 清空）+ 刷新详情
          genError.value = ''
          void loadScript()
        }
      },
      abortCtrl.signal,
      // S-06：按当前剧集生成（默认剧集 = 不传，后端作用于默认剧本行）
      currentEpisodeId.value ?? undefined,
    )
  } catch (e) {
    // 用户主动中断不提示错误
    if ((e as Error).name !== 'AbortError') {
      ElMessage.error((e as Error).message || '生成失败，请稍后重试')
    }
  } finally {
    generating.value = false
    abortCtrl = null
    // 中断/结束后内容可能已落库（done 前被截断时），兜底刷新一次
    void loadScript()
  }
}

/**
 * 中断当前生成（AbortController；已产出的文本保留在流式区，落库内容不受影响）。
 * @returns 无
 */
function stopGenerate(): void {
  abortCtrl?.abort()
}

// ============================================================
// S-09 生成失败三路径（重试 / 换模型 / 精简输入）
// ============================================================

/** 建议的原始输入长度上限（超长提示与「保留开头 N 字」截断口径） */
const INPUT_LIMIT = 8000

/** 原始输入字符数（三路径「精简输入」的统计口径） */
const rawCharCount = computed(() => (script.value?.raw_text ?? '').length)

/**
 * 「重试」（S-09 三路径 a）：以相同参数重新发起生成。
 * genError 清空后面板自动隐藏，开始新一轮流式生成。
 * @returns 无
 */
function retryGeneration(): void {
  genError.value = ''
  void startGenerate()
}

/** 换模型弹窗可见性 */
const modelDialogVisible = ref(false)
/** 模型目录拉取中 */
const catalogLoading = ref(false)
/** 文本能力逻辑模型列表（换模型弹窗展示） */
const llmModels = ref<LogicalModel[]>([])

/**
 * 「换模型」（S-09 三路径 b，最小实现）：打开弹窗并拉取模型目录，
 * 展示平台可用文本模型（含健康状态与默认标记）；项目无轻量「默认渠道
 * 偏好」接口，指定渠道的路径为在模型配置页添加自有渠道后返回重试。
 * @returns 无
 */
async function openModelDialog(): Promise<void> {
  modelDialogVisible.value = true
  catalogLoading.value = true
  try {
    const cat = await modelsCatalog()
    llmModels.value =
      cat.capabilities?.find((g) => g.capability === 'llm')?.models ?? []
  } catch {
    // 目录拉取失败：弹窗内展示空态与模型配置页入口，不阻断重试路径
    llmModels.value = []
  } finally {
    catalogLoading.value = false
  }
}

/** 小说输入 textarea 引用（「精简输入」填充后聚焦） */
const inputAreaRef = ref<{ focus: () => void } | null>(null)

/**
 * 「精简输入」（S-09 三路径 c）：切回录入视图并带出原始输入——超长
 * （> INPUT_LIMIT 字）时截取开头 8000 字，随后聚焦输入框，便于截取
 * 核心章节后重新保存并生成。
 * @returns 无
 */
function simplifyInput(): void {
  const raw = script.value?.raw_text ?? ''
  noScript.value = true
  inputMode.value = 'novel'
  inputText.value = raw.length > INPUT_LIMIT ? raw.slice(0, INPUT_LIMIT) : raw
  genError.value = ''
  void nextTick(() => inputAreaRef.value?.focus())
  ElMessage.info(
    raw.length > INPUT_LIMIT
      ? `已保留开头 ${INPUT_LIMIT} 字（原输入 ${raw.length} 字），建议截取核心章节后保存并重新生成`
      : '原始输入未超长，已带出录入框，可调整后重新保存并生成',
  )
}

// ============================================================
// 场景编辑（JSON 文本编辑单个场景）
// ============================================================

/** 编辑弹窗可见性 */
const editVisible = ref(false)
/** 正在编辑的场景下标（scenes 数组内） */
const editIndex = ref(-1)
/** 编辑弹窗内的 JSON 文本 */
const editJsonText = ref('')
/** 保存编辑中（防重复提交） */
const savingScene = ref(false)

/**
 * 打开某场景的编辑弹窗（该场景序列化为 JSON 供修改）。
 * @param index 场景下标
 * @returns 无
 */
function openSceneEdit(index: number): void {
  if (!parsed.value) return
  editIndex.value = index
  editJsonText.value = JSON.stringify(parsed.value.scenes[index], null, 2)
  editVisible.value = true
}

/**
 * 保存场景编辑：解析 JSON → 回写 scenes → saveScript 整体提交（source=user）。
 * @returns 无
 * @throws JSON 解析失败时提示并保留弹窗
 */
async function saveSceneEdit(): Promise<void> {
  if (!parsed.value || editIndex.value < 0) return
  let sceneObj: unknown
  try {
    sceneObj = JSON.parse(editJsonText.value)
  } catch {
    ElMessage.error('JSON 格式错误，请检查后再保存')
    return
  }
  savingScene.value = true
  try {
    // 提交解包后的纯结构（后端会整体包装为 {"value":…, "source":"user"}）
    const plain = { ...(parsed.value.plain ?? {}) }
    const scenes = [...parsed.value.scenes]
    scenes[editIndex.value] = sceneObj as ParsedScene
    plain.scenes = scenes
    script.value = await studioApi.saveScript(projectId.value, {
      content: plain as ScriptContent,
      // S-06：按当前剧集保存（默认剧集 = 不传，后端定位默认剧本行）
      episode_id: currentEpisodeId.value ?? undefined,
    })
    parsed.value = normalizeScript(script.value.content)
    editVisible.value = false
    ElMessage.success('场景已保存')
  } finally {
    savingScene.value = false
  }
}

// ============================================================
// AI 助手（剧本 Agent 建议，抽屉展示）
// ============================================================

/** 助手抽屉可见性 */
const assistantVisible = ref(false)
/** 优化粒度（润色对白/扩写场景/精简全文） */
const granularity = ref('润色对白')
/** 助手补充指令 */
const assistantPrompt = ref('')
/** 助手流式回复文本 */
const assistantReply = ref('')
/** 助手请求进行中（防重复点击） */
const chatting = ref(false)
/** 助手用的 AbortController */
let chatAbort: AbortController | null = null
/** 助手回复区滚动容器 */
const assistantBox = ref<HTMLElement | null>(null)

// 助手回复变化时自动滚动
watch(assistantReply, () => {
  void nextTick(() => {
    if (assistantBox.value) assistantBox.value.scrollTop = assistantBox.value.scrollHeight
  })
})

/**
 * 打开助手抽屉。
 * @returns 无
 */
function openAssistant(): void {
  assistantVisible.value = true
}

/**
 * 让剧本 Agent 生成优化建议（api/agent.chatStream 流式），结果仅作建议展示。
 * @returns 无
 */
async function askAssistant(): Promise<void> {
  if (chatting.value) return // 防重复点击
  const digest = parsed.value
    ? `剧本《${parsed.value.title || '未命名'}》：${parsed.value.logline}。共 ${parsed.value.scenes.length} 个场景。`
    : (script.value?.raw_text ?? '')
  assistantReply.value = ''
  chatting.value = true
  assistantVisible.value = true
  chatAbort = new AbortController()
  try {
    await agentApi.chatStream(
      {
        message: `请对以下漫剧剧本进行「${granularity.value}」，并给出建议内容（不要直接改写原剧本）：${digest}。补充要求：${assistantPrompt.value || '无'}`,
        project_id: projectId.value,
      },
      (event, data) => {
        const d = (data ?? {}) as Record<string, unknown>
        if (event === 'delta' && typeof d.text === 'string') {
          assistantReply.value += d.text
        } else if (event === 'error') {
          ElMessage.warning(typeof d.message === 'string' ? d.message : '助手服务异常')
        }
      },
      chatAbort.signal,
    )
  } catch (e) {
    if ((e as Error).name !== 'AbortError') {
      ElMessage.error((e as Error).message || '助手请求失败')
    }
  } finally {
    chatting.value = false
    chatAbort = null
  }
}

// ============================================================
// 剧本体检
// ============================================================

/** 体检请求进行中 */
const healthLoading = ref(false)
/** 体检问题条目（宽松结构） */
interface HealthIssue {
  /** 级别 error/warning */
  level: string
  /** 问题描述 */
  message: string
  /** 定位描述（如「场景2」） */
  location: string
}
/** 体检得分（null 表示未体检） */
const healthScore = ref<number | null>(null)
/** 体检问题列表 */
const healthIssues = ref<HealthIssue[]>([])

/**
 * 发起剧本体检并展示结果面板。
 * @returns 无
 */
async function runHealth(): Promise<void> {
  if (healthLoading.value) return
  healthLoading.value = true
  try {
    const report = await studioApi.scriptHealth(projectId.value)
    healthScore.value = typeof report.score === 'number' ? report.score : null
    healthIssues.value = Array.isArray(report.issues)
      ? (report.issues as Record<string, unknown>[]).map((i) => ({
          level: String(i.level ?? 'warning'),
          message: String(i.message ?? ''),
          location: String(i.location ?? ''),
        }))
      : []
    ElMessage.success(healthIssues.value.length ? '体检完成，存在问题待处理' : '体检完成，未发现问题')
  } finally {
    healthLoading.value = false
  }
}

/**
 * 点击体检问题定位到对应场景卡（location 形如「场景N」时滚动至该卡片）。
 * @param location 体检定位描述
 * @returns 无
 */
function locateIssue(location: string): void {
  const m = location.match(/(\d+)/)
  if (!m) return
  const target = document.getElementById(`scene-card-${m[1]}`)
  target?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

// ============================================================
// 确认剧本
// ============================================================

/**
 * 确认剧本，进入实体提取页。
 * @returns 无
 */
async function confirmScript(): Promise<void> {
  if (!parsed.value || parsed.value.isRaw || parsed.value.scenes.length === 0) {
    // 无结构化场景时二次确认，提示下游拆解依赖结构化内容
    try {
      await ElMessageBox.confirm(
        '当前剧本暂无结构化场景数据，实体提取与分镜拆解效果可能受限。是否继续？',
        '确认剧本',
        { type: 'warning', confirmButtonText: '仍要继续', cancelButtonText: '返回编辑' },
      )
    } catch {
      return
    }
  }
  await router.push(`/projects/${projectId.value}/script/extract`)
}

// ============================================================
// 版本历史（V-01 版本快照回退）
// ============================================================

/** 版本历史抽屉可见性 */
const versionVisible = ref(false)
/** 快照列表加载中 */
const snapshotsLoading = ref(false)
/** 项目版本快照列表（created_at 倒序，后端已排序） */
const snapshotRows = ref<VersionSnapshot[]>([])
/** 正在回退的快照 ID（按钮 loading），null 表示无进行中的回退 */
const rollingBackId = ref<number | null>(null)

/** 快照阶段中文标签映射（抽屉展示用；未覆盖的阶段原样展示标识） */
const STAGE_LABELS: Record<string, string> = {
  extract_confirmed: '资产抽取确认',
  asset_locked: '角色定妆锁定',
  pre_rollback: '回退前存档',
  manual: '手动快照',
}

/**
 * 快照阶段的中文标签。
 * @param stage 阶段标识
 * @returns 中文标签（未知阶段返回原标识）
 */
function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage
}

/**
 * 格式化快照创建时间为本地可读格式。
 * @param iso ISO8601 时间串（可空）
 * @returns 形如「2026-10-10 20:30」的串；无效输入原样返回或 '-'
 */
function formatSnapshotTime(iso?: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/**
 * 生成快照摘要文案（summary 为各类实体计数时拼接展示，否则占位）。
 * @param snap 快照行
 * @returns 摘要文案
 */
function summarizeSnapshot(snap: VersionSnapshot): string {
  const s = snap.summary as Record<string, unknown> | null | undefined
  if (s && typeof s === 'object') {
    const parts: string[] = []
    if (typeof s.characters === 'number') parts.push(`角色 ${s.characters}`)
    if (typeof s.scenes === 'number') parts.push(`场景 ${s.scenes}`)
    if (typeof s.props === 'number') parts.push(`道具 ${s.props}`)
    if (parts.length) return parts.join(' · ')
  }
  return '（无摘要）'
}

/**
 * 拉取项目版本快照列表（首页前 50 条，created_at 倒序）。
 * @returns 无
 */
async function fetchSnapshotRows(): Promise<void> {
  snapshotsLoading.value = true
  try {
    const page = await snapshotsApi(projectId.value, { page: 1, page_size: 50 })
    snapshotRows.value = page.items
  } catch {
    // 业务错误已由 client 拦截器统一弹窗提示
  } finally {
    snapshotsLoading.value = false
  }
}

/**
 * 打开版本历史抽屉并加载快照列表。
 * @returns 无
 */
async function openVersionDrawer(): Promise<void> {
  versionVisible.value = true
  await fetchSnapshotRows()
}

/**
 * 回退到指定快照：elMessageBox 二次确认 → 调回退 API → 成功提示并刷新列表。
 * 服务端会自动创建「回退前存档」快照（可再次回退撤销），并写审计日志。
 * @param snap 目标快照
 * @returns 无
 */
async function handleRollback(snap: VersionSnapshot): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '将把项目资产（角色/场景/道具）恢复到该快照记录的状态；回退前会自动保存当前状态为「回退前存档」快照，可再次回退撤销。是否继续？',
      '回退此版本',
      { type: 'warning', confirmButtonText: '确认回退', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消确认
  }
  rollingBackId.value = snap.id
  try {
    const res = await rollbackSnapshot(projectId.value, snap.id)
    ElMessage.success(`已回退；回退前状态已存为快照 #${res.new_snapshot_id}`)
    await fetchSnapshotRows()
  } catch {
    // 业务错误已由 client 拦截器统一弹窗提示
  } finally {
    rollingBackId.value = null
  }
}

// ============================================================
// S-08 剧本一键同步（Agent 会话 / 画布投递，后端幂等）
// ============================================================

/** 同步 Agent 会话进行中（防重复点击） */
const syncingAgent = ref(false)
/** 投递画布进行中（防重复点击） */
const syncingCanvas = ref(false)

/**
 * 剧本一键同步到 Agent 会话：调 send-to-agent（后端按剧本版本幂等），
 * 成功后跳转 AI 工作台并携带 thread_id query（工作台续聊该会话）。
 * @returns 无
 */
async function sendToAgent(): Promise<void> {
  if (syncingAgent.value) return
  syncingAgent.value = true
  try {
    const res = await studioApi.sendScriptToAgent(projectId.value, {
      // S-06：按当前选中剧集同步（默认剧集 = 不传）
      episode_id: currentEpisodeId.value ?? undefined,
    })
    ElMessage.success(res.duplicated ? '该剧本版本已同步过，已定位到既有会话' : '剧本已同步到 Agent 会话')
    await router.push({ path: '/agent', query: { thread_id: res.thread_id } })
  } finally {
    syncingAgent.value = false
  }
}

/**
 * 剧本一键投递画布：调 send-to-canvas（后端按剧本版本幂等），
 * 成功后 el-message 内提供跳转画布编辑器的链接。
 * @returns 无
 */
function sendToCanvas(): void {
  if (syncingCanvas.value) return
  syncingCanvas.value = true
  studioApi
    .sendScriptToCanvas(projectId.value, {
      // S-06：按当前选中剧集投递（默认剧集 = 不传）
      episode_id: currentEpisodeId.value ?? undefined,
    })
    .then((res) => {
      ElMessage({
        type: 'success',
        duration: 6000,
        showClose: true,
        message: h('span', null, [
          res.duplicated ? '该剧本版本已投递过，返回既有画布节点 ' : '剧本已投递到画布 ',
          h(
            'a',
            {
              href: `/canvas/${res.canvas_id}`,
              style: 'color: var(--el-color-primary); margin-left: 4px; text-decoration: underline;',
              onClick: (e: MouseEvent) => {
                e.preventDefault()
                void router.push(`/canvas/${res.canvas_id}`)
              },
            },
            '打开画布',
          ),
        ]),
      })
    })
    .finally(() => {
      syncingCanvas.value = false
    })
}

// 剧本已生成标记（有结构化场景或纯文本均视为已有内容）
const hasContent = computed(
  () => !!parsed.value && (parsed.value.isRaw || parsed.value.scenes.length > 0),
)

onMounted(loadScript)
onBeforeUnmount(() => {
  // 离开页面中止进行中的流式请求
  abortCtrl?.abort()
  chatAbort?.abort()
})
</script>

<template>
  <div class="page-container script-view">
    <div class="view-head">
      <h2 class="page-title">剧本创作</h2>
      <span v-if="script" class="ver-tag">v{{ script.version }}</span>
      <span v-if="projectStore.currentProject?.target_duration" class="dur-tag">
        目标时长 {{ projectStore.currentProject.target_duration }}s
      </span>
      <!-- 版本历史入口（V-01：快照列表 + 回退） -->
      <el-button class="ver-btn" size="small" @click="openVersionDrawer">版本历史</el-button>
    </div>

    <!-- 首屏骨架 -->
    <el-skeleton v-if="loading" :rows="8" animated />

    <template v-else>
      <!-- ========== 无剧本：三 Tab 快速录入 ========== -->
      <el-card v-if="noScript" shadow="never" class="input-card">
        <template #header>
          <div class="card-head">
            <span>原始输入</span>
            <span class="head-tip">尚未创建剧本，请选择一种方式录入创作素材</span>
          </div>
        </template>
        <el-tabs v-model="inputMode">
          <el-tab-pane label="一句话灵感" name="idea">
            <el-input
              v-model="inputText"
              type="textarea"
              :rows="4"
              maxlength="500"
              show-word-limit
              placeholder="例：深夜便利店，失眠的都市女孩与值夜班店员的一场温暖相遇"
            />
          </el-tab-pane>
          <el-tab-pane label="小说文本" name="novel">
            <!-- S-09 精简输入：截断填充后聚焦该输入框 -->
            <el-input
              ref="inputAreaRef"
              v-model="inputText"
              type="textarea"
              :rows="8"
              maxlength="8000"
              show-word-limit
              placeholder="粘贴小说片段或短篇故事原文，AI 将据此改编为漫剧剧本"
            />
          </el-tab-pane>
          <el-tab-pane label="故事大纲" name="outline">
            <el-input
              v-model="inputText"
              type="textarea"
              :rows="8"
              maxlength="4000"
              show-word-limit
              placeholder="按幕/按场罗列你的故事大纲，AI 将扩写为完整剧本"
            />
          </el-tab-pane>
        </el-tabs>
        <div class="input-actions">
          <el-button type="primary" :loading="savingInput" @click="saveInput">保存输入</el-button>
        </div>
      </el-card>

      <!-- ========== 双栏：左原始输入 / 右 AI 生成 ========== -->
      <div v-else class="two-col">
        <!-- 左栏：原始输入 -->
        <el-card shadow="never" class="col-left">
          <template #header>
            <div class="card-head"><span>原始输入</span></div>
          </template>
          <div class="raw-text">{{ script?.raw_text || '（无原始输入）' }}</div>
          <div class="col-left__ops">
            <el-button text type="primary" @click="noScript = true; inputText = script?.raw_text ?? ''">
              修改输入
            </el-button>
          </div>
        </el-card>

        <!-- 右栏：AI 生成 -->
        <el-card shadow="never" class="col-right">
          <template #header>
            <div class="card-head">
              <span>AI 生成</span>
              <div class="head-ops">
                <template v-if="generating">
                  <el-button type="danger" plain size="small" @click="stopGenerate">中断</el-button>
                </template>
                <el-button
                  v-else
                  type="primary"
                  size="small"
                  :loading="generating"
                  @click="startGenerate"
                >
                  {{ hasContent ? '重新生成' : '开始生成' }}
                </el-button>
              </div>
            </div>
          </template>

          <!-- S-09 生成失败三路径面板：重试 / 换模型 / 精简输入（genError 清空自动隐藏） -->
          <el-card v-if="genError" shadow="never" class="fail-panel">
            <div class="fail-title">⚠ 生成失败</div>
            <div class="fail-msg">{{ genError }}</div>
            <div class="fail-actions">
              <el-button
                type="primary"
                size="small"
                :loading="generating"
                @click="retryGeneration"
              >
                重试
              </el-button>
              <el-button size="small" @click="openModelDialog">换模型</el-button>
              <el-button size="small" @click="simplifyInput">精简输入</el-button>
            </div>
            <div class="fail-stats">
              当前原始输入 {{ rawCharCount }} 字
              <template v-if="rawCharCount > INPUT_LIMIT">
                ，超过建议上限 {{ INPUT_LIMIT }} 字——建议截取核心章节后重新生成
              </template>
            </div>
          </el-card>

          <!-- 流式输出区（生成中可见，含打字机光标） -->
          <div v-if="generating || streamText" ref="streamBox" class="stream-box">
            <span class="stream-text">{{ streamText || '正在连接模型…' }}</span><span
              v-if="generating"
              class="cursor"
            >▍</span>
          </div>
          <el-empty
            v-else-if="!hasContent"
            description="还没有剧本内容，点击右上角「开始生成」，AI 将根据原始输入流式创作"
          />
        </el-card>
      </div>

      <!-- ========== 结构化剧本展示 ========== -->
      <template v-if="parsed && parsed.isRaw">
        <el-alert
          class="raw-alert"
          type="info"
          show-icon
          :closable="false"
          title="当前剧本为纯文本形态（结构化解析未通过），请重新生成以获得场景结构"
        />
        <el-card shadow="never"><pre class="raw-pre">{{ parsed.rawText }}</pre></el-card>
      </template>

      <div v-if="parsed && !parsed.isRaw" class="structure">
        <!-- 左：大纲导航 -->
        <el-card shadow="never" class="nav-card">
          <template #header>
            <div class="card-head"><span>大纲导航</span></div>
          </template>
          <el-empty v-if="!parsed.outline.length && !parsed.scenes.length" description="暂无大纲" />
          <template v-else>
            <div v-if="parsed.title" class="nav-title">《{{ parsed.title }}》</div>
            <div v-if="parsed.logline" class="nav-logline">{{ parsed.logline }}</div>
            <div v-for="(o, i) in parsed.outline" :key="`o-${i}`" class="nav-item">
              <span class="nav-act">{{ o.act }}</span>
              <span class="nav-summary">{{ o.summary }}</span>
            </div>
            <el-divider>场景</el-divider>
            <a
              v-for="(s, i) in parsed.scenes"
              :key="`sn-${i}`"
              class="nav-scene"
              :href="`#scene-card-${s.scene_no}`"
            >
              第{{ s.scene_no }}场 · {{ s.title }}
            </a>
          </template>
        </el-card>

        <!-- 右：场景卡列表 -->
        <div class="scenes-col">
          <el-card
            v-for="(s, i) in parsed.scenes"
            :id="`scene-card-${s.scene_no}`"
            :key="i"
            shadow="never"
            class="scene-card"
          >
            <template #header>
              <div class="card-head">
                <span class="scene-no">场景 {{ s.scene_no }}</span>
                <span class="scene-title">{{ s.title }}</span>
                <el-tag v-if="s.duration_sec" size="small" type="info" effect="plain">
                  约 {{ s.duration_sec }}s
                </el-tag>
                <el-button size="small" text type="primary" @click="openSceneEdit(i)">编辑</el-button>
              </div>
            </template>
            <p class="scene-plot">{{ s.plot || '（无剧情描述）' }}</p>
            <el-table v-if="s.dialogue.length" :data="s.dialogue" size="small" class="dlg-table">
              <el-table-column prop="character" label="角色" width="120" />
              <el-table-column prop="line" label="台词" />
            </el-table>
            <el-empty v-else description="本场景暂无对白" :image-size="48" />
          </el-card>
          <el-empty v-if="!parsed.scenes.length" description="剧本中暂无场景数据" />
        </div>
      </div>

      <!-- ========== 体检结果面板 ========== -->
      <el-card v-if="healthScore !== null" shadow="never" class="health-card">
        <template #header>
          <div class="card-head">
            <span>剧本体检</span>
            <el-tag
              :type="healthScore >= 80 ? 'success' : healthScore >= 60 ? 'warning' : 'danger'"
              effect="dark"
            >
              得分 {{ healthScore }}
            </el-tag>
          </div>
        </template>
        <el-empty v-if="!healthIssues.length" description="未发现问题" :image-size="60" />
        <div
          v-for="(iss, i) in healthIssues"
          :key="i"
          class="issue-row"
          role="button"
          @click="locateIssue(iss.location)"
        >
          <el-tag size="small" :type="iss.level === 'error' ? 'danger' : 'warning'">
            {{ iss.level === 'error' ? '错误' : '提醒' }}
          </el-tag>
          <span class="issue-msg">{{ iss.message }}</span>
          <span class="issue-loc">{{ iss.location }}</span>
          <el-button size="small" text type="primary">定位</el-button>
        </div>
      </el-card>

      <!-- ========== 底部操作条 ========== -->
      <div class="footer-bar">
        <div class="footer-left">
          <el-button :loading="healthLoading" @click="runHealth">剧本体检</el-button>
          <el-button :disabled="!hasContent" @click="openAssistant">AI 助手</el-button>
          <!-- S-08 剧本一键同步：Agent 会话（幂等，跳转工作台续聊）/ 画布投递（幂等） -->
          <el-button :disabled="!hasContent" :loading="syncingAgent" @click="sendToAgent">
            同步到 Agent
          </el-button>
          <el-button :disabled="!hasContent" :loading="syncingCanvas" @click="sendToCanvas">
            投递画布
          </el-button>
        </div>
        <el-button
          size="large"
          class="confirm-btn"
          :loading="generating"
          @click="confirmScript"
        >
          <el-icon v-if="false"><Lock /></el-icon>
          确认剧本，进入实体提取
        </el-button>
      </div>
    </template>

    <!-- ========== S-09 换模型弹窗：展示平台文本模型 + 模型配置页入口 ========== -->
    <el-dialog v-model="modelDialogVisible" title="更换文本模型" width="520px" append-to-body>
      <div v-loading="catalogLoading">
        <p class="model-dialog-tip">平台按渠道健康状态自动降级选择文本模型，当前可用文本模型：</p>
        <el-empty
          v-if="!catalogLoading && !llmModels.length"
          description="目录拉取失败或暂无可用文本模型"
          :image-size="60"
        />
        <div v-for="m in llmModels" :key="m.id" class="model-row">
          <span class="model-name">{{ m.name }}</span>
          <el-tag v-if="m.is_default" size="small" type="success" effect="plain">默认</el-tag>
          <el-tag
            size="small"
            effect="light"
            :type="m.health === 'healthy' ? 'success' : m.health === 'down' ? 'danger' : 'warning'"
          >
            {{ ({ healthy: '健康', degraded: '降级', down: '不可用' } as Record<string, string>)[m.health] ?? m.health }}
          </el-tag>
          <span v-if="m.scene_desc" class="model-desc">{{ m.scene_desc }}</span>
        </div>
        <el-alert
          type="info"
          :closable="false"
          class="model-dialog-tip"
          title="如需指定渠道：在模型配置页添加自有文本渠道（选择健康渠道），返回本页重试即可生效"
        />
      </div>
      <template #footer>
        <el-button @click="modelDialogVisible = false">取消</el-button>
        <el-button @click="router.push('/models')">打开模型配置页</el-button>
        <el-button
          type="primary"
          :loading="generating"
          @click="modelDialogVisible = false; retryGeneration()"
        >
          使用当前模型重试
        </el-button>
      </template>
    </el-dialog>

    <!-- ========== 场景编辑弹窗 ========== -->
    <el-dialog v-model="editVisible" title="编辑场景（JSON）" width="640px" append-to-body>
      <el-input
        v-model="editJsonText"
        type="textarea"
        :rows="16"
        spellcheck="false"
        placeholder='{"scene_no":1,"title":"…","plot":"…","dialogue":[{"character":"…","line":"…"}],"duration_sec":30}'
      />
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingScene" @click="saveSceneEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- ========== AI 助手抽屉 ========== -->
    <el-drawer v-model="assistantVisible" title="AI 剧本助手" size="420px" append-to-body>
      <div class="assistant-grain">
        <span>优化粒度：</span>
        <el-select v-model="granularity" style="width: 160px">
          <el-option label="润色对白" value="润色对白" />
          <el-option label="扩写场景" value="扩写场景" />
          <el-option label="精简全文" value="精简全文" />
        </el-select>
      </div>
      <el-input
        v-model="assistantPrompt"
        type="textarea"
        :rows="3"
        placeholder="补充优化指令（可空），例：对白更口语化"
        class="assistant-prompt"
      />
      <el-button type="primary" :loading="chatting" @click="askAssistant">生成建议</el-button>
      <el-divider />
      <div ref="assistantBox" class="assistant-box">
        <el-empty v-if="!assistantReply && !chatting" description="点击「生成建议」获取 AI 优化建议" />
        <span v-else>{{ assistantReply }}<span v-if="chatting" class="cursor">▍</span></span>
      </div>
      <el-alert
        class="assistant-tip"
        type="info"
        :closable="false"
        title="以上为建议内容，请手动采纳到剧本中（本助手不会直接改写剧本）"
      />
    </el-drawer>
    <!-- ========== 版本历史抽屉（V-01：快照列表 + 回退） ========== -->
    <el-drawer v-model="versionVisible" title="版本历史" size="480px" append-to-body>
      <div v-loading="snapshotsLoading">
        <el-empty
          v-if="!snapshotsLoading && snapshotRows.length === 0"
          description="暂无版本快照（在关键阶段会自动创建）"
        />
        <div v-for="snap in snapshotRows" :key="snap.id" class="snapshot-item">
          <div class="snapshot-head">
            <el-tag size="small" :type="snap.stage === 'pre_rollback' ? 'info' : 'primary'">
              {{ stageLabel(snap.stage) }}
            </el-tag>
            <span class="snapshot-time">{{ formatSnapshotTime(snap.created_at) }}</span>
          </div>
          <p class="snapshot-summary">{{ summarizeSnapshot(snap) }}</p>
          <div class="snapshot-actions">
            <el-button
              size="small"
              type="warning"
              plain
              :loading="rollingBackId === snap.id"
              @click="handleRollback(snap)"
            >
              回退此版本
            </el-button>
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.script-view {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-5);
}
.view-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.page-title {
  margin: 0;
}
.ver-tag,
.dur-tag {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
/* 版本历史入口按钮推到页头最右（view-head 为 flex 布局） */
.ver-btn {
  margin-left: auto;
}
/* 版本历史抽屉内的快照卡片 */
.snapshot-item {
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  margin-bottom: 12px;
}
.snapshot-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.snapshot-time {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.snapshot-summary {
  font-size: 13px;
  color: var(--el-text-color-regular);
  margin: 8px 0;
}
.snapshot-actions {
  text-align: right;
}
.card-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.head-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.head-ops {
  margin-left: auto;
}
.input-actions {
  margin-top: var(--mf-sp-4);
  text-align: right;
}
.two-col {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(320px, 1.6fr);
  gap: var(--mf-sp-5);
  align-items: start;
}
.raw-text {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--mf-text-2);
  min-height: 96px;
  max-height: 300px;
  overflow-y: auto;
}
.col-left__ops {
  margin-top: var(--mf-sp-3);
  text-align: right;
}
/* S-09 生成失败三路径面板（重试 / 换模型 / 精简输入） */
.fail-panel {
  margin-bottom: var(--mf-sp-3);
  border: 1px solid var(--el-color-danger-light-7);
}
.fail-title {
  font-weight: var(--mf-fw-semibold);
  color: var(--el-color-danger);
  margin-bottom: 4px;
}
.fail-msg {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.6;
  word-break: break-word;
}
.fail-actions {
  display: flex;
  gap: var(--mf-sp-2);
  margin-top: var(--mf-sp-2);
}
.fail-stats {
  margin-top: var(--mf-sp-2);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
/* S-09 换模型弹窗内文本模型行 */
.model-dialog-tip {
  margin: 0 0 var(--mf-sp-2);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.model-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  padding: var(--mf-sp-2) 0;
  border-bottom: 1px dashed var(--mf-border);
}
.model-name {
  font-weight: var(--mf-fw-semibold);
}
.model-desc {
  margin-left: auto;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
  text-align: right;
}
.stream-box {
  min-height: 160px;
  max-height: 420px;
  overflow-y: auto;
  padding: var(--mf-sp-3);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-surface-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.8;
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
.raw-alert {
  margin-bottom: var(--mf-sp-3);
}
.raw-pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--mf-font);
}
.structure {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: var(--mf-sp-5);
  align-items: start;
}
.nav-title {
  font-weight: var(--mf-fw-semibold);
  margin-bottom: var(--mf-sp-1);
}
.nav-logline {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  margin-bottom: var(--mf-sp-3);
}
.nav-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--mf-sp-2) 0;
  border-bottom: 1px dashed var(--mf-border);
}
.nav-act {
  font-weight: var(--mf-fw-semibold);
  color: var(--mf-brand);
  font-size: var(--mf-fs-sm);
}
.nav-summary {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.nav-scene {
  display: block;
  padding: var(--mf-sp-2) 0;
  color: var(--mf-text-2);
  text-decoration: none;
  font-size: var(--mf-fs-sm);
  border-bottom: 1px dashed var(--mf-border);
}
.nav-scene:hover {
  color: var(--mf-brand);
}
.scenes-col {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-4);
}
.scene-no {
  font-weight: var(--mf-fw-semibold);
  color: var(--mf-brand);
}
.scene-title {
  font-weight: var(--mf-fw-semibold);
}
.scene-card .card-head .el-button {
  margin-left: auto;
}
.scene-plot {
  margin: 0 0 var(--mf-sp-3);
  color: var(--mf-text-2);
  line-height: 1.8;
}
.dlg-table {
  width: 100%;
}
.health-card .issue-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  padding: var(--mf-sp-2) 0;
  border-bottom: 1px dashed var(--mf-border);
  cursor: pointer;
}
.issue-row:hover {
  color: var(--mf-brand);
}
.issue-msg {
  flex: 1;
}
.issue-loc {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
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
.confirm-btn {
  background: var(--mf-brand);
  border-color: var(--mf-brand);
  color: var(--mf-bg);
}
.confirm-btn:hover {
  opacity: 0.9;
}
.assistant-grain {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-3);
}
.assistant-prompt {
  margin-bottom: var(--mf-sp-3);
}
.assistant-box {
  min-height: 180px;
  max-height: 400px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.8;
}
.assistant-tip {
  margin-top: var(--mf-sp-3);
}
</style>
