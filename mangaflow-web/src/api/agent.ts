/**
 * 智能体 API（文件级注释）。
 *
 * 对应后端 /api/agent：SSE 对话、会话列表、消息回放、意图承接（intake）。
 */
import client from './client'
import { ssePost } from './sse'
import type { AgentThread, IntakeResult, ThreadMessages } from '@/types'

/**
 * 智能体对话（POST SSE 流式）。
 * 事件约定：'delta' 增量回复、'stage' 阶段意图更新、'done' 回合结束、'error' 失败。
 * @param body 用户消息/关联项目/会话 ID
 * @param onEvent 每个事件帧回调
 * @param signal 中止信号（停止生成）
 */
export async function chatStream(
  body: { message: string; project_id?: number; thread_id?: string },
  onEvent: (event: string, data: unknown) => void,
  signal?: AbortSignal,
): Promise<void> {
  await ssePost('/api/agent/chat/stream', body, onEvent, signal)
}

/**
 * 查询当前用户的会话列表（按更新时间倒序）。
 * @returns 会话列表项
 */
export function threads(): Promise<AgentThread[]> {
  return client.get('/agent/threads').then((r) => r.data)
}

/**
 * 回放指定会话的历史消息。
 * @param threadId 会话 ID
 * @returns 会话阶段与消息列表
 */
export function threadMessages(threadId: string): Promise<ThreadMessages> {
  return client.get(`/agent/threads/${threadId}/messages`).then((r) => r.data)
}

/**
 * 意图承接（W-01 工作台输入分类）。
 * @param message 用户需求文本
 * @returns 意图/置信度/判定方式
 */
export function intake(message: string): Promise<IntakeResult> {
  return client.post('/agent/intake', { message }).then((r) => r.data)
}
