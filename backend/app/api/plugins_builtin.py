"""内置插件路由与实现模块（文件级说明）。

内置插件（endpoint_url 形如 "builtin:<name>"，CV-21）：
- builtin:text-enhance —— 文本规则增强（CV-20 演示）：
  1) 首尾空白清理；2) 连续重复标点折叠为一个；3) 连续重复汉字/字母
  （叠词/重复词）折叠为一个；4) 末尾无终结标点（。！？!?）时补句号。

路由：
- POST /api/plugins/builtin/text-enhance  {text} → {text}（登录用户可用），
  与插件体系调用共享同一实现（插件负载 {"text": ...}）。

本 router 挂载于 main.py。
"""

import re
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.security import get_current_user
from app.models import User
from app.services import plugin_service

router = APIRouter(prefix="/api/plugins/builtin", tags=["plugins-builtin"])


class TextEnhanceRequest(BaseModel):
    """文本增强请求。

    Attributes:
        text: 待增强的原始文本（最长 10000 字符）。
    """

    text: str = Field(default="", max_length=10000)


class TextEnhanceResponse(BaseModel):
    """文本增强响应。

    Attributes:
        text: 增强后的文本。
    """

    text: str


# 连续重复标点（中英文常见标点）折叠为一个（去重复标点）
_REPEAT_PUNCT_RE = re.compile(r"([，。！？、；：,.!?;:])\1+")
# 连续重复汉字/字母/数字（叠词/重复词）折叠为一个（如 "的的" → "的"）
_REPEAT_WORD_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9])\1+")
# 终结标点集合（末尾已有则不补句号）
_TERMINAL_PUNCTS = tuple("。！？!?；;~～")


def enhance_text(raw: str) -> str:
    """文本规则增强（内置插件 text-enhance 的核心实现）。

    处理顺序：strip 清理首尾空白 → 折叠重复标点 → 折叠重复词 → 补句号。

    Args:
        raw: 原始文本。

    Returns:
        str: 增强后的文本。
    """
    text = (raw or "").strip()
    text = _REPEAT_PUNCT_RE.sub(r"\1", text)
    text = _REPEAT_WORD_RE.sub(r"\1", text)
    if text and not text.endswith(_TERMINAL_PUNCTS):
        text = f"{text}。"
    return text


async def _text_enhance_handler(payload: Dict[str, Any]) -> Dict[str, Any]:
    """text-enhance 内置插件处理函数（经 plugin_service 注册表调用）。

    Args:
        payload: 调用负载（{"text": str}，缺省按空文本处理）。

    Returns:
        Dict[str, Any]: {"text": 增强后的文本}。

    Raises:
        PluginError: 负载缺少 text 字段或类型非法。
    """
    text = payload.get("text")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise plugin_service.PluginError("payload.text 必须为字符串")
    return {"text": enhance_text(text)}


# 注册到内置插件注册表：endpoint_url "builtin:text-enhance" → 本实现
plugin_service.register_builtin("text-enhance", _text_enhance_handler)


@router.post("/text-enhance", response_model=TextEnhanceResponse)
async def builtin_text_enhance(
    req: TextEnhanceRequest,
    _user: User = Depends(get_current_user),
) -> TextEnhanceResponse:
    """内置文本增强接口（登录用户直调，与插件调用链共享同一实现）。

    Args:
        req: 请求体（text）。
        _user: 当前登录用户（仅鉴权）。

    Returns:
        TextEnhanceResponse: {"text": 增强后的文本}。

    Raises:
        HTTPException: 401 —— 未登录。
    """
    return TextEnhanceResponse(text=enhance_text(req.text))
