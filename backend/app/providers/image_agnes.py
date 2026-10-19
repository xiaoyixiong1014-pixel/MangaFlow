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

from app.core.config import settings
from app.providers.reference import resolve_reference_data_uri
from app.services.model_router import ResolvedChannel

logger = logging.getLogger("mangaflow.providers.image_agnes")

# 生成结果 URL 前缀：main.py 已将 storage 目录 mount 到 /storage
_URL_PREFIX = "/storage/generated"
# 请求/下载超时（秒）
_HTTP_TIMEOUT = 120.0
# 输出画幅 → 生成尺寸映射（缺省竖版 9:16，与项目画幅配置口径一致）
_SIZE_BY_ASPECT: Dict[str, str] = {
    "9:16": "720x1280",
    "16:9": "1280x720",
    "1:1": "1024x1024",
}
# 画幅缺省尺寸（未传或白名单外的画幅回退竖版）
_DEFAULT_SIZE = "720x1280"


def _size_for_aspect(aspect_ratio: Optional[str]) -> str:
    """把项目画幅配置映射为生图尺寸（内部函数）。

    Args:
        aspect_ratio: 画幅配置（9:16/16:9/1:1，可空）。

    Returns:
        str: images 接口 size 参数（宽x高）；未传或不在白名单时回退竖版。

    Raises:
        无：纯映射，不产生异常路径。
    """
    return _SIZE_BY_ASPECT.get((aspect_ratio or "").strip(), _DEFAULT_SIZE)


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


def _resolve_reference_payload(reference_image_url: Optional[str]) -> Optional[str]:
    """把参考图 URL 解析为渠道可消费的载荷（共享工具的渠道内别名）。

    Args:
        reference_image_url: 参考图 URL（/storage/... 本地图或外部 URL，可空）。

    Returns:
        Optional[str]: data URI / 外部 URL / None（入参为空）。

    Raises:
        无：本地文件读取失败时告警并原样返回 URL（由渠道侧决定消费方式）。
    """
    return resolve_reference_data_uri(reference_image_url)


async def generate_image_real(
    prompt: str,
    out_dir: Path,
    seed: int,
    channel: ResolvedChannel,
    reference_image_url: Optional[str] = None,
    reference_image_urls: Optional[list] = None,
    aspect_ratio: Optional[str] = None,
) -> Optional[Dict]:
    """调用 Agnes 渠道真实生成一张图像（尺寸按项目画幅映射）。

    Args:
        prompt: 图像提示词。
        out_dir: 输出目录（storage/generated 的绝对路径）。
        seed: 随机种子（一般传任务 ID，保证文件名唯一）。
        channel: 已解析的候选渠道（含 base_url/api_key/model）。
        reference_image_url: 可选单张参考图 URL（A-04 资产生成锚点）。
        reference_image_urls: 可选多张参考图列表（关键帧绑定资产参考图组：
            角色[变体优先]→场景→道具，排序即重要性）。与单图合并后透传——
            单张传字符串、多张传数组（OpenAI images 兼容网关的参考图入参
            字段名存在实现差异，本渠道约定 image 字段直传；网关不识别时
            一般忽略该字段按纯文本提示词生成，不影响主流程）。
        aspect_ratio: 可选输出画幅（9:16/16:9/1:1），映射 size 参数；
            未传时回退竖版 720x1280。

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
        payload: Dict = {
            "model": channel.model,
            "prompt": prompt,
            "n": 1,
            "size": _size_for_aspect(aspect_ratio),
        }
        # A-04：合并单图与多图参考（本地 /storage 路径转 Base64 data URI，
        # 外部 URL 原样）；单张传字符串、多张传数组
        single = _resolve_reference_payload(reference_image_url)
        multiple = [
            p for p in (_resolve_reference_payload(u) for u in (reference_image_urls or []))
            if p
        ]
        ref_images: object = None
        if single and multiple:
            ref_images = [single, *multiple]
        elif single:
            ref_images = single
        elif multiple:
            ref_images = multiple if len(multiple) > 1 else multiple[0]
        if ref_images is not None:
            payload["image"] = ref_images

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
