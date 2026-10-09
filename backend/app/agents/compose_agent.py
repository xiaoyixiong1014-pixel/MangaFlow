"""合成方案智能体模块（文件级说明）。

compose_node：基于分镜与关键帧产出，输出漫剧合成方案——
片段顺序 / 转场 / 字幕建议，供下游合成引擎或人工剪辑参考。
"""

import json
import logging

from langchain_core.messages import AIMessage

from app.agents.state import AgentState, last_human_text
from app.providers.llm import chat

logger = logging.getLogger(__name__)

# 合成方案系统提示词：强制 JSON 输出三部分建议
_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的后期合成导演，负责为 AI 漫剧输出合成方案。"
    "请使用中文，仅输出 JSON（不要输出其他内容）：\n"
    '{"sequence":"片段顺序说明（按镜头号给出排列与节奏）",'
    '"transitions":"转场建议（硬切/叠化/甩镜等及使用位置）",'
    '"subtitles":"字幕建议（台词字幕样式/出现时机/关键信息强调）"}'
)


async def compose_node(state: AgentState) -> dict:
    """合成方案节点：输出片段顺序 / 转场 / 字幕建议。

    Args:
        state: 当前图状态；合成依据取值优先级 artifacts["storyboard"] > 最后一条人类消息。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(合成方案文本)],
        "artifacts": 合并后产出物（compose 键，含结构化建议与文本），"error": "" 或 "llm_unavailable"}。

    Raises:
        不抛出异常：LLM 调用失败时捕获并输出「（离线降级）」预置合成方案模板。
    """
    artifacts = dict(state.get("artifacts") or {})
    sb = artifacts.get("storyboard") or {}
    basis = (sb.get("text") if isinstance(sb, dict) else None) or last_human_text(state) or "按既有分镜表合成"
    try:
        raw = await chat(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"分镜/素材内容：{basis}"},
            ],
            temperature=0.6,
            json_mode=True,
        )
        data = json.loads(raw) if isinstance(raw, str) else {}
        data = data if isinstance(data, dict) else {}
        content = (
            f"🎬 合成方案：\n【片段顺序】{data.get('sequence', '待定')}\n"
            f"【转场建议】{data.get('transitions', '待定')}\n"
            f"【字幕建议】{data.get('subtitles', '待定')}"
        )
        artifacts["compose"] = {"plan": data, "text": content}
        return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": ""}
    except Exception as exc:  # noqa: BLE001 —— LLM/解析异常必须降级
        logger.warning("compose_node LLM 调用失败，降级输出离线模板：%s", exc)
        fallback = (
            "（离线降级）当前无法连接合成模型，以下为预置合成方案模板：\n"
            "【片段顺序】按镜头 1→2→3 顺序排列，节奏先慢后快，结尾定格 1 秒。\n"
            "【转场建议】镜头 1→2 用叠化承接情绪，镜头 2→3 用硬切制造反转冲击。\n"
            "【字幕建议】台词字幕居中下方白色描边，关键悬念词用金色加粗，转场期间字幕淡出。"
        )
        artifacts["compose"] = {"text": fallback}
        return {"messages": [AIMessage(content=fallback)], "artifacts": artifacts, "error": "llm_unavailable"}
