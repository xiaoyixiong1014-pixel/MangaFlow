"""成本估算智能体模块（文件级说明）。

- estimate_cost(): 纯函数，按任务类型与参数估算积分消耗
  （image=10/张、video=时长*20、tts=每百字 5、llm=每千字 1，均向上取整）
- cost_node(): 图中成本说明节点，输出本次任务的成本估算说明；
  同时承接 billing（积分/充值咨询）意图的路由。
"""

import logging
import math
from typing import Dict, Optional

from langchain_core.messages import AIMessage

from app.agents.state import AgentState, last_human_text

logger = logging.getLogger(__name__)


def estimate_cost(task_type: str, params: Optional[Dict] = None) -> int:
    """按任务类型估算积分消耗（纯函数，无 IO）。

    计价规则：
    - image：10 积分/张（params.count 缺省 1）
    - video：时长 * 20 积分（params.duration，单位秒，向上取整）
    - tts：每 100 字 5 积分（params.text，向上取整）
    - llm：每 1000 字 1 积分（params.text，向上取整）

    Args:
        task_type: 任务类型（image/video/tts/llm）。
        params: 计价参数字典（count/duration/text 按类型取用），可为 None。

    Returns:
        int: 估算积分值（最小 0）；未知任务类型返回 0。

    Raises:
        不抛出异常：参数缺失或类型非法时按默认值/0 兜底。
    """
    try:
        params = params or {}
        if task_type == "image":
            count = int(params.get("count", 1) or 1)
            return 10 * max(count, 1)
        if task_type == "video":
            duration = float(params.get("duration", 0) or 0)
            return math.ceil(duration * 20)
        if task_type == "tts":
            text = str(params.get("text", "") or "")
            return math.ceil(len(text) / 100 * 5)
        if task_type == "llm":
            text = str(params.get("text", "") or "")
            return math.ceil(len(text) / 1000)
        return 0
    except Exception:  # noqa: BLE001 —— 任何参数异常都按 0 兜底
        return 0


async def cost_node(state: AgentState) -> dict:
    """成本说明节点：读取 artifacts 中的成本估算参数，输出积分消耗说明。

    Args:
        state: 当前图状态；优先读取 artifacts["cost_estimate"]（generation 节点写入，
        含 task_type/params），无则按最后一条人类消息文本以 llm 类型估算。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(成本说明文本)], "error": ""}。

    Raises:
        不抛出异常：估算全程走纯函数 estimate_cost，已内部兜底。
    """
    artifacts = dict(state.get("artifacts") or {})
    est = artifacts.get("cost_estimate") or {}
    task_type = est.get("task_type", "llm")
    params = est.get("params")
    if not params:
        params = {"text": last_human_text(state)}
    points = estimate_cost(task_type, params)
    content = (
        f"💰 成本估算：本次任务类型为 {task_type}，预计消耗 {points} 积分。"
        "生成类操作将在实际执行前进行余额校验，请确保账户积分充足。"
    )
    return {"messages": [AIMessage(content=content)], "error": ""}
