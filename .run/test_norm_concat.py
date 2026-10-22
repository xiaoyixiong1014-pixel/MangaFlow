# -*- coding: utf-8 -*-
"""拼接归一化实测脚本（开发期验证用，不属于交付物）。

验证：两镜真实产物 → _merge_clip_audio 归一化（音轨钳制/规格统一）→
concat → 拼接产物音视频流 duration 差 < 10ms、总时长 ≈ 两镜之和。
"""
import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.services.compose_service import _concat_clips, _merge_clip_audio  # noqa: E402


def stream_durations(path: Path) -> tuple:
    """读视频/音频流时长（秒）。"""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    vdur = adur = 0.0
    for line in r.stdout.strip().splitlines():
        parts = line.split(",")
        if len(parts) < 2 or not parts[1]:
            continue
        try:
            dur = float(parts[1])
        except ValueError:
            continue
        if parts[0] == "video":
            vdur = dur
        elif parts[0] == "audio":
            adur = dur
    return vdur, adur


async def main() -> None:
    """主流程：两镜归一化 → concat → 等长校验。"""
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT id, sequence, video_url FROM storyboards "
            "WHERE project_id=34 AND episode_id=33 ORDER BY sequence LIMIT 2"
        ))).all()
    out_dir = Path("storage/outputs").resolve()
    merged_paths = []
    total_expect = 0.0
    for sid, seq, url in rows:
        vp = (Path("storage/generated") / Path(url).name).resolve()
        out = out_dir / f"_norm_{sid}.mp4"
        merged = await _merge_clip_audio("ffmpeg", "ffprobe", vp, None, out,
                                         target_size=(720, 1280), target_fps=24.0)
        vdur, adur = stream_durations(out)
        diff = abs(vdur - adur)
        total_expect += vdur
        merged_paths.append(out)
        print(f"镜#{seq}: v={vdur:.3f}s a={adur:.3f}s |v-a|={diff*1000:.1f}ms "
              f"{'OK' if diff < 0.01 else 'DRIFT'}")
    film = out_dir / "_norm_concat.mp4"
    ok = await _concat_clips("ffmpeg", merged_paths, film, out_dir / "_norm_list.txt")
    vdur, adur = stream_durations(film)
    print(f"concat: {'OK' if ok else 'FAIL'} | v={vdur:.3f}s a={adur:.3f}s "
          f"|v-a|={abs(vdur-adur)*1000:.1f}ms 期望≈{total_expect:.3f}s "
          f"偏差={abs(vdur-total_expect)*1000:.1f}ms")
    if not ok:
        print("保留中间产物以排查（_norm_*.mp4）")
        return
    for p in merged_paths:
        p.unlink(missing_ok=True)
    (out_dir / "_norm_list.txt").unlink(missing_ok=True)
    film.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
