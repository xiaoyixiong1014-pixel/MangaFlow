"""视频生成 Mock 提供方模块（文件级说明）。

无真实视频渠道时的占位实现：
- 本机存在 ffmpeg：用 lavfi 纯色源生成一段 720x1280 的 MP4 占位片段，
  并以 drawtext 叠加「演示占位」提示（无可用中文字体时降级为无文字纯色
  片段）；
- 不存在 ffmpeg：降级生成一张标注「演示占位·视频」的 SVG 海报图；
- 任何失败返回 None，由调用方按 platform_error 处理（自动重试/退款）。

打标约定（D3）：产物文件名统一携带 placeholder_ 前缀，返回结果 meta
携带 placeholder=True 标记（执行器平铺进任务 meta 供 API 透出与计费豁免）。

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
# 字体文件候选（参照 compose_service 的探测顺序）：占位提示 drawtext 依赖；
# 全部缺失时省略 drawtext（降级为无文字纯色占位片段）
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)


def _find_font_file() -> Optional[str]:
    """探测本机可用字体文件（内部函数，占位提示 drawtext 依赖）。

    Returns:
        Optional[str]: 首个存在的字体文件路径；全部缺失返回 None。

    Raises:
        无。
    """
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _placeholder_drawtext_filter(text: str) -> Optional[str]:
    """构造占位视频提示文字 drawtext 滤镜（D3 打标，内部函数）。

    Args:
        text: 提示文字（如「演示占位」）。

    Returns:
        Optional[str]: drawtext 滤镜表达式；本机无可用字体时返回 None
            （调用方降级为无文字纯色占位片段）。

    Raises:
        无：纯字符串构造，不产生异常路径。
    """
    font = _find_font_file()
    if font is None:
        return None
    # 规范化为 POSIX 风格后再转义（Windows 原生反斜杠路径会破坏滤镜解析）
    font_escaped = font.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    text_escaped = text.replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")
    return (
        f"drawtext=fontfile='{font_escaped}':text='{text_escaped}':"
        f"fontsize=42:fontcolor=white@0.85:x=(w-tw)/2:y=(h-th)/2"
    )


async def generate_video(
    prompt: str,
    duration_sec: int,
    out_dir: Path,
    seed: int,
    video_params: Optional[Dict] = None,
    first_frame_url: Optional[str] = None,
    end_frame_url: Optional[str] = None,
) -> Optional[Dict]:
    """生成一段占位视频片段或视频海报（Mock 视频渠道，可插拔）。

    Args:
        prompt: 视频提示词（生成降级海报时渲染到图上）。
        duration_sec: 期望时长（秒），实际生成时长截断到最多 5 秒。
        out_dir: 输出目录（storage/generated 的绝对路径）。
        seed: 随机种子（一般传任务 ID，保证文件名唯一）。
        video_params: 视频参数（P-12：resolution/fps/motion_strength，可空），
            仅记录进返回 meta 供任务溯源，不影响占位渲染。
        first_frame_url: 首帧图片地址（P-03，可空），仅记录进返回 meta。
        end_frame_url: 尾帧图片地址（P-08，可空），仅记录进返回 meta。

    Returns:
        Optional[Dict]: 成功返回
            {"url": "/storage/generated/placeholder_clip_{seed}.mp4",
             "meta": {"mock": True, "placeholder": True, ...溯源字段}}（ffmpeg 可用）；
            或 {"url": "/storage/generated/placeholder_poster_{seed}_{ts}.svg",
                "meta": {"mock": True, "poster": True, "placeholder": True, ...}}
            （ffmpeg 不可用，SVG 海报降级）；
            ffmpeg 执行失败 / 写盘失败等场景返回 None。

    Raises:
        无：内部捕获全部异常并返回 None，不向上抛出。
    """
    try:
        # 溯源字段（P-12/P-03）：有值才记录，保持 meta 精简
        trace_meta: Dict = {}
        if video_params:
            trace_meta["video_params"] = video_params
        if first_frame_url:
            trace_meta["first_frame"] = first_frame_url
        if end_frame_url:
            trace_meta["end_frame"] = end_frame_url
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            # D3 打标：文件名统一带 placeholder_ 前缀，画面叠加「演示占位」提示
            clip_name = f"placeholder_clip_{seed}.mp4"
            out_path = out_dir / clip_name
            out_dir.mkdir(parents=True, exist_ok=True)
            real_duration = max(1, min(int(duration_sec or 5), _MAX_CLIP_SECONDS))
            cmd = [
                ffmpeg, "-y",
                "-f", "lavfi",
                "-i", f"color=c=0x1C2223:s=720x1280:d={real_duration}",
                "-pix_fmt", "yuv420p",
            ]
            drawtext = _placeholder_drawtext_filter("演示占位")
            if drawtext:
                cmd += ["-vf", drawtext]
            cmd.append(str(out_path))
            # 同步子进程放入线程池执行，避免阻塞事件循环；超时按平台失败处理
            proc = await asyncio.to_thread(
                subprocess.run, cmd,
                timeout=_FFMPEG_TIMEOUT,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if proc.returncode == 0 and out_path.exists():
                return {
                    "url": f"{_URL_PREFIX}/{clip_name}",
                    "meta": {"mock": True, "placeholder": True, **trace_meta},
                }
            return None
        # 无 ffmpeg：降级生成 SVG 视频海报（D3 打标：前缀 + 画面显著「演示占位」）
        ts = int(time.time())
        filename = f"placeholder_poster_{seed}_{ts}.svg"
        content = render_placeholder_svg(prompt, seed, label="演示占位·视频")
        _write_svg(out_dir, filename, content)
        return {
            "url": f"{_URL_PREFIX}/{filename}",
            "meta": {"mock": True, "poster": True, "placeholder": True, **trace_meta},
        }
    except Exception:
        return None
