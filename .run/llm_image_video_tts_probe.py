# -*- coding: utf-8 -*-
"""LLM / 图像 / 视频 / TTS 五渠道真实连通性探测脚本（文件级注释）。

逐渠道发起最小化真实调用并记录 [PASS/FAIL]、耗时与关键信息：
1. 文本 LLM 主渠道（DeepSeek）：chat 一句"回复OK"；
2. 文本 LLM 兜底渠道（Agnes）：同上；
3. 图像渠道（Agnes）：generate_image_real 生成一张竖版图并落盘 storage/generated；
4. 视频渠道（Agnes）：generate_video_real 生成 5 秒竖版片段（异步任务轮询，
   总超时 settings.video_poll_timeout_seconds，可能耗时数分钟）；
5. TTS 渠道（百炼）：generate_tts_real 合成一段 WAV 配音。

退出码约定：0 仅当 LLM 主渠道 PASS（图像/视频/TTS 失败只告警——Key 有效性
无法预先保证，线上失败会自动降级 mock 渠道，不阻塞系统）。

使用 backend venv 的 python 运行：
    backend/.venv/Scripts/python.exe .run/llm_image_video_tts_probe.py
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

# 保证从任意工作目录运行都能导入 backend 的 app 包
_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(_BACKEND_DIR))

from openai import AsyncOpenAI  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.providers.image_agnes import generate_image_real  # noqa: E402
from app.providers.tts_bailian import generate_tts_real  # noqa: E402
from app.providers.video_agnes import generate_video_real  # noqa: E402
from app.services.model_router import ResolvedChannel  # noqa: E402

# provider 内部的 warning 日志直接透传到控制台，便于呈现具体失败原因
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

RESULTS = []  # [(渠道名, PASS/FAIL, 耗时秒, 关键信息)]


def record(name: str, ok: bool, elapsed: float, detail: str) -> None:
    """记录并打印单渠道探测结果。

    Args:
        name: 渠道显示名。
        ok: 是否探测成功。
        elapsed: 耗时（秒）。
        detail: 关键信息（模型名/产出 URL/错误摘要）。

    Returns:
        None。

    Raises:
        无。
    """
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name} | {elapsed:.1f}s | {detail}", flush=True)
    RESULTS.append((name, tag, elapsed, detail))


async def probe_chat_llm(label: str, base_url: str, api_key: str, model: str) -> None:
    """探测文本 LLM 渠道：发送一句"回复OK"（内部函数）。

    Args:
        label: 渠道显示名。
        base_url: OpenAI 兼容 API 地址。
        api_key: API Key。
        model: 模型名。

    Returns:
        None（结果经 record 写入 RESULTS）。

    Raises:
        无：异常统一收敛为 FAIL 记录。
    """
    start = time.monotonic()
    try:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=60.0, max_retries=0)
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "回复OK"}],
            temperature=0,
            max_tokens=16,
        )
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        record(label, bool(text), time.monotonic() - start, f"model={model} reply={text[:40]!r}")
    except Exception as exc:  # noqa: BLE001 —— 探测需捕获一切异常
        record(label, False, time.monotonic() - start, f"model={model} err={str(exc)[:160]}")


async def probe_image() -> None:
    """探测 Agnes 图像渠道：生成"一只橘猫坐在窗边"（内部函数）。

    Returns:
        None（结果经 record 写入 RESULTS）。

    Raises:
        无：provider 返回 None 或异常统一收敛为 FAIL 记录。
    """
    start = time.monotonic()
    channel = ResolvedChannel(
        channel_id=3,
        name="agnes-image",
        protocol="openai",
        base_url=settings.agnes_image_base_url,
        api_key=settings.agnes_image_api_key,
        model=settings.agnes_image_model,
        capability="image",
    )
    try:
        result = await generate_image_real(
            prompt="一只橘猫坐在窗边",
            out_dir=settings.storage_path() / "generated",
            seed=12345,
            channel=channel,
        )
        if result and result.get("url"):
            record("图像 Agnes", True, time.monotonic() - start,
                   f"model={channel.model} url={result['url']}")
        else:
            record("图像 Agnes", False, time.monotonic() - start,
                   f"model={channel.model} provider 返回 None（详见上方 warning 日志）")
    except Exception as exc:  # noqa: BLE001
        record("图像 Agnes", False, time.monotonic() - start, f"err={str(exc)[:160]}")


async def probe_video() -> None:
    """探测 Agnes 视频渠道：生成"海浪拍打沙滩"5 秒片段（内部函数）。

    异步任务轮询总超时取 settings.video_poll_timeout_seconds（默认 300s），
    可能耗时数分钟；超时/失败打印具体响应与错误，不阻塞其他渠道。

    Returns:
        None（结果经 record 写入 RESULTS）。

    Raises:
        无：provider 返回 None 或异常统一收敛为 FAIL 记录。
    """
    start = time.monotonic()
    channel = ResolvedChannel(
        channel_id=4,
        name="agnes-video",
        protocol="openai",
        base_url=settings.agnes_video_base_url,
        api_key=settings.agnes_video_api_key,
        model=settings.agnes_video_model,
        capability="video",
    )
    try:
        print(f"      （视频为异步任务，轮询超时 {settings.video_poll_timeout_seconds}s，请耐心等待…）",
              flush=True)
        result = await generate_video_real(
            prompt="海浪拍打沙滩",
            duration_sec=5,
            out_dir=settings.storage_path() / "generated",
            seed=54321,
            channel=channel,
        )
        if result and result.get("url"):
            record("视频 Agnes", True, time.monotonic() - start,
                   f"model={channel.model} url={result['url']}")
        else:
            record("视频 Agnes", False, time.monotonic() - start,
                   f"model={channel.model} provider 返回 None/超时"
                   f"（poll_timeout={settings.video_poll_timeout_seconds}s，详见上方 warning 日志）")
    except Exception as exc:  # noqa: BLE001
        record("视频 Agnes", False, time.monotonic() - start, f"err={str(exc)[:160]}")


async def probe_tts() -> None:
    """探测百炼 TTS 渠道：合成"漫序智能创作，让灵感流动"（内部函数）。

    Returns:
        None（结果经 record 写入 RESULTS）。

    Raises:
        无：provider 返回 None 或异常统一收敛为 FAIL 记录。
    """
    start = time.monotonic()
    channel = ResolvedChannel(
        channel_id=5,
        name="bailian-tts",
        protocol="openai",
        base_url=settings.bailian_tts_base_url,
        api_key=settings.bailian_tts_api_key,
        model=settings.bailian_tts_model,
        capability="tts",
    )
    try:
        result = await generate_tts_real(
            text="漫序智能创作，让灵感流动",
            voice="female_01",
            out_dir=settings.storage_path() / "generated",
            channel=channel,
        )
        if result and result.get("url"):
            record("TTS 百炼", True, time.monotonic() - start,
                   f"model={channel.model} url={result['url']} "
                   f"duration={result.get('duration_sec')}s voice={result.get('meta', {}).get('voice')}")
        else:
            record("TTS 百炼", False, time.monotonic() - start,
                   f"model={channel.model} provider 返回 None（详见上方 warning 日志）")
    except Exception as exc:  # noqa: BLE001
        record("TTS 百炼", False, time.monotonic() - start, f"err={str(exc)[:160]}")


def print_summary() -> int:
    """打印汇总表并返回脚本退出码。

    Returns:
        int: 0 仅当 LLM 主渠道 PASS；否则 1。

    Raises:
        无。
    """
    print("\n===== 探测汇总 =====", flush=True)
    print(f"{'渠道':<12}{'结果':<8}{'耗时(s)':<10}关键信息")
    main_ok = False
    for name, tag, elapsed, detail in RESULTS:
        print(f"{name:<12}{tag:<8}{elapsed:<10.1f}{detail[:110]}")
        if name == "LLM 主 DeepSeek":
            main_ok = tag == "PASS"
    print("====================", flush=True)
    warn = [n for n, t, _, _ in RESULTS if t == "FAIL" and n != "LLM 主 DeepSeek"]
    if warn:
        print(f"警告：以下渠道 FAIL（线上将自动降级 mock 渠道，不阻塞系统）：{', '.join(warn)}", flush=True)
    return 0 if main_ok else 1


async def main() -> int:
    """执行五渠道探测主流程。

    Returns:
        int: 0 仅当 LLM 主渠道 PASS；否则 1。

    Raises:
        无：单渠道异常均被收敛为 FAIL 记录，不中断其他渠道探测。
    """
    print(f"== MangaFlow 渠道连通性探测开始（storage={settings.storage_path()}）==", flush=True)
    # 1. LLM 主渠道（DeepSeek）
    await probe_chat_llm("LLM 主 DeepSeek", settings.deepseek_base_url,
                        settings.deepseek_api_key, settings.deepseek_model)
    # 2. LLM 兜底渠道（Agnes）
    await probe_chat_llm("LLM 兜底 Agnes", settings.agnes_llm_base_url,
                        settings.agnes_llm_api_key, settings.agnes_llm_model)
    # 3. 图像（Agnes）
    await probe_image()
    # 4. 视频（Agnes，可能耗时数分钟）
    await probe_video()
    # 5. TTS（百炼）
    await probe_tts()
    return print_summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
