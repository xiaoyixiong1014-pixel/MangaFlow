<!--
  剧本工作台 · 卡片① 创意 · 故事设定（文件级注释，redesign-script-workspace Task 1）。

  原「步骤 1 创意页」按 P1 冗余剥离方案减负（对应原型 S-02，路由 /projects/:id/script
  三栏工作台的卡片①内容）：
  - 原「原始输入 / AI 生成」左右双栏合并为**单一可折叠输入区**（el-collapse）：
    无剧本（录入态）默认展开，已有剧本默认收起（生成后收起）；区内保留创意
    文本编辑（三 Tab 录入 / 原始输入回显与修改）与快速模式开关（次要入口）；
  - 生成主链路保留：generateStream（POST SSE）增量追加流式文本（打字机光标 +
    自动滚动），支持 AbortController 中断；error 事件展示失败三路径面板
    （S-09：重试 / 换模型 / 精简输入），done 后刷新剧本详情；
  - 已删除（对应 spec REMOVED Requirements）：
    · 大纲导航块与场景卡列表块（后续任务在分集大纲 / 剧本正文卡片重建；
      剧本数据拉取与解析逻辑保留，供 hasContent 判断与确认剧本二次确认）；
    · AI 助手抽屉整段（含「本助手不会直接改写剧本」文案），由右侧常驻
      AssistantPanel 替代；
    · 工具行（版本历史抽屉 / 导出下拉），已上移 ScriptView 顶部通栏；
  - 保留：剧本体检面板（S-08 一键同步 Agent / 画布投递）、快速模式出口
    「确认剧本，进入实体提取」、精细流程「下一步」emit('next') 顺序推进、
    S-09 换模型弹窗。
  说明：本组件常驻挂载于容器卡片①；切走页面（页面级 keep-alive deactivated）
  或卸载时中止进行中的流式请求。
-->
<script setup lang="ts">
import { computed, h, nextTick, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadRequestOptions } from 'element-plus'
import * as studioApi from '@/api/studio'
import * as pipelineApi from '@/api/scriptPipeline'
import type { NovelChapterOut } from '@/api/scriptPipeline'
import { modelsCatalog } from '@/api/catalog'
import { useProjectStore } from '@/stores/project'
import type { LogicalModel, ScriptContent, ScriptOut } from '@/types'

/** 组件入参 */
const props = defineProps<{
  /** 当前项目 ID（取容器路由参数，剧本接口路径参数） */
  projectId: number
}>()

/** 组件事件 */
const emit = defineEmits<{
  /** 点击「下一步」，通知容器滚动定位到卡片②分集大纲（Task 3 故事设定卡接管确认后调整） */
  next: []
}>()

const router = useRouter()
const projectStore = useProjectStore()

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

/** 解析后的场景条目（宽松结构，兼容 LLM 输出；仅用于 hasContent 判断与确认校验） */
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
 * 把后端 content 统一解析为可判定结构。
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

// ============================================================
// 可折叠输入区（双栏合并后的单一录入区；生成前展开、生成后收起）
// ============================================================

/** 折叠面板展开项（'input' 在数组内 = 展开） */
const inputOpen = ref<string[]>([])

/**
 * 按录入态同步折叠区展开状态：无剧本（录入态）展开，已有剧本收起。
 * @returns 无
 */
function syncInputFold(): void {
  inputOpen.value = noScript.value ? ['input'] : []
}

/**
 * 拉取剧本详情并解析（无剧本行 404 时进入录入空态）。
 * S-06：按当前选中剧集过滤（默认剧集 = 不传 episode_id，兼容存量 NULL 行）。
 * @returns 无
 * @throws 接口错误已由 client 拦截器统一提示
 */
async function loadScript(): Promise<void> {
  loading.value = true
  try {
    script.value = await studioApi.getInput(props.projectId, currentEpisodeId.value ?? undefined)
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
    syncInputFold()
  }
}

// 切换剧集（S-06）后按新剧集重新加载剧本
watch(currentEpisodeId, () => {
  void loadScript()
})

// ============================================================
// 快速模式开关（折叠区内的次要入口；跳过角色与分集确认的旧链路分支）
// ============================================================

/** 快速模式：跳过角色与分集确认，剧本完成后直达实体提取 */
const quickMode = ref(false)

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
 * 保存原始输入，成功后收起折叠区、进入生成视图。
 * @returns 无
 * @throws 接口错误已由 client 拦截器统一提示
 */
async function saveInput(): Promise<void> {
  if (!inputText.value.trim()) {
    ElMessage.warning('请先输入内容')
    return
  }
  savingInput.value = true
  try {
    script.value = await studioApi.saveInput(props.projectId, {
      mode: inputMode.value,
      raw_text: inputText.value.trim(),
      // S-06：绑定当前剧集（默认剧集 = 不传，后端落 NULL 行）
      episode_id: currentEpisodeId.value ?? undefined,
    })
    noScript.value = false
    parsed.value = normalizeScript(script.value.content)
    syncInputFold()
    ElMessage.success('原始输入已保存，可开始 AI 生成')
  } finally {
    savingInput.value = false
  }
}

// ============================================================
// 小说上传解析（P-7 小说改编：txt/epub → 章节列表 → 按章节分集）
// ============================================================

/** 小说解析中 */
const novelParsing = ref(false)
/** 解析出的章节列表（内存态展示；暂存于项目 config） */
const novelChapters = ref<NovelChapterOut[]>([])
/** 按章节生成分集大纲中 */
const planFromChaptersLoading = ref(false)

/**
 * 上传小说文件并解析章节（el-upload 自定义上传）。
 * 成功后展示章节列表，原始文本由后端回写剧本 raw_text。
 * @param options el-upload 上传参数（含 file）
 * @returns 无
 * @throws 接口错误已由 client 拦截器统一提示
 */
async function handleNovelUpload(options: UploadRequestOptions): Promise<void> {
  novelParsing.value = true
  try {
    const out = await pipelineApi.parseNovel(props.projectId, options.file)
    novelChapters.value = out.chapters
    ElMessage.success(`已解析 ${out.chapter_count} 个章节，可一键按章节生成分集大纲`)
    // 后端已把拼接全文回写剧本 raw_text，刷新详情以展示
    await loadScript()
  } catch {
    // 业务错误已由 client 拦截器统一提示
  } finally {
    novelParsing.value = false
  }
}

/**
 * 按小说章节生成分集大纲（plan from_chapters 分支）。
 * 需已完成角色设定确认，否则后端 409（拦截器提示）。
 * @returns 无
 * @throws 409 未确认角色 / 未解析章节由拦截器提示
 */
async function generatePlanFromChapters(): Promise<void> {
  if (planFromChaptersLoading.value) return
  planFromChaptersLoading.value = true
  try {
    await pipelineApi.generateEpisodePlan(props.projectId, { from_chapters: true })
    ElMessage.success('已按章节生成分集大纲，请在卡片②「分集大纲」中查看与确认')
  } catch {
    // 409 未确认角色 / 未解析章节等由拦截器提示
  } finally {
    planFromChaptersLoading.value = false
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
 * @throws 用户中断不提示；其余错误 ElMessage 提示
 */
async function startGenerate(): Promise<void> {
  if (generating.value) return // 防重复点击
  streamText.value = ''
  genError.value = ''
  generating.value = true
  abortCtrl = new AbortController()
  try {
    await studioApi.generateStream(
      props.projectId,
      {},
      (event, data) => {
        const d = (data ?? {}) as Record<string, unknown>
        if (event === 'delta' && typeof d.text === 'string') {
          streamText.value += d.text
        } else if (event === 'error') {
          // 生成失败：丢弃流式半成品，仅保留失败三路径面板提示
          genError.value = typeof d.message === 'string' ? d.message : '生成过程出现异常'
          streamText.value = ''
        } else if (event === 'done') {
          // S-09：成功后面板自动隐藏（genError 清空）+ 刷新详情；
          // 清空流式文本，改由「查看生成原文」折叠面板承载展示
          genError.value = ''
          streamText.value = ''
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
 * @throws 目录拉取失败时弹窗内展示空态，不阻断重试路径
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
  inputOpen.value = ['input']
  genError.value = ''
  void nextTick(() => inputAreaRef.value?.focus())
  ElMessage.info(
    raw.length > INPUT_LIMIT
      ? `已保留开头 ${INPUT_LIMIT} 字（原输入 ${raw.length} 字），建议截取核心章节后保存并重新生成`
      : '原始输入未超长，已带出录入框，可调整后重新保存并生成',
  )
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
 * @throws 接口错误已由 client 拦截器统一提示
 */
async function runHealth(): Promise<void> {
  if (healthLoading.value) return
  healthLoading.value = true
  try {
    const report = await studioApi.scriptHealth(props.projectId)
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

// ============================================================
// 确认剧本（快速模式出口：进入实体提取页）
// ============================================================

/**
 * 确认剧本，进入实体提取页。
 * @returns 无
 * @throws 无结构化场景时先弹二次确认，用户取消则中止
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
  await router.push(`/projects/${props.projectId}/script/extract`)
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
 * @throws 接口错误已由 client 拦截器统一提示
 */
async function sendToAgent(): Promise<void> {
  if (syncingAgent.value) return
  syncingAgent.value = true
  try {
    const res = await studioApi.sendScriptToAgent(props.projectId, {
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
 * @throws 接口错误已由 client 拦截器统一提示
 */
function sendToCanvas(): void {
  if (syncingCanvas.value) return
  syncingCanvas.value = true
  studioApi
    .sendScriptToCanvas(props.projectId, {
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

/**
 * 中止本组件全部进行中的流式请求（keep-alive 切走 / 卸载时调用）。
 * @returns 无
 */
function abortAllStreams(): void {
  abortCtrl?.abort()
}

onMounted(loadScript)
// keep-alive 切走时中止流式请求（deactivated 不触发 onBeforeUnmount）
onDeactivated(abortAllStreams)
// keep-alive 切回时若剧本尚未加载过（首挂载被中断），兜底刷新
onActivated(() => {
  if (!script.value && !noScript.value && !loading.value) void loadScript()
})
onBeforeUnmount(abortAllStreams)
</script>

<template>
  <div class="step-idea">
    <!-- 首屏骨架 -->
    <el-skeleton v-if="loading" :rows="8" animated />

    <template v-else>
      <!-- ========== 单卡：可折叠输入区 + AI 生成主链路 ========== -->
      <el-card shadow="never" class="idea-card">
        <template #header>
          <div class="card-head">
            <span>创意 · 故事设定</span>
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

        <!-- 可折叠输入区：无剧本（录入态）默认展开，已有剧本收起；
             快速模式开关移入折叠区作为次要入口 -->
        <el-collapse v-model="inputOpen" class="input-fold">
          <el-collapse-item name="input">
            <template #title>
              <span class="fold-title">创作输入（原始素材）</span>
            </template>
            <p v-if="!quickMode" class="guide-text">
              输入创意素材并 AI 生成剧本；确认后进入「分集大纲」「剧本正文」卡片逐步定稿。
            </p>
            <p v-else class="guide-text guide-text--quick">
              快速模式：跳过角色与分集确认，剧本完成后直接「确认剧本」进入实体提取。
            </p>

            <!-- 无剧本：三 Tab 快速录入 -->
            <template v-if="noScript">
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
                  <!-- P-7 小说上传解析：txt/epub → 章节列表 → 按章节生成分集大纲 -->
                  <div class="novel-upload">
                    <el-upload
                      :show-file-list="false"
                      accept=".txt,.epub"
                      :http-request="handleNovelUpload"
                    >
                      <el-button size="small" :loading="novelParsing">
                        上传小说文件（.txt / .epub，≤20MB）
                      </el-button>
                    </el-upload>
                    <span class="novel-hint">上传后自动切分章节，可一键按章节生成分集大纲</span>
                  </div>
                  <div v-if="novelChapters.length" class="novel-chapters">
                    <div class="novel-chapters__head">
                      <span>已解析 {{ novelChapters.length }} 个章节</span>
                      <el-button
                        size="small"
                        type="primary"
                        :loading="planFromChaptersLoading"
                        @click="generatePlanFromChapters"
                      >
                        按章节生成分集大纲
                      </el-button>
                    </div>
                    <div v-for="c in novelChapters" :key="c.index" class="novel-chapter">
                      <span class="nc-index">第{{ c.index }}章</span>
                      <span class="nc-title">{{ c.title }}</span>
                      <span class="nc-count">{{ c.char_count }} 字</span>
                    </div>
                  </div>
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
            </template>

            <!-- 已有剧本：原始输入只读回显 + 修改入口 -->
            <template v-else>
              <div class="raw-text">{{ script?.raw_text || '（无原始输入）' }}</div>
              <div class="col-left__ops">
                <el-button
                  text
                  type="primary"
                  @click="noScript = true; inputText = script?.raw_text ?? ''; inputOpen = ['input']"
                >
                  修改输入
                </el-button>
              </div>
            </template>

            <!-- 快速模式开关（次要入口） -->
            <div class="quick-row">
              <el-switch v-model="quickMode" active-text="快速模式" inactive-text="精细流程" inline-prompt />
            </div>
          </el-collapse-item>
        </el-collapse>

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
          description="还没有剧本内容，点击右上角「开始生成」，AI 将根据创作输入流式创作"
        />

        <!-- 结构化已生成提示：原场景卡列表已按 spec 迁出，
             场景明细将在「分集大纲 / 剧本正文」卡片中查看与编辑 -->
        <el-alert
          v-if="parsed && !parsed.isRaw && hasContent"
          class="structured-tip"
          type="info"
          show-icon
          :closable="false"
          title="剧本已生成结构化内容；场景明细将在「分集大纲 / 剧本正文」卡片中查看与编辑"
        />

        <!-- 生成原文（默认收起）：原始输出（JSON/文本）收进此折叠面板 -->
        <el-collapse v-if="!generating && hasContent" class="raw-viewer">
          <el-collapse-item title="查看生成原文（原始输出）" name="raw">
            <pre class="raw-pre">{{ script?.raw_text || '（无原始输出）' }}</pre>
          </el-collapse-item>
        </el-collapse>

        <!-- 纯文本形态：结构化解析未通过，保留原文展示与重生成引导 -->
        <template v-if="parsed && parsed.isRaw">
          <el-alert
            class="raw-alert"
            type="info"
            show-icon
            :closable="false"
            title="当前剧本为纯文本形态（结构化解析未通过），请重新生成以获得场景结构"
          />
          <pre class="raw-pre raw-pre--plain">{{ parsed.rawText }}</pre>
        </template>
      </el-card>

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
        <div v-for="(iss, i) in healthIssues" :key="i" class="issue-row">
          <el-tag size="small" :type="iss.level === 'error' ? 'danger' : 'warning'">
            {{ iss.level === 'error' ? '错误' : '提醒' }}
          </el-tag>
          <span class="issue-msg">{{ iss.message }}</span>
          <span class="issue-loc">{{ iss.location }}</span>
        </div>
      </el-card>

      <!-- ========== 底部操作条 ========== -->
      <div class="footer-bar">
        <div class="footer-left">
          <el-button :loading="healthLoading" @click="runHealth">剧本体检</el-button>
          <!-- S-08 剧本一键同步：Agent 会话（幂等，跳转工作台续聊）/ 画布投递（幂等） -->
          <el-button :disabled="!hasContent" :loading="syncingAgent" @click="sendToAgent">
            同步到 Agent
          </el-button>
          <el-button :disabled="!hasContent" :loading="syncingCanvas" @click="sendToCanvas">
            投递画布
          </el-button>
        </div>
        <div class="footer-right">
          <!-- 精细流程（默认）：顺序推进到卡片②分集大纲（容器滚动定位） -->
          <el-button v-if="!quickMode" size="large" class="confirm-btn" @click="emit('next')">
            下一步：分集大纲
          </el-button>
          <!-- 快速模式出口（旧链路保留）：确认剧本直达实体提取 -->
          <el-button
            size="large"
            :class="quickMode ? 'confirm-btn' : 'quick-exit-btn'"
            @click="confirmScript"
          >
            确认剧本，进入实体提取
          </el-button>
        </div>
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
  </div>
</template>

<style scoped>
.step-idea {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-5);
}
.card-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.head-ops {
  margin-left: auto;
}
/* 可折叠输入区（双栏合并后的单一录入区） */
.input-fold {
  margin-bottom: var(--mf-sp-3);
  border-top: none;
  border-left: none;
  border-right: none;
}
.fold-title {
  font-weight: var(--mf-fw-semibold);
}
.guide-text {
  margin: 0 0 var(--mf-sp-3);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.6;
}
.guide-text--quick {
  color: var(--el-color-warning);
}
.input-actions {
  margin-top: var(--mf-sp-4);
  text-align: right;
}
/* 快速模式开关（次要入口） */
.quick-row {
  margin-top: var(--mf-sp-3);
  padding-top: var(--mf-sp-3);
  border-top: 1px dashed var(--mf-border);
}
/* 原始输入只读回显 */
.raw-text {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--mf-text-2);
  min-height: 64px;
  max-height: 240px;
  overflow-y: auto;
}
.col-left__ops {
  margin-top: var(--mf-sp-2);
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
  margin-top: var(--mf-sp-3);
  margin-bottom: var(--mf-sp-3);
}
.raw-pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--mf-font);
}
.raw-pre--plain {
  max-height: 420px;
  overflow-y: auto;
}
/* 结构化已生成提示 */
.structured-tip {
  margin-top: var(--mf-sp-3);
}
/* 生成原文折叠面板（默认收起，消除原始 JSON 残留观感） */
.raw-viewer {
  margin-top: var(--mf-sp-3);
}
/* P-7 小说上传解析区 */
.novel-upload {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  margin-bottom: var(--mf-sp-3);
}
.novel-hint {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.novel-chapters {
  margin-bottom: var(--mf-sp-3);
  padding: var(--mf-sp-3);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
}
.novel-chapters__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--mf-sp-2);
  font-weight: var(--mf-fw-semibold);
}
.novel-chapter {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  padding: 4px 0;
  border-bottom: 1px dashed var(--mf-border);
  font-size: var(--mf-fs-sm);
}
.novel-chapter:last-child {
  border-bottom: none;
}
.nc-index {
  flex-shrink: 0;
  color: var(--mf-brand);
}
.nc-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.nc-count {
  flex-shrink: 0;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
/* 体检结果面板 */
.health-card .issue-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  padding: var(--mf-sp-2) 0;
  border-bottom: 1px dashed var(--mf-border);
}
.issue-row:last-child {
  border-bottom: none;
}
.issue-msg {
  flex: 1;
}
.issue-loc {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
/* 底部操作条 */
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
.footer-right {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
/* 非快速模式下旧出口按钮弱化为普通按钮 */
.quick-exit-btn {
  color: var(--mf-text-2);
}
.confirm-btn {
  background: var(--mf-brand);
  border-color: var(--mf-brand);
  color: var(--mf-bg);
}
.confirm-btn:hover {
  opacity: 0.9;
}
</style>
