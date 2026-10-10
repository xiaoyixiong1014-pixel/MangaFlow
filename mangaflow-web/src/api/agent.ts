/**
 * 智能体 API（文件级注释）。
 *
 * 对应后端 /api/agent：SSE 对话、会话列表、消息回放、意图承接（intake）、
 * 会话检查点历史与回溯（MA-06 Time Travel）、HITL 确认恢复（MA-08 chat/resume）。
 */
import client from './client'
import { ssePost } from './sse'
import type { AgentThread, IntakeResult, RewindResult, ThreadHistory, ThreadMessages } from '@/types'

/**
 * 智能体对话（POST SSE 流式）。
 * 事件约定：'message'/'token' 增量回复、'stage' 阶段意图更新、
 * 'pending_confirm' HITL 中断确认请求（随后本次流结束）、
 * 'done' 回合结束、'error' 失败。
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
 * HITL 确认恢复（MA-08，POST /api/agent/chat/resume）。
 * 以 Command(resume=...) 续跑被 interrupt 的图；返回同样为 SSE 流，
 * 事件约定与 chatStream 一致（可能连环 pending_confirm）。
 * @param body 会话 ID / 是否通过确认 / 附加确认数据
 * @param onEvent 每个事件帧回调
 * @param signal 中止信号（可空）
 */
export async function chatResume(
  body: { thread_id: string; approved: boolean; payload?: Record<string, unknown> },
  onEvent: (event: string, data: unknown) => void,
  signal?: AbortSignal,
): Promise<void> {
  await ssePost('/api/agent/chat/resume', body, onEvent, signal)
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
 * 查询会话检查点历史（MA-06 Time Travel，按时间倒序分页）。
 * @param threadId 会话 ID
 * @param params 页码与每页条数（可空）
 * @returns 检查点历史分页
 * @throws 404 会话不存在/非本人
 */
export function threadHistory(
  threadId: string,
  params?: { page?: number; page_size?: number },
): Promise<ThreadHistory> {
  return client.get(`/agent/threads/${threadId}/history`, { params }).then((r) => r.data)
}

/**
 * 回溯会话到指定检查点（写入 rewind_checkpoint 标记，一次性）。
 * 下一次 chat/stream 将从该检查点分叉续聊，对话完成后标记自动清空。
 * @param threadId 会话 ID
 * @param checkpointId 目标检查点 ID（须存在于该线程历史）
 * @returns 回溯结果
 * @throws 400 检查点不在历史中；404 会话不存在/非本人
 */
export function rewindThread(threadId: string, checkpointId: string): Promise<RewindResult> {
  return client.post(`/agent/threads/${threadId}/rewind`, { checkpoint_id: checkpointId }).then((r) => r.data)
}

/**
 * 意图承接（W-01 工作台输入分类）。
 * @param message 用户需求文本
 * @returns 意图/置信度/判定方式
 */
export function intake(message: string): Promise<IntakeResult> {
  return client.post('/agent/intake', { message }).then((r) => r.data)
}
