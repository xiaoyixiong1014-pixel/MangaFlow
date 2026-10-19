"""生图参考图载荷工具模块（文件级说明）。

供各图像 provider 共用：把任务 meta 里的参考图地址（本地 /storage 相对
路径或外部 http(s) URL）解析为渠道可消费的载荷——外部网关无法访问本
服务的 /storage 静态目录，本地文件按 video_wan 首帧同款模式转为
Base64 data URI 内联上传，外部 URL 原样透传。

函数级说明：
- resolve_reference_data_uri：单张参考图 URL → data URI / 外部 URL / None。
"""

import base64
import logging
from typing import Optional

from app.core.config import settings

logger = logging.getLogger("mangaflow.providers.reference")


def resolve_reference_data_uri(reference_image_url: Optional[str]) -> Optional[str]:
    """把参考图 URL 解析为渠道可消费的载荷（本地 /storage 路径转 Base64 data URI）。

    Args:
        reference_image_url: 参考图 URL（/storage/... 本地图或外部 URL，可空）。

    Returns:
        Optional[str]: data URI / 外部 URL / None（入参为空）。

    Raises:
        无：本地文件读取失败时告警并原样返回 URL（由渠道侧决定消费方式）。
    """
    if not reference_image_url:
        return None
    if not reference_image_url.startswith("/storage/"):
        return reference_image_url
    rel = reference_image_url.removeprefix("/storage/").lstrip("/")
    local = settings.storage_path() / rel
    try:
        raw = local.read_bytes()
    except OSError:
        logger.warning("参考图本地文件不存在，原样透传 URL：%s", reference_image_url)
        return reference_image_url
    suffix = local.suffix.lower().lstrip(".") or "png"
    mime = "jpeg" if suffix in ("jpg", "jpeg") else suffix
    return f"data:image/{mime};base64," + base64.b64encode(raw).decode()
