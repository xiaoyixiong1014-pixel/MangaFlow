/**
 * 文件级注释：管理端全局 TypeScript 类型定义。
 * 与后端 /api/admin/* 契约一一对应；列表接口统一返回 {items, total, page, page_size}。
 */

/** 通用分页响应包装 */
export interface PageResult<T> {
  /** 当前页数据 */
  items: T[]
  /** 总条数 */
  total: number
  /** 当前页码 */
  page: number
  /** 每页条数 */
  page_size: number
}

/** 登录接口返回的管理员用户信息 */
export interface AdminUser {
  /** 用户 ID */
  id: number
  /** 用户名 */
  username: string
  /** 角色：operator 才允许进入管理端 */
  role: string
  /** 其他扩展字段 */
  [key: string]: unknown
}

/** 登录响应 */
export interface LoginResult {
  /** 访问令牌 */
  access_token: string
  /** 刷新令牌 */
  refresh_token: string
  /** 当前用户信息 */
  user: AdminUser
}

/** 用户钱包信息 */
export interface UserWallet {
  /** 可用积分余额 */
  points_balance: number
  /** 冻结积分 */
  frozen_points: number
}

/** 用户管理列表项 */
export interface AdminUserItem {
  /** 用户 ID */
  id: number
  /** 用户名 */
  username: string
  /** 邮箱 */
  email: string
  /** 状态：1 启用 / 0 禁用 */
  status: number
  /** 会员等级 */
  vip_level: string
  /** 钱包信息 */
  wallet: UserWallet
  /** 项目数量 */
  project_count: number
  /** 创建时间 */
  created_at: string
}

/** 用户审计日志 */
export interface UserLogItem {
  /** 日志 ID */
  id: number
  /** 操作类型 */
  action: string
  /** 操作详情 */
  detail: string
  /** 时间 */
  created_at: string
}

/** 用户近期项目 */
export interface UserProjectItem {
  /** 项目 ID */
  id: number
  /** 项目标题 */
  title: string
  /** 项目状态 */
  status: string
  /** 更新时间 */
  updated_at: string
}

/** 用户详情 */
export interface AdminUserDetail extends AdminUserItem {
  /** 钱包（冗余字段，与 wallet 一致） */
  wallet: UserWallet
  /** 近期审计日志 */
  recent_logs: UserLogItem[]
  /** 近期项目 */
  recent_projects: UserProjectItem[]
}

/** 审核记录列表项 */
export interface ReviewItem {
  /** 审核 ID */
  id: number
  /** 所属项目 ID */
  project_id: number
  /** 审核对象类型 */
  target_type: string
  /** 审核闸门：generation / composition / export */
  gate: string
  /** 机审结果 */
  result: string
  /** 命中的违规类型 */
  violation_type: string
  /** 人工处置状态：pending / passed / rejected */
  manual_status: string
  /** 处置建议 */
  suggestion: string | null
  /** 创建时间 */
  created_at: string
  /** 项目标题 */
  project_title: string
  /** 分镜序号 */
  storyboard_seq: number | null
}

/** 资损告警项 */
export interface AlertItem {
  /** 告警级别：high / medium / low */
  level: string
  /** 告警类型 */
  type: string
  /** 告警内容 */
  message: string
  /** 告警时间 */
  created_at: string
}

/** 看板核心指标 */
export interface DashboardOverview {
  /** 用户总数 */
  users_total: number
  /** 新增用户 */
  users_new: number
  /** 总收入（元） */
  revenue_total: number
  /** 已支付订单数 */
  orders_paid: number
  /** 积分负债 */
  points_liability: number
  /** 生成任务总数 */
  tasks_total: number
  /** 失败任务数 */
  tasks_failed: number
  /** 每日收入趋势 */
  revenue_by_day: Array<{ date: string; amount: number }>
  /** 每日生成量（图片/视频/TTS 三系列） */
  generation_by_day: Array<{ date: string; image: number; video: number; tts: number }>
}

/** 模型渠道 */
export interface ModelChannel {
  /** 渠道 ID */
  id: number
  /** 接入协议（当前固定 openai） */
  protocol: string
  /** 服务地址 */
  base_url: string
  /** 脱敏后的 API Key */
  api_key_masked: string
  /** 归属管理员用户 ID */
  owner_user_id: number
  /** 健康状态：healthy / degraded / down */
  health_status: string
  /** 最近探测时间 */
  last_check_at: string | null
  /** 是否启用 */
  enabled: boolean
  /** 渠道可用模型列表 */
  models: string[]
}

/** 逻辑模型 */
export interface LogicalModel {
  /** 逻辑模型 ID */
  id: number
  /** 能力域：text / image / video / tts */
  capability: string
  /** 逻辑模型名 */
  name: string
  /** 渠道优先级（渠道 ID 有序列表） */
  channel_priority: number[]
  /** 是否默认 */
  is_default: boolean
  /** 单价（JSON 字符串，按能力维度定义） */
  price_per_unit: string
  /** 场景说明 */
  scene_desc: string
  /** 文档地址 */
  doc_url: string
}

/** 连通性测试结果 */
export interface ChannelTestResult {
  /** 是否连通成功 */
  ok: boolean
  /** 失败原因 */
  error: string | null
  /** 延迟（毫秒） */
  latency_ms: number
}

/** 订单列表项 */
export interface OrderItem {
  /** 订单 ID */
  id: number
  /** 订单号 */
  order_no: string
  /** 下单用户名 */
  username: string
  /** 商品类型：points / subscription */
  product_type: string
  /** 金额（元） */
  amount: number
  /** 积分 */
  points: number
  /** 支付渠道 */
  pay_channel: string
  /** 订单状态：pending / paid / closed / refunded */
  status: string
  /** 支付时间 */
  paid_at: string | null
  /** 创建时间 */
  created_at: string
}

/** 公告 */
export interface Announcement {
  /** 公告 ID */
  id: number
  /** 标题 */
  title: string
  /** 正文 */
  content: string
  /** 是否置顶 */
  is_pinned: boolean
  /** 状态：published / draft / offline */
  status: string
  /** 发布时间 */
  published_at: string | null
  /** 创建时间 */
  created_at: string
}

/** 公共提示词 */
export interface PromptTemplate {
  /** 提示词 ID */
  id: number
  /** 标题 */
  title: string
  /** 能力分类 */
  capability: string
  /** 正文内容 */
  content: string
  /** 状态：published / offline */
  status: string
  /** 使用次数 */
  usage_count: number
  /** 创建时间 */
  created_at: string
}

/** 备份文件 */
export interface BackupFile {
  /** 文件名 */
  file: string
  /** 文件大小（字节） */
  size: number
  /** 创建时间 */
  created_at: string
}

/** 系统设置（字典结构） */
export type SettingsDict = Record<string, string | number | boolean>
