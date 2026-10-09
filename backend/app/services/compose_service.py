"""成片合成服务模块（文件级说明）。

由任务执行器（app/tasks/runner.py）在 compose 任务调度时调用：
- 取项目中已生成视频片段（video_url 非空）的分镜，按镜头序号拼接；
- 本机 ffmpeg 可用且片段数 >= 1：concat demuxer 拼接为 MP4 成片；
- 否则降级为占位成片（直接取首个片段地址；完全无片段则生成 SVG 海报充当成片地址）；
- 每次合成都写入一条 film_outputs 版本记录（版本号递增、is_current 切换、
  附带隐形水印标识 watermark_id —— 本阶段演示为纯标识写入，不做像素级水印）。
"""

import shutil
import subprocess
from pathlib import Path
from typing import List, Optional
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


async def shutil_get_run(cmd: List[str]):
    """执行外部命令的线程池封装（subprocess.run + 超时）。

    Args:
        cmd: 命令与参数列表。

    Returns:
        subprocess.CompletedProcess 或 None（超时/异常）。

    Raises:
        无：内部捕获全部异常返回 None。
    """
    import asyncio

    try:
        return await asyncio.to_thread(
            subprocess.run, cmd,
            timeout=_CONCAT_TIMEOUT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None


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

    Args:
        db: 异步数据库会话（调用方负责最终提交；本函数内不 commit）。
        task: 合成任务（meta 携带 resolution/aspect_ratio/storyboard_ids）。

    Returns:
        str: 成片资源 URL（/storage/outputs/...，占位场景可能指向片段或 SVG 海报）。

    Raises:
        Exception: 数据库查询异常向上抛出，由执行器按平台错误重试。
    """
    meta = task.meta or {}
    resolution = meta.get("resolution", "720P")
    aspect_ratio = meta.get("aspect_ratio", "9:16")

    rows = await db.execute(
        select(Storyboard)
        .where(Storyboard.project_id == task.project_id, Storyboard.video_url.is_not(None))
        .order_by(Storyboard.sequence)
    )
    storyboards: List[Storyboard] = list(rows.scalars().all())
    out_dir = settings.storage_path() / "outputs"
    version_no = await _next_version_no(db, task.project_id)

    film_url: Optional[str] = None
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg and storyboards:
        # 片段物理路径（按序）；仅拼接真实存在的本地文件
        clip_paths = [_physical_path(sb.video_url) for sb in storyboards]
        clip_paths = [p for p in clip_paths if p.exists()]
        if clip_paths:
            out_path = out_dir / f"{task.project_id}_v{version_no}.mp4"
            list_path = out_dir / f"_concat_{task.id}.txt"
            if await _concat_clips(ffmpeg, clip_paths, out_path, list_path):
                film_url = f"{_OUTPUT_URL_PREFIX}/{out_path.name}"
    if film_url is None:
        # 降级占位：有片段则直接引用第一个片段地址；完全无片段则生成 SVG 海报
        if storyboards:
            film_url = storyboards[0].video_url
        else:
            film_url = await _poster_film_url(task, out_dir)

    # 时长合计：优先取配音回写的实际时长（RD-31），缺省用分镜预估时长
    total_duration = sum(int(sb.actual_duration or sb.duration or 5) for sb in storyboards)

    # 版本切换：历史版本全部退出"当前版本"，新版本置为当前
    existing = (
        await db.execute(select(FilmOutput).where(FilmOutput.project_id == task.project_id))
    ).scalars().all()
    for old in existing:
        old.is_current = False

    # 隐形溯源水印标识（R-09）：本阶段演示为标识写入，不实际渲染到画面像素
    film = FilmOutput(
        project_id=task.project_id,
        episode_id=None,
        version_no=version_no,
        url=film_url,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        duration=total_duration,
        watermark_id=uuid4().hex[:32],
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
