"""画布助手智能体模块（文件级说明）。

canvas_node：输入画布节点摘要，产出与画布助手格式一致的建议 JSON
（{"reply": str, "actions": [...]}）。本节点仅供编排图调用并返回建议文本，
不直接操作画布持久化。
"""

import json
import logging

from langchain_core.messages import AIMessage

from app.agents.state import AgentState, last_human_text
from app.providers.llm import chat

logger = logging.getLogger(__name__)

# 画布助手系统提示词：强制 JSON 输出 reply + actions
_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 画布工作区的节点编排助手。请根据画布节点摘要，"
    "给出下一步编排建议（中文），仅输出 JSON（不要输出其他内容）：\n"
    '{"reply":"给用户的自然语言说明",'
    '"actions":[{"type":"动作类型（如 create_node/connect_node/update_node/remove_node）",'
    '"target":"目标节点或连线",'
    '"payload":"动作参数摘要"}]}\n'
    "actions 可为空数组；type 必须是 create_node/connect_node/update_node/remove_node 之一。"
)


async def canvas_node(state: AgentState) -> dict:
    """画布助手节点：根据画布节点摘要生成建议 actions JSON。

    Args:
        state: 当前图状态；画布摘要取值优先级 artifacts["canvas_input"] > 最后一条人类消息。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(建议 JSON 文本)],
        "artifacts": 合并后产出物（canvas_suggestion 键：{"reply","actions"}），"error": "" 或 "llm_unavailable"}。

    Raises:
        不抛出异常：LLM 调用失败时捕获并输出「（离线降级）」预置建议模板。
    """
    artifacts = dict(state.get("artifacts") or {})
    summary = artifacts.get("canvas_input") or last_human_text(state) or "画布为空，希望开始一个新漫剧项目"
    try:
        raw = await chat(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"画布节点摘要：{summary}"},
            ],
            temperature=0.5,
            json_mode=True,
        )
        data = json.loads(raw) if isinstance(raw, str) else {}
        data = data if isinstance(data, dict) else {}
        suggestion = {"reply": str(data.get("reply", "")), "actions": data.get("actions") or []}
        artifacts["canvas_suggestion"] = suggestion
        content = json.dumps(suggestion, ensure_ascii=False, indent=2)
        return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": ""}
    except Exception as exc:  # noqa: BLE001 —— LLM/解析异常必须降级
        logger.warning("canvas_node LLM 调用失败，降级输出预置建议模板：%s", exc)
        fallback = {
            "reply": "（离线降级）建议先创建「剧本 → 角色 → 分镜」三个节点并依次连线，建立基础创作流水线。",
            "actions": [
                {"type": "create_node", "target": "canvas", "payload": {"node_type": "script", "title": "剧本"}},
                {"type": "create_node", "target": "canvas", "payload": {"node_type": "asset", "title": "角色资产"}},
                {"type": "connect_node", "target": "script->asset", "payload": {"relation": "derive"}},
            ],
        }
        artifacts["canvas_suggestion"] = fallback
        content = json.dumps(fallback, ensure_ascii=False, indent=2)
        return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": "llm_unavailable"}
