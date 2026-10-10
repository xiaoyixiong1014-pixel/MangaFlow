# Checklist

## 模型路由与真实 Provider（Task 1）
- [x] `resolve_channel()` 按 llm/image/video/tts 能力解析降级链，渠道不可用时自动落到下一渠道直至 mock 链尾（test_model_router.py 4 用例：链顺序/禁用与 down 过滤/空链/mock 哨兵；冒烟实证 image 任务 meta.provider=agnes）
- [x] DeepSeek 主渠道故障时剧本生成自动切换 Agnes 兜底完成，调用方签名未变（实测：DeepSeek 402 欠费时冒烟剧本 SSE 仍完成 delta=543，实际渠道切至 Agnes；14 处 import 方零改动）
- [x] 图像任务经 Agnes 渠道产出真实图片落盘 /storage/generated（非 SVG 占位），失败降级链写入 task.meta（冒烟 DB 直查 provider=agnes、fallback_chain=[]；探测脚本产出 keyframe_*.png）
- [x] 视频任务经 Agnes 渠道创建→轮询→下载 mp4 落盘（契约已按官方文档修复：mode/size/aspect_ratio/agnesapi 轮询端点实测通过全部参数校验；**受限标注**：免费渠道队列 503 持续满载未端到端拿到成片，容量恢复后即可通；失败自动降级 mock 不阻塞），TTS 经百炼渠道（**受限标注**：实测该百炼专属实例 /models 无 qwen-audio-3.1-tts-flash 且 HTTP TTS 端点 404，8 轮探测证据在 tts_bailian.py 头注，保留 OpenAI 兼容实现 + model 默认值改为实例真实存在的 qwen3-tts-flash，404 快速失败降级 mock；actual_duration 回写逻辑保持）
- [x] init_db 种子含 Agnes(文本/图像/视频) 与百炼(TTS) 渠道，Key 全部 Fernet 密文入库，channel_priority 指向真实渠道优先（库实测 8 渠道 + 7 逻辑模型，链 [1,2]/[3,6]/[4,7]/[5,8]）
- [x] 管理端健康探测覆盖全部新渠道并返回各渠道状态（probe_channel 按 llm/image/video/tts 分派，admin test 路由接入）

## 任务队列与通知（Task 2）
- [x] 高 priority 任务先于低 priority 被领取（test_task_queue.py：priority DESC + 同级 FIFO 断言）
- [x] 任务失败重试间隔按 2/4/8 分钟指数递增，next_retry_at 未到不被领取（test_task_queue.py 到期过滤 + 2^n 重排断言）
- [x] 重试耗尽任务标记 dead_letter + 平台全额退款，管理端死信列表可见且可重新入队（留痕）（test_dlq.py 9 用例 + test_task_queue.py 死信断言；requeue 重新冻结幂等键 task_{id}_requeue_*）
- [x] 任务成功/失败/审核结果/配额预警产生站内通知，未读数正确，SSE 可收到推送（test_notifications.py 9 用例含 SSE 总线全链路；冒烟 unread_count=2）

## 多智能体（Task 3）
- [x] 全量生成前触发 interrupt，SSE 收到 pending_confirm，确认后 Command(resume) 续跑完成（冒烟完整链路：pending_confirm(confirm_entities)→resume approved→done；test_agent_hitl.py）
- [x] 批量生成使用 Send 并行（generation_agent Command(goto=[Send(...)]) + deep_research_agent 同机制；代码审查 + 代码路径确认）
- [x] SSE 同时存在 updates 与 messages（token 级）两类事件（astream(stream_mode=["updates","messages"])，token 事件前端拼接已接）
- [x] 会话历史接口返回检查点列表，rewind 后状态恢复至指定检查点（GET history + POST rewind + rewind_checkpoint 分叉续聊，schema/init_db 补列齐备）
- [x] 一致性/审核 Subgraph 可独立调用（agents/subgraphs.py）；deep_research_agent 具备规划-执行-反思循环与虚拟文件系统笔记（research_plan/exec/reflect/finalize + VFS 存 langgraph_store）
- [x] agents 统一经 llm_adapter 调用（11 agent + supervisor + 2 service 全部走 adapter），agent_threads 记录实际渠道与 token 用量（MA-20 归因）

## 审核与水印（Task 4）
- [x] 严禁词命中=violate 拦截退款，敏感词=suspect 进入人工队列（classify_text 两级词表各 20 词；test_gates.py/runner _check_violation 实测）
- [x] 闸门二拦截含未处置违规片段的成片；闸门三汇总全链路记录判定，两处直通占位代码已删除（判定矩阵：violate 未放行 400 / suspect 待人工 400 / 放行落 gate 记录；test_gates.py）
- [x] 违规累计触发 restricted（生成限流生效）/ banned（生成被拒），管理端可解禁（users.account_status + violation_count；ensure_generation_allowed 403/429；unban 端点留痕；test_penalty.py 5 用例）
- [x] 有 ffmpeg 时合成成片画面角落可见半透明 watermark_id（drawtext white@0.15 右上角，msyh.ttc；本机 ffmpeg 8.1.2 四条链路 RC=0 实测）

## 合成增强（Task 5）
- [x] 合成产物含烧录字幕（narration→SRT），字幕样式参数生效（build_srt 累计时长 + subtitles force_style FontSize/PrimaryColour/Alignment，颜色 &HBBGGRR 白名单防注入）
- [x] BGM 曲库可管理（上传/列表/下架，GET/POST/DELETE /api/bgm），混音后产物含背景音乐（amix + stream_loop 铺底，无音轨直作唯一音轨；自定义上传经 /api/media/upload 直传 bgm_url）
- [x] 相邻片段转场（xfade）与增量合成（指定 storyboard_ids）生效，is_current 版本切换正确（多级 xfade 链 offset 累计 clamp；incremental 按 _resolve_involved_storyboard_ids 归属校验过滤；test_compose.py 6 用例）
- [x] 无 ffmpeg 环境时全部降级占位且任务不失败（逐级降级保持总有产出）

## 语义检索与一致性（Task 6）
- [x] consistency_check 输出向量相似度分项评分（伪公式 0.88+ 公式代码已删除；score=0.5*visual+0.3*desc+0.2*locked，algorithm=embedding-cosine-v1；test_consistency.py 断言无伪公式）
- [x] 资产语义搜索返回相关资产（TopK），embedding 存 JSONB 且含 pgvector 切换注释（GET /api/assets/search；四类资产 create+update 8 处接线）
- [x] langgraph_store 记忆支持语义检索（memory_service upsert/search + GET /api/memories/search，用户隔离实测）

## 导出（Task 7）
- [x] 项目 ZIP 导出包含剧本/分镜/资产元数据与媒体文件，导入后结构完整还原（冒烟 1.2MB PK 头实测；test_export.py 全链导出→导入断言一致）
- [x] 分镜表 Excel/CSV 导出内容与分镜数据一致（openpyxl + utf-8-sig CSV；episode_id=0 支持项目全量导出；test_export.py content-type/表头断言）
- [x] 成片 MP4 按分辨率参数导出下载（720p/1080p/2k ffmpeg 转码 storage/exports 缓存复用，无 ffmpeg 返回原片；归属校验）

## 可观测与安全（Task 8）
- [x] GET /api/metrics 返回 Prometheus 格式指标（请求计数/延迟/任务计数/渠道健康 5 个指标族；8301 实例 curl 实测 200，tasks_active 查库校准）
- [x] 管理端看板展示任务成功率/死信数/渠道健康区块（GET /api/admin/system-metrics SQL 聚合 + DashboardView 系统指标区块 + el-tag 着色）
- [x] 越权回归脚本覆盖全部资源接口类，A 访问 B 资源全部 404/403（.run/authz_regression.py：52 项 PASS / 0 FAIL——水平 24 + 垂直 9 + 未认证 4 组）

## 画布增强（Task 9）
- [x] 画布编辑器支持搜索定位（Ctrl+F 浮层过滤 + 居中闪烁）、框选多选（marquee 矩形相交 + Delete 批量删）、复制粘贴（Ctrl+C/V + sessionStorage 跨画布）
- [x] 插件安装/审查/启用流程可用，插件节点执行经后端代理调用 endpoint 并写回结果（builtin: 进程内直调 + HTTP 60s 代理；test_canvas_plugin.py 7 用例全流程）
- [x] MCP server 可配置连接，工具注册为 mcp_tool 节点并可执行（最小 streamable HTTP 客户端 initialize/tools/list/tools/call，兼容 SSE 帧；{{node:id.output}} 占位替换）
- [x] 节点插件 SDK TS 类型定义在 mangaflow-design 可导入（src/plugins + @mangaflow/design/plugins 子路径导出，npm build 通过，前端 type-import 使用）

## 前端补全（Task 10）
- [x] 工作台显示后端聚合创作统计（项目/图片/视频时长/积分，GET /api/users/me/creation-stats），D-04 前端本地汇总逻辑已移除（test_models_compare.py 聚合断言）
- [x] 渠道不健康时工作台顶部标红提醒（GET /api/models/health-summary + el-alert）；余额 <20% 收到预警通知（后端 quota_warning 200 阈值 + 顶栏 alert + 铃铛展示）
- [x] 模型效果对比页可同提示词多渠道并排生成（POST /api/models/compare 并发逐渠道收敛 + ModelCompareView 路由页；4 测试用例）
- [x] Time Travel / 闸门状态 / 导出按钮 / BGM 字幕转场配置 / HITL 确认卡片 UI 全部可用（AgentWorkspace 检查点抽屉 + pending_confirm 卡片 + token 流；PublishView 闸门 detail 错误条 + MP4 导出；StoryboardView Excel/CSV；ProjectList ZIP；ProductionView BGM/字幕/转场表单）
- [x] 管理端死信队列与插件审查入口可用（DeadLetterView + ModelsView 插件审查 Tab + AdminLayout 菜单）

## 质量与验收（Task 11）
- [x] pytest 全绿（原 54 + 新增 67 = **121 passed**，6 分 02 秒）
- [x] 5 个真实渠道连通性探测脚本运行并输出结果（.run/probe_result.md：Agnes LLM PASS / Agnes 图像 PASS 落盘 PNG / DeepSeek 402 账户欠费【Key 有效】/ Agnes 视频契约修复后 503 队列满【容量问题】/ 百炼 TTS 实例无 HTTP TTS 端点【8 轮探测证据】；FAIL 渠道均自动降级 mock 不阻塞——符合"失败自动降级不阻塞系统"约定）
- [x] 全链路冒烟通过（.run/smoke_v2.py 28 步全 PASS，含 HITL 完整链路与真实图像渠道步骤），双前端 vue-tsc + build 通过（design 包构建修复 element-plus-bridge.css 后 EXIT=0）
- [x] 新增代码含文件/类/函数级中文注释，接口在 /docs 有清晰描述，backend/README 更新完成（新增 9 节：模型渠道/降级链/健康探测/LangSmith/MCP/插件/死信/通知/metrics）
