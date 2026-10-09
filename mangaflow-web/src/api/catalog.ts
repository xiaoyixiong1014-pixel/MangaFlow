/**
 * 模型目录与用户渠道 API（文件级注释）。
 *
 * 对应后端 /api/models：模型目录（按能力分组）、用户自有渠道 CRUD 与连通性测试。
 */
import client from './client'
import type {
  Catalog,
  Channel,
  ChannelCreatePayload,
  ChannelTestResult,
  MessageResponse,
} from '@/types'

/**
 * 查询模型目录（逻辑模型按能力分组 + 平台渠道信息）。
 * @returns 目录响应
 */
export function modelsCatalog(): Promise<Catalog> {
  return client.get('/models/catalog').then((r) => r.data)
}

/**
 * 查询当前用户自有渠道列表。
 * @returns 渠道列表（api_key 脱敏）
 */
export function myChannels(): Promise<Channel[]> {
  return client.get('/models/channels').then((r) => r.data)
}

/**
 * 新增用户自有渠道（api_key 服务端 AES 加密落库）。
 * @param payload 协议/接口地址/密钥/模型目录
 * @returns 新建渠道
 * @throws 400 base_url 非法；500 渠道连通性探测失败仍会创建（返回健康状态）
 */
export function addChannel(payload: ChannelCreatePayload): Promise<Channel> {
  return client.post('/models/channels', payload).then((r) => r.data)
}

/**
 * 删除用户自有渠道。
 * @param channelId 渠道 ID
 * @returns 消息响应
 * @throws 404 渠道不存在/非本人
 */
export function deleteChannel(channelId: number): Promise<MessageResponse> {
  return client.delete(`/models/channels/${channelId}`).then((r) => r.data)
}

/**
 * 渠道连通性测试。
 * @param channelId 渠道 ID
 * @returns 是否连通、失败原因、最新健康状态
 */
export function testChannel(channelId: number): Promise<ChannelTestResult> {
  return client.post(`/models/channels/${channelId}/test`).then((r) => r.data)
}
