/**
 * 用户中心 API（文件级注释）。
 *
 * 对应后端 /api/users：创作统计（D-04，GET /users/me/creation-stats）。
 */
import client from './client'
import type { CreationStats } from '@/types'

/**
 * 查询当前用户今日与本月创作统计（项目数/图片数/视频秒数/消耗积分）。
 * @returns 今日与本月两组统计
 */
export function creationStats(): Promise<CreationStats> {
  return client.get('/users/me/creation-stats').then((r) => r.data)
}
