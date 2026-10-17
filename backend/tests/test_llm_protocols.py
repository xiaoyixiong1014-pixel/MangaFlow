"""LLM 多协议支持测试模块（文件级说明）。

覆盖 providers/llm.py 的三种 API 格式真实接入（模型配置优化 2026-10-16）：
- 纯函数：_anthropic_messages_url 地址规范化（根地址/带 /v1 两种填法）、
  _split_system_messages（system 抽取）、_extract_anthropic_text（响应解析）、
  _channel_protocol（旧四元组兼容缺省 openai）；
- _call_anthropic_messages：fake httpx 端到端（URL/请求头/system 字段/
  max_tokens/响应文本拼接；json_mode 注入 JSON 约束）；
- chat_with_meta 协议分派：anthropic/responses 渠道走对应实现并返回
  渠道名；openai 渠道失败后 anthropic 渠道兜底降级。
"""

import json
from typing import Any, Dict, List, Optional

import pytest

import app.providers.llm as llm
from app.providers.llm import (
    _anthropic_messages_url,
    _call_channel,
    _channel_protocol,
    _extract_anthropic_text,
    _split_system_messages,
    chat_with_meta,
)


# ---------- 纯函数 ----------

def test_anthropic_url_guifanhua():
    """地址规范化：根地址补 /v1、带 /v1 不重复、rstrip 斜杠。

    Returns:
        None。

    Raises:
        AssertionError: 拼装结果与规范不符时中断。
    """
    assert _anthropic_messages_url("https://api.anthropic.com") == "https://api.anthropic.com/v1/messages"
    assert _anthropic_messages_url("https://api.anthropic.com/") == "https://api.anthropic.com/v1/messages"
    assert _anthropic_messages_url("https://gw.example.com/v1") == "https://gw.example.com/v1/messages"
    assert _anthropic_messages_url("https://gw.example.com/v1/") == "https://gw.example.com/v1/messages"


def test_split_system_messages():
    """system 消息抽取：system/developer 合并为 system 文本，其余保留。

    Returns:
        None。

    Raises:
        AssertionError: 拆分结果不符时中断。
    """
    messages = [
        {"role": "system", "content": "你是助手。"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "在的"},
        {"role": "developer", "content": "第二段"},
        {"role": "user", "content": "继续"},
    ]
    system_text, rest = _split_system_messages(messages)
    assert "你是助手。" in system_text and "第二段" in system_text
    assert [m["role"] for m in rest] == ["user", "assistant", "user"]
    assert len(rest) == 3


def test_extract_anthropic_text():
    """Anthropic 响应文本拼接：多 text 块合并、结构异常返回空串。

    Returns:
        None。

    Raises:
        AssertionError: 解析结果不符时中断。
    """
    assert _extract_anthropic_text({"content": [
        {"type": "text", "text": "你"},
        {"type": "text", "text": "好"},
        {"type": "tool_use", "id": "x"},
    ]}) == "你好"
    assert _extract_anthropic_text({"content": []}) == ""
    assert _extract_anthropic_text({}) == ""


def test_channel_protocol_compat():
    """协议位读取：五元组返回显式协议，旧四元组缺省 openai。

    Returns:
        None。

    Raises:
        AssertionError: 协议位读取不符时中断。
    """
    assert _channel_protocol(("u", "k", "m", "n", "anthropic")) == "anthropic"
    assert _channel_protocol(("u", "k", "m", "n")) == "openai"


# ---------- fake httpx（anthropic 真实请求形状验证） ----------

class _FakeResponse:
    """fake httpx 响应（测试替身）。

    Attributes:
        status_code: HTTP 状态码。
        text: 响应体文本。
    """

    def __init__(self, status_code: int, data: Optional[Dict[str, Any]] = None, text: str = ""):
        """初始化 fake 响应。

        Args:
            status_code: HTTP 状态码。
            data: JSON 响应体（可空）。
            text: 非 JSON 响应体文本。
        """
        self.status_code = status_code
        self._data = data or {}
        self.text = text or json.dumps(self._data, ensure_ascii=False)

    def json(self) -> Dict[str, Any]:
        """返回 JSON 响应体。

        Returns:
            Dict[str, Any]: 响应体字典。
        """
        return self._data


class _FakeAsyncClient:
    """fake httpx.AsyncClient（测试替身）：记录请求并返回预设响应。

    Attributes:
        last_url: 最近一次请求 URL。
        last_headers: 最近一次请求头。
        last_payload: 最近一次请求体。
    """

    last_url: str = ""
    last_headers: Dict[str, str] = {}
    last_payload: Dict[str, Any] = {}

    def __init__(self, **_kw):
        """初始化（忽略超时等参数）。

        Raises:
            无。
        """

    async def __aenter__(self) -> "_FakeAsyncClient":
        """进入上下文。

        Returns:
            _FakeAsyncClient: 自身。
        """
        return self

    async def __aexit__(self, *_args) -> bool:
        """退出上下文。

        Returns:
            bool: False（不吞异常）。
        """
        return False

    async def post(self, url: str, headers: Dict[str, str] = None, json: Dict[str, Any] = None):
        """记录请求并返回预设响应。

        Args:
            url: 请求 URL。
            headers: 请求头。
            json: 请求体。

        Returns:
            _FakeResponse: 预设响应（默认 200 + 单 text 块）。
        """
        _FakeAsyncClient.last_url = url
        _FakeAsyncClient.last_headers = dict(headers or {})
        _FakeAsyncClient.last_payload = json or {}
        return _FakeResponse(200, {"content": [{"type": "text", "text": "你好，世界"}]})


@pytest.fixture()
def fake_httpx(monkeypatch):
    """替换 llm 模块内的 httpx.AsyncClient 为 fake（内部夹具）。

    Args:
        monkeypatch: pytest monkeypatch。

    Returns:
        _FakeAsyncClient: fake 客户端类（读取请求记录）。
    """
    monkeypatch.setattr(llm.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


@pytest.mark.asyncio
async def test_call_anthropic_zhenshi_qingqiu(fake_httpx):
    """anthropic 非流式：URL/请求头/system 抽取/max_tokens/响应拼接全链路。

    Args:
        fake_httpx: fake httpx 夹具。

    Returns:
        None。

    Raises:
        AssertionError: 请求形状或解析结果不符时中断。
    """
    messages = [
        {"role": "system", "content": "你是漫剧助手。"},
        {"role": "user", "content": "写一句台词"},
    ]
    text = await llm._call_anthropic_messages(
        "https://gw.example.com", "sk-ant-test", "claude-sonnet-4-5", messages, 0.7, False)
    assert text == "你好，世界"
    assert _FakeAsyncClient.last_url == "https://gw.example.com/v1/messages"
    assert _FakeAsyncClient.last_headers["x-api-key"] == "sk-ant-test"
    assert _FakeAsyncClient.last_headers["anthropic-version"] == llm._ANTHROPIC_VERSION
    assert _FakeAsyncClient.last_payload["system"] == "你是漫剧助手。"
    assert _FakeAsyncClient.last_payload["max_tokens"] == llm._ANTHROPIC_MAX_TOKENS
    assert _FakeAsyncClient.last_payload["model"] == "claude-sonnet-4-5"
    assert _FakeAsyncClient.last_payload["messages"] == [{"role": "user", "content": "写一句台词"}]


@pytest.mark.asyncio
async def test_call_anthropic_jsonmode(fake_httpx):
    """anthropic json_mode：system 注入 JSON 输出约束。

    Args:
        fake_httpx: fake httpx 夹具。

    Returns:
        None。

    Raises:
        AssertionError: JSON 约束未注入时中断。
    """
    await llm._call_anthropic_messages(
        "https://gw.example.com/v1", "k", "claude-sonnet-4-5",
        [{"role": "user", "content": "出 JSON"}], 0.7, True)
    assert "JSON" in _FakeAsyncClient.last_payload.get("system", "")


@pytest.mark.asyncio
async def test_chat_with_meta_xieyi_fenpai(monkeypatch):
    """chat_with_meta 协议分派：anthropic/responses 渠道走对应实现。

    Args:
        monkeypatch: pytest monkeypatch。

    Returns:
        None。

    Raises:
        AssertionError: 未按协议分派或渠道名回传错误时中断。
    """
    called: List[str] = []

    async def fake_anthropic(*_a, **_kw) -> str:
        """记录 anthropic 调用（内部替身）。

        Returns:
            str: 固定文本。
        """
        called.append("anthropic")
        return "来自 anthropic"

    async def fake_responses(*_a, **_kw) -> str:
        """记录 responses 调用（内部替身）。

        Returns:
            str: 固定文本。
        """
        called.append("responses")
        return "来自 responses"

    monkeypatch.setattr(llm, "_call_anthropic_messages", fake_anthropic)
    monkeypatch.setattr(llm, "_call_openai_responses", fake_responses)

    content, meta = await chat_with_meta(
        messages=[{"role": "user", "content": "hi"}],
        channels=[("https://gw.example.com", "k", "claude-sonnet-4-5", "我的 Claude", "anthropic")],
    )
    assert content == "来自 anthropic" and meta["channel"] == "我的 Claude"
    content, meta = await chat_with_meta(
        messages=[{"role": "user", "content": "hi"}],
        channels=[("https://api.openai.com/v1", "k", "gpt-5", "我的 GPT", "openai_responses")],
    )
    assert content == "来自 responses" and meta["channel"] == "我的 GPT"
    assert called == ["anthropic", "responses"]


@pytest.mark.asyncio
async def test_chat_with_meta_jiangji_duoxieyi(monkeypatch):
    """多协议降级：openai 渠道失败后 anthropic 渠道兜底成功。

    Args:
        monkeypatch: pytest monkeypatch。

    Returns:
        None。

    Raises:
        AssertionError: 降级顺序或结果不符时中断。
    """

    async def fake_anthropic(*_a, **_kw) -> str:
        """anthropic 替身（兜底成功）。

        Returns:
            str: 固定文本。
        """
        return "兜底成功"

    async def fake_openai_call(channel, *_a, **_kw) -> str:
        """openai 替身（模拟失败）。

        Args:
            channel: 渠道元组（用于判定渠道名）。

        Raises:
            RuntimeError: 始终抛出模拟渠道故障。
        """
        raise RuntimeError(f"{channel[3]} 上游超时")

    monkeypatch.setattr(llm, "_call_anthropic_messages", fake_anthropic)
    monkeypatch.setattr(llm, "_call_openai_chat", fake_openai_call)

    text, meta = await chat_with_meta(
        messages=[{"role": "user", "content": "hi"}],
        channels=[
            ("https://api.deepseek.com", "k", "deepseek-chat", "deepseek", "openai"),
            ("https://gw.example.com", "k2", "claude-sonnet-4-5", "我的 Claude", "anthropic"),
        ],
    )
    assert text == "兜底成功" and meta["channel"] == "我的 Claude"


@pytest.mark.asyncio
async def test_call_channel_reponses_fenpai(monkeypatch):
    """_call_channel 分派：openai_responses 渠道进入 responses 实现。

    Args:
        monkeypatch: pytest monkeypatch。

    Returns:
        None。

    Raises:
        AssertionError: 未进入 responses 实现时中断。
    """
    called: List[bool] = []

    async def fake_responses(base_url, api_key, model, messages, temperature, json_mode) -> str:
        """responses 替身（记录参数）。

        Returns:
            str: 固定文本。
        """
        called.append(True)
        assert base_url == "https://api.openai.com/v1" and model == "gpt-5"
        return "ok"

    monkeypatch.setattr(llm, "_call_openai_responses", fake_responses)
    text = await _call_channel(
        ("https://api.openai.com/v1", "k", "gpt-5", "ch", "openai_responses"),
        [{"role": "user", "content": "hi"}], None, 0.7, False)
    assert text == "ok" and called == [True]
