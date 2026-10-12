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

---

## 视觉机审模型探测（Task 7 图像/视频真实视觉机审，2026-10-10 12:50 ~ 12:53）

- 脚本：`.run/vision_probe.py`（backend venv 运行，全部为真实 HTTP 调用）
- 目的：Task 7（合规红线 R-08/C-01）为图像/视频产物接入**真实视觉机审**，
  需先确认 Agnes 网关是否有可用的视觉理解模型及其请求格式。

### 1. GET /v1/models 模型列表（HTTP 200，鉴权通过）

共 11 个模型，均无显式视觉命名（无 vision / vl / multimodal 关键词）：

```
agnes-2.5-pro-alpha / agnes-video-2.5-flash / agnes-3.0-flash /
agnes-image-2.1-flash / agnes-video-2.5 / agnes-image-2.0-flash /
agnes-2.5-flash / agnes-3.0-flash-max / agnes-2.5-pro /
agnes-2.5-pro-beta / agnes-image-2.5-flash
```

### 2. 名称推断失效 → 转实测验证

候选筛选无命中后，对文本 LLM（排除 image/video 等）按旗舰优先取 3 个候选
做小样图文调用：`agnes-2.5-pro-alpha`、`agnes-3.0-flash-max`、`agnes-2.5-pro`。

- 请求格式：标准 OpenAI 视觉协议——`POST /v1/chat/completions`，messages 的
  user content 为数组
  `[{type:"text"}, {type:"image_url", image_url:{url:"data:image/png;base64,..."}}]`
  （测试图取 `storage/generated` 现有真实生成 PNG，705 KiB base64）；
- **三个候选全部 HTTP 200**，并返回准确的图片内容描述。响应样例（节选）：

```json
{"model": "agnes-3.0-flash-max",
 "choices": [{"message": {"content": "这是一张动漫风格的全身立绘，描绘了一位身穿白色长袖衬衫、黑色西裤和黑色皮鞋，双手自然下垂站立的年轻男子。"}}],
 "usage": {"prompt_tokens": 1063, "completion_tokens": 80,
           "prompt_tokens_details": {"image_tokens": 943}}}
```

`usage.prompt_tokens_details.image_tokens=943` 证明图片被真实编码进上下文
（真实视觉理解而非文本幻觉）。

### 3. 探测过程中的干扰项（已排除）

首轮小样调用用 `keyframe_999_1700000000.png`（空文件，0 字节）报
HTTP 400 `invalid chat image: convert image: unexpected EOF`——该错误同时
证明网关图文协议层正常工作（确实在解码图片），仅测试图损坏；换有效 PNG 后全部通过。

### 4. 结论

- **Agnes 旗舰文本模型即多模态视觉模型**，OpenAI 兼容图文协议可直接使用：
  可用模型 `agnes-3.0-flash-max`（默认，3.0 代旗舰）、`agnes-2.5-pro-alpha`、
  `agnes-2.5-pro`；
- 实施落地：`config.agnes_vision_model = "agnes-3.0-flash-max"`（可配置），
  provider `backend/app/providers/vision_review.py` 按上述请求格式实现，
  调用失败（网络/超时 15s/输出非法）由 runner 自动降级
  vision → llm-text → keyword 文本机审，任务链路不阻塞。

---

## 2026-10-10 复核（21:05 ~ 21:11，Task 8 渠道健康巡检）

- 时间：2026-10-10 21:05 ~ 21:11（Asia/Shanghai）
- 脚本：`.run/llm_image_video_tts_probe.py`（backend venv 运行）
- 触发：Task 8 渠道健康巡检——复核 5 渠道现状，核对库中 health_status 与实际可用性

### 汇总表（本轮复核）

| 渠道 | 结果 | 耗时 | 关键信息 |
| --- | --- | --- | --- |
| LLM 主 DeepSeek（deepseek-flash） | **FAIL** | 1.0s | HTTP 200 但回复为空（`reply=''`）——**与上轮 402 欠费不同**，本轮 Key 鉴权已通过，但 `deepseek-flash` 模型返回空 content；建议业务侧核对模型名配置（DeepSeek 官方现役模型为 `deepseek-chat` / `deepseek-reasoner） |
| LLM 兜底 Agnes（agnes-3.0-flash） | **PASS** | 31.2s | chat 回复 `OK`（较上轮 1.6s 明显变慢，但可用；预期结论一致） |
| 图像 Agnes（agnes-image-2.5-flash） | **FAIL** | 1.5s | HTTP 503 `text image queue is full`——**与预期 PASS 不符**：免费渠道图像队列本轮也饱和（上轮 00:10 时 PASS 出图成功），属容量波动非契约问题 |
| 视频 Agnes（agnes-video-2.5-flash） | **FAIL** | 242.5s | HTTP 503 `video_queue_full`，5 次 60s 退避重试横跨 4 分钟均满（与上轮一致，队列从未消化；与预期 503 队列满相符） |
| TTS 百炼（qwen3-tts-flash） | **FAIL** | 1.5s | `POST /audio/speech` HTTP 404 快速失败——与上轮 8 轮探测结论一致：该专属实例不开放任何 HTTP TTS 端点（与预期 404 实例限制相符） |

### 与预期的偏差说明

- **图像 Agnes**：预期 PASS（上轮出图成功），实测 503 队列满——免费渠道容量实时
  波动，契约层零错误；渠道恢复后重跑即可回归 PASS，无需改代码。
- **LLM 主 DeepSeek**：预期 402 欠费，实测 HTTP 200 空回复——鉴权与计费层均已
  放行（欠费状态有变化），但 `deepseek-flash` 模型名返回空 content。这不是网络
  或鉴权问题，指向模型名配置有效性，建议核对 `DEEPSEEK_MODEL`（官方现役模型
  为 `deepseek-chat`）。降级链不受影响：DeepSeek 失败自动切 Agnes 兜底（本轮
  Agnes PASS）。

### 健康巡检联动结论

- 本轮 4/5 渠道实测 FAIL，与 `model_channels.health_status` 的 degraded/down
  方向一致，不存在「长期 degraded 但探测 PASS」的失真场景；兜底链
  （Agnes LLM + 链尾 mock 哨兵）覆盖全部失败渠道，生成链路不阻塞。
- 呈现一致性已核对：用户端 `GET /api/models/health-summary` 与管理端
  `GET /api/admin/models/channels` 同源读取 `model_channels.health_status`；
  管理端 `POST /api/admin/models/channels/{id}/test`（probe_channel 落库）与
  `PUT /api/admin/models/channels/{id}`（手动修正）提供复位路径（README 已补说明）。
- `record_result` 成功自动复位语义已确认并在测试锁定
  （`tests/test_model_router.py::test_record_result_chenggong_fuwei_degraded`）：
  degraded 渠道一次业务调用成功即自动复位 healthy。

---

## 2026-10-10 21:15 复测（DeepSeek 模型名修复后，主渠道恢复 PASS）

- 触发：上轮复核发现 DeepSeek `deepseek-flash` 网关接受但返回空 content（指向模型名
  配置无效）。A/B 实测：`deepseek-flash` → 51 tokens 空输出；`deepseek-chat` → 正常
  回复「成功」。**根因确认为模型名过时**。
- 修复：`config.deepseek_model` 默认值 → `deepseek-chat`；主库
  `model_channels.models`（id=1）与 `logical_models.name`（id=1）同步更新；
  `init_db` 种子硬编码旧名一并修正（TTS 渠道种子同步改为引用 `settings.bailian_tts_model`）。
- **复测结果（21:15 ~ 21:18+）**：

| 渠道 | 结果 | 关键信息 |
| --- | --- | --- |
| LLM 主 DeepSeek（deepseek-chat） | **PASS** | 1.0s，reply='OK'——主渠道恢复 |
| LLM 兜底 Agnes（agnes-3.0-flash） | **PASS** | 4.7s |
| 图像 Agnes（agnes-image-2.5-flash） | **PASS** | 11.5s 出图落盘（上轮 503 为容量波动） |
| 视频 Agnes | **任务已接受** | 2 次 503 重试后 HTTP 200 拿到 video_id（task_OHpR7WYbSqSRvc…），轮询 `/agnesapi` 正常进行；探测脚本 300s 超时先于成片返回，accepted→completed 端到端待成片时间验证，契约与轮询链路已实证 |
| TTS 百炼 | FAIL（既有） | 实例不开放 HTTP TTS 端点（8 轮证据归档，降级链兜底） |

- 结论：**5 渠道中 3 个真实 PASS + 视频链路已通到服务端队列**；剩余 FAIL 仅
  TTS 百炼实例限制（外部依赖，证据归档）。配置侧问题已全部修复。
