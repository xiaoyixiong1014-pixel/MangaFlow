"""智能体接口测试模块（文件级说明）。

覆盖链路：
- POST /api/agent/intake：意图承接返回 intent 字段（LLM 或规则降级均可）
- POST /api/agent/chat/stream：SSE 流式对话必有 stage/message/done 事件
- GET /api/agent/threads：对话后会话列表非空
"""

import pytest

from tests.conftest import parse_sse_events, register_user


async def test_yituicheng_fanhui_intent(client, auth):
    """意图承接：响应包含 intent/confidence/mode 字段。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    resp = await client.post(
        "/api/agent/intake",
        headers=auth["headers"],
        json={"message": "帮我写一个关于夏日祭的漫剧剧本"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "intent" in body and body["intent"]
    assert 0.0 <= float(body["confidence"]) <= 1.0
    assert body["mode"] in ("llm", "rule")


async def test_duihua_sse_shijian_liu(client, auth):
    """SSE 对话流：依次出现 stage / message / done 事件（真实或降级均可）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 事件序列断言失败时中断。
    """
    resp = await client.post(
        "/api/agent/chat/stream",
        headers=auth["headers"],
        json={"message": "你好，请介绍一下你自己"},
    )
    assert resp.status_code == 200, resp.text
    events = parse_sse_events(resp.text)
    names = [name for name, _ in events]

    assert "stage" in names
    assert "message" in names  # LLM 降级也有离线模板 message
    assert "done" in names
    # 事件顺序：stage → message → done
    assert names.index("stage") < names.index("message") < names.index("done")

    # stage 事件负载含意图；done 事件负载含 thread_id
    stage_payload = next(data for name, data in events if name == "stage")
    assert stage_payload and "intent" in stage_payload
    done_payload = next(data for name, data in events if name == "done")
    assert done_payload and done_payload.get("thread_id")

    # message 负载含文本与智能体名
    message_payload = next(data for name, data in events if name == "message")
    assert message_payload.get("text")
    assert message_payload.get("agent")


async def test_huataliebiao_feikong(client, auth):
    """对话完成后会话列表非空（会话摘要落库 agent_threads）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 列表为空时中断。
    """
    # 先发起一次对话（等待 SSE 流完整返回即已落库）
    chat = await client.post(
        "/api/agent/chat/stream",
        headers=auth["headers"],
        json={"message": "画布节点怎么连线？"},
    )
    assert chat.status_code == 200
    events = parse_sse_events(chat.text)
    thread_id = next(
        (data.get("thread_id") for name, data in events if name == "done" and data), None
    )
    assert thread_id

    resp = await client.get("/api/agent/threads", headers=auth["headers"])
    assert resp.status_code == 200
    threads = resp.json()
    assert len(threads) >= 1
    assert any(t["thread_id"] == thread_id for t in threads)
    assert all("stage" in t and "updated_at" in t for t in threads)


async def test_huata_xiaoxi_huifang(client, auth):
    """会话消息回放：GET threads/{id}/messages 返回角色化消息列表。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    chat = await client.post(
        "/api/agent/chat/stream",
        headers=auth["headers"],
        json={"message": "帮我设计一个角色"},
    )
    assert chat.status_code == 200
    done_payload = next(
        (data for name, data in parse_sse_events(chat.text) if name == "done" and data), None
    )
    thread_id = done_payload["thread_id"]

    resp = await client.get(
        f"/api/agent/threads/{thread_id}/messages", headers=auth["headers"]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["thread_id"] == thread_id
    roles = [m["role"] for m in body["messages"]]
    assert "user" in roles
    assert "assistant" in roles

    # 他人会话 404
    other = await register_user(client, prefix="threadother")
    resp2 = await client.get(
        f"/api/agent/threads/{thread_id}/messages",
        headers={"Authorization": f"Bearer {other['access_token']}"},
    )
    assert resp2.status_code == 404
