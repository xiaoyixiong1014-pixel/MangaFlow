"""成片合成服务模块（文件级说明）。

由任务执行器（app/tasks/runner.py）在 compose 任务调度时调用：
- 取项目中已生成视频片段（video_url 非空）的分镜，按镜头序号拼接
  （增量合成按任务 meta.storyboard_ids 过滤，Task 5.3）；
- 本机 ffmpeg 可用且片段数 >= 1：concat demuxer 拼接为基础成片；
- 转场（Task 5.3，P-15）：请求携带 transition 且片段数 >= 2 时，改用 xfade
  链重编码实现片段间 fade 转场（其他 type 取值接受但同样按 fade 应用）；
- 后处理一次完成（Task 4.4 水印 + Task 5.1 字幕 + Task 5.2 BGM）：
  - 字幕：build_srt 按分镜对白/画面描述生成 SRT，ffmpeg subtitles 滤镜烧录；
  - 水印：drawtext 将 watermark_id 前 8 位以低透明度角标烧录（隐形溯源）；
  - BGM：amix 混入背景音乐（原片无音轨时 BGM 直接作为唯一音轨）；
  - 同一条 -vf/filter_complex 链按「先字幕、后水印」顺序应用；
  - 任一环节失败或本机无 ffmpeg/字体：逐级降级跳过，不影响成片产出；
- 否则降级为占位成片（直接取首个片段地址；完全无片段则生成 SVG 海报充当成片地址）；
- 每次合成都写入一条 film_outputs 版本记录（版本号递增、is_current 切换、
  附带隐形水印标识 watermark_id）。
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import FilmOutput, Storyboard, Task
from app.providers.image_mock import _URL_PREFIX, _write_svg, render_placeholder_svg

# 成片输出 URL 前缀（storage/outputs 目录经 /storage 挂载对外可访问）
_OUTPUT_URL_PREFIX = "/storage/outputs"
# ffmpeg 拼接子进程超时（秒）
_CONCAT_TIMEOUT = 30
# 后处理（字幕/水印/BGM/转场重编码）子进程超时（秒）
_POST_TIMEOUT = 180

# 字体文件候选（按顺序探测，首个存在者生效）：Windows 微软雅黑/黑体在前，
# Linux DejaVu 兜底；全部缺失时省略 drawtext 水印（降级）
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)

# 字幕位置 → ASS Alignment 对齐值（2 底部居中 / 5 中间居中 / 8 顶部居中）
_SUB_POSITION_ALIGNMENT = {"bottom": 2, "middle": 5, "top": 8}


def _physical_path(url: str) -> Path:
    """把 /storage 前缀的 URL 还原为本地物理路径。

    Args:
        url: 形如 "/storage/generated/clip_1.mp4" 的资源地址。

    Returns:
        Path: 对应的本地绝对路径（storage_dir 拼接）。

    Raises:
        无：纯字符串/路径运算，不产生异常路径。
    """
    relative = url.removeprefix("/storage/").lstrip("/")
    return settings.storage_path() / relative


def _escape_filter_path(path: Path) -> str:
    """转义 ffmpeg 滤镜参数中的文件路径（内部函数）。

    Windows 盘符冒号（C: → C\\:）、单引号需转义后才能安全嵌入滤镜表达式。

    Args:
        path: 本地文件路径。

    Returns:
        str: 转义后的 POSIX 风格路径字符串。

    Raises:
        无：纯字符串处理，不产生异常路径。
    """
    return path.as_posix().replace(":", "\\:").replace("'", "\\'")


def _find_font_file() -> Optional[str]:
    """探测本机可用字体文件（内部函数，水印 drawtext 依赖）。

    Returns:
        Optional[str]: 首个存在的字体文件路径；全部缺失返回 None。

    Raises:
        无。
    """
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def build_drawtext_filter(text: str) -> Optional[str]:
    """构造水印 drawtext 滤镜（Task 4.4 水印真实化）。

    水印语义为隐形溯源角标：低透明度（white@0.15）右上角小字号标识。
    本机无可用字体文件时返回 None（调用方降级跳过水印烧录）。

    Args:
        text: 水印文本（watermark_id 前 8 位）。

    Returns:
        Optional[str]: drawtext 滤镜表达式；无字体时 None。

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
        f"fontsize=18:fontcolor=white@0.15:x=w-tw-20:y=20"
    )


def _srt_timestamp(seconds: float) -> str:
    """把秒数格式化为 SRT 时间戳 HH:MM:SS,mmm（内部函数）。

    Args:
        seconds: 时间（秒，非负）。

    Returns:
        str: SRT 时间戳字符串。

    Raises:
        无：纯数值运算，负数按 0 处理。
    """
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms_part = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms_part:03d}"


def build_srt(storyboards: List) -> Optional[str]:
    """按分镜顺序与时长生成 SRT 字幕文本（Task 5.1，P-16）。

    时间轴按各分镜实际时长（actual_duration，缺省预估时长 duration，
    再缺省 5 秒）顺序累计；字幕行取分镜对白/旁白（dialogue），为空回退
    画面描述（visual_description）；两者皆空的分镜不产生字幕条目。

    Args:
        storyboards: 分镜对象列表（Storyboard 或同字段鸭子类型），按镜头序。

    Returns:
        Optional[str]: 完整 SRT 文本；无任何可用字幕文本时返回 None。

    Raises:
        无：纯文本/数值处理，不产生异常路径。
    """
    entries: List[Tuple[float, float, str]] = []
    cursor = 0.0
    for sb in storyboards:
        duration = float(sb.actual_duration or sb.duration or 5)
        text = ((sb.dialogue or "").strip() or (sb.visual_description or "").strip())
        if text:
            entries.append((cursor, cursor + duration, text))
        cursor += duration
    if not entries:
        return None
    blocks: List[str] = []
    for index, (start, end, text) in enumerate(entries, start=1):
        blocks.append(f"{index}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{text}\n")
    return "\n".join(blocks)


def build_subtitles_filter(srt_path: Path, style: Optional[dict]) -> str:
    """构造 ffmpeg subtitles 烧录滤镜（Task 5.1，内部函数）。

    样式经 force_style 注入：FontSize / PrimaryColour / Alignment；颜色仅接受
    ASS &H... 格式（正则白名单），非法值回退默认白色，防止滤镜注入。

    Args:
        srt_path: SRT 字幕文件本地路径。
        style: 字幕样式 dict（font_size/color/position，可空用默认）。

    Returns:
        str: subtitles 滤镜表达式。

    Raises:
        无：参数非法时逐项回退默认值，不产生异常路径。
    """
    font_size, color, align = 24, "&H00FFFFFF", 2
    if style:
        try:
            font_size = max(10, min(72, int(style.get("font_size", 24))))
        except (TypeError, ValueError):
            font_size = 24
        candidate = str(style.get("color") or "")
        if re_ass_color(candidate):
            color = candidate
        align = _SUB_POSITION_ALIGNMENT.get(style.get("position"), 2)
    return (
        f"subtitles=filename='{_escape_filter_path(srt_path)}':"
        f"force_style='FontSize={font_size},PrimaryColour={color},Alignment={align}'"
    )


def re_ass_color(value: str) -> bool:
    """校验 ASS 颜色值格式（&H 前缀 + 6~8 位十六进制，内部工具）。

    Args:
        value: 待校验颜色字符串。

    Returns:
        bool: 格式合法返回 True。

    Raises:
        无。
    """
    return bool(re.fullmatch(r"&H[0-9A-Fa-f]{6,8}", value or ""))


async def shutil_get_run(cmd: List[str], timeout: int = _CONCAT_TIMEOUT, capture: bool = False):
    """执行外部命令的线程池封装（subprocess.run + 超时）。

    Args:
        cmd: 命令与参数列表。
        timeout: 子进程超时秒数。
        capture: True 时捕获 stdout/stderr（ffprobe 探测用），否则丢弃输出。

    Returns:
        subprocess.CompletedProcess 或 None（超时/异常）。

    Raises:
        无：内部捕获全部异常返回 None。
    """
    import asyncio

    try:
        return await asyncio.to_thread(
            subprocess.run, cmd,
            timeout=timeout,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
        )
    except Exception:
        return None


async def _concat_clips(ffmpeg: str, clip_paths: List[Path], out_path: Path, list_path: Path) -> bool:
    """用 ffmpeg concat demuxer 拼接视频片段（同步子进程放入线程池）。

    Args:
        ffmpeg: ffmpeg 可执行文件绝对路径。
        clip_paths: 片段物理路径列表（按镜头序）。
        out_path: 成片输出路径。
        list_path: concat 清单文件路径（临时文件）。

    Returns:
        bool: 拼接成功返回 True；失败/超时返回 False（调用方降级占位）。

    Raises:
        无：内部捕获全部异常返回 False。
    """
    try:
        list_path.parent.mkdir(parents=True, exist_ok=True)
        lines = "".join(f"file '{p.as_posix()}'\n" for p in clip_paths)
        list_path.write_text(lines, encoding="utf-8")
        cmd = [
            ffmpeg, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            str(out_path),
        ]
        proc = await shutil_get_run(cmd)
        return proc is not None and proc.returncode == 0 and out_path.exists()
    except Exception:
        return False


async def _xfade_clips(
    ffmpeg: str, clip_paths: List[Path], durations: List[float],
    out_path: Path, transition: Dict,
) -> bool:
    """相邻片段 xfade 转场链重编码（Task 5.3，P-15）。

    多级 xfade：offset 按各片段时长累计（offset_k = 前序时长和 - k*转场时长）。
    实现复杂度控制：仅实现 fade 转场——type 参数接受其他 xfade 名但同样按
    fade 应用（验收口径）；转场链只处理视频轨（音频轨丢弃，如需 BGM 由后
    处理环节混入）。片段时长取分镜配置估算值，与真实片段时长有偏差时由
    offset 兜底（clamp 非负）。

    Args:
        ffmpeg: ffmpeg 可执行文件绝对路径。
        clip_paths: 片段物理路径列表（≥2 个）。
        durations: 各片段时长估算（秒，与 clip_paths 一一对应）。
        out_path: 转场产物输出路径。
        transition: 转场参数 dict（type/duration）。

    Returns:
        bool: 转场合成成功返回 True；失败/超时返回 False（调用方回退直拼产物）。

    Raises:
        无：内部捕获全部异常返回 False。
    """
    if len(clip_paths) < 2:
        return False
    try:
        t = min(max(float(transition.get("duration", 0.5) or 0.5), 0.1), 3.0)
    except (TypeError, ValueError):
        t = 0.5
    cmd: List[str] = [ffmpeg, "-y"]
    for path in clip_paths:
        cmd += ["-i", str(path)]
    parts: List[str] = []
    prev_label = "[0:v]"
    acc = 0.0
    for i in range(1, len(clip_paths)):
        acc += float(durations[i - 1] if i - 1 < len(durations) else 5)
        # 第 i 次拼接的转场起点 = 已累积时长 - 转场时长；clamp 非负防止 offset 非法
        offset = max(acc - i * t, 0.05)
        label = f"[v{i}]" if i < len(clip_paths) - 1 else "[vout]"
        parts.append(
            f"{prev_label}[{i}:v]xfade=transition=fade:duration={t:.2f}:offset={offset:.3f}{label}"
        )
        prev_label = f"[v{i}]"
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    proc = await shutil_get_run(cmd, timeout=_POST_TIMEOUT)
    return proc is not None and proc.returncode == 0 and out_path.exists()


async def _has_audio_stream(ffprobe: Optional[str], src: Path) -> bool:
    """探测视频文件是否包含音频轨（内部函数，BGM 混音分支依据）。

    Args:
        ffprobe: ffprobe 可执行文件路径（None 时按无音轨处理）。
        src: 视频文件路径。

    Returns:
        bool: 存在音频流返回 True；ffprobe 不可用/探测失败按无音轨返回 False。

    Raises:
        无：内部捕获全部异常返回 False。
    """
    if not ffprobe:
        return False
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(src),
    ]
    proc = await shutil_get_run(cmd, capture=True)
    return bool(
        proc is not None and proc.returncode == 0
        and proc.stdout and proc.stdout.strip()
    )


async def _postprocess_film(
    ffmpeg: str,
    ffprobe: Optional[str],
    src: Path,
    out_path: Path,
    *,
    subtitles_filter: Optional[str],
    drawtext_filter: Optional[str],
    bgm_path: Optional[Path],
    bgm_volume: float,
) -> bool:
    """合成产物后处理（一次转码完成字幕+水印烧录与 BGM 混音）。

    - 视频滤镜链（先字幕、后水印）与音频混音在同一命令内完成；
    - BGM 无限循环（-stream_loop -1）：有音轨原片按 amix duration=first 截断，
      无音轨原片以 -shortest 随视频结束；
    - 无视频滤镜时 -c:v copy 直拷（仅音频处理不重编码）。

    Args:
        ffmpeg: ffmpeg 可执行文件绝对路径。
        ffprobe: ffprobe 路径（音轨探测，可空）。
        src: 后处理输入（concat/xfade 产物）。
        out_path: 最终成片输出路径。
        subtitles_filter: subtitles 滤镜表达式（无字幕为 None）。
        drawtext_filter: drawtext 滤镜表达式（无字体为 None）。
        bgm_path: BGM 文件路径（不加 BGM 为 None）。
        bgm_volume: BGM 音量系数（0~1）。

    Returns:
        bool: 处理成功返回 True；无需处理/失败/超时返回 False（调用方沿用 src）。

    Raises:
        无：内部捕获全部异常返回 False。
    """
    vf_parts = [f for f in (subtitles_filter, drawtext_filter) if f]
    need_video = bool(vf_parts)
    need_bgm = bgm_path is not None and bgm_path.is_file()
    if not need_video and not need_bgm:
        return False
    try:
        cmd: List[str] = [ffmpeg, "-y", "-i", str(src)]
        if need_bgm:
            # BGM 循环铺底：有音轨按原片时长截断（duration=first），无音轨随视频结束
            cmd += ["-stream_loop", "-1", "-i", str(bgm_path)]
        try:
            volume = f"{max(0.0, min(1.0, float(bgm_volume))):.2f}"
        except (TypeError, ValueError):
            volume = "0.30"
        if need_bgm:
            # 音频子图：有音轨 amix 混入（原声不变），无音轨 BGM 直作唯一音轨
            if await _has_audio_stream(ffprobe, src):
                audio_graph = f"[1:a]volume={volume}[a1];[0:a][a1]amix=inputs=2:duration=first[aout]"
            else:
                audio_graph = f"[1:a]volume={volume}[aout]"
            if need_video:
                # 视频（先字幕、后水印）与音频混音同图完成，视频需重编码
                cmd += [
                    "-filter_complex", f"[0:v]{','.join(vf_parts)}[vout];{audio_graph}",
                    "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "aac", "-shortest",
                ]
            else:
                # 无视频滤镜：视频直拷（filtering 与 streamcopy 不可共存，视频不进子图）
                cmd += [
                    "-filter_complex", audio_graph,
                    "-map", "0:v", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac", "-shortest",
                ]
        else:
            # 仅视频滤镜：单条 -vf 链重编码
            cmd += [
                "-vf", ",".join(vf_parts),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            ]
        cmd.append(str(out_path))
        proc = await shutil_get_run(cmd, timeout=_POST_TIMEOUT)
        return proc is not None and proc.returncode == 0 and out_path.exists()
    except Exception:
        return False


async def _poster_film_url(task: Task, out_dir: Path) -> str:
    """生成一张 SVG 海报充当占位成片地址（项目完全无片段时兜底）。

    Args:
        task: 合成任务（用任务 ID 命名，避免并发覆盖）。
        out_dir: 成片输出目录（storage/outputs 绝对路径）。

    Returns:
        str: 占位成片的 /storage URL。

    Raises:
        无：写盘异常由上层调用方捕获后按平台错误处理。
    """
    filename = f"film_poster_{task.id}.svg"
    content = render_placeholder_svg(f"项目 {task.project_id} 成片占位", task.id, label="成片占位")
    _write_svg(out_dir, filename, content)
    return f"{_OUTPUT_URL_PREFIX}/{filename}"


async def compose_film(db: AsyncSession, task: Task) -> str:
    """执行成片合成并写入 film_outputs 版本记录（compose 任务执行主体）。

    合成流水线：分镜查询（增量过滤）→ concat 直拼 → xfade 转场（可选）→
    后处理（字幕/水印/BGM，可选）→ 版本落库。任一 ffmpeg 环节失败均降级
    到上一级产物（最终回退占位），保证合成总有产出。

    Args:
        db: 异步数据库会话（调用方负责最终提交；本函数内不 commit）。
        task: 合成任务（meta 携带 resolution/aspect_ratio/storyboard_ids/
            subtitles/subtitle_style/bgm/transition）。

    Returns:
        str: 成片资源 URL（/storage/outputs/...，占位场景可能指向片段或 SVG 海报）。

    Raises:
        Exception: 数据库查询异常向上抛出，由执行器按平台错误重试。
    """
    meta = task.meta or {}
    resolution = meta.get("resolution", "720P")
    aspect_ratio = meta.get("aspect_ratio", "9:16")
    # Task 5 合成增强参数（缺省安全值）
    subtitle_on = bool(meta.get("subtitles"))
    subtitle_style = meta.get("subtitle_style") or {}
    bgm_option = meta.get("bgm") or {}
    transition = meta.get("transition") or {}
    wanted_ids = meta.get("storyboard_ids")  # 增量合成过滤清单（Task 5.3）

    rows = await db.execute(
        select(Storyboard)
        .where(Storyboard.project_id == task.project_id, Storyboard.video_url.is_not(None))
        .order_by(Storyboard.sequence)
    )
    storyboards: List[Storyboard] = list(rows.scalars().all())
    # 增量合成：仅保留指定分镜（保持镜头顺序；清单为空/全量模式不过滤）
    if wanted_ids:
        wanted = {int(sid) for sid in wanted_ids}
        storyboards = [sb for sb in storyboards if sb.id in wanted]

    out_dir = settings.storage_path() / "outputs"
    version_no = await _next_version_no(db, task.project_id)
    # 隐形溯源水印标识（R-09）：前 8 位用于画面角标烧录（Task 4.4）
    watermark_id = uuid4().hex[:32]

    film_url: Optional[str] = None
    # 转场应用标记与时长（时长合计扣减用；作用域覆盖降级分支）
    xfade_applied = False
    transition_duration = 0.0
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and storyboards:
        # 片段物理路径（按序）；仅拼接真实存在的本地文件
        pairs = [(sb, _physical_path(sb.video_url)) for sb in storyboards]
        pairs = [(sb, p) for sb, p in pairs if p.exists()]
        clip_paths = [p for _, p in pairs]
        if clip_paths:
            base_path = out_dir / f"{task.project_id}_v{version_no}.mp4"
            list_path = out_dir / f"_concat_{task.id}.txt"
            current: Optional[Path] = (
                base_path if await _concat_clips(ffmpeg, clip_paths, base_path, list_path) else None
            )
            xfade_applied = False
            transition_duration = 0.0
            # 转场（Task 5.3）：≥2 片段时以 xfade 链重编码替代直拼产物；失败回退直拼
            if current and transition and len(clip_paths) >= 2:
                durations = [
                    float(sb.actual_duration or sb.duration or 5) for sb, _ in pairs
                ]
                xfade_path = out_dir / f"{task.project_id}_v{version_no}_xfade.mp4"
                if await _xfade_clips(ffmpeg, clip_paths, durations, xfade_path, transition):
                    current = xfade_path
                    xfade_applied = True
                    try:
                        transition_duration = min(
                            max(float(transition.get("duration", 0.5) or 0.5), 0.1), 3.0
                        )
                    except (TypeError, ValueError):
                        transition_duration = 0.5

            # 字幕（Task 5.1）：SRT 按涉及分镜生成并落盘（写盘失败降级无字幕）
            srt_path: Optional[Path] = None
            if subtitle_on and current:
                srt_content = build_srt(storyboards)
                if srt_content:
                    try:
                        srt_path = out_dir / f"_subs_{task.id}.srt"
                        srt_path.write_text(srt_content, encoding="utf-8")
                    except OSError:
                        srt_path = None

            # BGM（Task 5.2）：/storage URL 还原物理路径（曲库或用户上传素材）
            bgm_path: Optional[Path] = None
            bgm_url = str(bgm_option.get("url") or "")
            if bgm_url.startswith("/storage/") and current:
                candidate = _physical_path(bgm_url)
                if candidate.is_file():
                    bgm_path = candidate
            try:
                bgm_volume = float(bgm_option.get("volume", 0.3) or 0.3)
            except (TypeError, ValueError):
                bgm_volume = 0.3

            # 后处理（Task 4.4 水印 + 5.1 字幕 + 5.2 BGM）：失败沿用 pre 处理产物
            if current:
                final_path = out_dir / f"{task.project_id}_v{version_no}_final.mp4"
                if await _postprocess_film(
                    ffmpeg, ffprobe, current, final_path,
                    subtitles_filter=(
                        build_subtitles_filter(srt_path, subtitle_style) if srt_path else None
                    ),
                    drawtext_filter=build_drawtext_filter(watermark_id[:8]),
                    bgm_path=bgm_path,
                    bgm_volume=bgm_volume,
                ):
                    current = final_path
            if current is not None:
                film_url = f"{_OUTPUT_URL_PREFIX}/{current.name}"
    if film_url is None:
        # 降级占位：有片段则直接引用第一个片段地址；完全无片段则生成 SVG 海报
        if storyboards:
            film_url = storyboards[0].video_url
        else:
            film_url = await _poster_film_url(task, out_dir)

    # 时长合计：优先取配音回写的实际时长（RD-31），缺省用分镜预估时长；
    # 应用 xfade 转场时扣除相邻片段间的转场重叠时长
    total_duration = sum(int(sb.actual_duration or sb.duration or 5) for sb in storyboards)
    if xfade_applied and len(storyboards) >= 2:
        total_duration = max(1, int(total_duration - (len(storyboards) - 1) * transition_duration))

    # 版本切换：历史版本全部退出"当前版本"，新版本置为当前
    existing = (
        await db.execute(select(FilmOutput).where(FilmOutput.project_id == task.project_id))
    ).scalars().all()
    for old in existing:
        old.is_current = False

    film = FilmOutput(
        project_id=task.project_id,
        episode_id=None,
        version_no=version_no,
        url=film_url,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        duration=total_duration,
        watermark_id=watermark_id,
        is_current=True,
    )
    db.add(film)
    await db.flush()
    return film_url


async def _next_version_no(db: AsyncSession, project_id: int) -> int:
    """计算项目下一个成片版本号（现有最大版本号 + 1）。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。

    Returns:
        int: 新版本号（首个版本为 1）。

    Raises:
        无：查询不到历史版本时返回 1。
    """
    current_max = await db.scalar(
        select(FilmOutput.version_no)
        .where(FilmOutput.project_id == project_id)
        .order_by(FilmOutput.version_no.desc())
        .limit(1)
    )
    return (current_max or 0) + 1
