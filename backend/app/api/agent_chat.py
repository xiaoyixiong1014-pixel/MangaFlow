"""智能体对话路由模块（文件级说明）。

实现 /api/agent 下的多阶段智能体会话接口：
- POST /chat/stream：SSE 流式对话（stage → message → done 三类事件），
  并将会话摘要 upsert 到 agent_threads
- GET /threads：当前用户会话列表
- GET /threads/{thread_id}/messages：会话消息回放（取自 agent_threads.state JSONB）
- POST /intake：W-01 工作台意图承接条（classify_intent 结果）
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.graph import get_compiled_graph
from app.agents.supervisor import classify_intent
from app.core.database import get_db, SessionLocal
from app.core.security import get_current_user
from app.core.sse import sse_event, sse_stream
from app.models.agent import AgentThread
from app.models.user import User
from app.schemas.agent import (
    AgentChatRequest,
    AgentMessageItem,
    IntakeRequest,
    IntakeResponse,
    ThreadItem,
    ThreadMessagesResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

# 回放快照最多保留的消息条数（控制 agent_threads.state JSONB 体积）
_MAX_REPLAY_MESSAGES = 20


def _message_text(message: Any) -> str:
    """把 LangGraph 消息对象的 content 安全转为纯文本。

    Args:
        message: LangChain 消息对象（content 可能为 str 或分段 list）。

    Returns:
        str: 文本内容；分段结构时拼接各段。

    Raises:
        不抛出异常：任何结构异常返回空串。
    """
    try:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        return "".join(str(part) for part in content)
    except Exception:  # noqa: BLE001 —— 序列化兜底
        return ""


def _serialize_replay(messages: List[Any]) -> List[Dict[str, str]]:
    """把 LangGraph 消息对象列表序列化为可入库的回放结构（最后 N 条）。

    Args:
        messages: LangGraph 状态中的消息对象列表。

    Returns:
        List[Dict[str, str]]: [{"role": "user|assistant|system", "content": str}, ...]，
        按时间升序，最多保留 _MAX_REPLAY_MESSAGES 条。

    Raises:
        不抛出异常：单条消息序列化失败时跳过该条。
    """
    replay: List[Dict[str, str]] = []
    for msg in messages[-_MAX_REPLAY_MESSAGES:]:
        if isinstance(msg, HumanMessage):
            role = "user"
        elif isinstance(msg, AIMessage):
            role = "assistant"
        else:
            role = "system"
        content = _message_text(msg)
        if content:
            replay.append({"role": role, "content": content})
    return replay


async def _upsert_thread(thread_id: str, user_id: int, project_id: int | None,
                         stage: str, replay: List[Dict[str, str]]) -> None:
    """把会话摘要 upsert 到 agent_threads（thread_id 唯一，冲突则更新）。

    使用 PostgreSQL INSERT ... ON CONFLICT (thread_id) DO UPDATE；此处必须在
    SSE 生成器内新建独立数据库会话（请求级依赖注入的 session 在响应体生成前
    就已关闭，不能跨生命周期使用）。

    Args:
        thread_id: 会话唯一标识。
        user_id: 用户 ID。
        project_id: 项目 ID（可空）。
        stage: 最近意图（写入 stage 列）。
        replay: 消息回放摘要（写入 state JSONB）。

    Returns:
        None。

    Raises:
        不抛出异常：写库失败仅记录 warning，不影响 SSE 已输出的内容。
    """
    state_payload = {
        "intent": stage,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "messages": replay,
    }
    try:
        async with SessionLocal() as session, session.begin():
            stmt = pg_insert(AgentThread).values(
                thread_id=thread_id,
                user_id=user_id,
                project_id=project_id,
                stage=stage,
                state=state_payload,
            ).on_conflict_do_update(
                index_elements=["thread_id"],
                set_={
                    "stage": stage,
                    "state": state_payload,
                    "user_id": user_id,
                    "project_id": project_id,
                },
            )
            await session.execute(stmt)
    except Exception as exc:  # noqa: BLE001 —— 会话摘要落库失败不应影响对话流
        logger.warning("agent_threads upsert 失败（thread_id=%s）：%s", thread_id, exc)


@router.post("/chat/stream")
async def chat_stream(req: AgentChatRequest, user: User = Depends(get_current_user)):
    """SSE 流式对话：意图预判 → 运行多智能体图 → 会话摘要落库。

    事件序列：
    - stage：{"intent", "mode"} —— 实现说明：为避免解析图内部流事件，先用与
      supervisor 同源的 classify_intent() 预判一次并发出 stage 事件（二者结果
      一致；supervisor 内部的二次细化会额外覆盖扩展意图，不影响预判语义）；
    - message：{"text": 该智能体累计全文, "agent": 节点名} —— 通过
      astream(stream_mode="updates") 逐节点收集新增 AIMessage；
    - done：{"thread_id"} —— 图执行完毕后发出（LLM 降级也有 message，为离线模板）。

    Args:
        req: 对话请求体（message 必填；project_id/thread_id 可空）。
        user: 当前登录用户（Bearer 鉴权）。

    Returns:
        StreamingResponse: text/event-stream SSE 流式响应。

    Raises:
        HTTPException: 401 —— 未登录/令牌非法（由 get_current_user 抛出）。
    """
    thread_id = req.thread_id or f"u{user.id}_{uuid.uuid4().hex[:8]}"
    user_id = user.id
    project_id = req.project_id
    message = req.message

    async def _generator() -> AsyncGenerator[str, None]:
        """SSE 事件生成器（独立的数据库会话生命周期，不依赖请求级 session）。"""
        # 1) 意图预判：与图内 supervisor 使用同一 classify_intent，保证 stage 与路由一致
        try:
            intent_result = await classify_intent(message)
        except Exception as exc:  # noqa: BLE001 —— classify_intent 已兜底，此处双保险
            logger.warning("stage 预判异常，按 chat 兜底：%s", exc)
            intent_result = {"intent": "chat", "confidence": 0.0, "mode": "rule"}
        yield sse_event("stage", {"intent": intent_result["intent"], "mode": intent_result["mode"]})

        # 2) 运行多智能体图：逐节点收集新增 AIMessage 并推送 message 事件
        graph = await get_compiled_graph()
        # 标准可配置格式：astream/aget_state 均从 configurable.thread_id 读取线程标识
        config = {"configurable": {"thread_id": thread_id}}
        graph_input = {
            "messages": [HumanMessage(content=message)],
            "user_id": user_id,
            "project_id": project_id,
        }
        node_texts: Dict[str, str] = {}
        try:
            async for chunk in graph.astream(graph_input, config=config, stream_mode="updates"):
                for node_name, update in chunk.items():
                    if not isinstance(update, dict):
                        continue
                    for msg in (update.get("messages") or []):
                        if isinstance(msg, AIMessage):
                            text = _message_text(msg)
                            if not text:
                                continue
                            node_texts.setdefault(node_name, "")
                            node_texts[node_name] += text
                            yield sse_event("message", {"text": node_texts[node_name], "agent": node_name})
        except Exception as exc:  # noqa: BLE001 —— 图级异常兜底（各节点已内部捕获，此处防御框架层错误）
            logger.warning("图执行异常（thread_id=%s）：%s", thread_id, exc)
            fallback = "（离线降级）智能体服务暂时不可用，请稍后重试。"
            yield sse_event("message", {"text": fallback, "agent": "system"})

        # 3) 会话摘要落库：优先取检查点中的完整消息历史，失败时用本节点收集的文本兜底
        final_intent = intent_result["intent"]
        replay: List[Dict[str, str]] = []
        try:
            snapshot = await graph.aget_state(config)
            values = snapshot.values or {}
            final_intent = values.get("intent") or final_intent
            replay = _serialize_replay(values.get("messages") or [])
        except Exception as exc:  # noqa: BLE001 —— 检查点读取失败时用流内文本兜底
            logger.warning("读取检查点状态失败（thread_id=%s）：%s", thread_id, exc)
        if not replay:
            replay = [{"role": "user", "content": message}] + [
                {"role": "assistant", "content": text} for text in node_texts.values() if text
            ]
        await _upsert_thread(thread_id, user_id, project_id, final_intent, replay)

        # 4) 结束事件
        yield sse_event("done", {"thread_id": thread_id})

    return await sse_stream(_generator())


@router.get("/threads", response_model=List[ThreadItem])
async def list_threads(user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)) -> List[ThreadItem]:
    """获取当前用户的智能体会话列表（按创建时间倒序）。

    Args:
        user: 当前登录用户（Bearer 鉴权）。
        db: 请求级异步数据库会话。

    Returns:
        List[ThreadItem]: [{thread_id, stage, updated_at}, ...]；无会话时返回空数组。

    Raises:
        HTTPException: 401 —— 未登录/令牌非法（由 get_current_user 抛出）。
    """
    result = await db.execute(
        select(AgentThread)
        .where(AgentThread.user_id == user.id)
        .order_by(AgentThread.id.desc())
    )
    threads = result.scalars().all()
    return [
        ThreadItem(thread_id=t.thread_id, stage=t.stage, updated_at=t.updated_at)
        for t in threads
    ]


@router.get("/threads/{thread_id}/messages", response_model=ThreadMessagesResponse)
async def get_thread_messages(thread_id: str, user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)) -> ThreadMessagesResponse:
    """回放指定会话的消息（读 agent_threads.state JSONB 中的消息摘要）。

    Args:
        thread_id: 会话唯一标识（路径参数）。
        user: 当前登录用户（Bearer 鉴权，仅能回放自己的会话）。
        db: 请求级异步数据库会话。

    Returns:
        ThreadMessagesResponse: {thread_id, stage, messages: [{role, content}, ...]}。

    Raises:
        HTTPException: 404 —— 会话不存在或不属于当前用户。
        HTTPException: 401 —— 未登录/令牌非法（由 get_current_user 抛出）。
    """
    result = await db.execute(
        select(AgentThread).where(
            AgentThread.thread_id == thread_id,
            AgentThread.user_id == user.id,
        )
    )
    thread = result.scalar_one_or_none()
    if thread is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    raw_messages = (thread.state or {}).get("messages") or []
    messages = [
        AgentMessageItem(role=str(item.get("role", "system")), content=str(item.get("content", "")))
        for item in raw_messages
        if isinstance(item, dict)
    ]
    return ThreadMessagesResponse(thread_id=thread.thread_id, stage=thread.stage, messages=messages)


@router.post("/intake", response_model=IntakeResponse)
async def intake(req: IntakeRequest, user: User = Depends(get_current_user)) -> IntakeResponse:
    """W-01 工作台意图承接条：对用户需求文本做意图识别并返回分类结果。

    Args:
        req: 意图承接请求体（message 必填）。
        user: 当前登录用户（Bearer 鉴权）。

    Returns:
        IntakeResponse: {"intent", "confidence", "mode"} —— classify_intent 结果。

    Raises:
        HTTPException: 401 —— 未登录/令牌非法（由 get_current_user 抛出）。
    """
    result = await classify_intent(req.message)
    return IntakeResponse(
        intent=result["intent"],
        confidence=float(result["confidence"]),
        mode=result["mode"],
    )
