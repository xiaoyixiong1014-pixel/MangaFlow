/**
 * 公告 API（文件级注释）。
 *
 * 对应后端 GET /api/announcements（公开接口，置顶优先、发布时间倒序）。
 */
import client from './client'
import type { Announcement } from '@/types'

/**
 * 查询已发布公告列表（登录前可见，公告量级小不分页）。
 * @returns 已发布公告列表
 */
export function list(): Promise<Announcement[]> {
  return client.get('/announcements').then((r) => r.data)
}
