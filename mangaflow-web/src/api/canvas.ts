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
  GraphSyncPayload,
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
