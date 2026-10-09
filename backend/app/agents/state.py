"""智能体共享状态定义模块（文件级说明）。

定义 LangGraph 多智能体系统的全局共享状态 AgentState（TypedDict），
以及各域节点共用的「提取最后一条人类消息」工具函数。
所有节点以 AgentState 为唯一数据总线，通过返回部分更新字典驱动状态流转。
"""

from typing import Annotated, List, Optional, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    """LangGraph 图的全局共享状态（total=False：所有通道均可缺省，节点按需读写）。

    Attributes:
        thread_id: 会话唯一标识，与 agent_threads.thread_id 对应，用作检查点线程 ID。
        user_id: 发起会话的用户 ID（用于鉴权与数据归属）。
        project_id: 关联项目 ID，可空（未绑定项目时为 None）。
        intent: supervisor 识别并细化的路由意图（script/asset/storyboard/generation/compose/consistency/review/research/canvas/billing/chat）。
        stage: 当前流程阶段标记（预留通道，如 draft/storyboard/generation）。
        messages: 对话消息流；add_messages reducer 语义为「节点返回的新消息追加到既有列表」，是图内所有节点对外输出的统一通道。
        artifacts: 各域智能体的产出物汇总字典（brief/script/asset_prompt/storyboard/cost_estimate/research/compose/consistency/review/canvas_suggestion 等键），节点采用「读取-合并-写回」策略避免覆盖他人产出。
        error: 最近一次节点错误标记；空串表示无错误，"llm_unavailable" 表示 LLM 调用失败已走离线降级模板。
    """

    thread_id: str
    user_id: int
    project_id: Optional[int]
    intent: str
    stage: str
    messages: Annotated[List[AnyMessage], add_messages]
    artifacts: dict
    error: str


def last_human_text(state: AgentState) -> str:
    """提取状态消息流中最后一条人类消息的文本内容。

    Args:
        state: 当前图状态（AgentState）。

    Returns:
        str: 最后一条 HumanMessage 的文本；消息为多段结构时拼接为字符串；
        不存在人类消息时返回空串（调用方应自行提供默认输入）。

    Raises:
        不抛出异常：任何结构异常均安全返回空串。
    """
    try:
        for msg in reversed(state.get("messages") or []):
            if isinstance(msg, HumanMessage):
                content = msg.content
                if isinstance(content, str):
                    return content
                # 多模态/分段 content：拼接文本部分
                return "".join(str(part) for part in content)
    except Exception:  # noqa: BLE001 —— 工具函数保证不抛异常
        return ""
    return ""
