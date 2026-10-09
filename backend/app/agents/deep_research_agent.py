"""长文深度解析智能体模块（文件级说明）。

research_node：对长文本进行结构化解析，产出 {"outline", "characters", "worldview"}。
超过 6000 字的文本按每块 6000 字分块，逐块 chat 摘要后汇总为最终结论；
LLM 不可用时输出固定降级模板，保证图流程不中断。
"""

import json
import logging
from typing import List

from langchain_core.messages import AIMessage

from app.agents.state import AgentState, last_human_text
from app.providers.llm import chat

logger = logging.getLogger(__name__)

# 分块阈值与块大小（字）
_CHUNK_THRESHOLD = 6000
_CHUNK_SIZE = 6000

# 逐块摘要提示词
_CHUNK_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的长文拆解分析师。请对给定文本片段做要点摘要（中文），"
    "保留人物、关系、世界观设定与关键情节，输出 200 字以内的纯文本摘要。"
)

# 汇总提示词：强制 JSON 输出三要素
_MERGE_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的长文拆解分析师。请把多段摘要整合为结构化解析结论（中文），"
    "仅输出 JSON（不要输出其他内容）：\n"
    '{"outline":"故事大纲（起承转合，200 字以内）",'
    '"characters":"主要角色清单与设定（含关系）",'
    '"worldview":"世界观设定（时代/规则/核心冲突）"}'
)

# 一次性解析提示词（短文本直接解析）
_DIRECT_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的长文拆解分析师。请解析给定文本（中文），"
    "仅输出 JSON（不要输出其他内容）：\n"
    '{"outline":"故事大纲（起承转合，200 字以内）",'
    '"characters":"主要角色清单与设定（含关系）",'
    '"worldview":"世界观设定（时代/规则/核心冲突）"}'
)


def _split_chunks(text: str) -> List[str]:
    """把长文本按固定块长切分。

    Args:
        text: 原始长文本。

    Returns:
        List[str]: 依次排列的文本块（每块最长 _CHUNK_SIZE 字）。

    Raises:
        不抛出异常：text 非字符串时返回空列表。
    """
    text = text if isinstance(text, str) else ""
    return [text[i:i + _CHUNK_SIZE] for i in range(0, len(text), _CHUNK_SIZE)]


async def research_node(state: AgentState) -> dict:
    """长文解析节点：分块摘要 + 汇总，产出大纲/角色/世界观三要素。

    Args:
        state: 当前图状态；解析对象取值优先级 artifacts["research_input"] > 最后一条人类消息。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(解析结论文本)],
        "artifacts": 合并后产出物（research 键：{"outline","characters","worldview"}），"error": "" 或 "llm_unavailable"}。

    Raises:
        不抛出异常：任何 LLM/解析异常均降级为固定占位模板。
    """
    artifacts = dict(state.get("artifacts") or {})
    text = artifacts.get("research_input") or last_human_text(state) or ""
    try:
        if len(text) > _CHUNK_THRESHOLD:
            # 长文本：分块逐块摘要，再汇总（Map-Reduce 结构）
            chunk_summaries: List[str] = []
            for idx, chunk in enumerate(_split_chunks(text), start=1):
                summary = await chat(
                    [
                        {"role": "system", "content": _CHUNK_SYSTEM_PROMPT},
                        {"role": "user", "content": f"第 {idx} 段：{chunk}"},
                    ],
                    temperature=0.3,
                )
                chunk_summaries.append(summary)
            merged_raw = await chat(
                [
                    {"role": "system", "content": _MERGE_SYSTEM_PROMPT},
                    {"role": "user", "content": "\n\n".join(chunk_summaries)},
                ],
                temperature=0.3,
                json_mode=True,
            )
        else:
            # 短文本：直接一次解析
            merged_raw = await chat(
                [
                    {"role": "system", "content": _DIRECT_SYSTEM_PROMPT},
                    {"role": "user", "content": text or "（空文本）"},
                ],
                temperature=0.3,
                json_mode=True,
            )
        data = json.loads(merged_raw) if isinstance(merged_raw, str) else {}
        data = data if isinstance(data, dict) else {}
        result = {
            "outline": str(data.get("outline", "")),
            "characters": str(data.get("characters", "")),
            "worldview": str(data.get("worldview", "")),
        }
        artifacts["research"] = result
        content = (
            "📖 长文解析完成：\n"
            f"【大纲】{result['outline'] or '（待补充）'}\n"
            f"【角色】{result['characters'] or '（待补充）'}\n"
            f"【世界观】{result['worldview'] or '（待补充）'}"
        )
        return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": ""}
    except Exception as exc:  # noqa: BLE001 —— LLM/解析异常必须降级
        logger.warning("research_node LLM 调用失败，降级输出固定模板：%s", exc)
        fallback = {
            "outline": "（离线降级）大纲占位：待恢复连接后重新解析。",
            "characters": "（离线降级）角色清单占位：待恢复连接后重新解析。",
            "worldview": "（离线降级）世界观占位：待恢复连接后重新解析。",
        }
        artifacts["research"] = fallback
        content = (
            "（离线降级）当前无法连接解析模型，以下为固定模板：\n"
            f"【大纲】{fallback['outline']}\n【角色】{fallback['characters']}\n【世界观】{fallback['worldview']}"
        )
        return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": "llm_unavailable"}
