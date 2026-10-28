"""BGM 曲库路由模块（文件级说明）。

实现平台曲库背景音乐的管理接口（Task 5.2，P-14）：
- GET    /api/bgm        曲库文件列表（登录用户，名称/时长/大小）
- POST   /api/bgm        上传曲库文件（运营方，落盘 storage/bgm/）
- DELETE /api/bgm/{name} 下架曲库文件（运营方）

说明：
- 曲库为纯文件目录（storage/bgm，不入库），init_db 保证目录与 README 存在；
- 用户自定义 BGM 复用既有 /api/media/upload 上传，compose 请求以 bgm.url
  （/storage/bgm/... 或 /storage/uploads/...）直传，本路由仅管理平台曲库；
- 文件名参数做路径穿越防护（禁止 / \\ ..），扩展名白名单校验。
"""

import asyncio
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import List

import aiofiles
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user, require_operator
from app.models import User
from app.schemas.bgm import BgmItem, BgmUploadResponse

router = APIRouter(prefix="/api/bgm", tags=["bgm"])

# 允许入库的音频扩展名白名单
_BGM_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
# 曲库单文件大小上限（与媒体上传一致，50MB）
_MAX_BGM_SIZE = 50 * 1024 * 1024
# ffprobe 探测超时（秒）
_PROBE_TIMEOUT = 10


def _bgm_dir() -> Path:
    """获取曲库目录并确保存在（内部函数）。

    Returns:
        Path: storage/bgm 绝对路径。

    Raises:
        无：目录创建失败由 OSError 向上抛出。
    """
    directory = settings.storage_path() / "bgm"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _probe_duration(path: Path) -> float:
    """ffprobe 探测音频时长（内部函数）。

    Args:
        path: 音频文件路径。

    Returns:
        float: 时长秒数（保留两位小数）；ffprobe 不可用/探测失败返回 0.0。

    Raises:
        无：内部捕获全部异常返回 0.0。
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            timeout=_PROBE_TIMEOUT,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            return 0.0
        return round(float(proc.stdout.decode().strip() or 0), 2)
    except Exception:  # noqa: BLE001 —— 探测失败不影响列表主流程
        return 0.0


@router.get("", response_model=List[BgmItem])
async def list_bgm(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[BgmItem]:
    """列出曲库全部音频文件（名称/引用地址/大小/时长，按文件名排序）。

    Args:
        user: 当前登录用户。
        db: 请求级异步数据库会话（曲库为文件目录，本参数仅保持依赖注入一致）。

    Returns:
        List[BgmItem]: 曲库文件列表。

    Raises:
        无：目录缺失时按空列表处理。
    """
    directory = _bgm_dir()
    items: List[BgmItem] = []
    for file in sorted(directory.iterdir(), key=lambda p: p.name):
        if file.is_file() and file.suffix.lower() in _BGM_EXTS:
            # ffprobe 为同步子进程调用，放线程池执行，避免阻塞事件循环
            # （多文件逐个探测时累计秒级，会拖慢同 worker 的全部请求）
            duration = await asyncio.to_thread(_probe_duration, file)
            items.append(BgmItem(
                name=file.name,
                url=f"/storage/bgm/{file.name}",
                size=file.stat().st_size,
                duration=duration or None,
            ))
    return items


@router.post("", response_model=BgmUploadResponse)
async def upload_bgm(
    file: UploadFile = File(...),
    operator: User = Depends(require_operator),
) -> BgmUploadResponse:
    """运营方上传曲库文件（≤50MB，扩展名白名单，防重名随机后缀）。

    Args:
        file: 上传的音频文件。
        operator: 运营方身份（依赖注入校验）。

    Returns:
        BgmUploadResponse: 存储文件名 / 引用地址 / 字节数。

    Raises:
        HTTPException: 400 —— 扩展名非法、文件为空或超过 50MB。
    """
    original = file.filename or ""
    ext = Path(original).suffix.lower()
    if ext not in _BGM_EXTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"仅支持 {'/'.join(_BGM_EXTS)} 格式的音频文件",
        )
    # 文件名净化：保留中文/字母/数字/下划线/连字符，防止路径穿越与特殊字符
    safe_stem = re.sub(r"[^\w\u4e00-\u9fff-]", "_", Path(original).stem)[:50] or "bgm"
    store_name = f"{safe_stem}_{uuid.uuid4().hex[:8]}{ext}"
    abs_path = _bgm_dir() / store_name

    size = 0
    try:
        async with aiofiles.open(abs_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_BGM_SIZE:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="文件大小超过 50MB 限制",
                    )
                await out.write(chunk)
    except HTTPException:
        # 超限时清理半成品文件后再抛出
        abs_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    if size == 0:
        abs_path.unlink(missing_ok=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="上传文件为空")
    return BgmUploadResponse(name=store_name, url=f"/storage/bgm/{store_name}", size=size)


@router.delete("/{name}")
async def delete_bgm(
    name: str,
    operator: User = Depends(require_operator),
) -> dict:
    """运营方下架曲库文件（物理删除；路径穿越防护）。

    Args:
        name: 曲库文件名。
        operator: 运营方身份（依赖注入校验）。

    Returns:
        dict: {"message": ...}。

    Raises:
        HTTPException: 400 —— 文件名含路径穿越字符；404 —— 文件不存在或非音频。
    """
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件名非法")
    path = _bgm_dir() / name
    if not path.is_file() or path.suffix.lower() not in _BGM_EXTS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="曲库文件不存在")
    path.unlink()
    return {"message": f"曲库文件 {name} 已下架"}
