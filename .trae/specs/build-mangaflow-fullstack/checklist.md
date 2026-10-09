# Checklist

## 数据库与初始化
- [x] mangaflow 数据库自动创建成功，31 张业务表全部存在且字段/索引符合架构文档 §5.3（实测 35 表≥31，含 LangGraph 自动建的 checkpoint 表；storyboards.actual_duration、characters.face_locked/outfit_variant_id、uk_wtx_idem 唯一索引均实查存在）
- [x] 种子数据幂等写入：admin(operator)/demo 用户、DeepSeek 渠道与逻辑模型、公共提示词、公告、积分套餐（billing /api/billing/packages 返回套餐）
- [x] 重复执行 init_db 不报错、不产生重复数据（连跑两次日志"已存在，跳过"，行数不变）

## 后端核心
- [x] 注册/登录/刷新接口可用，JWT 含 role，密码为哈希存储（bcrypt $2b$12$）
- [x] 用户 A 无法访问用户 B 的项目资源（404 实测）
- [x] 计费流程：冻结→成功扣费 / 失败幂等退款，流水含幂等键与 bear，重复提交不重复扣款（重复 pay 余额不变实测）
- [x] 剧本生成 SSE 流式返回且结构化剧本落库（实测 742 事件 delta+done），实体提取含别名/冲突/低置信并支持人工裁决
- [x] 资产生成状态机与两层锁定（face_locked/outfit_variant_id）、变体 inherit_mask 生效（缺 mask 400 实测）
- [x] 分镜拆解强绑资产（storyboard_characters + link_confidence，psql 实查 16 行），提示词两层字段，重排可用
- [x] 生成任务：打样/全量 phase、进度/失败重试/取消可用，失败自动退款，幂等键防重（违规词→content_violation+退款、重复 full→409 实测）
- [x] 成片版本 is_current 管理、三级审核记录、分享链接（token/有效期/密码/撤回）可用（revoke 后 404 实测）
- [x] 画布：节点连线自环禁止、成环检测 400 且数据回滚、JSON 导入导出、增量执行复用 cache_ref
- [x] LangGraph：Supervisor 意图路由正确分发（intake→script/llm），/agent/chat SSE 可用，DeepSeek 调用配置正确（config.py 三参数与用户提供值一致）
- [x] 管理端接口 role=operator 门禁（end_user 403 实测），积分调整/禁用/退款留痕 operation_logs
- [x] 模型渠道 Key AES-256（Fernet）加密存储（含种子渠道已迁移为密文），健康探测与优先级接口可用

## 测试
- [x] backend pytest 全部通过（54/54，auth/billing/projects/script/assets/storyboard/generation/publish/canvas/admin/agents）
- [x] mangaflow-web 与 mangaflow-admin-web vue-tsc type-check 通过、build 成功（双端 exit=0）

## 前端 mangaflow-web
- [x] @mangaflow/design 接入（styles.css/element-plus.css/data-theme 主题桥接），官网浅色 + 创作端深色 + 明暗切换记忆（mf_theme）
- [x] 全部路由页面实现且可导航（25 个视图/路由核对无缺），/collab 灰显 disabled
- [x] 侧栏 11 项菜单 + 顶栏（积分余额/通知/主题/头像），Studio 五阶段步骤条互跳
- [x] 官网输入→登录弹窗保留输入→落地 /agent 意图预填的主转化链可用（HomePage→AuthDialog→setIntent→acceptIntent 代码链确认）
- [x] 画布编辑器支持节点拖拽/连线校验/自动保存/助手对话/小地图（自研实现，含类型白名单+DFS 预检+Ctrl+S）
- [x] 响应式：≤1360px 侧栏折叠、≤1024px 抽屉化（ui.isMobile + el-drawer 实现，type-check 零错误）

## 前端 mangaflow-admin-web
- [x] 独立登录页（非 operator 拒绝提示）+ 8 个管理页面全部实现并对接真实接口（grep 确认各视图调用 @/api/admin）
- [x] 高危操作（积分调整/禁用/退款）二次确认 + 原因输入 + 成功留痕（UsersView/OrdersView prompt 必填实测）

## 一键脚本与集成
- [x] `.\mangaflow.ps1 start` 一键拉起 backend:8300 / web:5173 / admin:5174，重复执行幂等（PID 不变实测；8300 为避开用户 8000 端口占用项目而调整）
- [x] `.\mangaflow.ps1 status` 正确显示各服务与数据库状态
- [x] `.\mangaflow.ps1 stop` 安全终止全部服务，8300/5173/5174 端口无残留（含项目进程路径匹配保护，未误杀他项目）
- [x] 全链路冒烟通过：注册→建项目→剧本 SSE（DeepSeek 真实流式 727 帧）→提取→资产→分镜→生成（关键帧/视频/TTS 含 actual_duration 回写）→合成→发布分享→管理端处置（40+ 步全 PASS 无 5xx）
- [x] backend/README.md 说明 API、启动方式与环境配置；/docs 可访问

## 验收方式说明
- 以上全部条目经独立验收子代理实测（29 项首次判定 + 2 项修复后复核），最终 31/31 PASS
- 独立验收报告要点：pytest 54/54（126s）、双前端 type-check exit=0、init_db 幂等、成环 400 回滚、越权 404、门禁 403、Fernet 密文、stop 后端口全释放
