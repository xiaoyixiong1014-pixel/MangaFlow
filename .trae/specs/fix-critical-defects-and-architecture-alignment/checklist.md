# Checklist

## 高危缺陷（报告 R-01）
- [x] 资产页正常渲染画廊，接口请求次数与用户操作一致（无循环请求，C-BUG-1 关闭）——根因：el-tab-pane 未声明 name 与 activeTab 恒不匹配致全部 pane 隐藏（空白）+ pending 初始态被误判 busy 的 3s 轮询死环；修复后首载仅 1 次 GET /assets 且零增长
- [x] 图像任务成功产物的 media.url 通过静态挂载可 GET 200，分镜缩略图不再「加载失败」（C-BUG-2 关闭）——根因：main.py 静态挂载相对路径解析出 backend\backend\storage 空目录，与 storage_path() 落盘口径脱节；修复后 mock/真实产物 curl 均 200（修复前 404）
- [x] 后台用户列表积分列与钱包实际余额一致（A-BUG-1 关闭）——根因：前端误用嵌套绑定 row.wallet?.points_balance（列表接口为扁平结构）；绑定修正 + 后端扁平 schema 契约测试锁死
- [x] 三项修复均有回归测试或浏览器实测证据留档——test_static_storage 4 用例 + test_admin 契约用例 + type-check + curl 证据（详见 tasks.md Task 1/2）

## 架构偏离收敛（报告 §2.1 七项逐项两态收敛）
- [x] 队列：启动时 running 孤儿任务自动重置 queued 并可被领取（3 用例佐证）；文档拍板「进程内 runner + Celery 演进路径」（架构文档 §8 修订记录）
- [x] pgvector：扩展可用走 SQL 余弦+ivfflat，不可用自动降级 JSONB+Python 余弦且行为不变；compose 采用 pgvector/pgvector:pg16 镜像（test_vector_dual 4 用例 + 自愈降级 + SAVEPOINT 双写）
- [x] 对象存储：文档拍板「本地存储 + OSS 演进项」（架构文档 §8）；storage_path() 唯一落盘入口经 Task 2 全量排查确认
- [x] Alembic：初始迁移 e2b3b5b5c90a 入库，空库 upgrade head 与主库 32 表全维度零差异，downgrade base 可回，ALEMBIC_ON_STARTUP 开关可用（asyncio.to_thread 规避事件循环），alembic check 干净
- [x] 部署：deploy/ 9 文件落库，本机三镜像构建成功（backend 1.28GB/web 96.1MB/admin 97.1MB），compose 冒烟 postgres+backend healthy + /api/metrics 200 + 32 表初始化 + 网关 5 项 200，nginx -t 通过，已 down -v 零残留
- [x] LangSmith：环境变量接线生效（配置时追踪、未配置零开销，2 用例佐证）；README 使用说明（链路就绪待密钥启用）
- [x] Deep Agents：文档拍板「自研等价实现」口径修订完成（架构文档 §8 修订记录）；7 项偏离对账全部处于「已补齐实现」或「文档已修订拍板」两态，无未收敛项

## 渠道健康与合规（报告 R-02/R-08）
- [x] 五渠道探测复核完成，record_result「成功即复位 healthy」逻辑确认已具备（测试锁定）；复核额外发现并修复 DeepSeek 模型名过时（deepseek-flash 空回复 → deepseek-chat，config/主库/种子四处同步）——修复后复测 DeepSeek/Agnes LLM/图像三渠道 PASS，视频任务被服务端接受进入轮询（契约修复实证）
- [x] DeepSeek（欠费误判实为模型名，已修复）/ 百炼 TTS 实例无 HTTP TTS 端点（8 轮证据归档）两项外部依赖处理完毕，看板健康呈现与实际一致（双端同源核对）
- [x] 图像/视频产物优先视觉机审（method=vision，Agnes 旗舰多模态实测 image_tokens 计费证据），失败降级 llm-text→keyword，verdict 流转与端到端拦截有 10 用例佐证（19 passed 含回归）

## Time Travel 与版本回退（报告 §7.2#9）
- [x] MA-06 history/rewind 链路复核证据留存（test_agent_hitl 4 passed + README 留证），报告「待确认」口径有更新依据
- [x] V-01 快照回退 API + 前端入口可用，回退后数据一致且操作留痕（4 用例：主链路/可撤销 pre_rollback/404/400；软删复活机制 + impact_scope 实体状态增强）

## 杂项与文档（报告 ENV-01/CV-21/CV-22/R-10）
- [x] 管理端 IPv4 可达（vite server.host 显式 0.0.0.0，实测 curl 127.0.0.1 200，对照旧配置复现 [::1] 现象）
- [x] 插件 endpoint 协议/主机白名单校验生效（SSRF 防护含十进制 IP 反解析与 IPv4-mapped IPv6，安装+invoke 双重校验，2 新用例 9 passed）；CV-21「已实现」核实关闭（TS SDK 导出完整 + dist 在位，报告口径已更正）
- [x] 架构文档 V1.1 修订完成（六处口径 + 新增 §8 修订记录留痕），进度报告 CV-21 口径更正记录

## 质量门
- [x] pytest 全量 `-k "not real"` 全绿：**153 passed**（基线 122 + 新增 31，5 分 38 秒）
- [x] 双前端 type-check 通过，build 通过（均 exit 0）
- [x] 新增代码含中文注释，README 更新完成（Alembic 迁移/pgvector 双模/LangSmith/部署/视觉机审/队列恢复说明）
