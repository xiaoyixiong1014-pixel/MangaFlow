/**
 * AI 画布 API（文件级注释）。
 *
 * 对应后端 /api/canvases：CRUD / 图全量同步 / 导入导出 /
 * 单节点与 DAG 增量执行 / 执行流（GET SSE）/ 画布助手。
 */
import client from './client'
import { sseGet } from './sse'
import type {
  AssistantChatResult,
  Canvas,
  CanvasCreatePayload,
  CanvasExecuteResult,
  CanvasGraph,
  CanvasListItem,
  CanvasPluginItem,
  CanvasSinkResult,
  CloneSubtreeResult,
  GraphSyncPayload,
  McpServersResult,
  McpToolInfo,
  NodeExecuteResult,
  PageQuery,
  PageResult,
} from '@/types'

/**
 * 分页查询画布列表（含节点/连线计数）。
 * @param params 分页/项目过滤参数
 * @returns 画布分页列表
 */
export function list(params?: PageQuery & { project_id?: number }): Promise<PageResult<CanvasListItem>> {
  return client.get('/canvases', { params }).then((r) => r.data)
}

/**
 * 创建画布（可选用预置模板）。
 * @param payload 名称/关联项目/模板
 * @returns 新建画布完整图
 */
export function create(payload: CanvasCreatePayload): Promise<CanvasGraph> {
  return client.post('/canvases', payload).then((r) => r.data)
}

/**
 * 查询画布完整图（节点/连线）。
 * @param id 画布 ID
 * @returns 画布完整图
 * @throws 404 不存在/非本人
 */
export function getGraph(id: number): Promise<CanvasGraph> {
  return client.get(`/canvases/${id}`).then((r) => r.data)
}

/**
 * 更新画布基础信息（名称/视口/缩略图）。
 * @param id 画布 ID
 * @param payload 更新内容（仅传入字段生效）
 * @returns 更新后的画布基础信息
 */
export function update(
  id: number,
  payload: { title?: string; viewport?: Record<string, unknown>; thumbnail_url?: string },
): Promise<Canvas> {
  return client.put(`/canvases/${id}`, payload).then((r) => r.data)
}

/**
 * 删除画布（级联删除节点/连线）。
 * @param id 画布 ID
 * @returns 消息响应
 */
export function remove(id: number): Promise<{ message: string }> {
  return client.delete(`/canvases/${id}`).then((r) => r.data)
}

/**
 * 图全量同步（自动保存：未出现的节点/连线将被删除，成环返回 400 回滚）。
 * @param id 画布 ID
 * @param graph 全量节点与连线
 * @returns 同步后的完整图
 */
export function saveGraph(id: number, graph: GraphSyncPayload): Promise<CanvasGraph> {
  return client.put(`/canvases/${id}/graph`, graph).then((r) => r.data)
}

/**
 * 导出画布 JSON（返回完整图结构供下载）。
 * @param id 画布 ID
 * @returns 画布完整图
 */
export function exportCanvas(id: number): Promise<CanvasGraph> {
  return client.post(`/canvases/${id}/export`).then((r) => r.data)
}

/**
 * 从 JSON 图导入新建画布（支持客户端临时 id 引用，成环校验失败不建画布）。
 * @param payload 名称/关联项目/图数据
 * @returns 新建画布完整图
 */
export function importCanvas(payload: {
  title: string
  project_id?: number
  graph: GraphSyncPayload
}): Promise<CanvasGraph> {
  return client.post('/canvases/import', payload).then((r) => r.data)
}

/**
 * 执行单个节点。
 * @param id 画布 ID
 * @param nodeId 节点 ID
 * @returns 节点执行后状态
 */
export function executeNode(id: number, nodeId: number): Promise<NodeExecuteResult> {
  return client.post(`/canvases/${id}/nodes/${nodeId}/execute`).then((r) => r.data)
}

/**
 * DAG 增量执行（all 全部 / dirty 仅脏节点 / node 指定起点）。
 * @param id 画布 ID
 * @param payload 执行模式与可选节点 ID
 * @returns 执行/跳过节点与新建任务
 */
export function execute(
  id: number,
  payload: { mode: 'all' | 'dirty' | 'node'; node_id?: number },
): Promise<CanvasExecuteResult> {
  return client.post(`/canvases/${id}/execute`, payload).then((r) => r.data)
}

/**
 * 订阅画布执行流（GET SSE，节点状态实时推送）。
 * @param id 画布 ID
 * @param onEvent 事件回调
 * @param signal 中止信号
 */
export async function stream(
  id: number,
  onEvent: (event: string, data: unknown) => void,
  signal?: AbortSignal,
): Promise<void> {
  await sseGet(`/api/canvases/${id}/stream`, onEvent, signal)
}

/**
 * 画布助手对话（LLM 解析指令并应用画布动作）。
 * @param id 画布 ID
 * @param message 用户消息（1~2000 字符）
 * @returns 回复、应用动作数与更新后的图
 */
export function assistantChat(id: number, message: string): Promise<AssistantChatResult> {
  return client.post(`/canvases/${id}/assistant/chat`, { message }).then((r) => r.data)
}

// ============================================================
// 画布插件 / MCP 集成 / 子图克隆（Task 9 画布增强）
// ============================================================

/**
 * 拉取画布插件列表（普通用户 = 已审核且启用；operator 传 all 查看全部）。
 * @param all operator 置 true 查看全部
 * @returns 插件清单列表
 */
export function listPlugins(all = false): Promise<CanvasPluginItem[]> {
  return client.get('/canvas-plugins', { params: all ? { all: true } : undefined }).then((r) => r.data)
}

/**
 * 手动调用插件（插件须已审核且启用）。
 * @param id 插件 ID
 * @param payload 调用负载（如 { text: string }）
 * @returns 插件返回结果
 */
export function invokePlugin(id: number, payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  return client.post(`/canvas-plugins/${id}/invoke`, { payload }).then((r) => r.data)
}

/**
 * 子图克隆：克隆指定节点与内部连线到目标画布（缺省克隆到自身，坐标 +40/+40）。
 * @param canvasId 源画布 ID
 * @param payload 节点 ID 列表与可选目标画布 ID
 * @returns 克隆出的节点与连线
 */
export function cloneSubtree(
  canvasId: number,
  payload: { node_ids: number[]; target_canvas_id?: number },
): Promise<CloneSubtreeResult> {
  return client.post(`/canvases/${canvasId}/clone-subtree`, payload).then((r) => r.data)
}

/**
 * 画布节点沉淀入资产库（A-10/CV-24：仅 image 节点且有产物时可用）。
 * @param nodeId 节点 ID
 * @param payload 资产类型（character/scene/prop）与可选名称（缺省取节点标签）
 * @returns 资产 ID/类型与是否重复沉淀（duplicated=true 时返回既有资产 ID）
 */
export function sinkNode(
  nodeId: number,
  payload: { asset_type: 'character' | 'scene' | 'prop'; name?: string },
): Promise<CanvasSinkResult> {
  return client.post(`/canvas/nodes/${nodeId}/sink`, payload).then((r) => r.data)
}

/**
 * 读取 MCP server 配置列表（站点设置 KV）。
 * @returns server 列表
 */
export function mcpServers(): Promise<McpServersResult> {
  return client.get('/mcp/servers').then((r) => r.data)
}

/**
 * 添加 MCP server（operator）。
 * @param payload 名称/端点地址/附加请求头
 * @returns 添加后的完整 server 列表
 */
export function addMcpServer(payload: { name: string; url: string; headers?: Record<string, string> }): Promise<McpServersResult> {
  return client.post('/mcp/servers', payload).then((r) => r.data)
}

/**
 * 删除 MCP server（operator）。
 * @param sid server 标识
 * @returns 删除后的完整 server 列表
 */
export function deleteMcpServer(sid: string): Promise<McpServersResult> {
  return client.delete(`/mcp/servers/${sid}`).then((r) => r.data)
}

/**
 * 现连列举 MCP server 的工具（tools/list）。
 * @param sid server 标识
 * @returns 工具描述列表
 */
export function mcpTools(sid: string): Promise<{ tools: McpToolInfo[] }> {
  return client.post(`/mcp/servers/${sid}/tools`).then((r) => r.data)
}

/**
 * 现连调用 MCP server 工具（tools/call）。
 * @param sid server 标识
 * @param payload 工具名与参数
 * @returns tools/call 的 result（content/isError 等）
 */
export function mcpCall(sid: string, payload: { tool: string; arguments?: Record<string, unknown> }): Promise<Record<string, unknown>> {
  return client.post(`/mcp/servers/${sid}/call`, payload).then((r) => r.data)
}
