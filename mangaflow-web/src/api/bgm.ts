/**
 * BGM 曲库 API（文件级注释）。
 *
 * 对应后端 /api/bgm：曲库列表 / 上传自定义 BGM / 删除。
 * 上传返回的 url 可直接用于 compose 请求体的 bgm.url 字段。
 */
import client from './client'
import type { BgmItem, BgmUploadResult } from '@/types'

/**
 * 查询 BGM 曲库列表（全部可用曲目）。
 * @returns 曲目列表（name/url/size/duration）
 */
export function list(): Promise<BgmItem[]> {
  return client.get('/bgm').then((r) => r.data)
}

/**
 * 上传自定义 BGM（multipart/form-data，audio 文件）。
 * @param file 音频文件对象
 * @returns 上传后的曲目（url 可直接用于合成）
 */
export function upload(file: File): Promise<BgmUploadResult> {
  const form = new FormData()
  form.append('file', file)
  return client.post('/bgm', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  }).then((r) => r.data)
}

/**
 * 删除曲库曲目。
 * @param name 曲目文件名（列表项 name 字段）
 * @returns 消息响应
 */
export function remove(name: string): Promise<{ message: string }> {
  return client.delete(`/bgm/${encodeURIComponent(name)}`).then((r) => r.data)
}
