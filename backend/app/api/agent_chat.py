"""智能体对话路由模块（文件级说明）。

实现 /api/agent 下的多阶段智能体会话接口：
- POST /chat/stream：SSE 流式对话（stage → message/token → done 事件，
  MA-11 双流 updates+messages；遇 HITL 中断推 pending_confirm 后结束本次流），
  并将会话摘要 upsert 到 agent_threads
- POST /chat/resume：MA-08 HITL 确认恢复（Command(resume=...) 续跑被中断的图）
- GET /threads：当前用户会话列表
- GET /threads/{thread_id}/messages：会话消息回放（取自 agent_threads.state JSONB）
- GET /threads/{thread_id}/history：MA-06 Time Travel 检查点历史（分页）
- POST /threads/{thread_id}/rewind：回溯标记（写入 rewind_checkpoint，下次
  对话从该检查点分叉续聊，一次性生效）
- POST /intake：W-01 工作台意图承接条（classify_intent 结果）
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.graph import get_compiled_graph
from app.agents.script_pipeline_graph import get_compiled_script_pipeline
from app.agents.supervisor import classify_intent
from app.core.database import get_db, SessionLocal
from app.core.security import get_current_user
from app.core.sse import sse_event, sse_stream
from app.models.agent import AgentThread
from app.models.user import User
from app.schemas.agent import (
    AgentChatRequest,
    AgentMessageItem,
    AgentResumeRequest,
    IntakeRequest,
    IntakeResponse,
    RewindRequest,
    RewindResponse,
    ThreadHistoryItem,
    ThreadHistoryResponse,
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
        # P-5：project_id 一并记录（script_pipeline 意图的 resume 分流依赖
        # state.intent；project_id 供断线恢复/前端回放定位项目）
        "project_id": project_id,
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
            # MA-07 记忆接线（Task 6.4）：会话摘要文本同步写入用户记忆库
            # （langgraph_store，复用同事务 session，commit=False；失败由外层 try 兜底）
            from app.services.memory_service import upsert_memory
            await upsert_memory(
                session, user_id, "chat", thread_id,
                "\n".join(f"{m['role']}: {m['content']}" for m in replay[-4:]),
                commit=False,
            )
    except Exception as exc:  # noqa: BLE001 —— 会话摘要落库失败不应影响对话流
        logger.warning("agent_threads upsert 失败（thread_id=%s）：%s", thread_id, exc)


async def _stream_graph_events(
    graph: Any,
    *,
    config: Dict[str, Any],
    graph_input: Any,
    thread_id: str,
    user_id: int,
    project_id: Optional[int],
    fallback_user_text: str = "",
    clear_rewind: bool = False,
    default_intent: str = "chat",
) -> AsyncGenerator[str, None]:
    """公共图事件泵（chat/stream 与 chat/resume 两个端点复用，MA-08/10/11）。

    消费 astream(stream_mode=["updates", "messages"]) 双流：
    - updates 流：逐节点收集新增 AIMessage 推 message 事件（保持既有格式）；
      检测到 __interrupt__ 时推 pending_confirm 事件（MA-08 HITL），随后结束
      本次流（检查点已保留状态，等 resume 续跑）；
    - messages 流：(AIMessageChunk, metadata) 逐段推 token 事件
      {"text": 增量文本, "agent": langgraph_node}（MA-11）；
    - 结束后（无论是否中断）读取检查点快照落库会话摘要并推 done 事件；
      clear_rewind=True 时同步清空 agent_threads.rewind_checkpoint
      （MA-06 分叉续聊一次性语义）。

    Args:
        graph: 编译后的 LangGraph 图。
        config: 运行配置（{"configurable": {"thread_id": ...}}，可含 checkpoint_id）。
        graph_input: 图输入——新对话为 dict（messages/user_id/project_id），
            确认恢复为 Command(resume=...)。
        thread_id: 会话线程 ID。
        user_id: 用户 ID（落库归属）。
        project_id: 项目 ID（可空）。
        fallback_user_text: 检查点读取失败时回放兜底的用户消息文本（resume 场景为空串）。
        clear_rewind: 本次对话完成后是否清空 rewind_checkpoint 标记。
        default_intent: 检查点快照缺意图时的落库兜底意图（chat/stream 传入预判结果）。

    Yields:
        str: SSE 文本帧（message/token/pending_confirm/done 事件）。

    Raises:
        不抛出异常：图执行异常输出离线降级 message；落库/清标记失败仅告警。
    """
    node_texts: Dict[str, str] = {}
    interrupted = False
    try:
        async for mode, chunk in graph.astream(graph_input, config=config,
                                               stream_mode=["updates", "messages"]):
            if mode == "updates":
                # MA-08 HITL：interrupt 更新 → pending_confirm 事件后结束本次流
                if isinstance(chunk, dict) and "__interrupt__" in chunk:
                    for itp in (chunk.get("__interrupt__") or ()):
                        value = getattr(itp, "value", None)
                        value = value if isinstance(value, dict) else {}
                        yield sse_event("pending_confirm", {
                            "thread_id": thread_id,
                            "confirm_type": value.get("type"),
                            "payload": value.get("payload"),
                        })
                    interrupted = True
                    break
                for node_name, update_ in (chunk or {}).items():
                    if node_name == "__interrupt__" or not isinstance(update_, dict):
                        continue
                    for msg in (update_.get("messages") or []):
                        if isinstance(msg, AIMessage):
                            text = _message_text(msg)
                            if not text:
                                continue
                            node_texts.setdefault(node_name, "")
                            node_texts[node_name] += text
                            yield sse_event("message", {"text": node_texts[node_name], "agent": node_name})
            elif mode == "messages":
                # MA-11 双流：逐 token 推送（content 为空/工具调用块时跳过）
                msg_chunk, metadata = chunk if isinstance(chunk, tuple) and len(chunk) == 2 else (None, None)
                text = _message_text(msg_chunk) if msg_chunk is not None else ""
                if text:
                    yield sse_event("token", {
                        "text": text,
                        "agent": (metadata or {}).get("langgraph_node") or "system",
                    })
    except Exception as exc:  # noqa: BLE001 —— 图级异常兜底（各节点已内部捕获，此处防御框架层错误）
        logger.warning("图执行异常（thread_id=%s）：%s", thread_id, exc)
        fallback = "（离线降级）智能体服务暂时不可用，请稍后重试。"
        yield sse_event("message", {"text": fallback, "agent": "system"})

    # 会话摘要落库：优先取检查点中的完整消息历史，失败时用流内收集文本兜底
    replay: List[Dict[str, str]] = []
    final_intent = ""
    try:
        snapshot = await graph.aget_state(config)
        values = snapshot.values or {}
        final_intent = str(values.get("intent") or "")
        replay = _serialize_replay(values.get("messages") or [])
    except Exception as exc:  # noqa: BLE001 —— 检查点读取失败时用流内文本兜底
        logger.warning("读取检查点状态失败（thread_id=%s）：%s", thread_id, exc)
    if not replay and (node_texts or fallback_user_text):
        replay = ([{"role": "user", "content": fallback_user_text}] if fallback_user_text else []) + [
            {"role": "assistant", "content": text} for text in node_texts.values() if text
        ]
    if replay:
        await _upsert_thread(thread_id, user_id, project_id,
                             final_intent or default_intent or "chat", replay)

    # MA-06：分叉续聊完成后清空一次性回溯标记
    if clear_rewind:
        try:
            async with SessionLocal() as session, session.begin():
                await session.execute(
                    update(AgentThread)
                    .where(AgentThread.thread_id == thread_id)
                    .values(rewind_checkpoint=None)
                )
        except Exception as exc:  # noqa: BLE001 —— 清标记失败不影响对话流
            logger.warning("清空 rewind_checkpoint 失败（thread_id=%s）：%s", thread_id, exc)

    # 结束事件（interrupted 标记为附加字段，向后兼容）
    yield sse_event("done", {"thread_id": thread_id, "interrupted": interrupted})


@router.post("/chat/stream")
async def chat_stream(req: AgentChatRequest, user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    """SSE 流式对话：意图预判 → 运行多智能体图 → 会话摘要落库。

    事件序列：
    - stage：{"intent", "mode"} —— 实现说明：为避免解析图内部流事件，先用与
      supervisor 同源的 classify_intent() 预判一次并发出 stage 事件（二者结果
      一致；supervisor 内部的二次细化会额外覆盖扩展意图，不影响预判语义）；
    - message：{"text": 该智能体累计全文, "agent": 节点名} —— updates 流逐节点
      收集新增 AIMessage（格式与既有版本兼容）；
    - token：{"text": 增量文本, "agent": 节点名} —— messages 流逐 token 推送
      （MA-11 双流，附加事件不影响既有消费方）；
    - pending_confirm：{"thread_id", "confirm_type", "payload"} —— 图内 HITL
      interrupt 触发时推送（MA-08），随后本次流结束，等 resume 续跑；
    - done：{"thread_id", "interrupted"} —— 图执行完毕后发出。

    P-5 意图分流：classify_intent 判为 script_pipeline（整本剧本流水线）时
    运行剧本子图（app/agents/script_pipeline_graph.py）而非主图，子图的三个
    interrupt（confirm_characters/confirm_episodes/confirm_body）复用同一
    pending_confirm 事件通道；子图运行要求请求携带 project_id（缺失时输出
    提示 message 后结束本次流）。

    Args:
        req: 对话请求体（message 必填；project_id/thread_id 可空）。
        user: 当前登录用户（Bearer 鉴权）。
        db: 请求级异步数据库会话（读取回溯标记 rewind_checkpoint）。

    Returns:
        StreamingResponse: text/event-stream SSE 流式响应。

    Raises:
        HTTPException: 401 —— 未登录/令牌非法（由 get_current_user 抛出）。
    """
    thread_id = req.thread_id or f"u{user.id}_{uuid.uuid4().hex[:8]}"
    user_id = user.id
    project_id = req.project_id
    message = req.message

    # MA-06：读取一次性回溯标记（仅归属本人的会话生效）
    rewind_checkpoint: Optional[str] = None
    if req.thread_id:
        try:
            row = (
                await db.execute(
                    select(AgentThread).where(
                        AgentThread.thread_id == thread_id,
                        AgentThread.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if row is not None and row.rewind_checkpoint:
                rewind_checkpoint = row.rewind_checkpoint
        except Exception as exc:  # noqa: BLE001 —— 回溯标记读取失败按普通对话处理
            logger.warning("读取 rewind_checkpoint 失败（thread_id=%s）：%s", thread_id, exc)

    async def _generator() -> AsyncGenerator[str, None]:
        """SSE 事件生成器（独立的数据库会话生命周期，不依赖请求级 session）。"""
        # 1) 意图预判：与图内 supervisor 使用同一 classify_intent，保证 stage 与路由一致
        try:
            intent_result = await classify_intent(message)
        except Exception as exc:  # noqa: BLE001 —— classify_intent 已兜底，此处双保险
            logger.warning("stage 预判异常，按 chat 兜底：%s", exc)
            intent_result = {"intent": "chat", "confidence": 0.0, "mode": "rule"}
        yield sse_event("stage", {"intent": intent_result["intent"], "mode": intent_result["mode"]})

        # 2) 运行图（P-5 分流：script_pipeline 意图走剧本子图，其余走主图）
        # 标准可配置格式：astream/aget_state 均从 configurable.thread_id 读取线程标识
        config: Dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        if rewind_checkpoint:
            # MA-06 Time Travel：从指定检查点分叉续聊（本次对话完成后清空标记）
            config["configurable"]["checkpoint_id"] = rewind_checkpoint
        if intent_result["intent"] == "script_pipeline":
            if not project_id:
                # 子图入口强依赖 project_id：缺失时输出提示并结束本次流
                yield sse_event("message", {
                    "text": "整本剧本流水线需要绑定项目：请在请求中携带 project_id 后重试。",
                    "agent": "system",
                })
                yield sse_event("done", {"thread_id": thread_id, "interrupted": False})
                return
            graph = await get_compiled_script_pipeline()
            # 子图输入：project_id 必填；用户消息作为补充指令透传（剧本输入源
            # 文本由子图 sp_input 节点从 scripts 表读取）
            graph_input: Any = {"project_id": project_id, "instruction": message}
        else:
            graph = await get_compiled_graph()
            graph_input = {
                "messages": [HumanMessage(content=message)],
                "user_id": user_id,
                "project_id": project_id,
            }
        async for frame in _stream_graph_events(
            graph,
            config=config,
            graph_input=graph_input,
            thread_id=thread_id,
            user_id=user_id,
            project_id=project_id,
            fallback_user_text=message,
            clear_rewind=bool(rewind_checkpoint),
            default_intent=intent_result["intent"],
        ):
            yield frame

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


@router.post("/chat/resume")
async def chat_resume(req: AgentResumeRequest, user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    """HITL 确认恢复端点（MA-08）：以 Command(resume=...) 续跑被 interrupt 的图。

    复用 chat/stream 的公共事件泵：恢复运行同样输出 message/token/
    pending_confirm（可能连环确认）/done 事件。会话必须存在且属于当前用户。

    P-5 分流：会话 state.intent 为 script_pipeline 时续跑剧本子图（三闸口
    confirm_characters/confirm_episodes/confirm_body 的连环确认即经本端点），
    否则续跑主图。决策值统一为 {"approved": bool, **payload}；请求体显式传
    decision（approved/rejected）时以其为准，feedback 原样并入 resume 值
    透传给 interrupt 决策。

    Args:
        req: 恢复请求体（thread_id 必填；approved 默认 True；decision/feedback/
        payload 可选附加）。
        user: 当前登录用户（Bearer 鉴权）。
        db: 请求级异步数据库会话（会话归属校验）。

    Returns:
        StreamingResponse: text/event-stream SSE 流式响应。

    Raises:
        HTTPException: 401 —— 未登录/令牌非法；404 —— 会话不存在或不属于当前用户。
    """
    thread_id = req.thread_id
    row = (
        await db.execute(
            select(AgentThread).where(
                AgentThread.thread_id == thread_id,
                AgentThread.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    # P-5 分流：按会话落库的意图选择续跑的图（子图/主图共享 checkpointer
    # 与 thread_id 命名空间，恢复配置结构一致）
    thread_state = row.state if isinstance(row.state, dict) else {}
    if str(thread_state.get("intent") or "") == "script_pipeline":
        graph = await get_compiled_script_pipeline()
    else:
        graph = await get_compiled_graph()
    config: Dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    # resume 值：{"approved": bool, **payload} —— interrupt() 处原样接收为决策
    resume_value: Dict[str, Any] = {"approved": bool(req.approved)}
    if req.decision is not None:
        resume_value["approved"] = req.decision == "approved"
    if req.feedback:
        resume_value["feedback"] = req.feedback
    if req.payload:
        resume_value.update(req.payload)
    command = Command(resume=resume_value)

    async def _generator() -> AsyncGenerator[str, None]:
        """SSE 事件生成器：复用公共事件泵续跑图（归属信息取自会话行）。"""
        async for frame in _stream_graph_events(
            graph,
            config=config,
            graph_input=command,
            thread_id=thread_id,
            user_id=row.user_id,
            project_id=row.project_id,
            fallback_user_text="",
        ):
            yield frame

    return await sse_stream(_generator())


@router.get("/threads/{thread_id}/history", response_model=ThreadHistoryResponse)
async def get_thread_history(thread_id: str,
                             page: int = Query(1, ge=1, description="页码（1 起）"),
                             page_size: int = Query(20, ge=1, le=100, description="每页条数"),
                             user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)) -> ThreadHistoryResponse:
    """会话检查点历史（MA-06 Time Travel）：返回可回溯的检查点分页列表。

    通过 graph.aget_state_history 收集最近 100 个检查点，输出
    {checkpoint_id, ts, step, intent, stage, messages_count, summary(最后一条
    AI 消息前 80 字)}，按 ts 倒序后分页。会话须存在且属于当前用户。

    Args:
        thread_id: 会话线程 ID（路径参数）。
        page: 页码（默认 1）。
        page_size: 每页条数（默认 20，上限 100）。
        user: 当前登录用户（Bearer 鉴权）。
        db: 请求级异步数据库会话（会话归属校验）。

    Returns:
        ThreadHistoryResponse: 分页后的检查点历史。

    Raises:
        HTTPException: 401 —— 未登录/令牌非法；404 —— 会话不存在或不属于当前用户。
    """
    row = (
        await db.execute(
            select(AgentThread).where(
                AgentThread.thread_id == thread_id,
                AgentThread.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    graph = await get_compiled_graph()
    config = {"configurable": {"thread_id": thread_id}}
    items: List[ThreadHistoryItem] = []
    try:
        async for snap in graph.aget_state_history(config, limit=100):
            values = snap.values or {}
            messages = values.get("messages") or []
            last_ai = ""
            for msg in reversed(messages):
                if isinstance(msg, AIMessage):
                    last_ai = _message_text(msg)
                    break
            checkpoint_id = None
            try:
                checkpoint_id = (snap.config or {}).get("configurable", {}).get("checkpoint_id")
            except Exception:  # noqa: BLE001 —— 配置结构异常按无检查点处理
                checkpoint_id = None
            metadata = snap.metadata or {}
            items.append(ThreadHistoryItem(
                checkpoint_id=checkpoint_id,
                ts=str(snap.created_at) if snap.created_at else None,
                step=int(metadata["step"]) if metadata.get("step") is not None else None,
                intent=str(values.get("intent") or "") or None,
                stage=str(values.get("stage") or "") or None,
                messages_count=len(messages),
                summary=last_ai[:80],
            ))
    except Exception as exc:  # noqa: BLE001 —— 历史读取失败返回空列表
        logger.warning("读取检查点历史失败（thread_id=%s）：%s", thread_id, exc)
    items.sort(key=lambda it: it.ts or "", reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    return ThreadHistoryResponse(
        thread_id=thread_id,
        page=page,
        page_size=page_size,
        total=total,
        items=items[start:start + page_size],
    )


@router.post("/threads/{thread_id}/rewind", response_model=RewindResponse)
async def rewind_thread(thread_id: str, req: RewindRequest,
                        user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)) -> RewindResponse:
    """会话回溯标记（MA-06 Time Travel）：把 rewind_checkpoint 写入会话行。

    校验 checkpoint_id 确实存在于该线程的检查点历史中后写入标记；下一次
    chat/stream 会从该检查点分叉续聊，并在对话完成后自动清空标记（一次性）。

    Args:
        thread_id: 会话线程 ID（路径参数）。
        req: 回溯请求体（checkpoint_id 必填）。
        user: 当前登录用户（Bearer 鉴权）。
        db: 请求级异步数据库会话（归属校验与标记写入）。

    Returns:
        RewindResponse: {"ok": True, "thread_id", "checkpoint_id"}。

    Raises:
        HTTPException: 400 —— 检查点不在该线程历史中；401 —— 未登录；
            404 —— 会话不存在或不属于当前用户。
    """
    row = (
        await db.execute(
            select(AgentThread).where(
                AgentThread.thread_id == thread_id,
                AgentThread.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 校验检查点存在于该线程历史（避免把他人/他线程的检查点注入）
    graph = await get_compiled_graph()
    config = {"configurable": {"thread_id": thread_id}}
    found = False
    try:
        async for snap in graph.aget_state_history(config, limit=100):
            try:
                checkpoint_id = (snap.config or {}).get("configurable", {}).get("checkpoint_id")
            except Exception:  # noqa: BLE001 —— 配置结构异常跳过该快照
                continue
            if checkpoint_id == req.checkpoint_id:
                found = True
                break
    except Exception as exc:  # noqa: BLE001 —— 历史读取失败按未找到处理
        logger.warning("回溯校验读取检查点历史失败（thread_id=%s）：%s", thread_id, exc)
    if not found:
        raise HTTPException(status_code=400, detail="检查点不存在于该会话历史中")

    row.rewind_checkpoint = req.checkpoint_id
    await db.commit()
    return RewindResponse(ok=True, thread_id=thread_id, checkpoint_id=req.checkpoint_id)
