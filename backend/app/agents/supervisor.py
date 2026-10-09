"""监督者（意图路由）智能体模块（文件级说明）。

职责：
- classify_intent(): 判断用户消息的创作意图（优先 LLM，异常时关键词规则降级）
- supervisor_node(): 图的入口节点，识别意图并写入 state.intent，向用户播报分派消息
"""

import json
import logging
from typing import Dict

from langchain_core.messages import AIMessage

from app.agents.state import AgentState, last_human_text
from app.providers.llm import chat

logger = logging.getLogger(__name__)

# 基础意图关键词表（任务规定的降级规则，按序匹配：越具体越靠前可减少误伤，
# 此处保持任务给定顺序 script→asset→storyboard→generation→canvas→billing）
_BASE_KEYWORDS = [
    ("script", ["剧本", "写", "大纲", "台词", "脚本", "故事"]),
    ("asset", ["角色", "定妆", "资产", "人设", "形象", "立绘"]),
    ("storyboard", ["分镜", "镜头", "景别", "运镜"]),
    ("generation", ["生成", "视频", "配音", "出图", "渲染"]),
    ("canvas", ["画布", "节点", "连线"]),
    ("billing", ["积分", "充值", "计费", "余额", "扣费"]),
]

# 扩展意图关键词：在任务规定的 7 类之外补充 compose/consistency/review/research，
# 用于保证编排图中所有域节点均可被路由可达（规则模式直接命中；LLM 模式由
# supervisor 节点内的二次细化 _refine_intent() 补偿）
_EXT_KEYWORDS = [
    ("compose", ["合成", "转场", "剪辑", "字幕"]),
    ("consistency", ["一致性", "形象统一", "角色一致"]),
    ("review", ["审核", "违禁", "敏感词", "合规检查"]),
    ("research", ["调研", "长文解析", "拆书", "提取世界观"]),
]

# LLM 可返回的合法意图枚举（任务规定的 7 类）
_ALLOWED_INTENTS = {"script", "asset", "storyboard", "generation", "canvas", "billing", "chat"}

# 意图分类系统提示词：强制 JSON 输出，枚举限定 7 类
_INTENT_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 多智能体系统的意图路由器。请判断用户消息最符合哪一类创作意图，"
    "仅输出 JSON（不要输出其他内容）：\n"
    '{"intent":"script|asset|storyboard|generation|canvas|billing|chat","confidence":0-1}\n'
    "分类说明：script=写剧本/大纲/故事；asset=角色定妆/视觉资产；storyboard=分镜/镜头设计；"
    "generation=生成图像/视频/配音；canvas=画布节点操作；billing=积分/充值/费用；chat=其余闲聊或咨询。"
)


def _rule_classify(message: str) -> Dict:
    """关键词规则分类（LLM 不可用时的降级路径）。

    Args:
        message: 用户消息文本。

    Returns:
        Dict: {"intent": str, "confidence": float, "mode": "rule"}。
        匹配顺序：扩展意图（更具体）→ 任务规定的基础 6 类 → 默认 chat。
    """
    for intent, words in _EXT_KEYWORDS:
        if any(word in message for word in words):
            return {"intent": intent, "confidence": 0.6, "mode": "rule"}
    for intent, words in _BASE_KEYWORDS:
        if any(word in message for word in words):
            return {"intent": intent, "confidence": 0.6, "mode": "rule"}
    return {"intent": "chat", "confidence": 0.5, "mode": "rule"}


def _refine_intent(message: str, intent: str) -> str:
    """在 LLM 返回的 7 类意图基础上，用扩展强特征词做二次细化。

    说明：LLM 分类枚举限定为任务规定的 7 类，无法直接产出 compose/consistency/
    review/research；为使图中全部域节点在 LLM 模式下同样可达，这里对命中扩展
    关键词的消息做意图覆盖（仅当扩展词出现时才覆盖，其余情况保持 LLM 结果）。

    Args:
        message: 用户消息文本。
        intent: classify_intent 产出的意图。

    Returns:
        str: 细化后的最终意图。
    """
    for ext_intent, words in _EXT_KEYWORDS:
        if any(word in message for word in words):
            return ext_intent
    return intent


async def classify_intent(message: str) -> Dict:
    """识别用户消息的创作意图（优先 LLM，异常时关键词规则降级，绝不抛异常）。

    Args:
        message: 用户消息文本。

    Returns:
        Dict: {"intent": str（意图枚举值）, "confidence": float（0-1）,
        "mode": str（"llm"=大模型判定 / "rule"=关键词规则降级）}。

    Raises:
        不抛出异常：LLM 调用失败或 JSON 解析失败时自动降级为规则分类。
    """
    # 1) 优先走 LLM 分类（json_mode 强制 JSON 输出）
    try:
        raw = await chat(
            [
                {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
            temperature=0,
            json_mode=True,
        )
        data = json.loads(raw)
        intent = str(data.get("intent", "")).strip().lower()
        if intent in _ALLOWED_INTENTS:
            try:
                confidence = float(data.get("confidence", 0.8))
            except (TypeError, ValueError):
                confidence = 0.8
            confidence = min(max(confidence, 0.0), 1.0)
            # chat 判定交叉纠偏：LLM 分类存在波动（对同一句强特征消息可能返回
            # chat/0.3 或 script/0.97）。含「剧本/分镜/配音」等显式创作强特征词
            # 的消息判为 chat 明显违背用户意图，因此当 LLM 判为 chat 而关键词
            # 规则命中具体意图时，采用规则结果（兜底优先级：LLM 判出具体意图
            # > LLM 判 chat 时规则纠偏 > LLM 与规则均 chat）。
            if intent == "chat":
                rule = _rule_classify(message)
                if rule["intent"] != "chat":
                    return rule
            return {"intent": intent, "confidence": confidence, "mode": "llm"}
        # 意图不在合法枚举内 → 视为无效输出，走规则降级
        logger.warning("意图分类 LLM 返回非法 intent=%r，降级为规则分类", intent)
    except Exception as exc:  # noqa: BLE001 —— 任何 LLM/解析异常都必须降级
        logger.warning("意图识别 LLM 调用失败，降级为关键词规则：%s", exc)
    # 2) 关键词规则降级
    return _rule_classify(message)


async def supervisor_node(state: AgentState) -> dict:
    """监督者节点：读取最后一条人类消息 → 识别意图 → 写入 state.intent 并播报分派消息。

    Args:
        state: 当前图状态（AgentState）。

    Returns:
        dict: 部分状态更新 {"intent": str, "messages": [AIMessage]}；
        AIMessage 文本为「已识别意图：xxx，正在分派…」。

    Raises:
        不抛出异常：classify_intent 内部已完整兜底，保证图永不因意图识别崩溃。
    """
    message = last_human_text(state)
    result = await classify_intent(message)
    final_intent = _refine_intent(message, result["intent"])
    return {
        "intent": final_intent,
        "messages": [AIMessage(content=f"已识别意图：{final_intent}，正在分派…")],
    }
