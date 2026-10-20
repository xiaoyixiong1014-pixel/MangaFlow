"""音源分离服务模块（文件级说明）。

基于 Meta demucs（htdemucs 模型，two-stems=vocals 模式）把媒体产物音轨
分离为「人声 / 无人声环境音」两条轨，供视频生成链路使用（2026-10-20
用户确认方案：wan 系模型按提示词对白自行合成角色人声，与环境音混在
同一条原生音轨中无法用 ffmpeg 选择性剔除；引入音源分离保留环境音、
彻底去除人声，最终分镜视频音轨 = 环境音(0.35) + 平台 TTS 配音(1.0)）。

调用方式：子进程执行 `同解释器 python -m demucs`（不 import torch 进
主进程，避免拖慢应用启动与常驻内存）；模型文件（htdemucs_th，约
80MB）由 demucs 首次运行时自动下载到用户 torch 缓存目录
（%USERPROFILE%/.cache/torch/hub/checkpoints），跨进程共享。

降级约定：分离任何环节失败（环境无 demucs/模型下载失败/超时/无音轨）
一律返回 None，由调用方降级为「全剥原生音轨（纯无声/纯配音）」，不
阻断视频任务主流程。
"""

import asyncio
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mangaflow.services.audio_separation")

# demucs 子进程默认超时（秒）：分镜视频音轨仅 2~15s，CPU 推理数十秒内
# 可完成；模型首次运行需额外下载 ~80MB，超时仅约束单次分离执行
_SEPARATE_TIMEOUT_SECONDS = 300.0

# 分离产物采样率/声道（ffmpeg 抽轨输入统一 44.1kHz 双声道，与配音
# 混流阶段 concat/amix 参数对齐）
_EXTRACT_SAMPLE_RATE = 44100


def _run(cmd: list, timeout: float) -> subprocess.CompletedProcess:
    """同步执行外部命令并返回结果（内部辅助，asyncio.to_thread 调用）。

    Args:
        cmd: 命令与参数列表。
        timeout: 超时（秒），超时杀死进程并抛出 TimeoutExpired。

    Returns:
        subprocess.CompletedProcess: 命令执行结果（capture_output 模式）。

    Raises:
        subprocess.TimeoutExpired: 命令执行超时。
        OSError: 可执行文件不存在等系统级错误。
    """
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def _has_audio_stream(ffprobe: str, media_path: Path) -> bool:
    """检测媒体文件是否含有音频流（内部辅助，同步实现）。

    Args:
        ffprobe: ffprobe 可执行文件路径。
        media_path: 媒体文件绝对路径。

    Returns:
        bool: 存在至少一条音频流返回 True；探测失败视为无音轨。

    Raises:
        无：内部捕获全部异常。
    """
    try:
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(media_path)],
            capture_output=True, timeout=30,
        )
        return bool((probe.stdout or b"").strip())
    except Exception:  # noqa: BLE001 —— 探测失败按无音轨处理
        return False


def _separate_sync(wav_in: Path, out_root: Path, timeout: float) -> Optional[Path]:
    """同步执行 demucs two-stems 分离（内部函数，to_thread 中运行）。

    以当前解释器（sys.executable -m demucs）子进程执行，保证使用与
    后端相同的 venv；输出目录结构固定为
    {out_root}/htdemucs/{track_stem}/no_vocals.wav。

    Args:
        wav_in: 输入音频 WAV 文件路径（已抽取的媒体音轨）。
        out_root: demucs 输出根目录（临时目录）。
        timeout: 子进程超时（秒）。

    Returns:
        Optional[Path]: 无人声环境音轨（no_vocals.wav）路径；产物缺失
            返回 None。

    Raises:
        Exception: 子进程超时/非零退出/OSError（由调用方捕获降级）。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "demucs",
         "--two-stems=vocals", "-n", "htdemucs",
         "-o", str(out_root), str(wav_in)],
        capture_output=True, timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or b"")[-500:].decode("utf-8", "replace")
        raise RuntimeError(f"demucs 退出码 {proc.returncode}: {tail}")
    stem = wav_in.stem
    candidate = out_root / "htdemucs" / stem / "no_vocals.wav"
    if not candidate.is_file():
        raise RuntimeError(f"demucs 产物缺失：{candidate}")
    return candidate


async def extract_no_vocals(
    media_path: Path,
    timeout: float = _SEPARATE_TIMEOUT_SECONDS,
) -> Optional[Path]:
    """从媒体文件音轨中分离出无人声环境音轨（公共主入口）。

    流程：ffprobe 检测音轨（无音轨直接返回 None）→ ffmpeg 抽音轨为
    44.1kHz 双声道 WAV（临时目录）→ demucs two-stems=vocals 分离 →
    no_vocals.wav 落到媒体同目录（{stem}_env.wav，存在则覆盖）→ 返回
    该路径。调用方使用完毕后可自行删除产物文件。

    失败降级：demucs 未安装（python -m demucs 失败）/ 模型下载失败 /
    超时 / 产物缺失等任何异常均记录 warning 后返回 None，调用方应降级
    为全剥原生音轨，不得让视频任务失败。

    Args:
        media_path: 媒体文件（mp4/wav 等）绝对路径。
        timeout: demucs 分离子进程超时（秒）。

    Returns:
        Optional[Path]: 无人声环境音 WAV 文件路径（媒体同目录
            {stem}_env.wav）；无可分离音轨或分离失败返回 None。

    Raises:
        无：内部捕获全部异常，失败统一收敛为 None。
    """
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe or not media_path.is_file():
        return None
    if not _has_audio_stream(ffprobe, media_path):
        return None  # 本就无音轨，无需分离
    demucs_probe = await asyncio.to_thread(
        _run, [sys.executable, "-m", "demucs", "--help"], 30.0,
    )
    if demucs_probe.returncode != 0:
        logger.warning("demucs 不可用（python -m demucs 探测失败），跳过环境音分离")
        return None
    out_path = media_path.parent / f"{media_path.stem}_env.wav"
    try:
        with tempfile.TemporaryDirectory(prefix="demucs_") as td:
            td_path = Path(td)
            wav_in = td_path / f"{media_path.stem}.wav"
            # 1) 抽音轨：统一 44.1kHz 双声道（分离与后续混流参数一致）
            extract = await asyncio.to_thread(
                _run,
                [ffmpeg, "-y", "-v", "error", "-i", str(media_path),
                 "-vn", "-ar", str(_EXTRACT_SAMPLE_RATE), "-ac", "2",
                 str(wav_in)],
                120.0,
            )
            if extract.returncode != 0 or not wav_in.is_file():
                logger.warning("音轨抽取失败，跳过环境音分离：%s",
                               (extract.stderr or b"")[-300:])
                return None
            # 2) demucs 分离（模型首次运行自动下载 htdemucs 权重）
            env_src = await asyncio.to_thread(
                _separate_sync, wav_in, td_path, timeout,
            )
            # 3) 产物落到媒体同目录稳定路径（覆盖旧文件）
            shutil.copyfile(env_src, out_path)
            logger.info("环境音分离完成：%s -> %s", media_path.name, out_path.name)
            return out_path
    except Exception as exc:  # noqa: BLE001 —— 分离失败降级，不阻断任务
        logger.warning("环境音分离失败（%s），降级剥除原生音轨：%s",
                       media_path.name, exc)
        return None
