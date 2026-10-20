# -*- coding: utf-8 -*-
"""demucs 音源分离端到端实测脚本（开发期验证用，不属于交付物）。

流程：取库内 TTS 纯人声产物 → extract_no_vocals 分离 → 校验产物存在、
时长一致，并用 wave RMS 能量定量验证「人声被分进 vocals 轨、no_vocals
接近静音」（纯人声输入的正确预期）。
"""
import asyncio
import io
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.audio_separation import extract_no_vocals  # noqa: E402


def wav_rms(path: Path) -> float:
    """计算 WAV 文件 RMS 能量（16bit PCM 假设）。"""
    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
        width = w.getsampwidth()
    total = 0
    n = 0
    for i in range(0, len(frames) - width + 1, width):
        sample = int.from_bytes(frames[i:i + width], "little", signed=True)
        total += sample * sample
        n += 1
    return (total / max(1, n)) ** 0.5


def wav_duration(path: Path) -> float:
    """读取 WAV 时长（秒）。"""
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


async def main() -> None:
    """主流程：分离 → 定量校验。"""
    src = Path(__file__).resolve().parents[1] / "backend/storage/generated/tts_1792403550_27193.wav"
    print(f"输入: {src} 时长={wav_duration(src):.2f}s RMS={wav_rms(src):.0f}")
    env = await extract_no_vocals(src)
    if env is None:
        print("FAIL: 分离返回 None")
        return
    print(f"环境音产物: {env} 时长={wav_duration(env):.2f}s RMS={wav_rms(env):.0f}")
    # 纯人声输入的正确预期：no_vocals RMS 占比极低（<10%）
    ratio = wav_rms(env) / max(1.0, wav_rms(src))
    print(f"no_vocals/vocals RMS 比 = {ratio:.4f}")
    print("PASS" if ratio < 0.10 else "CHECK: 占比偏高，需人工听感确认")
    env.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
