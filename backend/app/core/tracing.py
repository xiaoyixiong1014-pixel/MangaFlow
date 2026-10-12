"""LangSmith 追踪接线模块（文件级说明）。

MA-13（架构偏离 #6 收敛）：LangGraph/LangChain 原生支持经环境变量自动上报
trace 到 LangSmith（langchain-core >= 0.3 的回调管理器读取标准追踪环境变量，
无需引入新依赖）。setup_tracing() 在应用启动早期（main.py lifespan 开头）
调用一次：
- settings.langsmith_tracing=False（默认）：直接 return，零开销、零副作用；
- True：注入 LangChain 标准追踪环境变量（LANGSMITH_* 新命名 + LANGCHAIN_*
  旧命名兼容），并记录确认日志。

注意：环境变量须在任何 LLM/Graph 调用发生前注入（启动早期一次性设置），
langchain-core 在回调构造时读取，运行期修改不回溯生效。
"""

import logging
import os

from app.core.config import settings

logger = logging.getLogger("mangaflow.core.tracing")


def setup_tracing() -> None:
    """按配置启用 LangSmith 链路追踪（公共函数，进程级一次性调用，幂等）。

    未启用（langsmith_tracing=False）时直接返回，不触碰进程环境变量；
    启用时把 settings 中的 LangSmith 配置写入 os.environ——langchain-core
    读到 LANGSMITH_TRACING / LANGCHAIN_TRACING_V2 后自动为全部 LLM 调用、
    图节点执行与子图运行挂载 LangSmith 回调并上报 trace。新旧两套变量名
    同时设置，兼容不同版本的 langchain-core。

    Returns:
        None

    Raises:
        无：环境变量写入为进程内存操作，不会失败。
    """
    # 未启用：零开销直接返回（不写任何环境变量）
    if not settings.langsmith_tracing:
        return
    # 新版标准命名（langsmith-core / langchain-core >= 0.3 识别）
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    if settings.langsmith_api_key:
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    # 旧版兼容命名（早期 langchain 版本仅识别 LANGCHAIN_TRACING_V2）
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    if settings.langsmith_api_key:
        os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
    logger.info(
        "LangSmith 追踪已启用：project=%s，trace 将上报 LangSmith 控制台"
        "（可在控制台按 thread_id / agent 名检索排障）",
        settings.langsmith_project,
    )
