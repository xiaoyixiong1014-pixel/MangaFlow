# -*- coding: utf-8 -*-
"""单行字幕切分实测脚本（开发期验证用，不属于交付物）。"""
import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.compose_service import (  # noqa: E402
    build_storyboard_subtitle_segments,
    burn_storyboard_subtitles,
)


class FakeSb:
    """模拟分镜：用户截图中的 13 字长句。"""

    audio = {
        "dialogue": [
            {"speaker": "老陈", "text": "啊——林晚你工位上怎么有只一",
             "audio_url": "/x.wav", "duration_sec": 2.4}
        ]
    }
    dialogue = "老陈：啊——林晚你工位上怎么有只一"
    actual_duration = 2.4
    duration = 2.4


async def main() -> None:
    """主流程：9 字上限切分验证 + 真实视频烧录抽帧。"""
    segs = build_storyboard_subtitle_segments(FakeSb(), max_chars=9, min_chars=4)
    print(f"条数: {len(segs or [])}")
    for s in (segs or []):
        print(f"  [{s['start']:5.2f} -> {s['end']:5.2f}] ({len(s['text'])}字) {s['text']}")
    video = Path("storage/generated/clip_369_1792474709.mp4")
    out = Path("storage/generated/_sub_check.mp4")
    ok = await burn_storyboard_subtitles("ffmpeg", "ffprobe", video, FakeSb(), out)
    print("burn:", ok)
    if ok:
        shutil.copy(out, "../.run/_sub_line.mp4")
        out.unlink()


if __name__ == "__main__":
    asyncio.run(main())
