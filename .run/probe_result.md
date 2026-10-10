# 渠道连通性探测结果（Task 11.2 + 视频/TTS 契约修复复测）

- 时间：2026-10-10 00:10 ~ 00:15（Asia/Shanghai）
- 脚本：`.run/llm_image_video_tts_probe.py`（backend venv 运行）
- 网络：api.deepseek.com / apihub.agnes-ai.com / ws-66dgbbhi5gzzll8r.cn-beijing.maas.aliyuncs.com 均可达
- 本轮为视频/TTS 契约修复后的复测（首次探测见下方历史记录）

## 汇总表（契约修复后）

| 渠道 | 结果 | 耗时 | 关键信息 |
| --- | --- | --- | --- |
| LLM 主 DeepSeek（deepseek-flash） | **FAIL** | 0.9s | HTTP 402 `Insufficient Balance`（Key 鉴权通过，账户余额不足；既有问题未变） |
| LLM 兜底 Agnes（agnes-3.0-flash） | **PASS** | 1.6s | chat 回复 `OK` |
| 图像 Agnes（agnes-image-2.5-flash） | **PASS** | 24.5s | 落盘 `backend/storage/generated/keyframe_12345_1791562222.png` |
| 视频 Agnes（agnes-video-2.5-flash） | **FAIL** | 245.5s | 契约已修复：请求通过全部参数校验进入服务端队列层，但免费渠道 `503 video_queue_full` 持续（5 次 60s 退避重试横跨 5 分钟均满，探测期间 23:50~00:14 队列从未消化）；未拿到 video_id，异步轮询段未走到 |
| TTS 百炼（qwen3-tts-flash） | **FAIL** | 0.6s | `POST /audio/speech` HTTP 404 快速失败——经 8 轮探测确认该专属实例不开放任何 HTTP TTS 端点（详见下文），降级链兜底 |

## 视频 Agnes 契约修复说明（本轮核心改动）

- 根因：官方文档（wiki.agnes-ai.com/en/docs/agnes-video-25-flash）确认请求体
  需要 `mode` 字段（枚举 `text` / `keyframe` / `reference`），且 Flash 版
  `size` 仅支持字符串 `"720P"`（竖版经 `aspect_ratio="9:16"` 表达），
  轮询须走根域名 `GET /agnesapi?video_id=<ID>&model_name=<model>`。
- 修复内容（`backend/app/providers/video_agnes.py`）：
  1. 请求体增加 `mode=settings.agnes_video_mode`（默认 `"text"`，settings 新字段），
     `size="720P"`、`aspect_ratio="9:16"`，`seconds` 钳制为字符串 `"4"~"12"`；
  2. 新增可选参数 `first_frame_url`：传入时自动切 `keyframe` 模式并携带
     `first_frame` 图片 URL（首帧图生视频场景，runner 现不传、签名向后兼容）；
  3. 轮询改走官方 `{root}/agnesapi` 端点（带 `video_id` + `model_name` 参数）；
  4. 提交遇 `503 video_queue_full` / `429` 做 60s 退避 ×5 次重试（免费渠道容量保护）。
- 验证证据：
  - 修复前：HTTP 400 `mode is required`；错误候选值 400 `invalid mode`；
  - 修复后：请求体被服务端完整接受（无任何参数校验错误），返回
    `503 {"code":"video_queue_full","message":"video queue is full, please retry later"}`——
    契约层零错误，仅剩免费渠道容量/限流问题（重试逻辑已按官方提示实现）；
  - 轮询端点可达性：`GET /agnesapi?video_id=<假ID>&model_name=agnes-video-2.5-flash`
    返回 HTTP 404 `{"error":{"code":404,"message":"task not found"}}`（业务层错误——
    端点路由、鉴权、响应结构均与官方文档对齐）；由于队列持续满未能拿到真实
    video_id，accepted → completed 的完整轮询旅程未实测，待渠道容量恢复后重跑即可。

## TTS 百炼探测结论（本轮核心改动）

- `GET {base}/models` HTTP 200（鉴权通过，262 个模型），但默认模型
  `qwen-audio-3.1-tts-flash` **不存在**，真实 TTS 模型为 `qwen3-tts-flash`、
  `qwen3-tts-instruct-flash`、`qwen-tts-2025-05-22` 等（settings 默认值已改为
  `qwen3-tts-flash`）。
- `POST {base}/audio/speech`（含 `/v1` 双拼、`/services/aigc/text2speech`、
  `/audio/tts` 变体）对所有真实 TTS 模型均 HTTP 404 空响应——网关不开放 REST TTS。
- `POST {base}/chat/completions` + `qwen3-tts-flash`：缺 `voice` 报
  `The voice property is required.`；补 `voice` 后对任何文本（含阿里官方文档
  示例原样）均报 `Due to invalid text, invalid audio was returned.`——合成链不可用；
  instruct/老 qwen-tts 透传 DashScope 原生错误 `Field required: input.text`
  （同时给 messages 与 input.text 仍报错）——网关内部协议转换链未完成；
  realtime 系列（`qwen3-tts-flash-realtime` 等）仅支持 WebSocket，不在 HTTP provider 范围。
- 处理：`tts_bailian.py` 顶部保留探测证据说明；保留 OpenAI 兼容 `/audio/speech`
  实现以备正式端点开放（仅改 base_url 即可启用）；当前 404 快速失败返回 None，
  由 runner 降级链（链尾 channel_id=0 mock 哨兵）兜底，pytest 回归通过。

## 结论与影响

- 退出码 1（LLM 主渠道 FAIL）——符合脚本约定：退出码 0 仅当主渠道 PASS。
- 主渠道欠费不影响系统可用性：业务调用走 model_router 降级链，DeepSeek 失败自动切
  Agnes 兜底；图像/视频/TTS 失败自动降级链尾本地 mock 渠道（channel_id=0），生成链路不阻塞。
- 视频渠道：**接口契约已修复并实测通过参数校验**（400 mode is required 消失），
  当前 FAIL 仅因免费渠道视频队列持续饱和（容量问题）；`agnes_video_mode` 已配置化，
  首帧图生视频（keyframe 模式）已预留支持，待渠道容量恢复重跑探测即可端到端通过。
- TTS 渠道：该百炼专属实例经探测不支持任何 HTTP TTS 端点（证据见上），provider 保持
  快速失败 + 降级链兜底，模型名默认值已修正为实例真实存在的 `qwen3-tts-flash`。

---

## 历史记录（契约修复前首次探测，2026-10-09 23:13 ~ 23:14）

| 渠道 | 结果 | 耗时 | 关键信息 |
| --- | --- | --- | --- |
| LLM 主 DeepSeek（deepseek-flash） | **FAIL** | 0.9s | HTTP 402 `Insufficient Balance` |
| LLM 兜底 Agnes（agnes-3.0-flash） | **PASS** | 3.1s | chat 回复 `OK` |
| 图像 Agnes（agnes-image-2.5-flash） | **PASS** | 64.2s | 落盘 `backend/storage/generated/keyframe_12345_1791558831.png` |
| 视频 Agnes（agnes-video-2.5-flash） | **FAIL** | 1.7s | 提交 HTTP 400：`mode is required` |
| TTS 百炼（qwen-audio-3.1-tts-flash） | **FAIL** | 0.7s | `POST /audio/speech` HTTP 404 |
