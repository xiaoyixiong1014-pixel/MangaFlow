"""阿里云百炼文生图提供方模块（文件级说明）。

通过 DashScope 原生多模态生成协议调用百炼文生图模型（qwen-image 系列）：
- 同步调用：POST {root}/api/v1/services/aigc/multimodal-generation/generation
  （2026-10-16 实测：该端点对 qwen-image 不支持 X-DashScope-Async 异步头，
  返回 403 "current user api does not support asynchronous calls"；text2image
  端点对 qwen-image 返回 400 url error——正确契约即本模块实现）；
- 请求体契约：input.messages=[{role:"user", content:[{text: prompt}]}]；
- 响应契约：output.choices[0].message.content 数组中 type=="image" 项的
  image 字段为结果图 URL（实测输出 1792x2400 竖版，跟随提示词语义）；
- 结果下载落盘（URL 为 OSS 临时签名地址，含 Expires 必须即时下载）。

注意：百炼 OpenAI 兼容模式（compatible-mode）不提供 /images/generations
端点（公共云 404 / 专属实例 403 access_denied，2026-10-16 实测），图像
生成必须走本模块的 DashScope 原生协议；渠道 base_url 填实例根域名
（如 https://dashscope.aliyuncs.com 或专属实例根域名，专属实例需已开通
多模态生成端点权限，否则 403 Endpoint.AccessDenied）。

任何失败返回 None（不抛出），由调用方（tasks/runner.py）降级到下一渠道。
channel 为 model_router.ResolvedChannel（protocol="dashscope"）。
"""

import logging
import time
from pathlib import Path
from typing import Dict, Optional

import httpx

from app.services.model_router import ResolvedChannel

logger = logging.getLogger("mangaflow.providers.image_dashscope")

# 生成结果 URL 前缀：main.py 已将 storage 目录 mount 到 /storage
_URL_PREFIX = "/storage/generated"
# 生成请求超时（秒）：qwen-image 同步生成实测约 10~30 秒
_HTTP_TIMEOUT = 180.0
# 媒体下载超时（秒）
_DOWNLOAD_TIMEOUT = 120.0


def _extract_image_url(data: Dict) -> Optional[str]:
    """从多模态生成响应中提取首个图片 URL（内部函数）。

    解析路径：output.choices[*].message.content[*] 中 type=="image" 项的
    image 字段；兼容顶层 output.results[*].url（异步任务结果形态）。

    Args:
        data: 响应 JSON。

    Returns:
        Optional[str]: 图片 URL；无结果返回 None。

    Raises:
        无：结构异常安全返回 None。
    """
    try:
        output = data.get("output") or {}
        # 形态一（同步多模态生成）：choices[].message.content[].image
        for choice in output.get("choices") or []:
            content = ((choice or {}).get("message") or {}).get("content") or []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image" and block.get("image"):
                    return str(block["image"])
        # 形态二（异步任务结果）：results[].url
        for item in output.get("results") or []:
            if isinstance(item, dict) and item.get("url"):
                return str(item["url"])
    except Exception:  # noqa: BLE001 —— 解析失败按无结果兜底
        pass
    return None


def _ext_from_response(resp: httpx.Response, url: str) -> str:
    """根据响应 content-type 或 URL 后缀推断文件扩展名（内部函数）。

    Args:
        resp: 下载响应对象。
        url: 资源 URL。

    Returns:
        str: 扩展名（不含点，缺省 png）。
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


async def generate_image_dashscope(
    prompt: str,
    out_dir: Path,
    seed: int,
    channel: ResolvedChannel,
    reference_image_url: Optional[str] = None,
) -> Optional[Dict]:
    """调用百炼 DashScope 原生多模态生成协议真实生成一张图像（文生图）。

    Args:
        prompt: 图像提示词。
        out_dir: 输出目录（storage/generated 的绝对路径）。
        seed: 随机种子（一般传任务 ID，保证文件名唯一）。
        channel: 已解析的候选渠道（protocol="dashscope"，base_url 为实例根域名）。
        reference_image_url: 可选参考图 URL（当前文生图不使用，仅保留签名
            对齐；后续接入 qwen-image-edit 图生图时可扩展 content 结构）。

    Returns:
        Optional[Dict]: 成功返回
            {"url": "/storage/generated/xxx.png",
             "meta": {"provider": "dashscope-image", "channel_id": 渠道ID}}；
            任一失败返回 None（由调用方降级到下一渠道）。

    Raises:
        无：内部捕获全部异常并返回 None，不向上抛出。
    """
    # 提取根域名：兼容用户填根域名或带路径的形式（截到 netloc 为止）
    root = channel.base_url.rstrip("/")
    if "://" in root:
        root = root.split("://", 1)[0] + "://" + root.split("://", 1)[1].split("/", 1)[0]
    url = f"{root}/api/v1/services/aigc/multimodal-generation/generation"
    headers = {"Authorization": f"Bearer {channel.api_key}", "Content-Type": "application/json"}
    payload = {
        "model": channel.model,
        "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                logger.warning(
                    "渠道 %s 文生图失败（HTTP %s）：%s", channel.name, resp.status_code, resp.text[:300])
                return None
            image_url = _extract_image_url(resp.json() or {})
            if not image_url:
                logger.warning("渠道 %s 文生图响应缺少图片 URL", channel.name)
                return None
            # OSS 临时签名 URL 含 Expires，须即时下载落盘
            dl = await client.get(image_url)
        if dl.status_code != 200:
            logger.warning("渠道 %s 文生图结果下载失败（HTTP %s）", channel.name, dl.status_code)
            return None
        filename = f"keyframe_{seed}_{int(time.time())}.{_ext_from_response(dl, image_url)}"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / filename).write_bytes(dl.content)
    except Exception as exc:  # noqa: BLE001 —— provider 失败由调用方降级处理
        logger.warning("渠道 %s 文生图异常：%s", channel.name, exc)
        return None

    return {
        "url": f"{_URL_PREFIX}/{filename}",
        "meta": {"provider": "dashscope-image", "channel_id": channel.channel_id},
    }
