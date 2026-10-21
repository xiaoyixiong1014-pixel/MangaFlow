# -*- coding: utf-8 -*-
"""分镜字幕烧录端到端实测脚本（开发期验证用，不属于交付物）。

流程：取库内有配音的分镜 → build_storyboard_subtitle_segments 构建
时间轴 → burn_storyboard_subtitles 烧录 → 校验产物存在与时长。
"""
import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.models import Storyboard  # noqa: E402
from app.services.compose_service import (  # noqa: E402
    build_storyboard_subtitle_segments,
    burn_storyboard_subtitles,
)


async def main() -> None:
    """主流程：选镜 → 构建字幕段 → 烧录 → 校验。"""
    async with SessionLocal() as db:
        # 取最近一个有关键帧与配音的分镜
        rows = (await db.execute(
            select(Storyboard)
            .where(Storyboard.video_url.is_not(None), Storyboard.audio_url.is_not(None))
            .order_by(Storyboard.id.desc())
            .limit(1)
        )).scalars().all()
        sb = rows[0] if rows else None
    if sb is None:
        print("无可测分镜")
        return
    segs = build_storyboard_subtitle_segments(sb)
    print(f"分镜 {sb.id}：audio.dialogue 段数={len((sb.audio or {}).get('dialogue') or [])}"
          f" 字幕段数={len(segs or [])}")
    for s in (segs or [])[:4]:
        print(f"  [{s['start']:.2f} -> {s['end']:.2f}] {s['text'][:24]}")
    if not segs:
        print("无字幕段，跳过烧录")
        return
    video = Path("storage/generated") / Path(sb.video_url).name
    out = Path("storage/generated/_sub_test.mp4")
    ffmpeg = "ffmpeg"
    ok = await burn_storyboard_subtitles(ffmpeg, "ffprobe", video, segs, out)
    print(f"烧录结果: {ok}")
    if ok:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True)
        print(f"输出时长: {r.stdout.strip()}s（原视频 {video.name}）")
        out.unlink()


if __name__ == "__main__":
    asyncio.run(main())
