"""BGM 曲库 Pydantic Schema 模块（文件级说明）。

定义曲库文件列表项与上传响应模型（Task 5.2，P-14 背景音乐曲库）。
"""

from typing import Optional

from pydantic import BaseModel


class BgmItem(BaseModel):
    """曲库文件列表项。

    Attributes:
        name: 文件名（曲库内唯一，DELETE 接口的定位键）。
        url: 播放/引用地址（/storage/bgm/<name>，可直接用于 compose.bgm.url）。
        size: 文件字节数。
        duration: 时长（秒，ffprobe 探测；不可用时为 None）。
    """

    name: str
    url: str
    size: int
    duration: Optional[float] = None


class BgmUploadResponse(BaseModel):
    """曲库上传响应。

    Attributes:
        name: 存储文件名（防重命名后）。
        url: 引用地址。
        size: 文件字节数。
    """

    name: str
    url: str
    size: int
