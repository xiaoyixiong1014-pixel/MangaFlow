"""阿里云百炼 TTS 提供方模块（文件级说明）。

通过 OpenAI 兼容协议调用百炼语音合成渠道（POST /audio/speech）：
- 音色映射：业务侧音色标识（male_01/female_01 等）→ 百炼 qwen-tts 音色名
  （Ethan/Cherry 等），未知音色回退 Cherry；
- 响应 content-type 为 audio/* 或 application/octet-stream 时按 WAV 落盘；
- 时长优先用 wave 模块读取实际帧数计算（RD-31 回写分镜实际时长的依据），
  解析失败降级为 len(text)*0.18 估算，并在 meta 中标注 estimated=True。
任何失败返回 None（不抛出），由调用方（tasks/runner.py）降级到下一渠道。

【2026-10-09 实例探测结论】当前专属实例
（ws-66dgbbhi5gzzll8r.cn-beijing.maas.aliyuncs.com）经 8 轮真实探测不支持
可用的 HTTP TTS 端点，证据如下（临时探测脚本 .run/tmp_probe_tts*.py，已删）：
1. GET {base}/models → HTTP 200（鉴权通过，262 个模型），但列表中不存在
   默认模型 qwen-audio-3.1-tts-flash，真实 TTS 模型为 qwen3-tts-flash、
   qwen3-tts-instruct-flash、qwen-tts-2025-05-22 等；
2. POST {base}/audio/speech（/v1 双拼、/services/aigc/text2speech、/audio/tts
   等变体）对以上所有真实模型名均 HTTP 404 空响应体——网关不开放 REST TTS；
3. POST {base}/chat/completions + model=qwen3-tts-flash：缺 voice 时报
   "The voice property is required."（说明该实例 TTS 走 chat 通道），补
   voice="Cherry" 后对任何文本（含阿里官方文档示例原样）均报
   "Due to invalid text, invalid audio was returned."——合成链不可用；
4. qwen3-tts-instruct-flash / qwen-tts-2025-05-22 走 chat 通道时透传
   DashScope 原生错误 "<400> InternalError.Algo.InvalidParameter: Field
   required: input.text"（同时提供 messages 与 input.text 仍报错）——
   网关内部协议转换链未完成；qwen3-tts-realtime 系列仅支持 WebSocket，
   不在 HTTP provider 范围内。

处理策略：保留 OpenAI 兼容 /audio/speech 实现以备正式端点开放（仅改
base_url 即可启用）；当前 404 时快速失败返回 None，由 runner 降级链
（链尾 channel_id=0 本地 mock 哨兵）兜底，生成链路不阻塞。

函数签名与 mock TTS 渠道对齐（text + voice + out_dir + channel → url + duration_sec），
channel 为 model_router.ResolvedChannel。
"""

import logging
import time
import wave
from pathlib import Path
from typing import Dict, Optional

import httpx

from app.services.model_router import ResolvedChannel

logger = logging.getLogger("mangaflow.providers.tts_bailian")

# 生成结果 URL 前缀：main.py 已将 storage 目录 mount 到 /storage
_URL_PREFIX = "/storage/generated"
# 请求超时（秒）
_HTTP_TIMEOUT = 60.0
# 估算时长系数：每字 0.18 秒（与 tts_mock 一致）
_SECONDS_PER_CHAR = 0.18

# 业务音色标识 → 百炼 qwen-tts 音色名映射
_VOICE_MAP: Dict[str, str] = {
    "male_01": "Ethan",
    "male_02": "Aiden",
    "female_01": "Cherry",
    "female_02": "Serena",
    "narrator": "Ethan",
}


def _read_wav_duration(path: Path) -> Optional[float]:
    """用 wave 模块读取 WAV 文件实际时长（内部函数）。

    Args:
        path: WAV 文件路径。

    Returns:
        Optional[float]: 时长（秒，浮点）；文件损坏/非 WAV 格式返回 None。

    Raises:
        无：解析异常统一收敛为 None。
    """
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 1
            return round(frames / rate, 2)
    except Exception:  # noqa: BLE001 —— 时长读取失败走估算降级
        return None


async def generate_tts_real(
    text: str,
    voice: str,
    out_dir: Path,
    channel: ResolvedChannel,
) -> Optional[Dict]:
    """调用百炼渠道真实合成一段配音 WAV。

    Args:
        text: 待合成的台词文本。
        voice: 业务音色标识（male_01/female_01/narrator 等，按 _VOICE_MAP 映射）。
        out_dir: 输出目录（storage/generated 的绝对路径）。
        channel: 已解析的候选渠道（含 base_url/api_key/model）。

    Returns:
        Optional[Dict]: 成功返回
            {"url": "/storage/generated/tts_xxx.wav",
             "duration_sec": 时长秒数,
             "meta": {"provider": "bailian", "channel_id": 渠道ID,
                      "voice": 百炼音色名, ["estimated": True]}}；
            请求失败/响应格式异常返回 None（由调用方降级到下一渠道）。

    Raises:
        无：内部捕获全部异常并返回 None，不向上抛出。
    """
    try:
        base = channel.base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {channel.api_key}"}
        payload = {
            "model": channel.model,
            "input": text,
            "voice": _VOICE_MAP.get(voice, "Cherry"),
            "response_format": "wav",
        }
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(f"{base}/audio/speech", headers=headers, json=payload)
            content_type = (resp.headers.get("content-type") or "").lower()
            is_audio = content_type.startswith("audio/") or content_type == "application/octet-stream"
            if resp.status_code != 200 or not is_audio:
                logger.warning("渠道 %s 语音合成失败（HTTP %s, %s）：%s",
                               channel.name, resp.status_code, content_type, resp.text[:300])
                return None
            filename = f"tts_{int(time.time())}_{abs(hash((text or '', voice))) % 100000}.wav"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / filename
            path.write_bytes(resp.content)

        # 时长：优先真实帧数计算，失败按文本长度估算（meta 标注 estimated）
        duration = _read_wav_duration(path)
        meta: Dict = {"provider": "bailian", "channel_id": channel.channel_id,
                      "voice": _VOICE_MAP.get(voice, "Cherry")}
        if duration is None:
            duration = round(min(max(len(text or ""), 1) * _SECONDS_PER_CHAR, 60.0), 2)
            meta["estimated"] = True

        return {"url": f"{_URL_PREFIX}/{filename}", "duration_sec": duration, "meta": meta}
    except Exception as exc:  # noqa: BLE001 —— provider 失败由调用方降级处理
        logger.warning("渠道 %s 语音合成异常：%s", channel.name, exc)
        return None
