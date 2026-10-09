<!--
  画布编辑器（文件级注释，C-02 —— 本任务核心，自研轻量节点画布，无第三方图库依赖）。

  三栏布局：左侧节点面板（10 类节点）· 中部画布区 · 右侧检查器 + 画布助手。
  画布实现要点：
  - 世界坐标 4000x3000，容器可滚动；缩放 transform: scale（origin 左上，0.4~2），
    缩放后由 world-sizer（尺寸 = 世界 × scale）提供正确滚动范围；
  - 节点为绝对定位 div（宽 200 / 高 100），拖拽 mousemove 仅更新本地 state，
    mouseup 落库；节点增删移与连线统一走 saveGraph 全量同步（不含即删），
    自动保存 debounce 1s，Ctrl+S 立即保存；
  - 连线：输出锚点 mousedown 进入连线模式 → mousemove 画临时 SVG path →
    mouseup 落在目标节点（含输入锚点）完成；本地做类型白名单 + 自环 + 成环预检，
    后端仍会 400 拦截（成环 detail 经拦截器 ElMessage 提示并回滚本地边）；
  - SVG 连线层按节点位置画贝塞尔曲线（出口右中 → 入口左中），点击边删除；
  - 小地图 120x90 仅展示（节点点阵 + 当前视口矩形）；
  - 快捷键：Delete 删除选中节点、Ctrl+S 立即保存、F 适配视图；
  - 运行：execute（all / dirty / node 三种模式）→ 开启 GET SSE stream 监听
    'nodes'（节点状态实时刷新）与 'done'（批次结束）；
  - 画布助手：assistantChat 对话，回复后按后端返回的 nodes/edges 自动刷新画布。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadRequestOptions, UploadFile } from 'element-plus'
import * as canvasApi from '@/api/canvas'
import type { CanvasEdge, CanvasGraph, CanvasNode, GraphSyncPayload } from '@/types'

const route = useRoute()
const router = useRouter()

// ============================================================
// 常量与类型
// ============================================================

/** 世界宽度（画布逻辑坐标） */
const WORLD_W = 4000
/** 世界高度 */
const WORLD_H = 3000
/** 节点统一宽度（px） */
const NODE_W = 200
/** 节点统一高度（px） */
const NODE_H = 100
/** 最小缩放 */
const SCALE_MIN = 0.4
/** 最大缩放 */
const SCALE_MAX = 2
/** 小地图宽度 */
const MINI_W = 120
/** 小地图高度 */
const MINI_H = 90

/** 节点类型联合 */
type NodeType = 'text' | 'image' | 'video' | 'audio' | 'generate' | 'asset_ref' | 'shot' | 'compose' | 'logic' | 'group'

/** 左侧面板 10 类节点定义 */
const NODE_PALETTE: { type: NodeType; label: string; icon: string }[] = [
  { type: 'text', label: '文本', icon: '📝' },
  { type: 'image', label: '图像', icon: '🖼️' },
  { type: 'video', label: '视频', icon: '🎬' },
  { type: 'audio', label: '音频', icon: '🎵' },
  { type: 'generate', label: '生成', icon: '✨' },
  { type: 'asset_ref', label: '资产引用', icon: '🧑‍🎨' },
  { type: 'shot', label: '分镜', icon: '🎞️' },
  { type: 'compose', label: '合成', icon: '🧫' },
  { type: 'logic', label: '逻辑', icon: '🔀' },
  { type: 'group', label: '分组', icon: '🗂️' },
]

/** 类型中文名映射 */
const TYPE_LABELS: Record<NodeType, string> = {
  text: '文本', image: '图像', video: '视频', audio: '音频', generate: '生成',
  asset_ref: '资产引用', shot: '分镜', compose: '合成', logic: '逻辑', group: '分组',
}

/**
 * 连线类型白名单（有序对映射表）：
 * key = 目标节点类型，value = 允许作为其来源的节点类型；
 * 未列出的目标类型为"自由连接"（任何来源均可）。
 * - generate 只能作为目标接收 text / asset_ref / shot（提示词/资产/镜头输入）；
 * - compose  只能接 image / video / audio（素材合成）。
 */
const EDGE_RULES: Partial<Record<NodeType, NodeType[]>> = {
  generate: ['text', 'asset_ref', 'shot'],
  compose: ['image', 'video', 'audio'],
}

/** 节点状态角标颜色映射（idle/queued/running/done/failed） */
const STATUS_COLORS: Record<string, string> = {
  idle: 'var(--mf-text-3)',
  queued: 'var(--mf-info)',
  running: 'var(--mf-warn)',
  done: 'var(--mf-ok)',
  failed: 'var(--mf-danger)',
}

// ============================================================
// 基础状态
// ============================================================

/** 画布 ID（路由参数） */
const canvasId = Number(route.params.id)

/** 画布完整图（基础信息） */
const graph = ref<CanvasGraph | null>(null)
/** 节点列表（本地可变副本，临时节点用负 id） */
const nodes = ref<CanvasNode[]>([])
/** 连线列表（新连线用负 id） */
const edges = ref<CanvasEdge[]>([])
/** 页面加载状态 */
const loading = ref(true)

/** 客户端临时 id 发放器（负数递减，与后端正 id 不冲突） */
let nextTempId = -1

/** 当前选中节点 id */
const selectedId = ref<number | null>(null)
/** 选中节点计算属性 */
const selectedNode = computed(() => nodes.value.find((n) => n.id === selectedId.value) ?? null)

/** 缩放比例 */
const scale = ref(1)
/** 画布视口容器引用 */
const viewport = ref<HTMLDivElement | null>(null)
/** 世界层引用（坐标换算用） */
const world = ref<HTMLDivElement | null>(null)

// ---------- 保存状态 ----------
/** 保存状态：saved / unsaved / saving / error */
const saveState = ref<'saved' | 'unsaved' | 'saving' | 'error'>('unsaved')
/** 最近保存时间 */
const savedAt = ref<Date | null>(null)
/** 自动保存开关 */
const autoSave = ref(true)
/** 保存状态展示文案 */
const saveText = computed(() => {
  switch (saveState.value) {
    case 'saving': return '保存中…'
    case 'saved': return savedAt.value ? `已保存 ${fmtTime(savedAt.value)}` : '已保存'
    case 'error': return '保存失败，请重试'
    default: return '有未保存更改'
  }
})

// ============================================================
// 图加载 / 保存（saveGraph 全量同步）
// ============================================================

/**
 * 将秒级时间格式化为 hh:mm:ss（内部函数）。
 * @param d 日期对象
 * @returns 形如 14:32:05 的字符串
 */
function fmtTime(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/**
 * 组装全量同步负载（内部函数）：
 * 正 id 原样提交（upsert），负 id（本页新建）不传 id（插入）；
 * 连线同理。保存时后端"未出现即删除"，因此删除节点/边只需不提交。
 */
function toPayload(): GraphSyncPayload {
  return {
    nodes: nodes.value.map((n) => ({
      id: n.id > 0 ? n.id : undefined,
      node_type: n.node_type,
      position_x: n.position_x,
      position_y: n.position_y,
      data: n.data ?? {},
      config: n.config ?? undefined,
      z_index: n.z_index,
    })),
    edges: edges.value.map((e) => ({
      id: e.id > 0 ? e.id : undefined,
      source_node_id: e.source_node_id,
      target_node_id: e.target_node_id,
    })),
  }
}

/** 保存互斥锁（防并发全量同步） */
let saving = false
/** 保存期间有新变更 → 完成后需再保存一次 */
let resaveNeeded = false
/** 自动保存 debounce 定时器 */
let saveTimer: number | null = null

/**
 * 标记为"未保存"并按开关调度 debounce 自动保存（1s）。
 * 拖拽/表单输入等高频操作仅更新本地 state，由此函数统一收口落库。
 */
function scheduleSave(): void {
  saveState.value = 'unsaved'
  if (saveTimer) window.clearTimeout(saveTimer)
  if (!autoSave.value) return
  // 自动保存 debounce 1s：连续变更合并为一次全量同步
  saveTimer = window.setTimeout(() => { void persist() }, 1000)
}

/**
 * 立即保存（Ctrl+S / 保存按钮 / 运行前同步）。
 * 保存成功后以响应图替换本地 nodes/edges，并用「类型+位置」键
 * 将负 id 映射回真实 id 以恢复选中态。
 */
async function persist(): Promise<void> {
  if (!graph.value) return
  if (saving) { resaveNeeded = true; return }
  if (saveTimer) { window.clearTimeout(saveTimer); saveTimer = null }
  saving = true
  saveState.value = 'saving'
  const prevTemps = nodes.value.filter((n) => n.id < 0)
  try {
    const resp = await canvasApi.saveGraph(graph.value.canvas.id, toPayload())
    // 以服务端图为准替换本地
    nodes.value = resp.nodes.map((n) => ({ ...n, data: { ...(n.data ?? {}) }, config: { ...(n.config ?? {}) } }))
    edges.value = resp.edges.map((e) => ({ ...e }))
    // 恢复选中：负 id → 真实 id（按 node_type + position 唯一匹配）
    if (selectedId.value != null && selectedId.value < 0) {
      const prev = prevTemps.find((n) => n.id === selectedId.value)
      if (prev) {
        const hit = nodes.value.find(
          (n) => n.node_type === prev.node_type && n.position_x === prev.position_x && n.position_y === prev.position_y,
        )
        selectedId.value = hit?.id ?? null
      }
    }
    saveState.value = 'saved'
    savedAt.value = new Date()
  } catch {
    // 400（类型/自环/成环）等 detail 已由拦截器 ElMessage 提示；本地数据不变待用户修正
    saveState.value = 'error'
  } finally {
    saving = false
    if (resaveNeeded) { resaveNeeded = false; scheduleSave() }
  }
}

/**
 * 加载画布完整图。
 */
async function loadGraph(): Promise<void> {
  loading.value = true
  try {
    const resp = await canvasApi.getGraph(canvasId)
    graph.value = resp
    nodes.value = resp.nodes.map((n) => ({ ...n, data: { ...(n.data ?? {}) }, config: { ...(n.config ?? {}) } }))
    edges.value = resp.edges.map((e) => ({ ...e }))
    saveState.value = 'saved'
    savedAt.value = new Date()
    await nextTick()
    updateScrollState()
  } catch {
    ElMessage.error('画布加载失败，返回列表')
    router.replace('/canvas')
  } finally {
    loading.value = false
  }
}

// ============================================================
// 节点增删 / 选中 / 检查器
// ============================================================

/**
 * 节点默认 data（内部函数）：按类型给可编辑字段的初值。
 */
function defaultData(type: NodeType): Record<string, unknown> {
  switch (type) {
    case 'text': return { text: '', label: '文本' }
    case 'generate': return { prompt: '', label: '生成节点' }
    case 'shot': return { description: '', label: '分镜' }
    case 'logic': return { expression: '', label: '逻辑' }
    case 'asset_ref': return { asset_id: undefined, label: '资产引用' }
    default: return { url: '', label: TYPE_LABELS[type] }
  }
}

/**
 * 节点默认 config（内部函数）：generate 需要任务类型。
 */
function defaultConfig(type: NodeType): Record<string, unknown> {
  return type === 'generate' ? { task_type: 'image' } : {}
}

/**
 * 在画布可视中心添加节点（点击左侧面板按钮触发）。
 * @param type 节点类型
 */
function addNode(type: NodeType): void {
  const vp = viewport.value
  if (!vp) return
  // 视口中心换算为世界坐标（含缩放），加少量抖动避免重叠
  const jitter = (nodes.value.length % 5) * 28
  const x = Math.min(WORLD_W - NODE_W, Math.max(0, (vp.scrollLeft + vp.clientWidth / 2) / scale.value - NODE_W / 2 + jitter))
  const y = Math.min(WORLD_H - NODE_H, Math.max(0, (vp.scrollTop + vp.clientHeight / 2) / scale.value - NODE_H / 2 + jitter))
  const node: CanvasNode = {
    id: nextTempId--,
    canvas_id: canvasId,
    node_type: type,
    position_x: Math.round(x),
    position_y: Math.round(y),
    size_w: NODE_W,
    size_h: NODE_H,
    z_index: nodes.value.length + 1,
    data: defaultData(type),
    config: defaultConfig(type),
    is_dirty: true,
    cache_ref: null,
    loop_config: null,
    status: 'idle',
    parent_group_id: null,
  }
  nodes.value.push(node)
  selectedId.value = node.id
  scheduleSave()
}

/**
 * 删除当前选中节点（检查器按钮 / Delete 键）。
 * 连边级联：saveGraph 全量提交不含被删项即删（无需单独删边接口）。
 */
async function removeSelected(): Promise<void> {
  const id = selectedId.value
  if (id == null) return
  const node = nodes.value.find((n) => n.id === id)
  nodes.value = nodes.value.filter((n) => n.id !== id)
  edges.value = edges.value.filter((e) => e.source_node_id !== id && e.target_node_id !== id)
  selectedId.value = null
  scheduleSave()
  if (node) ElMessage.success(`已删除${TYPE_LABELS[node.node_type as NodeType]}节点`)
}

/**
 * 删除连线（点击边触发，同走全量同步）。
 * @param edge 连线
 */
function removeEdge(edge: CanvasEdge): void {
  edges.value = edges.value.filter((e) => e.id !== edge.id)
  scheduleSave()
}

// ============================================================
// 节点拖拽（mousemove 仅更新本地，mouseup 落库）
// ============================================================

/** 拖拽会话状态 */
const drag = ref<null | { nodeId: number; startX: number; startY: number; origX: number; origY: number }>(null)

/**
 * 节点 mousedown：选中并开始拖拽（监听挂在 window，避免快速甩出丢失事件）。
 */
function onNodeMouseDown(e: MouseEvent, node: CanvasNode): void {
  if (e.button !== 0) return
  e.stopPropagation()
  selectedId.value = node.id
  drag.value = { nodeId: node.id, startX: e.clientX, startY: e.clientY, origX: node.position_x, origY: node.position_y }
  window.addEventListener('mousemove', onDragMove)
  window.addEventListener('mouseup', onDragUp)
}

/**
 * 拖拽移动：delta 除以 scale 换算为世界坐标位移。
 */
function onDragMove(e: MouseEvent): void {
  const d = drag.value
  if (!d) return
  const node = nodes.value.find((n) => n.id === d.nodeId)
  if (!node) return
  node.position_x = Math.round(Math.min(WORLD_W - NODE_W, Math.max(0, d.origX + (e.clientX - d.startX) / scale.value)))
  node.position_y = Math.round(Math.min(WORLD_H - NODE_H, Math.max(0, d.origY + (e.clientY - d.startY) / scale.value)))
}

/**
 * 拖拽结束：清理监听，位置有变化才触发落库。
 */
function onDragUp(): void {
  const d = drag.value
  drag.value = null
  window.removeEventListener('mousemove', onDragMove)
  window.removeEventListener('mouseup', onDragUp)
  if (!d) return
  const node = nodes.value.find((n) => n.id === d.nodeId)
  if (node && (node.position_x !== d.origX || node.position_y !== d.origY)) scheduleSave()
}

// ============================================================
// 连线（类型白名单 + 自环 + 成环预检；成环后端 400 兜底）
// ============================================================

/** 连线会话状态（临时终点为世界坐标） */
const connecting = ref<null | { sourceId: number; x: number; y: number }>(null)

/**
 * 屏幕坐标 → 世界坐标（内部函数）：基于 world 元素实时 rect 与当前 scale。
 */
function worldPos(e: MouseEvent): { x: number; y: number } {
  const rect = world.value?.getBoundingClientRect()
  if (!rect) return { x: 0, y: 0 }
  return { x: (e.clientX - rect.left) / scale.value, y: (e.clientY - rect.top) / scale.value }
}

/**
 * 输出锚点 mousedown：进入连线模式。
 */
function onOutAnchorDown(e: MouseEvent, node: CanvasNode): void {
  if (e.button !== 0) return
  e.stopPropagation()
  connecting.value = {
    sourceId: node.id,
    x: node.position_x + NODE_W,
    y: node.position_y + NODE_H / 2,
  }
  window.addEventListener('mousemove', onConnMove)
  window.addEventListener('mouseup', onConnUp)
}

/**
 * 连线模式移动：更新临时终点（SVG 虚线路径跟随）。
 */
function onConnMove(e: MouseEvent): void {
  if (!connecting.value) return
  const p = worldPos(e)
  connecting.value.x = p.x
  connecting.value.y = p.y
}

/**
 * 判断两个节点间是否允许连线（内部函数）：按 EDGE_RULES 白名单校验。
 * @param srcType 来源类型
 * @param dstType 目标类型
 * @returns 是否允许
 */
function canConnect(srcType: NodeType, dstType: NodeType): boolean {
  const allowed = EDGE_RULES[dstType]
  return !allowed || allowed.includes(srcType)
}

/**
 * 本地成环预检（内部函数）：假设加入 source→target 后做 DFS，
 * 若 target 可回到 source 则成环。后端保存时仍会校验兜底。
 */
function wouldCycle(sourceId: number, targetId: number): boolean {
  const adj = new Map<number, number[]>()
  for (const e of [...edges.value, { source_node_id: sourceId, target_node_id: targetId } as CanvasEdge]) {
    const list = adj.get(e.source_node_id) ?? []
    list.push(e.target_node_id)
    adj.set(e.source_node_id, list)
  }
  const stack = [...(adj.get(targetId) ?? [])]
  const seen = new Set<number>()
  while (stack.length) {
    const cur = stack.pop() as number
    if (cur === sourceId) return true
    if (seen.has(cur)) continue
    seen.add(cur)
    stack.push(...(adj.get(cur) ?? []))
  }
  return false
}

/**
 * 连线结束（mouseup）：命中目标节点则校验并建边，本地建临时边后走保存。
 */
function onConnUp(e: MouseEvent): void {
  window.removeEventListener('mousemove', onConnMove)
  window.removeEventListener('mouseup', onConnUp)
  const conn = connecting.value
  connecting.value = null
  if (!conn) return
  const source = nodes.value.find((n) => n.id === conn.sourceId)
  if (!source) return

  // 命中检测：鼠标落点是否在某节点矩形内（输入锚点在边缘 ±10px 也算）
  const p = worldPos(e)
  const target = nodes.value.find(
    (n) => n.id !== conn.sourceId
      && p.x >= n.position_x - 10 && p.x <= n.position_x + NODE_W + 10
      && p.y >= n.position_y - 10 && p.y <= n.position_y + NODE_H + 10,
  )
  if (!target) return

  const srcType = source.node_type as NodeType
  const dstType = target.node_type as NodeType
  if (!canConnect(srcType, dstType)) {
    ElMessage.warning(`不允许连线：${TYPE_LABELS[srcType]} → ${TYPE_LABELS[dstType]}（类型校验未通过）`)
    return
  }
  if (edges.value.some((ed) => ed.source_node_id === source.id && ed.target_node_id === target.id)) {
    ElMessage.info('两个节点间已存在连线')
    return
  }
  if (wouldCycle(source.id, target.id)) {
    ElMessage.warning('该连线将形成环路，已拒绝（后端保存时同样会拦截）')
    return
  }
  edges.value.push({ id: nextTempId--, source_node_id: source.id, target_node_id: target.id })
  scheduleSave()
}

/**
 * 边的贝塞尔路径（内部函数）：出口右中 → 入口左中。
 */
function edgePath(edge: CanvasEdge): string {
  const s = nodes.value.find((n) => n.id === edge.source_node_id)
  const t = nodes.value.find((n) => n.id === edge.target_node_id)
  if (!s || !t) return ''
  const x1 = s.position_x + NODE_W
  const y1 = s.position_y + NODE_H / 2
  const x2 = t.position_x
  const y2 = t.position_y + NODE_H / 2
  const dx = Math.max(40, Math.abs(x2 - x1) * 0.5)
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`
}

/** 连线中临时路径（虚线预览） */
const tempEdgePath = computed(() => {
  const conn = connecting.value
  if (!conn) return ''
  const s = nodes.value.find((n) => n.id === conn.sourceId)
  if (!s) return ''
  const x1 = s.position_x + NODE_W
  const y1 = s.position_y + NODE_H / 2
  return `M ${x1} ${y1} L ${conn.x} ${conn.y}`
})

// ============================================================
// 缩放 / 适配 / 小地图
// ============================================================

/** 视口滚动状态（小地图视口矩形用） */
const scrollState = reactive({ left: 0, top: 0, width: 0, height: 0 })

/**
 * 记录视口滚动/尺寸（@scroll 触发 + 挂载后/缩放后手动刷新）。
 */
function updateScrollState(): void {
  const vp = viewport.value
  if (!vp) return
  scrollState.left = vp.scrollLeft
  scrollState.top = vp.scrollTop
  scrollState.width = vp.clientWidth
  scrollState.height = vp.clientHeight
}

/**
 * Ctrl + 滚轮缩放（0.4~2；简化为以容器中心为缩放原点）。
 */
function onWheel(e: WheelEvent): void {
  if (!e.ctrlKey && !e.metaKey) return
  e.preventDefault()
  const step = e.deltaY < 0 ? 0.1 : -0.1
  scale.value = Math.min(SCALE_MAX, Math.max(SCALE_MIN, Math.round((scale.value + step) * 10) / 10))
  nextTick(updateScrollState)
}

/**
 * F 键 / 适配按钮：全部节点入视口。
 */
function fitView(): void {
  const vp = viewport.value
  if (!vp || nodes.value.length === 0) return
  const minX = Math.min(...nodes.value.map((n) => n.position_x))
  const minY = Math.min(...nodes.value.map((n) => n.position_y))
  const maxX = Math.max(...nodes.value.map((n) => n.position_x + NODE_W))
  const maxY = Math.max(...nodes.value.map((n) => n.position_y + NODE_H))
  const bw = Math.max(200, maxX - minX)
  const bh = Math.max(160, maxY - minY)
  const s = Math.min(SCALE_MAX, Math.max(SCALE_MIN, Math.min(vp.clientWidth / (bw + 120), vp.clientHeight / (bh + 120))))
  scale.value = Math.round(s * 10) / 10
  awaitTickCenter(minX, minY, bw, bh)
}

/**
 * 缩放生效后把边界框滚动居中（内部函数，nextTick 两段以等 sizer 尺寸生效）。
 */
async function awaitTickCenter(minX: number, minY: number, bw: number, bh: number): Promise<void> {
  await nextTick()
  await nextTick()
  const vp = viewport.value
  if (!vp) return
  vp.scrollLeft = Math.max(0, minX * scale.value - (vp.clientWidth - bw * scale.value) / 2)
  vp.scrollTop = Math.max(0, minY * scale.value - (vp.clientHeight - bh * scale.value) / 2)
  updateScrollState()
}

/** 小地图世界 → 缩略比例 */
const MINI_K = MINI_W / WORLD_W

/**
 * 节点在小地图上的点样式（内部 computed 数据）。
 */
const miniDots = computed(() =>
  nodes.value.map((n) => ({
    id: n.id,
    left: n.position_x * MINI_K,
    top: n.position_y * (MINI_H / WORLD_H),
    color: STATUS_COLORS[n.status] ?? STATUS_COLORS.idle,
    selected: n.id === selectedId.value,
  })),
)

/**
 * 当前视口在小地图上的矩形（随滚动与缩放更新，仅展示）。
 */
const miniRect = computed(() => ({
  left: (scrollState.left / scale.value) * MINI_K,
  top: (scrollState.top / scale.value) * (MINI_H / WORLD_H),
  width: (scrollState.width / scale.value) * MINI_K,
  height: (scrollState.height / scale.value) * (MINI_H / WORLD_H),
}))

// ============================================================
// 运行（execute + SSE stream 实时刷新）
// ============================================================

/** 运行模式：all 全部执行 / dirty 仅脏节点 / node 指定节点（先选中再运行） */
const runMode = ref<'all' | 'dirty' | 'node'>('all')
/** 执行中（stream 监听期间置位） */
const running = ref(false)
/** SSE 中止控制器 */
let streamCtl: AbortController | null = null

/**
 * 提交执行：node 模式需先选中节点；执行前先立即保存保证后端图一致；
 * 成功后开启 stream 监听节点状态实时刷新。
 */
async function run(): Promise<void> {
  if (running.value) return
  if (runMode.value === 'node' && selectedId.value == null) {
    ElMessage.warning('指定节点模式需先在画布中选中一个节点')
    return
  }
  await persist()
  try {
    const resp = await canvasApi.execute(canvasId, {
      mode: runMode.value,
      node_id: runMode.value === 'node' ? (selectedId.value as number) : undefined,
    })
    ElMessage.success(`已提交执行：执行 ${resp.executed.length} 个节点 · 跳过 ${resp.skipped.length} · 生成任务 ${resp.tasks.length} 个`)
    startStream()
  } catch {
    // 400（成环/模式非法）/409（积分不足）已由拦截器提示
  }
}

/**
 * 开启 GET SSE 执行流：事件 'nodes' 携带节点状态增量，'done' 结束监听。
 */
function startStream(): void {
  stopStream()
  streamCtl = new AbortController()
  running.value = true
  void canvasApi.stream(
    canvasId,
    (event, data) => {
      if (event === 'nodes') {
        const payload = data as { nodes?: { id: number; status: string; is_dirty: boolean; data_output?: unknown }[] }
        for (const sn of payload.nodes ?? []) {
          const local = nodes.value.find((n) => n.id === sn.id)
          if (!local) continue
          local.status = sn.status
          local.is_dirty = sn.is_dirty
          // 后端把执行产出写入 data.output（如 text 节点的输出文本），同步到本地供预览
          if (sn.data_output !== null && sn.data_output !== undefined) {
            local.data = { ...local.data, output: sn.data_output }
          }
        }
      } else if (event === 'done') {
        ElMessage.success('画布执行批次完成')
        stopStream()
      }
    },
    streamCtl.signal,
  )
    .catch(() => {
      /* 流异常/中断：静默结束（任务失败状态已由 nodes 事件刷新） */
    })
    .finally(() => {
      running.value = false
      streamCtl = null
    })
}

/** 停止 stream 监听 */
function stopStream(): void {
  streamCtl?.abort()
  streamCtl = null
  running.value = false
}

// ============================================================
// 画布助手（assistantChat）
// ============================================================

/** 助手输入 */
const assistantInput = ref('')
/** 对话记录 */
const chatLog = ref<{ role: 'user' | 'assistant'; text: string }[]>([])
/** 请求中 */
const chatBusy = ref(false)

/**
 * 发送助手消息：展示 reply + applied 数，并用响应的 nodes/edges 刷新画布。
 */
async function sendChat(): Promise<void> {
  const msg = assistantInput.value.trim()
  if (!msg || chatBusy.value) return
  chatLog.value.push({ role: 'user', text: msg })
  assistantInput.value = ''
  chatBusy.value = true
  try {
    const resp = await canvasApi.assistantChat(canvasId, msg)
    chatLog.value.push({ role: 'assistant', text: `${resp.reply}（已应用 ${resp.applied} 个动作）` })
    // 画布自动刷新：直接采用助手应用动作后的最新图
    nodes.value = resp.nodes.map((n) => ({ ...n, data: { ...(n.data ?? {}) }, config: { ...(n.config ?? {}) } }))
    edges.value = resp.edges.map((e) => ({ ...e }))
    saveState.value = 'saved'
    savedAt.value = new Date()
  } catch {
    chatLog.value.push({ role: 'assistant', text: '请求失败，请稍后重试' })
  } finally {
    chatBusy.value = false
  }
}

// ============================================================
// 导入 / 导出（工具条）
// ============================================================

/**
 * 导出当前画布 JSON（exportCanvas → Blob 下载）。
 */
async function exportJson(): Promise<void> {
  try {
    const resp = await canvasApi.exportCanvas(canvasId)
    const blob = new Blob([JSON.stringify(resp, null, 2)], { type: 'application/json' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${graph.value?.canvas.title ?? 'canvas'}-${canvasId}.json`
    a.click()
    URL.revokeObjectURL(a.href)
    ElMessage.success('已导出 JSON')
  } catch {
    // 错误已由拦截器提示
  }
}

/**
 * 导入 JSON 覆盖当前画布：读取文件 → confirm → saveGraph 全量同步（不含即删，等效覆盖）。
 */
async function onImportFile(uploadFile: UploadFile): Promise<void> {
  const raw = uploadFile.raw
  if (!raw) return
  let parsed: CanvasGraph
  try {
    parsed = JSON.parse(await raw.text()) as CanvasGraph
    if (!parsed?.nodes || !Array.isArray(parsed.nodes)) throw new Error('bad')
  } catch {
    ElMessage.error('JSON 格式错误：须为画布导出的完整图（含 nodes/edges）')
    return
  }
  try {
    await ElMessageBox.confirm('导入将覆盖当前画布的全部节点与连线，是否继续？', '导入 JSON', { type: 'warning' })
  } catch {
    return
  }
  try {
    await canvasApi.saveGraph(canvasId, {
      nodes: parsed.nodes.map((n) => ({
        node_type: n.node_type,
        position_x: n.position_x,
        position_y: n.position_y,
        data: n.data ?? undefined,
        config: n.config ?? undefined,
      })),
      edges: (parsed.edges ?? []).map((e) => ({
        source_node_id: e.source_node_id,
        target_node_id: e.target_node_id,
      })),
    })
    await loadGraph()
    ElMessage.success('导入成功')
  } catch {
    // 400 成环/类型非法已由拦截器提示
  }
}

// ============================================================
// 节点展示辅助
// ============================================================

/**
 * 相对存储路径补 /storage 前缀（内部函数）。
 */
function resolveUrl(u: unknown): string {
  if (typeof u !== 'string' || !u) return ''
  if (/^https?:\/\//.test(u) || u.startsWith('/')) return u
  return `/storage/${u}`
}

/**
 * 判断值是否像资源 URL（内部函数）。
 */
function looksLikeUrl(v: unknown): boolean {
  return typeof v === 'string' && (/^(https?:)?\/\//.test(v) || v.startsWith('/storage/') || /^(generated|uploads)\//.test(v))
}

/** 节点预览图 URL（data.output 优先，其次 data.url；入参可为空） */
function previewUrl(node: CanvasNode | null): string {
  if (!node) return ''
  const out = (node.data as Record<string, unknown> | null)?.output
  if (looksLikeUrl(out)) return resolveUrl(out)
  const url = (node.data as Record<string, unknown> | null)?.url
  return looksLikeUrl(url) ? resolveUrl(url) : ''
}

/**
 * 节点标题（data.label 或类型名；入参可为空）。
 */
function nodeTitle(node: CanvasNode | null): string {
  if (!node) return ''
  const label = (node.data as Record<string, unknown> | null)?.label
  return (typeof label === 'string' && label) || TYPE_LABELS[node.node_type as NodeType]
}

/**
 * 节点正文摘要（text/生成提示词/描述/表达式等，取不到则显示类型占位）。
 */
function nodeSummary(node: CanvasNode | null): string {
  if (!node) return ''
  const d = (node.data ?? {}) as Record<string, unknown>
  const pick = d.text ?? d.prompt ?? d.description ?? d.expression ?? d.label
  const text = typeof pick === 'string' ? pick : ''
  if (!text) return node.node_type === 'text' ? '双击检查器输入文本' : '（无内容）'
  return text.length > 60 ? `${text.slice(0, 60)}…` : text
}

/**
 * 节点输出文本（data.output 为非 URL 标量时的展示；入参可为空）。
 */
function outputText(node: CanvasNode | null): string {
  if (!node) return ''
  const out = (node.data as Record<string, unknown> | null)?.output
  if (out == null || looksLikeUrl(out)) return ''
  return typeof out === 'string' ? out : JSON.stringify(out)
}

/**
 * 节点面板图标（入参可为空，供模板调用免收窄）。
 */
function iconOf(node: CanvasNode | null): string {
  if (!node) return '⚙️'
  return NODE_PALETTE.find((p) => p.type === node.node_type)?.icon ?? '⚙️'
}

/**
 * 节点类型中文名（入参可为空，供模板调用免收窄）。
 */
function typeOf(node: CanvasNode | null): string {
  if (!node) return ''
  return TYPE_LABELS[node.node_type as NodeType] ?? node.node_type
}

/**
 * 文本输出截断展示。
 */
function clippedOutput(node: CanvasNode): string {
  const t = outputText(node)
  return t.length > 80 ? `${t.slice(0, 80)}…` : t
}

// ============================================================
// 检查器表单（按 node_type 编辑 data / config）
// ============================================================

/**
 * 表单字段写入辅助：直接改本地节点 + 调度保存（debounce 1s）。
 * 入参允许为空（模板中 selectedNode 可能为 null，免收窄断言）。
 * @param node 节点
 * @param key data 键
 * @param value 值
 */
function setData(node: CanvasNode | null, key: string, value: unknown): void {
  if (!node) return
  node.data = { ...(node.data ?? {}), [key]: value }
  scheduleSave()
}

/**
 * config 字段写入（generate.task_type）。
 */
function setConfig(node: CanvasNode | null, key: string, value: unknown): void {
  if (!node) return
  node.config = { ...(node.config ?? {}), [key]: value }
  scheduleSave()
}

/** generate 可选任务类型 */
const taskTypeOptions = [
  { value: 'image', label: '文生图' },
  { value: 'video', label: '视频生成' },
  { value: 'tts', label: '配音生成' },
]

// ============================================================
// 快捷键（window keydown）
// ============================================================

/**
 * 全局快捷键（注释）：
 * - Ctrl/⌘ + S：立即保存（阻止浏览器默认保存页面）；
 * - Delete / Backspace：删除选中节点（输入控件内不劫持）；
 * - F：适配视图（全部节点入视口）。
 */
function onKeydown(e: KeyboardEvent): void {
  const target = e.target as HTMLElement | null
  const inEditable = !!target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault()
    void persist()
    return
  }
  if (inEditable) return
  if (e.key === 'Delete' || e.key === 'Backspace') {
    if (selectedId.value != null) {
      e.preventDefault()
      void removeSelected()
    }
  } else if (e.key.toLowerCase() === 'f') {
    fitView()
  }
}

/** 点击空白处取消选中 */
function onViewportBlank(): void {
  selectedId.value = null
}

// ============================================================
// 生命周期
// ============================================================

onMounted(() => {
  void loadGraph()
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('resize', updateScrollState)
})

onBeforeUnmount(() => {
  // 离开前若有未保存变更做一次最终保存（fire-and-forget）
  if (saveState.value === 'unsaved') void persist()
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', updateScrollState)
  stopStream()
})
</script>

<template>
  <div v-loading="loading" class="editor">
    <!-- ============ 顶部工具条 ============ -->
    <header class="toolbar">
      <el-button size="small" @click="router.push('/canvas')">← 返回</el-button>
      <span class="canvas-title" :title="graph?.canvas?.title">{{ graph?.canvas?.title ?? '画布' }}</span>

      <span class="divider" />

      <!-- 缩放 -->
      <div class="zoom-group">
        <button type="button" class="zoom-btn" title="缩小" @click="scale = Math.max(SCALE_MIN, Math.round((scale - 0.1) * 10) / 10)">−</button>
        <span class="zoom-value">{{ Math.round(scale * 100) }}%</span>
        <button type="button" class="zoom-btn" title="放大" @click="scale = Math.min(SCALE_MAX, Math.round((scale + 0.1) * 10) / 10)">＋</button>
        <el-button size="small" link type="primary" @click="fitView">适配 (F)</el-button>
      </div>

      <span class="divider" />

      <!-- 保存状态 -->
      <span class="save-state" :class="`is-${saveState}`">● {{ saveText }}</span>

      <span class="spacer" />

      <!-- 运行 -->
      <el-select v-model="runMode" size="small" style="width: 130px">
        <el-option label="全部执行" value="all" />
        <el-option label="仅脏节点" value="dirty" />
        <el-option label="指定节点" value="node" />
      </el-select>
      <el-button type="primary" size="small" :loading="running" @click="run">运行</el-button>

      <span class="divider" />

      <!-- 导入 / 导出 / 自动保存 -->
      <el-upload :show-file-list="false" accept=".json" :auto-upload="false" :on-change="onImportFile">
        <el-button size="small">导入</el-button>
      </el-upload>
      <el-button size="small" @click="exportJson">导出</el-button>
      <el-tooltip content="开启后变更 1 秒后自动全量同步保存" placement="bottom">
        <span class="autosave">
          自动保存
          <el-switch v-model="autoSave" size="small" @change="(v: unknown) => (v ? scheduleSave() : undefined)" />
        </span>
      </el-tooltip>
    </header>

    <div class="editor-body">
      <!-- ============ 左侧节点面板 ============ -->
      <aside class="palette">
        <div class="palette-title">节点</div>
        <button
          v-for="p in NODE_PALETTE"
          :key="p.type"
          type="button"
          class="palette-item"
          :title="`添加${p.label}节点`"
          @click="addNode(p.type)"
        >
          <span class="pi-icon" aria-hidden="true">{{ p.icon }}</span>
          <span>{{ p.label }}</span>
        </button>
        <div class="palette-tip">
          <p>拖动右侧圆点连线</p>
          <p>Ctrl+滚轮缩放</p>
          <p>F 适配 · Delete 删除</p>
        </div>
      </aside>

      <!-- ============ 中部画布区 ============ -->
      <section ref="viewport" class="viewport" @scroll="updateScrollState" @wheel="onWheel" @mousedown="onViewportBlank">
        <!-- 缩放滚动范围占位 -->
        <div class="world-sizer" :style="{ width: `${WORLD_W * scale}px`, height: `${WORLD_H * scale}px` }">
          <!-- 世界层（网格背景 + 节点 + SVG 连线） -->
          <div
            ref="world"
            class="world"
            :style="{ width: `${WORLD_W}px`, height: `${WORLD_H}px`, transform: `scale(${scale})` }"
          >
            <!-- SVG 连线层（先画线，节点盖于其上） -->
            <svg class="edge-layer" :width="WORLD_W" :height="WORLD_H">
              <g v-for="edge in edges" :key="edge.id">
                <!-- 可见贝塞尔曲线 -->
                <path :d="edgePath(edge)" class="edge-line" />
                <!-- 加粗透明命中层：点击删除连线 -->
                <path
                  :d="edgePath(edge)"
                  class="edge-hit"
                  title="点击删除连线"
                  @click.stop="removeEdge(edge)"
                />
              </g>
              <!-- 连线中的临时虚线 -->
              <path v-if="tempEdgePath" :d="tempEdgePath" class="edge-temp" />
            </svg>

            <!-- 空画布引导 -->
            <div v-if="nodes.length === 0 && !loading" class="empty-guide">
              <p class="eg-title">空画布</p>
              <p>从左侧面板添加节点，或到右侧「助手」对话式生成子图</p>
            </div>

            <!-- 节点 -->
            <div
              v-for="n in nodes"
              :key="n.id"
              class="cnode"
              :class="{
                selected: n.id === selectedId,
                dirty: n.is_dirty,
                connecting: connecting?.sourceId === n.id,
              }"
              :style="{ left: `${n.position_x}px`, top: `${n.position_y}px`, width: `${NODE_W}px`, height: `${NODE_H}px` }"
              @mousedown="onNodeMouseDown($event, n)"
            >
              <div class="cnode-head">
                <span class="cnode-icon">{{ NODE_PALETTE.find((p) => p.type === n.node_type)?.icon ?? '⚙️' }}</span>
                <span class="cnode-label" :title="nodeTitle(n)">{{ nodeTitle(n) }}</span>
                <span
                  class="status-dot"
                  :class="{ pulse: n.status === 'running' }"
                  :style="{ background: STATUS_COLORS[n.status] ?? STATUS_COLORS.idle }"
                  :title="`状态：${n.status}${n.is_dirty ? ' · 有变更' : ''}`"
                />
              </div>
              <div class="cnode-body">
                <el-image
                  v-if="previewUrl(n)"
                  :src="previewUrl(n)"
                  fit="cover"
                  class="cnode-img"
                  :preview-src-list="[previewUrl(n)]"
                  preview-teleported
                  @mousedown.stop
                >
                  <template #error>
                    <div class="cnode-note">图片加载失败</div>
                  </template>
                </el-image>
                <div v-else-if="outputText(n)" class="cnode-note" :title="outputText(n)">{{ clippedOutput(n) }}</div>
                <div v-else class="cnode-text">{{ nodeSummary(n) }}</div>
              </div>

              <!-- 输入锚点（左中）/ 输出锚点（右中，mousedown 进入连线模式） -->
              <span class="anchor anchor-in" title="输入" />
              <span
                class="anchor anchor-out"
                title="拖动连线到目标节点"
                @mousedown="onOutAnchorDown($event, n)"
              />
            </div>
          </div>
        </div>

        <!-- 小地图（仅展示：节点点阵 + 当前视口矩形） -->
        <div class="minimap" aria-hidden="true">
          <span
            v-for="dot in miniDots"
            :key="dot.id"
            class="mini-dot"
            :class="{ selected: dot.selected }"
            :style="{ left: `${dot.left}px`, top: `${dot.top}px`, background: dot.color }"
          />
          <span
            class="mini-viewport"
            :style="{
              left: `${miniRect.left}px`,
              top: `${miniRect.top}px`,
              width: `${Math.min(MINI_W, miniRect.width)}px`,
              height: `${Math.min(MINI_H, miniRect.height)}px`,
            }"
          />
        </div>

        <!-- 底部状态条 -->
        <div class="statusbar">
          节点 {{ nodes.length }} · 连线 {{ edges.length }} · 运行中 {{ nodes.filter((n) => n.status === 'running' || n.status === 'queued').length }}
        </div>
      </section>

      <!-- ============ 右侧检查器 + 助手 ============ -->
      <aside class="inspector">
        <el-tabs class="inspector-tabs">
          <!-- 检查器 -->
          <el-tab-pane label="检查器">
            <template v-if="selectedNode">
              <div class="insp-head">
                <span class="insp-type">{{ iconOf(selectedNode) }} {{ typeOf(selectedNode) }}</span>
                <el-tag v-if="selectedNode.is_dirty" size="small" type="warning" effect="plain">有变更</el-tag>
              </div>

              <div class="insp-form">
                <!-- 标签（所有类型通用） -->
                <label class="field">
                  <span class="field-label">标签</span>
                  <el-input
                    :model-value="nodeTitle(selectedNode)"
                    size="small"
                    @input="(v: string) => setData(selectedNode, 'label', v)"
                  />
                </label>

                <!-- 文本节点 -->
                <label v-if="selectedNode.node_type === 'text'" class="field">
                  <span class="field-label">文本内容</span>
                  <el-input
                    type="textarea"
                    :rows="5"
                    :model-value="String((selectedNode.data as Record<string, unknown>)?.text ?? '')"
                    @input="(v: string) => setData(selectedNode, 'text', v)"
                  />
                </label>

                <!-- 生成节点 -->
                <template v-else-if="selectedNode.node_type === 'generate'">
                  <label class="field">
                    <span class="field-label">提示词</span>
                    <el-input
                      type="textarea"
                      :rows="5"
                      :model-value="String((selectedNode.data as Record<string, unknown>)?.prompt ?? '')"
                      @input="(v: string) => setData(selectedNode, 'prompt', v)"
                    />
                  </label>
                  <label class="field">
                    <span class="field-label">任务类型</span>
                    <el-select
                      size="small"
                      :model-value="String((selectedNode.config as Record<string, unknown>)?.task_type ?? 'image')"
                      @change="(v: unknown) => setConfig(selectedNode, 'task_type', v)"
                    >
                      <el-option v-for="t in taskTypeOptions" :key="t.value" :label="t.label" :value="t.value" />
                    </el-select>
                  </label>
                </template>

                <!-- 分镜节点 -->
                <label v-else-if="selectedNode.node_type === 'shot'" class="field">
                  <span class="field-label">画面描述</span>
                  <el-input
                    type="textarea"
                    :rows="4"
                    :model-value="String((selectedNode.data as Record<string, unknown>)?.description ?? '')"
                    @input="(v: string) => setData(selectedNode, 'description', v)"
                  />
                </label>

                <!-- 逻辑节点 -->
                <label v-else-if="selectedNode.node_type === 'logic'" class="field">
                  <span class="field-label">表达式 / 条件</span>
                  <el-input
                    type="textarea"
                    :rows="3"
                    :model-value="String((selectedNode.data as Record<string, unknown>)?.expression ?? '')"
                    @input="(v: string) => setData(selectedNode, 'expression', v)"
                  />
                </label>

                <!-- 资产引用 -->
                <label v-else-if="selectedNode.node_type === 'asset_ref'" class="field">
                  <span class="field-label">资产 ID</span>
                  <el-input-number
                    size="small"
                    :min="1"
                    :model-value="((selectedNode.data as Record<string, unknown>)?.asset_id as number | undefined) ?? undefined"
                    placeholder="项目资产 ID"
                    @change="(v: number | undefined) => setData(selectedNode, 'asset_id', v ?? undefined)"
                  />
                </label>

                <!-- 容器类（图像/视频/音频/合成/分组）：URL -->
                <label v-else class="field">
                  <span class="field-label">资源 URL</span>
                  <el-input
                    size="small"
                    placeholder="https:// 或 /storage/..."
                    :model-value="String((selectedNode.data as Record<string, unknown>)?.url ?? '')"
                    @input="(v: string) => setData(selectedNode, 'url', v)"
                  />
                </label>

                <!-- 输出（只读） -->
                <div v-if="outputText(selectedNode)" class="field">
                  <span class="field-label">执行输出</span>
                  <div class="output-box">{{ outputText(selectedNode) }}</div>
                </div>

                <el-button type="danger" plain size="small" class="del-btn" @click="removeSelected">
                  删除节点（级联删除连边）
                </el-button>
              </div>
            </template>

            <el-empty v-else description="点击画布中的节点进行编辑" :image-size="72" />
          </el-tab-pane>

          <!-- 画布助手 -->
          <el-tab-pane label="助手">
            <div class="chat">
              <div class="chat-log">
                <div v-if="chatLog.length === 0" class="chat-guide">
                  <p>试试对助手说：</p>
                  <p>「添加一个文本节点和一个图像生成节点并连线」</p>
                </div>
                <div
                  v-for="(msg, i) in chatLog"
                  :key="i"
                  class="chat-item"
                  :class="`is-${msg.role}`"
                >{{ msg.text }}</div>
              </div>
              <div class="chat-input">
                <el-input
                  v-model="assistantInput"
                  type="textarea"
                  :rows="2"
                  placeholder="描述你想在画布上做的事…"
                  @keydown.enter.exact.prevent="sendChat"
                />
                <el-button type="primary" size="small" :loading="chatBusy" @click="sendChat">发送</el-button>
              </div>
            </div>
          </el-tab-pane>
        </el-tabs>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.editor {
  display: flex;
  flex-direction: column;
  height: calc(100vh - var(--mf-topbar-h));
  background: var(--mf-bg);
  color: var(--mf-text);
  overflow: hidden;
}

/* ---------- 工具条 ---------- */
.toolbar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  padding: var(--mf-sp-2) var(--mf-sp-4);
  background: var(--mf-surface);
  border-bottom: 1px solid var(--mf-border);
  flex-shrink: 0;
  flex-wrap: wrap;
}
.canvas-title {
  max-width: 220px;
  font-weight: var(--mf-fw-semibold);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.divider {
  width: 1px;
  height: 18px;
  background: var(--mf-border-strong);
}
.spacer {
  flex: 1;
}
.zoom-group {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-1);
}
.zoom-btn {
  width: 24px;
  height: 24px;
  border: 1px solid var(--mf-border);
  border-radius: 6px;
  background: var(--mf-surface-2);
  color: var(--mf-text-2);
  cursor: pointer;
  line-height: 1;
}
.zoom-btn:hover {
  color: var(--mf-text);
  border-color: var(--mf-border-strong);
}
.zoom-value {
  min-width: 44px;
  text-align: center;
  font-size: var(--mf-fs-sm);
  font-family: var(--mf-mono);
}
.save-state {
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}
.save-state.is-saved { color: var(--mf-ok); }
.save-state.is-saving { color: var(--mf-info); }
.save-state.is-error { color: var(--mf-danger); }
.autosave {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-2);
}

/* ---------- 三栏 ---------- */
.editor-body {
  flex: 1;
  display: flex;
  min-height: 0;
}

/* 左侧面板 */
.palette {
  width: 108px;
  flex-shrink: 0;
  padding: var(--mf-sp-3);
  background: var(--mf-surface);
  border-right: 1px solid var(--mf-border);
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-2);
  overflow-y: auto;
}
.palette-title {
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
  font-weight: var(--mf-fw-semibold);
}
.palette-item {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  padding: 8px 10px;
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-surface-2);
  color: var(--mf-text);
  font-size: var(--mf-fs-sm);
  cursor: pointer;
  transition: all var(--mf-dur) var(--mf-ease);
}
.palette-item:hover {
  border-color: var(--mf-brand2);
  background: var(--mf-brand2-soft);
}
.pi-icon {
  font-size: 15px;
}
.palette-tip {
  margin-top: auto;
  padding-top: var(--mf-sp-3);
  color: var(--mf-text-3);
  font-size: 11px;
  line-height: 1.8;
}
.palette-tip p {
  margin: 0;
}

/* ---------- 画布视口 ---------- */
.viewport {
  position: relative;
  flex: 1;
  overflow: auto;
  background: var(--mf-surface-3);
}
.world-sizer {
  position: relative;
}
.world {
  position: absolute;
  top: 0;
  left: 0;
  transform-origin: 0 0;
  background-image:
    linear-gradient(var(--mf-border) 1px, transparent 1px),
    linear-gradient(90deg, var(--mf-border) 1px, transparent 1px);
  background-size: 40px 40px;
}
.edge-layer {
  position: absolute;
  top: 0;
  left: 0;
  pointer-events: none;
}
.edge-line {
  fill: none;
  stroke: var(--mf-brand2);
  stroke-width: 2;
  opacity: 0.85;
}
.edge-hit {
  fill: none;
  stroke: transparent;
  stroke-width: 14;
  pointer-events: stroke;
  cursor: pointer;
}
/* hover 命中层时冒泡到 g，高亮本组可见曲线 */
g:hover .edge-line {
  stroke: var(--mf-danger);
}
.edge-temp {
  fill: none;
  stroke: var(--mf-brand2);
  stroke-width: 2;
  stroke-dasharray: 6 4;
  pointer-events: none;
}

/* 空画布引导 */
.empty-guide {
  position: absolute;
  top: 38%;
  left: 50%;
  transform: translate(-50%, -50%);
  text-align: center;
  color: var(--mf-text-3);
  pointer-events: none;
}
.eg-title {
  font-size: var(--mf-fs-lg);
  font-weight: var(--mf-fw-semibold);
  color: var(--mf-text-2);
  margin: 0 0 var(--mf-sp-2);
}
.empty-guide p {
  margin: 0;
  font-size: var(--mf-fs-sm);
}

/* ---------- 节点 ---------- */
.cnode {
  position: absolute;
  border-radius: 10px;
  background: var(--mf-surface);
  border: 1.5px solid var(--mf-border-strong);
  box-shadow: var(--mf-shadow);
  cursor: grab;
  user-select: none;
  display: flex;
  flex-direction: column;
  overflow: visible;
  transition: border-color var(--mf-dur) var(--mf-ease), box-shadow var(--mf-dur) var(--mf-ease);
}
.cnode:active {
  cursor: grabbing;
}
.cnode.selected {
  border-color: var(--mf-brand2);
  box-shadow: 0 0 0 3px var(--mf-brand2-soft), var(--mf-shadow-pop);
}
.cnode.dirty:not(.selected) {
  border-style: dashed;
}
.cnode-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--mf-border);
  border-radius: 9px 9px 0 0;
  background: var(--mf-brand2-soft);
}
.cnode-icon {
  font-size: 13px;
  line-height: 1;
}
.cnode-label {
  flex: 1;
  font-size: 12px;
  font-weight: var(--mf-fw-semibold);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.status-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex-shrink: 0;
}
.status-dot.pulse {
  animation: dot-pulse 1s ease-in-out infinite;
}
@keyframes dot-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(224, 163, 58, 0.5); }
  50% { box-shadow: 0 0 0 5px rgba(224, 163, 58, 0); }
}
.cnode-body {
  flex: 1;
  padding: 6px 8px;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
}
.cnode-img {
  width: 100%;
  height: 56px;
  border-radius: 6px;
  pointer-events: auto;
}
.cnode-text,
.cnode-note {
  width: 100%;
  font-size: 11px;
  line-height: 1.5;
  color: var(--mf-text-2);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-all;
}
.cnode-note {
  color: var(--mf-info);
}

/* 锚点 */
.anchor {
  position: absolute;
  width: 11px;
  height: 11px;
  border-radius: 50%;
  background: var(--mf-surface);
  border: 2px solid var(--mf-brand2);
  top: 50%;
  transform: translateY(-50%);
  cursor: crosshair;
  transition: transform var(--mf-dur) var(--mf-ease);
}
.anchor-in { left: -6px; }
.anchor-out { right: -6px; }
.anchor:hover {
  transform: translateY(-50%) scale(1.35);
  background: var(--mf-brand2);
}
.connecting .anchor-out {
  background: var(--mf-brand2);
}

/* ---------- 小地图 ---------- */
.minimap {
  position: absolute;
  right: var(--mf-sp-4);
  bottom: var(--mf-sp-6);
  width: 120px;
  height: 90px;
  border-radius: 8px;
  background: var(--mf-surface);
  border: 1px solid var(--mf-border-strong);
  box-shadow: var(--mf-shadow-pop);
  overflow: hidden;
  z-index: 5;
}
.mini-dot {
  position: absolute;
  width: 4px;
  height: 4px;
  border-radius: 1px;
}
.mini-dot.selected {
  outline: 1px solid var(--mf-brand2);
}
.mini-viewport {
  position: absolute;
  border: 1px solid var(--mf-brand2);
  background: var(--mf-brand2-soft);
  pointer-events: none;
}

/* 底部状态条 */
.statusbar {
  position: absolute;
  left: var(--mf-sp-4);
  bottom: var(--mf-sp-5);
  padding: 3px 12px;
  border-radius: var(--mf-r-pill);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  font-size: 11px;
  color: var(--mf-text-2);
  z-index: 5;
}

/* ---------- 右侧检查器 ---------- */
.inspector {
  width: 300px;
  flex-shrink: 0;
  background: var(--mf-surface);
  border-left: 1px solid var(--mf-border);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.inspector-tabs {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 0 var(--mf-sp-3);
}
.inspector-tabs :deep(.el-tabs__content) {
  flex: 1;
  overflow-y: auto;
}
.inspector-tabs :deep(.el-tabs__header) {
  margin-bottom: var(--mf-sp-3);
}
.insp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--mf-sp-3);
}
.insp-type {
  font-weight: var(--mf-fw-semibold);
  font-size: var(--mf-fs-sm);
}
.insp-form {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.field-label {
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-2);
}
.output-box {
  padding: 8px;
  border-radius: 6px;
  background: var(--mf-ok-soft);
  color: var(--mf-text);
  font-size: var(--mf-fs-xs);
  line-height: 1.6;
  word-break: break-all;
  max-height: 120px;
  overflow-y: auto;
  white-space: pre-wrap;
}
.del-btn {
  margin-top: var(--mf-sp-2);
}

/* 助手 */
.chat {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.chat-log {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-2);
  padding-right: 2px;
}
.chat-guide {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
  line-height: 1.9;
}
.chat-guide p { margin: 0; }
.chat-item {
  padding: 8px 10px;
  border-radius: 10px;
  font-size: var(--mf-fs-xs);
  line-height: 1.7;
  max-width: 92%;
  word-break: break-all;
}
.chat-item.is-user {
  align-self: flex-end;
  background: var(--mf-brand2-soft);
  color: var(--mf-text);
}
.chat-item.is-assistant {
  align-self: flex-start;
  background: var(--mf-surface-2);
  border: 1px solid var(--mf-border);
  color: var(--mf-text);
}
.chat-input {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-2);
  padding: var(--mf-sp-2) 0;
}
.chat-input .el-button {
  align-self: flex-end;
}
</style>
