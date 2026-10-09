"""剧本创作智能体模块（文件级说明）。

script_node：根据创作简报（artifacts.brief）或用户消息，生成漫剧剧本创意，
输出结构固定为「标题 + Logline + 三幕大纲 + 主角设定」的中文创作内容。
"""

import logging

from langchain_core.messages import AIMessage

from app.agents.state import AgentState, last_human_text
from app.providers.llm import chat

logger = logging.getLogger(__name__)

# 剧本创作系统提示词（角色化 + 输出结构约束）
_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的剧本创作专家，擅长为 AI 漫剧（动态漫/竖屏短剧）撰写剧本。"
    "请全程使用中文，按以下固定结构输出：\n"
    "【标题】一个吸引人、符合短剧调性的片名\n"
    "【Logline】一句话故事梗概（30 字以内，突出核心冲突）\n"
    "【三幕大纲】\n第一幕（建置）：……\n第二幕（对抗）：……\n第三幕（结局）：……\n"
    "【主角设定】姓名 / 年龄 / 性格 / 核心动机 / 成长弧光\n"
    "要求：冲突鲜明、反转有力，节奏适配 3-5 分钟漫剧体量。"
)


async def script_node(state: AgentState) -> dict:
    """剧本创作节点：生成标题 + Logline + 三幕大纲 + 主角设定。

    Args:
        state: 当前图状态；输入优先级为 artifacts["brief"] > 最后一条人类消息 > 默认需求。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(剧本内容)],
        "artifacts": 合并后产出物（script 键），"error": ""（成功）或 "llm_unavailable"（降级）}。

    Raises:
        不抛出异常：LLM 调用失败时捕获并输出「（离线降级）」预置剧本框架模板。
    """
    brief = (state.get("artifacts") or {}).get("brief")
    user_text = brief or last_human_text(state) or "请创作一个原创校园题材短剧剧本"
    artifacts = dict(state.get("artifacts") or {})
    try:
        content = await chat(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"创作需求：{user_text}"},
            ],
            temperature=0.8,
        )
        artifacts["script"] = content
        return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": ""}
    except Exception as exc:  # noqa: BLE001 —— LLM 异常必须降级，保证图不崩溃
        logger.warning("script_node LLM 调用失败，降级输出离线模板：%s", exc)
        fallback = (
            "（离线降级）当前无法连接创作模型，以下为预置剧本框架模板：\n"
            "【标题】《未命名的叙事》\n"
            "【Logline】主人公在校园里发现了一个改变命运的秘密。\n"
            "【三幕大纲】第一幕（建置）：平静日常中出现异常线索，悬念埋下；"
            "第二幕（对抗）：主人公追查真相，与阻力正面冲突，误会加深；"
            "第三幕（结局）：真相揭晓，完成反转，人物完成成长。\n"
            "【主角设定】姓名待定 / 17 岁 / 敏感内向 / 寻找真相 / 从逃避到直面。"
        )
        artifacts["script"] = fallback
        return {"messages": [AIMessage(content=fallback)], "artifacts": artifacts, "error": "llm_unavailable"}
