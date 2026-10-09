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
    """意图承接响应（classify_intent 结果）。

    Attributes:
        intent: 意图枚举值（script/asset/storyboard/generation/canvas/billing/chat 等）。
        confidence: 置信度（0-1）。
        mode: 判定方式（"llm"=大模型 / "rule"=关键词规则降级）。
    """

    intent: str
    confidence: float
    mode: str
