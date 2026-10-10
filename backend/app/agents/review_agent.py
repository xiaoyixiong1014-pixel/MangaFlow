"""内容审核智能体模块（文件级说明）。

review_node：主图的内容审核入口节点。核心判定逻辑由审核子图承担
（MA-09 Subgraph，见 agents/subgraphs.build_review_subgraph）：词表机审
（一票否决）→ LLM 辅助复核 → 输出 {"result": "pass|suspect|violate",
"reason": str}；LLM 不可用时输出固定模板（词表命中仍判 violate，否则给
人工复审建议），保证审核不中断。本节点仅负责状态读取、子图调用与
artifacts/messages 产出（结构与改造前完全兼容）。
"""

import logging

from langchain_core.messages import AIMessage

from app.agents.state import AgentState, last_human_text
from app.agents.subgraphs import BANNED_WORDS, run_review  # noqa: F401 —— BANNED_WORDS 兼容再导出

logger = logging.getLogger(__name__)


async def review_node(state: AgentState) -> dict:
    """内容审核节点：经审核子图执行词表机审 + LLM 辅助判断，输出三级结论。

    Args:
        state: 当前图状态；待审文本取值优先级 artifacts["review_input"] > 最后一条人类消息。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(审核结论文本)],
        "artifacts": 合并后产出物（review 键：{"result","reason","banned"}，
        成功路径附 _usage 用量记录），"error": "" 或 "llm_unavailable"}。

    Raises:
        不抛出异常：子图内部已降级（词表命中判 violate；LLM 不可用时输出
        「（离线降级）人工复审建议」模板并判 suspect）。
    """
    artifacts = dict(state.get("artifacts") or {})
    text = artifacts.get("review_input") or last_human_text(state) or ""
    verdict = await run_review(text)
    artifacts["review"] = {
        "result": verdict["result"],
        "reason": verdict["reason"],
        "banned": verdict["banned"],
    }
    # 子图内 LLM 调用的用量经子图状态带出（contextvar 不跨任务回传）
    if verdict.get("usage"):
        artifacts["_usage"] = verdict["usage"]
    icon = {"pass": "✅", "suspect": "⚠️", "violate": "🚫"}[verdict["result"]]
    prefix = "（离线降级）当前无法连接审核模型，词表机审未命中违禁词；" if verdict["degraded"] else ""
    content = f"{icon} {prefix}审核结论：{verdict['result']} —— {verdict['reason']}"
    return {
        "messages": [AIMessage(content=content)],
        "artifacts": artifacts,
        "error": "llm_unavailable" if verdict["degraded"] else "",
    }
