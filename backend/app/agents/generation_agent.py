"""关键帧生成智能体模块（文件级说明）。

generation_node：基于分镜/角色产出，生成符合「风格 + 主体 + 动作 + 环境 + 构图」
五要素的关键帧提示词；随后调用成本估算纯函数 estimate_cost（来自 cost_agent），
把估算结果写入 artifacts["cost_estimate"]，供图中后继的 cost 节点输出成本说明，
体现「生成 → 成本校验」链路。
"""

import json
import logging

from langchain_core.messages import AIMessage

from app.agents.cost_agent import estimate_cost
from app.agents.state import AgentState, last_human_text
from app.providers.llm import chat

logger = logging.getLogger(__name__)

# 关键帧提示词系统提示词：强制 JSON 输出五要素
_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的关键帧美术指导，负责产出可直接用于图像模型的关键帧提示词。"
    "请使用中文，按「风格 + 主体 + 动作 + 环境 + 构图」五要素生成，仅输出 JSON（不要输出其他内容）：\n"
    '{"style":"画风与光影基调","subject":"画面主体（人物/物件及其特征）",'
    '"action":"主体动作/姿态","environment":"环境场景与氛围","composition":"构图与镜头语言"}'
)


def _format_five_parts(data: dict) -> str:
    """把五要素 JSON 拼装为一行式提示词文本。

    Args:
        data: 含 style/subject/action/environment/composition 键的字典。

    Returns:
        str: 「风格：…｜主体：…｜动作：…｜环境：…｜构图：…」格式文本。

    Raises:
        不抛出异常：data 非字典或键缺失时以占位符兜底。
    """
    data = data if isinstance(data, dict) else {}
    return (
        f"风格：{data.get('style', '待定')}｜主体：{data.get('subject', '待定')}｜"
        f"动作：{data.get('action', '待定')}｜环境：{data.get('environment', '待定')}｜"
        f"构图：{data.get('composition', '待定')}"
    )


async def generation_node(state: AgentState) -> dict:
    """关键帧生成节点：产出五要素关键帧提示词，并写入成本估算参数。

    Args:
        state: 当前图状态；生成依据取值优先级 artifacts["storyboard"] > artifacts["asset_prompt"] > 最后一条人类消息。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(五要素提示词)],
        "artifacts": 合并后产出物（keyframe_prompt / cost_estimate 键），"error": "" 或 "llm_unavailable"}。
        cost_estimate 结构：{"task_type": "image", "params": {"count": N}, "points": 估算积分}，
        供后继 cost 节点消费（成本校验链路）。

    Raises:
        不抛出异常：LLM 调用失败时捕获并输出「（离线降级）」预置五要素模板（成本估算仍会执行）。
    """
    artifacts = dict(state.get("artifacts") or {})
    sb = artifacts.get("storyboard") or {}
    basis = (
        (sb.get("text") if isinstance(sb, dict) else None)
        or (artifacts.get("asset_prompt") or {}).get("text")
        or last_human_text(state)
        or "校园天台黄昏，主角回眸的悬念关键帧"
    )
    # 估算张数：优先取分镜镜头数（每个镜头 1 张关键帧），缺省 3 张
    try:
        shot_count = len((sb or {}).get("shots") or []) if isinstance(sb, dict) else 0
    except Exception:  # noqa: BLE001 —— 结构异常按缺省张数
        shot_count = 0
    frame_count = shot_count or 3
    try:
        raw = await chat(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"请为以下内容生成关键帧提示词（共 {frame_count} 个镜头各 1 张的统一风格基调）：{basis}"},
            ],
            temperature=0.7,
            json_mode=True,
        )
        data = json.loads(raw)
        content = _format_five_parts(data)
        artifacts["keyframe_prompt"] = {"parts": data, "text": content, "count": frame_count}
        error = ""
    except Exception as exc:  # noqa: BLE001 —— LLM/解析异常必须降级
        logger.warning("generation_node LLM 调用失败，降级输出离线模板：%s", exc)
        content = (
            "（离线降级）当前无法连接生成模型，以下为预置关键帧提示词模板：\n"
            "风格：日系新海诚风，黄昏暖色逆光｜主体：十七岁黑发少年，校服微卷袖口｜"
            "动作：立于天台栏杆前回眸｜环境：空旷校园天台，远处操场剪影｜构图：三分法，留白天空压住悬念。"
        )
        artifacts["keyframe_prompt"] = {"text": content, "count": frame_count}
        error = "llm_unavailable"
    # 直接调用 cost_agent 的估算纯函数（成本校验链路：估算结果交给后继 cost 节点播报）
    params = {"count": frame_count}
    artifacts["cost_estimate"] = {
        "task_type": "image",
        "params": params,
        "points": estimate_cost("image", params),
    }
    return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": error}
