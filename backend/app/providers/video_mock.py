"""视频生成 Mock 提供方模块（文件级说明）。

无真实视频渠道时的占位实现：
- 本机存在 ffmpeg：用 lavfi 纯色源生成一段 720x1280 的 MP4 占位片段；
- 不存在 ffmpeg：降级生成一张标注"视频占位"的 SVG 海报图；
- 任何失败返回 None，由调用方按 platform_error 处理（自动重试/退款）。

函数签名与真实视频渠道对齐（prompt + duration + out_dir + seed → url + meta），
日后接入真实渠道仅需替换本模块实现。
"""

import asyncio
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

from app.providers.image_mock import _URL_PREFIX, _write_svg, render_placeholder_svg

# 单段占位视频最大时长（秒），超长截断
_MAX_CLIP_SECONDS = 5
# ffmpeg 子进程超时（秒）
_FFMPEG_TIMEOUT = 10


async def generate_video(
    prompt: str, duration_sec: int, out_dir: Path, seed: int
) -> Optional[Dict]:
    """生成一段占位视频片段或视频海报（Mock 视频渠道，可插拔）。

    Args:
        prompt: 视频提示词（生成降级海报时渲染到图上）。
        duration_sec: 期望时长（秒），实际生成时长截断到最多 5 秒。
        out_dir: 输出目录（storage/generated 的绝对路径）。
        seed: 随机种子（一般传任务 ID，保证文件名唯一）。

    Returns:
        Optional[Dict]: 成功返回
            {"url": "/storage/generated/clip_{seed}.mp4", "meta": {"mock": True}}（ffmpeg 可用）；
            或 {"url": "/storage/generated/poster_{seed}_{ts}.svg",
                "meta": {"mock": True, "poster": True}}（ffmpeg 不可用，SVG 海报降级）；
            ffmpeg 执行失败 / 写盘失败等场景返回 None。

    Raises:
        无：内部捕获全部异常并返回 None，不向上抛出。
    """
    try:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            clip_name = f"clip_{seed}.mp4"
            out_path = out_dir / clip_name
            out_dir.mkdir(parents=True, exist_ok=True)
            real_duration = max(1, min(int(duration_sec or 5), _MAX_CLIP_SECONDS))
            cmd = [
                ffmpeg, "-y",
                "-f", "lavfi",
                "-i", f"color=c=0x1C2223:s=720x1280:d={real_duration}",
                "-pix_fmt", "yuv420p",
                str(out_path),
            ]
            # 同步子进程放入线程池执行，避免阻塞事件循环；超时按平台失败处理
            proc = await asyncio.to_thread(
                subprocess.run, cmd,
                timeout=_FFMPEG_TIMEOUT,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if proc.returncode == 0 and out_path.exists():
                return {"url": f"{_URL_PREFIX}/{clip_name}", "meta": {"mock": True}}
            return None
        # 无 ffmpeg：降级生成 SVG 视频海报
        ts = int(time.time())
        filename = f"poster_{seed}_{ts}.svg"
        content = render_placeholder_svg(prompt, seed, label="视频占位")
        _write_svg(out_dir, filename, content)
        return {
            "url": f"{_URL_PREFIX}/{filename}",
            "meta": {"mock": True, "poster": True},
        }
    except Exception:
        return None
