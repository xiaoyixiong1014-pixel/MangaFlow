"""智能体编排图模块（文件级说明）。

职责：
- build_graph(): 构建 StateGraph 编排（节点注册 + 条件路由 + 成本校验链路），可选挂载 checkpointer
- get_compiled_graph(): 获取带检查点的编译图模块级单例（PostgresSaver 优先，失败降级 InMemorySaver）

图结构：
START → supervisor --(intent 条件路由)→ script/asset/storyboard/generation/compose/
consistency/review/research/canvas/cost(billing)/chat → END；
- script → script_confirm（MA-08 HITL：interrupt 确认实体后 END，拒绝则标记 rejected）；
- generation --(批量拒绝)→ END；--(批量确认)→ Send 并行 generation_item →
  generation_reduce → cost（MA-10 Send 并行）；--(非批量)→ cost（生成 → 成本校验）；
- research → research_exec → research_reflect --(补充任务循环/汇总)→
  research_finalize → END（MA-18 Deep Agents 规划-执行-反思链路）。

MA-13 可观测性（LangSmith 追踪，已接线，待密钥启用）：
应用启动时 main.py lifespan 开头调用 core/tracing.setup_tracing()，按 settings
（config.py 的 langsmith_tracing / langsmith_api_key / langsmith_project，经
LANGSMITH_* 环境变量覆盖）注入 LangChain 标准追踪环境变量（含旧名
LANGCHAIN_TRACING_V2 兼容）；未配置时零开销不上报。启用后本图全部节点执行、
LLM 调用、子图运行均自动产出 trace，可在 LangSmith 控制台按 thread_id /
agent 名检索排障。当前无真实 Key，链路就绪，配置 LANGSMITH_TRACING=true +
LANGSMITH_API_KEY 即启用（详见 backend/README.md「LangSmith 可选接入」）。

检查点决策记录（重要）：
1. langgraph-checkpoint-postgres 的 AsyncPostgresSaver.from_conn_string() 是 async
   context manager，退出 with 块即关闭底层连接，无法与「进程级长期持有的编译图单例」
   共存。因此本模块复刻其内部参数（autocommit=True / prepare_threshold=0 /
   dict_row 行工厂）手动建立连接并由单例长期持有。
2. 实测 Windows（uvicorn 默认 ProactorEventLoop）下 psycopg 异步模式直接报错
   「Psycopg cannot use the 'ProactorEventLoop' to run in async mode」——psycopg
   async 依赖 selector 语义的 IO 回调。为不改全局 asyncio policy（避免影响业务侧
   asyncpg 等组件），采用任务预授权的「底层 PostgresSaver 同步类」方案：同步
   psycopg 连接 + _ThreadedPostgresCheckpointer 适配器（asyncio.to_thread 包装
   异步接口），在同一业务库上实现等价持久化且完全规避事件循环限制。
3. 以上均失败（库不可达/无权限等）→ 降级 InMemorySaver 并记录 warning（检查点
   降级），服务可用性优先于持久化能力。
"""

import asyncio
import logging
from typing import Optional

from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from psycopg import AsyncConnection, Connection
from psycopg.rows import dict_row

from app.agents.asset_agent import asset_node
from app.agents.canvas_agent import canvas_node
from app.agents.compose_agent import compose_node
from app.agents.consistency_agent import consistency_node
from app.agents.cost_agent import cost_node
from app.agents.deep_research_agent import (
    research_exec_node,
    research_finalize_node,
    research_plan_node,
    research_reflect_node,
)
from app.agents.generation_agent import generation_item, generation_node, generation_reduce
from app.agents.llm_adapter import llm
from app.agents.review_agent import review_node
from app.agents.script_agent import script_confirm_node, script_node
from app.agents.state import AgentState, last_human_text
from app.agents.storyboard_agent import storyboard_node
from app.agents.supervisor import supervisor_node
from app.core.config import settings

logger = logging.getLogger(__name__)

# 意图 → 节点名路由映射：
# - billing（积分/充值咨询）由 cost 成本说明节点承接
# - 其余意图与同名节点一一对应
_ROUTER_MAP = {
    "script": "script",
    "asset": "asset",
    "storyboard": "storyboard",
    "generation": "generation",
    "compose": "compose",
    "consistency": "consistency",
    "review": "review",
    "research": "research",
    "canvas": "canvas",
    "billing": "cost",
    "chat": "chat",
}

# chat 节点系统提示词
_CHAT_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow（AI 漫剧创作平台）的智能助手「漫序」。请用中文与用户自然交流，"
    "可以解答平台功能、创作方法等一般问题；如用户需求涉及具体创作（剧本/角色/分镜等），"
    "可提示其在消息中直接说明创作需求，系统会自动分派给对应创作智能体。"
)


def _route_by_intent(state: AgentState) -> str:
    """supervisor 之后的条件路由函数：按 state.intent 映射到目标节点。

    Args:
        state: 当前图状态（AgentState）。

    Returns:
        str: 目标节点名；未知意图一律回退 "chat"。

    Raises:
        不抛出异常：映射缺失时回退 chat，保证图永不崩溃。
    """
    return _ROUTER_MAP.get(state.get("intent") or "chat", "chat")


def _route_after_generation(state: AgentState) -> str:
    """generation 之后的条件路由函数（MA-08 / MA-10）。

    说明：generation_node 批量确认后返回的是 Send 并行分发（LangGraph 直接
    按 Send 目标路由，不经本函数）；本函数仅处理 dict 返回的两种情形：
    - 批量被拒绝（artifacts["aborted"]=True）→ 直达 END（中止流程）；
    - 其余（非批量生成完成）→ 进入 cost 成本校验节点。

    Args:
        state: 当前图状态（AgentState）。

    Returns:
        str: "__end__" 或 "cost"。

    Raises:
        不抛出异常：artifacts 结构异常按非中止处理。
    """
    try:
        if (state.get("artifacts") or {}).get("aborted"):
            return "__end__"
    except Exception:  # noqa: BLE001 —— 路由兜底
        pass
    return "cost"


async def chat_node(state: AgentState) -> dict:
    """通用闲聊节点：直接调用 LLM 回复用户最后一条消息（降级模板兜底）。

    Args:
        state: 当前图状态（AgentState）。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(回复文本)], "error": "" 或 "llm_unavailable"}。

    Raises:
        不抛出异常：LLM 异常时捕获并输出「（离线降级）」预置回复。
    """
    user_text = last_human_text(state)
    try:
        content = await llm.chat(
            state,
            [
                {"role": "system", "content": _CHAT_SYSTEM_PROMPT},
                {"role": "user", "content": user_text or "你好"},
            ],
            agent_name="chat",
            temperature=0.7,
        )
        return {"messages": [AIMessage(content=content)], "error": ""}
    except Exception as exc:  # noqa: BLE001 —— LLM 异常必须降级
        logger.warning("chat_node LLM 调用失败，降级输出离线模板：%s", exc)
        fallback = (
            "（离线降级）你好，我是漫序创作助手。当前模型服务暂不可用，"
            "你仍可以描述创作需求（如「写一个校园悬疑短片剧本」），我会在服务恢复后继续为你创作。"
        )
        return {"messages": [AIMessage(content=fallback)], "error": "llm_unavailable"}


def build_graph(checkpointer=None):
    """构建并编译多智能体编排图。

    节点：supervisor/script/script_confirm/asset/storyboard/generation/
    generation_item/generation_reduce/compose/consistency/review/cost/
    research/research_exec/research_reflect/research_finalize/canvas/chat。
    边设计：
    - START→supervisor：所有请求先经意图识别；
    - supervisor 条件路由：intent→对应域节点（billing→cost，未知→chat）；
    - script→script_confirm→END：MA-08 HITL 确认点（interrupt 等待用户
      确认/拒绝剧本实体）；
    - generation 条件路由：批量拒绝→END；非批量→cost；批量确认经 Send
      并行分发 generation_item（MA-10），全部完成后 generation_reduce→cost；
    - research→research_exec（MA-18 plan 经 Send 并行分发 exec）→
      research_reflect（反思循环：补充任务再派 exec，最多 2 轮）→
      research_finalize→END；
    - 其余域节点→END；chat→END。

    Args:
        checkpointer: LangGraph 检查点实例（AsyncPostgresSaver / InMemorySaver）；
        为 None 时编译为无检查点图（仅供测试，interrupt 节点将不可用）。

    Returns:
        CompiledStateGraph: 可执行的编译图。

    Raises:
        Exception: 节点/边定义非法时由 LangGraph 抛出（属开发期错误，不捕获）。
    """
    builder = StateGraph(AgentState)
    # 注册全部节点
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("script", script_node)
    builder.add_node("script_confirm", script_confirm_node)
    builder.add_node("asset", asset_node)
    builder.add_node("storyboard", storyboard_node)
    builder.add_node("generation", generation_node)
    builder.add_node("generation_item", generation_item)
    builder.add_node("generation_reduce", generation_reduce)
    builder.add_node("compose", compose_node)
    builder.add_node("consistency", consistency_node)
    builder.add_node("review", review_node)
    builder.add_node("cost", cost_node)
    builder.add_node("research", research_plan_node)
    builder.add_node("research_exec", research_exec_node)
    builder.add_node("research_reflect", research_reflect_node)
    builder.add_node("research_finalize", research_finalize_node)
    builder.add_node("canvas", canvas_node)
    builder.add_node("chat", chat_node)
    # 入口与条件路由
    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges("supervisor", _route_by_intent, list(set(_ROUTER_MAP.values())))
    # MA-08 HITL：script → script_confirm（interrupt 确认点）→ END
    builder.add_edge("script", "script_confirm")
    builder.add_edge("script_confirm", END)
    # 成本校验链路：generation --(拒绝→END / 非批量→cost)；批量 Send 分发走
    # generation_item → generation_reduce → cost（Send 目标路由不经条件边）
    builder.add_conditional_edges("generation", _route_after_generation, ["cost", END])
    builder.add_edge("generation_item", "generation_reduce")
    builder.add_edge("generation_reduce", "cost")
    builder.add_edge("cost", END)
    # MA-18 Deep Agents：research(plan) --Send--> research_exec --> research_reflect
    # （反思循环经 Command(goto) 补充派发 exec 或转入 finalize）
    builder.add_edge("research_exec", "research_reflect")
    builder.add_edge("research_finalize", END)
    # 其余域节点直达终点
    for node_name in ("asset", "storyboard", "compose", "consistency",
                      "review", "canvas", "chat"):
        builder.add_edge(node_name, END)
    return builder.compile(checkpointer=checkpointer)


# 模块级检查点单例（懒加载，供主图与剧本子图等所有编译图复用同一实例）
_checkpointer: Optional[BaseCheckpointSaver] = None
_checkpointer_lock = asyncio.Lock()
# 模块级编译图缓存（懒加载单例）
_compiled_graph = None
_compiled_graph_lock = asyncio.Lock()


class _ThreadedPostgresCheckpointer(BaseCheckpointSaver):
    """同步 PostgresSaver 的线程池异步适配器（Windows ProactorEventLoop 兼容方案）。

    背景：psycopg 异步连接在 Windows 默认 ProactorEventLoop 上不可用
    （依赖 selector 语义 IO 回调），而同步 psycopg 连接不受事件循环限制。
    本适配器继承 BaseCheckpointSaver，把同步 PostgresSaver 的全部接口用
    asyncio.to_thread 包装为 async 接口，交给 async 图原样使用——阻塞调用
    在线程池执行，不阻塞事件循环。

    Attributes:
        _saver: 内部持有的同步 PostgresSaver 实例。
    """

    def __init__(self, sync_saver) -> None:
        """初始化适配器。

        Args:
            sync_saver: 已完成 setup() 的同步 PostgresSaver 实例。

        Returns:
            None。

        Raises:
            无显式抛出：透传 BaseCheckpointSaver 初始化行为。
        """
        super().__init__()
        self._saver = sync_saver
        # 与内部 saver 共享序列化器，保证 checkpoint 消息编解码一致
        self.serde = sync_saver.serde

    async def aget_tuple(self, config: dict):
        """异步读取检查点（线程池执行同步 get_tuple）。

        Args:
            config: 含 thread_id 的运行配置。

        Returns:
            CheckpointTuple | None: 检查点元组；无则 None。

        Raises:
            Exception: 透传同步底层异常（由图框架统一处理）。
        """
        return await asyncio.to_thread(self._saver.get_tuple, config)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        """异步列出检查点（包装同步 list 生成器为异步生成器）。

        Args:
            config: 过滤配置（可空）。
            filter: 元数据过滤条件（可空）。
            before: 分页游标（可空）。
            limit: 数量上限（可空）。

        Yields:
            CheckpointTuple: 逐条检查点元组。

        Raises:
            Exception: 透传同步底层异常。
        """
        sentinel = object()
        iterator = await asyncio.to_thread(
            lambda: iter(self._saver.list(config, filter=filter, before=before, limit=limit))
        )
        while True:
            item = await asyncio.to_thread(next, iterator, sentinel)
            if item is sentinel:
                return
            yield item

    async def aput(self, config, checkpoint, metadata, new_versions):
        """异步写入检查点（线程池执行同步 put）。

        Args:
            config: 运行配置。
            checkpoint: 检查点对象。
            metadata: 检查点元数据。
            new_versions: 通道版本。

        Returns:
            RunnableConfig: 写入后的运行配置。

        Raises:
            Exception: 透传同步底层异常。
        """
        return await asyncio.to_thread(self._saver.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        """异步写入中间 writes（线程池执行同步 put_writes）。

        Args:
            config: 运行配置。
            writes: 待写入的 (通道, 值) 序列。
            task_id: 任务 ID。
            task_path: 任务路径（缺省空串）。

        Returns:
            None。

        Raises:
            Exception: 透传同步底层异常。
        """
        await asyncio.to_thread(self._saver.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        """异步删除指定线程全部检查点（线程池执行同步 delete_thread）。

        Args:
            thread_id: 会话线程 ID。

        Returns:
            None。

        Raises:
            Exception: 透传同步底层异常。
        """
        await asyncio.to_thread(self._saver.delete_thread, thread_id)


def _build_sync_postgres_checkpointer():
    """以同步方式建立 PostgresSaver（复刻 from_conn_string 内部连接参数）。

    Args:
        无参数：连接串取自 settings.database_url（postgresql+asyncpg:// 前缀替换为 postgresql://）。

    Returns:
        PostgresSaver: 已完成 setup()（自动建表）的同步检查点实例；连接由实例长期持有，
        进程存活期不关闭（单例语义，等同 AsyncPostgresSaver 的长持有策略）。

    Raises:
        Exception: 数据库不可达/驱动缺失/建表失败时向上抛出（由调用方降级处理）。
    """
    conn_string = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = Connection.connect(
        conn_string,
        autocommit=True,        # 检查点 DDL/写入需 autocommit（与官方实现一致）
        prepare_threshold=0,    # 禁用 prepared statement，兼容连接池/中间件
        row_factory=dict_row,   # 官方同款行工厂
    )
    from langgraph.checkpoint.postgres import PostgresSaver  # 延迟导入：仅在实际使用同步方案时加载

    saver = PostgresSaver(conn)
    saver.setup()  # 自动创建/迁移 langgraph_checkpoints 相关表
    return saver


async def get_checkpointer() -> BaseCheckpointSaver:
    """获取检查点模块级单例（线程/协程安全懒加载，三级降级策略）。

    供主图 get_compiled_graph() 与剧本子图 get_compiled_script_pipeline()
    （app/agents/script_pipeline_graph.py）共同复用，保证同一进程内所有
    编译图共享同一个 checkpointer 实例（同一连接/同一组检查点表，以
    thread_id 维度隔离线程）。

    检查点三级策略（详见模块头「检查点决策记录」）：
    1. AsyncPostgresSaver：当前事件循环非 Windows Proactor 时直接使用（异步原生）；
    2. 同步 PostgresSaver + 线程池适配器：Windows ProactorEventLoop 下的等价持久化方案；
    3. InMemorySaver：以上均失败时降级（重启即丢，服务保持可用，记录「检查点降级」warning）。

    Returns:
        BaseCheckpointSaver: 检查点单例（AsyncPostgresSaver / 线程池适配器 / InMemorySaver）。

    Raises:
        不抛出异常：初始化失败自动逐级降级，恒返回可用实例。
    """
    global _checkpointer
    if _checkpointer is not None:
        return _checkpointer
    async with _checkpointer_lock:
        if _checkpointer is not None:
            return _checkpointer
        checkpointer = None
        async_error = None
        # 策略 1：AsyncPostgresSaver（psycopg 异步模式，需 selector 语义事件循环）
        try:
            current_loop = asyncio.get_running_loop()
            if isinstance(current_loop, asyncio.ProactorEventLoop):
                # Windows 默认事件循环下 psycopg 异步必然失败，直接跳到同步适配方案
                raise RuntimeError("当前为 Windows ProactorEventLoop，psycopg 异步模式不兼容")
            conn_string = settings.database_url.replace(
                "postgresql+asyncpg://", "postgresql://", 1
            )
            conn = await AsyncConnection.connect(
                conn_string,
                autocommit=True,        # 检查点 DDL/写入需 autocommit（与官方 from_conn_string 一致）
                prepare_threshold=0,    # 禁用 prepared statement，兼容连接池/中间件
                row_factory=dict_row,   # 官方同款行工厂
            )
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # 延迟导入，避免降级路径加载负担

            saver = AsyncPostgresSaver(conn)
            await saver.setup()  # 自动创建/迁移 langgraph_checkpoints 相关表
            checkpointer = saver
            logger.info("LangGraph 检查点已启用 AsyncPostgresSaver（持久化到业务库）")
        except Exception as exc:  # noqa: BLE001 —— 异步方案失败转入同步适配
            async_error = exc
            logger.warning("AsyncPostgresSaver 初始化失败（%s），尝试同步线程池适配方案", exc)
        # 策略 2：同步 PostgresSaver + to_thread 适配器（Windows Proactor 兼容）
        if checkpointer is None:
            try:
                checkpointer = _ThreadedPostgresCheckpointer(_build_sync_postgres_checkpointer())
                logger.info(
                    "LangGraph 检查点已启用 PostgresSaver（同步线程池适配；异步方案失败原因：%s）",
                    async_error,
                )
            except Exception as exc:  # noqa: BLE001 —— 同步方案亦失败，最终降级内存检查点
                logger.warning(
                    "检查点降级：PostgresSaver 同步适配也失败（%s），改用 InMemorySaver", exc
                )
                checkpointer = InMemorySaver()
        _checkpointer = checkpointer
        return _checkpointer


async def get_compiled_graph():
    """获取带检查点的编译图模块级单例（线程/协程安全懒加载）。

    检查点经 get_checkpointer() 单例获取（三级降级策略详见该函数说明），
    与剧本子图等其它编译图共享同一 checkpointer 实例。

    Returns:
        CompiledStateGraph: 编译完成的图单例（含 checkpointer）。

    Raises:
        不抛出异常：检查点初始化失败自动降级；图构建本身无运行期外部依赖。
    """
    global _compiled_graph
    if _compiled_graph is not None:
        return _compiled_graph
    async with _compiled_graph_lock:
        if _compiled_graph is not None:
            return _compiled_graph
        checkpointer = await get_checkpointer()
        _compiled_graph = build_graph(checkpointer=checkpointer)
        return _compiled_graph
