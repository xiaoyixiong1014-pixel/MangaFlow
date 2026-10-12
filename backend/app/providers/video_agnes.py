"""Agnes 视频生成提供方模块（文件级说明）。

通过 Agnes 官方视频契约调用（POST {base}/videos，官方文档
wiki.agnes-ai.com/en/docs/agnes-video-25-flash，2026-10-09 实测对齐）：
- 请求体：model / prompt / mode（text=文生视频；传入 first_frame_url 时自动
  切换 keyframe 模式并携带 first_frame 图片 URL）/ seconds（字符串，钳制
  4~12）/ size="720P"（Flash 固定值）/ aspect_ratio="9:16"（竖版 720x1280）；
- 异步任务：创建响应携带 video_id/id/task_id，轮询
  GET {root}/agnesapi?video_id=<ID>&model_name=<model>（根域名专用端点，
  间隔 settings.video_poll_interval_seconds，总超时
  settings.video_poll_timeout_seconds）；status=completed 后取顶层 url 字段
  下载落盘（下载走完整 https 地址）；
- 提交遇 HTTP 503 "video_queue_full"（官方提示稍后重试）时做快速探测重试，
  重试耗尽抛出 VideoQueueFullError（由 runner 层弹性等待重试，避免持续
  满载时过早降级 mock 占位）；
任何失败返回 None（不抛出），由调用方（tasks/runner.py）降级到下一渠道；
仅队列满以异常信号上抛。

函数签名与 mock 视频渠道对齐（prompt + duration + out_dir + seed + channel → url + meta），
channel 为 model_router.ResolvedChannel。
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.core.config import settings
from app.services.model_router import ResolvedChannel

logger = logging.getLogger("mangaflow.providers.video_agnes")

# 生成结果 URL 前缀：main.py 已将 storage 目录 mount 到 /storage
_URL_PREFIX = "/storage/generated"
# 常规请求超时（秒）
_HTTP_TIMEOUT = 30.0
# 媒体下载超时（秒）
_DOWNLOAD_TIMEOUT = 120.0
# Flash 版固定输出规格（官方契约：size 仅支持 "720P"，竖版经 aspect_ratio 表达）
_SIZE_FIXED = "720P"
# 竖版宽高比（对应输出 720x1280）
_ASPECT_RATIO_PORTRAIT = "9:16"
# seconds 允许范围（官方契约：字符串 "4"~"12"）
_SECONDS_MIN = 4
_SECONDS_MAX = 12
# 503 队列满（官方提示 retry later）/ 429 免费档限流时的快速探测重试次数：
# 仅覆盖短时空隙；持续满载由 runner 层弹性等待重试（见 _run_video）
_SUBMIT_MAX_RETRIES = 3
# 503/429 重试间隔（秒）：快速探测节奏
_SUBMIT_RETRY_INTERVAL = 30.0


class VideoQueueFullError(Exception):
    """Agnes 视频渠道队列满异常（503 video_queue_full 重试耗尽后抛出）。

    runner 层捕获后进入弹性等待重试（不立即降级 mock），详见 _run_video。
    """


def _pick_video_url(data: Dict[str, Any]) -> Optional[str]:
    """从响应 JSON 中提取视频下载地址（内部函数）。

    优先级：video_url > output（字符串或 dict.url） > url。

    Args:
        data: 提交/轮询接口返回的任务 JSON。

    Returns:
        Optional[str]: 视频 URL；无法提取返回 None。

    Raises:
        无。
    """
    if data.get("video_url"):
        return str(data["video_url"])
    output = data.get("output")
    if isinstance(output, str) and output:
        return output
    if isinstance(output, dict):
        nested = output.get("url") or output.get("video_url")
        if nested:
            return str(nested)
    if data.get("url"):
        return str(data["url"])
    return None


async def _download_video(
    client: httpx.AsyncClient,
    url: str,
    base: str,
    headers: Dict[str, str],
    out_dir: Path,
    seed: int,
) -> Optional[str]:
    """下载视频二进制并落盘为 mp4（内部函数）。

    Args:
        client: 复用的 httpx 异步客户端（下载用更长超时）。
        url: 视频 URL；以 / 开头视为相对路径，按渠道 base_url 拼接。
        base: 渠道 base_url（去除尾斜杠）。
        headers: 携带鉴权的请求头。
        out_dir: 输出目录。
        seed: 随机种子（用于文件名）。

    Returns:
        Optional[str]: 落盘后的文件名；下载/写盘失败返回 None。

    Raises:
        无：异常统一收敛为 None。
    """
    try:
        target = f"{base}{url}" if url.startswith("/") else url
        resp = await client.get(target, headers=headers)
        if resp.status_code != 200:
            logger.warning("视频下载失败（HTTP %s）：%s", resp.status_code, target)
            return None
        filename = f"clip_{seed}_{int(time.time())}.mp4"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / filename).write_bytes(resp.content)
        return filename
    except Exception as exc:  # noqa: BLE001
        logger.warning("视频下载异常：%s", exc)
        return None


async def generate_video_real(
    prompt: str,
    duration_sec: int,
    out_dir: Path,
    seed: int,
    channel: ResolvedChannel,
    first_frame_url: Optional[str] = None,
) -> Optional[Dict]:
    """调用 Agnes 渠道真实生成一段竖版视频片段。

    Args:
        prompt: 视频提示词。
        duration_sec: 期望时长（秒，钳制到官方允许的 4~12 范围）。
        out_dir: 输出目录（storage/generated 的绝对路径）。
        seed: 随机种子（一般传任务 ID，保证文件名唯一）。
        channel: 已解析的候选渠道（含 base_url/api_key/model）。
        first_frame_url: 首帧图片 URL（可选）；传入时自动切换 keyframe 模式
            实现图生视频，未传走 settings.agnes_video_mode（默认 text 文生视频）。

    Returns:
        Optional[Dict]: 成功返回
            {"url": "/storage/generated/clip_xxx.mp4",
             "meta": {"provider": "agnes", "channel_id": 渠道ID}}；
            提交失败/轮询超时/任务 failed/下载失败返回 None。

    Raises:
        无：内部捕获全部异常并返回 None，不向上抛出。
    """
    try:
        base = channel.base_url.rstrip("/")
        # 轮询端点位于根域名 /agnesapi（官方契约），需从 base_url 剥离 /v1 后缀
        root = base[:-3].rstrip("/") if base.endswith("/v1") else base
        headers = {"Authorization": f"Bearer {channel.api_key}"}
        # mode：带首帧图走 keyframe（官方要求携带 first_frame 图片 URL），否则用配置模式
        mode = "keyframe" if first_frame_url else settings.agnes_video_mode
        payload: Dict[str, Any] = {
            "model": channel.model,
            "prompt": prompt,
            "mode": mode,
            "seconds": str(min(max(int(duration_sec or 5), _SECONDS_MIN), _SECONDS_MAX)),
            "size": _SIZE_FIXED,
            "aspect_ratio": _ASPECT_RATIO_PORTRAIT,
        }
        if first_frame_url:
            payload["first_frame"] = first_frame_url

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            # 提交任务：503 队列满（官方提示稍后重试）做有限次退避重试
            data: Dict[str, Any] = {}
            for attempt in range(1, _SUBMIT_MAX_RETRIES + 1):
                resp = await client.post(f"{base}/videos", headers=headers, json=payload)
                if resp.status_code in (200, 201, 202):
                    data = resp.json() or {}
                    break
                if resp.status_code in (503, 429) and attempt < _SUBMIT_MAX_RETRIES:
                    logger.warning("渠道 %s 视频提交被限流/队列满（HTTP %s，第 %s/%s 次重试）",
                                   channel.name, resp.status_code, attempt, _SUBMIT_MAX_RETRIES)
                    await asyncio.sleep(_SUBMIT_RETRY_INTERVAL)
                    continue
                if resp.status_code == 503 and "video_queue_full" in resp.text:
                    # 队列满重试耗尽：抛出可重试信号（runner 弹性等待），不视为渠道死亡
                    raise VideoQueueFullError(
                        f"渠道 {channel.name} 视频队列满（video_queue_full，已快速探测 {_SUBMIT_MAX_RETRIES} 次）"
                    )
                logger.warning("渠道 %s 视频任务提交失败（HTTP %s）：%s",
                               channel.name, resp.status_code, resp.text[:300])
                return None

            # 同步模式：创建响应直接携带视频地址（官方为异步，此分支兜底）
            direct_url = _pick_video_url(data)
            if direct_url:
                async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as dl_client:
                    filename = await _download_video(dl_client, direct_url, base, headers, out_dir, seed)
                if filename is None:
                    return None
                return {"url": f"{_URL_PREFIX}/{filename}",
                        "meta": {"provider": "agnes", "channel_id": channel.channel_id}}

            # 异步模式：官方要求以 video_id 轮询根域名 /agnesapi 端点
            task_id = data.get("video_id") or data.get("id") or data.get("task_id")
            if not task_id:
                logger.warning("渠道 %s 视频响应缺少 video_id/id 字段：%s", channel.name, str(data)[:300])
                return None
            detail: Dict[str, Any] = {}
            deadline = time.monotonic() + settings.video_poll_timeout_seconds
            while time.monotonic() < deadline:
                await asyncio.sleep(settings.video_poll_interval_seconds)
                poll = await client.get(
                    f"{root}/agnesapi",
                    params={"video_id": str(task_id), "model_name": channel.model},
                    headers=headers,
                )
                if poll.status_code != 200:
                    logger.warning("渠道 %s 视频轮询失败（HTTP %s）", channel.name, poll.status_code)
                    return None
                detail = poll.json() or {}
                status = str(detail.get("status", "")).lower()
                if status == "completed":
                    break
                if status == "failed":
                    logger.warning("渠道 %s 视频任务 failed：%s", channel.name, str(detail)[:300])
                    return None
            else:
                logger.warning("渠道 %s 视频轮询超时（%ds）", channel.name, settings.video_poll_timeout_seconds)
                return None

            # completed：官方顶层 url 字段即成片地址（_pick_video_url 已兼容）
            video_url = _pick_video_url(detail)
            if not video_url:
                logger.warning("渠道 %s 视频任务 completed 但缺少 url：%s", channel.name, str(detail)[:300])
                return None
            async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as dl_client:
                filename = await _download_video(dl_client, video_url, base, headers, out_dir, seed)
            if filename is None:
                return None
            return {"url": f"{_URL_PREFIX}/{filename}",
                    "meta": {"provider": "agnes", "channel_id": channel.channel_id}}
    except Exception as exc:  # noqa: BLE001 —— provider 失败由调用方降级处理
        logger.warning("渠道 %s 视频生成异常：%s", channel.name, exc)
        return None
