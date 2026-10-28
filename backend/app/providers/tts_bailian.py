"""阿里云百炼 TTS 提供方模块（文件级说明）。

通过 dashscope SDK 的 WebSocket 语音合成 API（dashscope.audio.tts_v2.
SpeechSynthesizer）调用专属实例的 qwen-audio-3.1-tts-flash 真实合成：
- 端点推导：渠道 base_url（https://ws-xxx.../compatible-mode/v1）→
  wss://ws-xxx.../api-ws/v1/inference（qwen-audio-3.1-tts-flash 仅提供
  WebSocket 端点，OpenAI 兼容 HTTP 的 /audio/speech 与 /models 均不含该模型，
  2026-10-12 实测）；
- 音色映射：业务音色标识（male_01/female_01 等）→ qwen-audio-3.1-tts-flash
  专属音色列表（全部带 _v3.1 后缀，如 longanhuan_v3.1；Cherry/Ethan 等
  qwen3-tts 系音色会触发 [cosyvoice:]Engine error 411，2026-10-12 实测）；
- 输出格式：WAV 22050Hz mono 16bit，wave 模块读取实际帧数计算时长
  （RD-31 回写分镜实际时长的依据），解析失败降级 len(text)*0.18 估算并在
  meta 中标注 estimated=True；
- 并发约束：dashscope.api_key 为 SDK 进程级全局状态，模块级锁保护
  「设置 key + 构造 synthesizer」临界区（WebSocket 连接与鉴权在构造时
  建立，call() 仅投递文本，故 call 移出锁外不引入 Key 竞态）；
- 挂死防护（2026-10-19）：专属实例 WebSocket 偶发挂起（握手成功但响应
  永不返回）时，asyncio.wait_for 对 to_thread 的取消无法中断底层线程
  （_cancel_and_wait 需等线程结束），任务会永久卡 running。故 call() 以
  独立看门狗线程执行 + join 硬超时：超时放弃本次合成（返回 None 降级
  下一渠道），挂起线程为 daemon 不阻塞进程；锁获取同样带超时，拿不到
  锁直接返回 None，避免多任务在等锁处连环卡死。

DashScope 原生 HTTP 分支（2026-10-28）：渠道 protocol="dashscope" 时走
官方 SpeechSynthesizer HTTP 端点（POST {root}/api/v1/services/audio/tts/
SpeechSynthesizer，input.text_prompt + input.format 口径，响应
output.audio.url 为临时音频地址即时下载）——面向仅提供 HTTP 端点的
新一代模型（如 qwen-audio-3.1-tts-next，无 WebSocket 推理端点）；音色
首请求携带映射后的 input.voice，非鉴权类 4xx 时去 voice 重试一次兜底。
语速/音调参数该端点不支持，忽略。

任何失败返回 None（不抛出），由调用方（tasks/runner.py）降级到下一渠道。
函数签名与 mock TTS 渠道对齐（text + voice + out_dir + channel → url +
duration_sec），channel 为 model_router.ResolvedChannel。
"""

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlunparse, urlparse

from app.services.model_router import ResolvedChannel

logger = logging.getLogger("mangaflow.providers.tts_bailian")

# 生成结果 URL 前缀：main.py 已将 storage 目录 mount 到 /storage
_URL_PREFIX = "/storage/generated"
# 估算时长系数：每字 0.18 秒（与 tts_mock 一致）
_SECONDS_PER_CHAR = 0.18
# 缺省音色：业务音色标识不在映射表时的兜底（多语言女声）
_DEFAULT_VOICE = "longanhuan_v3.1"

# 锁获取超时（秒）：拿不到锁视为渠道繁忙，返回 None 降级，避免多任务连环等锁卡死
_LOCK_TIMEOUT = 30.0
# 单次合成看门狗超时（秒）：call() 线程超时未返回即放弃本次合成（返回 None 降级）
_CALL_TIMEOUT = 80.0
# 外层 asyncio.wait_for 兜底超时（秒）：大于锁+看门狗最坏耗时，仅防线程级异常
_OUTER_TIMEOUT = 150.0

# DashScope 原生 HTTP TTS 端点路径（相对渠道根域名）：qwen-audio-3.1-tts-next
# 等新一代模型仅提供该 HTTP 端点，无 WebSocket 推理端点
_HTTP_TTS_PATH = "/api/v1/services/audio/tts/SpeechSynthesizer"
# DashScope HTTP TTS 单次请求超时（秒）：同步端点整句合成较慢
_HTTP_TIMEOUT = 180.0
# DashScope HTTP 音频结果下载超时（秒）
_HTTP_DOWNLOAD_TIMEOUT = 120.0

# 业务音色标识 → qwen-audio-3.1-tts-flash 音色名映射
# （音色列表来源：阿里云百炼 Qwen-Audio-TTS 音色文档，2026-10-12）
_VOICE_MAP: Dict[str, str] = {
    "male_01": "anmingyuan_v3.1",      # 男声：清晰自然（旁白/解说）
    "male_02": "longhuohuo_v3.1",      # 男声：顽皮少年（角色音）
    "female_01": "longanhuan_v3.1",    # 女声：多语言/方言
    "female_02": "longanlingxin_v3.1", # 女声：多语言
    "narrator": "anmingyuan_v3.1",     # 旁白：男声解说
}

# SDK 全局 api_key 竞态保护锁（见模块 docstring 并发约束说明）
_SYNTH_LOCK = threading.Lock()


def _call_with_watchdog(synthesizer: Any, text: str) -> Optional[bytes]:
    """以独立看门狗线程执行 SDK 合成调用并施加硬超时（内部函数）。

    asyncio.wait_for 取消 to_thread 时需等底层线程结束（_cancel_and_wait
    语义），WebSocket 挂起时超时不生效；此处把 call() 放进独立 daemon
    线程，主线程 join 超时后直接放弃——挂起线程不阻塞调用方与进程退出
    （SDK 底层连接最终超时后会自行结束并释放资源）。

    Args:
        synthesizer: 已构造的 dashscope SpeechSynthesizer 实例。
        text: 待合成的台词文本。

    Returns:
        Optional[bytes]: 合成成功返回完整 WAV 音频字节；超时/异常/空结果
            返回 None。

    Raises:
        无：内部捕获全部异常收敛为 None。
    """
    holder: list = []

    def _invoke() -> None:
        """线程体：执行同步合成调用，结果（或异常占位）写入闭包 holder。"""
        try:
            holder.append(synthesizer.call(text))
        except Exception:  # noqa: BLE001 —— 线程内异常收敛为空结果
            logger.exception("SDK 语音合成线程内异常")

    worker = threading.Thread(target=_invoke, daemon=True, name="tts-bailian-call")
    worker.start()
    worker.join(_CALL_TIMEOUT)
    if worker.is_alive():
        logger.warning("SDK 语音合成超过 %ss 未返回，放弃本次调用（挂起线程为 daemon）", _CALL_TIMEOUT)
        return None
    return holder[0] if holder else None


def _ws_url_from_base(base_url: str) -> str:
    """由渠道 base_url 推导 WebSocket 推理端点（内部函数）。

    规则：https://host/xxx → wss://host/api-ws/v1/inference；
    base_url 本身为 wss:// 或 ws:// 时原样返回（路径缺失则拼接推理路径）。

    Args:
        base_url: 渠道配置的接口地址（OpenAI 兼容 HTTP 基址或 WS 地址）。

    Returns:
        str: DashScope WebSocket 推理端点 URL。

    Raises:
        无：解析异常时回退拼接结果。
    """
    try:
        if base_url.startswith("wss://") or base_url.startswith("ws://"):
            if "/api-ws/v1/inference" not in base_url:
                return base_url.rstrip("/") + "/api-ws/v1/inference"
            return base_url
        parsed = urlparse(base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunparse((scheme, parsed.netloc, "/api-ws/v1/inference", "", "", ""))
    except Exception:  # noqa: BLE001 —— 端点推导失败按原样回退
        return base_url


def _read_wav_duration(path: Path) -> Optional[float]:
    """解析 WAV 文件实际时长（内部函数）。

    dashscope 流式合成产出的 WAV 头部 size 字段为占位值（0x7fffffff，探测
    实测），标准 wave 模块会信任该值导致时长爆炸（NUMERIC 溢出），故自行
    解析：采样参数读自 fmt chunk，数据长度以实际文件剩余字节为准（头部
    size 合理且不越界时优先采用）。

    Args:
        path: WAV 文件绝对路径。

    Returns:
        Optional[float]: 时长（秒，浮点）；文件损坏/非 WAV/参数缺失返回 None。

    Raises:
        无：解析异常统一收敛为 None。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
            return None
        rate = channels = width = None
        pos = 12
        while pos + 8 <= len(raw):
            cid = raw[pos:pos + 4]
            size = int.from_bytes(raw[pos + 4:pos + 8], "little")
            body_start = pos + 8
            if cid == b"fmt " and body_start + 16 <= len(raw):
                # fmt 体：audio_format(2) channels(2) rate(4) byte_rate(4) block_align(2) bits(2)
                channels = int.from_bytes(raw[body_start + 2:body_start + 4], "little")
                rate = int.from_bytes(raw[body_start + 4:body_start + 8], "little")
                bits = int.from_bytes(raw[body_start + 14:body_start + 16], "little")
                width = bits // 8
            elif cid == b"data":
                remaining = len(raw) - body_start
                # 头部 size 占位不可信：仅在合理且不越界时采用，否则用实际剩余字节
                data_size = size if 0 < size <= remaining else remaining
                if rate and channels and width:
                    return round(data_size / (rate * channels * width), 2)
            pos = body_start + size + (size & 1)  # chunk 按 2 字节对齐
        return None
    except Exception:  # noqa: BLE001 —— 时长读取失败走估算降级
        return None


def _root_from_base(base_url: str) -> str:
    """由渠道 base_url 提取根域名（内部函数，口径与 image_dashscope 一致）。

    用户可能填根域名或带路径的实例地址，HTTP 端点统一从根域名拼接，
    避免 /compatible-mode 等路径与 /api/v1 叠加产生 404。

    Args:
        base_url: 渠道配置的接口地址（http/https）。

    Returns:
        str: 根域名（scheme://netloc）。

    Raises:
        无：解析异常时回退原值。
    """
    try:
        if "://" in base_url:
            scheme, rest = base_url.split("://", 1)
            return f"{scheme}://{rest.split('/', 1)[0]}"
        return base_url.rstrip("/")
    except Exception:  # noqa: BLE001 —— 解析失败按原值回退
        return base_url


def _http_synth_sync(
    text: str,
    voice_id: str,
    out_dir: Path,
    channel: ResolvedChannel,
) -> Optional[Dict]:
    """同步执行 DashScope 原生 HTTP 语音合成并落盘（内部函数，供 to_thread 调用）。

    面向仅提供 HTTP 端点的百炼语音模型（qwen-audio-3.1-tts-next 等，与
    WebSocket 推理的 qwen-audio-3.1-tts-flash 相区分）：POST {root}/api/v1/
    services/audio/tts/SpeechSynthesizer，请求体 input.text_prompt +
    input.format（官方示例口径），响应 output.audio.url 为临时音频地址，
    即时下载落盘（URL 含签名过期，不可长期缓存）。

    音色策略：首请求携带映射后的 input.voice（flash 系音色命名）；若非
    鉴权类 4xx 失败（模型不接受 voice 字段等），去 voice 重试一次——
    tts-next 支持在 text_prompt 内以指令描述音色，缺省同样可出声。

    Args:
        text: 待合成的台词文本。
        voice_id: 映射后的音色名（_VOICE_MAP 命中名或默认音色，可空串）。
        out_dir: 输出目录（storage/generated 的绝对路径）。
        channel: 已解析的候选渠道（protocol="dashscope"，base_url 为实例根域名）。

    Returns:
        Optional[Dict]: 成功返回
            {"url": "/storage/generated/tts_xxx.wav",
             "duration_sec": 时长秒数,
             "meta": {"provider": "bailian-dashscope", "channel_id": 渠道ID,
                      "voice": 音色名, ["estimated": True]}}；
            失败返回 None。

    Raises:
        无：全部异常收敛为 None（由调用方降级下一渠道）。
    """
    import httpx

    url = f"{_root_from_base(channel.base_url)}{_HTTP_TTS_PATH}"
    headers = {"Authorization": f"Bearer {channel.api_key}", "Content-Type": "application/json"}

    def _payload(with_voice: bool) -> Dict:
        """构造请求体（内部函数）：voice 字段按重试阶段开关。

        Args:
            with_voice: 是否携带 input.voice。

        Returns:
            Dict: DashScope SpeechSynthesizer 请求体。
        """
        input_body: Dict[str, str] = {"text_prompt": text, "format": "wav"}
        if with_voice and voice_id:
            input_body["voice"] = voice_id
        return {"model": channel.model, "input": input_body}

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            audio_url: Optional[str] = None
            # 音色两段式重试：先带 voice，非鉴权类失败则去 voice 再试一次
            for with_voice in (True, False):
                resp = client.post(url, headers=headers, json=_payload(with_voice))
                if resp.status_code in (401, 403):
                    logger.warning("渠道 %s 语音合成鉴权失败（HTTP %s）", channel.name, resp.status_code)
                    return None
                if resp.status_code >= 400:
                    if with_voice:
                        logger.warning(
                            "渠道 %s 语音合成带 voice 请求失败（HTTP %s），去 voice 重试：%s",
                            channel.name, resp.status_code, resp.text[:200])
                        continue
                    logger.warning("渠道 %s 语音合成失败（HTTP %s，model=%s）：%s",
                                   channel.name, resp.status_code, channel.model, resp.text[:300])
                    return None
                data = resp.json() or {}
                audio_field = (data.get("output") or {}).get("audio")
                # 官方形态 output.audio.url；兼容 output.audio 直接为字符串
                audio_url = audio_field.get("url") if isinstance(audio_field, dict) else audio_field
                if audio_url:
                    break
                if not with_voice:
                    logger.warning("渠道 %s 语音合成响应缺少音频 URL", channel.name)
                    return None
                logger.warning("渠道 %s 语音合成响应缺少音频 URL，去 voice 重试", channel.name)
            if not audio_url:
                return None
            # OSS 临时签名 URL 须即时下载落盘
            dl = client.get(audio_url, timeout=_HTTP_DOWNLOAD_TIMEOUT)
        if dl.status_code != 200 or not dl.content:
            logger.warning("渠道 %s 语音结果下载失败（HTTP %s）", channel.name, dl.status_code)
            return None

        filename = f"tts_{int(time.time())}_{abs(hash((text or '', voice_id))) % 100000}.wav"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / filename
        path.write_bytes(dl.content)

        # 时长：优先真实帧数计算，失败按文本长度估算（meta 标注 estimated）
        duration = _read_wav_duration(path)
        meta: Dict = {"provider": "bailian-dashscope", "channel_id": channel.channel_id, "voice": voice_id}
        if duration is None:
            duration = round(min(max(len(text or ""), 1) * _SECONDS_PER_CHAR, 60.0), 2)
            meta["estimated"] = True

        return {"url": f"{_URL_PREFIX}/{filename}", "duration_sec": duration, "meta": meta}
    except Exception as exc:  # noqa: BLE001 —— provider 失败由调用方降级处理
        logger.warning("渠道 %s DashScope HTTP 语音合成异常：%s", channel.name, exc)
        return None


def _synth_sync(
    text: str,
    voice_id: str,
    out_dir: Path,
    channel: ResolvedChannel,
    speed: float = 1.0,
    pitch: float = 1.0,
) -> Optional[Dict]:
    """同步执行一次 WebSocket 语音合成并落盘（内部函数，供 to_thread 调用）。

    Args:
        text: 待合成的台词文本。
        voice_id: qwen-audio-3.1-tts-flash 音色名（已由 _VOICE_MAP 映射）。
        out_dir: 输出目录（storage/generated 的绝对路径）。
        channel: 已解析的候选渠道（含 base_url/api_key/model）。
        speed: 语速倍率（voice_note 推导，0.5~2.0）。
        pitch: 音调倍率（voice_note 推导，0.5~2.0）。

    Returns:
        Optional[Dict]: 成功返回
            {"url": "/storage/generated/tts_xxx.wav",
             "duration_sec": 时长秒数,
             "meta": {"provider": "bailian", "channel_id": 渠道ID,
                      "voice": 模型音色名, ["estimated": True]}}；
            空结果返回 None。

    Raises:
        Exception: dashscope SDK 调用异常（由调用方 generate_tts_real 收敛）。
    """
    import dashscope
    from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

    # 锁保护「设置 key + 构造 synthesizer」临界区：WebSocket 连接与鉴权在
    # 构造时完成，call() 仅投递文本，移出锁外不引入 Key 竞态；锁获取带
    # 超时，渠道忙时直接降级（防止挂起线程持锁导致后续任务连环卡死）
    if not _SYNTH_LOCK.acquire(timeout=_LOCK_TIMEOUT):
        logger.warning("渠道 %s 合成锁获取超时（%ss），本次跳过", channel.name, _LOCK_TIMEOUT)
        return None
    try:
        dashscope.api_key = channel.api_key
        try:
            # speech_rate/pitch_rate：dashscope tts_v2 SDK 语速/音调参数
            #（voice_note 推导；渠道不支持该参数时回退无参构造）
            synthesizer = SpeechSynthesizer(
                model=channel.model,
                voice=voice_id,
                format=AudioFormat.WAV_22050HZ_MONO_16BIT,
                url=_ws_url_from_base(channel.base_url),
                speech_rate=speed,
                pitch_rate=pitch,
            )
        except TypeError:
            synthesizer = SpeechSynthesizer(
                model=channel.model,
                voice=voice_id,
                format=AudioFormat.WAV_22050HZ_MONO_16BIT,
                url=_ws_url_from_base(channel.base_url),
            )
    finally:
        _SYNTH_LOCK.release()
    # 合成调用在锁外执行：独立看门狗线程 + 硬超时（WebSocket 挂起时放弃
    # 本次调用，不阻塞锁与其他任务）
    audio = _call_with_watchdog(synthesizer, text)
    if not audio:
        logger.warning("渠道 %s 语音合成返回空音频", channel.name)
        return None

    filename = f"tts_{int(time.time())}_{abs(hash((text or '', voice_id))) % 100000}.wav"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_bytes(audio)

    # 时长：优先真实帧数计算，失败按文本长度估算（meta 标注 estimated）
    duration = _read_wav_duration(path)
    meta: Dict = {"provider": "bailian", "channel_id": channel.channel_id, "voice": voice_id}
    if duration is None:
        duration = round(min(max(len(text or ""), 1) * _SECONDS_PER_CHAR, 60.0), 2)
        meta["estimated"] = True

    return {"url": f"{_URL_PREFIX}/{filename}", "duration_sec": duration, "meta": meta}


async def generate_tts_real(
    text: str,
    voice: str,
    out_dir: Path,
    channel: ResolvedChannel,
    speed: float = 1.0,
    pitch: float = 1.0,
) -> Optional[Dict]:
    """调用百炼渠道真实合成一段配音 WAV（按渠道协议自动分派）。

    分派规则：protocol="dashscope" 走 DashScope 原生 HTTP SpeechSynthesizer
    端点（qwen-audio-3.1-tts-next 等新一代模型）；其余协议（openai 兼容
    渠道登记百炼 TTS）走 dashscope SDK WebSocket 推理（qwen-audio-3.1-tts-
    flash 专属实例）。HTTP 端点不支持语速/音调参数，speed/pitch 仅 WS 路径生效。

    Args:
        text: 待合成的台词文本。
        voice: 业务音色标识（male_01/female_01/narrator 等，按 _VOICE_MAP 映射）。
        out_dir: 输出目录（storage/generated 的绝对路径）。
        channel: 已解析的候选渠道（含 base_url/api_key/model）。
        speed: 语速倍率（voice_note 推导，0.5~2.0；仅 WebSocket 路径生效）。
        pitch: 音调倍率（voice_note 推导，0.5~2.0；仅 WebSocket 路径生效）。

    Returns:
        Optional[Dict]: 成功返回
            {"url": "/storage/generated/tts_xxx.wav",
             "duration_sec": 时长秒数,
             "meta": {"provider": "bailian"/"bailian-dashscope", "channel_id": 渠道ID,
                      "voice": 模型音色名, ["estimated": True]}}；
            请求失败/响应异常返回 None（由调用方降级到下一渠道）。

    Raises:
        无：内部捕获全部异常并返回 None，不向上抛出。
    """
    voice_id = _VOICE_MAP.get(voice, _DEFAULT_VOICE)
    try:
        if channel.protocol == "dashscope":
            # DashScope 原生 HTTP 端点（qwen-audio-3.1-tts-next 等）：同步
            # HTTP 调用放线程池避免阻塞事件循环
            return await asyncio.wait_for(
                asyncio.to_thread(_http_synth_sync, text, voice_id, out_dir, channel),
                timeout=_OUTER_TIMEOUT,
            )
        # SDK 为同步 WebSocket 客户端，放线程池避免阻塞事件循环；
        # 锁与看门狗均在 _synth_sync 内部生效（本 wait_for 仅为线程级
        # 异常的最后防线，超时值大于锁+看门狗最坏耗时）
        return await asyncio.wait_for(
            asyncio.to_thread(_synth_sync, text, voice_id, out_dir, channel, speed, pitch),
            timeout=_OUTER_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 —— provider 失败由调用方降级处理
        logger.warning("渠道 %s 语音合成异常：%s", channel.name, exc)
        return None
