# -*- coding: utf-8 -*-
"""成片无声修复实测脚本（开发期验证用，不属于交付物）。

场景：compose_film 对「已内嵌配音的 v3 产物」跳过混音（audio_path=None）。
验证修复后 _merge_clip_audio 输出音轨非静音（volumedetect mean_volume）。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.compose_service import _merge_clip_audio  # noqa: E402


def mean_volume(path: Path) -> float:
    """volumedetect 平均音量（dB），静音轨约 -91dB。"""
    import subprocess
    r = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "mean_volume" in line:
            return float(line.split(":")[1].replace("dB", "").strip())
    return -999.0


async def main() -> None:
    """主流程：带配音 v3 产物 → 跳过混音路径（None）→ 验证音轨有声。"""
    gen = Path("storage/generated")
    # 找最近一个带视频与配音的分镜产物
    sb_video = None
    for p in sorted(gen.glob("clip_*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
        sb_video = p
        break
    print("样本:", sb_video.name)
    out = Path("storage/outputs/_avfix_check.mp4")
    merged = await _merge_clip_audio("ffmpeg", "ffprobe", sb_video, None, out)
    print("merge:", merged)
    if merged:
        mv = mean_volume(out)
        print(f"输出 mean_volume: {mv:.1f} dB（> -50 为有声，-91 为静音）")
        print("PASS" if mv > -50 else "FAIL: 音轨仍是静音")
        out.unlink()


if __name__ == "__main__":
    asyncio.run(main())
