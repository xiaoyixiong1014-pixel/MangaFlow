"""视觉资产智能体模块（文件级说明）。

asset_node：基于角色描述（artifacts 中已有产出或用户消息），生成标准化
视觉提示词，固定「外形 / 服装 / 气质 / 画风」四段式结构，供下游绘图模型使用。
"""

import json
import logging

from langchain_core.messages import AIMessage

from app.agents.llm_adapter import llm
from app.agents.state import AgentState, last_human_text

logger = logging.getLogger(__name__)

# 资产生成系统提示词：强制 JSON 输出四段式视觉提示词
_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的角色定妆师与视觉资产专家。请基于提供的角色描述，"
    "生成标准化的中文视觉提示词，仅输出 JSON（不要输出其他内容）：\n"
    '{"appearance":"外形段（体貌/发型/五官/身形等细节）",'
    '"outfit":"服装段（服饰风格/材质/配色/配饰）",'
    '"temperament":"气质段（性格外显/眼神/姿态气场）",'
    '"art_style":"画风段（适合 AI 漫剧的统一画风与光影基调）"}'
)


def _format_sections(data: dict) -> str:
    """将四段式 JSON 数据格式化为人类可读文本。

    Args:
        data: 含 appearance/outfit/temperament/art_style 键的字典。

    Returns:
        str: 「【外形】…【服装】…【气质】…【画风】…」四段式文本；缺失段以占位符补齐。

    Raises:
        不抛出异常：data 非字典或键缺失时使用空串兜底。
    """
    data = data if isinstance(data, dict) else {}
    return (
        f"【外形】{data.get('appearance', '（待补充）')}\n"
        f"【服装】{data.get('outfit', '（待补充）')}\n"
        f"【气质】{data.get('temperament', '（待补充）')}\n"
        f"【画风】{data.get('art_style', '（待补充）')}"
    )


async def asset_node(state: AgentState) -> dict:
    """视觉资产节点：产出角色标准化视觉提示词（外形/服装/气质/画风四段式）。

    Args:
        state: 当前图状态；角色描述取值优先级 artifacts["character"] > artifacts["script"] > 最后一条人类消息。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(四段式提示词文本)],
        "artifacts": 合并后产出物（asset_prompt 键，含四段式结构化与文本），"error": "" 或 "llm_unavailable"}。

    Raises:
        不抛出异常：LLM 调用失败时捕获并输出「（离线降级）」预置四段式模板。
    """
    artifacts = dict(state.get("artifacts") or {})
    character_desc = (
        artifacts.get("character") or artifacts.get("script") or last_human_text(state) or "一位校园少年主角"
    )
    try:
        raw = await llm.chat(
            state,
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"角色描述：{character_desc}"},
            ],
            agent_name="asset",
            temperature=0.7,
            json_mode=True,
        )
        data = json.loads(raw)
        content = _format_sections(data)
        artifacts["asset_prompt"] = {"sections": data, "text": content}
        artifacts = llm.attach_usage(artifacts)
        return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": ""}
    except Exception as exc:  # noqa: BLE001 —— LLM/解析异常必须降级
        logger.warning("asset_node LLM 调用失败，降级输出离线模板：%s", exc)
        fallback = (
            "（离线降级）当前无法连接定妆模型，以下为预置标准化视觉提示词模板：\n"
            "【外形】十七岁少年，黑色短发微乱，眉目清秀，身形清瘦挺拔。\n"
            "【服装】深蓝校服外套配白衬衫，袖口微卷，背单肩书包。\n"
            "【气质】安静内敛，眼神带着好奇与警惕，站姿笔直。\n"
            "【画风】日系新海诚风，柔和逆光，高饱和天空，电影感构图。"
        )
        artifacts["asset_prompt"] = {"text": fallback}
        return {"messages": [AIMessage(content=fallback)], "artifacts": artifacts, "error": "llm_unavailable"}
