"""P-5 剧本子图编排测试模块（文件级说明，P-1 范式级重构 Task 6）。

覆盖链路（子图全部以 **InMemorySaver** 编译 + mock 模块级 _llm_chat /
_llm_chat_stream 驱动，数据库走 tests.conftest 的测试库；REST 驱动入口经
monkeypatch 替换编译子图单例，绝不触碰主库检查点表）：
1. 一键串跑：build_script_pipeline_subgraph(auto_confirm=True) → 三处确认闸口
   自动放行、无 interrupt，三态齐备（characters/episodes/bodies）、阶段到 body；
2. 三 interrupt 暂停/恢复：default 模式首次停在 stage=characters →
   resume approved → outline → resume approved → body → resume approved → END；
3. 退回重生：stage=characters 处 resume rejected（带 reason）→ 回退
   sp_character_planner 重新生成（LLM 第二次调用）后再次停在同闸口；
4. Send 并行逐集：2 集大纲 → sp_episode_writer 调用 2 次、bodies 含 2 个
   episode_id；
5. 检查点续跑与隔离：同 thread_id 二次读取拿到中间状态；换 thread_id 状态隔离；
6. REST 集成：POST pipeline/run/stream 首次 → SSE 含 node + pending_confirm；
   POST pipeline/resume(approved) → 续跑到下一闸口；GET pipeline/status 返回
   pending_stage；重复 run/stream 幂等回放（不重复调 LLM）；
7. 越权：他人项目/他人 thread → 404。

LLM 桩说明：monkeypatch app.agents.script_pipeline 的模块级 _llm_chat /
_llm_chat_stream（子图节点经 plan_characters/plan_episodes/stream_episode_body/
plan_episode_scenes 间接调用，运行时查找模块全局，替换即生效）。
"""

import json
from typing import Any, Dict, List

import httpx
import pytest
from fastapi import HTTPException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import select

import app.api.script_pipeline as sp_api_mod
from app.agents import script_pipeline as sp_mod
from app.agents.script_pipeline_graph import (
    NODE_CHARACTER_PLANNER,
    NODE_EPISODE_PLANNER,
    build_script_pipeline_subgraph,
)
from app.models import Character, Episode, Project, Script
from tests.conftest import create_project, parse_sse_events, register_user

# 保存输入时的原始文本（idea 模式一句话创意）
_SAMPLE_RAW = "一句话创意：两个少女约定一起成为漫画家的故事。"

# 正文流单批输出的固定增量块（单段情绪曲线 → 每集 1 次流调用）
_STREAM_CHUNKS = ["天台的风吹过，", "林小满握紧了拳头。"]

# 正文流拼接结果（落库断言用）
_BODY_TEXT = "".join(_STREAM_CHUNKS)


def _characters_payload() -> str:
    """构造角色班底 LLM 输出 JSON（内部函数：2 角色，lead+support）。

    Returns:
        str: {"characters": [林小满(lead), 陈默(support)]} 的 JSON 文本。

    Raises:
        无：纯结构构造。
    """
    return json.dumps({
        "characters": [
            {"name": "林小满", "role_type": "lead", "age": "约17岁",
             "personality": "倔强善良，遇挫不折", "appearance": "短发少女，戴红色发卡",
             "motivation": "和挚友一起成为漫画家", "arc": "从畏缩退让到坚定追梦",
             "relations": [{"target": "陈默", "relation": "漫画搭档"}]},
            {"name": "陈默", "role_type": "support", "age": "约18岁",
             "personality": "沉稳寡言，内心炽热", "appearance": "高瘦少年，黑框眼镜",
             "motivation": "守护小满的梦想", "arc": "从旁观者到并肩者",
             "relations": [{"target": "林小满", "relation": "漫画搭档"}]},
        ]
    }, ensure_ascii=False)


def _episodes_payload(count: int) -> str:
    """构造分集大纲 LLM 输出 JSON（内部函数：集数可配置，单段情绪曲线）。

    Args:
        count: 分集条数。

    Returns:
        str: {"episodes": [...]} 的 JSON 文本（emotion_curve 单段不分批）。

    Raises:
        无：纯结构构造。
    """
    return json.dumps({"episodes": [
        {
            "title": f"第{idx + 1}集：追梦之路",
            "logline": f"两个少女朝漫画家梦想迈进的第 {idx + 1} 步",
            "core_event": f"第 {idx + 1} 集核心事件",
            "characters": ["林小满", "陈默"],
            "emotion_curve": "热血",
            "duration_sec": 90,
        }
        for idx in range(count)
    ]}, ensure_ascii=False)


def _scenes_payload() -> str:
    """构造结构化场次 LLM 输出 JSON（内部函数：2 场，含对白与地点）。

    Returns:
        str: {"locations": [...], "scenes": [...]} 的 JSON 文本
        （结构通过 validate_script_body_content 的 ScriptBodyContent 门槛）。

    Raises:
        无：纯结构构造。
    """
    return json.dumps({
        "locations": [
            {"name": "学校天台", "description": "黄昏的学校天台", "time_of_day": "dusk"},
        ],
        "scenes": [
            {"scene_no": 1, "title": "天台立约", "action": "两人站上天台边缘远眺城市",
             "plot": "立约", "location": "学校天台", "time_of_day": "dusk",
             "int_ext": "ext", "sfx": ["风声"], "emotion": "热血",
             "dialogue": [{"character": "林小满", "type": "对白",
                           "emotion": "坚定", "line": "我们一起拿金奖吧。"}],
             "duration_sec": 45},
            {"scene_no": 2, "title": "画室赶稿", "action": "深夜画室两人赶稿",
             "plot": "赶稿", "location": "学校天台", "time_of_day": "night",
             "int_ext": "int", "sfx": ["笔尖沙沙"], "emotion": "温情",
             "dialogue": [{"character": "陈默", "type": "旁白",
                           "line": "分镜我来帮你。"}],
             "duration_sec": 45},
        ],
    }, ensure_ascii=False)


class _PipelineLLMStub:
    """剧本子图 LLM 桩（测试辅助类）：按 agent_name 路由返回并记录调用。

    Attributes:
        episode_count: 分集大纲桩返回的集数（用例可按需调整，缺省 3）。
        chat_calls: 非流式调用记录 [{"agent", "temperature"}, ...]。
        stream_calls: 流式调用记录 [{"agent"}, ...]（每次调用 1 条）。
    """

    def __init__(self) -> None:
        """初始化桩对象（清空调用记录，集数缺省 3）。

        Returns:
            None。

        Raises:
            无。
        """
        self.episode_count = 3
        self.chat_calls: List[Dict[str, Any]] = []
        self.stream_calls: List[Dict[str, Any]] = []

    async def chat(self, messages, *, temperature, **kwargs) -> str:
        """替换 _llm_chat：按 agent_name 返回对应阶段的固定 JSON。

        Args:
            messages: OpenAI 格式消息列表（忽略）。
            temperature: 采样温度（记录进 chat_calls）。
            **kwargs: 其余关键字参数（取 agent_name 路由）。

        Returns:
            str: 预置的阶段 payload（characters/episodes/scenes JSON）。

        Raises:
            AssertionError: 遇到未预期的 agent_name 时中断（发现桩路由遗漏）。
        """
        agent = str(kwargs.get("agent_name") or "character_planner")
        self.chat_calls.append({"agent": agent, "temperature": temperature})
        if agent == "character_planner":
            return _characters_payload()
        if agent == "episode_planner":
            return _episodes_payload(self.episode_count)
        if agent == "episode_writer":
            return _scenes_payload()
        raise AssertionError(f"LLM 桩未覆盖的 agent_name：{agent}")

    async def stream(self, messages, *, agent_name="episode_writer", **kwargs):
        """替换 _llm_chat_stream：恒产出 2 块固定增量。

        Args:
            messages: OpenAI 格式消息列表（忽略）。
            agent_name: 调用方 Agent 标识（记录进 stream_calls）。
            **kwargs: 其余关键字参数（忽略）。

        Yields:
            str: 固定正文增量块（拼接为「天台的风吹过，林小满握紧了拳头。」）。

        Raises:
            无。
        """
        self.stream_calls.append({"agent": str(agent_name)})
        for chunk in _STREAM_CHUNKS:
            yield chunk

    def agent_calls(self, agent: str) -> List[Dict[str, Any]]:
        """统计指定 Agent 的非流式调用记录（测试辅助）。

        Args:
            agent: Agent 标识（character_planner/episode_planner/episode_writer）。

        Returns:
            List[Dict[str, Any]]: 该 Agent 的调用记录列表。

        Raises:
            无。
        """
        return [c for c in self.chat_calls if c["agent"] == agent]


@pytest.fixture
def llm_stub(monkeypatch):
    """夹具：替换 script_pipeline 模块级 _llm_chat/_llm_chat_stream 为桩。

    Args:
        monkeypatch: pytest monkeypatch 夹具。

    Returns:
        _PipelineLLMStub: 桩实例（测试内断言 chat_calls/stream_calls）。

    Raises:
        无。
    """
    stub = _PipelineLLMStub()
    monkeypatch.setattr(sp_mod, "_llm_chat", stub.chat)
    monkeypatch.setattr(sp_mod, "_llm_chat_stream", stub.stream)
    return stub


def _new_subgraph(auto_confirm: bool = False) -> Any:
    """构建带 InMemorySaver 检查点的剧本子图（测试辅助，绝不连主库）。

    Args:
        auto_confirm: 是否一键串跑（跳过三处确认闸口）。

    Returns:
        Any: 编译子图（checkpointer=InMemorySaver，支持 interrupt/resume）。

    Raises:
        Exception: 图定义非法时由 LangGraph 抛出。
    """
    return build_script_pipeline_subgraph(
        checkpointer=InMemorySaver(), auto_confirm=auto_confirm)


async def _run_to_interrupt(graph: Any, graph_input: Any, config: dict) -> Any:
    """运行子图直至出现 interrupt 更新（测试辅助，同 test_agent_hitl 模式）。

    Args:
        graph: 编译子图。
        graph_input: 图输入（首次为 dict，续跑为 Command(resume=...)）。
        config: 运行配置（含 thread_id）。

    Returns:
        Any: interrupt 值（形如 {"type":..., "stage":..., "payload":...}）。

    Raises:
        AssertionError: 本轮运行未触发 interrupt 时中断。
    """
    interrupt_value = None
    async for chunk in graph.astream(graph_input, config=config, stream_mode="updates"):
        if isinstance(chunk, dict) and "__interrupt__" in chunk:
            interrupts = chunk["__interrupt__"] or ()
            assert interrupts, "interrupt 更新为空"
            interrupt_value = interrupts[0].value
            break
    assert interrupt_value is not None, "子图运行未触发 interrupt"
    return interrupt_value


async def _make_project(client: httpx.AsyncClient, headers: Dict[str, str],
                        target_duration: int = 600) -> Dict[str, Any]:
    """辅助：创建项目并保存一句话创意输入（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        headers: 认证请求头。
        target_duration: 目标时长（缺省 600 秒 → 子图缺省建议 3 集）。

    Returns:
        dict: 项目响应 JSON（含 id）。

    Raises:
        AssertionError: 任一接口返回非 200 时中断。
    """
    project = await create_project(client, headers, title="剧本子图项目",
                                   target_duration=target_duration)
    resp = await client.put(
        f"/api/projects/{project['id']}/script/input",
        headers=headers,
        json={"mode": "idea", "raw_text": _SAMPLE_RAW},
    )
    assert resp.status_code == 200, resp.text
    return project


# ---------------------------------------------------------------------------
# 1. 一键串跑：auto_confirm=True 全流程无 interrupt
# ---------------------------------------------------------------------------
async def test_auto_confirm_full_run(client, auth, db, llm_stub):
    """一键串跑链路：auto_confirm 图单次 ainvoke 跑通三阶段，无 interrupt。

    Args:
        client: 内存 HTTP 客户端（造项目与剧本输入）。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言落库）。
        llm_stub: LLM 桩夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project(client, auth["headers"])
    project_id = int(project["id"])
    graph = _new_subgraph(auto_confirm=True)

    final = await graph.ainvoke(
        {"project_id": project_id, "user_id": auth["user_id"]},
        config={"configurable": {"thread_id": "sp_auto"}},
    )

    assert final.get("auto_confirm") is True  # 入口注入生效
    assert final.get("error") == ""
    flags = final.get("confirmed_flags") or {}
    assert flags == {"characters": True, "episodes": True, "body": True}
    bodies = final.get("bodies") or []
    assert len(bodies) == 3 and all(b["ok"] for b in bodies)
    # 三态齐备：角色候选 / 大纲计划 / 逐集正文
    assert len(final.get("character_candidates") or []) == 2
    assert len(final.get("episodes_plan") or []) == 3
    assert all(b["raw_text"] == _BODY_TEXT for b in bodies)
    # LLM 调用：角色 1 次 + 大纲 1 次 + 场次结构化 3 次；正文流 3 次
    assert len(llm_stub.agent_calls("character_planner")) == 1
    assert len(llm_stub.agent_calls("episode_planner")) == 1
    assert len(llm_stub.stream_calls) == 3

    # 落库：阶段到 body、3 集 scripted、scripts 3 行、角色 2 行
    proj = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()
    assert proj.script_phase == "body"
    episodes = (
        await db.execute(select(Episode).where(Episode.project_id == project_id))
    ).scalars().all()
    assert len(episodes) == 3 and all(e.status == "scripted" for e in episodes)
    scripts = (
        await db.execute(
            select(Script).where(Script.project_id == project_id, Script.episode_id.is_not(None)))
    ).scalars().all()
    assert len(scripts) == 3 and all(s.version == 1 for s in scripts)
    chars = (
        await db.execute(select(Character).where(Character.project_id == project_id))
    ).scalars().all()
    assert {c.name for c in chars} == {"林小满", "陈默"}


# ---------------------------------------------------------------------------
# 2. 三 interrupt 暂停/恢复（default 模式）
# ---------------------------------------------------------------------------
async def test_three_interrupts_pause_and_resume(client, auth, llm_stub):
    """三闸口暂停/恢复：characters → outline → body → END 逐段 approved。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        llm_stub: LLM 桩夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project(client, auth["headers"])
    graph = _new_subgraph()
    config = {"configurable": {"thread_id": "sp_three"}}

    first = await _run_to_interrupt(graph, {"project_id": int(project["id"])}, config)
    assert first["type"] == "confirm_characters" and first["stage"] == "characters"
    assert first["payload"]["count"] == 2
    snapshot = await graph.aget_state(config)
    assert tuple(snapshot.next) == (NODE_CHARACTER_PLANNER,)

    second = await _run_to_interrupt(graph, Command(resume={"decision": "approved"}), config)
    assert second["type"] == "confirm_episodes" and second["stage"] == "outline"
    assert second["payload"]["count"] == 3

    third = await _run_to_interrupt(graph, Command(resume={"decision": "approved"}), config)
    assert third["type"] == "confirm_body" and third["stage"] == "body"
    assert third["payload"]["count"] == 3

    final = await graph.ainvoke(Command(resume={"decision": "approved"}), config=config)
    assert (final.get("confirmed_flags") or {}).get("body") is True
    assert len(final.get("bodies") or []) == 3


# ---------------------------------------------------------------------------
# 3. 退回重生：rejected → 回退角色节点重新生成（LLM 第二次调用）
# ---------------------------------------------------------------------------
async def test_reject_regenerate_characters(client, auth, llm_stub):
    """退回链路：confirm_characters 处 rejected → 回退重生后再次停在同闸口。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        llm_stub: LLM 桩夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project(client, auth["headers"])
    graph = _new_subgraph()
    config = {"configurable": {"thread_id": "sp_reject"}}

    first = await _run_to_interrupt(graph, {"project_id": int(project["id"])}, config)
    assert first["type"] == "confirm_characters"
    assert len(llm_stub.agent_calls("character_planner")) == 1

    # 退回（带意见）→ 回退重生 → 再次停在 characters 闸口
    second = await _run_to_interrupt(
        graph, Command(resume={"decision": "rejected", "reason": "主角性格再强硬些"}), config)
    assert second["type"] == "confirm_characters"
    assert second["payload"]["count"] == 2
    # 角色生成被第二次调用（重生），且未进入大纲阶段（无大纲 LLM 调用）
    assert len(llm_stub.agent_calls("character_planner")) == 2
    assert llm_stub.agent_calls("episode_planner") == []
    # 退回意见留存于状态并播报
    mid = await graph.aget_state(config)
    assert mid.values.get("reject_reason") == "主角性格再强硬些"
    assert mid.values.get("retry_stage") == "characters"

    # 重生稿确认 → 继续前进到大纲闸口
    third = await _run_to_interrupt(graph, Command(resume={"decision": "approved"}), config)
    assert third["type"] == "confirm_episodes"


# ---------------------------------------------------------------------------
# 4. Send 并行逐集：2 集大纲 → writer 调用 2 次
# ---------------------------------------------------------------------------
async def test_send_parallel_two_episodes(client, auth, llm_stub):
    """Send 并行分发：2 集大纲 → bodies 含 2 个 episode_id、writer 调用 2 次。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        llm_stub: LLM 桩夹具（集数改为 2）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    llm_stub.episode_count = 2
    project = await _make_project(client, auth["headers"])
    graph = _new_subgraph(auto_confirm=True)
    final = await graph.ainvoke(
        {"project_id": int(project["id"])},
        config={"configurable": {"thread_id": "sp_parallel"}},
    )
    bodies = final.get("bodies") or []
    assert len(bodies) == 2
    assert len({b["episode_id"] for b in bodies}) == 2  # 并行叶子各写一条
    # mock 计数：正文流 2 次（每集 1 批）、场次结构化 2 次
    assert len(llm_stub.stream_calls) == 2
    assert len(llm_stub.agent_calls("episode_writer")) == 2


# ---------------------------------------------------------------------------
# 5. 检查点续跑与线程隔离
# ---------------------------------------------------------------------------
async def test_checkpoint_resume_and_thread_isolation(client, auth, llm_stub):
    """检查点：同 thread 二次读到中间状态；换 thread 状态隔离（各自首跑）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        llm_stub: LLM 桩夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project(client, auth["headers"])
    project_id = int(project["id"])
    graph = _new_subgraph()
    config_a = {"configurable": {"thread_id": "sp_ckpt_a"}}
    config_b = {"configurable": {"thread_id": "sp_ckpt_b"}}

    await _run_to_interrupt(graph, {"project_id": project_id}, config_a)
    state_a = await graph.aget_state(config_a)
    assert tuple(state_a.next) == (NODE_CHARACTER_PLANNER,)
    assert state_a.values.get("raw_text") == _SAMPLE_RAW
    assert len(llm_stub.agent_calls("character_planner")) == 1

    # 同 thread 二次运行到中断：复用已落库候选（不重复调 LLM）
    await _run_to_interrupt(graph, Command(resume={"decision": "approved"}), config_a)
    assert len(llm_stub.agent_calls("character_planner")) == 1

    # 换 thread：状态隔离（无中间状态，首跑重新生成角色）
    state_b = await graph.aget_state(config_b)
    assert not state_b.values  # 全新线程无检查点
    assert state_b.next == ()
    await _run_to_interrupt(graph, {"project_id": project_id}, config_b)
    assert len(llm_stub.agent_calls("character_planner")) == 2  # 新线程独立生成


# ---------------------------------------------------------------------------
# 6. REST 集成：run/stream → pending_confirm → resume → status
# ---------------------------------------------------------------------------
async def test_rest_run_resume_and_status(client, auth, llm_stub, monkeypatch):
    """REST 集成：run/stream 首跑暂停 → resume 续跑 → status 反映断点。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        llm_stub: LLM 桩夹具。
        monkeypatch: 把端点使用的编译子图单例替换为 InMemorySaver 编译图。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project(client, auth["headers"])
    project_id = int(project["id"])
    graph = _new_subgraph()
    thread_id = f"script_{project_id}_{auth['user_id']}"

    async def _fake_get_graph():
        """替换 get_compiled_script_pipeline：恒返回测试用 InMemory 子图。

        Returns:
            Any: 编译子图（InMemorySaver）。

        Raises:
            无。
        """
        return graph

    monkeypatch.setattr(sp_api_mod, "get_compiled_script_pipeline", _fake_get_graph)

    # 1) 首跑：SSE 含 node（sp_input/sp_character_planner）+ pending_confirm(characters)
    resp = await client.post(
        f"/api/projects/{project_id}/script/pipeline/run/stream",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    events = parse_sse_events(resp.text)
    names = [n for n, _ in events]
    assert "node" in names and "pending_confirm" in names and "done" in names
    nodes = {d["node"] for n, d in events if n == "node"}
    assert {"sp_input", "sp_character_planner"} <= nodes
    pending = next(d for n, d in events if n == "pending_confirm")
    assert pending["stage"] == "characters"
    assert pending["confirm_type"] == "confirm_characters"
    assert pending["payload"]["count"] == 2
    done = next(d for n, d in events if n == "done")
    assert done["thread_id"] == thread_id and done["interrupted"] is True
    assert len(llm_stub.agent_calls("character_planner")) == 1

    # 2) status：反映挂起闸口与断点节点（前端刷新恢复用）
    status = await client.get(
        f"/api/projects/{project_id}/script/pipeline/status", headers=auth["headers"])
    assert status.status_code == 200, status.text
    status_body = status.json()
    assert status_body["thread_id"] == thread_id
    assert status_body["has_pending_confirm"] is True
    assert status_body["pending_stage"] == "characters"
    assert status_body["current_stage"] == NODE_CHARACTER_PLANNER
    assert status_body["summary"]["character_count"] == 2

    # 3) 幂等重入 run/stream：不重跑图（LLM 不再调用），原样回放待确认
    again = await client.post(
        f"/api/projects/{project_id}/script/pipeline/run/stream", headers=auth["headers"])
    again_events = parse_sse_events(again.text)
    again_pending = next(d for n, d in again_events if n == "pending_confirm")
    assert again_pending["stage"] == "characters"
    assert "node" not in [n for n, _ in again_events]
    assert len(llm_stub.agent_calls("character_planner")) == 1

    # 4) resume approved → 续跑到大纲闸口
    resumed = await client.post(
        f"/api/projects/{project_id}/script/pipeline/resume",
        headers=auth["headers"],
        json={"decision": "approved"},
    )
    assert resumed.status_code == 200, resumed.text
    resumed_events = parse_sse_events(resumed.text)
    assert NODE_EPISODE_PLANNER in {d["node"] for n, d in resumed_events if n == "node"}
    resumed_pending = next(d for n, d in resumed_events if n == "pending_confirm")
    assert resumed_pending["stage"] == "outline"
    assert resumed_pending["payload"]["count"] == 3
    # 落库：角色已入池、阶段推进到 outline
    chars = (
        await client.get(f"/api/projects/{project_id}/script/characters",
                         headers=auth["headers"])
    ).json()
    assert {c["name"] for c in chars["confirmed"]} == {"林小满", "陈默"}
    assert chars["phase"] == "outline"

    # 5) status 反映断点已前移到大纲闸口
    status2 = await client.get(
        f"/api/projects/{project_id}/script/pipeline/status", headers=auth["headers"])
    assert status2.json()["pending_stage"] == "outline"


# ---------------------------------------------------------------------------
# 7. 越权：他人项目 404 / 他人 thread 404
# ---------------------------------------------------------------------------
async def test_pipeline_cross_user_404(client, auth, llm_stub, monkeypatch):
    """越权防护：他人项目访问 404；检查点归属不符 404（不泄露存在性）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文（项目所有者）。
        llm_stub: LLM 桩夹具。
        monkeypatch: 替换编译子图单例为 InMemorySaver 图。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project(client, auth["headers"])
    project_id = int(project["id"])
    graph = _new_subgraph()

    async def _fake_get_graph():
        """替换 get_compiled_script_pipeline：恒返回测试用 InMemory 子图。

        Returns:
            Any: 编译子图（InMemorySaver）。

        Raises:
            无。
        """
        return graph

    monkeypatch.setattr(sp_api_mod, "get_compiled_script_pipeline", _fake_get_graph)

    # 项目所有者先跑到第一个闸口（建立检查点）
    owner_run = await client.post(
        f"/api/projects/{project_id}/script/pipeline/run/stream", headers=auth["headers"])
    assert owner_run.status_code == 200, owner_run.text

    # 另一用户：访问他人项目 → 404（项目归属校验）
    other = await register_user(client, prefix="other")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    denied_status = await client.get(
        f"/api/projects/{project_id}/script/pipeline/status", headers=other_headers)
    assert denied_status.status_code == 404
    denied_run = await client.post(
        f"/api/projects/{project_id}/script/pipeline/run/stream", headers=other_headers)
    assert denied_run.status_code == 404
    denied_resume = await client.post(
        f"/api/projects/{project_id}/script/pipeline/resume",
        headers=other_headers, json={"decision": "approved"})
    assert denied_resume.status_code == 404

    # 他人 thread：检查点归属细校验（thread_id 前缀 + state.user_id 双重校验）
    # 此处直接验证守卫函数（REST 侧 thread_id 由服务端按访问者推导，跨用户
    # 场景由项目归属 404 先拦截；守卫为纵深防御）
    with pytest.raises(HTTPException) as foreign_user:
        sp_api_mod._assert_thread_owner(
            {"user_id": other["user"]["id"], "project_id": project_id},
            auth["user_id"], project_id)
    assert foreign_user.value.status_code == 404
    with pytest.raises(HTTPException) as foreign_project:
        sp_api_mod._assert_thread_owner(
            {"user_id": auth["user_id"], "project_id": project_id + 999},
            auth["user_id"], project_id)
    assert foreign_project.value.status_code == 404

    # 本人在自己项目上仍可正常回放（对照组）
    ok_state = await client.get(
        f"/api/projects/{project_id}/script/pipeline/state",
        params={"thread_id": f"script_{project_id}_{auth['user_id']}"},
        headers=auth["headers"],
    )
    assert ok_state.status_code == 200, ok_state.text
    assert ok_state.json()["pending_interrupt"]["type"] == "confirm_characters"
