/**
 * 全局 TS 类型定义（文件级注释）。
 *
 * 字段命名与后端 FastAPI 契约保持一致（蛇形命名），避免引入映射层。
 * 时间字段后端返回 ISO 字符串，统一声明为 string | null。
 */

// ============================================================
// 通用
// ============================================================

/** 通用分页结果（对应后端 PageResult[T]） */
export interface PageResult<T> {
  /** 总记录数 */
  total: number
  /** 当前页码（从 1 开始） */
  page: number
  /** 每页条数 */
  page_size: number
  /** 当前页数据 */
  items: T[]
}

/** 通用消息响应（{message: string}） */
export interface MessageResponse {
  /** 提示信息 */
  message: string
}

/** 通用列表查询分页参数 */
export interface PageQuery {
  /** 页码，从 1 开始 */
  page?: number
  /** 每页条数 */
  page_size?: number
}

// ============================================================
// 认证 / 用户
// ============================================================

/** 用户信息（对应 UserOut） */
export interface User {
  /** 用户 ID */
  id: number
  /** 用户名 */
  username: string
  /** 邮箱（可空） */
  email?: string | null
  /** 头像 URL（可空） */
  avatar?: string | null
  /** 角色：user / operator */
  role: string
  /** 会员等级 */
  vip_level: number
  /** 状态：0 禁用 / 1 正常 */
  status: number
  /** 最近登录时间 */
  last_login_at?: string | null
  /** 注册时间 */
  created_at?: string | null
}

/** 令牌对（对应 TokenResponse / RefreshResponse 公共部分） */
export interface AuthTokens {
  /** 访问令牌 */
  access_token: string
  /** 刷新令牌 */
  refresh_token: string
  /** 令牌类型（bearer） */
  token_type: string
}

/** 登录 / 注册响应：令牌对 + 用户信息 */
export interface LoginResponse extends AuthTokens {
  /** 用户信息 */
  user: User
}

/** 刷新令牌响应（refresh 不换新，原样返回） */
export interface RefreshResponse extends AuthTokens {}

/** GET /auth/me 响应：用户信息 + 钱包摘要 */
export interface MeResponse {
  /** 用户信息 */
  user: User
  /** 钱包摘要 */
  wallet: WalletSummary
}

/** 钱包摘要（/auth/me 内嵌） */
export interface WalletSummary {
  /** 可用积分 */
  points_balance: number
  /** 冻结积分 */
  frozen_points: number
  /** 乐观锁版本号 */
  version: number
}

/** 注册请求体 */
export interface RegisterPayload {
  /** 用户名 */
  username: string
  /** 邮箱（可空） */
  email?: string
  /** 密码（至少 6 位） */
  password: string
}

/** 更新个人资料请求体 */
export interface UpdateUserPayload {
  /** 新头像 URL（可空） */
  avatar?: string
  /** 新邮箱（可空） */
  email?: string
}

// ============================================================
// 计费
// ============================================================

/** 钱包概要（GET /billing/wallet） */
export interface Wallet {
  /** 可用积分 */
  points_balance: number
  /** 冻结积分 */
  frozen_points: number
  /** 是否低余额（可用 < 1000） */
  low_balance: boolean
  /** 乐观锁版本号 */
  version: number
}

/** 积分套餐（GET /billing/packages） */
export interface Pkg {
  /** 套餐标识（p_small/p_mid/p_large/p_sub） */
  id: string
  /** 套餐名称 */
  name: string
  /** 套餐积分 */
  points: number
  /** 售价（人民币元） */
  price: number
  /** 是否订阅型套餐 */
  subscription: boolean
}

/** 订单（GET /billing/orders） */
export interface Order {
  /** 订单 ID */
  id: number
  /** 订单号 */
  order_no: string
  /** 商品类型：points / subscription */
  product_type: string
  /** 订单金额（元） */
  amount: number
  /** 兑换积分 */
  points: number
  /** 支付渠道 */
  pay_channel?: string | null
  /** 订单状态：pending/paid/closed/refunded */
  status: string
  /** 支付时间 */
  paid_at?: string | null
  /** 下单时间 */
  created_at?: string | null
}

/** 模拟支付响应 */
export interface PayResponse {
  /** 支付后的订单 */
  order: Order
  /** 到账后的可用积分余额 */
  points_balance: number
}

/** 积分流水（GET /billing/transactions） */
export interface WalletTransactionItem {
  /** 流水 ID */
  id: number
  /** 类型：freeze/consume/refund/recharge/grant */
  type: string
  /** 变动积分（带符号） */
  amount: number
  /** 成本承担方：user / platform */
  bear: string
  /** 关联任务 ID */
  ref_task_id?: number | null
  /** 关联订单 ID */
  ref_order_id?: number | null
  /** 幂等键 */
  idempotency_key: string
  /** 备注 */
  remark?: string | null
  /** 流水时间 */
  created_at?: string | null
}

/** 月度消耗统计项（GET /billing/wallet/summary） */
export interface MonthConsumed {
  /** 月份（YYYY-MM） */
  month: string
  /** 该月消耗积分（正值） */
  consumed: number
}

// ============================================================
// 公告
// ============================================================

/** 系统公告（GET /announcements，公开接口） */
export interface Announcement {
  /** 公告 ID */
  id: number
  /** 标题 */
  title: string
  /** 内容 */
  content: string
  /** 是否置顶 */
  is_pinned: boolean
  /** 状态：draft/published/offline */
  status: string
  /** 发布时间 */
  published_at?: string | null
  /** 创建时间 */
  created_at?: string | null
}

// ============================================================
// 模型目录 / 渠道
// ============================================================

/** 逻辑模型条目（模型目录内嵌） */
export interface LogicalModel {
  /** 逻辑模型 ID */
  id: number
  /** 逻辑模型名 */
  name: string
  /** 是否官方默认 */
  is_default: boolean
  /** 计价规则（JSONB） */
  price_per_unit?: unknown
  /** 适用场景描述 */
  scene_desc?: string | null
  /** 官方文档链接 */
  doc_url?: string | null
  /** 健康状态：unknown/healthy/degraded/down */
  health: string
}

/** 按能力分组的逻辑模型组 */
export interface CatalogGroup {
  /** 能力类型：llm/image/video/tts */
  capability: string
  /** 该能力下的逻辑模型列表 */
  models: LogicalModel[]
}

/** 平台渠道信息（不含 Key） */
export interface PlatformChannel {
  /** 渠道 ID */
  id: number
  /** 协议类型 */
  protocol: string
  /** 接口地址 */
  base_url: string
  /** 健康状态 */
  health_status: string
  /** 是否启用 */
  enabled: boolean
  /** 最近探测时间 */
  last_check_at?: string | null
}

/** 模型目录响应（GET /models/catalog） */
export interface Catalog {
  /** 按能力分组的逻辑模型列表 */
  capabilities: CatalogGroup[]
  /** 平台渠道信息列表 */
  platform_channels: PlatformChannel[]
}

/** 用户自有渠道（GET/POST /models/channels） */
export interface Channel {
  /** 渠道 ID */
  id: number
  /** 协议类型（当前 openai） */
  protocol: string
  /** 接口地址 */
  base_url: string
  /** 脱敏后的 API Key（前 4 位 + **** + 后 4 位） */
  api_key?: string | null
  /** 模型目录（JSONB，如 {chat: [...]}） */
  models?: Record<string, unknown> | null
  /** 健康状态 */
  health_status: string
  /** 最近探测时间 */
  last_check_at?: string | null
  /** 是否启用 */
  enabled: boolean
}

/** 渠道创建请求体 */
export interface ChannelCreatePayload {
  /** 协议类型，默认 openai */
  protocol?: string
  /** 接口地址（http/https 开头） */
  base_url: string
  /** API Key 明文（服务端加密落库） */
  api_key: string
  /** 模型目录（可空） */
  models?: Record<string, unknown>
}

/** 渠道连通性测试响应 */
export interface ChannelTestResult {
  /** 是否连通成功 */
  ok: boolean
  /** 失败原因（成功时为空） */
  error?: string | null
  /** 测试后的健康状态 */
  health_status: string
}

// ============================================================
// 项目
// ============================================================

/** 项目（GET /projects 列表项 / 创建 / 更新返回） */
export interface Project {
  /** 项目 ID */
  id: number
  /** 项目标题 */
  title: string
  /** 体裁 */
  genre?: string | null
  /** 视觉风格 */
  style?: string | null
  /** 忠实度档位：free/outline/faithful */
  fidelity?: string | null
  /** 状态：draft/scripting/asset/storyboard/producing/review/published */
  status: string
  /** 输出比例 */
  aspect_ratio: string
  /** 目标时长（秒） */
  target_duration?: number | null
  /** 项目级模型配置（JSONB） */
  config?: Record<string, unknown> | null
  /** 封面 URL */
  cover_url?: string | null
  /** 创建时间 */
  created_at?: string | null
  /** 更新时间 */
  updated_at?: string | null
}

/** 项目详情（GET /projects/{id}，含子资源计数） */
export interface ProjectDetail extends Project {
  /** 剧集数量 */
  episode_count: number
  /** 剧本数量 */
  script_count: number
}

/** 项目创建请求体 */
export interface ProjectCreatePayload {
  /** 项目标题（1~100 字符） */
  title: string
  /** 体裁 */
  genre?: string
  /** 视觉风格 */
  style?: string
  /** 忠实度档位 */
  fidelity?: string
  /** 输出比例，默认 9:16 */
  aspect_ratio?: string
  /** 目标时长（秒） */
  target_duration?: number
  /** 项目级模型配置 */
  config?: Record<string, unknown>
}

/** 项目更新请求体（仅传入字段生效） */
export interface ProjectUpdatePayload extends Partial<ProjectCreatePayload> {
  /** 项目状态（合法状态机值） */
  status?: string
}

/** 版本快照（GET /projects/{id}/snapshots） */
export interface VersionSnapshot {
  /** 快照 ID */
  id: number
  /** 项目 ID */
  project_id: number
  /** 阶段标识 */
  stage: string
  /** LangGraph 检查点 ID */
  checkpoint_id?: string | null
  /** 快照摘要（JSONB） */
  summary?: unknown
  /** 影响面记录（JSONB） */
  impact_scope?: unknown
  /** 创建时间 */
  created_at?: string | null
}

// ============================================================
// 剧本（Studio 阶段一）
// ============================================================

/**
 * 结构化剧本内容（宽松类型）。
 * 后端 content 为 JSONB 自由结构：每个一级字段包装为 {value, source: 'ai'|'user'}。
 */
export interface ScriptContent {
  /** 任意一级字段（标题/故事梗概/分集大纲/台词本等） */
  [key: string]: unknown
}

/** 剧本输出（GET /projects/{id}/script） */
export interface ScriptOut {
  /** 剧本 ID */
  id: number
  /** 项目 ID */
  project_id: number
  /** 结构化剧本内容 */
  content?: ScriptContent | null
  /** 原始输入文本 */
  raw_text?: string | null
  /** 别名归一与冲突记录（JSONB） */
  fences?: unknown
  /** 结构化体检结果（JSONB） */
  health_report?: unknown
  /** 版本号 */
  version: number
  /** 更新时间 */
  updated_at?: string | null
}

/** 剧本原始输入请求体 */
export interface ScriptInputPayload {
  /** 输入模式：idea / novel / outline */
  mode: 'idea' | 'novel' | 'outline'
  /** 原始输入文本 */
  raw_text: string
}

/** 剧本手动保存请求体 */
export interface ScriptSavePayload {
  /** 用户编辑后的剧本内容 */
  content: ScriptContent
  /** 可选体检结果回写 */
  health_report?: unknown
}

/** 抽取实体通用条目（角色/场景/道具共用） */
export interface ExtractedEntity {
  /** 实体名称 */
  name: string
  /** 别名列表 */
  aliases: string[]
  /** 描述 */
  description: string
  /** 置信度（0~1） */
  confidence: number
  /** 低置信度标记（< 0.7 前端标黄） */
  low_confidence: boolean
}

/** 别名冲突记录（RD-06） */
export interface ExtractConflict {
  /** 冲突类型（当前 alias） */
  type: string
  /** 冲突候选名列表 */
  candidates: string[]
  /** 建议保留的规范名 */
  suggestion: string
}

/** 实体抽取结果（POST /script/extract） */
export interface ExtractResult {
  /** 角色抽取结果 */
  characters: ExtractedEntity[]
  /** 场景抽取结果 */
  scenes: ExtractedEntity[]
  /** 道具抽取结果 */
  props: ExtractedEntity[]
  /** 别名冲突列表 */
  conflicts: ExtractConflict[]
}

/** 实体确认入池条目（角色/场景/道具共用结构，按需取字段） */
export interface ConfirmEntityItem {
  /** 实体名称 */
  name: string
  /** 别名列表（仅角色） */
  aliases?: string[]
  /** 描述 */
  description?: string
  /** 视觉提示词（缺省用模板生成） */
  visual_prompt?: string
}

/** 实体抽取确认请求体 */
export interface ExtractConfirmPayload {
  /** 确认入池的角色列表 */
  characters?: ConfirmEntityItem[]
  /** 确认入池的场景列表 */
  scenes?: ConfirmEntityItem[]
  /** 确认入池的道具列表 */
  props?: ConfirmEntityItem[]
}

/** 实体确认结果（返回各类资产现有总数） */
export interface ExtractConfirmResult {
  /** 项目内角色资产总数 */
  characters: number
  /** 项目内场景资产总数 */
  scenes: number
  /** 项目内道具资产总数 */
  props: number
}

// ============================================================
// 资产（Studio 阶段二）
// ============================================================

/** 角色资产（对应 CharacterOut） */
export interface CharacterAsset {
  /** 角色 ID */
  id: number
  /** 项目 ID */
  project_id: number
  /** 角色名 */
  name: string
  /** 别名集合（JSONB） */
  aliases?: unknown
  /** 角色描述 */
  description?: string | null
  /** 标准化视觉提示词 */
  visual_prompt: string
  /** 主定妆照 URL */
  asset_url?: string | null
  /** 面部层锁定 */
  face_locked: boolean
  /** 生成状态：pending/generating/done/failed */
  generation_status: string
  /** 创建时间 */
  created_at?: string | null
  /** 更新时间 */
  updated_at?: string | null
}

/** 场景资产（对应 SceneOut） */
export interface SceneAsset {
  /** 场景 ID */
  id: number
  /** 项目 ID */
  project_id: number
  /** 场景名 */
  name: string
  /** 场景描述 */
  description?: string | null
  /** 视觉提示词 */
  visual_prompt: string
  /** 概念图 URL */
  asset_url?: string | null
  /** 锁定状态 */
  is_locked: boolean
  /** 创建时间 */
  created_at?: string | null
  /** 更新时间 */
  updated_at?: string | null
}

/** 道具资产（对应 PropOut） */
export interface PropAsset {
  /** 道具 ID */
  id: number
  /** 项目 ID */
  project_id: number
  /** 道具名 */
  name: string
  /** 分类 */
  category?: string | null
  /** 描述 */
  description?: string | null
  /** 视觉提示词 */
  visual_prompt: string
  /** 标准图 URL */
  asset_url?: string | null
  /** 锁定状态 */
  is_locked: boolean
  /** 创建时间 */
  created_at?: string | null
  /** 更新时间 */
  updated_at?: string | null
}

/** 风格资产（对应 StyleOut） */
export interface StyleAsset {
  /** 风格 ID */
  id: number
  /** 项目 ID */
  project_id: number
  /** 风格名 */
  name: string
  /** 内置预设编码 */
  preset_code?: string | null
  /** 参考图 URL */
  reference_url?: string | null
  /** 风格提示词 */
  visual_prompt: string
  /** 是否项目默认风格 */
  is_default: boolean
  /** 创建时间 */
  created_at?: string | null
  /** 更新时间 */
  updated_at?: string | null
}

/** 四类资产聚合输出（GET /projects/{id}/assets） */
export interface AssetsGroup {
  /** 角色列表 */
  characters: CharacterAsset[]
  /** 场景列表 */
  scenes: SceneAsset[]
  /** 道具列表 */
  props: PropAsset[]
  /** 风格列表 */
  styles: StyleAsset[]
}

/** 资产变体（对应 VariantOut） */
export interface AssetVariant {
  /** 变体 ID */
  id: number
  /** 多态归属类型：character/scene/prop */
  owner_type: string
  /** 归属资产 ID */
  owner_id: number
  /** 变体类型：outfit/age/other/lighting */
  variant_type: string
  /** 变体名 */
  name: string
  /** 继承/独立声明（RD-14） */
  inherit_mask?: unknown
  /** 变体图 URL */
  asset_url?: string | null
  /** 变体提示词 */
  visual_prompt?: string | null
  /** 是否启用 */
  is_active: boolean
  /** 创建时间 */
  created_at?: string | null
  /** 更新时间 */
  updated_at?: string | null
}

/** 资产变体创建请求体 */
export interface VariantCreatePayload {
  /** 变体类型 */
  variant_type: string
  /** 变体名 */
  name: string
  /** 继承/独立声明（必填 dict） */
  inherit_mask: Record<string, unknown>
  /** 变体提示词 */
  visual_prompt?: string
  /** 是否启用 */
  is_active?: boolean
}

/** 一致性校验结果（POST /assets/consistency-check） */
export interface ConsistencyCheckResult {
  /** 伪相似度（0~1） */
  similarity: number
  /** 是否通过（>= 0.85） */
  passed: boolean
  /** 校验报告文本 */
  report: string
}

/** 跨项目资产总览行（GET /api/characters，角色 + 场景联合） */
export interface CrossProjectAsset {
  /** 资产 ID */
  id: number
  /** 资产类型：character / scene */
  asset_type: string
  /** 所属项目 ID */
  project_id: number
  /** 所属项目标题 */
  project_title: string
  /** 资产名 */
  name: string
  /** 描述 */
  description?: string | null
  /** 视觉提示词 */
  visual_prompt: string
  /** 资产图 URL */
  asset_url?: string | null
  /** 创建时间 */
  created_at?: string | null
}

// ============================================================
// 分镜（Studio 阶段三）
// ============================================================

/** 分镜-角色关联输出 */
export interface SbCharacterLink {
  /** 角色资产 ID */
  character_id: number
  /** 角色名（联查回显） */
  name: string
  /** 该镜头使用的造型变体 ID */
  variant_id?: number | null
  /** 实体链接置信度（0~1） */
  link_confidence?: number | null
}

/** 分镜（对应 StoryboardOut） */
export interface Storyboard {
  /** 分镜 ID */
  id: number
  /** 项目 ID */
  project_id: number
  /** 镜头序号（项目内 1..n） */
  sequence: number
  /** 景别 */
  shot_type?: string | null
  /** 运镜 */
  camera_movement?: string | null
  /** 预计时长（秒） */
  duration: number
  /** 画面描述 */
  visual_description?: string | null
  /** 对白/旁白（多行为 "角色名：台词"） */
  dialogue?: string | null
  /** 强绑场景资产 ID */
  scene_id?: number | null
  /** 场景名（联查回显） */
  scene_name?: string | null
  /** 提示词系统锁定层（RD-04） */
  prompt_locked?: string | null
  /** 提示词用户可编辑层 */
  prompt_editable?: string | null
  /** 关键帧 URL */
  keyframe_url?: string | null
  /** 视频片段 URL */
  video_url?: string | null
  /** 状态：pending/generating/done/failed */
  status: string
  /** 重生成次数 */
  regen_count: number
  /** 关联角色列表 */
  characters: SbCharacterLink[]
}

/** 分镜更新请求体 */
export interface StoryboardUpdatePayload {
  /** 景别 */
  shot_type?: string
  /** 运镜 */
  camera_movement?: string
  /** 预计时长（秒） */
  duration?: number
  /** 画面描述 */
  visual_description?: string
  /** 对白/旁白 */
  dialogue?: string
  /** 提示词可编辑层 */
  prompt_editable?: string
  /** 强绑场景资产 ID */
  scene_id?: number
  /** 角色关联列表（整体替换） */
  character_links?: { character_id: number; variant_id?: number; link_confidence?: number }[]
}

/** 单条分镜告警 */
export interface StoryboardWarning {
  /** 关联分镜 ID（全局性告警为空） */
  storyboard_id?: number | null
  /** 告警级别：warning / error */
  level: string
  /** 告警内容 */
  message: string
}

/** 分镜告警汇总（GET /storyboards/warnings） */
export interface StoryboardWarnings {
  /** 全部分镜预计时长合计（秒） */
  total_duration: number
  /** 项目目标时长（秒） */
  target_duration?: number | null
  /** 总时长相对目标的偏差比例 */
  over_ratio?: number | null
  /** 告警列表 */
  warnings: StoryboardWarning[]
}

/** 分镜重生成响应 */
export interface StoryboardRegenResult {
  /** 累计重生成次数 */
  regen_count: number
  /** >= 3 次时建议进入画布精修（RD-25） */
  suggest_canvas: boolean
}

// ============================================================
// 生成任务 / 成片（Studio 阶段四 / 发布）
// ============================================================

/** 生成任务（对应 TaskOut） */
export interface GenTask {
  /** 任务 ID */
  id: number
  /** 项目 ID */
  project_id?: number | null
  /** 关联分镜 ID */
  storyboard_id?: number | null
  /** 任务类型：image/video/tts/compose/llm/extract */
  type: string
  /** 阶段：trial/full/incremental */
  phase: string
  /** 状态：queued/running/done/failed/canceled */
  status: string
  /** 进度百分比 */
  progress: number
  /** 结果资源地址 */
  result_url?: string | null
  /** 错误码 */
  error_code?: string | null
  /** 错误信息 */
  error?: string | null
  /** 已重试次数 */
  retry_count: number
  /** 冻结/消耗积分 */
  cost_points: number
  /** 预估剩余秒数 */
  estimated_seconds?: number | null
  /** 创建时间 */
  created_at?: string | null
}

/** 关键帧任务创建响应 */
export interface KeyframeCreateResult {
  /** 新建任务列表 */
  tasks: GenTask[]
  /** 本次冻结积分总额 */
  total_cost: number
}

/** SSE 进度事件负载（任务摘要） */
export interface TaskProgressEvent {
  /** 项目任务摘要列表 */
  tasks: { id: number; type: string; status: string; progress: number }[]
  /** 非终态任务数 */
  running: number
}

/** 成片版本（对应 OutputOut） */
export interface FilmOutput {
  /** 版本记录 ID */
  id: number
  /** 版本号 */
  version_no: number
  /** 成片地址 */
  url: string
  /** 分辨率 */
  resolution: string
  /** 画幅比例 */
  aspect_ratio: string
  /** 时长（秒） */
  duration: number
  /** 隐形水印标识 */
  watermark_id?: string | null
  /** 是否当前版本 */
  is_current: boolean
  /** 分享令牌（未发布为空） */
  share_token?: string | null
  /** 分享过期时间 */
  share_expires_at?: string | null
  /** 是否设置了提取码 */
  has_password: boolean
  /** 分享是否已撤销 */
  share_revoked: boolean
  /** 创建时间 */
  created_at?: string | null
}

/** 审核记录（对应 ReviewOut） */
export interface ReviewRecord {
  /** 记录 ID */
  id: number
  /** 审核闸门：generate/compose/publish */
  gate: string
  /** 目标类型：text/image/video/film */
  target_type: string
  /** 目标 ID（多态） */
  target_id?: number | null
  /** 机审结果：pass/suspect/violate */
  result: string
  /** 违规类型 */
  violation_type?: string | null
  /** 人工复核状态 */
  manual_status?: string | null
  /** 可操作建议 */
  suggestion?: string | null
  /** 创建时间 */
  created_at?: string | null
}

/** 成片合成请求体 */
export interface ComposePayload {
  /** 输出分辨率：720P/1080P/2K */
  resolution?: '720P' | '1080P' | '2K'
  /** 画幅比例（缺省沿用项目配置） */
  aspect_ratio?: '9:16' | '16:9' | '1:1'
  /** 合成模式：incremental/full */
  mode?: 'incremental' | 'full'
}

/** 发布（生成分享链接）请求体 */
export interface PublishPayload {
  /** 要发布的成片版本 ID */
  output_id: number
  /** 分享有效期（天） */
  share_expires_days?: number
  /** 提取码（可空） */
  share_password?: string
}

/** 发布响应 */
export interface PublishResult {
  /** 分享链接路径（/share/{token}） */
  share_url: string
  /** 分享过期时间 */
  expires_at: string
}

/** 公开分享页数据（GET /api/share/{token}，免认证） */
export interface ShareInfo {
  /** 项目标题 */
  title: string
  /** 成片地址 */
  video_url: string
  /** 分辨率 */
  resolution: string
  /** 时长（秒） */
  duration: number
  /** 隐形水印标识 */
  watermark_id?: string | null
  /** 发布时间 */
  published_at?: string | null
}

// ============================================================
// AI 画布
// ============================================================

/** 画布基础信息（对应 CanvasOut） */
export interface Canvas {
  /** 画布 ID */
  id: number
  /** 关联项目 ID（空为独立画布） */
  project_id?: number | null
  /** 画布名称 */
  title: string
  /** 类型：standalone/studio_linked */
  type: string
  /** 视口状态（JSONB） */
  viewport?: Record<string, unknown> | null
  /** 缩略图 URL */
  thumbnail_url?: string | null
  /** 创建时间 */
  created_at?: string | null
  /** 更新时间 */
  updated_at?: string | null
}

/** 画布节点（对应 CanvasNodeOut） */
export interface CanvasNode {
  /** 节点 ID */
  id: number
  /** 画布 ID */
  canvas_id: number
  /** 节点类型：text/image/video/audio/generate/asset_ref/shot/compose/logic/group */
  node_type: string
  /** 画布 X 坐标 */
  position_x: number
  /** 画布 Y 坐标 */
  position_y: number
  /** 节点宽 */
  size_w?: number | null
  /** 节点高 */
  size_h?: number | null
  /** 层级 */
  z_index: number
  /** 节点内容（JSONB） */
  data?: Record<string, unknown> | null
  /** 节点配置（JSONB） */
  config?: Record<string, unknown> | null
  /** 脏节点标记（RD-45） */
  is_dirty: boolean
  /** 缓存结果引用 */
  cache_ref?: string | null
  /** 逻辑节点循环语义（RD-48） */
  loop_config?: Record<string, unknown> | null
  /** 节点状态：idle/queued/running/done/failed */
  status: string
  /** 分组父节点 ID */
  parent_group_id?: number | null
}

/** 画布连线（对应 CanvasEdgeOut） */
export interface CanvasEdge {
  /** 连线 ID */
  id: number
  /** 源节点 ID */
  source_node_id: number
  /** 目标节点 ID */
  target_node_id: number
  /** 源端口/输出类型 */
  source_port?: string | null
  /** 目标端口/输入类型 */
  target_port?: string | null
}

/** 画布完整图（GET 图 / 同步 / 导出共用） */
export interface CanvasGraph {
  /** 画布基础信息 */
  canvas: Canvas
  /** 全量节点列表 */
  nodes: CanvasNode[]
  /** 全量连线列表 */
  edges: CanvasEdge[]
}

/** 画布列表项（含计数缩略字段） */
export interface CanvasListItem {
  /** 画布 ID */
  id: number
  /** 关联项目 ID */
  project_id?: number | null
  /** 画布名称 */
  title: string
  /** 类型 */
  type: string
  /** 缩略图 URL */
  thumbnail_url?: string | null
  /** 节点数量 */
  node_count: number
  /** 连线数量 */
  edge_count: number
  /** 创建时间 */
  created_at?: string | null
  /** 更新时间 */
  updated_at?: string | null
}

/** 画布创建请求体 */
export interface CanvasCreatePayload {
  /** 画布名称 */
  title: string
  /** 关联项目 ID（可空） */
  project_id?: number
  /** 预置模板（可空） */
  template?: string
}

/** 画布节点提交条目（同步/导入共用） */
export interface CanvasNodeIn {
  /** 节点 ID（新建可传客户端临时 id） */
  id?: number
  /** 节点类型 */
  node_type: string
  /** 画布 X 坐标 */
  position_x?: number
  /** 画布 Y 坐标 */
  position_y?: number
  /** 节点宽 */
  size_w?: number
  /** 节点高 */
  size_h?: number
  /** 节点内容 */
  data?: Record<string, unknown>
  /** 节点配置 */
  config?: Record<string, unknown>
  /** 层级 */
  z_index?: number
  /** 分组父节点 ID */
  parent_group_id?: number
}

/** 画布连线提交条目 */
export interface CanvasEdgeIn {
  /** 连线 ID（新连线不传） */
  id?: number
  /** 源节点 ID（可为同批临时 id） */
  source_node_id: number
  /** 目标节点 ID */
  target_node_id: number
  /** 源端口 */
  source_port?: string
  /** 目标端口 */
  target_port?: string
}

/** 图全量同步请求体 */
export interface GraphSyncPayload {
  /** 全量节点列表 */
  nodes: CanvasNodeIn[]
  /** 全量连线列表 */
  edges: CanvasEdgeIn[]
}

/** 单节点执行结果 */
export interface NodeExecuteResult {
  /** 节点 ID */
  id: number
  /** 节点类型 */
  node_type: string
  /** 执行后状态 */
  status: string
  /** 执行后脏标记 */
  is_dirty: boolean
  /** 执行后缓存引用 */
  cache_ref?: string | null
  /** 执行后的节点内容 */
  data?: Record<string, unknown> | null
  /** generate 节点创建的任务 ID */
  task_id?: number | null
}

/** DAG 增量执行结果 */
export interface CanvasExecuteResult {
  /** 实际执行的节点 ID（拓扑序） */
  executed: number[]
  /** 因缓存跳过的节点 ID */
  skipped: number[]
  /** 新建的生成任务 ID */
  tasks: number[]
}

/** 画布助手对话响应 */
export interface AssistantChatResult {
  /** 助手回复文本 */
  reply: string
  /** 实际应用的动作数量 */
  applied: number
  /** 应用动作后的最新节点列表 */
  nodes: CanvasNode[]
  /** 应用动作后的最新连线列表 */
  edges: CanvasEdge[]
}

// ============================================================
// 智能体
// ============================================================

/** 智能体会话列表项（GET /agent/threads） */
export interface AgentThread {
  /** 会话唯一标识 */
  thread_id: string
  /** 当前阶段（最近一次意图） */
  stage?: string | null
  /** 最近更新时间 */
  updated_at?: string | null
}

/** 会话消息回放项 */
export interface AgentMessageItem {
  /** 角色：user/assistant/system */
  role: string
  /** 消息文本 */
  content: string
}

/** 会话消息回放响应 */
export interface ThreadMessages {
  /** 会话唯一标识 */
  thread_id: string
  /** 当前阶段 */
  stage?: string | null
  /** 消息列表（时间升序） */
  messages: AgentMessageItem[]
}

/** 意图承接结果（POST /agent/intake） */
export interface IntakeResult {
  /** 意图枚举：script/asset/storyboard/generation/canvas/billing/chat 等 */
  intent: string
  /** 置信度（0-1） */
  confidence: number
  /** 判定方式：llm / rule */
  mode: string
}

// ============================================================
// 素材库
// ============================================================

/** 媒体素材（对应 MediaOut） */
export interface MediaAsset {
  /** 素材 ID */
  id: number
  /** 归属用户 ID */
  user_id: number
  /** 归属项目 ID（可空） */
  project_id?: number | null
  /** 媒体类型：image/video/audio/text */
  media_type: string
  /** 来源：ai_generated/upload/canvas/studio */
  source: string
  /** 稳定地址（相对存储路径） */
  url: string
  /** 元数据（JSONB） */
  meta?: Record<string, unknown> | null
  /** 上传时间 */
  created_at?: string | null
}

// ============================================================
// 提示词库
// ============================================================

/** 提示词（对应 PromptOut） */
export interface Prompt {
  /** 提示词 ID */
  id: number
  /** 归属用户 ID（空为公共提示词） */
  user_id?: number | null
  /** 范围：public / private */
  scope: string
  /** 适用能力（角色定妆/分镜画面/运镜/配音等） */
  capability?: string | null
  /** 标题 */
  title: string
  /** 内容 */
  content: string
  /** 分类 */
  category?: string | null
  /** 标签（JSONB） */
  tags?: unknown
  /** 创建时间 */
  created_at?: string | null
}

/** 提示词创建/更新请求体（更新时字段可空） */
export interface PromptPayload {
  /** 标题 */
  title: string
  /** 内容 */
  content: string
  /** 适用能力 */
  capability?: string
  /** 分类 */
  category?: string
  /** 标签 */
  tags?: unknown
}
