"""分镜设计智能体模块（文件级说明）。

storyboard_node：输入场景描述，拆解为 3-5 个镜头，每个镜头给出
景别 / 运镜 / 时长 / 画面四要素的中文分镜表。
"""

import json
import logging

from langchain_core.messages import AIMessage

from app.agents.llm_adapter import llm
from app.agents.state import AgentState, last_human_text

logger = logging.getLogger(__name__)

# 分镜设计系统提示词：强制 JSON 输出镜头数组
_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的分镜设计师，擅长把场景描述拆解为可执行的漫剧分镜。"
    "请使用中文，拆解出 3-5 个镜头，仅输出 JSON（不要输出其他内容）：\n"
    '{"shots":[{"shot":1,"size":"景别（远/全/中/近/特）","camera":"运镜（推/拉/摇/移/固定等）",'
    '"duration":"时长（如 3s）","visual":"画面内容描述"}]}\n'
    "要求：镜头间有叙事递进，画面描述具体可视，总时长适配 3-5 分钟漫剧节奏。"
)


def _format_shots(shots) -> str:
    """把镜头数组格式化为分镜表文本。

    Args:
        shots: 镜头列表，每项含 shot/size/camera/duration/visual 字段。

    Returns:
        str: 「镜头 N｜景别…｜运镜…｜时长…｜画面…」逐行文本。

    Raises:
        不抛出异常：结构异常时返回空串。
    """
    try:
        lines = []
        for s in shots:
            lines.append(
                f"镜头 {s.get('shot', '-')}｜景别：{s.get('size', '-')}｜"
                f"运镜：{s.get('camera', '-')}｜时长：{s.get('duration', '-')}｜"
                f"画面：{s.get('visual', '-')}"
            )
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 —— 格式化失败返回空串
        return ""


async def storyboard_node(state: AgentState) -> dict:
    """分镜设计节点：把场景描述拆解为 3-5 个镜头的分镜表。

    Args:
        state: 当前图状态；场景描述取值优先级 artifacts["storyboard_input"] > 最后一条人类消息。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(分镜表文本)],
        "artifacts": 合并后产出物（storyboard 键，含结构化 shots 与文本），"error": "" 或 "llm_unavailable"}。

    Raises:
        不抛出异常：LLM 调用失败时捕获并输出「（离线降级）」预置三镜头模板。
    """
    artifacts = dict(state.get("artifacts") or {})
    scene = artifacts.get("storyboard_input") or last_human_text(state) or "主角在校园天台发现秘密的场景"
    try:
        raw = await llm.chat(
            state,
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"场景描述：{scene}"},
            ],
            agent_name="storyboard",
            temperature=0.7,
            json_mode=True,
        )
        data = json.loads(raw)
        shots = data.get("shots", []) if isinstance(data, dict) else []
        content = _format_shots(shots) or "（模型未返回有效镜头，请重试）"
        artifacts["storyboard"] = {"shots": shots, "text": content}
        artifacts = llm.attach_usage(artifacts)
        return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": ""}
    except Exception as exc:  # noqa: BLE001 —— LLM/解析异常必须降级
        logger.warning("storyboard_node LLM 调用失败，降级输出离线模板：%s", exc)
        fallback = (
            "（离线降级）当前无法连接分镜模型，以下为预置三镜头模板：\n"
            "镜头 1｜景别：全景｜运镜：缓推｜时长：4s｜画面：黄昏校园天台全景，主角剪影立于栏杆前。\n"
            "镜头 2｜景别：特写｜运镜：固定｜时长：3s｜画面：主角眼神微颤，手中纸条一角入画。\n"
            "镜头 3｜景别：近景｜运镜：慢摇｜时长：4s｜画面：主角回头，夕阳逆光勾勒侧脸，悬念定格。"
        )
        artifacts["storyboard"] = {"text": fallback}
        return {"messages": [AIMessage(content=fallback)], "artifacts": artifacts, "error": "llm_unavailable"}
