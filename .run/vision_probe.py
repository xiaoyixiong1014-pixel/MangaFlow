"""Agnes 网关视觉理解模型探测脚本（Task 7 图像/视频真实视觉机审前置调研，文件级说明）。

探测流程：
1. GET {agnes_llm_base_url}/models（Bearer agnes_llm_api_key）列出全部模型；
2. 按名称关键词（vision/vl/multimodal/omni/4o/gemini/glm-4v/qvq 等）筛出疑似视觉模型；
3. 对前 2 个候选做小样图文调用（OpenAI chat 格式，content 数组带 image_url data URI），
   测试图取 backend/storage/generated 下现成 PNG 的 base64；
4. 输出可用模型名、响应样例与请求格式结论（供 vision_review.py 与 config 默认值依据）。

运行方式：backend venv python .run/vision_probe.py
退出码：0 = 至少一个候选接受图文输入并返回内容；1 = 无可用视觉模型。

Raises:
    Exception: 网络/鉴权异常向上抛出（探测脚本需要真实结果，不做静默兜底）。
"""

import asyncio
import base64
import json
import sys
from pathlib import Path

# 保证中文在 Windows 控制台正常输出
sys.stdout.reconfigure(encoding="utf-8")

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402

BASE_URL = settings.agnes_llm_base_url.rstrip("/")
API_KEY = settings.agnes_llm_api_key

# 疑似视觉模型的名称关键词（小写匹配）
VISION_HINTS = [
    "vision", "vl", "multimodal", "omni", "4o", "gemini", "glm-4v", "qvq",
    "gpt-4.1", "gpt-5", "claude", "internvl", "minicpm-v", "thinking",
]


async def probe() -> int:
    """执行完整探测流程（主入口）。

    Returns:
        int: 0 = 至少一个候选接受图文输入；1 = 网关无可用视觉模型。

    Raises:
        Exception: HTTP 请求异常向上抛出。
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        # ---------- 1. 列出全部模型 ----------
        resp = await client.get(
            f"{BASE_URL}/models", headers={"Authorization": f"Bearer {API_KEY}"}
        )
        print(f"== GET {BASE_URL}/models -> HTTP {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        model_ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        print(f"模型总数：{len(model_ids)}")
        for mid in model_ids:
            print(f"  - {mid}")

        # ---------- 2. 筛选疑似视觉模型 ----------
        candidates = [
            mid for mid in model_ids
            if any(hint in mid.lower() for hint in VISION_HINTS)
        ]
        print(f"\n== 疑似视觉模型候选（关键词 {VISION_HINTS}）：{candidates[:20]}")
        if not candidates:
            # 名称未命中：名称推断失效，改为对文本 LLM（排除 image/video 等非 chat 模型）
            # 做实测图文调用验证——旗舰命名（pro/max）优先，最多取 3 个候选
            exclude_hints = ["image", "video", "tts", "audio", "whisper", "embedding", "rerank", "moderation"]
            text_models = [
                mid for mid in model_ids
                if not any(h in mid.lower() for h in exclude_hints)
            ]
            # 旗舰优先：含 pro/max 的排前，其余保持列表顺序
            text_models.sort(key=lambda m: (0 if ("pro" in m or "max" in m) else 1, model_ids.index(m)))
            candidates = text_models[:3]
            print(f"名称无命中，转实测文本 LLM 候选（旗舰优先，最多 3 个）：{candidates}")

        # ---------- 3. 小样图文调用（取前 3 个候选） ----------
        # 测试图要求非空有效 PNG：优先指定文件，损坏（<1KiB）时自动挑选真实生成图
        gen_dir = BACKEND_DIR / "storage" / "generated"
        png_path = gen_dir / "keyframe_999_1700000000.png"
        if not png_path.exists() or png_path.stat().st_size < 1024:
            pngs = sorted(
                (p for p in gen_dir.glob("*.png") if p.stat().st_size > 10240),
                key=lambda p: p.stat().st_size,
            )
            assert pngs, "storage/generated 下无大于 10KiB 的有效 PNG 可用作测试图"
            png_path = pngs[0]
        b64 = base64.b64encode(png_path.read_bytes()).decode()
        print(f"\n== 测试图：{png_path.name}（{len(b64) // 1024} KiB base64）")

        ok_models = []
        for mid in candidates[:3]:
            print(f"\n---- 小样图文调用：{mid} ----")
            payload = {
                "model": mid,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "用一句话描述这张图片的内容。"},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64}"},
                            },
                        ],
                    }
                ],
                "max_tokens": 200,
                "temperature": 0.1,
            }
            try:
                r = await client.post(
                    f"{BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                    json=payload,
                )
                print(f"HTTP {r.status_code}")
                body = r.text[:800]
                print(f"响应样例：{body}")
                if r.status_code == 200:
                    content = (((r.json().get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
                    if content.strip():
                        print(f"提取的回复内容：{content[:200]}")
                        ok_models.append(mid)
                    else:
                        print("HTTP 200 但回复内容为空（视为不可用）")
            except Exception as exc:  # noqa: BLE001 —— 探测脚本记录失败继续下一候选
                print(f"调用异常：{exc}")

        # ---------- 4. 结论 ----------
        print("\n== 探测结论 ==")
        if ok_models:
            print(f"可用视觉模型：{ok_models}")
            print("请求格式：OpenAI chat/completions，content 数组 [{type:text},{type:image_url,image_url:{url:data:image/png;base64,...}}]")
            return 0
        print("结论：网关无可用视觉模型（候选全部拒绝图文输入）。")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(probe()))
