"""智能体 HITL / Send 并行链路测试模块（文件级说明）。

覆盖链路（MA-08 / MA-10，直接以 MemorySaver 编译图 + mock LLM 渠道驱动，
不依赖数据库与真实模型服务）：
- script 意图 → script_confirm 节点 interrupt（confirm_entities）→ resume
  approved=True 后图正常完成；
- script 意图 → interrupt → resume approved=False → artifacts 标记 rejected；
- generation 批量意图 → generation_node interrupt（confirm_generation）→
  resume approved=True 后 Send 并行分发 generation_item → generation_reduce
  汇总 → cost 播报，artifacts 含逐项 keyframe_prompt.items；
- generation 批量拒绝 → resume approved=False → artifacts["aborted"]=True
  且不产生生成结果（退款标记路径）。
"""

import json
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.graph import build_graph

# mock 渠道元信息（chat_with_meta 返回值第二元素）
_MOCK_META = {"channel": "mock", "model": "mock-model"}

# script_node 产出的固定剧本全文（含确认摘要所需的结构标签）
_MOCK_SCRIPT = (
    "【标题】测试剧本\n"
    "【Logline】一个用于测试的确认链路故事。\n"
    "【三幕大纲】第一幕：起；第二幕：承；第三幕：转。\n"
    "【主角设定】测试主角 / 18 岁 / 勇敢 / 寻找真相 / 成长。"
)

# generation_item 产出的固定五要素 JSON
_MOCK_KEYFRAME = json.dumps({
    "style": "测试画风", "subject": "测试主体", "action": "测试动作",
    "environment": "测试环境", "composition": "测试构图",
}, ensure_ascii=False)


class _LLMStub:
    """LLM 渠道桩（测试辅助类）：按系统提示词路由返回固定内容并记录调用。

    Attributes:
        calls: 累积收到的 messages 列表（供断言调用次数与内容）。
    """

    def __init__(self) -> None:
        """初始化桩对象。

        Returns:
            None。

        Raises:
            无。
        """
        self.calls = []

    async def __call__(self, messages, model=None, temperature=0.7, json_mode=False):
        """模拟 chat_with_meta：按系统提示词特征路由返回固定响应。

        Args:
            messages: OpenAI 格式消息列表。
            model: 模型名（忽略）。
            temperature: 采样温度（忽略）。
            json_mode: JSON 模式（忽略，桩返回内容本身即为合法 JSON/文本）。

        Returns:
            Tuple[str, Dict]: (响应文本, mock 渠道元信息)。

        Raises:
            AssertionError: 遇到未预期的系统提示词时中断（便于发现桩路由遗漏）。
        """
        self.calls.append(messages)
        system = messages[0]["content"] if messages else ""
        if "意图路由器" in system:
            return json.dumps({"intent": self.intent, "confidence": 0.99}), _MOCK_META
        if "剧本创作专家" in system:
            return _MOCK_SCRIPT, _MOCK_META
        if "关键帧美术指导" in system:
            return _MOCK_KEYFRAME, _MOCK_META
        raise AssertionError(f"LLM 桩未覆盖的系统提示词：{system[:40]}")


@pytest.fixture
def llm_stub(monkeypatch):
    """夹具：把 AgentLLM 适配器的底层 chat_with_meta 替换为 _LLMStub。

    Args:
        monkeypatch: pytest monkeypatch 夹具。

    Yields:
        _LLMStub: 桩实例（测试内可断言 calls / 设置 intent）。

    Raises:
        无。
    """
    stub = _LLMStub()
    stub.intent = "chat"
    monkeypatch.setattr("app.agents.llm_adapter.chat_with_meta", stub)
    yield stub


def _new_graph() -> Any:
    """构建带 MemorySaver 检查点的编译图（测试辅助）。

    Returns:
        Any: 编译图（checkpointer=InMemorySaver，支持 interrupt/resume）。

    Raises:
        Exception: 图定义非法时由 LangGraph 抛出。
    """
    return build_graph(checkpointer=InMemorySaver())


async def _run_to_interrupt(graph: Any, config: dict, graph_input: Any) -> Any:
    """运行图直至出现 interrupt 更新（测试辅助）。

    Args:
        graph: 编译图。
        config: 运行配置（含 thread_id）。
        graph_input: 图输入。

    Returns:
        Any: interrupt 值（interrupt() 的 payload，形如 {"type":..., "payload":...}）。

    Raises:
        AssertionError: 未在本轮运行中出现 interrupt 时中断。
    """
    interrupt_value = None
    async for chunk in graph.astream(graph_input, config=config, stream_mode="updates"):
        if isinstance(chunk, dict) and "__interrupt__" in chunk:
            interrupts = chunk["__interrupt__"] or ()
            assert interrupts, "interrupt 更新为空"
            interrupt_value = interrupts[0].value
            break
    assert interrupt_value is not None, "图运行未触发 interrupt"
    return interrupt_value


async def test_script_interrupt_hou_resume_tongguo(llm_stub):
    """剧本确认链路：interrupt 产出 confirm_entities → resume 放行 → 图正常完成。

    Args:
        llm_stub: LLM 桩夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    llm_stub.intent = "script"
    graph = _new_graph()
    config = {"configurable": {"thread_id": "hitl_script_ok"}}

    # 1) 首次运行：应在 script_confirm 节点 interrupt（confirm_entities）
    value = await _run_to_interrupt(
        graph, config,
        {"messages": [HumanMessage(content="帮我写一个剧本")], "user_id": 1, "project_id": None},
    )
    assert value["type"] == "confirm_entities"
    summary = value["payload"]["summary"]
    assert summary["title"] == "测试剧本"
    assert "测试剧本" in summary["preview"]

    # 2) resume 放行：图完成，产出确认播报，artifacts.script 保留草稿
    final_messages = []
    async for chunk in graph.astream(Command(resume={"approved": True}), config=config,
                                     stream_mode="updates"):
        for _name, update in (chunk or {}).items():
            if isinstance(update, dict):
                final_messages.extend(update.get("messages") or [])
    snapshot = await graph.aget_state(config)
    artifacts = snapshot.values.get("artifacts") or {}
    assert "测试剧本" in str(artifacts.get("script") or "")
    assert "rejected" not in artifacts
    assert any("已确认" in str(getattr(m, "content", "")) for m in final_messages)


async def test_script_interrupt_hou_resume_jujue(llm_stub):
    """剧本拒绝链路：resume approved=False → artifacts 标记 rejected。

    Args:
        llm_stub: LLM 桩夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    llm_stub.intent = "script"
    graph = _new_graph()
    config = {"configurable": {"thread_id": "hitl_script_reject"}}

    value = await _run_to_interrupt(
        graph, config,
        {"messages": [HumanMessage(content="帮我写一个剧本")], "user_id": 1, "project_id": None},
    )
    assert value["type"] == "confirm_entities"

    cancel_texts = []
    async for chunk in graph.astream(Command(resume={"approved": False}), config=config,
                                     stream_mode="updates"):
        if isinstance(chunk, dict):
            for _name, update in chunk.items():
                if isinstance(update, dict):
                    for msg in (update.get("messages") or []):
                        cancel_texts.append(str(getattr(msg, "content", "")))
    snapshot = await graph.aget_state(config)
    artifacts = snapshot.values.get("artifacts") or {}
    assert artifacts.get("rejected") is True
    assert artifacts.get("rejected_type") == "confirm_entities"
    # script_confirm 节点产出了取消播报
    assert any("已取消" in text for text in cancel_texts)


async def test_generation_piliang_interrupt_resume_bingxing(llm_stub):
    """批量生成链路：confirm_generation interrupt → 放行 → Send 并行 + reduce 汇总。

    Args:
        llm_stub: LLM 桩夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    llm_stub.intent = "generation"
    graph = _new_graph()
    config = {"configurable": {"thread_id": "hitl_gen_ok"}}
    graph_input = {
        "messages": [HumanMessage(content="把所有镜头批量生成关键帧")],
        "user_id": 1,
        "project_id": None,
        "artifacts": {"batch_items": ["镜头一描述", "镜头二描述", "镜头三描述"]},
    }

    # 1) 批量确认点：payload 应含张数与积分估算
    value = await _run_to_interrupt(graph, config, graph_input)
    assert value["type"] == "confirm_generation"
    assert value["payload"]["count"] == 3
    assert value["payload"]["cost_points"] > 0

    # 2) resume 放行 → generation_item 并行 ×3 → generation_reduce → cost → END
    nodes_run = set()
    async for chunk in graph.astream(Command(resume={"approved": True}), config=config,
                                     stream_mode="updates"):
        if isinstance(chunk, dict):
            nodes_run.update(chunk.keys())
    assert "generation_item" in nodes_run, "批量确认后应经 Send 并行分发到 generation_item"
    assert "generation_reduce" in nodes_run
    assert "cost" in nodes_run
    snapshot = await graph.aget_state(config)
    values = snapshot.values or {}
    artifacts = values.get("artifacts") or {}
    keyframe = artifacts.get("keyframe_prompt") or {}
    assert len(keyframe.get("items") or []) == 3
    assert all(item["ok"] for item in keyframe["items"])
    assert artifacts.get("cost_estimate", {}).get("params", {}).get("count") == 3
    # 汇总后并行结果通道已重置（None 哨兵语义）
    assert not (values.get("generation_results") or [])


async def test_generation_piliang_jujue_zhongzhi(llm_stub):
    """批量生成拒绝链路：resume approved=False → aborted 标记且无生成结果。

    Args:
        llm_stub: LLM 桩夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    llm_stub.intent = "generation"
    graph = _new_graph()
    config = {"configurable": {"thread_id": "hitl_gen_reject"}}
    graph_input = {
        "messages": [HumanMessage(content="全部镜头批量生成")],
        "user_id": 1,
        "project_id": None,
        "artifacts": {"batch_items": ["镜头一描述", "镜头二描述"]},
    }

    value = await _run_to_interrupt(graph, config, graph_input)
    assert value["type"] == "confirm_generation"

    nodes_run = set()
    async for chunk in graph.astream(Command(resume={"approved": False}), config=config,
                                     stream_mode="updates"):
        if isinstance(chunk, dict):
            nodes_run.update(chunk.keys())
    # 拒绝后直达 END：不进入并行子任务与汇总，也不进 cost
    assert "generation_item" not in nodes_run
    assert "generation_reduce" not in nodes_run
    assert "cost" not in nodes_run
    snapshot = await graph.aget_state(config)
    artifacts = (snapshot.values or {}).get("artifacts") or {}
    assert artifacts.get("aborted") is True
    assert artifacts.get("refund_pending") is True
    assert "keyframe_prompt" not in artifacts
