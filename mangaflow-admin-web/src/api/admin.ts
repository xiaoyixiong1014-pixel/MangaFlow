/**
 * 文件级注释：管理端后端 API 封装。
 * 按模块分区：认证 / 看板 / 用户 / 审核 / 模型 / 订单 / 内容 / 设置。
 * 列表接口统一 {items,total,page,page_size}；错误 {detail}（由 client 拦截器统一提示）。
 */
import client from './client'
import type {
  AdminUserDetail,
  AdminUserItem,
  AlertItem,
  Announcement,
  BackupFile,
  ChannelTestResult,
  DashboardOverview,
  LogicalModel,
  LoginResult,
  ModelChannel,
  OrderItem,
  PageResult,
  PromptTemplate,
  ReviewItem,
  SettingsDict,
} from '@/types'

/* ============================== 认证 ============================== */

/**
 * 管理员登录。
 * @param data 用户名与密码
 * @returns 含 access_token / refresh_token / user 的登录结果
 * @throws 后端校验失败时抛出错误（detail 已由拦截器提示）
 */
export function login(data: { username: string; password: string }) {
  return client.post<LoginResult>('/auth/login', data).then((r) => r.data)
}

/* ============================== 看板 ============================== */

/**
 * 获取经营看板核心指标与趋势数据。
 * @param range 时间范围：7d / 30d / 90d
 * @returns 指标聚合与每日趋势
 */
export function getDashboardOverview(range: string) {
  return client
    .get<DashboardOverview>('/admin/dashboard/overview', { params: { range } })
    .then((r) => r.data)
}

/**
 * 获取资损与告警列表。
 * @returns 告警数组
 */
export function getDashboardAlerts() {
  return client.get<AlertItem[]>('/admin/dashboard/alerts').then((r) => r.data)
}

/* ============================== 用户管理 ============================== */

/**
 * 分页获取用户列表。
 * @param params 页码 / 每页条数 / 搜索关键字 / 状态筛选
 * @returns 用户分页列表
 */
export function getUsers(params: {
  page: number
  page_size: number
  search?: string
  status?: number | ''
}) {
  return client.get<PageResult<AdminUserItem>>('/admin/users', { params }).then((r) => r.data)
}

/**
 * 获取用户详情（钱包、近期日志、近期项目）。
 * @param id 用户 ID
 * @returns 用户详情
 */
export function getUserDetail(id: number) {
  return client.get<AdminUserDetail>(`/admin/users/${id}`).then((r) => r.data)
}

/**
 * 调整用户积分（高危操作，需先二次确认）。
 * @param id 用户 ID
 * @param data 调整量（正负数）与原因
 */
export function adjustUserPoints(id: number, data: { amount: number; reason: string }) {
  return client.post(`/admin/users/${id}/points`, data)
}

/**
 * 修改用户状态（启用 / 禁用）。
 * @param id 用户 ID
 * @param data 目标状态与原因
 */
export function setUserStatus(id: number, data: { status: 0 | 1; reason: string }) {
  return client.post(`/admin/users/${id}/status`, data)
}

/* ============================== 审核 ============================== */

/**
 * 分页获取审核记录。
 * @param params tab：pending / passed / rejected；gate 可选闸门筛选
 */
export function getReviews(params: { page: number; page_size: number; tab: string; gate?: string }) {
  return client
    .get<PageResult<ReviewItem>>('/admin/review', { params })
    .then((r) => r.data)
}

/**
 * 处置审核记录。
 * @param id 审核 ID
 * @param data action：approve / reject；驳回时 suggestion 必填
 */
export function handleReview(id: number, data: { action: 'approve' | 'reject'; suggestion?: string }) {
  return client.post(`/admin/review/${id}/handle`, data)
}

/* ============================== 模型配置 ============================== */

/**
 * 获取模型渠道列表。
 * @returns 渠道数组（API Key 已脱敏）
 */
export function getChannels() {
  return client.get<ModelChannel[]>('/admin/models/channels').then((r) => r.data)
}

/**
 * 新增模型渠道。
 * @param data 协议（固定 openai）、服务地址、API Key 明文（传输后加密存储）
 */
export function createChannel(data: { protocol: string; base_url: string; api_key: string }) {
  return client.post<ModelChannel>('/admin/models/channels', data).then((r) => r.data)
}

/**
 * 更新渠道（地址 / 启停）。
 * @param id 渠道 ID
 * @param data 可选字段：base_url / enabled
 */
export function updateChannel(id: number, data: { base_url?: string; enabled?: boolean }) {
  return client.put(`/admin/models/channels/${id}`, data)
}

/**
 * 测试渠道连通性。
 * @param id 渠道 ID
 * @returns ok / error / latency_ms
 */
export function testChannel(id: number) {
  return client
    .post<ChannelTestResult>(`/admin/models/channels/${id}/test`)
    .then((r) => r.data)
}

/**
 * 删除渠道。
 * @param id 渠道 ID
 */
export function deleteChannel(id: number) {
  return client.delete(`/admin/models/channels/${id}`)
}

/**
 * 获取逻辑模型列表。
 * @returns 逻辑模型数组
 */
export function getLogicalModels() {
  return client.get<LogicalModel[]>('/admin/models/logical').then((r) => r.data)
}

/**
 * 更新逻辑模型配置。
 * @param id 逻辑模型 ID
 * @param data 可选字段：优先级 / 默认标记 / 单价 JSON / 场景说明 / 文档地址
 */
export function updateLogicalModel(
  id: number,
  data: {
    channel_priority?: number[]
    is_default?: boolean
    price_per_unit?: string
    scene_desc?: string
    doc_url?: string
  },
) {
  return client.put(`/admin/models/logical/${id}`, data)
}

/**
 * 新增逻辑模型。
 * @param data 完整逻辑模型字段
 */
export function createLogicalModel(data: {
  capability: string
  name: string
  channel_priority: number[]
  is_default: boolean
  price_per_unit: string
  scene_desc: string
  doc_url: string
}) {
  return client.post<LogicalModel>('/admin/models/logical', data).then((r) => r.data)
}

/* ============================== 订单与计费 ============================== */

/**
 * 分页获取订单列表。
 * @param params 页码 / 状态筛选 / 搜索（订单号或用户）
 */
export function getOrders(params: { page: number; page_size: number; status?: string; search?: string }) {
  return client.get<PageResult<OrderItem>>('/admin/orders', { params }).then((r) => r.data)
}

/**
 * 人工完成订单（高危操作）。
 * @param id 订单 ID
 * @param data 原因（写入财务流水）
 */
export function completeOrder(id: number, data: { reason?: string }) {
  return client.post(`/admin/orders/${id}/complete`, data)
}

/**
 * 关闭订单。
 * @param id 订单 ID
 * @param data 原因
 */
export function closeOrder(id: number, data: { reason?: string }) {
  return client.post(`/admin/orders/${id}/close`, data)
}

/**
 * 订单退款（高危操作，原因必填）。
 * @param id 订单 ID
 * @param data 原因
 */
export function refundOrder(id: number, data: { reason: string }) {
  return client.post(`/admin/orders/${id}/refund`, data)
}

/* ============================== 内容运营 ============================== */

/**
 * 分页获取公告列表。
 * @param params 页码 / 状态筛选
 */
export function getAnnouncements(params: { page: number; page_size: number; status?: string }) {
  return client
    .get<PageResult<Announcement>>('/admin/content/announcements', { params })
    .then((r) => r.data)
}

/**
 * 新建公告。
 * @param data 标题 / 正文 / 置顶 / 状态
 */
export function createAnnouncement(data: {
  title: string
  content: string
  is_pinned: boolean
  status: string
}) {
  return client.post<Announcement>('/admin/content/announcements', data).then((r) => r.data)
}

/**
 * 更新公告（含置顶开关 / 发布下线）。
 * @param id 公告 ID
 * @param data 部分字段
 */
export function updateAnnouncement(
  id: number,
  data: Partial<{ title: string; content: string; is_pinned: boolean; status: string }>,
) {
  return client.put(`/admin/content/announcements/${id}`, data)
}

/**
 * 删除公告。
 * @param id 公告 ID
 */
export function deleteAnnouncement(id: number) {
  return client.delete(`/admin/content/announcements/${id}`)
}

/**
 * 分页获取公共提示词列表。
 * @param params 页码
 */
export function getPrompts(params: { page: number; page_size: number }) {
  return client
    .get<PageResult<PromptTemplate>>('/admin/content/prompts', { params })
    .then((r) => r.data)
}

/**
 * 新建公共提示词。
 * @param data 标题 / 能力分类 / 内容 / 状态
 */
export function createPrompt(data: {
  title: string
  capability: string
  content: string
  status: string
}) {
  return client.post<PromptTemplate>('/admin/content/prompts', data).then((r) => r.data)
}

/**
 * 修改提示词上下架状态。
 * @param id 提示词 ID
 * @param data status：published / offline
 */
export function updatePromptStatus(id: number, data: { status: 'published' | 'offline' }) {
  return client.put(`/admin/content/prompts/${id}/status`, data)
}

/**
 * 删除公共提示词。
 * @param id 提示词 ID
 */
export function deletePrompt(id: number) {
  return client.delete(`/admin/content/prompts/${id}`)
}

/* ============================== 系统设置 ============================== */

/**
 * 获取系统设置（字典）。
 * @returns 设置键值对
 */
export function getSettings() {
  return client.get<SettingsDict>('/admin/settings').then((r) => r.data)
}

/**
 * 批量保存系统设置。
 * @param settings 键值对集合
 */
export function saveSettings(settings: SettingsDict) {
  return client.put('/admin/settings', { settings })
}

/**
 * 获取备份文件列表。
 * @returns 备份文件数组
 */
export function getSettingsBackups() {
  return client.get<BackupFile[]>('/admin/settings/backups').then((r) => r.data)
}

/**
 * 立即创建备份（异步任务）。
 */
export function createSettingsBackup() {
  return client.post('/admin/settings/backups')
}
