"""P-5 剧本子图编排测试模块（文件级说明，P-1 范式级重构 Task 6）。

覆盖链路（子图以 InMemorySaver 编译 + mock 模块级 _llm_chat/_llm_chat_stream
驱动，数据库走 tests.conftest 的测试库，Agent 会话/断线恢复语义不依赖主库）：
- 子图整跑：invoke 到第一个 interrupt（confirm_characters）停住（state.next
  断言）→ Command(resume=True) 续跑到第二个 interrupt（confirm_episodes）→
  再 resume 到第三个 interrupt（confirm_body）→ 终 resume 后 END，断言
  bodies 汇聚 3 条、scripts 落库 3 行（version=1、episode.status=scripted、
  phase=body、三个里程碑快照齐全）；
- 退回重生路径：第一个 interrupt resume=False → 回退 sp_character_planner 按
  退回意见重新生成（角色 LLM 二次调用）后再次停在同闸口；二次确认后可继续
  进入大纲阶段；
- Send 并行：大纲 3 集 → sp_episode_writer 被调用 3 次（mock 计数：正文流
  3 次 + 场次结构化 3 次）、bodies 3 条；
- 断线恢复 API：invoke 中断后 GET /script/pipeline/state → pending_interrupt
  .type 正确、next_nodes 指向被中断节点；未知 thread → pending 为 null；
- Agent 接入回归：script_pipeline 意图的规则降级分类命中（LLM 故障时），
  普通剧本诉求仍归 script 意图（配合 test_agent_hitl.py 全量回归）。

LLM 桩说明：monkeypatch app.agents.script_pipeline 的模块级 _llm_chat /
_llm_chat_stream（子图节点经 plan_characters/plan_episodes/stream_episode_body/
plan_episode_scenes 间接调用，运行时查找模块全局，替换即生效）。
"""

import json
from typing import Any, Dict, List

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import select

import app.api.script_pipeline as sp_api_mod
from app.agents import script_pipeline as sp_mod
from app.agents.script_pipeline_graph import build_script_pipeline_subgraph
from app.agents.supervisor import classify_intent
from app.models import Character, Episode, Project, Script, VersionSnapshot
from tests.conftest import create_project

# 保存输入时的原始文本（idea 模式一句话创意）
_SAMPLE_RAW = "一句话创意：两个少女约定一起成为漫画家的故事。"

# 正文流单批输出的固定增量块（每集相同；单段情绪曲线不分批 → 每集 1 次流调用）
_STREAM_CHUNKS = ["天台的风吹过，", "林小满握紧了拳头。"]


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


def _episodes_payload(count: int = 3) -> str:
    """构造分集大纲 LLM 输出 JSON（内部函数：默认 3 集，单段情绪曲线）。

    Args:
        count: 分集条数（1~3；用例 1/3 用 3 集，其余缺省）。

    Returns:
        str: {"episodes": [...]} 的 JSON 文本（emotion_curve 单段不分批）。

    Raises:
        无：纯结构构造。
    """
    titles = ["第一集：天台之约", "第二集：新人奖投稿", "第三集：评审之日"]
    return json.dumps({"episodes": [
        {
            "title": titles[idx],
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
    """剧本流水线 LLM 桩（测试辅助类）：按 agent_name 路由返回并记录调用。

    Attributes:
        chat_calls: 非流式调用记录 [{"agent", "temperature"}, ...]。
        stream_calls: 流式调用记录 [{"agent"}, ...]（每次调用 1 条）。
    """

    def __init__(self) -> None:
        """初始化桩对象（清空调用记录）。

        Returns:
            None。

        Raises:
            无。
        """
        self.chat_calls: List[Dict[str, Any]] = []
        self.stream_calls: List[Dict[str, Any]] = []

    async def chat(self, messages, *, temperature, **kwargs) -> str:
        """替换 _llm_chat：按 agent_name 返回对应阶段的固定 JSON（内部函数）。

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
            return _episodes_payload(3)
        if agent == "episode_writer":
            return _scenes_payload()
        raise AssertionError(f"LLM 桩未覆盖的 agent_name：{agent}")

    async def stream(self, messages, *, agent_name="episode_writer", **kwargs):
        """替换 _llm_chat_stream：恒产出 2 块固定增量（内部函数）。

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


@pytest.fixture
def llm_stub(monkeypatch):
    """夹具：替换 script_pipeline 模块级 _llm_chat/_llm_chat_stream 为桩。

    Args:
        monkeypatch: pytest monkeypatch 夹具。

    Yields:
        _PipelineLLMStub: 桩实例（测试内断言 chat_calls/stream_calls）。

    Raises:
        无。
    """
    stub = _PipelineLLMStub()
    monkeypatch.setattr(sp_mod, "_llm_chat", stub.chat)
    monkeypatch.setattr(sp_mod, "_llm_chat_stream", stub.stream)
    return stub


def _new_subgraph() -> Any:
    """构建带 InMemorySaver 检查点的剧本子图（测试辅助）。

    Returns:
        Any: 编译子图（checkpointer=InMemorySaver，支持 interrupt/resume）。

    Raises:
        Exception: 图定义非法时由 LangGraph 抛出。
    """
    return build_script_pipeline_subgraph(checkpointer=InMemorySaver())


async def _run_to_interrupt(graph: Any, graph_input: Any, config: dict) -> Any:
    """运行子图直至出现 interrupt 更新（测试辅助，同 test_agent_hitl 模式）。

    Args:
        graph: 编译子图。
        graph_input: 图输入（首次为 dict，续跑为 Command(resume=...)）。
        config: 运行配置（含 thread_id）。

    Returns:
        Any: interrupt 值（形如 {"type":..., "payload":...}）。

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


async def _make_project(client: httpx.AsyncClient, auth: Dict[str, Any],
                        target_duration: int = 600) -> Dict[str, Any]:
    """辅助：创建项目并保存一句话创意输入（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        target_duration: 目标时长（缺省 600 秒 → 子图缺省建议 3 集）。

    Returns:
        dict: 项目响应 JSON（含 id）。

    Raises:
        AssertionError: 任一接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title="剧本子图项目",
                                   target_duration=target_duration)
    resp = await client.put(
        f"/api/projects/{project['id']}/script/input",
        headers=auth["headers"],
        json={"mode": "idea", "raw_text": _SAMPLE_RAW},
    )
    assert resp.status_code == 200, resp.text
    return project


# ---------------------------------------------------------------------------
# 1. 子图整跑：三段 interrupt 连环确认 → END + 落库断言
# ---------------------------------------------------------------------------
async def test_subgraph_full_run_three_interrupts(client, auth, db, llm_stub):
    """整跑链路：三个 interrupt 逐段 resume=True → END；bodies 3 条、scripts 3 行。

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
    project = await _make_project(client, auth)
    project_id = int(project["id"])
    graph = _new_subgraph()
    config = {"configurable": {"thread_id": "sp_full_run"}}

    # 1) 首跑：停在第 1 个 interrupt（角色确认），state.next 断言指向被中断节点
    value1 = await _run_to_interrupt(graph, {"project_id": project_id}, config)
    assert value1["type"] == "confirm_characters"
    assert value1["payload"]["count"] == 2
    names = {c["name"] for c in value1["payload"]["characters"]}
    assert names == {"林小满", "陈默"}
    snapshot = await graph.aget_state(config)
    assert tuple(snapshot.next) == ("sp_character_planner",)

    # 2) resume → 第 2 个 interrupt（大纲确认）
    value2 = await _run_to_interrupt(graph, Command(resume=True), config)
    assert value2["type"] == "confirm_episodes"
    assert value2["payload"]["count"] == 3
    assert {e["title"] for e in value2["payload"]["episodes"]} == {
        "第一集：天台之约", "第二集：新人奖投稿", "第三集：评审之日"}

    # 3) resume → 第 3 个 interrupt（正文确认）
    value3 = await _run_to_interrupt(graph, Command(resume=True), config)
    assert value3["type"] == "confirm_body"
    assert value3["payload"]["count"] == 3
    assert value3["payload"]["failed"] == []

    # 4) 终 resume → END：bodies 汇聚 3 条、无 error
    final = await graph.ainvoke(Command(resume=True), config=config)
    assert final.get("error") == ""
    bodies = final.get("bodies") or []
    assert len(bodies) == 3
    assert all(b["ok"] for b in bodies)
    assert all(b["raw_text"] == "天台的风吹过，林小满握紧了拳头。" for b in bodies)
    assert all(len(b["scenes"]) == 2 for b in bodies)
    flags = final.get("confirmed_flags") or {}
    assert flags.get("characters") is True and flags.get("episodes") is True
    assert flags.get("body") is True

    # 5) 落库断言：绑定剧集的 scripts 3 行（version=1、raw_text 落值；
    # 另有 1 条 episode_id=NULL 的剧本输入行不计入）、episodes 全 scripted
    scripts = (
        await db.execute(
            select(Script).where(
                Script.project_id == project_id,
                Script.episode_id.is_not(None),
            )
        )
    ).scalars().all()
    assert len(scripts) == 3
    for row in scripts:
        assert row.version == 1
        assert row.episode_id is not None
        assert row.raw_text == "天台的风吹过，林小满握紧了拳头。"
        # RD-03 字段级 ai 来源包装（content["scenes"]["value"] 取场次数组）
        assert row.content["title"]["source"] == "ai"
        assert len(row.content["scenes"]["value"]) == 2
    episodes = (
        await db.execute(select(Episode).where(Episode.project_id == project_id))
    ).scalars().all()
    assert len(episodes) == 3
    assert all(e.status == "scripted" for e in episodes)
    assert all(e.confirmed_at is not None for e in episodes)
    proj = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()
    assert proj.script_phase == "body"
    # 已确认角色入池 2 行
    chars = (
        await db.execute(select(Character).where(Character.project_id == project_id))
    ).scalars().all()
    assert {c.name for c in chars} == {"林小满", "陈默"}
    # 三个里程碑快照齐全
    stages = {
        s.stage for s in (
            await db.execute(
                select(VersionSnapshot).where(VersionSnapshot.project_id == project_id))
        ).scalars().all()
    }
    assert {"characters_confirmed", "outline_confirmed", "body_generated"} <= stages


# ---------------------------------------------------------------------------
# 2. 退回重生：第 1 个 interrupt resume rejected → 回退角色节点重新生成
# ---------------------------------------------------------------------------
async def test_subgraph_reject_regenerate_at_first_interrupt(client, auth, llm_stub):
    """退回链路：confirm_characters 处退回 → 回退重生（LLM 二次调用）后再次停在同闸口。

    说明：Task 6 退回语义为「回退上游生成节点按退回意见重新生成」（而非终止
    流程），退回后再次停在 characters 闸口等用户二次确认。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        llm_stub: LLM 桩夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project(client, auth)
    graph = _new_subgraph()
    config = {"configurable": {"thread_id": "sp_reject"}}

    value = await _run_to_interrupt(graph, {"project_id": int(project["id"])}, config)
    assert value["type"] == "confirm_characters"
    assert len(llm_stub.chat_calls) == 1

    # 退回（不带意见）：仍触发重生 → 再次停在 characters 闸口，角色 LLM 二次调用
    second = await _run_to_interrupt(graph, Command(resume=False), config)
    assert second["type"] == "confirm_characters"
    agents = [c["agent"] for c in llm_stub.chat_calls]
    assert agents == ["character_planner", "character_planner"]
    assert llm_stub.stream_calls == []
    # 退回意见（此处为空）与回退标记留存，未进入后续阶段
    assert (await graph.aget_state(config)).values.get("retry_stage") == "characters"

    # 二次确认通过 → 继续进入大纲阶段（端到端仍可走完）
    third = await _run_to_interrupt(graph, Command(resume=True), config)
    assert third["type"] == "confirm_episodes"
    assert [c["agent"] for c in llm_stub.chat_calls].count("episode_planner") == 1


# ---------------------------------------------------------------------------
# 3. Send 并行：3 集大纲 → sp_episode_writer 调用 3 次、bodies 3 条
# ---------------------------------------------------------------------------
async def test_send_parallel_three_episodes(client, auth, llm_stub):
    """Send 并行分发：大纲 3 集 → 正文流调用 3 次 + 场次结构化 3 次、bodies 3 条。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        llm_stub: LLM 桩夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project(client, auth)
    graph = _new_subgraph()
    config = {"configurable": {"thread_id": "sp_parallel"}}

    await _run_to_interrupt(graph, {"project_id": int(project["id"])}, config)
    await _run_to_interrupt(graph, Command(resume=True), config)
    await _run_to_interrupt(graph, Command(resume=True), config)
    final = await graph.ainvoke(Command(resume=True), config=config)

    bodies = final.get("bodies") or []
    assert len(bodies) == 3
    # 并行叶子各写一条（episode_id 互不重复）
    assert len({b["episode_id"] for b in bodies}) == 3
    # mock 计数：正文流 3 次（每集 1 批）、场次结构化 3 次
    assert len(llm_stub.stream_calls) == 3
    writer_chat_calls = [c for c in llm_stub.chat_calls if c["agent"] == "episode_writer"]
    assert len(writer_chat_calls) == 3


# ---------------------------------------------------------------------------
# 4. 断线恢复 API：GET /script/pipeline/state
# ---------------------------------------------------------------------------
async def test_pipeline_state_api(client, auth, llm_stub, monkeypatch):
    """断线恢复 API：中断后 GET state 回放 pending_interrupt；未知 thread 为 null。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        llm_stub: LLM 桩夹具（前两段子图运行必须 mock，否则触发真实 LLM 调用）。
        monkeypatch: 把端点使用的子图单例替换为 InMemorySaver 编译图。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project(client, auth)
    project_id = int(project["id"])
    graph = _new_subgraph()

    async def _fake_get_graph():
        """替换 get_compiled_script_pipeline：恒返回测试用 InMemory 子图（内部函数）。

        Returns:
            Any: 编译子图。

        Raises:
            无。
        """
        return graph

    monkeypatch.setattr(sp_api_mod, "get_compiled_script_pipeline", _fake_get_graph)

    # 运行到第 2 个 interrupt（大纲确认）后断线
    config = {"configurable": {"thread_id": "sp_state_t1"}}
    value1 = await _run_to_interrupt(graph, {"project_id": project_id}, config)
    assert value1["type"] == "confirm_characters"
    value2 = await _run_to_interrupt(graph, Command(resume=True), config)
    assert value2["type"] == "confirm_episodes"

    # 回放：pending_interrupt 指向 confirm_episodes，next_nodes 指向被中断节点
    resp = await client.get(
        f"/api/projects/{project_id}/script/pipeline/state?thread_id=sp_state_t1",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["thread_id"] == "sp_state_t1"
    assert body["project_id"] == project_id
    assert body["pending_interrupt"]["type"] == "confirm_episodes"
    assert body["pending_interrupt"]["payload"]["count"] == 3
    assert body["next_nodes"] == ["sp_episode_planner"]
    # 进度摘要：候选 2 角色、大纲 3 集、本轮已生成 0 集
    assert body["summary"]["character_count"] == 2
    assert body["summary"]["episode_count"] == 3
    assert body["summary"]["body_count"] == 0

    # 无检查点线程：pending 为 null、next_nodes 为空
    missing = await client.get(
        f"/api/projects/{project_id}/script/pipeline/state?thread_id=sp_state_missing",
        headers=auth["headers"],
    )
    assert missing.status_code == 200, missing.text
    missing_body = missing.json()
    assert missing_body["pending_interrupt"] is None
    assert missing_body["next_nodes"] == []


# ---------------------------------------------------------------------------
# 5. Agent 接入回归：script_pipeline 意图识别
# ---------------------------------------------------------------------------
async def test_supervisor_script_pipeline_intent(monkeypatch):
    """意图回归：LLM 故障降级规则时「整本剧本自动写完」命中 script_pipeline，
    普通剧本诉求仍归 script（不误伤既有 script 意图）。

    Args:
        monkeypatch: 把 AgentLLM 底层 chat_with_meta 替换为恒抛异常。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """

    async def _boom(*args, **kwargs):
        """替换 chat_with_meta：恒抛异常强制走规则降级（内部函数）。

        Args:
            *args: 位置参数（忽略）。
            **kwargs: 关键字参数（忽略）。

        Raises:
            RuntimeError: 模拟 LLM 渠道完全不可用。
        """
        raise RuntimeError("llm down")

    monkeypatch.setattr("app.agents.llm_adapter.chat_with_meta", _boom)
    pipeline = await classify_intent("帮我把这个项目整本剧本自动写完")
    assert pipeline["intent"] == "script_pipeline"
    normal = await classify_intent("帮我写一个校园剧本")
    assert normal["intent"] == "script"
