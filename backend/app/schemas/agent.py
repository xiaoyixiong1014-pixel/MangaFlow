"""智能体接口 Schema 模块（文件级说明）。

定义 /api/agent 路由的请求/响应模型：SSE 对话请求、会话列表项、
会话消息回放、意图承接（intake）结果。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class AgentChatRequest(BaseModel):
    """SSE 对话请求体。

    Attributes:
        message: 用户消息文本（必填，非空）。
        project_id: 关联项目 ID（可空，缺省未绑定项目）。
        thread_id: 会话 ID（可空，缺省自动生成 f"u{user_id}_{8位随机}"）。
    """

    message: str = Field(min_length=1, description="用户消息")
    project_id: Optional[int] = Field(default=None, description="关联项目 ID（可空）")
    thread_id: Optional[str] = Field(default=None, max_length=80, description="会话 ID（可空，缺省自动生成）")


class ThreadItem(BaseModel):
    """会话列表项。

    Attributes:
        thread_id: 会话唯一标识。
        stage: 当前阶段（最近一次意图）。
        updated_at: 最近更新时间（来自 agent_threads.updated_at）。
    """

    thread_id: str
    stage: Optional[str] = None
    updated_at: Optional[datetime] = None


class AgentMessageItem(BaseModel):
    """会话消息回放项。

    Attributes:
        role: 角色（user/assistant/system）。
        content: 消息文本内容。
    """

    role: str
    content: str


class ThreadMessagesResponse(BaseModel):
    """会话消息回放响应。

    Attributes:
        thread_id: 会话唯一标识。
        stage: 当前阶段。
        messages: 消息回放列表（按时间升序）。
    """

    thread_id: str
    stage: Optional[str] = None
    messages: List[AgentMessageItem]


class IntakeRequest(BaseModel):
    """意图承接（W-01 工作台）请求体。

    Attributes:
        message: 用户输入的需求文本。
    """

    message: str = Field(min_length=1, description="用户需求文本")


class IntakeResponse(BaseModel):
    """意图承接（W-01 工作台）响应体。

    Attributes:
        intent: 意图枚举值（script/asset/storyboard/generation/canvas/billing/chat 等）。
        confidence: 置信度（0-1）。
        mode: 判定方式（"llm"=大模型 / "rule"=关键词规则降级）。
    """

    intent: str
    confidence: float
    mode: str


class AgentResumeRequest(BaseModel):
    """HITL 确认恢复请求体（MA-08，POST /api/agent/chat/resume）。

    Attributes:
        thread_id: 被中断会话的线程 ID（必填）。
        approved: 是否确认通过（True=放行后续流程 / False=拒绝并中止）。
        payload: 附加确认数据（可空，原样并入 resume 值透传给 interrupt 决策）。
    """

    thread_id: str = Field(min_length=1, max_length=80, description="会话线程 ID")
    approved: bool = Field(default=True, description="是否确认通过")
    payload: Optional[dict] = Field(default=None, description="附加确认数据（可空）")


class ThreadHistoryItem(BaseModel):
    """会话检查点历史项（MA-06 Time Travel）。

    Attributes:
        checkpoint_id: 检查点唯一标识（可作为 rewind 目标）。
        ts: 检查点创建时间（ISO 字符串，可空）。
        step: 图执行步数（可空）。
        intent: 该检查点时刻的意图（可空）。
        stage: 该检查点时刻的阶段标记（可空）。
        messages_count: 该检查点时刻消息总数。
        summary: 最后一条 AI 消息前 80 字摘要。
    """

    checkpoint_id: Optional[str] = None
    ts: Optional[str] = None
    step: Optional[int] = None
    intent: Optional[str] = None
    stage: Optional[str] = None
    messages_count: int = 0
    summary: str = ""


class ThreadHistoryResponse(BaseModel):
    """会话检查点历史响应体（GET /api/agent/threads/{thread_id}/history）。

    Attributes:
        thread_id: 会话线程 ID。
        page: 当前页码（1 起）。
        page_size: 每页条数。
        total: 历史检查点总数。
        items: 历史项列表（按 ts 倒序）。
    """

    thread_id: str
    page: int
    page_size: int
    total: int
    items: List[ThreadHistoryItem]


class RewindRequest(BaseModel):
    """会话回溯请求体（MA-06，POST /api/agent/threads/{thread_id}/rewind）。

    Attributes:
        checkpoint_id: 目标检查点 ID（须存在于该线程历史中）。
    """

    checkpoint_id: str = Field(min_length=1, description="回溯目标检查点 ID")


class RewindResponse(BaseModel):
    """会话回溯响应体。

    Attributes:
        ok: 是否成功写入回溯标记。
        thread_id: 会话线程 ID。
        checkpoint_id: 回溯目标检查点 ID。
    """

    ok: bool
    thread_id: str
    checkpoint_id: str
