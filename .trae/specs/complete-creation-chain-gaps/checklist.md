# Checklist

## P0 可信度与合规（D1/D2/D3）
- [x] config.py 无任何 `sk-` 明文默认值，密钥全部经环境变量注入，`.env.example` 提供模板
- [x] 无密钥启动时输出"渠道缺少密钥"WARNING 且服务正常启动（真实渠道走降级链）
- [x] 空片段项目合成产出可播放 MP4 占位（或明确 failed），film_outputs 不再出现 `.svg` 成片 URL
- [x] image/video/tts mock 产物统一带"演示占位"标识（画面文字 + `placeholder_` 文件名前缀 + meta.placeholder=true）
- [x] 占位产物自动退款且流水备注"演示占位豁免"，前端产物显示占位角标
- [x] test_placeholder_governance.py 通过

## S-06 剧集管理
- [x] episodes CRUD + reorder 接口齐全，归属校验与软删生效
- [x] 剧本/分镜/合成/发布按 episode_id 组织，storyboards.py 不再有 episode_id=0 占位
- [x] StudioLayout 剧集切换器可用，五阶段列表按当前剧集过滤
- [x] 跨剧集资产复用入口可用
- [x] test_episodes.py 通过

## P1 链路补全
- [x] `POST /script/send-to-agent` 与 `/script/send-to-canvas` 幂等可用，ScriptView 有入口
- [x] characters/scenes 有 reference_image_url 字段（init_db 幂等补列，项目惯例），上传真实落库
- [x] 生成任务携带参考图入参（真实渠道图生图 / mock 记录 meta）
- [x] `POST /tasks/{id}/replace` 可用：归属校验、落库、联动分镜缩略图
- [x] VideoCreateRequest 含 resolution/fps/motion_strength，参数透传渠道并写 meta，前端有三参数配置
- [x] 尾帧生成接口可用，use_end_frame=true 走首尾帧模式，前端可指定尾帧
- [x] `POST /canvas/nodes/{id}/sink` 可沉淀为资产（source=canvas + 自动 visual_prompt），画布有入口
- [x] test_script_sync / test_reference_image / test_replace_upload / test_video_params / test_canvas_sink 通过

## P2 收尾
- [x] 资产标签可编辑、可搜索过滤，批量删除与单删行为一致
- [x] 分镜导出支持 PDF 且中文不乱码（字体降级不 500）
- [x] 连续性检查只报高置信度冲突，无冲突时结果为空
- [x] 目标总时长自动调整可一键应用并返回摘要
- [x] 分享卡片生成二维码
- [x] 发布页可一键跳转重生成指定片段
- [x] 双语字幕开关可用（ASS 双行，LLM 失败降级单行）
- [x] 转场支持淡入淡出/叠化/闪黑/闪白映射，非法值回退 fade
- [x] 剧本失败面板有重试/换模型/精简输入三路径
- [x] 工作台统计卡展示图片数/视频时长/积分消耗；项目列表最近编辑置顶
- [x] test_asset_tags / test_storyboard_export_pdf / test_continuity / test_compose_bilingual / test_transition_types 通过

## 文档与验收
- [x] backend/docs/api-creation-chain.md 覆盖全部新增/修改接口（方法/路径/参数/响应/错误码，约 30 端点 + 全局约定 + 变更记录）
- [x] 新增列表接口分页评估完成，索引评估与响应时间基线记录于 README 与文档 §11
- [x] 后端 pytest 全量回归全绿（247 例：既有 + 新增 93 例）并留存证据（backend/.pytest_final_run.log）
- [x] 双端 vue-tsc + build 通过（mangaflow-web build exit 0 / admin-web type-check exit 0）
