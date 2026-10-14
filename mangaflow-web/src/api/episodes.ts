/**
 * 剧集管理 API（文件级注释，S-06）。
 *
 * 对应后端 /api/projects/{project_id}/episodes：CRUD / 重排
 * （均校验项目归属，他人项目一律 404）。
 */
import client from './client'
import type { Episode, MessageResponse } from '@/types'

/**
 * 查询项目剧集列表（按 sequence 升序）。
 * @param projectId 项目 ID
 * @returns 剧集列表
 */
export function list(projectId: number): Promise<Episode[]> {
  return client.get(`/projects/${projectId}/episodes`).then((r) => r.data)
}

/**
 * 创建剧集（sequence 自动取项目内 max+1，初始状态 draft）。
 * @param projectId 项目 ID
 * @param payload 剧集标题
 * @returns 新建剧集
 */
export function create(projectId: number, payload: { title: string }): Promise<Episode> {
  return client.post(`/projects/${projectId}/episodes`, payload).then((r) => r.data)
}

/**
 * 更新剧集（仅传入字段生效；status 仅允许合法白名单值）。
 * @param projectId 项目 ID
 * @param episodeId 剧集 ID
 * @param payload 标题/状态
 * @returns 更新后的剧集
 */
export function update(
  projectId: number,
  episodeId: number,
  payload: { title?: string; status?: string },
): Promise<Episode> {
  return client.patch(`/projects/${projectId}/episodes/${episodeId}`, payload).then((r) => r.data)
}

/**
 * 删除剧集（硬删剧集行；scripts/storyboards/成片引用行 episode_id 置 NULL，数据保留）。
 * @param projectId 项目 ID
 * @param episodeId 剧集 ID
 * @returns 消息响应（含 unbound 解绑行数）
 */
export function remove(projectId: number, episodeId: number): Promise<MessageResponse> {
  return client.delete(`/projects/${projectId}/episodes/${episodeId}`).then((r) => r.data)
}

/**
 * 重排剧集（ordered_ids 须恰好覆盖项目全部剧集且无重复）。
 * @param projectId 项目 ID
 * @param orderedIds 目标顺序的剧集 ID 列表
 * @returns 重排数量
 */
export function reorder(
  projectId: number,
  orderedIds: number[],
): Promise<{ total: number }> {
  return client
    .put(`/projects/${projectId}/episodes/reorder`, { ordered_ids: orderedIds })
    .then((r) => r.data)
}
