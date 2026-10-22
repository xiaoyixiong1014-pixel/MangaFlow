# -*- coding: utf-8 -*-
"""片头标题卡生成实测脚本（开发期验证用，不属于交付物）。"""
import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.compose_service import _build_text_card  # noqa: E402


async def main() -> None:
    """主流程：生成 720x1280 片头卡（剧名+集数两行）并抽帧校验。"""
    out = Path("storage/outputs/_intro_check.mp4")
    card = await _build_text_card(
        "ffmpeg", out, 720, 1280,
        [("倒计时10分钟奇迹", 0.055, 0.42), ("第1集 · 电梯里的最后一次心跳", 0.032, 0.55)],
        2.5,
    )
    print("card:", card)
    if card:
        import subprocess
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(card)],
            capture_output=True, text=True)
        print("duration:", r.stdout.strip())
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1.2", "-i", str(card),
                        "-frames:v", "1", "../.run/card_check.png"])
        out.unlink()


if __name__ == "__main__":
    asyncio.run(main())
