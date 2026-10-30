"""MiniMax（海螺）视频生成提供方模块（文件级说明）。

通过 MiniMax 开放平台原生异步任务协议调用自有 Key 的海螺视频模型，
按模型自动选择 v1 / v2 双契约（2026-10-30 线上实测：Hailuo-3 仅在 v2
端点可用，v1 对其返回 2013 incorrect model param input）：

v1 契约（T2V-01 / I2V-01 / S2V-01 / Hailuo-02 家族）：
- 创建任务：POST {base}/v1/video_generation（body: model + prompt，带首帧
  图时追加 first_frame_image 为 data URI；无首帧退化为文生视频 T2V），
  响应 task_id（HTTP 200 包裹 base_resp 状态，status_code != 0 视为失败）；
- 轮询任务：GET {base}/v1/query/video_generation?task_id=xxx，status 为
  Queueing/Processing/Success/Fail；Success 时取 file.download_url。

v2 契约（Hailuo-3 等新模型，curl 实测要求必填 content 字段）：
- 创建任务：POST {base}/v2/video_generation，body: model + content 消息数组
  （[{type:"text",text:prompt}] + 图生视频追加
  {type:"image_url",image_url:[data_uri]}）；错误可能以
  {type:"error",error:{...}}（非 base_resp 包裹）返回，需双形态拦截；
- 轮询任务：GET {base}/v2/query/video_generation?task_id=xxx（由创建端点
  同构推导）；响应结构与 v1 不完全一致，状态与下载地址走递归防御解析
  （status / download_url / video_url）。

下载落盘：结果地址为临时链接，即时下载写 storage/generated。
地址归一：渠道 base_url 支持根域名（按模型名补 v1 或 v2）、带版本前缀
（…/v1、…/v2）或完整任务端点（…/v2/video_generation）三种填法。

与 DashScope wan 渠道（video_wan.py）的差异：MiniMax 创建/查询是自有契约
（非 X-DashScope-Async），任务 ID 从响应根取，时长/分辨率参数 MiniMax 按模型
档位内定，不透传 video_params（仅在任务 meta 溯源）。

任何失败返回 None（不抛出），由调用方（tasks/runner.py）降级到下一渠道。
函数签名与 wan 渠道对齐（prompt + duration + out_dir + seed + channel →
url + meta），channel 为 model_router.ResolvedChannel。
"""

import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.services.model_router import ResolvedChannel

logger = logging.getLogger("mangaflow.providers.video_minimax")

# 生成结果 URL 前缀：main.py 已将 storage 目录 mount 到 /storage
_URL_PREFIX = "/storage/generated"
# 创建任务单次请求超时（秒）：同步返回 task_id，通常数秒内
_HTTP_TIMEOUT = 60.0
# 结果视频下载超时（秒）
_DOWNLOAD_TIMEOUT = 300.0
# 首帧图片 MIME（关键帧 PNG，与 video_wan 同口径）
_FIRST_FRAME_MIME = "image/png"


# v1 契约模型家族关键字（小写匹配）：命中走 v1 端点与旧式 body
_V1_FAMILY_KEYWORDS = ("t2v", "i2v", "s2v", "hailuo-02", "hailuo-2")


def _is_v1_family(model: str) -> bool:
    """判定模型是否属于 v1 契约家族（内部函数）。

    Args:
        model: 渠道登记的模型 ID（如 T2V-01 / Hailuo-3）。

    Returns:
        bool: v1 家族模型（T2V/I2V/S2V/Hailuo-02 系列）返回 True；
            Hailuo-3 等新模型返回 False（走 v2 契约）。

    Raises:
        无：纯字符串判定，不产生异常路径。
    """
    lowered = str(model or "").strip().lower()
    return any(keyword in lowered for keyword in _V1_FAMILY_KEYWORDS)


def _find_value(node: object, keys: Tuple[str, ...], depth: int = 4) -> Optional[object]:
    """在未知嵌套结构的响应 JSON 中递归查找首个命中键的值（内部函数）。

    v2 契约的轮询响应结构与 v1 不完全一致（可能包 data 层或字段改名），
    对状态与下载地址采用递归防御解析，兼容官方后续结构微调。

    Args:
        node: 待查找的 JSON 节点（dict/list/标量）。
        keys: 目标键名元组（按顺序优先匹配）。
        depth: 递归深度上限（防超深结构死循环，默认 4 层足够）。

    Returns:
        Optional[object]: 首个命中键的值；未命中返回 None。

    Raises:
        无：类型不匹配按未命中处理，不抛出。
    """
    if depth < 0 or not isinstance(node, dict):
        return None
    for key in keys:
        if key in node:
            return node[key]
    for value in node.values():
        if isinstance(value, dict):
            found = _find_value(value, keys, depth - 1)
            if found is not None:
                return found
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    found = _find_value(item, keys, depth - 1)
                    if found is not None:
                        return found
    return None


def _endpoints_from_base(base_url: str, model: str) -> Tuple[str, str]:
    """由渠道 base_url 推导任务创建与查询端点（内部函数）。

    兼容三种填法：
    - 根域名（https://api.minimax.cn）→ 按模型名补版本前缀
      （v1 家族模型补 /v1，其余含 Hailuo-3 及未知新模型补 /v2）；
    - 带版本前缀（https://api.minimax.cn/v1 或 /v2）→ 原样拼接任务路径；
    - 完整任务端点（…/v2/video_generation）→ 直接作为创建端点。

    查询端点由创建端点同构替换推导（/video_generation → /query/video_generation，
    与官方 /v1/query/video_generation 契约一致，v2 同构沿用）。

    Args:
        base_url: 渠道配置的接口地址（http/https）。
        model: 渠道登记的模型 ID（根域名填法时用于选择契约版本）。

    Returns:
        Tuple[str, str]: (创建端点 URL, 查询端点 URL)。

    Raises:
        无：解析异常时回退按根域名 + 按模型名推导版本处理。
    """
    try:
        trimmed = base_url.rstrip().rstrip("/")
        if trimmed.endswith("/video_generation"):
            create = trimmed
        else:
            path = urlparse(trimmed).path
            if path:
                # 已带 /vN 版本前缀：原样拼任务路径
                create = f"{trimmed}/video_generation"
            else:
                # 根域名：按模型名选择契约版本
                version = "v1" if _is_v1_family(model) else "v2"
                create = f"{trimmed}/{version}/video_generation"
        query = create.replace("/video_generation", "/query/video_generation")
        return create, query
    except Exception:  # noqa: BLE001 —— 端点推导失败按根域名默认回退
        root = base_url.rstrip("/")
        version = "v1" if _is_v1_family(model) else "v2"
        return (f"{root}/{version}/video_generation",
                f"{root}/{version}/query/video_generation")


async def generate_video_minimax(
    prompt: str,
    duration_sec: float,
    out_dir: Path,
    seed: int,
    channel: ResolvedChannel,
    first_frame_path: Optional[Path] = None,
    video_params: Optional[Dict] = None,
    end_frame_path: Optional[Path] = None,
) -> Optional[Dict]:
    """调用 MiniMax 渠道（海螺系列）生成一段视频片段（异步任务轮询）。

    携带首帧本地关键帧时以 first_frame_image（data URI）走图生视频；缺失
    时退化为文生视频（prompt 驱动）。时长/分辨率等参数由所选 MiniMax 模型
    档位内定，本 provider 不透传（duration_sec/video_params 仅记录日志溯源），
    避免无效参数导致创建失败。

    Args:
        prompt: 视频提示词（描述画面动态）。
        duration_sec: 期望时长（秒；MiniMax 不支持显式指定，仅日志溯源）。
        out_dir: 输出目录（storage/generated 的绝对路径）。
        seed: 随机种子（一般传任务 ID，保证文件名唯一）。
        channel: 已解析的候选渠道（protocol="minimax"）。
        first_frame_path: 首帧图片本地路径（关键帧 PNG，可空=文生视频）。
        video_params: 视频参数（P-12，可空；MiniMax 契约不消费，仅溯源）。
        end_frame_path: 尾帧图片本地路径（P-08，可空；MiniMax 本 provider
            暂不支持尾帧控制，传入时忽略并记日志）。

    Returns:
        Optional[Dict]: 成功返回
            {"url": "/storage/generated/clip_xxx.mp4",
             "meta": {"provider": "minimax-video", "channel_id": 渠道ID,
                      "task_id": MiniMax任务ID}}；
            提交失败/轮询超时/任务 Fail/下载失败返回 None。

    Raises:
        无：内部捕获全部异常并返回 None，不向上抛出。
    """
    if end_frame_path and end_frame_path.is_file():
        logger.info("渠道 %s MiniMax 协议暂不支持尾帧控制，忽略尾帧：%s",
                    channel.name, end_frame_path.name)

    create_url, query_url = _endpoints_from_base(channel.base_url, channel.model)
    use_v2 = "/v2/" in create_url
    headers = {"Authorization": f"Bearer {channel.api_key}",
               "Content-Type": "application/json"}
    poll_headers = {"Authorization": f"Bearer {channel.api_key}"}

    # 首帧读取：v1 用 data URI（first_frame_image 字段）；v2 的 image_url
    # 仅接受公网 URL（data URI 触发 2013 Mismatch type of schema），需把
    # 本地首帧写盘后经 settings.public_base_url 暴露为公网直链
    data_uri: Optional[str] = None
    img_bytes: Optional[bytes] = None
    if first_frame_path and first_frame_path.is_file():
        try:
            img_bytes = first_frame_path.read_bytes()
            if img_bytes:
                data_uri = f"data:{_FIRST_FRAME_MIME};base64,{base64.b64encode(img_bytes).decode()}"
        except Exception:  # noqa: BLE001 —— 首帧读取失败降级文生视频
            logger.warning("渠道 %s 首帧图片读取失败，退化为文生视频：%s",
                           channel.name, first_frame_path)

    # v2 首帧公网化：写盘 storage/generated → {PUBLIC_BASE_URL}/storage 直链
    first_frame_url: Optional[str] = None
    if use_v2 and img_bytes:
        if settings.public_base_url:
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                ff_name = f"ff_{seed}_{int(time.time())}.png"
                (out_dir / ff_name).write_bytes(img_bytes)
                first_frame_url = (f"{settings.public_base_url.rstrip('/')}"
                                   f"{_URL_PREFIX}/{ff_name}")
            except Exception:  # noqa: BLE001 —— 首帧公网化失败退化文生视频
                logger.warning("渠道 %s v2 首帧公网化失败，退化文生视频：%s",
                               channel.name, first_frame_path)
        else:
            logger.warning(
                "渠道 %s v2 契约首帧需公网 URL（未配置 PUBLIC_BASE_URL），本次退化文生视频",
                channel.name)

    # 请求体按契约版本构造：v1 旧式字段；v2 必填 content 消息数组
    # （curl 实测 v2 缺 content 返回 2013 binding: expr_path=content）
    if use_v2:
        content: list = [{"type": "text", "text": prompt}]
        if first_frame_url:
            content.append({"type": "image_url", "image_url": [first_frame_url]})
        payload: Dict = {"model": channel.model, "content": content}
    else:
        payload = {"model": channel.model, "prompt": prompt}
        if data_uri:
            payload["first_frame_image"] = data_uri
    logger.info(
        "渠道 %s MiniMax 视频任务提交（%s，model=%s，首帧=%s，期望时长=%ss）",
        channel.name, "v2/content 契约" if use_v2 else "v1 契约",
        channel.model, bool(first_frame_url or data_uri), duration_sec)

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            # 创建任务（v1/v2 契约：HTTP 200 + base_resp.status_code 表状态；
            # v2 部分错误以 {"type":"error",...} 非 base_resp 形态返回，需双形态拦截）
            resp = await client.post(create_url, headers=headers, json=payload)
            if resp.status_code >= 400:
                logger.warning("渠道 %s MiniMax 视频任务创建失败（HTTP %s）：%s",
                               channel.name, resp.status_code, resp.text[:300])
                return None
            data = resp.json() or {}
            base_resp = data.get("base_resp") or {}
            status_code = int(base_resp.get("status_code", 0) or 0)
            if status_code != 0 or data.get("type") == "error":
                logger.warning("渠道 %s MiniMax 任务创建被拒（status_code=%s）：%s",
                               channel.name, status_code or data.get("error"),
                               str(base_resp.get("status_msg") or data.get("error"))[:200])
                return None
            # v2 响应可能包 data 层，task_id 递归防御取值
            task_id = str(data.get("task_id")
                          or _find_value(data, ("task_id",))
                          or "")
            if not task_id:
                logger.warning("渠道 %s MiniMax 创建响应缺少 task_id：%s",
                               channel.name, resp.text[:300])
                return None

            # 轮询任务状态（海螺生成一般 1~5 分钟，超时口径与 wan 渠道一致）
            deadline = time.monotonic() + settings.video_poll_timeout_seconds
            detail: Dict = {}
            while time.monotonic() < deadline:
                await asyncio.sleep(settings.video_poll_interval_seconds)
                poll = await client.get(query_url, headers=poll_headers,
                                        params={"task_id": task_id})
                if poll.status_code != 200:
                    logger.warning("渠道 %s MiniMax 轮询失败（HTTP %s）",
                                   channel.name, poll.status_code)
                    return None
                detail = poll.json() or {}
                if detail.get("type") == "error":
                    logger.warning("渠道 %s MiniMax 轮询被拒（error 形态）：%s",
                                   channel.name, str(detail)[:200])
                    return None
                poll_base = detail.get("base_resp") or {}
                if int(poll_base.get("status_code", 0) or 0) != 0:
                    logger.warning("渠道 %s MiniMax 轮询被拒（status_code=%s）：%s",
                                   channel.name, poll_base.get("status_code"),
                                   str(poll_base.get("status_msg"))[:200])
                    return None
                # 状态：v1 顶层 status；v2 结构未知走递归防御
                status = str(detail.get("status")
                             or _find_value(detail, ("status",))
                             or "").lower()
                if status == "success":
                    break
                if status in ("fail", "failed"):
                    logger.warning("渠道 %s MiniMax 任务 %s 失败：%s",
                                   channel.name, task_id, str(detail)[:300])
                    return None
            else:
                logger.warning("渠道 %s MiniMax 轮询超时（%ds，task_id=%s）",
                               channel.name, settings.video_poll_timeout_seconds, task_id)
                return None

            # 取结果地址：v1 官方形态 file.download_url / 顶层 file_download_url；
            # v2 结构未知，递归防御取 download_url / video_url（可能是数组）
            file_block = detail.get("file") or {}
            video_url = file_block.get("download_url") if isinstance(file_block, dict) else None
            video_url = (video_url
                         or detail.get("file_download_url")
                         or _find_value(detail, ("download_url", "video_url")))
            if isinstance(video_url, (list, tuple)):
                video_url = video_url[0] if video_url else None
            if not video_url:
                logger.warning("渠道 %s MiniMax 任务成功但缺少下载地址：%s",
                               channel.name, str(detail)[:300])
                return None
            dl = await client.get(video_url, timeout=_DOWNLOAD_TIMEOUT)
        if dl.status_code != 200 or not dl.content:
            logger.warning("渠道 %s MiniMax 结果下载失败（HTTP %s）",
                           channel.name, dl.status_code)
            return None

        filename = f"clip_{seed}_{int(time.time())}.mp4"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / filename).write_bytes(dl.content)
    except Exception as exc:  # noqa: BLE001 —— provider 失败由调用方降级处理
        logger.warning("渠道 %s MiniMax 视频生成异常：%s", channel.name, exc)
        return None

    return {
        "url": f"{_URL_PREFIX}/{filename}",
        "meta": {
            "provider": "minimax-video",
            "channel_id": channel.channel_id,
            "task_id": task_id,
        },
    }
