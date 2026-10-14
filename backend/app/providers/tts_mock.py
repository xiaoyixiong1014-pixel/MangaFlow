"""语音合成（TTS）Mock 提供方模块（文件级说明）。

无真实语音渠道时的占位实现：仅用标准库（wave/math/struct/hashlib）合成
16kHz 单声道 16bit WAV 音频——整体为静音，按规律插入低幅（0.02 振幅）
正弦发声段，模拟"有声音"的波形；时长按文本长度估算（RD-31 回写依据）。

打标约定（D3）：产物文件名统一携带 placeholder_ 前缀，返回结果 meta
携带 placeholder=True 标记（执行器平铺进任务 meta 供 API 透出与计费豁免）。

函数签名与真实 TTS 渠道对齐（text + voice + out_dir → url + duration_sec），
日后接入真实渠道仅需替换本模块实现。
"""

import hashlib
import math
import struct
import time
import wave
from pathlib import Path
from typing import Dict, Optional

from app.providers.image_mock import _URL_PREFIX

# 音频参数：16kHz 采样率、单声道、16bit
_SAMPLE_RATE = 16000
# 发声正弦振幅（16bit 满幅 32767 的 0.02）
_AMPLITUDE = 0.02
# 发声正弦频率（Hz）
_TONE_HZ = 220
# 时长系数：每字 0.18 秒
_SECONDS_PER_CHAR = 0.18
# 时长上限（秒）
_MAX_SECONDS = 60.0


async def generate_tts(text: str, voice: str, out_dir: Path) -> Optional[Dict]:
    """合成一段占位 WAV 音频（Mock TTS 渠道，可插拔）。

    Args:
        text: 待合成的文本（时长按 len(text)*0.18 估算，封顶 60 秒）。
        voice: 音色标识（如 male_01 / female_01，Mock 下仅参与文件命名）。
        out_dir: 输出目录（storage/generated 的绝对路径）。

    Returns:
        Optional[Dict]: 成功返回
            {"url": "/storage/generated/placeholder_tts_{ts}_{hash}.wav",
             "duration_sec": 实际音频时长（秒，浮点）,
             "meta": {"mock": True, "placeholder": True}}；
            写盘失败等异常场景返回 None。

    Raises:
        无：内部捕获全部异常并返回 None，不向上抛出。
    """
    try:
        duration = min(max(len(text or ""), 1) * _SECONDS_PER_CHAR, _MAX_SECONDS)
        n_samples = int(_SAMPLE_RATE * duration)
        frames = bytearray()
        for i in range(n_samples):
            t = i / _SAMPLE_RATE
            # 规律发声：每 0.5 秒周期内的前 0.3 秒为低幅正弦，其余静音
            if (t % 0.5) < 0.3:
                value = int(_AMPLITUDE * 32767 * math.sin(2 * math.pi * _TONE_HZ * t))
            else:
                value = 0
            frames += struct.pack("<h", value)
        # 文件名：时间戳 + 文本摘要，保证唯一；D3 打标统一带 placeholder_ 前缀
        digest = hashlib.md5((text or "").encode("utf-8")).hexdigest()[:8]
        filename = f"placeholder_tts_{int(time.time())}_{digest}.wav"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / filename
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)          # 单声道
            wf.setsampwidth(2)          # 16bit
            wf.setframerate(_SAMPLE_RATE)
            wf.writeframes(bytes(frames))
        return {
            "url": f"{_URL_PREFIX}/{filename}",
            "duration_sec": round(n_samples / _SAMPLE_RATE, 2),
            "meta": {"mock": True, "placeholder": True},
        }
    except Exception:
        return None
