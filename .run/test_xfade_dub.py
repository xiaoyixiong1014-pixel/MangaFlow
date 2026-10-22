# -*- coding: utf-8 -*-
"""有配音转场实测脚本（开发期验证用，不属于交付物）。

验证：两镜带配音片段 → _merge_clip_audio 归一化 → _xfade_clips（fade 0.5s）
→ 输出时长=d1+d2-0.5、音轨有声、转场过渡正常。
"""
import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.services.compose_service import (  # noqa: E402
    _merge_clip_audio,
    _probe_duration,
    _xfade_clips,
)


def mean_volume(path: Path) -> float:
    """volumedetect 平均音量（dB）。"""
    r = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "mean_volume" in line:
            return float(line.split(":")[1].replace("dB", "").strip())
    return -999.0


def duration(path: Path) -> float:
    """format duration。"""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(r.stdout.strip())


async def main() -> None:
    """主流程：归一化两镜 → 有配音转场 → 时长/音轨/转场帧校验。"""
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT id, sequence, video_url FROM storyboards "
            "WHERE project_id=34 AND episode_id=33 ORDER BY sequence LIMIT 2"
        ))).all()
    out_dir = Path("storage/outputs").resolve()
    paths, durs = [], []
    for sid, seq, url in rows:
        vp = (Path("storage/generated") / Path(url).name).resolve()
        out = out_dir / f"_xv_{sid}.mp4"
        m = await _merge_clip_audio("ffmpeg", "ffprobe", vp, None, out,
                                    target_size=(720, 1280), target_fps=24.0)
        d = await _probe_duration("ffprobe", m)
        paths.append(m)
        durs.append(d)
        print(f"镜#{seq}: 归一化 {d:.3f}s")
    xf = out_dir / "_xv_film.mp4"
    ok = await _xfade_clips("ffmpeg", paths, durs, xf,
                            {"type": "fade", "duration": 0.5})
    exp = sum(durs) - 0.5
    got = duration(xf) if ok else -1
    mv = mean_volume(xf) if ok else -999
    print(f"xfade: {'OK' if ok else 'FAIL'} 时长 {got:.3f}s（期望 {exp:.3f}s "
          f"偏差 {abs(got-exp)*1000:.0f}ms）音轨 {mv:.1f}dB")
    if ok:
        # 转场中点抽帧（应看到叠化混合画面）+ 音轨验证
        mid = durs[0] - 0.25
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{mid:.2f}",
                        "-i", str(xf), "-frames:v", "1", "../.run/xfade_mid.png"])
        print("转场中点帧: ../.run/xfade_mid.png")
        for p in paths:
            p.unlink(missing_ok=True)
        xf.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
