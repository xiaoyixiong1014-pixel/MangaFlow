/**
 * 素材库 API（文件级注释）。
 *
 * 对应后端 /api/media：查询 / 上传（multipart）/ 详情 / meta 更新 / 删除。
 */
import client from './client'
import type { MediaAsset, MessageResponse, PageQuery, PageResult } from '@/types'

/** 素材列表查询参数 */
export interface MediaListQuery extends PageQuery {
  /** 媒体类型：image/video/audio/text */
  media_type?: string
  /** 来源过滤 */
  source?: string
  /** 项目过滤 */
  project_id?: number
}

/**
 * 分页查询当前用户素材。
 * @param params 分页/过滤参数
 * @returns 素材分页列表
 */
export function list(params?: MediaListQuery): Promise<PageResult<MediaAsset>> {
  return client.get('/media', { params }).then((r) => r.data)
}

/**
 * 上传素材文件（multipart/form-data）。
 * @param file 文件对象
 * @param options 媒体类型与关联项目（可空）
 * @returns 上传后的素材记录
 */
export function upload(
  file: File,
  options?: { media_type?: string; project_id?: number },
): Promise<MediaAsset> {
  const form = new FormData()
  form.append('file', file)
  if (options?.media_type) form.append('media_type', options.media_type)
  if (options?.project_id != null) form.append('project_id', String(options.project_id))
  return client.post('/media/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  }).then((r) => r.data)
}

/**
 * 查询素材详情。
 * @param id 素材 ID
 * @returns 素材记录
 * @throws 404 不存在/非本人素材
 */
export function detail(id: number): Promise<MediaAsset> {
  return client.get(`/media/${id}`).then((r) => r.data)
}

/**
 * 更新素材元数据（整体替换 meta 字段）。
 * @param id 素材 ID
 * @param meta 新元数据对象
 * @returns 更新后的素材记录
 */
export function updateMeta(id: number, meta: Record<string, unknown>): Promise<MediaAsset> {
  return client.put(`/media/${id}/meta`, { meta }).then((r) => r.data)
}

/**
 * 删除素材。
 * @param id 素材 ID
 * @returns 消息响应
 */
export function remove(id: number): Promise<MessageResponse> {
  return client.delete(`/media/${id}`).then((r) => r.data)
}
