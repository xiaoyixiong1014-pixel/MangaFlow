# -*- coding: utf-8 -*-
"""LLM 网关错误体识别与渠道降级单元测试（文件级说明）。

覆盖 redesign-script-workspace 缺陷修复：部分聚合网关把 429/402 限流包装成
HTTP 200 + 错误 JSON 体返回，SDK 层不抛异常导致渠道降级失效——验证
providers/llm.py 的内容级校验（_looks_like_gateway_error）能识别错误体并
在非流式与流式两条路径上正确降级，且不误伤正常业务 JSON。

运行方式：backend venv 下 `python -m pytest tests/test_providers_llm_gateway.py -q`。
数据隔离：纯函数 + monkeypatch 假客户端，不触库、不发真实网络请求。
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.providers import llm as llm_mod

# 限流错误体（agnes 网关实测形态）
_GATEWAY_LIMIT_BODY = json.dumps(
    {"message": "too many requests, please slow down and retry"},
    ensure_ascii=False,
)
# 配额错误体（另一常见形态）
_GATEWAY_QUOTA_BODY = json.dumps(
    {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}},
    ensure_ascii=False,
)
# 正常业务 JSON（含业务字段，且正文里恰好出现限流词——防误伤关键用例）
_NORMAL_BUSINESS_JSON = json.dumps({
    "logline": "少女在 rate limit 严苛的编辑部追梦",
    "core_conflict": "创作初心与商业压力的对抗",
    "characters": [{"name": "林小满", "role_type": "lead"}],
}, ensure_ascii=False)


def _make_response(content: str) -> SimpleNamespace:
    """构造 openai SDK 形态的补全响应对象。

    Args:
        content: message.content 文本。

    Returns:
        SimpleNamespace: 含 choices[0].message.content 的伪响应。
    """
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class _FakeCompletions:
    """伪 completions 接口：按预设脚本逐次返回响应或抛异常。

    Attributes:
        _script: 预设行为序列（Exception 则抛出，否则作为响应返回）。
        calls: 已收到的调用参数（供断言请求形态）。
    """

    def __init__(self, script: List[Any]) -> None:
        """保存预设脚本。

        Args:
            script: 行为序列，每项为 Exception（抛出）或响应对象（返回）。
        """
        self._script = list(script)
        self.calls: List[Dict] = []

    async def create(self, **kwargs: Any):
        """按脚本弹出下一行为。

        Args:
            **kwargs: 调用参数（记录到 calls）。

        Returns:
            响应对象。

        Raises:
            Exception: 脚本该项为异常时原样抛出；脚本耗尽时抛 RuntimeError。
        """
        self.calls.append(kwargs)
        if not self._script:
            raise RuntimeError("测试脚本耗尽")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeAsyncOpenAI:
    """伪 AsyncOpenAI 客户端工厂：按 base_url 前缀路由到不同行为脚本。

    Attributes:
        _behaviors: base_url 前缀 → 行为脚本列表。
        created: 已创建客户端的 base_url 列表（供断言渠道切换顺序）。
    """

    def __init__(self, behaviors: Dict[str, List[Any]]) -> None:
        """记录按 base_url 前缀的路由脚本。

        Args:
            behaviors: base_url 前缀 → 行为脚本。
        """
        self._behaviors = behaviors
        self.created: List[str] = []

    def __call__(self, *, base_url: str, api_key: str, **kwargs: Any) -> SimpleNamespace:
        """按 base_url 前缀匹配脚本，构造伪客户端。

        Args:
            base_url: 渠道地址。
            api_key: 渠道密钥（忽略）。
            **kwargs: 其余初始化参数（忽略）。

        Returns:
            SimpleNamespace: 含 chat.completions 的伪客户端。
        """
        self.created.append(str(base_url))
        for prefix, script in self._behaviors.items():
            if str(base_url).startswith(prefix):
                completions = _FakeCompletions(list(script))
                # 同一渠道多次创建客户端时共享脚本消费进度（按序出队）
                return SimpleNamespace(chat=SimpleNamespace(completions=completions))
        raise AssertionError(f"未配置的测试渠道: {base_url}")


_CHANNELS = [
    ("https://primary.test/v1", "key-a", "model-a", "primary"),
    ("https://fallback.test/v1", "key-b", "model-b", "fallback"),
]


@pytest.fixture()
def patch_openai(monkeypatch):
    """夹具：替换 providers.llm.AsyncOpenAI 为伪客户端工厂。

    Args:
        monkeypatch: pytest monkeypatch 夹具。

    Returns:
        Callable: 注入行为脚本的函数（base_url 前缀 → 行为脚本）。
    """
    def _install(behaviors: Dict[str, List[Any]]) -> _FakeAsyncOpenAI:
        factory = _FakeAsyncOpenAI(behaviors)
        monkeypatch.setattr(llm_mod, "AsyncOpenAI", factory)
        return factory
    return _install


# ---------------------------------------------------------------------------
# _looks_like_gateway_error 纯函数直测
# ---------------------------------------------------------------------------
def test_gateway_error_detector_hits_limit_body() -> None:
    """限流错误体（agnes 实测形态）应被判定为网关错误。

    Args:
        无。

    Returns:
        无。
    """
    assert llm_mod._looks_like_gateway_error(_GATEWAY_LIMIT_BODY) is True


def test_gateway_error_detector_hits_quota_body() -> None:
    """配额错误体应被判定为网关错误。

    Args:
        无。

    Returns:
        无。
    """
    assert llm_mod._looks_like_gateway_error(_GATEWAY_QUOTA_BODY) is True


def test_gateway_error_detector_spares_business_json() -> None:
    """含限流词但带业务字段的正常 JSON 不得误判（防误伤关键用例）。

    Args:
        无。

    Returns:
        无。
    """
    assert llm_mod._looks_like_gateway_error(_NORMAL_BUSINESS_JSON) is False


def test_gateway_error_detector_spares_plain_text() -> None:
    """命中特征词但无错误结构的普通长文本不得误判。

    Args:
        无。

    Returns:
        无。
    """
    long_text = "剧本正文：" + "两人讨论平台的 rate limit 政策。" * 30
    assert llm_mod._looks_like_gateway_error(long_text) is False
    assert llm_mod._looks_like_gateway_error("") is False


def test_gateway_error_detector_spares_overlong_body() -> None:
    """超长内容即使命中特征词也不判定（错误体必然很短）。

    Args:
        无。

    Returns:
        无。
    """
    overlong = json.dumps({"message": "too many requests " + "x" * 600})
    assert llm_mod._looks_like_gateway_error(overlong) is False


# ---------------------------------------------------------------------------
# chat_with_meta 非流式降级
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_with_meta_falls_through_on_gateway_error(patch_openai) -> None:
    """渠道 A 返回限流错误体时应降级到渠道 B 并交付正常内容。

    Args:
        patch_openai: 伪客户端夹具。

    Returns:
        无。
    """
    patch_openai({
        "https://primary.test": [_make_response(_GATEWAY_LIMIT_BODY)],
        "https://fallback.test": [_make_response("正常补全内容")],
    })
    content, meta = await llm_mod.chat_with_meta(
        [{"role": "user", "content": "hi"}],
        channels=_CHANNELS,
    )
    assert content == "正常补全内容"
    assert meta["channel"] == "fallback"


@pytest.mark.asyncio
async def test_chat_with_meta_spares_normal_business_json(patch_openai) -> None:
    """渠道 A 返回正常业务 JSON（含限流词）时不得降级，直接交付。

    Args:
        patch_openai: 伪客户端夹具。

    Returns:
        无。
    """
    factory = patch_openai({
        "https://primary.test": [_make_response(_NORMAL_BUSINESS_JSON)],
        "https://fallback.test": [_make_response("不应到达")],
    })
    content, meta = await llm_mod.chat_with_meta(
        [{"role": "user", "content": "hi"}],
        channels=_CHANNELS,
    )
    assert content == _NORMAL_BUSINESS_JSON
    assert meta["channel"] == "primary"
    # 仅创建了主渠道客户端（未发生降级）
    assert factory.created == ["https://primary.test/v1"]


@pytest.mark.asyncio
async def test_chat_with_meta_all_gateway_errors_raises(patch_openai) -> None:
    """全部渠道都返回错误体时应抛出最后一个异常。

    Args:
        patch_openai: 伪客户端夹具。

    Returns:
        无。
    """
    patch_openai({
        "https://primary.test": [_make_response(_GATEWAY_LIMIT_BODY)],
        "https://fallback.test": [_make_response(_GATEWAY_QUOTA_BODY)],
    })
    with pytest.raises(RuntimeError, match="网关错误体"):
        await llm_mod.chat_with_meta([{"role": "user", "content": "hi"}], channels=_CHANNELS)


# ---------------------------------------------------------------------------
# chat_stream_with_meta 流式降级（首块即完整错误体）
# ---------------------------------------------------------------------------
class _FakeStream:
    """伪流式响应：按序产出 delta chunk。

    Attributes:
        _chunks: chunk 文本序列。
    """

    def __init__(self, chunks: List[str]) -> None:
        """保存 chunk 序列。

        Args:
            chunks: delta.content 文本列表。
        """
        self._chunks = list(chunks)

    def __aiter__(self):
        """返回异步迭代器。

        Returns:
            自身（实现 __anext__）。
        """
        return self

    async def __anext__(self) -> SimpleNamespace:
        """产出下一 chunk。

        Returns:
            SimpleNamespace: choices[0].delta.content 形态的 chunk。

        Raises:
            StopAsyncIteration: 序列耗尽。
        """
        if not self._chunks:
            raise StopAsyncIteration
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=self._chunks.pop(0)))])


@pytest.mark.asyncio
async def test_chat_stream_falls_through_on_gateway_error_first_chunk(patch_openai) -> None:
    """流式渠道首块即完整限流错误体时应降级到下一渠道。

    Args:
        patch_openai: 伪客户端夹具。

    Returns:
        无。
    """
    patch_openai({
        "https://primary.test": [_FakeStream([_GATEWAY_LIMIT_BODY])],
        "https://fallback.test": [_FakeStream(["第一段", "第二段"])],
    })
    pieces: List[str] = []
    metas: List[Optional[Dict]] = []
    async for piece, meta in llm_mod.chat_stream_with_meta(
        [{"role": "user", "content": "hi"}], channels=_CHANNELS,
    ):
        pieces.append(piece)
        metas.append(meta)
    assert "".join(pieces) == "第一段第二段"
    assert metas[0]["channel"] == "fallback"
