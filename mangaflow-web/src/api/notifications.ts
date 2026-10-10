/**
 * 通知中心 API（文件级注释）。
 *
 * 对应后端 /api/notifications：列表 / 未读数 / 标记已读 / SSE 实时流订阅。
 */
import client from './client'
import { sseGet } from './sse'
import type { NotificationList, NotificationReadResult, PageQuery, UnreadCount } from '@/types'

/**
 * 分页查询当前用户通知列表（创建时间倒序，附带未读数）。
 * @param params 分页参数
 * @returns 通知分页列表
 */
export function list(params?: PageQuery): Promise<NotificationList> {
  return client.get('/notifications', { params }).then((r) => r.data)
}

/**
 * 查询当前用户未读通知数量（顶栏角标）。
 * @returns 未读数
 */
export function unreadCount(): Promise<UnreadCount> {
  return client.get('/notifications/unread-count').then((r) => r.data)
}

/**
 * 标记通知已读（按 ID 批量或全部）。
 * @param payload ids 与 all 至少提供一个
 * @returns 实际标记条数
 * @throws 400 ids 与 all 均未提供
 */
export function markRead(payload: { ids?: number[]; all?: boolean }): Promise<NotificationReadResult> {
  return client.post('/notifications/read', payload).then((r) => r.data)
}

/**
 * 订阅通知实时推送流（GET SSE）。
 * 事件约定：'message' 携带单条通知 JSON（实时追加 + 角标 +1）、
 * 'heartbeat' 心跳、'done' 服务端超时主动结束（调用方可静默重连）。
 * @param onEvent 每个事件帧回调
 * @param signal 中止信号（页面离开时传入）
 * @throws Error 响应非 2xx 时抛出（调用方需静默容错）
 */
export async function stream(
  onEvent: (event: string, data: unknown) => void,
  signal?: AbortSignal,
): Promise<void> {
  await sseGet('/api/notifications/stream', onEvent, signal)
}
