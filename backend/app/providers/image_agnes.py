"""Agnes 图像生成提供方模块（文件级说明）。

通过 OpenAI 兼容协议调用 Agnes 图像渠道（POST /images/generations）：
- 支持 b64_json 与 url 两种返回形态（b64 解码落盘 / httpx 下载落盘）；
- 对 OpenAI 兼容层差异做容错：400/422 且错误信息含 response_format 时，
  携带 response_format=b64_json 重试一次；
- 任何失败返回 None（不抛出），由调用方（tasks/runner.py）降级到下一渠道。

函数签名与 mock 图像渠道对齐（prompt + out_dir + seed + channel → url + meta），
channel 为 model_router.ResolvedChannel。
"""

import base64
import logging
import time
from pathlib import Path
from typing import Dict, Optional

import httpx

from app.services.model_router import ResolvedChannel

logger = logging.getLogger("mangaflow.providers.image_agnes")

# 生成结果 URL 前缀：main.py 已将 storage 目录 mount 到 /storage
_URL_PREFIX = "/storage/generated"
# 请求/下载超时（秒）
_HTTP_TIMEOUT = 120.0


def _build_endpoint(base_url: str) -> str:
    """拼装图像生成端点 URL（内部函数）。

    base_url 已以 /images/generations 结尾时直接使用，否则追加该路径。

    Args:
        base_url: 渠道接口地址（如 https://apihub.agnes-ai.com/v1）。

    Returns:
        str: 完整的图像生成端点 URL。

    Raises:
        无。
    """
    base = base_url.rstrip("/")
    if base.endswith("/images/generations"):
        return base
    return f"{base}/images/generations"


def _ext_from_response(resp: httpx.Response, url: str) -> str:
    """根据响应 content-type 或 URL 后缀推断文件扩展名（内部函数）。

    Args:
        resp: 下载响应对象。
        url: 原始资源 URL。

    Returns:
        str: 扩展名（不含点，缺省 png）。

    Raises:
        无。
    """
    content_type = (resp.headers.get("content-type") or "").lower()
    if "jpeg" in content_type or "jpg" in content_type:
        return "jpg"
    if "webp" in content_type:
        return "webp"
    for ext in ("png", "jpg", "jpeg", "webp"):
        if url.lower().endswith(f".{ext}"):
            return "png" if ext == "jpeg" else ext
    return "png"


async def generate_image_real(
    prompt: str,
    out_dir: Path,
    seed: int,
    channel: ResolvedChannel,
) -> Optional[Dict]:
    """调用 Agnes 渠道真实生成一张竖版图像。

    Args:
        prompt: 图像提示词。
        out_dir: 输出目录（storage/generated 的绝对路径）。
        seed: 随机种子（一般传任务 ID，保证文件名唯一）。
        channel: 已解析的候选渠道（含 base_url/api_key/model）。

    Returns:
        Optional[Dict]: 成功返回
            {"url": "/storage/generated/xxx.png",
             "meta": {"provider": "agnes", "channel_id": 渠道ID}}；
            请求/下载/落盘任一失败返回 None（由调用方降级到下一渠道）。

    Raises:
        无：内部捕获全部异常并返回 None，不向上抛出。
    """
    try:
        endpoint = _build_endpoint(channel.base_url)
        headers = {"Authorization": f"Bearer {channel.api_key}"}
        payload: Dict = {"model": channel.model, "prompt": prompt, "n": 1, "size": "720x1280"}

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(endpoint, headers=headers, json=payload)
            # 兼容层容错：部分 OpenAI 兼容实现要求显式 response_format
            if resp.status_code in (400, 422) and "response_format" in resp.text:
                retry_payload = {**payload, "response_format": "b64_json"}
                resp = await client.post(endpoint, headers=headers, json=retry_payload)
            if resp.status_code != 200:
                logger.warning(
                    "渠道 %s 图像生成失败（HTTP %s）：%s", channel.name, resp.status_code, resp.text[:300]
                )
                return None
            data = (resp.json() or {}).get("data") or []
            if not data:
                logger.warning("渠道 %s 图像生成返回空 data", channel.name)
                return None
            item = data[0]

            filename = f"keyframe_{seed}_{int(time.time())}"
            if item.get("b64_json"):
                # 形态一：base64 编码直接解码落盘
                filename = f"{filename}.png"
                raw = base64.b64decode(item["b64_json"])
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / filename).write_bytes(raw)
            elif item.get("url"):
                # 形态二：返回临时 URL，下载落盘（扩展名按 content-type/url 推断）
                image_url = item["url"]
                dl = await client.get(image_url)
                if dl.status_code != 200:
                    logger.warning("渠道 %s 图像下载失败（HTTP %s）", channel.name, dl.status_code)
                    return None
                filename = f"{filename}.{_ext_from_response(dl, image_url)}"
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / filename).write_bytes(dl.content)
            else:
                logger.warning("渠道 %s 图像响应缺少 b64_json/url 字段", channel.name)
                return None

        return {
            "url": f"{_URL_PREFIX}/{filename}",
            "meta": {"provider": "agnes", "channel_id": channel.channel_id},
        }
    except Exception as exc:  # noqa: BLE001 —— provider 失败由调用方降级处理
        logger.warning("渠道 %s 图像生成异常：%s", channel.name, exc)
        return None
