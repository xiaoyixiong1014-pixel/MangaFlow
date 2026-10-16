<!--
  剧本创作页 · 右侧 AI 助手常驻面板（文件级注释，redesign-script-workspace Task 1 骨架；
  Task 6 完整实现对话式共创，P4 对话式改写通道 B 档）。

  对话式共创主链路（POST /script/assist 预览 → diff 回执 → /script/assist/apply 采纳落库）：
  - 聚焦联动：聚焦对象由容器（ScriptView）按中部卡片选中态计算后经 props.focus 下发，
    切换时追加「已切换聚焦对象」分隔消息（首轮对话前静默）；scope=setting 不传 target；
  - 对话流：消息数组（user 指令 / assistant 回执 / system 提示）。发送指令 → assistPreview
    → assistant 消息渲染「diff 回执卡片」：summary 概括 + 逐片段卡片（diff_lines 红删绿增、
    复选框默认全选、reason 次文案）+ 底部「采纳 / 全部采纳 / 拒绝」；预览失败渲染可重试
    错误消息；preview loading 显示内联气泡；
  - 采纳：勾选片段回传 accepts → assistApply → 成功追加系统消息（skipped>0 提示因内容
    已变化跳过）、emit('applied') 通知容器刷新对应卡片、刷新面板内历史版本列表；
  - 快捷 Chips 三组（方案 §6.2 原文文案）：按作用域过滤（剧情/台词/节奏），点击即以该
    文案为指令直接发送（填入并发送一次完成）；
  - 历史版本：面板底部折叠区，最近 10 条快照（时间 + stage 中文标签 + 回退 + 对比）；
    回退复用 /versions/{id}/rollback（服务端自动写「回退前存档」快照，可再次回退撤销），
    成功后 emit('applied', {})（scope 缺省 → 容器刷新全部卡片）；「对比」弹窗展示该快照
    摘要 vs 当前流水线摘要（GET /script/pipeline/status 的 summary，复用既有接口不新增协议）。
-->
<script setup lang="ts">
import { computed, inject, nextTick, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Promotion, ChatDotRound, Close } from '@element-plus/icons-vue'
import * as pipelineApi from '@/api/scriptPipeline'
import type {
  AssistApplyOut,
  AssistPreviewOut,
  AssistScope,
  AssistantAppliedPayload,
  AssistantFocus,
  OptimizeChange,
  QuoteScope,
} from '@/api/scriptPipeline'
import { rollbackSnapshot, snapshots as snapshotsApi } from '@/api/projects'
import { EMPTY_QUOTE_BRIDGE, SCRIPT_QUOTE_KEY } from './quoteBridge'
import type { ScriptQuoteBridge, ScriptQuoteItem } from './quoteBridge'
import type { VersionSnapshot } from '@/types'

/** 组件入参 */
const props = defineProps<{
  /** 当前项目 ID（assist / snapshots / rollback 等接口路径参数） */
  projectId: number
  /** 聚焦对象（容器按中部卡片选中态计算下发；scope=setting 时 target 省略） */
  focus: AssistantFocus
}>()

/** 组件事件 */
const emit = defineEmits<{
  /**
   * 采纳/回退完成（容器刷新对应卡片）：
   * payload.scope 缺省（回退快照等全项目动作）时容器刷新全部已解锁卡片。
   */
  applied: [payload: AssistantAppliedPayload]
}>()

// ============================================================
// 选中文本引用（工作台「添加对话中」→ quote-rewrite 优化）
// ============================================================

/** 引用桥（容器 provide；缺省空桥静默） */
const quoteBridge = inject(SCRIPT_QUOTE_KEY, EMPTY_QUOTE_BRIDGE)

/** 当前引用队列（桥内 ref 的模板解包视图） */
const quotes = computed(() => quoteBridge.quotes.value)

/** 发送中捕获的引用快照（发起请求时冻结，避免预览期间引用变化导致错位） */
interface FrozenQuote {
  /** 来源展示标签 */
  source: string
  /** 引用作用域 */
  scope: QuoteScope
  /** 目标剧集 ID（可选） */
  targetEpisodeId: number | null
  /** 选中的原文 */
  text: string
}

// ============================================================
// 快捷 Chips（方案 §6.2 原文文案）
// ============================================================

/**
 * 快捷指令 Chips 分组与作用域过滤规则（写在常量上方，方案 §6.2）：
 * - scope=setting → 只显示剧情组（设定层不涉台词/节奏，台词组/节奏组文案不适配，
 *   剧情组文案适用于设定三段的整体调整）；
 * - scope=episode_outline → 剧情组 + 节奏组（大纲层涉剧情走向与单集时长/节奏，
 *   台词组文案需正文对白上下文，不适配）；
 * - scope=episode_body → 三组全显（正文层剧情/台词/节奏均可改）。
 */
const CHIP_GROUPS: Array<{
  /** 分组 key */
  key: string
  /** 分组标签 */
  label: string
  /** 预置指令文案（方案 §6.2 原文） */
  items: string[]
  /** 可见作用域白名单 */
  scopes: AssistScope[]
}> = [
  {
    key: 'plot',
    label: '剧情',
    items: [
      '第1集冲突太弱，提前安排反派出场',
      '增加男女主的甜蜜互动',
      '提高结尾悬念强度',
    ],
    scopes: ['setting', 'episode_outline', 'episode_body'],
  },
  {
    key: 'dialogue',
    label: '台词',
    items: [
      '台词太书面了，改得更像日漫那种傲娇语气',
      '把男主台词改得更有都市精英感',
      '台词更短，适合竖屏快节奏',
    ],
    scopes: ['episode_body'],
  },
  {
    key: 'pace',
    label: '节奏',
    items: [
      '180 秒太短，结尾留个大悬念',
      '节奏太快了，多加点环境描写',
      '单集时长压到 60 秒内',
    ],
    scopes: ['episode_outline', 'episode_body'],
  },
]

/** 当前聚焦作用域下可见的 Chips 分组（按上方过滤规则计算） */
const visibleChipGroups = computed(() =>
  CHIP_GROUPS.filter((g) => g.scopes.includes(props.focus.scope)),
)

// ============================================================
// 对话消息流
// ============================================================

/** 对话消息（user 用户指令 / assistant diff 回执或文本 / system 分隔与结果提示） */
interface ChatMessage {
  /** 唯一 key（自增） */
  id: number
  /** 角色：user 用户 / assistant 助手 / system 系统 */
  role: 'user' | 'assistant' | 'system'
  /** 内容形态：text 纯文本 / diff 回执卡片 / quote 优化文本 / loading 内联加载 / error 可重试错误 */
  kind: 'text' | 'diff' | 'quote' | 'loading' | 'error'
  /** 文本内容（user 指令 / system 提示 / error 文案 / quote 优化结果文本） */
  text: string
  /** diff 回执数据（kind=diff 时） */
  preview?: AssistPreviewOut
  /** 片段勾选状态（片段 id → 是否勾选，默认全选；kind=diff 时） */
  checked?: Record<string, boolean>
  /** 采纳中（kind=diff 时按钮 loading 与防重入） */
  applying?: boolean
  /** 发起预览时的作用域（apply 回传，避免聚焦切换后错位） */
  scope?: AssistScope
  /** 发起预览时的目标剧集 ID（scope=setting 为 null） */
  target?: number | null
  /** 可重试的原指令（kind=error 时） */
  retryInstruction?: string
  /** 引用来源标签列表（kind=quote 时展示「来自 xxx」） */
  quoteSources?: string[]
  /** 复制中（kind=quote 时按钮 loading） */
  copying?: boolean
}

/** 对话消息列表 */
const messages = ref<ChatMessage[]>([])
/** 消息自增 ID */
let msgSeq = 0
/** 输入框内容（≤500 字） */
const input = ref('')
/** 预览/采纳请求进行中（禁用重复发送） */
const sending = ref(false)
/** 消息滚动容器引用（新消息后滚到底部） */
const msgAreaRef = ref<HTMLElement | null>(null)

/**
 * 追加一条消息并在下一帧滚动到底部。
 * @param msg 消息内容（不含 id，自动分配）
 * @returns 新增的消息对象（便于后续原位替换）
 */
function pushMessage(msg: Omit<ChatMessage, 'id'>): ChatMessage {
  const full: ChatMessage = { ...msg, id: ++msgSeq }
  messages.value.push(full)
  void nextTick(() => {
    if (msgAreaRef.value) msgAreaRef.value.scrollTop = msgAreaRef.value.scrollHeight
  })
  return full
}

/**
 * 移除指定消息（diff 卡片采纳/拒绝后移除，避免对已失效片段重复采纳）。
 * @param id 目标消息 ID
 * @returns 无
 */
function removeMessage(id: number): void {
  messages.value = messages.value.filter((m) => m.id !== id)
}

/**
 * 勾选/取消某片段（diff 卡片复选框回调）。
 * @param msg 目标 diff 消息
 * @param changeId 片段 ID
 * @param val 勾选值
 * @returns 无
 */
function setChecked(msg: ChatMessage, changeId: string, val: boolean): void {
  if (msg.checked) msg.checked[changeId] = val
}

/**
 * 取消息中勾选的片段列表。
 * @param msg 目标 diff 消息
 * @returns 勾选的片段数组（全不勾选时为空）
 */
function pickedChanges(msg: ChatMessage): OptimizeChange[] {
  return (msg.preview?.changes ?? []).filter((c) => msg.checked?.[c.id])
}

// ============================================================
// 发送指令 → assistPreview 预览
// ============================================================

/**
 * 发送指令（输入框发送按钮 / Chips 点击 / 错误重试共用入口）：
 * 前置校验（空指令不发、episode 两作用域未选中集提示）→ 追加 user 消息与
 * loading 气泡 → assistPreview → 成功替换为 diff 回执卡片（片段默认全选；
 * count=0 时替换为引导文本）→ 失败替换为可重试错误消息。
 * @param rawText 可选直接传入的指令文本（Chips/重试入口）；缺省取输入框内容
 * @returns 无
 * @throws 接口错误由 client 拦截器统一弹后端 detail（409/422/502），此处转为可重试错误消息
 */
async function send(rawText?: string): Promise<void> {
  const instruction = (rawText ?? input.value).trim()
  if (!instruction || sending.value) return
  // 发送分流：引用区非空 → 走「选中文本优化」链路（quote-rewrite，输出可复制文本）
  if (quoteBridge.quotes.value.length) {
    await sendQuoteRewrite(instruction)
    return
  }
  // 发送前置校验：episode 两作用域必须已选中集（聚焦 label 由容器置「未选择集」）
  if (props.focus.scope !== 'setting' && (props.focus.target == null)) {
    ElMessage.warning('请先在中部卡片选择一集，再发送改写指令')
    return
  }
  // 捕获发起时刻的聚焦对象：apply 回传用，避免预览期间聚焦切换导致目标错位
  const scope = props.focus.scope
  const target = props.focus.target ?? null
  input.value = ''
  pushMessage({ role: 'user', kind: 'text', text: instruction })
  const loadingMsg = pushMessage({
    role: 'assistant', kind: 'loading',
    text: scope === 'setting' ? 'AI 正在分析故事设定…' : 'AI 正在分析本集内容…',
  })
  sending.value = true
  try {
    const out = await pipelineApi.assistPreview(props.projectId, {
      scope,
      target: target ?? undefined,
      instruction,
    })
    removeMessage(loadingMsg.id)
    if (!out.count) {
      pushMessage({
        role: 'assistant', kind: 'text',
        text: out.summary || '本轮未产生可执行的修改建议，请换个说法或补充更具体的调整方向。',
      })
      return
    }
    pushMessage({
      role: 'assistant', kind: 'diff', text: '',
      preview: out,
      checked: Object.fromEntries(out.changes.map((c) => [c.id, true])),
      scope,
      target,
    })
  } catch {
    removeMessage(loadingMsg.id)
    pushMessage({
      role: 'assistant', kind: 'error',
      text: '预览失败，接口错误详情已由全局提示展示',
      retryInstruction: instruction,
    })
  } finally {
    sending.value = false
  }
}

// ============================================================
// 选中文本优化（引用 + 优化方向 → 可复制优化文本）
// ============================================================

/**
 * 发送引用优化：引用区非空时由 send() 分流进入——冻结当前引用快照 → 追加
 * user 消息（指令 + 引用摘要）与 loading → 多段引用以空行拼接调 quoteRewrite
 * → 成功渲染「优化文本」消息（复制按钮，用户自行粘贴回对应位置）并清空引用
 * 区；失败渲染可重试错误消息（引用保留）。
 * @param instruction 用户优化方向描述（非空）
 * @returns 无
 * @throws 接口错误由 client 拦截器统一弹后端 detail（422/502），此处转为可重试错误消息
 */
async function sendQuoteRewrite(instruction: string): Promise<void> {
  // 冻结引用快照（发送后清空引用区，失败时恢复以便重试）
  const frozen: FrozenQuote[] = quoteBridge.quotes.value.map((q: ScriptQuoteItem) => ({
    source: q.source, scope: q.scope, targetEpisodeId: q.targetEpisodeId ?? null, text: q.text,
  }))
  const mergedText = frozen.map((q) => q.text).join('\n\n')
  const firstScope = frozen[0]?.scope ?? 'setting'
  const firstTarget = frozen[0]?.targetEpisodeId ?? null
  const sourcesLabel = [...new Set(frozen.map((q) => q.source))].join('、')
  input.value = ''
  quoteBridge.quotes.value = []
  pushMessage({
    role: 'user', kind: 'text',
    text: `[引用 ${frozen.length} 段 · ${sourcesLabel}]\n${instruction}`,
  })
  const loadingMsg = pushMessage({
    role: 'assistant', kind: 'loading',
    text: 'AI 正在按优化方向改写选中内容…',
  })
  sending.value = true
  try {
    const out = await pipelineApi.quoteRewrite(props.projectId, {
      scope: firstScope,
      target_episode_id: firstTarget,
      quote_text: mergedText,
      instruction,
    })
    removeMessage(loadingMsg.id)
    pushMessage({
      role: 'assistant', kind: 'quote', text: out.optimized_text,
      quoteSources: [...new Set(frozen.map((q) => q.source))],
    })
  } catch {
    removeMessage(loadingMsg.id)
    // 恢复引用（用户可补充指令重试）
    quoteBridge.quotes.value = frozen.map((q, i) => ({
      id: Date.now() + i, source: q.source, scope: q.scope,
      targetEpisodeId: q.targetEpisodeId, text: q.text,
    }))
    pushMessage({
      role: 'assistant', kind: 'error',
      text: '优化失败，接口错误详情已由全局提示展示（引用已恢复，可重试）',
      retryInstruction: instruction,
    })
  } finally {
    sending.value = false
  }
}

/**
 * 复制优化文本到剪贴板（quote 消息「复制」按钮）：成功提示粘贴指引。
 * @param msg 目标 quote 消息
 * @returns 无
 */
async function copyQuoteResult(msg: ChatMessage): Promise<void> {
  if (msg.copying) return
  msg.copying = true
  try {
    await navigator.clipboard.writeText(msg.text)
    ElMessage.success('优化文本已复制，可粘贴回对应位置')
  } catch {
    ElMessage.error('复制失败，请手动选择文本复制')
  } finally {
    msg.copying = false
  }
}

// ============================================================
// 采纳 / 拒绝
// ============================================================

/**
 * 采纳 diff 回执的选中片段（「采纳」按钮）：勾选片段回传 accepts → assistApply
 * → 成功：移除该 diff 卡片、追加系统结果消息、emit applied 通知容器刷新卡片、
 * 刷新历史版本列表；失败：追加错误消息（片段保留可重试）。
 * @param msg 目标 diff 消息
 * @param all 是否全部采纳（「全部采纳」按钮；true 时忽略勾选取全部片段）
 * @returns 无
 * @throws 接口错误由 client 拦截器统一弹后端 detail（409 片段失效/阶段冲突等）
 */
async function applyMessage(msg: ChatMessage, all = false): Promise<void> {
  if (msg.applying) return
  const changes = all ? (msg.preview?.changes ?? []) : pickedChanges(msg)
  if (!changes.length) {
    ElMessage.warning('请先勾选要采纳的修改片段')
    return
  }
  const scope = msg.scope ?? 'setting'
  const target = msg.target ?? null
  msg.applying = true
  try {
    const out: AssistApplyOut = await pipelineApi.assistApply(props.projectId, {
      scope,
      target: target ?? undefined,
      accepts: changes.map((c) => ({
        id: c.id, path: c.path, original: c.original, proposed: c.proposed ?? '',
      })),
    })
    removeMessage(msg.id)
    const extra = out.skipped > 0 ? `，另有 ${out.skipped} 处因内容已变化被跳过` : ''
    const extraSnap = out.snapshot_id != null ? `（版本点 #${out.snapshot_id}）` : ''
    pushMessage({
      role: 'system', kind: 'text',
      text: out.applied > 0
        ? `已应用 ${out.applied} 处修改${extra}${extraSnap}，中部卡片已刷新`
        : '片段均已失效，未应用任何修改（内容可能已变化，请重新发送指令）',
    })
    if (out.applied > 0) {
      emit('applied', { scope, target, phase: out.phase })
      void fetchSnapshots()
    }
  } catch {
    pushMessage({
      role: 'assistant', kind: 'error',
      text: '采纳失败，接口错误详情已由全局提示展示',
    })
  } finally {
    msg.applying = false
  }
}

/**
 * 拒绝 diff 回执（「拒绝」按钮）：移除卡片并留痕系统消息，不调接口。
 * @param msg 目标 diff 消息
 * @returns 无
 */
function rejectMessage(msg: ChatMessage): void {
  removeMessage(msg.id)
  pushMessage({ role: 'system', kind: 'text', text: '已拒绝本轮修改建议' })
}

// ============================================================
// 聚焦联动（focus 变化 → 分隔消息）
// ============================================================

// 聚焦对象变化时追加分隔消息（对话流已有内容才提示，首轮静默）；
// getter 返回新数组引用，回调内先比对三元组，实际未变化则跳过
watch(
  () => [props.focus.scope, props.focus.target, props.focus.label] as const,
  (cur, prev) => {
    if (prev
      && cur[0] === prev[0] && cur[1] === prev[1] && cur[2] === prev[2]) return
    if (messages.value.length) {
      pushMessage({ role: 'system', kind: 'text', text: `已切换聚焦对象：${props.focus.label}` })
    }
  },
)

// ============================================================
// 片段 path → 中文标签（沿用 StepEpisodeBody 的字段口径）
// ============================================================

/** 简单字段路径 → 中文标签映射（setting/episode_outline 两作用域的 path 为字段名） */
const PATH_LABELS: Record<string, string> = {
  logline: '一句话故事',
  core_conflict: '核心冲突',
  worldview: '世界观',
  title: '标题',
  core_event: '核心事件',
  characters: '出场角色',
  emotion_curve: '情绪走向',
}

/**
 * 把片段字段路径转为中文可读标签：
 * - setting/episode_outline：path 即字段名（logline/core_event 等），查表翻译；
 * - episode_body：optimize 口径 scenes[N]….field，场景号按序号 +1 兜底展示。
 * @param path 字段路径
 * @returns 中文标签（无法解析时原样返回路径）
 */
function pathLabel(path: string): string {
  const m = /^scenes\[(\d+)\](?:\.dialogue\[(\d+)\])?\.([A-Za-z_]+)$/.exec(path)
  if (m) {
    const sceneNo = Number(m[1]) + 1
    const fieldKey = m[3] ?? ''
    const field = PATH_LABELS[fieldKey] ?? fieldKey
    if (m[2] != null) return `场景${sceneNo} · 第${Number(m[2]) + 1}句对白`
    return `场景${sceneNo} · ${field}`
  }
  return PATH_LABELS[path] ?? path
}

// ============================================================
// 历史版本（面板底部折叠区）：快照列表 / 回退 / 对比
// ============================================================

/**
 * 快照阶段中文标签映射（面板展示用）：
 * - 任务指定：assist_applied→「AI 协作采纳」、setting_generated→「生成设定」、
 *   setting_edited→「编辑设定」；
 * - 其余沿用已有中文名（VersionHistoryDrawer 四项）与剧本流水线阶段快照中文名；
 * - 未覆盖的 stage 原样显示标识。
 */
const STAGE_LABELS: Record<string, string> = {
  assist_applied: 'AI 协作采纳',
  setting_generated: '生成设定',
  setting_edited: '编辑设定',
  characters_confirmed: '角色设定确认',
  outline_confirmed: '分集大纲确认',
  body_generated: '正文已生成',
  episode_outline_regenerated: '单集大纲重生成',
  extract_confirmed: '资产抽取确认',
  asset_locked: '角色定妆锁定',
  pre_rollback: '回退前存档',
  manual: '手动快照',
  script: '剧本保存',
}

/** 快照摘要/当前摘要的 key → 中文标签（对比弹窗两栏共用） */
const SUMMARY_KEY_LABELS: Record<string, string> = {
  characters: '角色',
  scenes: '场景',
  props: '道具',
  character_count: '角色',
  episode_count: '剧集',
  body_count: '已写正文集数',
}

/** 折叠区展开的面板名（历史版本） */
const histActive = ref<string[]>([])
/** 历史版本加载中 */
const snapshotsLoading = ref(false)
/** 快照列表（最近 10 条，created_at 倒序，后端已排序） */
const snapshotRows = ref<VersionSnapshot[]>([])
/** 是否已加载过快照列表（展开时懒加载，此后采纳/回退后静默刷新） */
const snapshotsLoaded = ref(false)
/** 正在回退的快照 ID（按钮 loading），null 表示无进行中的回退 */
const rollingBackId = ref<number | null>(null)

/**
 * 快照阶段的中文标签。
 * @param stage 阶段标识
 * @returns 中文标签（未知阶段返回原标识）
 */
function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage
}

/**
 * 格式化快照创建时间为本地可读格式（沿用 VersionHistoryDrawer 口径）。
 * @param iso ISO8601 时间串（可空）
 * @returns 形如「2026-10-14 20:30」的串；无效输入原样返回或 '-'
 */
function formatSnapshotTime(iso?: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/**
 * 拉取最近 10 条版本快照（created_at 倒序；失败静默，拦截器已提示）。
 * @returns 无
 * @throws 接口错误已由 client 拦截器统一弹窗提示
 */
async function fetchSnapshots(): Promise<void> {
  snapshotsLoading.value = true
  try {
    const page = await snapshotsApi(props.projectId, { page: 1, page_size: 10 })
    snapshotRows.value = page.items
    snapshotsLoaded.value = true
  } catch {
    // 业务错误已由 client 拦截器统一弹窗提示
  } finally {
    snapshotsLoading.value = false
  }
}

// 折叠区展开历史版本时懒加载快照列表（只拉一次，之后由采纳/回退动作刷新）
watch(histActive, (names) => {
  if (names.length && !snapshotsLoaded.value) void fetchSnapshots()
})

/**
 * 回退到指定快照：二次确认 → rollbackSnapshot → 成功提示 + 刷新列表 +
 * emit applied（scope 缺省 → 容器刷新全部卡片）。服务端事务内自动创建
 * 「回退前存档」快照（可再次回退撤销）。
 * @param snap 目标快照
 * @returns 无
 * @throws 接口错误已由 client 拦截器统一弹窗提示
 */
async function handleRollback(snap: VersionSnapshot): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '将把项目恢复到该快照记录的状态；回退前会自动保存当前状态为「回退前存档」快照，可再次回退撤销。是否继续？',
      '回退此版本',
      { type: 'warning', confirmButtonText: '确认回退', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消确认
  }
  rollingBackId.value = snap.id
  try {
    const res = await rollbackSnapshot(props.projectId, snap.id)
    ElMessage.success(`已回退；回退前状态已存为快照 #${res.new_snapshot_id}`)
    pushMessage({ role: 'system', kind: 'text', text: '已回退到历史版本，中部卡片已刷新' })
    await fetchSnapshots()
    emit('applied', {})
  } catch {
    // 业务错误已由 client 拦截器统一弹窗提示
  } finally {
    rollingBackId.value = null
  }
}

// ============================================================
// 对比弹窗（该快照摘要 vs 当前流水线摘要）
// ============================================================

/** 对比弹窗显隐 */
const compareVisible = ref(false)
/** 对比目标快照（null=未打开） */
const compareSnap = ref<VersionSnapshot | null>(null)
/** 当前流水线摘要（GET pipeline/status 的 summary，对比弹窗打开时拉取） */
const currentSummary = ref<Record<string, number> | null>(null)
/** 当前摘要加载中 */
const compareLoading = ref(false)

/**
 * 打开对比弹窗：记录目标快照并拉取当前流水线摘要（失败降级为 null，
 * 弹窗当前栏显示「暂无法获取」）。
 * @param snap 目标快照
 * @returns 无
 * @throws 接口错误已由 client 拦截器统一弹窗提示
 */
async function openCompare(snap: VersionSnapshot): Promise<void> {
  compareSnap.value = snap
  compareVisible.value = true
  compareLoading.value = true
  try {
    const status = await pipelineApi.getPipelineStatus(props.projectId)
    currentSummary.value = status.summary ?? null
  } catch {
    currentSummary.value = null
  } finally {
    compareLoading.value = false
  }
}

/**
 * 取目标快照的摘要对象（类型收窄供模板使用）。
 * @param snap 快照行（可空）
 * @returns 摘要对象（非对象形态返回 undefined）
 */
function snapSummary(snap: VersionSnapshot | null): Record<string, number> | undefined {
  const s = snap?.summary
  return s && typeof s === 'object' ? (s as Record<string, number>) : undefined
}

/**
 * 把摘要 JSONB 渲染为「中文标签：数值」行列表（未知 key 原样展示 key）。
 * @param summary 摘要对象（可空）
 * @returns 标签行数组
 */
function summaryRows(summary: Record<string, number> | null | undefined): string[] {
  if (!summary || typeof summary !== 'object') return []
  return Object.entries(summary)
    .filter(([, v]) => typeof v === 'number')
    .map(([k, v]) => `${SUMMARY_KEY_LABELS[k] ?? k}：${v}`)
}
</script>

<template>
  <el-card shadow="never" class="assist-card">
    <template #header>
      <div class="assist-head">
        <span>AI 助手</span>
        <el-tag size="small" type="success" effect="plain">对话共创</el-tag>
      </div>
    </template>

    <!-- 聚焦对象显示行：随中部卡片选中态联动（容器下发 focus.label） -->
    <div class="focus-row">
      <span class="focus-label">聚焦对象</span>
      <el-tag size="small" effect="light">{{ props.focus.label }}</el-tag>
    </div>

    <!-- 快捷指令 Chips：按作用域过滤分组，点击即发送 -->
    <div v-if="visibleChipGroups.length" class="chip-area">
      <div v-for="group in visibleChipGroups" :key="group.key" class="chip-group">
        <span class="chip-group__label">{{ group.label }}</span>
        <div class="chip-group__tags">
          <el-tag
            v-for="chip in group.items"
            :key="chip"
            class="chip-tag"
            size="small"
            effect="plain"
            role="button"
            @click="send(chip)"
          >
            {{ chip }}
          </el-tag>
        </div>
      </div>
    </div>

    <!-- 对话消息滚动区：空态引导 / 消息气泡 / diff 回执卡片 / loading / 错误重试 -->
    <div ref="msgAreaRef" class="msg-area">
      <el-empty
        v-if="!messages.length"
        description="输入指令，与 AI 共创剧本"
        :image-size="72"
      />
      <template v-else>
        <div
          v-for="msg in messages"
          :key="msg.id"
          class="msg-row"
          :class="`msg-row--${msg.role}`"
        >
          <!-- user 指令气泡（右对齐） -->
          <div v-if="msg.role === 'user'" class="bubble bubble--user">{{ msg.text }}</div>

          <!-- system 分隔/结果提示（居中细字） -->
          <div v-else-if="msg.role === 'system'" class="sys-tip">{{ msg.text }}</div>

          <!-- assistant：预览 loading 气泡 -->
          <div v-else-if="msg.kind === 'loading'" class="bubble bubble--assist is-loading">
            {{ msg.text }}
          </div>

          <!-- assistant：diff 回执卡片 -->
          <div v-else-if="msg.kind === 'diff'" class="diff-card">
            <p v-if="msg.preview?.summary" class="diff-summary">{{ msg.preview.summary }}</p>
            <div v-for="ch in msg.preview?.changes ?? []" :key="ch.id" class="change-card">
              <div class="change-head">
                <el-checkbox
                  :model-value="msg.checked?.[ch.id] ?? true"
                  @change="(v: string | number | boolean) => setChecked(msg, ch.id, !!v)"
                >
                  <span class="change-path">{{ pathLabel(ch.path) }}</span>
                </el-checkbox>
              </div>
              <div class="diff-lines">
                <div
                  v-for="(line, li) in ch.diff_lines"
                  :key="li"
                  class="diff-line"
                  :class="`diff-line--${line.op}`"
                >
                  <span class="diff-sign">
                    {{ line.op === 'insert' ? '+' : line.op === 'delete' ? '-' : ' ' }}
                  </span>
                  <span class="diff-text">{{ line.text }}</span>
                </div>
              </div>
              <div v-if="ch.reason" class="change-reason">理由：{{ ch.reason }}</div>
            </div>
            <div class="diff-ops">
              <el-button
                size="small"
                type="primary"
                :loading="msg.applying"
                @click="applyMessage(msg)"
              >
                采纳（{{ pickedChanges(msg).length }}）
              </el-button>
              <el-button
                size="small"
                type="primary"
                plain
                :loading="msg.applying"
                @click="applyMessage(msg, true)"
              >
                全部采纳
              </el-button>
              <el-button size="small" :disabled="msg.applying" @click="rejectMessage(msg)">
                拒绝
              </el-button>
            </div>
          </div>

          <!-- assistant：引用优化回执卡片（可复制文本，用户粘贴回对应位置） -->
          <div v-else-if="msg.kind === 'quote'" class="quote-card">
            <div class="quote-card__meta">
              <el-tag size="small" type="primary" effect="plain">优化文本</el-tag>
              <span class="quote-card__src">来自：{{ msg.quoteSources?.join('、') }}</span>
            </div>
            <pre class="quote-card__text">{{ msg.text }}</pre>
            <div class="quote-card__ops">
              <el-button
                size="small"
                type="primary"
                :loading="msg.copying"
                @click="copyQuoteResult(msg)"
              >
                复制
              </el-button>
              <span class="quote-card__tip">复制后粘贴到对应位置自行保存</span>
            </div>
          </div>

          <!-- assistant：错误消息（可重试） -->
          <div v-else class="err-card">
            <span class="err-text">{{ msg.text }}</span>
            <el-button
              v-if="msg.retryInstruction"
              size="small"
              type="primary"
              text
              :disabled="sending"
              @click="send(msg.retryInstruction)"
            >
              重试
            </el-button>
          </div>
        </div>
      </template>
    </div>

    <!-- 引用区（工作台选中文本「添加对话中」入队）：紧凑标签行（气泡图标 +
         选中文本序号 + 移除），hover 显示来源与全文 -->
    <div v-if="quotes.length" class="quote-area">
      <span
        v-for="(q, qi) in quotes"
        :key="q.id"
        class="quote-chip"
        :title="`${q.source}：${q.text}`"
      >
        <el-icon class="quote-chip__icon"><ChatDotRound /></el-icon>
        <span class="quote-chip__label">选中文本 {{ qi + 1 }}</span>
        <el-icon class="quote-chip__x" role="button" @click="quoteBridge.remove(q.id)">
          <Close />
        </el-icon>
      </span>
    </div>

    <!-- 底部输入区：textarea ≤500 字计数 + 发送 -->
    <div class="input-row">
      <el-input
        v-model="input"
        type="textarea"
        :rows="3"
        :maxlength="500"
        show-word-limit
        resize="none"
        placeholder="输入改写指令，如「把开场冲突提前」"
      />
      <el-button
        type="primary"
        :icon="Promotion"
        :loading="sending"
        :disabled="!input.trim()"
        @click="send()"
      >
        发送
      </el-button>
    </div>

    <!-- 历史版本折叠区：最近 10 条快照 + 回退 / 对比 -->
    <el-collapse v-model="histActive" class="hist-collapse">
      <el-collapse-item name="history" title="历史版本">
        <div v-loading="snapshotsLoading" class="hist-body">
          <el-empty
            v-if="snapshotsLoaded && !snapshotsLoading && !snapshotRows.length"
            description="暂无版本快照（关键动作会自动创建）"
            :image-size="50"
          />
          <div v-for="snap in snapshotRows" :key="snap.id" class="hist-item">
            <div class="hist-head">
              <el-tag size="small" :type="snap.stage === 'assist_applied' ? 'success' : 'info'">
                {{ stageLabel(snap.stage) }}
              </el-tag>
              <span class="hist-time">{{ formatSnapshotTime(snap.created_at) }}</span>
            </div>
            <div class="hist-ops">
              <el-button size="small" text type="primary" @click="openCompare(snap)">
                对比
              </el-button>
              <el-button
                size="small"
                text
                type="warning"
                :loading="rollingBackId === snap.id"
                @click="handleRollback(snap)"
              >
                回退
              </el-button>
            </div>
          </div>
        </div>
      </el-collapse-item>
    </el-collapse>

    <!-- 对比弹窗：该快照摘要 vs 当前流水线摘要 -->
    <el-dialog
      v-model="compareVisible"
      title="版本对比"
      width="480px"
      append-to-body
    >
      <div v-loading="compareLoading" class="cmp-body">
        <div class="cmp-col">
          <div class="cmp-col__title">
            快照 #{{ compareSnap?.id }}（{{ stageLabel(compareSnap?.stage ?? '') }} ·
            {{ formatSnapshotTime(compareSnap?.created_at) }}）
          </div>
          <p v-if="summaryRows(snapSummary(compareSnap)).length">
            <span
              v-for="row in summaryRows(snapSummary(compareSnap))"
              :key="row"
              class="cmp-line"
            >
              {{ row }}
            </span>
          </p>
          <p v-else class="cmp-empty">（无摘要）</p>
        </div>
        <div class="cmp-col">
          <div class="cmp-col__title">当前</div>
          <p v-if="summaryRows(currentSummary).length">
            <span v-for="row in summaryRows(currentSummary)" :key="row" class="cmp-line">
              {{ row }}
            </span>
          </p>
          <p v-else class="cmp-empty">{{ compareLoading ? '加载中…' : '暂无法获取当前摘要' }}</p>
        </div>
      </div>
      <template #footer>
        <el-button @click="compareVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.assist-card {
  align-self: start;
  display: flex;
  flex-direction: column;
}
.assist-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: var(--mf-fw-semibold);
}
.focus-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  padding-bottom: var(--mf-sp-3);
  border-bottom: 1px dashed var(--mf-border);
}
.focus-label {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
/* 快捷 Chips：分组横向滚动，视觉轻量 */
.chip-area {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-2);
  padding: var(--mf-sp-3) 0;
  border-bottom: 1px dashed var(--mf-border);
}
.chip-group {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.chip-group__label {
  flex-shrink: 0;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.chip-group__tags {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  scrollbar-width: none;
}
.chip-group__tags::-webkit-scrollbar {
  display: none;
}
.chip-tag {
  flex-shrink: 0;
  cursor: pointer;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chip-tag:hover {
  border-color: var(--mf-brand);
  color: var(--mf-brand);
}
/* 消息滚动区 */
.msg-area {
  min-height: 320px;
  max-height: 420px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
  padding: var(--mf-sp-3) 0;
}
.msg-area:empty,
.msg-area > .el-empty {
  margin: auto;
}
.msg-row {
  display: flex;
  flex-direction: column;
}
.msg-row--user {
  align-items: flex-end;
}
.msg-row--assistant {
  align-items: flex-start;
}
.msg-row--system {
  align-items: center;
}
/* 气泡：user 主色 / assistant 浅底 */
.bubble {
  max-width: 88%;
  padding: 6px 10px;
  border-radius: var(--mf-r-ctl);
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.bubble--user {
  background: var(--mf-brand);
  color: var(--mf-bg);
}
.bubble--assist {
  background: var(--mf-surface-2);
  color: var(--mf-text-2);
}
.sys-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
  padding: 2px 0;
}
/* diff 回执卡片（红删绿增沿用 StepEpisodeBody 优化 diff 口径） */
.diff-card {
  width: 100%;
  padding: var(--mf-sp-3);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-surface-2);
}
.diff-summary {
  margin: 0 0 var(--mf-sp-2);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
}
.change-card {
  padding: var(--mf-sp-2);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-surface);
  margin-bottom: var(--mf-sp-2);
}
.change-head {
  margin-bottom: var(--mf-sp-2);
}
.change-path {
  font-weight: var(--mf-fw-semibold);
}
.diff-lines {
  border-radius: var(--mf-r-ctl);
  overflow: hidden;
  font-size: var(--mf-fs-xs);
  line-height: 1.7;
}
.diff-line {
  display: flex;
  gap: 6px;
  padding: 2px 6px;
  white-space: pre-wrap;
  word-break: break-word;
}
.diff-sign {
  flex-shrink: 0;
  width: 10px;
  text-align: center;
  opacity: 0.7;
}
.diff-line--insert {
  background: #e7f7e7;
  color: #1a7f37;
}
.diff-line--delete {
  background: #fdecec;
  color: #c0392b;
  text-decoration: line-through;
}
.diff-line--equal {
  color: var(--mf-text-2);
}
.change-reason {
  margin-top: var(--mf-sp-2);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.diff-ops {
  display: flex;
  justify-content: flex-end;
  gap: var(--mf-sp-2);
}
/* 错误消息（可重试） */
.err-card {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  padding: 6px 10px;
  border: 1px solid var(--el-color-danger-light-7, #fde2e2);
  border-radius: var(--mf-r-ctl);
  background: var(--el-color-danger-light-9, #fef0f0);
}

/* ===== 选中文本引用区（工作台「添加对话中」入队，紧凑标签样式） ===== */
.quote-area {
  display: flex;
  flex-wrap: wrap;
  gap: var(--mf-sp-1);
  margin-bottom: var(--mf-sp-2);
}
.quote-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  padding: 2px 8px;
  border-radius: 6px;
  background: var(--mf-surface-2, #262626);
  border: 1px solid var(--mf-border, #3a3a3a);
  color: var(--mf-text-1, #eee);
  font-size: var(--mf-fs-xs, 12px);
  line-height: 1.6;
  cursor: default;
}
.quote-chip__icon {
  flex-shrink: 0;
  color: var(--mf-brand, #36cfc9);
}
.quote-chip__label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.quote-chip__x {
  flex-shrink: 0;
  color: var(--mf-text-3, #777);
  cursor: pointer;
  transition: color var(--mf-dur, 0.15s) ease;
}
.quote-chip__x:hover {
  color: var(--el-color-danger, #f56c6c);
}

/* ===== 引用优化回执卡片（可复制文本） ===== */
.quote-card {
  padding: var(--mf-sp-3);
  border: 1px solid var(--mf-border, #333);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-surface-2, #1c1c1c);
}
.quote-card__meta {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-2);
}
.quote-card__src {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--mf-text-3, #777);
  font-size: var(--mf-fs-xs);
}
.quote-card__text {
  margin: 0;
  max-height: 280px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
  font-size: var(--mf-fs-sm);
  line-height: 1.8;
  color: var(--mf-text-1, #eee);
}
.quote-card__ops {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-top: var(--mf-sp-2);
}
.quote-card__tip {
  color: var(--mf-text-3, #777);
  font-size: var(--mf-fs-xs);
}
.err-text {
  color: var(--el-color-danger, #c0392b);
  font-size: var(--mf-fs-sm);
}
/* 输入区 */
.input-row {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-2);
  padding-top: var(--mf-sp-3);
  border-top: 1px dashed var(--mf-border);
}
.input-row .el-button {
  align-self: flex-end;
}
/* 历史版本折叠区 */
.hist-collapse {
  margin-top: var(--mf-sp-3);
  border-top: 1px solid var(--mf-border);
}
.hist-body {
  min-height: 48px;
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-2);
}
.hist-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-2);
  padding: 6px 8px;
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
}
.hist-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  min-width: 0;
}
.hist-time {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.hist-ops {
  flex-shrink: 0;
}
/* 对比弹窗两栏 */
.cmp-body {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--mf-sp-3);
  min-height: 96px;
}
.cmp-col {
  padding: var(--mf-sp-2);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
}
.cmp-col__title {
  font-weight: var(--mf-fw-semibold);
  font-size: var(--mf-fs-sm);
  margin-bottom: var(--mf-sp-2);
}
.cmp-line {
  display: block;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.8;
}
.cmp-empty {
  margin: 0;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
</style>
