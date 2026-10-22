# -*- coding: utf-8 -*-
"""抽查分镜视频产物完整性（口型/字幕/配音，开发期排查用）。"""
import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402


def probe(path: str) -> None:
    """音轨音量 + 抽帧。"""
    r = subprocess.run(
        ["ffmpeg", "-i", path, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True)
    mv = next((l.split(":")[1].strip() for l in r.stderr.splitlines() if "mean_volume" in l), "?")
    print(f"  音轨 mean_volume: {mv} dB")
    out = Path("../.run/sb_sample.png")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", path,
                    "-frames:v", "1", str(out)])
    print(f"  抽帧: {out.resolve()}")


async def main() -> None:
    """取该集第 1、12 镜的当前视频产物检查。"""
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT id, sequence, video_url FROM storyboards "
            "WHERE project_id=34 AND episode_id=33 AND sequence IN (1,12) ORDER BY sequence"
        ))).all()
    for sid, seq, url in rows:
        path = "storage/generated/" + Path(url).name
        print(f"镜#{seq} ({Path(url).name}):")
        probe(path)


if __name__ == "__main__":
    asyncio.run(main())
