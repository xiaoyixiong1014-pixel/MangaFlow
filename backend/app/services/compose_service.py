"""成片合成服务模块（文件级说明）。

由任务执行器（app/tasks/runner.py）在 compose 任务调度时调用：
- 取项目中已生成视频片段（video_url 非空）的分镜，按镜头序号拼接
  （增量合成按任务 meta.storyboard_ids 过滤，Task 5.3）；
- 本机 ffmpeg 可用且片段数 >= 1：concat demuxer 拼接为基础成片；
- 转场（Task 5.3，P-15）：请求携带 transition 且片段数 >= 2 时，改用 xfade
  链重编码实现片段间转场；转场类型经 map_transition_type 白名单映射为
  xfade 原生滤镜名（P-17：fade/dissolve/fadeblack/fadewhite，其余回退 fade）；
- 后处理一次完成（Task 4.4 水印 + Task 5.1 字幕 + Task 5.2 BGM）：
  - 字幕：build_srt 按分镜对白/画面描述生成 SRT，ffmpeg subtitles 滤镜烧录；
    双语字幕（P-16，subtitle_style.bilingual=true）：先批量调用 LLM 把台词
    翻译为英文，成功生成双语 ASS（原文在上主样式、译文在下副样式——字号略
    小、颜色区分），翻译失败/超时（15s）降级为单行 SRT，绝不阻断合成；
  - 水印：drawtext 将 watermark_id 前 8 位以低透明度角标烧录（隐形溯源）；
  - BGM：amix 混入背景音乐（原片无音轨时 BGM 直接作为唯一音轨）；
  - 同一条 -vf/filter_complex 链按「先字幕、后水印」顺序应用；
  - 任一环节失败或本机无 ffmpeg/字体：逐级降级跳过，不影响成片产出；
- 否则降级为占位成片（直接取首个片段地址；完全无片段时：本机有 ffmpeg 则
  生成 2 秒黑底「演示占位」MP4 充当成片（保证 <video> 可播放，D2），无
  ffmpeg 则抛异常交由执行器置任务 failed，绝不返回假 URL）；
- 每次合成都写入一条 film_outputs 版本记录（版本号递增、is_current 切换、
  附带隐形水印标识 watermark_id）。
"""

import asyncio
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Episode, FilmOutput, Project, Storyboard, Task
from app.providers.llm import chat

logger = logging.getLogger("mangaflow.services.compose_service")

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

# 转场类型白名单映射（P-17）：请求 type（小写归一化）→ ffmpeg xfade 原生滤镜名；
# 不在映射内的取值（如 wipeleft/bogus）一律回退 fade（淡入淡出）
_XFADE_TYPE_MAP = {
    "fade": "fade",
    "dissolve": "dissolve",
    "fadeblack": "fadeblack",
    "fadewhite": "fadewhite",
}


def map_transition_type(type_name: Optional[str]) -> str:
    """把请求的转场类型映射为 ffmpeg xfade 原生滤镜名（P-17）。

    白名单仅支持四种转场：fade（淡入淡出）/ dissolve（叠化）/
    fadeblack（闪黑）/ fadewhite（闪白）；大小写不敏感，前后空白剔除，
    其余/非法/空值一律回退 fade，保证 xfade 滤镜参数恒合法。

    Args:
        type_name: 请求中的转场类型（如 "dissolve"，可空/非法）。

    Returns:
        str: xfade 原生滤镜名（映射失败恒为 "fade"）。

    Raises:
        无：纯字典查询，不产生异常路径。
    """
    return _XFADE_TYPE_MAP.get((type_name or "").strip().lower(), "fade")


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


def build_placeholder_drawtext_filter(text: str) -> Optional[str]:
    """构造占位成片提示文字 drawtext 滤镜（D2，内部函数）。

    与水印角标（build_drawtext_filter）不同：占位提示为高对比度居中显示的
    大字号文字，用于向用户明示当前成片为演示占位。本机无可用字体文件时
    返回 None（调用方降级为无文字纯黑占位视频）。

    Args:
        text: 提示文字（如「演示占位·无可用片段」）。

    Returns:
        Optional[str]: drawtext 滤镜表达式；无字体时 None。

    Raises:
        无：纯字符串构造，不产生异常路径。
    """
    font = _find_font_file()
    if font is None:
        return None
    # 规范化为 POSIX 风格后再转义（与水印滤镜相同的转义规则）
    font_escaped = font.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    text_escaped = text.replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")
    return (
        f"drawtext=fontfile='{font_escaped}':text='{text_escaped}':"
        f"fontsize=42:fontcolor=white@0.85:x=(w-tw)/2:y=(h-th)/2"
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
    已烧分镜字幕的镜（storyboard.audio.burned_subtitles，2026-10-20 分镜
    级字幕）整镜跳过——画面已含字幕，成片再烧会重复。

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
        # 已烧分镜字幕的镜整镜跳过（时间轴仍累计，保证后续镜时段正确）
        if (getattr(sb, "audio", None) or {}).get("burned_subtitles"):
            cursor += duration
            continue
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


def build_subtitles_filter(
    sub_path: Path, style: Optional[dict], video_height: int = 0,
) -> str:
    """构造 ffmpeg subtitles 烧录滤镜（Task 5.1，内部函数）。

    字幕源可为 SRT（单行）或 ASS（P-16 双语）文件；样式经 force_style 注入：
    FontSize / PrimaryColour / Alignment；颜色仅接受 ASS &H... 格式（正则白
    名单），非法值回退默认白色，防止滤镜注入。

    字号语义（2026-10-20 统一）：style.font_size 为「视频画面上的真实像素」。
    SRT 外挂字幕由 libass 按默认脚本高度 288 渲染再放大到视频分辨率，故
    video_height>0 时按 video_height 换算注入值（libass_fontsize =
    real_px * 288 / video_height）；未提供 video_height 时保持原值（兼容
    ASS 路径——ASS 脚本自带 PlayRes，字号已相对 PlayRes 定义）。

    Args:
        sub_path: 字幕文件本地路径（.srt 或 .ass）。
        style: 字幕样式 dict（font_size/color/position，可空用默认）。
        video_height: 视频实际高度（像素，SRT 路径换算用；0/缺省不换算）。

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
    if video_height > 0:
        # 真实像素 → libass 脚本字号（默认 PlayResY=288 渲染基准）
        font_size = max(4, round(font_size * _LIBASS_DEFAULT_PLAYRES_Y / video_height))
    return (
        f"subtitles=filename='{_escape_filter_path(sub_path)}':"
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


# ---------------------------------------------------------------------------
# 分镜级字幕（2026-10-20）：分镜视频烧录对白字幕 + 成片防重复
# ---------------------------------------------------------------------------

# 段级配音的段间静音时长（与 runner._concat_wav_segments 拼接口径一致，
# 字幕时间轴据此推算每段在整镜音轨中的实际起点）
_SUB_SEGMENT_GAP = 0.35
# 段时长缺失时的估算语速（字/秒，兜底不至于时间为 0）
_SUB_FALLBACK_CHARS_PER_SEC = 4.0

# 字幕真实像素规格（2026-10-20 用户确认）：字号语义统一为「视频画面上的
# 实际像素高度」，9:16 竖屏 60~80px（默认 70）、16:9 横屏 40~55px（默认
# 48）。libass 对 SRT 外挂字幕按默认脚本高度 288 渲染再放大到视频实际
# 分辨率（如 720x1280 下 FontSize=24 实际渲染约 107px），因此 force_style
# 注入前必须按 video_height 换算：libass_fontsize = real_px * 288 / video_h
_SUB_REAL_PX_BASE = {"portrait": (60, 80, 70), "landscape": (40, 55, 48)}
# libass 对无样式脚本（SRT）的默认 PlayResY（渲染缩放基准）
_LIBASS_DEFAULT_PLAYRES_Y = 288


def default_subtitle_font_size(width: int, height: int) -> int:
    """按视频画幅计算字幕默认字号（真实像素，公共函数）。

    竖屏（h>w）基准 70px@1280 高等比缩放并钳制 60~80；横屏基准 48px@720
    高等比缩放并钳制 40~55——不同分辨率输出（720p/1080p）下观感一致。

    Args:
        width: 视频宽度（像素）。
        height: 视频高度（像素）。

    Returns:
        int: 默认字号（真实像素）。

    Raises:
        无：参数异常回退竖屏 70 / 横屏 48。
    """
    if width <= 0 or height <= 0:
        return 70
    lo, hi, base = _SUB_REAL_PX_BASE["portrait" if height > width else "landscape"]
    ref_h = 1280 if height > width else 720
    return int(min(hi, max(lo, round(height * base / ref_h))))


async def _probe_resolution(ffprobe: Optional[str], video_path: Path) -> Tuple[int, int, float]:
    """探测视频分辨率与帧率（内部函数，字幕字号换算/拼接归一化基准用）。

    Args:
        ffprobe: ffprobe 可执行文件路径（None 时返回 (0, 0, 0) 走默认字号）。
        video_path: 视频文件路径。

    Returns:
        Tuple[int, int, float]: (width, height, fps)；探测失败返回 (0, 0, 0)。

    Raises:
        无：内部捕获全部异常。
    """
    if not ffprobe:
        return (0, 0, 0.0)
    try:
        proc = await shutil_get_run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate", "-of", "csv=p=0",
             str(video_path)],
            timeout=30, capture=True,
        )
        if proc is None or proc.returncode != 0:
            return (0, 0, 0.0)
        parts = str((proc.stdout or b"").decode("utf-8", "replace")).strip().split(",")
        if len(parts) >= 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
            fps = 0.0
            if len(parts) >= 3 and "/" in parts[2]:
                try:
                    num, den = parts[2].split("/")
                    if float(den) > 0:
                        fps = float(num) / float(den)
                except (TypeError, ValueError):
                    fps = 0.0
            return (int(parts[0]), int(parts[1]), fps)
    except Exception:  # noqa: BLE001 —— 探测失败走默认字号
        pass
    return (0, 0, 0.0)


def _strip_speaker_prefix(line: str) -> str:
    """剥离单行对白的说话人前缀（内部函数，字幕文本口径）。

    与 generation_service._strip_dialogue_speakers 同款正则（「XXX：台词」
    取冒号后台词），字幕不显示说话人名；无前缀行原样保留。

    Args:
        line: 单行对白文本。

    Returns:
        str: 纯台词文本。

    Raises:
        无：纯文本处理，不产生异常路径。
    """
    m = re.match(r"^(?:[^：:]{1,20})[：:](.+)$", line.strip())
    return (m.group(1) if m else line).strip()


# 句级拆分标点（中英文句读；拆出的标点保留在句尾维持语感）
_SUB_SENTENCE_SPLIT_RE = re.compile(r"([，。！？；…,.!?;])")
# 短句粒度（字）：拆句后过短合并、过长硬切——单条字幕观感匹配 70px 字号
# 下 720 宽一行约 10 字的排版
_SUB_CHUNK_MIN = 4
_SUB_CHUNK_MAX = 14
# 句时长分配的去标点字数正则（TTS 朗读速度按音节，标点不计）
_SUB_PUNCT_RE = re.compile(r"[，。！？；…、：,.!?;:\s]")


def _split_subtitle_chunks(
    text: str, max_chars: int = _SUB_CHUNK_MAX, min_chars: int = _SUB_CHUNK_MIN,
) -> List[str]:
    """把一段台词按标点拆成短句序列（内部函数，句级字幕粒度）。

    规则：按标点切句（标点保留句尾）→ 过短句与相邻句合并（≥ min_chars
    且遇句末强标点优先出条）→ 超长无标点片段按 max_chars 硬切 → 尾部碎句
    并入前条。max_chars 由调用方按「视频宽度 / 字号」的单行容纳字数动态
    计算（2026-10-21 用户确认单行显示），保证每条字幕不折行。

    Args:
        text: 单段台词文本（已剥说话人前缀）。
        max_chars: 单条字幕最大字数（单行容纳上限）。
        min_chars: 短句合并下限。

    Returns:
        List[str]: 短句序列（非空）。

    Raises:
        无：纯文本处理，不产生异常路径。
    """
    max_chars = max(2, min_chars + 1 if max_chars <= min_chars else max_chars)
    parts = [p for p in _SUB_SENTENCE_SPLIT_RE.split(text) if p]
    sentences: List[str] = []
    for i in range(0, len(parts) - 1, 2):
        s = (parts[i] + parts[i + 1]).strip()
        if s:
            sentences.append(s)
    if len(parts) % 2 == 1 and parts[-1].strip():
        sentences.append(parts[-1].strip())
    if not sentences:
        return [text.strip()] if text.strip() else []

    chunks: List[str] = []
    buf = ""
    for s in sentences:
        cand = buf + s if buf else s
        if len(cand) <= max_chars:
            buf = cand
            # 达到最短长度且本句以句末强标点收尾：先出条（贴合说话节奏）
            if len(buf) >= min_chars and s[-1] in "。！？!?；;…":
                chunks.append(buf)
                buf = ""
        else:
            if buf:
                chunks.append(buf)
                buf = ""
            while len(s) > max_chars:
                chunks.append(s[:max_chars])
                s = s[max_chars:]
            buf = s
    if buf:
        if chunks and len(buf) < min_chars:
            chunks[-1] += buf  # 尾部碎句并入前条
        else:
            chunks.append(buf)
    return chunks


def _chunk_weight(text: str) -> int:
    """短句时长权重：去标点后的有效字数（内部函数）。

    TTS 朗读时长与音节数正相关，标点仅产生停顿（已含在段间/句间分配的
    时间里），权重按去标点字数计算使句级时间轴更贴合实际说话速度。

    Args:
        text: 短句文本。

    Returns:
        int: 有效字数（≥1，防除零）。

    Raises:
        无：纯文本处理。
    """
    return max(1, len(_SUB_PUNCT_RE.sub("", text)))


def build_storyboard_subtitle_segments(
    storyboard, max_chars: int = _SUB_CHUNK_MAX, min_chars: int = _SUB_CHUNK_MIN,
) -> Optional[List[Dict]]:
    """构建分镜视频的对白字幕时间轴（公共函数，供 runner 烧录调用）。

    句级粒度（2026-10-20 用户确认「逐句按说话速度对齐出现」）：段级真实
    时长（TTS 实测 duration_sec）内按短句去标点字数占比线性切分时间轴，
    长台词逐句出现而非整段铺满——TTS 语速均匀，字数比例即真实节奏。
    max_chars 传「视频宽度 / 字号」的单行容纳字数（2026-10-21 单行显示）
    可保证每条字幕不折行。时间轴数据源优先级：
    - 段级配音产物（storyboard.audio.dialogue，2026-10-20 段级模式）：逐段
      duration_sec 精确累计，段间补 0.35s 静音间隔（与整镜音轨拼接口径一致）；
    - 回退：dialogue 逐行剥离说话人前缀后按行均分整镜时长（旧整镜模式无
      段级时长，均分近似）。

    Args:
        storyboard: 分镜 ORM 对象（audio/dialogue/actual_duration/duration）。
        max_chars: 单条字幕最大字数（单行容纳上限，可空用默认 14）。
        min_chars: 短句合并下限（可空用默认 4）。

    Returns:
        Optional[List[Dict]]: [{"text": 短句, "start": 起始秒, "end": 结束秒}]；
            无任何对白文本返回 None（无对白镜不烧字幕）。

    Raises:
        无：数据异常逐段跳过，不产生异常路径。
    """
    audio = getattr(storyboard, "audio", None) or {}
    segments: List[Dict] = []
    dlg = [
        d for d in (audio.get("dialogue") or [])
        if isinstance(d, dict) and str(d.get("text") or "").strip()
    ]
    if dlg:
        # 段级精确时间轴：段时长累计 + 段间 0.35s 静音；段内按短句字数占比切分
        cursor = 0.0
        for d in dlg:
            seg_text = str(d.get("text")).strip()
            try:
                dur = float(d.get("duration_sec") or 0)
            except (TypeError, ValueError):
                dur = 0.0
            if dur <= 0:
                dur = max(0.8, len(seg_text) / _SUB_FALLBACK_CHARS_PER_SEC)
            chunks = _split_subtitle_chunks(seg_text, max_chars, min_chars)
            weights = [_chunk_weight(c) for c in chunks]
            total_w = sum(weights)
            seg_start = cursor
            acc = 0.0
            for c, w in zip(chunks, weights):
                cdur = dur * w / total_w
                segments.append({
                    "text": c,
                    "start": round(seg_start + acc, 3),
                    "end": round(seg_start + acc + cdur, 3),
                })
                acc += cdur
            cursor = seg_start + dur + _SUB_SEGMENT_GAP
    else:
        # 回退：逐行剥说话人前缀，按行均分整镜时长（行内再按短句占比切分）
        lines = [
            _strip_speaker_prefix(line)
            for line in str(storyboard.dialogue or "").splitlines()
        ]
        lines = [x for x in lines if x]
        if not lines:
            return None
        try:
            total = float(getattr(storyboard, "actual_duration", None)
                          or getattr(storyboard, "duration", None) or 5)
        except (TypeError, ValueError):
            total = 5.0
        total = max(1.0, total)
        per = total / len(lines)
        for i, line in enumerate(lines):
            chunks = _split_subtitle_chunks(line, max_chars, min_chars)
            weights = [_chunk_weight(c) for c in chunks]
            total_w = sum(weights)
            seg_start = i * per
            acc = 0.0
            for c, w in zip(chunks, weights):
                cdur = per * w / total_w
                segments.append({
                    "text": c,
                    "start": round(seg_start + acc, 3),
                    "end": round(seg_start + acc + cdur, 3),
                })
                acc += cdur
    return segments or None


async def burn_storyboard_subtitles(
    ffmpeg: str,
    ffprobe: Optional[str],
    video_path: Path,
    storyboard,
    out_path: Path,
    style: Optional[dict] = None,
) -> bool:
    """把对白字幕烧录进分镜视频（公共函数，供 runner 口型同步后调用）。

    流程：探测视频分辨率 → 按画幅定字幕真实字号（style.font_size 优先）
    → 按单行容纳字数（宽度/字号，留 10% 余量）句级切分时间轴（2026-10-21
    用户确认单行显示，保证每条字幕不折行）→ SRT 临时文件 → subtitles 滤镜
    重编码（libx264 crf 20，音轨 -c:a copy）→ 清理临时文件。

    Args:
        ffmpeg: ffmpeg 可执行文件路径。
        ffprobe: ffprobe 可执行文件路径（分辨率探测，None 时走默认竖屏字号）。
        video_path: 输入分镜视频（已含配音音轨）。
        storyboard: 分镜 ORM 对象（时间轴数据源：audio.dialogue/dialogue）。
        out_path: 输出视频路径（不得与 video_path 相同，调用方再 replace）。
        style: 字幕样式 dict（font_size 为真实像素/color/position，可空用默认）。

    Returns:
        bool: 成功 True；无对白/失败 False（调用方保留无字幕产物降级）。

    Raises:
        无：内部捕获全部异常收敛为 False。
    """
    # 分辨率探测 → 字号（真实像素）→ 单行容纳字数（宽度/字号，留 10% 余量）
    v_w, v_h, _fps = await _probe_resolution(ffprobe, video_path)
    burn_style = dict(style or {})
    if not burn_style.get("font_size"):
        burn_style["font_size"] = default_subtitle_font_size(v_w, v_h)
    font_px = int(burn_style["font_size"])
    line_chars = (
        max(4, int(v_w / font_px * 0.9)) if v_w > 0 else _SUB_CHUNK_MAX
    )
    segments = build_storyboard_subtitle_segments(
        storyboard, max_chars=line_chars, min_chars=min(4, max(2, line_chars - 1)),
    )
    if not segments:
        return False
    srt_path = video_path.parent / f"_sb_sub_{video_path.stem}.srt"
    try:
        blocks: List[str] = []
        for index, seg in enumerate(segments, start=1):
            blocks.append(
                f"{index}\n{_srt_timestamp(float(seg['start']))} --> "
                f"{_srt_timestamp(float(seg['end']))}\n{str(seg['text'])}\n"
            )
        srt_path.write_text("\n".join(blocks), encoding="utf-8")
        vf = build_subtitles_filter(srt_path, burn_style, v_h or 1280)
        cmd = [
            ffmpeg, "-y", "-v", "error", "-i", str(video_path),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            str(out_path),
        ]
        proc = await shutil_get_run(cmd, timeout=_POST_TIMEOUT)
        return bool(
            proc is not None and proc.returncode == 0 and out_path.is_file()
        )
    except Exception:  # noqa: BLE001 —— 字幕烧录失败降级不阻断
        logger.warning("分镜字幕烧录异常：%s", video_path.name, exc_info=True)
        return False
    finally:
        try:
            srt_path.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 双语字幕（P-16）：LLM 批量翻译 + 双样式 ASS 生成 + 失败降级单行 SRT
# ---------------------------------------------------------------------------

# 双语翻译单次 LLM 调用超时（秒）：超时/失败一律降级，不阻断合成
_TRANSLATE_TIMEOUT = 15
# 双语副样式（译文行）颜色：中灰（ASS &HBBGGRR），与主行白色形成区分
_SUB_STYLE_COLOR = "&H00B4B4B4"


def _ass_timestamp(seconds: float) -> str:
    """把秒数格式化为 ASS 时间戳 H:MM:SS.cc（厘秒，内部函数）。

    Args:
        seconds: 时间（秒，非负）。

    Returns:
        str: ASS 时间戳字符串（如 "0:00:05.50"）。

    Raises:
        无：纯数值运算，负数按 0 处理。
    """
    cs = max(0, int(round(seconds * 100)))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs_part = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs_part:02d}"


def _ass_escape(text: str) -> str:
    """转义 ASS Dialogue 文本（内部函数）。

    去除会被 libass 解析为覆盖标签的大括号（替换为全角括号）、压缩行内
    空白、把真实换行替换为 \\N 软换行，防止字幕原文破坏 ASS 事件结构。

    Args:
        text: 原始字幕文本（单行/多行）。

    Returns:
        str: 转义后可安全嵌入 ASS 的文本（空文本以 "-" 占位）。

    Raises:
        无：纯字符串处理，不产生异常路径。
    """
    cleaned = (text or "").replace("{", "（").replace("}", "）")
    lines = [" ".join(line.split()) for line in cleaned.splitlines()]
    return "\\N".join(line for line in lines if line) or "-"


def _subtitle_texts(storyboards: List) -> List[str]:
    """按 build_srt 相同规则收集各分镜字幕原文（内部函数，P-16 双语用）。

    取文规则与 build_srt 完全一致（对白优先、画面描述回退、皆空跳过），
    保证双语翻译列表与字幕条目一一对应。

    Args:
        storyboards: 分镜对象列表（Storyboard 或同字段鸭子类型），按镜头序。

    Returns:
        List[str]: 有字幕文本的分镜原文列表（与 build_srt 条目同序同数量）。

    Raises:
        无：纯文本处理，不产生异常路径。
    """
    texts: List[str] = []
    for sb in storyboards:
        # 已烧分镜字幕的镜整镜跳过（与 build_srt 过滤口径一致）
        if (getattr(sb, "audio", None) or {}).get("burned_subtitles"):
            continue
        text = ((sb.dialogue or "").strip() or (sb.visual_description or "").strip())
        if text:
            texts.append(text)
    return texts


async def _translate_bilingual_lines(texts: List[str]) -> Optional[List[Optional[str]]]:
    """批量把字幕原文翻译为英文（P-16 双语字幕，内部函数）。

    一次 LLM 调用传入全部台词列表（json_mode），要求返回等长 JSON 数组；
    15s 超时 / 全渠道失败 / JSON 解析失败 / 结构非法时返回 None（调用方整
    体降级单行）；返回数组长度不足时缺失位以 None 填充（该镜降级单行）。

    Args:
        texts: 字幕原文列表（与 build_srt 条目同序）。

    Returns:
        Optional[List[Optional[str]]]: 与 texts 等长的译文列表（缺失位 None）；
            翻译整体失败返回 None。

    Raises:
        无：内部捕获全部异常（超时/LLM/解析）返回 None，绝不向上抛出。
    """
    if not texts:
        return None
    prompt = (
        "你是漫画短剧字幕翻译。请把下面 JSON 数组中的每条中文字幕逐条翻译为"
        "简洁的英文口语（保持数组顺序与条数一致，不要合并或拆分条目），"
        "仅返回 JSON 数组，不要任何解释。中文字幕数组：\n"
        + json.dumps(texts, ensure_ascii=False)
    )
    try:
        raw = await asyncio.wait_for(
            chat([{"role": "user", "content": prompt}], temperature=0.2, json_mode=True),
            timeout=_TRANSLATE_TIMEOUT,
        )
        parsed = json.loads(raw)
        # 兼容 {"translations": [...]} 包装：取首个数组值
        if isinstance(parsed, dict):
            parsed = next((v for v in parsed.values() if isinstance(v, list)), None)
        if not isinstance(parsed, list):
            return None
        translated: List[Optional[str]] = []
        for i in range(len(texts)):
            item = parsed[i] if i < len(parsed) else None
            item = str(item).strip() if item is not None else ""
            translated.append(item or None)
        return translated
    except Exception:  # noqa: BLE001 —— 翻译是增强能力，任何失败都降级单行
        return None


def build_ass_subtitles(
    storyboards: List,
    translations: Optional[List[Optional[str]]],
    style: Optional[dict],
    aspect_ratio: Optional[str] = None,
) -> Optional[str]:
    """生成双语 ASS 字幕文件内容（P-16，内部函数）。

    时间轴与 build_srt 一致（按分镜实际时长累计）。脚本 PlayRes 按画幅
    （2026-10-20 字号真实像素语义：9:16 竖 720x1280 / 16:9 横 1280x720 /
    1:1 方 1080x1080），style.font_size 即画面真实像素：缺省按画幅取规格
    默认（竖 70 / 横 48 / 方 60），与分镜烧录字幕观感一致。双样式定义：
    - Style Main（原文在上）：主字号/主颜色，底部位置时 MarginV 抬高为译文行留空；
    - Style Sub（译文在下）：字号为主样式 0.7 倍（下限 10）、灰色区分，
      贴近画面底部（MarginV 12）。
    某条目无译文（translations 缺失位）时仅生成 Main 单行（逐条降级）。

    Args:
        storyboards: 分镜对象列表（按镜头序）。
        translations: 与 _subtitle_texts(storyboards) 等长的译文列表
            （缺失位 None 单行显示）；None 表示全部单行。
        style: 字幕样式 dict（font_size/color/position，font_size 为真实像素）。
        aspect_ratio: 项目画幅（"9:16"/"16:9"/"1:1"，决定 PlayRes 与默认字号）。

    Returns:
        Optional[str]: 完整 ASS 文本；无任何可用字幕文本时返回 None。

    Raises:
        无：纯文本构造，不产生异常路径。
    """
    entries: List[Tuple[float, float, str]] = []
    cursor = 0.0
    for sb in storyboards:
        duration = float(sb.actual_duration or sb.duration or 5)
        # 已烧分镜字幕的镜整镜跳过（与 build_srt 过滤口径一致，防双字幕）
        if (getattr(sb, "audio", None) or {}).get("burned_subtitles"):
            cursor += duration
            continue
        text = ((sb.dialogue or "").strip() or (sb.visual_description or "").strip())
        if text:
            entries.append((cursor, cursor + duration, text))
        cursor += duration
    if not entries:
        return None

    # PlayRes 按画幅（字号真实像素语义的渲染基准）
    ar = str(aspect_ratio or "")
    if ar == "16:9":
        play_w, play_h = 1280, 720
    elif ar == "1:1":
        play_w, play_h = 1080, 1080
    else:
        play_w, play_h = 720, 1280

    try:
        font_size = int((style or {}).get("font_size") or 0)
    except (TypeError, ValueError):
        font_size = 0
    if font_size <= 0:
        font_size = default_subtitle_font_size(play_w, play_h)
    font_size = max(10, min(120, font_size))
    main_color = str((style or {}).get("color") or "")
    if not re_ass_color(main_color):
        main_color = "&H00FFFFFF"
    position = (style or {}).get("position")
    main_align = _SUB_POSITION_ALIGNMENT.get(position, 2)
    # 主行 MarginV：底部时抬高为译文行留空；居中/顶部用默认值
    main_margin_v = 45 if main_align == 2 else 0
    sub_font_size = max(10, int(font_size * 0.7))
    sub_margin_v = 12

    header = (
        "[Script Info]\n"
        "; MangaFlow P-16 bilingual subtitles (auto-generated)\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_w}\n"
        f"PlayResY: {play_h}\n"
        "WrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Main,Microsoft YaHei,{font_size},{main_color},&H000000FF,"
        f"&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2,1,{main_align},"
        f"20,20,{main_margin_v},1\n"
        f"Style: Sub,Microsoft YaHei,{sub_font_size},{_SUB_STYLE_COLOR},&H000000FF,"
        f"&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2,1,2,20,20,{sub_margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines: List[str] = []
    for index, (start, end, text) in enumerate(entries):
        ts_start, ts_end = _ass_timestamp(start), _ass_timestamp(end)
        lines.append(f"Dialogue: 0,{ts_start},{ts_end},Main,,0,0,0,,{_ass_escape(text)}")
        trans = None
        if translations is not None and index < len(translations):
            trans = translations[index]
        if trans:
            lines.append(f"Dialogue: 0,{ts_start},{ts_end},Sub,,0,0,0,,{_ass_escape(str(trans))}")
    return header + "\n".join(lines) + "\n"


def _write_subtitle_file(path: Path, content: str) -> Optional[Path]:
    """把字幕内容写盘（内部函数）。

    Args:
        path: 字幕文件目标路径（.srt 或 .ass）。
        content: 字幕文件完整文本。

    Returns:
        Optional[Path]: 写盘成功返回路径；OSError（目录不可写等）返回 None。

    Raises:
        无：内部捕获 OSError 返回 None。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path
    except OSError:
        return None


async def _prepare_subtitle_file(
    storyboards: List, subtitle_style: Dict, task_id: int, out_dir: Path,
    aspect_ratio: Optional[str] = None,
) -> Optional[Path]:
    """按字幕样式准备待烧录字幕文件（内部函数，P-16 双语支持）。

    流程：
    - bilingual 为真：收集字幕原文 → LLM 批量翻译（15s 超时）→ 成功则生成
      双语 ASS 文件（.ass）；翻译失败/无译文 → 降级 SRT 单行；
    - bilingual 为假：直接生成 SRT 单行；
    - 无任何可用字幕文本 / 写盘失败：返回 None（调用方降级为无字幕）。

    Args:
        storyboards: 涉及分镜列表（按镜头序）。
        subtitle_style: 字幕样式 dict（font_size/color/position/bilingual，
            font_size 为真实像素）。
        task_id: 任务 ID（字幕临时文件命名，避免并发覆盖）。
        out_dir: 字幕文件输出目录（storage/outputs）。
        aspect_ratio: 项目画幅（ASS PlayRes 与默认字号基准，可空默认竖屏）。

    Returns:
        Optional[Path]: 字幕文件路径（.ass 或 .srt）；无可字幕文本/写盘失败为 None。

    Raises:
        无：内部捕获全部异常（翻译/写盘）返回 None，绝不阻断合成任务。
    """
    if subtitle_style.get("bilingual"):
        texts = _subtitle_texts(storyboards)
        if texts:
            translations = await _translate_bilingual_lines(texts)
            if translations:
                ass_content = build_ass_subtitles(
                    storyboards, translations, subtitle_style, aspect_ratio,
                )
                if ass_content:
                    return _write_subtitle_file(out_dir / f"_subs_{task_id}.ass", ass_content)
        # 双语任一环节失败：降级 SRT 单行
    srt_content = build_srt(storyboards)
    if not srt_content:
        return None
    return _write_subtitle_file(out_dir / f"_subs_{task_id}.srt", srt_content)


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
            # 生成缺失时间戳：各片段音视频轨已在 _merge_clip_audio 严格等长
            # 归一化，genpts 兜底个别片段 pts 缺失导致的接缝漂移
            "-fflags", "+genpts",
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
    """相邻片段 xfade 转场链重编码（Task 5.3，P-15/P-17；2026-10-21 解除配音互斥）。

    多级 xfade + acrossfade 双链：视频轨 xfade（offset_k = 前序时长和 -
    k*转场时长），音频轨 acrossfade 平行链（交叉段=上一合成段尾 t 秒，与
    视频 xfade 的转场起点天然对齐）——有配音的片段也保留音轨（2026-10-21
    用户确认「有配音也要有转场」）。转场类型经 map_transition_type 白名单
    映射（fade/dissolve/fadeblack/fadewhite 直通，其余回退 fade）。片段时长
    由调用方传真实探测值，估算偏差时 offset clamp 非负兜底。前提：片段均经
    _merge_clip_audio 归一化（恒有 44.1kHz 音轨、同分辨率/帧率）。

    Args:
        ffmpeg: ffmpeg 可执行文件绝对路径。
        clip_paths: 片段物理路径列表（≥2 个）。
        durations: 各片段真实时长（秒，与 clip_paths 一一对应）。
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
    # P-17：转场类型白名单映射（非法值回退 fade），整条 xfade 链统一应用同名转场
    xfade_type = map_transition_type(str(transition.get("type") or ""))
    parts: List[str] = []
    prev_v, prev_a = "[0:v]", "[0:a]"
    acc = 0.0
    n = len(clip_paths)
    for i in range(1, n):
        try:
            acc += float(durations[i - 1] if i - 1 < len(durations) else 5)
        except (TypeError, ValueError):
            acc += 5.0
        # 第 i 次拼接的转场起点 = 已累积时长 - 转场时长；clamp 非负防止 offset 非法
        offset = max(acc - i * t, 0.05)
        is_last = i == n - 1
        v_label = "[vout]" if is_last else f"[v{i}]"
        a_label = "[aout]" if is_last else f"[a{i}]"
        parts.append(
            f"{prev_v}[{i}:v]xfade=transition={xfade_type}:duration={t:.2f}:offset={offset:.3f}{v_label}"
        )
        # 音轨平行交叉淡化：acrossfade 交叉段=prev 尾 t 秒 ↔ 当前片段头 t 秒，
        # 与视频 xfade 的 [offset, offset+t] 同位（画面与声音同步过渡）
        parts.append(f"{prev_a}[{i}:a]acrossfade=d={t:.2f}{a_label}")
        prev_v, prev_a = v_label, a_label
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
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


async def _probe_duration(ffprobe: Optional[str], src: Path) -> Optional[float]:
    """探测媒体文件时长（秒，内部函数，音画对齐补齐策略依据）。

    Args:
        ffprobe: ffprobe 可执行文件路径（None 时返回 None）。
        src: 媒体文件路径（视频或音频）。

    Returns:
        Optional[float]: 时长（秒）；ffprobe 不可用/探测失败/解析异常返回 None。

    Raises:
        无：内部捕获全部异常返回 None。
    """
    if not ffprobe:
        return None
    cmd = [
        ffprobe, "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(src),
    ]
    proc = await shutil_get_run(cmd, capture=True)
    if proc is None or proc.returncode != 0 or not proc.stdout:
        return None
    try:
        dur = float(proc.stdout.strip())
    except (TypeError, ValueError):
        return None
    return dur if dur > 0 else None


async def _merge_clip_audio(
    ffmpeg: str,
    ffprobe: Optional[str],
    video_path: Path,
    audio_path: Optional[Path],
    out_path: Path,
    env_audio_path: Optional[Path] = None,
    target_size: Optional[Tuple[int, int]] = None,
    target_fps: Optional[float] = None,
) -> Optional[Path]:
    """把分镜配音合入视频片段（音频先行音画对齐，2026-10-19 流程确认）。

    对齐策略（输出时长 = max(视频, 配音)）：
    - 配音长于视频：视频末帧定格补齐（tpad stop_mode=clone）；
    - 配音短于视频：视频保持完整，音频补静音至视频末（apad whole_dur）；
    - 视频自带音轨（如 wan 输出含环境音）：环境音降音量 0.35 与配音 amix；
    - 显式环境音源（env_audio_path 非空，2026-10-20 音源分离方案）：调用方
      已剥除视频原生音轨（人声+环境音混合体），以分离出的无人声环境音轨
      0.35 与配音 1.0 amix——人声不进最终产物；
    - 无配音但视频自带音轨（2026-10-20 口径「自带音轨⟺已含配音」，跳过
      混音防双声）：**原音轨保留**（2026-10-21 修复：旧实现该场景走
      anullsrc 静音分支导致成片整段无声）；
    - 真无声片段（无配音且视频无原生音轨）：补静音音轨（anullsrc），统一
      「片段恒有 AAC 音轨 + 同参编码」，保证 concat -c copy 拼接兼容与
      音轨连续。

    拼接归一化（2026-10-21 修复成片音画漂移）：target_size/target_fps 提供
    时，视频轨统一 scale/fps 到基准规格；所有分支输出音轨统一经
    aresample=44100 + apad/atrim 钳制到 total 秒——各镜 mp4 音视频轨严格
    等长、同规格，消除 concat -c copy 逐镜累积的时间轴漂移（配音快于画面/
    口型字幕错位的根因）。

    任一失败返回 None（调用方回退原片段——音画对齐降级不阻断合成）。

    Args:
        ffmpeg: ffmpeg 可执行文件路径。
        ffprobe: ffprobe 可执行文件路径（探测时长用，None 时无法对齐返回 None）。
        video_path: 视频片段物理路径。
        audio_path: 配音音频物理路径（None = 补静音轨仅做编码统一）。
        out_path: 输出片段路径。
        env_audio_path: 显式环境音源 WAV 路径（音源分离产物，可空）。非空时
            优先于视频原生音轨作为环境音源（要求配音 audio_path 同时存在）。
        target_size: 归一化目标分辨率 (w, h)（拼接基准，可空不缩放）。
        target_fps: 归一化目标帧率（可空不调整）。

    Returns:
        Optional[Path]: 成功返回输出路径；失败返回 None。

    Raises:
        无：内部捕获全部异常返回 None。
    """
    if not ffprobe:
        return None
    try:
        video_dur = await _probe_duration(ffprobe, video_path)
        if video_dur is None:
            return None
        audio_dur = (
            await _probe_duration(ffprobe, audio_path) if audio_path else None
        )
        total = max(video_dur, audio_dur or 0.0)
        pad = max(0.0, total - video_dur)
        has_native_audio = await _has_audio_stream(ffprobe, video_path)
        # 输入装配：[0]=视频；显式环境音源时 [1]=环境音 [2]=配音（调用方
        # 已剥除原生音轨）；否则 [1]=配音；静音源仅在「真无声镜」（无配音
        # 且视频无原生音轨）时注入——2026-10-21 修复成片无声：跳过混音
        # 场景（视频自带配音音轨）曾被 anullsrc 静音轨整轨替换
        cmd: List[str] = [ffmpeg, "-y", "-i", str(video_path)]
        use_explicit_env = bool(
            env_audio_path and audio_path and Path(env_audio_path).is_file()
        )
        if use_explicit_env:
            cmd += ["-i", str(env_audio_path), "-i", str(audio_path)]
            env_in, audio_in = "[1:a]", "[2:a]"
        elif audio_path:
            cmd += ["-i", str(audio_path)]
            audio_in = "[1:a]"
        elif has_native_audio:
            audio_in = ""  # 原音轨保留，无需外部音频输入
        else:
            # 静音源（与配音同采样率，保证 concat 音轨参数一致）
            cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
            audio_in = "[1:a]"
        # 视频滤镜链：末帧定格补齐 → 归一化分辨率/帧率（拼接同构，防漂移）
        vf_parts = [f"tpad=stop_mode=clone:stop_duration={pad:.3f}"]
        if target_size and target_size[0] > 0 and target_size[1] > 0:
            vf_parts.append(f"scale={target_size[0]}:{target_size[1]}")
        if target_fps and target_fps > 0:
            vf_parts.append(f"fps={target_fps:.3f}")
        vf = f"[0:v]{','.join(vf_parts)}[v]"
        # 音频子图：各分支产出 [mix]（内容来源不同），出口统一钳制到
        # 「44.1kHz + 精确 total 秒」——apad 补齐不足、atrim 截掉超出，
        # 保证每镜音视频轨严格等长（concat -c copy 无累积漂移）
        audio_map = "[a]"
        if use_explicit_env:
            mix_chain = (
                f"{env_in}volume=0.35[env];{audio_in}volume=1.0[dub];"
                f"[env][dub]amix=inputs=2:duration=longest:dropout_transition=0[mix]"
            )
        elif audio_path and has_native_audio:
            # 片段自带音轨：环境音降量后与配音混音（配音优先）
            mix_chain = (
                f"[0:a]volume=0.35[env];{audio_in}volume=1.0[dub];"
                f"[env][dub]amix=inputs=2:duration=longest:dropout_transition=0[mix]"
            )
        elif audio_path:
            mix_chain = f"{audio_in}anull[mix]"
        elif has_native_audio:
            # 跳过混音场景（视频自带配音音轨，2026-10-20 口径「自带音轨⟺已含
            # 配音」）：原音轨保留——曾被 anullsrc 静音轨整轨替换导致成片无声
            mix_chain = "[0:a]anull[mix]"
        else:
            # 真无声镜（无配音且视频无原生音轨）：补静音轨保证 concat 兼容
            mix_chain = f"{audio_in}anull[mix]"
        af = (
            f"{mix_chain};"
            f"[mix]aresample=44100,apad=whole_dur={total:.3f},"
            f"atrim=end={total:.3f},asetpts=N/SR/TB[a]"
        )
        cmd += [
            "-filter_complex", f"{vf};{af}",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-t", f"{total:.3f}",
            str(out_path),
        ]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        proc = await shutil_get_run(cmd, timeout=_POST_TIMEOUT)
        if proc is None or proc.returncode != 0 or not out_path.exists():
            return None
        return out_path
    except Exception:
        return None


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
    bgm_ducking: bool = True,
) -> bool:
    """合成产物后处理（一次转码完成字幕+水印烧录与 BGM 混音）。

    - 视频滤镜链（先字幕、后水印）与音频混音在同一命令内完成；
    - BGM 无限循环（-stream_loop -1）：有音轨原片按 amix duration=first 截断，
      无音轨原片以 -shortest 随视频结束；
    - BGM 自动闪避（2026-10-21 成片整合，bgm_ducking=True）：原声 asplit 两路
      ——一路直出混音、一路作 sidechaincompress 的触发源，对白响起时 BGM
      自动压低（threshold=0.02 / ratio=8 / attack=120ms / release=900ms），
      间隙恢复基础音量，解决 BGM 盖配音/间隙太空的听感问题；
    - 无视频滤镜时 -c:v copy 直拷（仅音频处理不重编码）。

    Args:
        ffmpeg: ffmpeg 可执行文件绝对路径。
        ffprobe: ffprobe 路径（音轨探测，可空）。
        src: 后处理输入（concat/xfade 产物）。
        out_path: 最终成片输出路径。
        subtitles_filter: subtitles 滤镜表达式（无字幕为 None）。
        drawtext_filter: drawtext 滤镜表达式（无字体为 None）。
        bgm_path: BGM 文件路径（不加 BGM 为 None）。
        bgm_volume: BGM 音量系数（0~1，闪避时的基础音量）。
        bgm_ducking: 是否启用 BGM 对白闪避（缺省开启）。

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
            # 音频子图：有音轨时 BGM 对白闪避混入（原声不变），无音轨 BGM 直作唯一音轨
            if await _has_audio_stream(ffprobe, src):
                if bgm_ducking:
                    # sidechaincompress：main=BGM（被压），sidechain=原片配音/环境音
                    # （asplit 出一路作触发源），对白响 → BGM 压低，间隙 → 恢复基础音量
                    audio_graph = (
                        f"[0:a]asplit=2[vo][sc];"
                        f"[1:a]volume={volume}[bgm];"
                        f"[bgm][sc]sidechaincompress=threshold=0.02:ratio=8:"
                        f"attack=120:release=900[ducked];"
                        f"[vo][ducked]amix=inputs=2:duration=first[aout]"
                    )
                else:
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
            # 仅视频滤镜：单条 -vf 链重编码（音频参数同步统一，防采样率漂移）
            cmd += [
                "-vf", ",".join(vf_parts),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            ]
        cmd.append(str(out_path))
        proc = await shutil_get_run(cmd, timeout=_POST_TIMEOUT)
        return proc is not None and proc.returncode == 0 and out_path.exists()
    except Exception:
        return False


async def _build_text_card(
    ffmpeg: str,
    out_path: Path,
    width: int,
    height: int,
    lines: List[Tuple[str, float, float]],
    duration: float = 2.5,
    fps: float = 24.0,
) -> Optional[Path]:
    """生成黑底文字标题卡片段（片头/片尾卡，内部函数，2026-10-21 成片整合）。

    lavfi 黑色视频源 + 逐行 drawtext（白字居中，字号/纵向位置按高度比例）
    + 首尾 0.5s 淡入淡出 + 静音音轨（anullsrc，与分镜片段 AAC 44.1kHz 同参
    + 帧率与成片一致），保证 concat -c copy / xfade 链编码兼容。本机无可用
    中文字体时返回 None（调用方降级为不插卡，不阻断合成）。

    Args:
        ffmpeg: ffmpeg 可执行文件路径。
        out_path: 输出片段路径（storage/outputs 下）。
        width: 卡片宽度（与成片片段一致）。
        height: 卡片高度（与成片片段一致）。
        lines: 文字行列表 (文本, 字号高度比, 纵向位置高度比)。
        duration: 卡片时长（秒，片头 2.5 / 片尾 2.0）。
        fps: 卡片帧率（与成片片段一致）。

    Returns:
        Optional[Path]: 成功返回输出路径；失败/无字体返回 None。

    Raises:
        无：内部捕获全部异常收敛为 None。
    """
    font = _find_font_file()
    if font is None:
        logger.warning("无可用中文字体，跳过片头/片尾卡生成")
        return None
    try:
        font_escaped = font.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        drawtexts: List[str] = []
        for text, size_ratio, y_ratio in lines:
            text = str(text or "").strip()
            if not text:
                continue
            # drawtext text 转义：冒号/单引号/百分号/逗号（逗号会破坏滤镜图分隔）
            escaped = (
                text.replace("\\", "\\\\\\\\")
                .replace(":", "\\:")
                .replace("'", "\\'")
                .replace("%", "\\%")
                .replace(",", "\\,")
            )
            fs = max(18, int(height * size_ratio))
            y = int(height * y_ratio)
            drawtexts.append(
                f"drawtext=fontfile='{font_escaped}':text='{escaped}':"
                f"fontsize={fs}:fontcolor=white:x=(w-tw)/2:y={y}"
            )
        fade_out = max(0.0, duration - 0.5)
        vf_parts = drawtexts + [
            "fade=t=in:st=0:d=0.5",
            f"fade=t=out:st={fade_out:.2f}:d=0.5",
        ]
        cmd = [
            ffmpeg, "-y", "-v", "error",
            "-f", "lavfi", "-i",
            f"color=c=black:s={width}x{height}:d={duration:.2f}:r={fps:.3f}",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-vf", ",".join(vf_parts),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-shortest",
            str(out_path),
        ]
        proc = await shutil_get_run(cmd, timeout=_POST_TIMEOUT)
        if proc is not None and proc.returncode == 0 and out_path.is_file():
            return out_path
        logger.warning("片头/片尾卡生成失败：%s", (proc.stderr or b"")[-300:] if proc else "timeout")
        return None
    except Exception:  # noqa: BLE001 —— 标题卡失败降级不插卡，不阻断合成
        logger.warning("片头/片尾卡生成异常", exc_info=True)
        return None


async def _placeholder_film(task: Task, out_dir: Path, ffmpeg: Optional[str]) -> str:
    """生成演示占位成片（项目完全无可用片段时兜底，D2 修复）。

    行为约定：
    - 本机有 ffmpeg：用 lavfi 纯色源生成 2 秒 720x1280 黑底占位 MP4，并以
      drawtext 叠加「演示占位·无可用片段」提示（本机无可用中文字体时降级
      为无文字纯黑 MP4），保证 <video> 元素可正常播放；
    - 本机无 ffmpeg：不再返回假 URL（旧实现返回 SVG 海报导致 <video> 无法
      播放），直接抛异常，由 compose_film / 任务执行器的既有异常路径把任务
      置为 failed 并按平台承担退款。

    Args:
        task: 合成任务（用任务 ID 命名，避免并发覆盖）。
        out_dir: 成片输出目录（storage/outputs 绝对路径）。
        ffmpeg: ffmpeg 可执行文件路径（None 表示本机无 ffmpeg）。

    Returns:
        str: 占位成片的 /storage URL（恒以 .mp4 结尾）。

    Raises:
        RuntimeError: 本机无 ffmpeg，或 ffmpeg 生成失败——均表示无法产出
            可播放的占位成片，不允许用假地址充当交付物。
    """
    if not ffmpeg:
        raise RuntimeError("无可用片段且无 ffmpeg，无法合成演示占位成片")
    filename = f"film_placeholder_{task.id}.mp4"
    out_path = out_dir / filename
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        # 2 秒 720x1280 黑底 lavfi 源；有可用字体时叠加居中占位提示文字
        drawtext = build_placeholder_drawtext_filter("演示占位·无可用片段")
        cmd: List[str] = [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=0x000000:s=720x1280:d=2"]
        if drawtext:
            cmd += ["-vf", drawtext]
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            str(out_path),
        ]
        proc = await shutil_get_run(cmd, timeout=_POST_TIMEOUT)
        if proc is None or proc.returncode != 0 or not out_path.exists():
            raise RuntimeError("演示占位成片生成失败（ffmpeg 执行异常），无法提供可播放成片")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"演示占位成片写入异常：{exc}") from exc
    return f"{_OUTPUT_URL_PREFIX}/{filename}"


async def compose_film(db: AsyncSession, task: Task) -> str:
    """执行成片合成并写入 film_outputs 版本记录（compose 任务执行主体）。

    合成流水线：分镜查询（增量过滤）→ concat 直拼 → xfade 转场（可选）→
    后处理（字幕/水印/BGM，可选）→ 版本落库。任一 ffmpeg 环节失败均降级
    到上一级产物（最终回退占位成片）；项目完全无片段且本机无 ffmpeg 时
    抛异常（由执行器置任务 failed，不返回假 URL）。

    Args:
        db: 异步数据库会话（调用方负责最终提交；本函数内不 commit）。
        task: 合成任务（meta 携带 resolution/aspect_ratio/storyboard_ids/
            subtitles/subtitle_style/bgm/transition）。

    Returns:
        str: 成片资源 URL（/storage/outputs/...，占位场景可能指向片段或
            演示占位 MP4，恒以 .mp4 结尾）。

    Raises:
        Exception: 数据库查询异常向上抛出，由执行器按平台错误重试；
            RuntimeError: 无可用片段且无 ffmpeg（或占位成片生成失败）时抛出，
            由执行器置任务 failed。
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
    # S-06：剧集范围（None/缺省=全部，向后兼容；传入时仅合成该剧集分镜）
    raw_episode_id = meta.get("episode_id")
    try:
        episode_filter: Optional[int] = int(raw_episode_id) if raw_episode_id is not None else None
    except (TypeError, ValueError):
        episode_filter = None

    conds = [
        Storyboard.project_id == task.project_id,
        Storyboard.video_url.is_not(None),
    ]
    if episode_filter is not None:
        conds.append(Storyboard.episode_id == episode_filter)
    rows = await db.execute(
        select(Storyboard).where(*conds).order_by(Storyboard.sequence)
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
    # 片头片尾卡状态（块外兜底初始化：ffmpeg 缺失/无片段分支不进入生成逻辑）
    intro_dur = 0.0
    outro_dur = 0.0
    all_paths: List[Path] = []
    # 转场应用标记与时长（时长合计扣减用；作用域覆盖降级分支）
    xfade_applied = False
    transition_duration = 0.0
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and storyboards:
        # 拼接归一化基准（2026-10-21）：取首镜产物分辨率/帧率，全部片段统一
        # scale/fps 与音轨钳制——各镜 mp4 同构后 concat -c copy 才无累积漂移
        base_w, base_h, base_fps = 0, 0, 0.0
        for sb0 in storyboards:
            vp0 = _physical_path(sb0.video_url)
            if vp0.exists():
                base_w, base_h, base_fps = await _probe_resolution(ffprobe, vp0)
                break
        target_size = (base_w, base_h) if base_w > 0 and base_h > 0 else None
        target_fps = base_fps if base_fps > 0 else None
        # 音画对齐（音频先行）：每镜先把配音合入片段（配音长于视频时末帧定格
        # 补齐，短于视频时补静音），无配音镜也统一重编码补静音轨——保证 concat
        # -c copy 拼接编码参数与音轨连续性；对齐失败的镜回退原片段不阻断合成
        pairs: List[Tuple[Storyboard, Path]] = []
        for idx, sb in enumerate(storyboards):
            vp = _physical_path(sb.video_url)
            if not vp.exists():
                continue
            audio_phys = _physical_path(sb.audio_url) if sb.audio_url else None
            # 视频自带音轨 ⟺ 已含当前配音（视频任务阶段完成「环境音+配音」
            # 混流，2026-10-20 口径）：不再叠加 audio_url，避免配音双重混入
            if audio_phys and await _has_audio_stream(ffprobe, vp):
                audio_phys = None
            has_audio = bool(audio_phys and audio_phys.is_file())
            # 统一处理：有配音合入音轨（末帧定格/静音补齐），无配音补静音轨，
            # 全部片段同参编码保证 concat -c copy 兼容；失败回退原片段
            merged = await _merge_clip_audio(
                ffmpeg, ffprobe, vp, audio_phys if has_audio else None,
                out_dir / f"_av_{task.id}_{idx}.mp4",
                target_size=target_size, target_fps=target_fps,
            )
            pairs.append((sb, merged or vp))
        clip_paths = [p for _, p in pairs]
        # 转场（2026-10-21 用户确认解除配音互斥）：xfade+acrossfade 双链保留
        # 各片段配音音轨，有配音也生效；转场失败回退 concat 直拼
        # 片头片尾卡（2026-10-21 成片整合）：黑底文字卡作为独立片段插入
        # 拼接链首尾（片头=剧名+集数 2.5s，片尾=剧终 2s，淡入淡出）；生成
        # 失败/无字体降级不插卡；卡参与 xfade 时按自身时长计入 durations
        intro_path: Optional[Path] = None
        outro_path: Optional[Path] = None
        if (meta.get("intro_card") or meta.get("outro_card")) and pairs and ffprobe:
            v_w0, v_h0, v_fps0 = await _probe_resolution(ffprobe, clip_paths[0])
            if v_w0 > 0 and v_h0 > 0:
                proj = await db.get(Project, task.project_id)
                ep = (
                    await db.get(Episode, episode_filter)
                    if episode_filter is not None else None
                )
                if meta.get("intro_card"):
                    intro_lines: List[Tuple[str, float, float]] = [
                        (proj.title if proj else "", 0.055, 0.42)
                    ]
                    if ep is not None:
                        intro_lines.append((f"第{ep.sequence}集 · {ep.title}", 0.032, 0.55))
                    intro_path = await _build_text_card(
                        ffmpeg, out_dir / f"_intro_{task.id}.mp4",
                        v_w0, v_h0, intro_lines, 2.5,
                        fps=v_fps0 if v_fps0 > 0 else 24.0,
                    )
                if meta.get("outro_card"):
                    outro_path = await _build_text_card(
                        ffmpeg, out_dir / f"_outro_{task.id}.mp4",
                        v_w0, v_h0, [("剧终", 0.05, 0.45)], 2.0,
                        fps=v_fps0 if v_fps0 > 0 else 24.0,
                    )
        intro_dur = 2.5 if intro_path is not None else 0.0
        outro_dur = 2.0 if outro_path is not None else 0.0
        all_paths = (
            ([intro_path] if intro_path else [])
            + clip_paths
            + ([outro_path] if outro_path else [])
        )
        if all_paths:
            base_path = out_dir / f"{task.project_id}_v{version_no}.mp4"
            list_path = out_dir / f"_concat_{task.id}.txt"
            current: Optional[Path] = (
                base_path if await _concat_clips(ffmpeg, all_paths, base_path, list_path) else None
            )
            xfade_applied = False
            transition_duration = 0.0
            # 转场（Task 5.3）：≥2 片段时以 xfade+acrossfade 双链重编码替代
            # 直拼产物（视频交叉淡化 + 配音音轨交叉淡化）；失败回退直拼。
            # durations 逐片段真实探测（估算偏差会让转场起点漂移）
            if current and transition and len(all_paths) >= 2:
                real_durs: List[float] = []
                for p_i in all_paths:
                    d_i = (
                        await _probe_duration(ffprobe, p_i)
                        if ffprobe else None
                    )
                    real_durs.append(d_i if d_i and d_i > 0 else 5.0)
                xfade_path = out_dir / f"{task.project_id}_v{version_no}_xfade.mp4"
                if await _xfade_clips(ffmpeg, all_paths, real_durs, xfade_path, transition):
                    current = xfade_path
                    xfade_applied = True
                    try:
                        transition_duration = min(
                            max(float(transition.get("duration", 0.5) or 0.5), 0.1), 3.0
                        )
                    except (TypeError, ValueError):
                        transition_duration = 0.5

            # 字幕（Task 5.1/P-16）：双语样式先 LLM 翻译生成 ASS，任一失败
            # 降级 SRT 单行；无可字幕文本/写盘失败降级无字幕（不阻断合成）
            sub_path: Optional[Path] = None
            if subtitle_on and current:
                sub_path = await _prepare_subtitle_file(
                    storyboards, subtitle_style, task.id, out_dir, aspect_ratio,
                )

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
                # SRT 字幕按 libass 默认脚本高度 288 渲染：字号为真实像素时
                # 需按成片实际高度换算；ASS 自带 PlayRes 无需换算
                sub_video_height = 0
                if sub_path is not None and sub_path.suffix.lower() == ".srt":
                    _, sub_video_height = await _probe_resolution(ffprobe, current)
                final_path = out_dir / f"{task.project_id}_v{version_no}_final.mp4"
                if await _postprocess_film(
                    ffmpeg, ffprobe, current, final_path,
                    subtitles_filter=(
                        build_subtitles_filter(sub_path, subtitle_style, sub_video_height)
                        if sub_path else None
                    ),
                    drawtext_filter=build_drawtext_filter(watermark_id[:8]),
                    bgm_path=bgm_path,
                    bgm_volume=bgm_volume,
                ):
                    current = final_path
            if current is not None:
                film_url = f"{_OUTPUT_URL_PREFIX}/{current.name}"
    if film_url is None:
        # 降级占位：有片段则直接引用第一个片段地址；完全无片段则生成演示占位
        # MP4（无 ffmpeg 时抛异常走任务失败路径，不再以 SVG 假 URL 充当成片）
        if storyboards:
            film_url = storyboards[0].video_url
        else:
            film_url = await _placeholder_film(task, out_dir, ffmpeg)

    # 时长合计：优先取配音回写的实际时长（RD-31），缺省用分镜预估时长；
    # 片头片尾卡时长计入（2026-10-21）；应用 xfade 转场时按全链片段数扣除
    # 相邻片段间的转场重叠时长
    total_duration = sum(int(sb.actual_duration or sb.duration or 5) for sb in storyboards)
    total_duration += intro_dur + outro_dur
    chain_len = len(all_paths) if ffmpeg and storyboards else 0
    if xfade_applied and chain_len >= 2:
        total_duration = max(1, int(total_duration - (chain_len - 1) * transition_duration))

    # 版本切换：历史版本全部退出"当前版本"，新版本置为当前
    existing = (
        await db.execute(select(FilmOutput).where(FilmOutput.project_id == task.project_id))
    ).scalars().all()
    for old in existing:
        old.is_current = False

    film = FilmOutput(
        project_id=task.project_id,
        # S-06：成片版本绑定来源剧集（None=未绑定剧集的全量合成，与存量一致）
        episode_id=episode_filter,
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
