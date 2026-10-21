# -*- coding: utf-8 -*-
"""句级字幕时间轴实测脚本（开发期验证用，不属于交付物）。"""
import asyncio
import shutil
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
    """主流程：取真实分镜 → 句级时间轴 → 烧录验证。"""
    async with SessionLocal() as db:
        sb = (await db.execute(
            select(Storyboard)
            .where(Storyboard.video_url.is_not(None), Storyboard.audio_url.is_not(None))
            .order_by(Storyboard.id.desc()).limit(1)
        )).scalars().first()
    segs = build_storyboard_subtitle_segments(sb)
    print(f"分镜 {sb.id} 句级字幕条数: {len(segs or [])}")
    for s in (segs or []):
        print(f"  [{s['start']:5.2f} -> {s['end']:5.2f}] {s['text']}")
    video = Path("storage/generated") / Path(sb.video_url).name
    out = Path("storage/generated/_sub_check.mp4")
    ok = await burn_storyboard_subtitles("ffmpeg", "ffprobe", video, segs, out)
    print("burn:", ok)
    if ok:
        shutil.copy(out, "../.run/_sub_check.mp4")
        out.unlink()


if __name__ == "__main__":
    asyncio.run(main())
