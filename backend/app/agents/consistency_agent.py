"""资产一致性分析智能体模块（文件级说明）。

consistency_node：输入两个资产（角色/场景等）描述，输出一致性分析——
相同点 / 差异 / 建议；LLM 不可用时输出固定分析模板，保证流程可继续。
"""

import json
import logging

from langchain_core.messages import AIMessage

from app.agents.state import AgentState, last_human_text
from app.providers.llm import chat

logger = logging.getLogger(__name__)

# 一致性分析系统提示词：强制 JSON 输出三部分结论
_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的资产一致性审查员，负责判断两个视觉资产（角色/场景）"
    "在多镜头中能否保持形象统一。请使用中文，仅输出 JSON（不要输出其他内容）：\n"
    '{"similarities":"两者相同的特征（外形/服装/色调等）",'
    '"differences":"两者存在冲突或漂移的差异点",'
    '"suggestions":"为保持一致性给出的可执行建议"}'
)


def _extract_two_assets(state: AgentState) -> str:
    """从状态中提取两个待比对的资产描述文本。

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
    """一致性分析节点：比对两个资产描述，输出相同点/差异/建议。

    Args:
        state: 当前图状态；比对输入见 _extract_two_assets()。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(一致性分析文本)],
        "artifacts": 合并后产出物（consistency 键，含结构化结论与文本），"error": "" 或 "llm_unavailable"}。

    Raises:
        不抛出异常：LLM 调用失败时捕获并输出「（离线降级）」固定分析模板。
    """
    artifacts = dict(state.get("artifacts") or {})
    basis = _extract_two_assets(state)
    try:
        raw = await chat(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": basis},
            ],
            temperature=0.3,
            json_mode=True,
        )
        data = json.loads(raw) if isinstance(raw, str) else {}
        data = data if isinstance(data, dict) else {}
        content = (
            f"🔍 一致性分析：\n【相同点】{data.get('similarities', '待定')}\n"
            f"【差异】{data.get('differences', '待定')}\n"
            f"【建议】{data.get('suggestions', '待定')}"
        )
        artifacts["consistency"] = {"analysis": data, "text": content}
        return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": ""}
    except Exception as exc:  # noqa: BLE001 —— LLM/解析异常必须降级为固定模板
        logger.warning("consistency_node LLM 调用失败，降级输出固定模板：%s", exc)
        fallback = (
            "（离线降级）当前无法连接一致性模型，以下为固定分析模板：\n"
            "【相同点】主体身份、核心服饰与主色调保持一致。\n"
            "【差异】发型细节、光照方向与背景元素可能存在镜头间漂移。\n"
            "【建议】锁定同一段角色视觉提示词与 seed，统一画风关键词，逐镜头比对参考帧。"
        )
        artifacts["consistency"] = {"text": fallback}
        return {"messages": [AIMessage(content=fallback)], "artifacts": artifacts, "error": "llm_unavailable"}
