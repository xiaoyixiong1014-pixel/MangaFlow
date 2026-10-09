"""媒体文件服务模块（文件级说明）。

负责上传文件的落盘与媒体类型推断：
- 上传目录：{storage_dir}/uploads/{yyyy-mm}/{uuid}.{ext}
- 大小限制：50MB（超限中断写盘并抛出 HTTPException）
- 媒体类型：优先使用调用方显式传入的表单值，否则按文件扩展名推断
"""

import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

import aiofiles
from fastapi import HTTPException, UploadFile, status

from app.core.config import settings

# 上传文件大小上限（50MB）
MAX_UPLOAD_SIZE = 50 * 1024 * 1024

# 扩展名 -> 媒体类型映射（用于无法显式指定 media_type 时的推断）
_EXT_TYPE_MAP = {
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image",
    "webp": "image", "bmp": "image", "svg": "image",
    "mp4": "video", "mov": "video", "avi": "video", "mkv": "video",
    "webm": "video", "flv": "video",
    "mp3": "audio", "wav": "audio", "m4a": "audio", "aac": "audio",
    "flac": "audio", "ogg": "audio",
    "txt": "text", "md": "text", "json": "text", "srt": "text",
}

# 合法媒体类型集合
MEDIA_TYPES = ("image", "video", "audio", "text")


def infer_media_type(filename: Optional[str], explicit: Optional[str]) -> str:
    """推断上传文件的媒体类型。

    优先级：显式表单值 > 扩展名推断；两者均不可用时抛出参数错误。

    Args:
        filename: 原始文件名（用于扩展名推断）。
        explicit: 表单显式指定的 media_type（可空）。

    Returns:
        str: 媒体类型（image / video / audio / text）。

    Raises:
        HTTPException: 400 —— 显式值非法，或扩展名无法识别且未显式指定。
    """
    if explicit:
        if explicit not in MEDIA_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="media_type 仅支持 image/video/audio/text",
            )
        return explicit
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()
        inferred = _EXT_TYPE_MAP.get(ext)
        if inferred:
            return inferred
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="无法识别媒体类型，请指定 media_type 或使用常见扩展名的文件",
    )


async def save_upload_file(file: UploadFile, media_type: str) -> Tuple[str, int]:
    """将上传文件流式写入本地存储目录。

    目录规则：{storage_dir}/uploads/{yyyy-mm}/{uuid}.{ext}；
    写入过程中累计字节数，超过 50MB 立即中断并清理半成品文件。

    Args:
        file: FastAPI 上传文件对象。
        media_type: 已确认的媒体类型（仅用于异常提示与落库，不影响目录结构）。

    Returns:
        Tuple[str, int]: (相对存储路径 url, 文件字节数)。
            url 形如 "uploads/2026-10/xxxx.png"，可直接写入 media_assets.url。

    Raises:
        HTTPException: 400 —— 文件为空、超过 50MB 限制或文件名缺少扩展名；
        OSError: 磁盘写入失败时向上抛出（由全局异常处理器兜底）。
    """
    original_name = file.filename or ""
    if "." not in original_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件名缺少扩展名")
    ext = original_name.rsplit(".", 1)[-1].lower()
    # 扩展名仅允许字母数字，防止路径穿越
    if not ext.isalnum():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件扩展名非法")

    now = datetime.now(timezone.utc)
    month_dir = now.strftime("%Y-%m")
    store_name = f"{uuid.uuid4().hex}.{ext}"
    # 相对存储根目录的路径（写入 media_assets.url）
    rel_url = f"uploads/{month_dir}/{store_name}"
    abs_dir = settings.storage_path() / "uploads" / month_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    abs_path = abs_dir / store_name

    size = 0
    try:
        async with aiofiles.open(abs_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)  # 1MB 分块读取
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_SIZE:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="文件大小超过 50MB 限制",
                    )
                await out.write(chunk)
    except HTTPException:
        # 超限时清理已写入的半成品文件后再抛出
        try:
            abs_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        await file.close()

    if size == 0:
        try:
            abs_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="上传文件为空")
    return rel_url, size
