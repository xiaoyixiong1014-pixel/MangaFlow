<!--
  画布编辑器（文件级注释，C-02 —— 本任务核心，自研轻量节点画布，无第三方图库依赖）。

  三栏布局：左侧节点面板（12 类节点 + 插件分组）· 中部画布区 · 右侧检查器 + 集成 + 画布助手。
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
  - 搜索：Ctrl+F 打开浮层，按类型/标签/id 过滤，点击结果视口平移居中 + 300ms 高亮闪烁，ESC 关闭；
  - 框选：画布空白 mousedown 拖拽半透明选框（SVG rect），mouseup 与节点包围盒
    相交 → 多选（selectedIds）；点击空白清空；多选态 Delete 批量删除；
  - 复制粘贴：Ctrl+C 深拷贝选中子图（组件内 ref + sessionStorage 跨画布），
    Ctrl+V 克隆粘贴（负临时 id、坐标 +40、连线端点重映射后走保存持久化）；
  - 快捷键：Delete 删除选中节点、Ctrl+S 立即保存、Ctrl+F 搜索、
    Ctrl+C/V 复制粘贴、F 适配视图、ESC 关闭搜索；
  - 运行：execute（all / dirty / node 三种模式）→ 开启 GET SSE stream 监听
    'nodes'（节点状态实时刷新）与 'done'（批次结束）；
  - 画布助手：assistantChat 对话，回复后按后端返回的 nodes/edges 自动刷新画布；
  - 插件/集成（Task 9）：左侧插件分组（listPlugins）一键添加 plugin 节点；
    右侧"集成"页签管理 MCP server（operator 可增删）、浏览工具并添加
    mcp_tool 节点；检查器支持 plugin 执行与 mcp_tool arguments JSON 编辑。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadRequestOptions, UploadFile } from 'element-plus'
import { Cpu, Connection } from '@element-plus/icons-vue'
import type { Component } from 'vue'
import * as canvasApi from '@/api/canvas'
import type {
  CanvasEdge,
  CanvasGraph,
  CanvasNode,
  GraphSyncPayload,
  McpServer,
  McpToolInfo,
} from '@/types'
import type { PluginManifest } from '@mangaflow/design/plugins'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
/** 认证 store：isOperator 控制 MCP server 的增删表单可见性 */
const authStore = useAuthStore()

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

/** 节点类型联合（Task 9 追加 plugin/mcp_tool 集成类节点） */
type NodeType = 'text' | 'image' | 'video' | 'audio' | 'generate' | 'asset_ref' | 'shot' | 'compose' | 'logic' | 'group' | 'plugin' | 'mcp_tool'

/** 左侧面板节点定义（icon 为 emoji；epIcon 为 Element Plus 图标组件，两者取一渲染） */
const NODE_PALETTE: { type: NodeType; label: string; icon: string; epIcon?: Component }[] = [
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
  { type: 'plugin', label: '插件', icon: '', epIcon: Cpu },
  { type: 'mcp_tool', label: 'MCP 工具', icon: '', epIcon: Connection },
]

/** 类型中文名映射 */
const TYPE_LABELS: Record<NodeType, string> = {
  text: '文本', image: '图像', video: '视频', audio: '音频', generate: '生成',
  asset_ref: '资产引用', shot: '分镜', compose: '合成', logic: '逻辑', group: '分组',
  plugin: '插件', mcp_tool: 'MCP 工具',
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

/** 当前选中节点 id（单选，驱动检查器） */
const selectedId = ref<number | null>(null)
/** 选中节点计算属性 */
const selectedNode = computed(() => nodes.value.find((n) => n.id === selectedId.value) ?? null)

/** 框选多选集合（Task 9：与单选互斥使用；Delete 批量删除 / Ctrl+C 子图复制） */
const selectedIds = ref<Set<number>>(new Set())

/** 搜索面板开关（Ctrl+F 打开 / ESC 关闭） */
const searchOpen = ref(false)
/** 搜索关键词（按标签/类型/id 过滤） */
const searchKeyword = ref('')
/** 搜索定位后高亮闪烁的节点 id（300ms 自动清除） */
const highlightId = ref<number | null>(null)
/** 高亮闪烁定时器 */
let highlightTimer: number | null = null
/** 搜索结果（最多 20 条） */
const searchResults = computed<CanvasNode[]>(() => {
  const kw = searchKeyword.value.trim().toLowerCase()
  if (!kw) return []
  return nodes.value
    .filter((n) =>
      nodeTitle(n).toLowerCase().includes(kw)
      || n.node_type.toLowerCase().includes(kw)
      || String(n.id).includes(kw))
    .slice(0, 20)
})

/** 框选会话状态（世界坐标起止点；null = 未在框选） */
const marquee = ref<null | { x0: number; y0: number; x1: number; y1: number }>(null)
/** 本次 mousedown 后是否发生过移动（区分"点击空白"与"拖拽框选"） */
let marqueeMoved = false

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
    case 'plugin': return { label: '插件', text: '' }
    case 'mcp_tool': return { label: 'MCP 工具', arguments_text: '{}' }
    default: return { url: '', label: TYPE_LABELS[type] }
  }
}

/**
 * 节点默认 config（内部函数）：generate 需任务类型；plugin/mcp_tool
 * 由添加入口（插件分组 / 集成面板）写入绑定信息，此处给空占位。
 */
function defaultConfig(type: NodeType): Record<string, unknown> {
  if (type === 'generate') return { task_type: 'image' }
  if (type === 'mcp_tool') return { server_id: '', tool: '' }
  return {}
}

/**
 * 在画布可视中心添加节点（内部通用函数，palette / 插件分组 / 集成面板共用）。
 * @param type 节点类型
 * @param dataOverride 覆盖默认 data（如插件节点写入 label/text）
 * @param configOverride 覆盖默认 config（如 plugin_id / server_id / tool）
 * @returns 新建的本地节点（负临时 id）
 */
function addNodeRaw(type: NodeType, dataOverride?: Record<string, unknown>, configOverride?: Record<string, unknown>): CanvasNode {
  const vp = viewport.value
  const jitter = (nodes.value.length % 5) * 28
  const baseX = vp ? (vp.scrollLeft + vp.clientWidth / 2) / scale.value - NODE_W / 2 + jitter : 200 + jitter
  const baseY = vp ? (vp.scrollTop + vp.clientHeight / 2) / scale.value - NODE_H / 2 + jitter : 160 + jitter
  const node: CanvasNode = {
    id: nextTempId--,
    canvas_id: canvasId,
    node_type: type,
    position_x: Math.round(Math.min(WORLD_W - NODE_W, Math.max(0, baseX))),
    position_y: Math.round(Math.min(WORLD_H - NODE_H, Math.max(0, baseY))),
    size_w: NODE_W,
    size_h: NODE_H,
    z_index: nodes.value.length + 1,
    data: { ...defaultData(type), ...(dataOverride ?? {}) },
    config: { ...defaultConfig(type), ...(configOverride ?? {}) },
    is_dirty: true,
    cache_ref: null,
    loop_config: null,
    status: 'idle',
    parent_group_id: null,
  }
  nodes.value.push(node)
  selectedId.value = node.id
  selectedIds.value = new Set()
  scheduleSave()
  return node
}

/**
 * 在画布可视中心添加节点（点击左侧面板按钮触发）。
 * @param type 节点类型
 */
function addNode(type: NodeType): void {
  if (type === 'plugin' && !viewport.value) return
  // palette 的插件入口：默认绑定第一个可用插件（更精细的绑定走下方插件分组）
  if (type === 'plugin') {
    const plugin = plugins.value[0]
    if (!plugin) {
      ElMessage.info('暂无可用插件（请稍后重试或联系运营方上架）')
      return
    }
    addNodeRaw('plugin', { label: plugin.name }, { plugin_id: plugin.id, plugin_name: plugin.name })
    return
  }
  if (!viewport.value) return
  addNodeRaw(type)
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
 * 批量删除框选多选集合中的节点（Task 9，Delete 键多选态触发）。
 * 循环现有删除语义：过滤节点 + 过滤相关连边，一次 scheduleSave 落库。
 */
function removeSelectedIds(): void {
  const ids = selectedIds.value
  if (ids.size === 0) return
  const count = ids.size
  nodes.value = nodes.value.filter((n) => !ids.has(n.id))
  edges.value = edges.value.filter((e) => !ids.has(e.source_node_id) && !ids.has(e.target_node_id))
  if (selectedId.value != null && ids.has(selectedId.value)) selectedId.value = null
  selectedIds.value = new Set()
  scheduleSave()
  ElMessage.success(`已删除 ${count} 个节点（含相关连线）`)
}

// ============================================================
// 搜索定位（Ctrl+F 浮层 → 视口平移居中 + 高亮闪烁）
// ============================================================

/**
 * 打开搜索浮层（Ctrl+F 触发）。
 */
function openSearch(): void {
  searchOpen.value = true
}

/**
 * 关闭搜索浮层并清空关键词（ESC 触发）。
 */
function closeSearch(): void {
  searchOpen.value = false
  searchKeyword.value = ''
}

/**
 * 定位到节点：视口平移使其居中 + 300ms 高亮闪烁（点击搜索结果触发）。
 * @param node 目标节点
 */
async function focusNode(node: CanvasNode): Promise<void> {
  const vp = viewport.value
  if (!vp) return
  selectedId.value = node.id
  selectedIds.value = new Set()
  await nextTick()
  await nextTick()
  // 以节点中心为锚点平移视口（保持当前缩放不变）
  vp.scrollLeft = Math.max(0, (node.position_x + NODE_W / 2) * scale.value - vp.clientWidth / 2)
  vp.scrollTop = Math.max(0, (node.position_y + NODE_H / 2) * scale.value - vp.clientHeight / 2)
  updateScrollState()
  // 高亮闪烁 300ms
  highlightId.value = node.id
  if (highlightTimer) window.clearTimeout(highlightTimer)
  highlightTimer = window.setTimeout(() => { highlightId.value = null }, 300)
}

// ============================================================
// 框选多选（空白 mousedown 拖拽 SVG 半透明选框 → 包围盒相交多选）
// ============================================================

/**
 * 视口空白 mousedown：开始框选会话（点击空白清空选中在 mouseup 判定）。
 * @param e 鼠标事件（节点/锚点分支已 stopPropagation，不会进入此处）
 */
function onViewportBlank(e: MouseEvent): void {
  if (e.button !== 0) return
  const p = worldPos(e)
  marquee.value = { x0: p.x, y0: p.y, x1: p.x, y1: p.y }
  marqueeMoved = false
  window.addEventListener('mousemove', onMarqueeMove)
  window.addEventListener('mouseup', onMarqueeUp)
}

/**
 * 框选拖拽移动：更新选框终点（世界坐标）。
 * @param e 鼠标事件
 */
function onMarqueeMove(e: MouseEvent): void {
  if (!marquee.value) return
  const p = worldPos(e)
  marquee.value.x1 = p.x
  marquee.value.y1 = p.y
  marqueeMoved = true
}

/**
 * 框选结束（mouseup）：位移超过阈值视为框选 → 与节点包围盒相交者加入多选；
 * 未移动视为点击空白 → 清空单选与多选。
 */
function onMarqueeUp(): void {
  window.removeEventListener('mousemove', onMarqueeMove)
  window.removeEventListener('mouseup', onMarqueeUp)
  const m = marquee.value
  marquee.value = null
  if (!m) return
  if (!marqueeMoved) {
    selectedId.value = null
    selectedIds.value = new Set()
    return
  }
  const rx0 = Math.min(m.x0, m.x1)
  const rx1 = Math.max(m.x0, m.x1)
  const ry0 = Math.min(m.y0, m.y1)
  const ry1 = Math.max(m.y0, m.y1)
  const hits = new Set<number>()
  for (const n of nodes.value) {
    if (rx1 >= n.position_x && rx0 <= n.position_x + NODE_W
      && ry1 >= n.position_y && ry0 <= n.position_y + NODE_H) {
      hits.add(n.id)
    }
  }
  selectedIds.value = hits
  if (hits.size > 0) selectedId.value = null
}

/**
 * 框选矩形的 SVG 属性（世界坐标，edge-layer 内渲染）。
 */
const marqueeRect = computed(() => {
  const m = marquee.value
  if (!m) return null
  return {
    x: Math.min(m.x0, m.x1),
    y: Math.min(m.y0, m.y1),
    width: Math.abs(m.x1 - m.x0),
    height: Math.abs(m.y1 - m.y0),
  }
})

// ============================================================
// 复制 / 粘贴（Ctrl+C 子图深拷贝；Ctrl+V 克隆，支持跨画布 sessionStorage）
// ============================================================

/** sessionStorage 剪贴板键名（跨画布粘贴） */
const CLIPBOARD_KEY = 'mf_canvas_clipboard'
/** 组件内剪贴板（优先于 sessionStorage） */
const clipboard = ref<{ nodes: CanvasNode[]; edges: CanvasEdge[] } | null>(null)

/**
 * 复制当前选中（多选集合，多选为空时退化为单选节点）：
 * 深拷贝 nodes + 两端均在集合内的 edges，写入组件内 ref 与 sessionStorage。
 */
function copySelection(): void {
  const ids = new Set<number>(selectedIds.value)
  if (ids.size === 0 && selectedId.value != null) ids.add(selectedId.value)
  if (ids.size === 0) return
  const ns = nodes.value
    .filter((n) => ids.has(n.id))
    .map((n) => JSON.parse(JSON.stringify(n)) as CanvasNode)
  const es = edges.value
    .filter((e) => ids.has(e.source_node_id) && ids.has(e.target_node_id))
    .map((e) => ({ ...e }))
  clipboard.value = { nodes: ns, edges: es }
  try {
    sessionStorage.setItem(CLIPBOARD_KEY, JSON.stringify(clipboard.value))
  } catch {
    /* 存储配额/隐私模式失败不阻断：组件内剪贴板仍可用 */
  }
  ElMessage.success(`已复制 ${ns.length} 个节点${es.length ? `（含 ${es.length} 条连线）` : ''}`)
}

/**
 * 粘贴剪贴板：优先组件内剪贴板，否则读 sessionStorage（跨画布）。
 * 克隆为负临时 id、坐标 +40、连线端点重映射，随后 scheduleSave 持久化。
 */
function pasteClipboard(): void {
  let clip = clipboard.value
  if (!clip || clip.nodes.length === 0) {
    try {
      const raw = sessionStorage.getItem(CLIPBOARD_KEY)
      if (raw) clip = JSON.parse(raw) as { nodes: CanvasNode[]; edges: CanvasEdge[] }
    } catch {
      clip = null
    }
  }
  if (!clip || clip.nodes.length === 0) {
    ElMessage.info('剪贴板为空：请先 Ctrl+C 复制节点')
    return
  }
  // 旧 id → 新临时 id 映射；坐标 +40 偏移，状态重置为待执行
  const idMap = new Map<number, number>()
  const newNodes: CanvasNode[] = clip.nodes.map((n, i) => {
    const nid = nextTempId--
    idMap.set(n.id, nid)
    return {
      ...(JSON.parse(JSON.stringify(n)) as CanvasNode),
      id: nid,
      canvas_id: canvasId,
      position_x: n.position_x + 40,
      position_y: n.position_y + 40,
      z_index: nodes.value.length + 1 + i,
      is_dirty: true,
      cache_ref: null,
      status: 'idle',
    }
  })
  const newEdges: CanvasEdge[] = []
  for (const e of clip.edges) {
    const s = idMap.get(e.source_node_id)
    const t = idMap.get(e.target_node_id)
    if (s == null || t == null) continue
    newEdges.push({
      id: nextTempId--,
      source_node_id: s,
      target_node_id: t,
      source_port: e.source_port ?? null,
      target_port: e.target_port ?? null,
    })
  }
  nodes.value.push(...newNodes)
  edges.value.push(...newEdges)
  // 粘贴后选中新子图，便于连续 Ctrl+C/V 微调
  selectedIds.value = new Set(newNodes.map((n) => n.id))
  selectedId.value = null
  scheduleSave()
  ElMessage.success(`已粘贴 ${newNodes.length} 个节点`)
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
 * 单击节点时清空框选多选集合（多选仅由框选/粘贴产生）。
 */
function onNodeMouseDown(e: MouseEvent, node: CanvasNode): void {
  if (e.button !== 0) return
  e.stopPropagation()
  if (!selectedIds.value.has(node.id)) selectedIds.value = new Set()
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
 * 节点的 Element Plus 图标组件（plugin/mcp_tool 用 EP 图标，其余返回 null 走 emoji）。
 * @param node 节点
 * @returns EP 图标组件或 null
 */
function paletteEpIcon(node: CanvasNode | null): Component | null {
  if (!node) return null
  return NODE_PALETTE.find((p) => p.type === node.node_type)?.epIcon ?? null
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
// 插件面板 / MCP 集成（Task 9，CV-19 ~ CV-22）
// ============================================================

/** 已审核且启用的插件清单（listPlugins，左侧插件分组数据源） */
const plugins = ref<PluginManifest[]>([])
/** 插件清单加载中 */
const pluginsLoading = ref(false)

/**
 * 拉取插件清单（挂载时调用；后端仅返回 reviewed+enabled 的插件）。
 */
async function loadPlugins(): Promise<void> {
  pluginsLoading.value = true
  try {
    // 后端 PluginOut 与 @mangaflow/design 的 PluginManifest 字段对齐（结构类型兼容）
    plugins.value = (await canvasApi.listPlugins()) as unknown as PluginManifest[]
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    pluginsLoading.value = false
  }
}

/**
 * 从插件分组添加 plugin 节点：绑定 plugin_id/plugin_name，label 取插件名。
 * @param plugin 插件清单条目
 */
function addPluginNode(plugin: PluginManifest): void {
  addNodeRaw('plugin', { label: plugin.name, text: '' }, { plugin_id: plugin.id, plugin_name: plugin.name })
  ElMessage.success(`已添加插件节点：${plugin.name}`)
}

/** 按节点 config.plugin_id 查插件清单条目（检查器回显端口用） */
const selectedPlugin = computed<PluginManifest | null>(() => {
  const pid = (selectedNode.value?.config as Record<string, unknown> | null)?.plugin_id
  return plugins.value.find((p) => p.id === pid) ?? null
})

/** 单节点执行中（检查器"执行此节点"按钮 loading） */
const executingNode = ref(false)

/**
 * 执行当前选中节点（plugin/mcp_tool 检查器按钮）：先立即保存保证后端图一致，
 * 再走现有单节点执行接口，执行结果（status/data.output）写回本地节点。
 */
async function executeSelectedNode(): Promise<void> {
  const node = selectedNode.value
  if (!node) return
  if (node.id < 0) {
    ElMessage.warning('请先保存画布（新节点尚未落库）再执行')
    return
  }
  executingNode.value = true
  try {
    await persist()
    const target = nodes.value.find((n) => n.id === (node.id > 0 ? node.id : selectedId.value))
    if (!target || target.id < 0) {
      ElMessage.warning('节点尚未保存，无法执行')
      return
    }
    const resp = await canvasApi.executeNode(canvasId, target.id)
    target.status = resp.status
    target.is_dirty = resp.is_dirty
    if (resp.data) target.data = { ...(target.data ?? {}), ...resp.data }
    ElMessage.success(resp.status === 'failed' ? '节点执行失败（详见执行输出）' : '节点执行完成')
  } catch {
    /* 400/404 等已由拦截器提示 */
  } finally {
    executingNode.value = false
  }
}

/** MCP server 配置列表（GET /api/mcp/servers，站点设置 KV） */
const mcpList = ref<McpServer[]>([])
/** MCP 列表加载中 */
const mcpLoading = ref(false)
/** operator 添加表单（headers 为 JSON 文本） */
const mcpForm = reactive({ name: '', url: '', headers: '' })
/** 添加请求中 */
const mcpAdding = ref(false)
/** 浏览工具结果（server + tools/list 响应） */
const browsedTools = ref<{ server: McpServer; tools: McpToolInfo[] } | null>(null)
/** 浏览工具请求中 */
const toolsLoading = ref(false)

/**
 * 拉取 MCP server 列表（挂载时调用）。
 */
async function loadMcpServers(): Promise<void> {
  mcpLoading.value = true
  try {
    mcpList.value = (await canvasApi.mcpServers()).servers
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    mcpLoading.value = false
  }
}

/**
 * operator 添加 MCP server：headers 文本域解析为对象（空串 = 无附加头）。
 */
async function submitMcpServer(): Promise<void> {
  const name = mcpForm.name.trim()
  const url = mcpForm.url.trim()
  if (!name || !url) {
    ElMessage.warning('请填写名称与端点地址')
    return
  }
  let headers: Record<string, string> | undefined
  if (mcpForm.headers.trim()) {
    try {
      const parsed = JSON.parse(mcpForm.headers)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('bad')
      headers = parsed as Record<string, string>
    } catch {
      ElMessage.error('headers 须为合法 JSON 对象（如 {"Authorization": "Bearer x"}）')
      return
    }
  }
  mcpAdding.value = true
  try {
    mcpList.value = (await canvasApi.addMcpServer({ name, url, headers })).servers
    mcpForm.name = ''
    mcpForm.url = ''
    mcpForm.headers = ''
    ElMessage.success('MCP server 已添加')
  } catch {
    /* 403/校验错误已由拦截器提示 */
  } finally {
    mcpAdding.value = false
  }
}

/**
 * 删除 MCP server（operator）。
 * @param server 目标 server
 */
async function removeMcpServer(server: McpServer): Promise<void> {
  try {
    mcpList.value = (await canvasApi.deleteMcpServer(server.id)).servers
    if (browsedTools.value?.server.id === server.id) browsedTools.value = null
    ElMessage.success('MCP server 已删除')
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/**
 * 浏览 MCP server 工具（现连 tools/list）。
 * @param server 目标 server
 */
async function browseTools(server: McpServer): Promise<void> {
  toolsLoading.value = true
  browsedTools.value = null
  try {
    const { tools } = await canvasApi.mcpTools(server.id)
    browsedTools.value = { server, tools }
    if (!tools.length) ElMessage.info('该 server 未声明任何工具')
  } catch {
    /* 连接失败 400 已由拦截器提示 */
  } finally {
    toolsLoading.value = false
  }
}

/**
 * 从工具列表添加 mcp_tool 节点：绑定 server_id/tool，label 取工具名。
 * @param server 所属 server
 * @param tool 工具描述
 */
function addMcpNode(server: McpServer, tool: McpToolInfo): void {
  addNodeRaw(
    'mcp_tool',
    { label: tool.name, arguments_text: '{}' },
    { server_id: server.id, server_name: server.name, tool: tool.name },
  )
  ElMessage.success(`已添加 MCP 工具节点：${tool.name}`)
}

/**
 * 编辑 mcp_tool 节点的 arguments（JSON 文本域 change 触发）：
 * 解析成功写 config.arguments 与 data.arguments_text（后端兜底解析用）。
 * @param raw JSON 文本
 */
function setMcpArguments(raw: string): void {
  const node = selectedNode.value
  if (!node) return
  try {
    const parsed = JSON.parse(raw || '{}')
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('bad')
    node.config = { ...(node.config ?? {}), arguments: parsed as Record<string, unknown> }
    node.data = { ...(node.data ?? {}), arguments_text: raw }
    scheduleSave()
  } catch {
    ElMessage.warning('arguments 须为合法 JSON 对象，未保存本次修改')
  }
}

/** mcp_tool 检查器 arguments 文本域回显（config.arguments 序列化；内部 computed） */
const mcpArgumentsText = computed(() => {
  const args = (selectedNode.value?.config as Record<string, unknown> | null)?.arguments
  return JSON.stringify(args ?? {}, null, 2)
})

// ============================================================
// 快捷键（window keydown）
// ============================================================

/**
 * 全局快捷键（注释）：
 * - Ctrl/⌘ + S：立即保存（阻止浏览器默认保存页面）；
 * - Ctrl/⌘ + F：打开节点搜索浮层（阻止浏览器默认页内查找）；
 * - Ctrl/⌘ + C / V：子图复制 / 粘贴（输入控件内放行，避免劫持文本复制）；
 * - ESC：关闭搜索浮层；
 * - Delete / Backspace：删除选中节点（多选态批量删除；输入控件内不劫持）；
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
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
    e.preventDefault()
    openSearch()
    return
  }
  if (e.key === 'Escape') {
    if (searchOpen.value) {
      e.preventDefault()
      closeSearch()
    }
    return
  }
  // 复制/粘贴：输入控件内放行（避免劫持文本编辑）
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'c') {
    if (inEditable) return
    e.preventDefault()
    copySelection()
    return
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'v') {
    if (inEditable) return
    e.preventDefault()
    pasteClipboard()
    return
  }
  if (inEditable) return
  if (e.key === 'Delete' || e.key === 'Backspace') {
    if (selectedIds.value.size > 0) {
      e.preventDefault()
      removeSelectedIds()
    } else if (selectedId.value != null) {
      e.preventDefault()
      void removeSelected()
    }
  } else if (e.key.toLowerCase() === 'f') {
    fitView()
  }
}

// ============================================================
// 生命周期
// ============================================================

onMounted(() => {
  void loadGraph()
  void loadPlugins()
  void loadMcpServers()
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('resize', updateScrollState)
})

onBeforeUnmount(() => {
  // 离开前若有未保存变更做一次最终保存（fire-and-forget）
  if (saveState.value === 'unsaved') void persist()
  if (highlightTimer) window.clearTimeout(highlightTimer)
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
          <span class="pi-icon" aria-hidden="true">
            <el-icon v-if="p.epIcon"><component :is="p.epIcon" /></el-icon>
            <template v-else>{{ p.icon }}</template>
          </span>
          <span>{{ p.label }}</span>
        </button>

        <!-- 插件分组（listPlugins 动态拉取，点击添加绑定 plugin 节点） -->
        <div class="palette-title palette-group-title">插件</div>
        <div v-loading="pluginsLoading" class="plugin-list">
          <button
            v-for="p in plugins"
            :key="p.id"
            type="button"
            class="palette-item plugin-item"
            :title="p.description || p.endpoint_url"
            @click="addPluginNode(p)"
          >
            <span class="pi-icon"><el-icon><Cpu /></el-icon></span>
            <span class="plugin-name">{{ p.name }}</span>
          </button>
          <p v-if="!pluginsLoading && plugins.length === 0" class="plugin-empty">暂无可用插件</p>
        </div>

        <div class="palette-tip">
          <p>拖动右侧圆点连线</p>
          <p>Ctrl+滚轮缩放 · F 适配</p>
          <p>Ctrl+F 搜索 · 框选多选</p>
          <p>Ctrl+C/V 复制粘贴</p>
          <p>Delete 删除（多选批量）</p>
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
              <!-- 框选半透明选框（Task 9） -->
              <rect
                v-if="marqueeRect"
                class="marquee-rect"
                :x="marqueeRect.x"
                :y="marqueeRect.y"
                :width="marqueeRect.width"
                :height="marqueeRect.height"
              />
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
                'multi-selected': selectedIds.has(n.id),
                flash: n.id === highlightId,
                dirty: n.is_dirty,
                connecting: connecting?.sourceId === n.id,
              }"
              :style="{ left: `${n.position_x}px`, top: `${n.position_y}px`, width: `${NODE_W}px`, height: `${NODE_H}px` }"
              @mousedown="onNodeMouseDown($event, n)"
            >
              <div class="cnode-head">
                <span class="cnode-icon">
                  <el-icon v-if="paletteEpIcon(n)"><component :is="paletteEpIcon(n)" /></el-icon>
                  <template v-else>{{ iconOf(n) }}</template>
                </span>
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

        <!-- 搜索浮层（Ctrl+F 打开 / ESC 关闭；点击结果视口平移居中 + 高亮闪烁） -->
        <div v-if="searchOpen" class="search-panel">
          <div class="search-row">
            <el-input
              v-model="searchKeyword"
              size="small"
              clearable
              autofocus
              placeholder="搜索节点（标签 / 类型 / id）"
              @keydown.enter.prevent="() => searchResults[0] && focusNode(searchResults[0])"
            />
            <el-button size="small" text @click="closeSearch">关闭 (Esc)</el-button>
          </div>
          <div class="search-results">
            <button
              v-for="n in searchResults"
              :key="n.id"
              type="button"
              class="search-item"
              @click="focusNode(n)"
            >
              <span class="si-icon">
                <el-icon v-if="paletteEpIcon(n)"><component :is="paletteEpIcon(n)" /></el-icon>
                <template v-else>{{ iconOf(n) }}</template>
              </span>
              <span class="si-title">{{ nodeTitle(n) }}</span>
              <span class="si-meta">#{{ n.id }} · {{ typeOf(n) }}</span>
            </button>
            <p v-if="searchKeyword && searchResults.length === 0" class="search-empty">无匹配节点</p>
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

                <!-- 插件节点（Task 9：插件名/输入输出端口回显 + 手动执行） -->
                <template v-else-if="selectedNode.node_type === 'plugin'">
                  <div class="field">
                    <span class="field-label">绑定插件</span>
                    <div class="binding-box">
                      {{ String((selectedNode.config as Record<string, unknown>)?.plugin_name ?? '未绑定') }}
                      <el-tag v-if="selectedPlugin" size="small" type="success" effect="plain">已审核启用</el-tag>
                    </div>
                  </div>
                  <div v-if="selectedPlugin" class="field">
                    <span class="field-label">输入端口</span>
                    <div class="binding-box ports">
                      <el-tag v-for="port in selectedPlugin.inputs" :key="port.name" size="small" effect="plain">
                        {{ port.name }}: {{ port.type }}
                      </el-tag>
                      <span v-if="selectedPlugin.inputs.length === 0" class="port-none">（无）</span>
                    </div>
                  </div>
                  <div v-if="selectedPlugin" class="field">
                    <span class="field-label">输出端口</span>
                    <div class="binding-box ports">
                      <el-tag v-for="port in selectedPlugin.outputs" :key="port.name" size="small" type="info" effect="plain">
                        {{ port.name }}: {{ port.type }}
                      </el-tag>
                      <span v-if="selectedPlugin.outputs.length === 0" class="port-none">（无）</span>
                    </div>
                  </div>
                  <label class="field">
                    <span class="field-label">输入文本（payload.text）</span>
                    <el-input
                      type="textarea"
                      :rows="4"
                      :model-value="String((selectedNode.data as Record<string, unknown>)?.text ?? '')"
                      @input="(v: string) => setData(selectedNode, 'text', v)"
                    />
                  </label>
                  <el-button
                    type="primary"
                    plain
                    size="small"
                    :loading="executingNode"
                    class="exec-btn"
                    @click="executeSelectedNode"
                  >
                    执行此节点
                  </el-button>
                </template>

                <!-- MCP 工具节点（Task 9：server/tool 回显 + arguments JSON 编辑 + 手动执行） -->
                <template v-else-if="selectedNode.node_type === 'mcp_tool'">
                  <div class="field">
                    <span class="field-label">绑定工具</span>
                    <div class="binding-box">
                      {{ String((selectedNode.config as Record<string, unknown>)?.server_name ?? '未绑定 server') }}
                      /
                      {{ String((selectedNode.config as Record<string, unknown>)?.tool ?? '未绑定 tool') }}
                    </div>
                  </div>
                  <label class="field">
                    <span class="field-label">
                      arguments（JSON，支持 <span v-pre>{{node:&lt;id&gt;.output}}</span> 占位引用上游输出）
                    </span>
                    <el-input
                      type="textarea"
                      :rows="5"
                      :model-value="mcpArgumentsText"
                      @change="(v: string) => setMcpArguments(v)"
                    />
                  </label>
                  <el-button
                    type="primary"
                    plain
                    size="small"
                    :loading="executingNode"
                    class="exec-btn"
                    @click="executeSelectedNode"
                  >
                    执行此节点
                  </el-button>
                </template>

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

          <!-- 集成（Task 9：MCP server 管理 + 工具浏览 → 添加 mcp_tool 节点） -->
          <el-tab-pane label="集成">
            <div class="integrate">
              <div class="integrate-head">
                <span class="integrate-title">MCP Servers</span>
                <el-button size="small" text :loading="mcpLoading" @click="loadMcpServers">
                  <el-icon><Connection /></el-icon>&nbsp;刷新
                </el-button>
              </div>

              <!-- server 列表 -->
              <div v-for="s in mcpList" :key="s.id" class="mcp-server">
                <div class="mcp-server-row">
                  <span class="mcp-name" :title="s.url">{{ s.name }}</span>
                  <el-button size="small" text type="primary" :loading="toolsLoading" @click="browseTools(s)">
                    浏览工具
                  </el-button>
                  <el-button v-if="authStore.isOperator" size="small" text type="danger" @click="removeMcpServer(s)">
                    删除
                  </el-button>
                </div>
                <div v-if="browsedTools?.server.id === s.id" class="mcp-tools">
                  <p v-if="browsedTools.tools.length === 0" class="mcp-tool-empty">无工具</p>
                  <div v-for="t in browsedTools.tools" :key="t.name" class="mcp-tool">
                    <div class="mcp-tool-info">
                      <span class="mcp-tool-name">{{ t.name }}</span>
                      <span class="mcp-tool-desc" :title="t.description ?? ''">{{ t.description || '（无描述）' }}</span>
                    </div>
                    <el-button size="small" text type="primary" @click="addMcpNode(s, t)">添加节点</el-button>
                  </div>
                </div>
              </div>
              <p v-if="!mcpLoading && mcpList.length === 0" class="mcp-empty">尚未配置 MCP server</p>

              <!-- operator 添加表单 -->
              <template v-if="authStore.isOperator">
                <div class="integrate-head form-head">
                  <span class="integrate-title">添加 Server（运营方）</span>
                </div>
                <label class="field">
                  <span class="field-label">名称</span>
                  <el-input v-model="mcpForm.name" size="small" placeholder="如 demo-mcp" />
                </label>
                <label class="field">
                  <span class="field-label">端点地址（streamable HTTP）</span>
                  <el-input v-model="mcpForm.url" size="small" placeholder="https://host/mcp" />
                </label>
                <label class="field">
                  <span class="field-label">附加请求头（JSON，可选）</span>
                  <el-input
                    v-model="mcpForm.headers"
                    type="textarea"
                    :rows="2"
                    placeholder='{"Authorization": "Bearer xxx"}'
                  />
                </label>
                <el-button type="primary" size="small" :loading="mcpAdding" class="exec-btn" @click="submitMcpServer">
                  添加 Server
                </el-button>
              </template>
              <p v-else class="mcp-empty">MCP server 由运营方统一配置（如需调整请联系运营方）</p>
            </div>
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
/* 插件分组（Task 9） */
.palette-group-title {
  margin-top: var(--mf-sp-2);
}
.plugin-list {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-2);
  min-height: 18px;
}
.plugin-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.plugin-empty {
  margin: 0;
  font-size: 11px;
  color: var(--mf-text-3);
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

/* ---------- 框选 / 多选 / 高亮（Task 9） ---------- */
.marquee-rect {
  fill: var(--mf-brand2-soft);
  stroke: var(--mf-brand2);
  stroke-width: 1;
  stroke-dasharray: 4 3;
}
.cnode.multi-selected {
  border-color: var(--mf-brand2);
  box-shadow: 0 0 0 2px var(--mf-brand2-soft), var(--mf-shadow);
}
/* 搜索定位高亮闪烁（300ms） */
.cnode.flash {
  animation: node-flash 0.15s ease-in-out 2;
}
@keyframes node-flash {
  0%, 100% { box-shadow: 0 0 0 0 var(--mf-brand2-soft), var(--mf-shadow); }
  50% { box-shadow: 0 0 0 6px var(--mf-brand2), var(--mf-shadow-pop); }
}

/* ---------- 搜索浮层（Ctrl+F） ---------- */
.search-panel {
  position: absolute;
  top: var(--mf-sp-3);
  left: 50%;
  transform: translateX(-50%);
  width: 360px;
  max-width: calc(100% - 24px);
  padding: var(--mf-sp-2);
  border-radius: 10px;
  background: var(--mf-surface);
  border: 1px solid var(--mf-border-strong);
  box-shadow: var(--mf-shadow-pop);
  z-index: 8;
}
.search-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.search-results {
  margin-top: var(--mf-sp-2);
  max-height: 240px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.search-item {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  width: 100%;
  padding: 6px 8px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--mf-text);
  font-size: var(--mf-fs-sm);
  cursor: pointer;
  text-align: left;
}
.search-item:hover {
  background: var(--mf-brand2-soft);
}
.si-icon {
  width: 18px;
  display: inline-flex;
  justify-content: center;
}
.si-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.si-meta {
  color: var(--mf-text-3);
  font-size: 11px;
  font-family: var(--mf-mono);
}
.search-empty {
  margin: 0;
  padding: 6px 8px;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}

/* ---------- 检查器绑定信息 / 执行按钮 ---------- */
.binding-box {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--mf-surface-2);
  border: 1px solid var(--mf-border);
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-2);
  word-break: break-all;
}
.binding-box.ports {
  flex-wrap: wrap;
}
.port-none {
  color: var(--mf-text-3);
}
.exec-btn {
  align-self: flex-start;
}

/* ---------- 集成页签（MCP） ---------- */
.integrate {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
}
.integrate-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.integrate-title {
  font-size: var(--mf-fs-sm);
  font-weight: var(--mf-fw-semibold);
}
.form-head {
  margin-top: var(--mf-sp-2);
  border-top: 1px dashed var(--mf-border);
  padding-top: var(--mf-sp-3);
}
.mcp-server {
  padding: var(--mf-sp-2);
  border: 1px solid var(--mf-border);
  border-radius: 8px;
  background: var(--mf-surface-2);
}
.mcp-server-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-1);
}
.mcp-name {
  flex: 1;
  font-size: var(--mf-fs-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mcp-tools {
  margin-top: var(--mf-sp-2);
  display: flex;
  flex-direction: column;
  gap: 2px;
  border-top: 1px dashed var(--mf-border);
  padding-top: var(--mf-sp-2);
}
.mcp-tool {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.mcp-tool-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.mcp-tool-name {
  font-size: var(--mf-fs-xs);
  font-weight: var(--mf-fw-semibold);
  font-family: var(--mf-mono);
}
.mcp-tool-desc {
  font-size: 11px;
  color: var(--mf-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mcp-tool-empty,
.mcp-empty {
  margin: 0;
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}
</style>
