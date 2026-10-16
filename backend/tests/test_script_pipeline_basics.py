"""剧本生成管线基础行为测试模块（文件级说明，P0 断链修复四件套验证）。

覆盖链路：
- 抽取源改判：实体抽取优先使用生成的完整剧本 content（场景对白）作为源文本，
  而非用户原始输入 raw_text（强制规则路，验证角色从 content 对白中抽出、
  conflicts 检出、fences 落库）；
- 生成结构校验 + 重试成功（PRD S-03）：第一轮输出坏 JSON（缺 scenes）、
  第二轮输出合法剧本 → 事件序列含 done、done.content.scenes 非空、版本+1；
- 生成结构校验重试耗尽：三轮全坏 → 有 error、无 done、content 保持保存输入
  时的结构、版本不加（不落库）；
- 并发幂等保护：同项目同时发起两个生成请求（mock 流内 sleep 0.5），
  恰好一个 200 一个 409（进程内生成锁）。

SSE 请求统一使用 httpx POST 且 headers 携带认证令牌，响应用
tests.conftest.parse_sse_events 解析为 (event, data) 列表后断言。
"""

import asyncio
import json
from typing import Any, Dict

import pytest
from sqlalchemy import select

from app.agents import llm_adapter
from app.models import Script
from tests.conftest import create_project, parse_sse_events

# 保存输入时的原始文本（idea 模式一句话创意——改造前抽取只会拿到这句话）
_SAMPLE_RAW = "一句话创意：两个少女约定一起成为漫画家的故事。"

# 三轮重试全部输出的坏 JSON：仅缺 scenes（合法 JSON 但不满足结构校验）
_BAD_JSON = json.dumps(
    {"title": "坏剧本", "logline": "缺场景的剧本", "outline": [{"act": "第一幕", "summary": "概要"}]},
    ensure_ascii=False,
)


def _valid_script() -> Dict[str, Any]:
    """构造一份可通过 ScriptContent 结构校验的合法剧本 dict（内部函数）。

    Returns:
        Dict[str, Any]: 含 title/logline/outline（3 幕）/scenes（3 场，均含
        对白与正数时长）的完整剧本结构。

    Raises:
        无：纯结构构造。
    """
    return {
        "title": "天台之约",
        "logline": "两位少女约定一起成为漫画家",
        "outline": [
            {"act": "第一幕", "summary": "天台相遇立下约定"},
            {"act": "第二幕", "summary": "共同赶稿磨合分歧"},
            {"act": "第三幕", "summary": "赛场获奖兑现约定"},
        ],
        "scenes": [
            {"scene_no": 1, "title": "天台", "plot": "两人初遇并约定",
             "dialogue": [{"character": "林小满", "line": "我们一起拿金奖吧。"}],
             "duration_sec": 30},
            {"scene_no": 2, "title": "画室", "plot": "深夜赶稿互相扶持",
             "dialogue": [{"character": "陈默", "line": "分镜我来帮你。"}],
             "duration_sec": 25},
            {"scene_no": 3, "title": "赛场", "plot": "作品获奖",
             "dialogue": [{"character": "林小满", "line": "我们做到了。"}],
             "duration_sec": 35},
        ],
    }


def _structured_content() -> Dict[str, Any]:
    """构造含「林小满 / 小满」别名对的结构化剧本 content（内部函数）。

    Returns:
        Dict[str, Any]: 可整体包装 source=user 落库的剧本 dict；
        两个角色名互相包含（小满 ⊂ 林小满），用于验证别名冲突检测。

    Raises:
        无：纯结构构造。
    """
    return {
        "title": "天台之约",
        "logline": "两个少女约定成为漫画家",
        "outline": [{"act": "第一幕", "summary": "天台相遇立下约定"}],
        "scenes": [
            {"scene_no": 1, "title": "天台", "plot": "两人在天台相遇",
             "dialogue": [
                 {"character": "林小满", "line": "我要成为最强的漫画家。"},
                 {"character": "小满", "line": "好，我们一起加油。"},
             ],
             "duration_sec": 30},
        ],
    }


async def _make_project_with_input(client, auth) -> dict:
    """辅助：创建项目并保存一句话创意输入（content 仅含 input_mode，内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        dict: 项目响应 JSON（含 id）。

    Raises:
        AssertionError: 任一接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title="管线项目")
    resp = await client.put(
        f"/api/projects/{project['id']}/script/input",
        headers=auth["headers"],
        json={"mode": "idea", "raw_text": _SAMPLE_RAW},
    )
    assert resp.status_code == 200, resp.text
    return project


async def test_chouqu_yuan_gai_pan_shi_yong_content(client, auth, db, monkeypatch):
    """抽取源改判：强制规则路（llm_extract→None）下，角色应从 content 的
    场景对白中抽出（而非 raw_text），conflicts 检出别名冲突并落库 fences。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直接改写 script.content 造数 + 断言 fences 落库）。
        monkeypatch: 用例级补丁（把 llm_extract 置为恒返 None）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from app.services import script_service

    # 强制走规则抽取路：LLM 路恒返回 None
    async def _fake_llm_extract(raw_text):
        """替换 script_service.llm_extract：恒返 None（内部函数）。

        Args:
            raw_text: 抽取源文本（忽略）。

        Returns:
            None：强制调用方降级到规则抽取。

        Raises:
            无。
        """
        return None

    monkeypatch.setattr(script_service, "llm_extract", _fake_llm_extract)

    project = await _make_project_with_input(client, auth)

    # 用 db 夹具把 content 写为含场次对白的结构化剧本（模拟已生成/手动保存）
    script_row = (
        await db.execute(select(Script).where(Script.project_id == project["id"]))
    ).scalar_one()
    script_row.content = {"value": _structured_content(), "source": "user"}
    await db.commit()

    extract = await client.post(
        f"/api/projects/{project['id']}/script/extract", headers=auth["headers"]
    )
    assert extract.status_code == 200, extract.text
    data = extract.json()

    # 角色来自 content 场景对白（「角色名：台词」逐行格式被规则路命中）
    names = {c["name"] for c in data["characters"]}
    assert "林小满" in names

    # 别名冲突：小满 ⊂ 林小满 → conflicts 非空
    assert data["conflicts"], "应检出「小满/林小满」别名冲突"

    # fences 落库（RD-05/RD-06 记录位）
    await db.refresh(script_row)
    assert script_row.fences is not None
    fence_names = {c["name"] for c in (script_row.fences.get("characters") or [])}
    assert "林小满" in fence_names
    assert script_row.fences.get("conflicts")


async def test_jiaoyan_chong_shi_cheng_gong(client, auth, monkeypatch):
    """校验重试成功：第一轮坏 JSON（缺 scenes）、第二轮合法剧本 →
    事件序列含 done、done.content.scenes 非空、版本 +1。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 用例级补丁（mock llm.chat_stream 两轮输出）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    calls = {"n": 0}

    async def fake_chat_stream(state_aware, messages, **kwargs):
        """替换 AgentLLM.chat_stream：首轮坏 JSON，第二轮合法剧本（内部函数）。

        Args:
            state_aware: 适配器占位参数（None）。
            messages: OpenAI 格式消息列表（忽略）。
            **kwargs: 其余关键字参数（agent_name 等，忽略）。

        Yields:
            str: 文本增量片段（此处单块输出完整 JSON）。

        Raises:
            无。
        """
        calls["n"] += 1
        if calls["n"] == 1:
            yield _BAD_JSON
        else:
            yield json.dumps(_valid_script(), ensure_ascii=False)

    monkeypatch.setattr(llm_adapter.llm, "chat_stream", fake_chat_stream)

    project = await _make_project_with_input(client, auth)
    resp = await client.post(
        f"/api/projects/{project['id']}/script/generate/stream", headers=auth["headers"]
    )
    assert resp.status_code == 200, resp.text

    events = parse_sse_events(resp.text)
    names = [n for n, _ in events]
    # 确实发生了两轮流式尝试，且最终发出 done
    assert calls["n"] == 2
    assert "done" in names

    done = next(d for n, d in events if n == "done")
    # done.content 为 RD-03 字段级包装：scenes.value 非空
    scenes_val = done["content"]["scenes"]["value"]
    assert isinstance(scenes_val, list) and scenes_val
    # 保存输入建行 version=1，生成成功后 +1
    assert done["version"] == 2


async def test_jiaoyan_chong_shi_hao_jin(client, auth, db, monkeypatch):
    """校验重试耗尽：三轮全坏 → 有 error、无 done、content 保持保存输入
    时的结构（不落库）、版本不加。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言 content/version 未被生成路径改写）。
        monkeypatch: 用例级补丁（mock chat_stream 恒输出坏 JSON）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """

    async def bad_chat_stream(state_aware, messages, **kwargs):
        """替换 AgentLLM.chat_stream：每轮均输出缺 scenes 的坏 JSON（内部函数）。

        Args:
            state_aware: 适配器占位参数（None）。
            messages: OpenAI 格式消息列表（忽略）。
            **kwargs: 其余关键字参数（agent_name 等，忽略）。

        Yields:
            str: 文本增量片段（坏 JSON）。

        Raises:
            无。
        """
        yield _BAD_JSON

    monkeypatch.setattr(llm_adapter.llm, "chat_stream", bad_chat_stream)

    project = await _make_project_with_input(client, auth)
    resp = await client.post(
        f"/api/projects/{project['id']}/script/generate/stream", headers=auth["headers"]
    )
    assert resp.status_code == 200, resp.text

    events = parse_sse_events(resp.text)
    names = [n for n, _ in events]
    assert "error" in names
    assert "done" not in names
    # 耗尽文案
    error_payload = next(d for n, d in events if n == "error")
    assert "已重试 3 次" in error_payload["message"]

    # 不落库：content 仍是保存输入时的结构，版本未增加
    script_row = (
        await db.execute(select(Script).where(Script.project_id == project["id"]))
    ).scalar_one()
    assert script_row.content == {"input_mode": "idea"}
    assert script_row.version == 1


async def test_bing_fa_suo_409(client, auth, monkeypatch):
    """并发幂等保护：同项目同时发起两个生成请求（mock 流内 sleep 0.5 保证
    锁占用窗口），恰好一个 200 一个 409。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 用例级补丁（mock chat_stream 延迟 0.5s 后输出合法剧本）。

    Returns:
        None。

    Raises:
        AssertionError: 状态码组合不是 {200, 409} 时中断。
    """

    async def slow_chat_stream(state_aware, messages, **kwargs):
        """替换 AgentLLM.chat_stream：延迟 0.5s 后输出合法剧本（内部函数）。

        Args:
            state_aware: 适配器占位参数（None）。
            messages: OpenAI 格式消息列表（忽略）。
            **kwargs: 其余关键字参数（agent_name 等，忽略）。

        Yields:
            str: 文本增量片段（合法剧本 JSON）。

        Raises:
            无。
        """
        await asyncio.sleep(0.5)
        yield json.dumps(_valid_script(), ensure_ascii=False)

    monkeypatch.setattr(llm_adapter.llm, "chat_stream", slow_chat_stream)

    project = await _make_project_with_input(client, auth)

    async def do_generate():
        """发起一次生成流式请求并返回状态码（内部函数）。

        Returns:
            int: HTTP 状态码（200 或 409）。

        Raises:
            无：非 2xx 响应由状态码表达，不抛异常。
        """
        r = await client.post(
            f"/api/projects/{project['id']}/script/generate/stream",
            headers=auth["headers"],
        )
        return r.status_code

    code_a, code_b = await asyncio.gather(do_generate(), do_generate())
    assert sorted([code_a, code_b]) == [200, 409]
