"""资产一致性分析智能体模块（文件级说明）。

consistency_node：主图的资产一致性分析入口节点。核心比对逻辑由一致性
子图承担（MA-09 Subgraph，见 agents/subgraphs.build_consistency_subgraph）：
特征采集 → 相似度评估 → 输出相同点 / 差异 / 建议分项结论；LLM 不可用时
输出固定分析模板，保证流程可继续。本节点仅负责状态读取（两个资产描述
提取）、子图调用与 artifacts/messages 产出（结构与改造前完全兼容）。
"""

import logging

from langchain_core.messages import AIMessage

from app.agents.state import AgentState, last_human_text
from app.agents.subgraphs import run_consistency

logger = logging.getLogger(__name__)


def _extract_two_assets(state: AgentState) -> str:
    """从状态中提取两个待比对的资产描述文本（输入采集）。

    Args:
        state: 当前图状态；优先读 artifacts["assets"]（list 两元素或 dict 两键），
        否则使用最后一条人类消息。

    Returns:
        str: 拼接好的「资产 A…；资产 B…」比对输入文本。

    Raises:
        不抛出异常：结构异常时回退最后一条人类消息。
    """
    try:
        assets = (state.get("artifacts") or {}).get("assets")
        if isinstance(assets, (list, tuple)) and len(assets) >= 2:
            return f"资产 A：{assets[0]}\n资产 B：{assets[1]}"
        if isinstance(assets, dict) and len(assets) >= 2:
            items = list(assets.items())[:2]
            return f"资产 A（{items[0][0]}）：{items[0][1]}\n资产 B（{items[1][0]}）：{items[1][1]}"
    except Exception:  # noqa: BLE001 —— 提取失败走默认输入
        pass
    return last_human_text(state) or "资产 A：黑发校服少年；资产 B：第二集中出现的同主角形象"


async def consistency_node(state: AgentState) -> dict:
    """一致性分析节点：经一致性子图比对两个资产描述，输出相同点/差异/建议。

    Args:
        state: 当前图状态；比对输入见 _extract_two_assets()。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(一致性分析文本)],
        "artifacts": 合并后产出物（consistency 键，含结构化结论与文本，成功
        路径附 _usage 用量记录），"error": "" 或 "llm_unavailable"}。

    Raises:
        不抛出异常：子图内部已降级（LLM 不可用时固定分析模板）。
    """
    artifacts = dict(state.get("artifacts") or {})
    basis = _extract_two_assets(state)
    outcome = await run_consistency(basis)
    data = outcome["analysis"]
    if outcome.get("usage"):
        artifacts["_usage"] = outcome["usage"]
    content = (
        f"🔍 一致性分析：\n【相同点】{data.get('similarities', '待定')}\n"
        f"【差异】{data.get('differences', '待定')}\n"
        f"【建议】{data.get('suggestions', '待定')}"
    )
    artifacts["consistency"] = {"analysis": data, "text": content}
    return {
        "messages": [AIMessage(content=content)],
        "artifacts": artifacts,
        "error": "llm_unavailable" if outcome["degraded"] else "",
    }
