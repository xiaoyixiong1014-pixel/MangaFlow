"""阿里云百炼通义万相视频提供方模块（文件级说明）。

通过 DashScope 原生异步任务协议调用专属实例的 wan2.7-i2v（图生视频）：
- 任务创建：POST {root}/api/v1/services/aigc/video-generation/video-synthesis，
  header X-DashScope-Async: enable（HTTP 仅支持异步，缺失该头报同步调用错误）；
- 请求体契约（wan2.7 新一代结构，与 wan2.6 及以前的 input.img_url 不同，
  2026-10-12 实测对齐官方文档 "Wan 2.7 - image-to-video API"）：
  input.media=[{type: "first_frame", url}] —— url 支持公网 URL 或
  Base64 data URI（data:image/png;base64,...），本地关键帧以 base64 传入；
  parameters.duration 取值 2~15（整数，超出钳制），resolution 取 "720P"
  （竖版 720x1280，输出画幅跟随首帧比例）；
- 任务轮询：GET {root}/api/v1/tasks/{task_id}，间隔
  settings.video_poll_interval_seconds，总超时 settings.video_poll_timeout_seconds；
  task_status=SUCCEEDED 后取 output.video_url 下载落盘；
- 首帧来源：本地关键帧文件（storage/generated/keyframe_*.png）读取后转
  base64 data URI；官方图片限制 JPEG/PNG/BMP/WEBP、宽高 [240,8000]、≤20MB。
- P-12/P-08 扩展：video_params.resolution 映射 parameters.resolution；
  fps 透传 parameters.fps（渠道约定待验证）；尾帧以 input.media 追加
  {type: "last_frame", url} 条目传入（渠道约定待验证）。

任何失败返回 None（不抛出），由调用方（tasks/runner.py）降级到下一渠道。
channel 为 model_router.ResolvedChannel（protocol="dashscope"，base_url 为
实例根域名）。
"""

import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import Dict, Optional

import httpx

from app.core.config import settings
from app.services.model_router import ResolvedChannel

logger = logging.getLogger("mangaflow.providers.video_wan")

# 生成结果 URL 前缀：main.py 已将 storage 目录 mount 到 /storage
_URL_PREFIX = "/storage/generated"
# 常规请求超时（秒）
_HTTP_TIMEOUT = 30.0
# 媒体下载超时（秒）
_DOWNLOAD_TIMEOUT = 120.0
# Flash 系固定输出分辨率（竖版输出画幅跟随首帧比例）
_RESOLUTION = "720P"
# duration 允许范围（官方契约：整数 2~15，默认 5）
_DURATION_MIN = 2
_DURATION_MAX = 15
# 首帧 data URI 的 MIME（关键帧生成为 PNG）
_FIRST_FRAME_MIME = "image/png"


async def generate_video_wan(
    prompt: str,
    duration_sec: int,
    out_dir: Path,
    seed: int,
    channel: ResolvedChannel,
    first_frame_path: Optional[Path] = None,
    video_params: Optional[Dict] = None,
    end_frame_path: Optional[Path] = None,
) -> Optional[Dict]:
    """调用百炼 wan2.7-i2v 渠道以首帧图驱动生成一段竖版视频片段。

    Args:
        prompt: 视频提示词（描述画面动态）。
        duration_sec: 期望时长（秒，钳制到官方允许的 2~15 范围）。
        out_dir: 输出目录（storage/generated 的绝对路径）。
        seed: 随机种子（一般传任务 ID，保证文件名唯一）。
        channel: 已解析的候选渠道（protocol="dashscope"，base_url 为实例根域名）。
        first_frame_path: 首帧图片本地路径（关键帧 PNG）；缺失或读取失败时
            返回 None（wan2.7-i2v 必须有首帧，无法文生视频）。
        video_params: 视频参数（P-12，可空）：resolution 映射 parameters.resolution
            （大写档位）；fps 透传 parameters.fps（渠道约定待验证）；
            motion_strength 无 wan 对应字段，不透传（仅在任务 meta 溯源）。
        end_frame_path: 尾帧图片本地路径（P-08，可空）；传入时以 input.media
            追加 {type: "last_frame", url} 条目实现首尾帧控制（渠道约定待验证）。

    Returns:
        Optional[Dict]: 成功返回
            {"url": "/storage/generated/clip_xxx.mp4",
             "meta": {"provider": "bailian-wan", "channel_id": 渠道ID}}；
            提交失败/轮询超时/任务 FAILED/下载失败/无首帧返回 None。

    Raises:
        无：内部捕获全部异常并返回 None，不向上抛出。
    """
    try:
        if not first_frame_path or not first_frame_path.is_file():
            logger.warning("渠道 %s 缺少首帧图片（wan2.7-i2v 为图生视频，需关键帧）",
                           channel.name)
            return None
        img_bytes = first_frame_path.read_bytes()
        if not img_bytes:
            logger.warning("渠道 %s 首帧图片为空：%s", channel.name, first_frame_path)
            return None
        data_uri = f"data:{_FIRST_FRAME_MIME};base64,{base64.b64encode(img_bytes).decode()}"

        # media 条目：首帧必带；尾帧（P-08）有本地文件时追加 last_frame 条目
        media_items: list = [{"type": "first_frame", "url": data_uri}]
        if end_frame_path and end_frame_path.is_file():
            end_bytes = end_frame_path.read_bytes()
            if end_bytes:
                end_uri = f"data:{_FIRST_FRAME_MIME};base64,{base64.b64encode(end_bytes).decode()}"
                # P-08 首尾帧控制：last_frame 条目字段名（渠道约定待验证）
                media_items.append({"type": "last_frame", "url": end_uri})
            else:
                logger.warning("渠道 %s 尾帧图片为空，忽略尾帧控制：%s",
                               channel.name, end_frame_path)

        root = channel.base_url.rstrip("/")
        # 根域名规范化（与 image_dashscope 同口径，截到 netloc 为止）：
        # 用户可能把渠道 base_url 配成 OpenAI 兼容模式地址（…/compatible-mode/v1），
        # 直接拼接会产生 /compatible-mode/v1/api/v1/... 的 404——DashScope
        # 原生 video-synthesis 端点必须挂在实例根域名下
        if "://" in root:
            root = root.split("://", 1)[0] + "://" + root.split("://", 1)[1].split("/", 1)[0]
        # 创建与轮询请求头分离：X-DashScope-Async 仅用于创建（轮询 GET 携带会 403）
        headers = {
            "Authorization": f"Bearer {channel.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        poll_headers = {"Authorization": f"Bearer {channel.api_key}"}
        # parameters：分辨率档位可由 video_params 覆盖（大写映射），其余保持官方契约
        params_block: Dict = {
            "resolution": _RESOLUTION,
            "duration": min(max(int(duration_sec or 5), _DURATION_MIN), _DURATION_MAX),
            # 分镜提示词已含完整画面/运镜描述，关闭重写降低时延
            "prompt_extend": False,
            "watermark": False,
        }
        if video_params:
            resolution = str(video_params.get("resolution") or "").upper()
            if resolution:
                params_block["resolution"] = resolution
            fps = video_params.get("fps")
            if fps:
                # P-12 帧率透传：parameters.fps（渠道约定待验证）
                params_block["fps"] = int(fps)
        payload: Dict = {
            "model": channel.model,
            "input": {
                "prompt": prompt,
                "media": media_items,
            },
            "parameters": params_block,
        }

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            # 创建任务
            resp = await client.post(
                f"{root}/api/v1/services/aigc/video-generation/video-synthesis",
                headers=headers, json=payload,
            )
            if resp.status_code not in (200, 201, 202):
                logger.warning("渠道 %s 视频任务创建失败（HTTP %s）：%s",
                               channel.name, resp.status_code, resp.text[:300])
                return None
            task_id = ((resp.json() or {}).get("output") or {}).get("task_id")
            if not task_id:
                logger.warning("渠道 %s 视频创建响应缺少 task_id：%s",
                               channel.name, resp.text[:300])
                return None

            # 轮询任务状态（DashScope 原生异步任务，万相生成一般 1~5 分钟）
            deadline = time.monotonic() + settings.video_poll_timeout_seconds
            detail: Dict = {}
            while time.monotonic() < deadline:
                await asyncio.sleep(settings.video_poll_interval_seconds)
                poll = await client.get(f"{root}/api/v1/tasks/{task_id}", headers=poll_headers)
                if poll.status_code != 200:
                    logger.warning("渠道 %s 视频轮询失败（HTTP %s）", channel.name, poll.status_code)
                    return None
                detail = (poll.json() or {}).get("output") or {}
                status = str(detail.get("task_status", "")).upper()
                if status == "SUCCEEDED":
                    break
                if status in ("FAILED", "CANCELED", "UNKNOWN"):
                    logger.warning("渠道 %s 视频任务 %s：%s",
                                   channel.name, status, str(detail)[:300])
                    return None
            else:
                logger.warning("渠道 %s 视频轮询超时（%ds）",
                               channel.name, settings.video_poll_timeout_seconds)
                return None

            video_url = detail.get("video_url")
            if not video_url:
                logger.warning("渠道 %s 视频任务 SUCCEEDED 但缺少 video_url：%s",
                               channel.name, str(detail)[:300])
                return None
            async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as dl_client:
                dl = await dl_client.get(video_url)
                if dl.status_code != 200 or not dl.content:
                    logger.warning("渠道 %s 视频下载失败（HTTP %s）",
                                   channel.name, dl.status_code)
                    return None
                filename = f"clip_{seed}_{int(time.time())}.mp4"
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / filename).write_bytes(dl.content)

            return {"url": f"{_URL_PREFIX}/{filename}",
                    "meta": {"provider": "bailian-wan", "channel_id": channel.channel_id}}
    except Exception as exc:  # noqa: BLE001 —— provider 失败由调用方降级处理
        logger.warning("渠道 %s 视频生成异常：%s", channel.name, exc)
        return None
