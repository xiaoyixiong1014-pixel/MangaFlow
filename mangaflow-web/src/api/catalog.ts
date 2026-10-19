/**
 * 模型目录与用户渠道 API（文件级注释）。
 *
 * 对应后端 /api/models：模型目录（按能力分组）、用户自有渠道 CRUD 与连通性测试、
 * 渠道健康摘要（D-05）、模型效果对比（PL-11）。
 */
import client from './client'
import type {
  Catalog,
  Channel,
  ChannelCreatePayload,
  ChannelProbeModelsPayload,
  ChannelProbeModelsResult,
  ChannelTestResult,
  ChannelUpdatePayload,
  ComparePayload,
  CompareResult,
  HealthSummary,
  LogicalModel,
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
 * 切换当前用户对系统逻辑模型的启用/停用偏好。
 * 停用后生成路由跳过该模型的平台渠道链（仅自有渠道 + mock 兜底）。
 * @param modelId 逻辑模型 ID
 * @param enabled 是否启用
 * @returns 更新后的模型条目
 * @throws 404 逻辑模型不存在
 */
export function setModelEnabled(modelId: number, enabled: boolean): Promise<LogicalModel> {
  return client.put(`/models/catalog/${modelId}/enabled`, { enabled }).then((r) => r.data)
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
 * @param payload 备注名/协议/接口地址/密钥/模型目录
 * @returns 新建渠道
 * @throws 400 base_url 非法；500 渠道连通性探测失败仍会创建（返回健康状态）
 */
export function addChannel(payload: ChannelCreatePayload): Promise<Channel> {
  return client.post('/models/channels', payload).then((r) => r.data)
}

/**
 * 更新用户自有渠道（换 Key / 改地址 / 改模型目录 / 启停）。
 * api_key 缺省表示保持原 Key；任意变更后服务端将健康状态重置为 unknown。
 * @param channelId 渠道 ID
 * @param payload 更新字段（全可选）
 * @returns 更新后的渠道
 * @throws 400 base_url 非法；404 渠道不存在/非本人
 */
export function updateChannel(channelId: number, payload: ChannelUpdatePayload): Promise<Channel> {
  return client.put(`/models/channels/${channelId}`, payload).then((r) => r.data)
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

/**
 * 拉取渠道真实模型列表（GET {base}/models 后端代理，模型下拉候选）。
 * Key 仅本次请求使用；服务端拒绝本机/内网地址（防 SSRF）。
 * @param payload 协议/接口地址/密钥
 * @returns ok + 模型 ID 列表或失败原因
 */
export function probeModels(payload: ChannelProbeModelsPayload): Promise<ChannelProbeModelsResult> {
  return client.post('/models/probe-models', payload, { timeout: 30000 }).then((r) => r.data)
}

/**
 * 查询平台渠道健康摘要（D-05，工作台渠道健康标红条数据源）。
 * @returns 渠道健康列表与 down/degraded 计数
 */
export function healthSummary(): Promise<HealthSummary> {
  return client.get('/models/health-summary').then((r) => r.data)
}

/**
 * 模型效果对比（PL-11）：对 1~4 个渠道用同一提示词逐渠道生成。
 * 单渠道失败收敛进结果（ok=false + error），不影响其他渠道。
 * @param payload 能力（llm/image）/提示词/渠道 ID 列表
 * @returns 与请求顺序一致的逐渠道结果
 */
export function compare(payload: ComparePayload): Promise<CompareResult> {
  return client.post('/models/compare', payload, { timeout: 120000 }).then((r) => r.data)
}
