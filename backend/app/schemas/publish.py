"""发布域 Pydantic Schema 模块（文件级说明）。

定义成片合成请求、成片版本输出、审核记录输出、发布（分享链接）请求与
响应、公开分享页数据模型。
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class SubtitleStyle(BaseModel):
    """字幕样式（Task 5.1，P-16）。

    Attributes:
        font_size: 字号（ASS FontSize，10~72）。
        color: 主字幕颜色（ASS PrimaryColour，&HBBGGRR 格式，缺省白色）。
        position: 字幕位置 bottom 底部 / middle 居中 / top 顶部。
        bilingual: 是否双语字幕（P-16；True 时原文在上 + 英文译文在下，
            LLM 翻译失败自动降级为单行原文，不影响合成任务成功）。
    """

    font_size: int = Field(default=24, ge=10, le=72)
    color: str = Field(default="&H00FFFFFF", max_length=16)
    position: Literal["bottom", "middle", "top"] = "bottom"
    bilingual: bool = False


class BgmOption(BaseModel):
    """背景音乐参数（Task 5.2，P-14）。

    Attributes:
        url: BGM 资源地址（/storage/bgm/... 曲库或 /storage/uploads/... 用户上传，
            可空表示不加背景音乐）。
        volume: 音量系数（0~1，原声音量保持不变，仅作用于 BGM 轨）。
    """

    url: Optional[str] = Field(default=None, max_length=255)
    volume: float = Field(default=0.3, ge=0.0, le=1.0)


class TransitionOption(BaseModel):
    """转场参数（Task 5.3，P-15）。

    Attributes:
        type: 转场类型（xfade 滤镜名；本阶段实现按 fade 处理，其余取值接受
            但同样应用 fade，注释与文档说明）。
        duration: 转场时长（秒，0.1~3.0）。
    """

    type: str = Field(default="fade", max_length=30)
    duration: float = Field(default=0.5, ge=0.1, le=3.0)


class ComposeCreateRequest(BaseModel):
    """成片合成请求。

    Attributes:
        resolution: 输出分辨率 720P/1080P/2K。
        aspect_ratio: 画幅比例 9:16/16:9/1:1（缺省沿用项目配置）。
        mode: 合成模式 incremental（增量）/ full（全量），映射为任务 phase。
        storyboard_ids: 增量合成指定分镜 ID 清单（仅 incremental 生效，Task 5.3）。
        episode_id: 可选剧集 ID（S-06；传入时仅合成该剧集的分镜并将成片版本
            绑定该剧集，缺省为项目全部分镜，行为与既有接口一致）。
        subtitles: 是否烧录字幕（Task 5.1，缺省关闭）。
        subtitle_style: 字幕样式（可空用默认样式）。
        bgm: 背景音乐参数（可空表示不加）。
        transition: 转场参数（可空表示不加转场，直接拼接）。
        intro_card: 是否插入片头标题卡（剧名+集数，2.5s 黑底白字，缺省关闭）。
        outro_card: 是否插入剧终片尾卡（2s 黑底白字，缺省关闭）。
    """

    resolution: Literal["720P", "1080P", "2K"] = "720P"
    aspect_ratio: Optional[Literal["9:16", "16:9", "1:1"]] = None
    mode: Literal["incremental", "full"] = "full"
    storyboard_ids: Optional[List[int]] = Field(default=None, max_length=200)
    episode_id: Optional[int] = None
    subtitles: bool = False
    subtitle_style: Optional[SubtitleStyle] = None
    bgm: Optional[BgmOption] = None
    transition: Optional[TransitionOption] = None
    intro_card: bool = False
    outro_card: bool = False


class OutputOut(BaseModel):
    """成片版本输出模型。

    Attributes:
        id: 版本记录 ID。
        version_no: 版本号。
        url: 成片地址。
        resolution: 分辨率。
        aspect_ratio: 画幅比例。
        duration: 时长（秒）。
        watermark_id: 隐形水印标识。
        is_current: 是否当前版本。
        share_token: 分享令牌（未发布为 None）。
        share_expires_at: 分享过期时间。
        has_password: 是否设置了提取码（不回传明文密码）。
        share_revoked: 分享是否已撤销。
        created_at: 创建时间。
    """

    id: int
    version_no: int
    url: str
    resolution: str
    aspect_ratio: str
    duration: int
    watermark_id: Optional[str] = None
    is_current: bool
    share_token: Optional[str] = None
    share_expires_at: Optional[datetime] = None
    has_password: bool = False
    share_revoked: bool = False
    created_at: Optional[datetime] = None

    @classmethod
    def from_output(cls, o) -> "OutputOut":
        """从 ORM 成片对象构造输出模型（密码仅回传是否设置）。

        Args:
            o: FilmOutput ORM 对象。

        Returns:
            OutputOut: 成片版本输出实例。

        Raises:
            无：字段均为简单映射，不产生异常路径。
        """
        return cls(
            id=o.id,
            version_no=o.version_no,
            url=o.url,
            resolution=o.resolution,
            aspect_ratio=o.aspect_ratio,
            duration=o.duration,
            watermark_id=o.watermark_id,
            is_current=o.is_current,
            share_token=o.share_token,
            share_expires_at=o.share_expires_at,
            has_password=bool(o.share_password),
            share_revoked=o.share_revoked,
            created_at=o.created_at,
        )


class ReviewOut(BaseModel):
    """审核记录输出模型。

    Attributes:
        id: 记录 ID。
        gate: 审核闸门 generate/compose/publish。
        target_type: 目标类型 text/image/video/film。
        target_id: 目标 ID（多态）。
        result: 机审结果 pass/suspect/violate。
        violation_type: 违规类型。
        manual_status: 人工复核状态。
        suggestion: 可操作建议。
        created_at: 创建时间。
    """

    id: int
    gate: str
    target_type: str
    target_id: Optional[int] = None
    result: str
    violation_type: Optional[str] = None
    manual_status: Optional[str] = None
    suggestion: Optional[str] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_record(cls, r) -> "ReviewOut":
        """从 ORM 审核记录构造输出模型。

        Args:
            r: ReviewRecord ORM 对象。

        Returns:
            ReviewOut: 审核记录输出实例。

        Raises:
            无：字段均为简单映射，不产生异常路径。
        """
        return cls(
            id=r.id,
            gate=r.gate,
            target_type=r.target_type,
            target_id=r.target_id,
            result=r.result,
            violation_type=r.violation_type,
            manual_status=r.manual_status,
            suggestion=r.suggestion,
            created_at=r.created_at,
        )


class PublishRequest(BaseModel):
    """发布（生成分享链接）请求。

    Attributes:
        output_id: 要发布的成片版本 ID。
        share_expires_days: 分享有效期（天），默认 7。
        share_password: 提取码（可空，设置后访问需携带）。
    """

    output_id: int
    share_expires_days: int = Field(default=7, ge=1, le=365)
    share_password: Optional[str] = Field(default=None, max_length=50)


class PublishResponse(BaseModel):
    """发布响应。

    Attributes:
        share_url: 分享链接路径（/share/{token}）。
        expires_at: 分享过期时间。
    """

    share_url: str
    expires_at: datetime


class ShareInfoResponse(BaseModel):
    """公开分享页数据模型（L-04 分享页数据源，免认证访问）。

    Attributes:
        title: 项目标题。
        video_url: 成片地址。
        resolution: 分辨率。
        duration: 时长（秒）。
        watermark_id: 隐形水印标识（溯源）。
        published_at: 发布时间（成片版本更新时间）。
    """

    title: str
    video_url: str
    resolution: str
    duration: int
    watermark_id: Optional[str] = None
    published_at: Optional[datetime] = None
