"""视觉机审提供方模块（文件级说明，Task 7 / 合规红线 R-08、C-01）。

通过 OpenAI 兼容 chat/completions 图文混合协议调用视觉理解模型，对图像/视频
产物做**真实视觉机审**（区别于既有 llm-text 纯文本机审）：
- review_image()：送审一张图（本地存储 URL / 公网 URL / 裸 base64），返回
  {"verdict": "pass|suspect|violate", "reason": "...", "model": "..."}；
- 判定口径与审核词表分级对齐：严禁内容（赌博/枪械暴力/毒品/暴恐/色情）→
  violate；敏感擦边（政治/低俗/血腥暗示）→ suspect；正常创作 → pass；
- JSON 提取容错：剥 Markdown 代码围栏、截取首尾大括号间内容，解析失败抛异常
  由调用方（tasks/runner.py）降级 llm-text → keyword 文本机审。

探测证据（2026-10-10，详见 .run/probe_result.md「视觉机审模型探测」一节）：
Agnes 网关 GET /v1/models 共 11 个模型均无显式视觉命名（无 vision/vl 关键词），
但对文本旗舰 agnes-2.5-pro-alpha / agnes-3.0-flash-max / agnes-2.5-pro 实测
图文小样调用（content 数组 [{type:text},{type:image_url,image_url:{url:
"data:image/png;base64,..."}}]）均 HTTP 200 并返回准确图片描述，且
usage.prompt_tokens_details.image_tokens=943 证明图片真实编码入模——即 Agnes
旗舰文本模型即多模态视觉模型。请求格式即标准 OpenAI 视觉协议，默认模型取
agnes-3.0-flash-max（config.agnes_vision_model，可配置）。
"""

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Dict, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger("mangaflow.providers.vision_review")

# 视觉机审调用超时（秒）：超时视为本次视觉审核失败，由调用方降级文本机审
_VISION_TIMEOUT = 15.0

# 视觉机审系统提示词：要求只输出 JSON 判定，判定口径与词表分级对齐
_VISION_SYSTEM_PROMPT = (
    "你是内容安全审核员，正在对 AI 生成的图像/视频画面做真实视觉机审。"
    "请仔细观察送审画面的实际视觉内容，结合补充上下文（生成提示词/画面描述）"
    '做内容安全判定，只输出 JSON：{"verdict": "pass|suspect|violate", "reason": "一句话理由"}。'
    "判定标准（与平台内容词表分级口径一致）："
    "画面含赌博、枪械暴力、毒品、暴恐、色情淫秽等严禁内容 → violate；"
    "画面含政治敏感、低俗擦边、血腥/暴力暗示、烟酒等内容 → suspect；"
    "其余正常创作内容 → pass。"
)

# 本地存储 URL 前缀：main.py 已将 storage 目录 mount 到 /storage
_STORAGE_URL_PREFIX = "/storage"


def _new_http_client() -> httpx.AsyncClient:
    """创建视觉机审 HTTP 客户端（模块级包装，供测试注入 MockTransport）。

    Returns:
        httpx.AsyncClient: 已配置超时的异步 HTTP 客户端。

    Raises:
        无。
    """
    return httpx.AsyncClient(timeout=_VISION_TIMEOUT)


def _guess_mime(path: Path) -> str:
    """按文件扩展名推断图片 MIME 类型（内部函数）。

    Args:
        path: 图片文件路径。

    Returns:
        str: MIME 类型字符串（未知扩展名默认 image/png）。

    Raises:
        无。
    """
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
    }.get(ext, "image/png")


def _resolve_image_ref(
    image_url: Optional[str], image_b64: Optional[str]
) -> Optional[str]:
    """把调用方传入的图像引用规整为模型可读的 image_url（内部函数）。

    支持三种输入形态（优先级 image_b64 > image_url）：
    - 裸 base64：直接拼 PNG data URI；
    - http(s):// 或 data: 开头：原样透传（模型端自行拉取/解析）；
    - 本地存储 URL（/storage/...）：映射到 storage 目录读文件转 base64 data URI。

    Args:
        image_url: 图像 URL（本地存储 URL / 公网 URL，可空）。
        image_b64: 裸 base64 图像数据（不含 data: 前缀，可空）。

    Returns:
        Optional[str]: 模型 image_url 字段可用值；无法解析出有效图像返回 None。

    Raises:
        无：本地文件读取失败记 warning 后返回 None（由调用方按失败降级处理）。
    """
    if image_b64:
        return f"data:image/png;base64,{image_b64}"
    if not image_url:
        return None
    ref = image_url.strip()
    if ref.startswith(("http://", "https://", "data:")):
        return ref
    # 本地存储 URL → storage 目录相对路径 → 读文件转 base64
    rel = ref.lstrip("/")
    if rel.startswith("storage/"):
        rel = rel[len("storage/"):]
    path = settings.storage_path() / rel
    try:
        raw = path.read_bytes()
    except OSError:
        logger.warning("视觉机审产物文件不可读：%s", path)
        return None
    mime = _guess_mime(path)
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _strip_json_fences(text: str) -> str:
    """剥离模型输出中的 Markdown 代码围栏（内部函数）。

    Args:
        text: 模型原始输出文本。

    Returns:
        str: 去除 ```json ... ``` 围栏后的纯文本。

    Raises:
        无：纯字符串处理，不产生异常路径。
    """
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
    return stripped


def _extract_json(text: str) -> Dict:
    """从模型输出文本中提取 JSON 判定对象（内部函数，带容错）。

    提取顺序：剥围栏后整体解析 → 截取首个 { 到最后一个 } 之间内容再解析；
    两步均失败抛出 ValueError（由调用方降级文本机审）。

    Args:
        text: 模型原始输出文本。

    Returns:
        Dict: 解析出的 JSON 对象。

    Raises:
        ValueError: 输出中不含可解析的 JSON 对象。
    """
    stripped = _strip_json_fences(text)
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # 容错：截取首尾大括号之间的内容再尝试一次
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(stripped[start:end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    raise ValueError(f"视觉机审输出不含有效 JSON：{(text or '')[:120]}")


async def _request_vision_json(image_ref: str, context: str) -> Dict:
    """调用视觉模型做一次图文机审并解析判定对象（内部函数）。

    Args:
        image_ref: 模型 image_url 字段值（data URI 或公网 URL）。
        context: 补充上下文（生成提示词/画面描述，可空）。

    Returns:
        Dict: 模型输出的 JSON 判定对象（含 verdict/reason 等键）。

    Raises:
        Exception: HTTP 非 200、响应结构异常或 JSON 解析失败时向上抛出
            （整体受 review_image 的 wait_for 超时保护）。
    """
    user_text = (
        "请对该画面做内容安全机审判定。"
        + (f"补充上下文（生成提示词/画面描述）：{context[:1500]}" if context else "")
    )
    payload = {
        "model": settings.agnes_vision_model,
        "messages": [
            {"role": "system", "content": _VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": image_ref}},
                ],
            },
        ],
        "temperature": 0.1,
        "max_tokens": 300,
    }
    async with _new_http_client() as client:
        resp = await client.post(
            f"{settings.agnes_llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.agnes_llm_api_key}"},
            json=payload,
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"视觉模型返回 HTTP {resp.status_code}：{resp.text[:200]}"
        )
    data = resp.json()
    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
    if not content.strip():
        raise RuntimeError("视觉模型返回内容为空")
    return _extract_json(content)


async def review_image(
    image_url: Optional[str] = None,
    image_b64: Optional[str] = None,
    context: str = "",
) -> Dict:
    """对单张图像做真实视觉机审（Task 7 / R-08、C-01）。

    调用 Agnes 视觉模型（OpenAI 兼容图文混合协议）对画面实际视觉内容做
    内容安全判定；整体受 asyncio.wait_for 超时保护（15s），任何异常
    （网络/鉴权/HTTP 非 200/JSON 解析失败/图像不可读）向上抛出，由调用方
    （tasks/runner.py）降级为 LLM 文本机审（method=llm-text）→ 词表（keyword）。

    Args:
        image_url: 图像 URL（本地存储 URL 如 /storage/generated/x.png、
            公网 http(s) URL 或 data URI，可空）。
        image_b64: 裸 base64 图像数据（不含 data: 前缀，可空；优先于 image_url）。
        context: 补充上下文（生成提示词/画面描述，可空）。

    Returns:
        Dict: {"verdict": "pass|suspect|violate", "reason": 一句话理由,
        "model": 实际使用的视觉模型名}；verdict 未知取值保守判 suspect。

    Raises:
        ValueError: 无任何可用图像输入。
        Exception: HTTP 调用失败、超时或 JSON 解析失败时向上抛出。
    """
    image_ref = _resolve_image_ref(image_url, image_b64)
    if not image_ref:
        raise ValueError("视觉机审缺少可用图像输入（image_url/image_b64 均无效）")
    data = await asyncio.wait_for(
        _request_vision_json(image_ref, context), timeout=_VISION_TIMEOUT
    )
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in ("pass", "suspect", "violate"):
        # 未知判定结果按可疑处理（保守口径，交人工复核）
        verdict = "suspect"
    reason = str(data.get("reason") or "")[:200]
    return {"verdict": verdict, "reason": reason, "model": settings.agnes_vision_model}
