# -*- coding: utf-8 -*-
"""Wav2Lip 口型同步端到端实测脚本（开发期验证用，不属于交付物）。

流程：库内最新 wan 视频产物 + 段级拼接配音 → lipsync_video_sync →
校验输出存在、时长与配音对齐、音轨存在。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.lipsync_service import lipsync_video  # noqa: E402


async def main() -> None:
    """主流程：口型同步 → 产物校验。"""
    gen = Path(__file__).resolve().parents[1] / "backend/storage/generated"
    video = gen / "clip_369_1792474709.mp4"
    audio = gen / "tts_1_segfull_58250.wav"
    out = gen / "_lipsync_test.mp4"
    print(f"视频: {video.name} | 配音: {audio.name}")
    t0 = time.time()
    ok = await lipsync_video(video, audio, out, Path(__file__).resolve().parents[1] / "backend/storage/models/lipsync")
    dt = time.time() - t0
    print(f"lipsync 结果: {ok}  耗时 {dt:.1f}s")
    if ok:
        ffprobe = "ffprobe"
        import subprocess
        for label, p in (("输出", out), ("配音", audio)):
            r = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                capture_output=True, text=True)
            print(f"{label}时长: {r.stdout.strip()}s")
        a = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True)
        print(f"输出音轨: {a.stdout.strip() or '（无）'}")


if __name__ == "__main__":
    asyncio.run(main())
