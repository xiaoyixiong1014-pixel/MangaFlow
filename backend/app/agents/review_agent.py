"""内容审核智能体模块（文件级说明）。

review_node：对输入文本执行「词表机审 + LLM 辅助判断」两级审核，
输出 {"result": "pass|suspect|violate", "reason": str}；LLM 不可用时输出
固定模板（词表命中仍判 violate，否则给人工复审建议），保证审核不中断。
"""

import json
import logging

from langchain_core.messages import AIMessage

from app.agents.state import AgentState, last_human_text
from app.providers.llm import chat

logger = logging.getLogger(__name__)

# 机审违禁词表（任务规定）
BANNED_WORDS = ["枪击", "血腥", "赌博"]

# LLM 辅助审核系统提示词：强制 JSON 输出三级结论
_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的内容安全审核员。请判断以下文本是否存在违反平台内容规范的"
    "风险（暴力/色情/赌博/违法等）。仅输出 JSON（不要输出其他内容）：\n"
    '{"result":"pass|suspect|violate","reason":"判定理由（中文，简要）"}\n'
    "判定标准：pass=无明显风险；suspect=存在擦边或需要人工复核的内容；violate=明确违规。"
)


def _banned_scan(text: str):
    """词表机审：扫描违禁词。

    Args:
        text: 待审文本。

    Returns:
        list[str]: 命中的违禁词列表（未命中返回空列表）。

    Raises:
        不抛出异常：text 非字符串时按空串处理。
    """
    text = text if isinstance(text, str) else ""
    return [word for word in BANNED_WORDS if word in text]


async def review_node(state: AgentState) -> dict:
    """内容审核节点：词表机审 + LLM 辅助判断，输出三级审核结论。

    Args:
        state: 当前图状态；待审文本取值优先级 artifacts["review_input"] > 最后一条人类消息。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(审核结论文本)],
        "artifacts": 合并后产出物（review 键：{"result","reason","banned"}），"error": "" 或 "llm_unavailable"}。

    Raises:
        不抛出异常：LLM 调用失败时降级——词表命中判 violate，未命中输出固定
        「（离线降级）人工复审建议」模板并判 suspect。
    """
    artifacts = dict(state.get("artifacts") or {})
    text = artifacts.get("review_input") or last_human_text(state) or ""
    hits = _banned_scan(text)
    # 词表机审一票否决：命中即 violate（无需再问 LLM）
    if hits:
        verdict = {"result": "violate", "reason": f"机审命中违禁词：{'、'.join(hits)}"}
        artifacts["review"] = {**verdict, "banned": hits}
        content = f"🚫 审核结论：violate —— {verdict['reason']}"
        return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": ""}
    try:
        raw = await chat(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"待审文本：{text}"},
            ],
            temperature=0,
            json_mode=True,
        )
        data = json.loads(raw) if isinstance(raw, str) else {}
        result = str(data.get("result", "")).strip().lower()
        if result not in {"pass", "suspect", "violate"}:
            result = "suspect"
        verdict = {"result": result, "reason": str(data.get("reason", "模型未给出理由"))}
        artifacts["review"] = {**verdict, "banned": []}
        icon = {"pass": "✅", "suspect": "⚠️", "violate": "🚫"}[result]
        content = f"{icon} 审核结论：{result} —— {verdict['reason']}"
        return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": ""}
    except Exception as exc:  # noqa: BLE001 —— LLM 异常降级为固定模板
        logger.warning("review_node LLM 调用失败，降级输出固定模板：%s", exc)
        fallback = (
            "（离线降级）当前无法连接审核模型，词表机审未命中违禁词；"
            "建议转人工复审确认语境风险。"
        )
        verdict = {"result": "suspect", "reason": "LLM 辅助审核不可用，已转人工复审建议"}
        artifacts["review"] = {**verdict, "banned": []}
        content = f"⚠️ {fallback}"
        return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": "llm_unavailable"}
