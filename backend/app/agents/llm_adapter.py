"""智能体 LLM 适配器模块（文件级说明）。

对应里程碑 MA-16 / MA-19 / MA-20（统一 Agent 侧 LLM 出入口与用量观测）：
- AgentLLM：多渠道（用户自有渠道前置 → DeepSeek → Agnes）LLM 调用适配器，
  所有 Agent 节点统一经本适配器调用 providers.llm，保持原 chat()/chat_stream()
  行为不变；从图状态 user_id 前置注入该用户自有 openai 兼容渠道（免生成积分
  优先使用），全部自有渠道失败仍降级平台默认链；
- 每次调用成功后把用量元信息 {"agent","channel","model","tokens","prompt_chars"}
  写入模块级 ContextVar（_last_usage），节点函数通过 get_last_usage()/attach_usage()
  取用并追加到 artifacts["_usage"]，实现无侵入的用量观测；
- estimate_tokens(): 中文粗估 token 数（len//2）；
- usage_record(): 标准用量记录结构构造器。

说明：
1. ContextVar 采用「调用前置 None → 成功后写入」策略，避免节点降级路径误读
   上一次调用的陈旧用量；
2. 流式调用仅在收到首块（携带渠道元信息）时记录用量，与渠道降级语义一致。
"""

import logging
from contextvars import ContextVar
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.providers.llm import _channels as _default_channels
from app.providers.llm import chat_stream_with_meta, chat_with_meta
from app.services.model_router import get_user_llm_channel_tuples

logger = logging.getLogger(__name__)

# 模块级上下文变量：保存最近一次 LLM 调用的用量记录（默认 None）
_last_usage: ContextVar = ContextVar("agent_llm_last_usage", default=None)


def _user_id_from_state(state_aware: Any) -> Optional[int]:
    """从图状态提取 user_id（内部函数）。

    Args:
        state_aware: 调用方所在的图状态（AgentState dict）或 None。

    Returns:
        Optional[int]: 用户 ID；状态缺失/结构异常/无 user_id 时返回 None。

    Raises:
        无：任何结构异常均安全返回 None。
    """
    try:
        if isinstance(state_aware, dict):
            uid = state_aware.get("user_id")
            return int(uid) if uid is not None else None
    except (TypeError, ValueError):
        return None
    return None


def estimate_tokens(text: Any) -> int:
    """粗估文本 token 数（中文场景按 2 字符 ≈ 1 token 线性折算）。

    Args:
        text: 任意输入（通常为 str；非字符串安全转 str，None/异常按空处理）。

    Returns:
        int: 估算 token 数（len(text)//2，最小 0）。

    Raises:
        不抛出异常：任何输入类型异常均按 0 兜底。
    """
    try:
        s = "" if text is None else str(text)
    except Exception:  # noqa: BLE001 —— 工具函数保证不抛异常
        return 0
    return len(s) // 2


def usage_record(agent_name: str, channel: Optional[str], model: Optional[str], tokens: int) -> dict:
    """构造标准用量记录结构（供 artifacts["_usage"] / 观测管道消费）。

    Args:
        agent_name: 发起调用的智能体/节点名（如 "script"、"generation_item"）。
        channel: 实际使用的渠道名（deepseek/agnes），未知为 None。
        model: 实际使用的模型名，未知为 None。
        tokens: 粗估 token 消耗（输入 + 输出）。

    Returns:
        dict: {"agent", "channel", "model", "tokens", "prompt_chars"} 结构；
        prompt_chars 由调用方按需补充。

    Raises:
        不抛出异常：纯结构构造。
    """
    return {
        "agent": agent_name,
        "channel": channel,
        "model": model,
        "tokens": int(tokens or 0),
        "prompt_chars": 0,
    }


def get_last_usage() -> Optional[dict]:
    """读取当前上下文中最近一次 LLM 调用的用量记录。

    Returns:
        Optional[dict]: 用量记录；当前上下文尚未发生过成功调用（或最近一次
        调用失败已清空）时返回 None。

    Raises:
        不抛出异常。
    """
    return _last_usage.get()


class AgentLLM:
    """Agent 侧 LLM 适配器（MA-16）：双渠道调用 + 用量记录（类级说明）。

    所有智能体节点经本类调用 LLM，替代直接使用 providers.llm.chat：
    - 行为完全兼容（同样的多渠道降级、同样的参数语义）；
    - 额外在每次成功调用后写入模块级 ContextVar 用量记录，节点可经
      attach_usage() 把 {"agent","channel","model","tokens","prompt_chars"}
      追加进 artifacts["_usage"]。

    Attributes:
        无实例状态：适配器无状态，可全局共用单例 llm。
    """

    @staticmethod
    def _prompt_chars(messages: List[Dict[str, str]]) -> int:
        """统计消息列表的提示词总字符数（内部函数）。

        Args:
            messages: OpenAI 格式消息列表。

        Returns:
            int: 全部 content 字符数之和（异常按 0 兜底）。

        Raises:
            不抛出异常。
        """
        try:
            return sum(len(str(m.get("content") or "")) for m in messages if isinstance(m, dict))
        except Exception:  # noqa: BLE001 —— 统计失败不影响主流程
            return 0

    async def chat(
        self,
        state_aware: Any,
        messages: List[Dict[str, str]],
        *,
        agent_name: str,
        json_mode: bool = False,
        temperature: float = 0.7,
    ) -> str:
        """非流式对话补全（状态感知入口，内部走 chat_with_meta 多渠道降级）。

        Args:
            state_aware: 调用方所在的图状态（AgentState）或 None；仅用于日志
                上下文（thread_id），不参与请求构造。
            messages: OpenAI 格式消息列表（role/content）。
            agent_name: 智能体名（写入用量记录的 agent 字段）。
            json_mode: True 时启用 JSON 输出模式。
            temperature: 采样温度。

        Returns:
            str: 模型返回的完整文本内容。

        Raises:
            openai.APIError: 全部渠道均失败时抛出最后一个渠道的上游异常
                （与 providers.llm.chat 语义一致）；抛出前会清空用量记录，
                避免节点降级路径读到陈旧用量。
        """
        thread_id = None
        try:
            thread_id = state_aware.get("thread_id") if isinstance(state_aware, dict) else None
        except Exception:  # noqa: BLE001 —— 日志上下文提取失败不影响调用
            thread_id = None
        _last_usage.set(None)  # 调用前置空，防止失败路径读到陈旧记录
        # 自有渠道前置注入：用户配置的 openai 兼容渠道优先于平台默认链，
        # 全部自有渠道失败仍降级到平台默认链（DeepSeek → Agnes）；
        # 无自有渠道时不传 channels（保持原调用签名，兼容测试替身）
        user_channels = await get_user_llm_channel_tuples(_user_id_from_state(state_aware))
        if user_channels:
            content, meta = await chat_with_meta(
                messages, temperature=temperature, json_mode=json_mode,
                channels=user_channels + _default_channels(),
            )
        else:
            content, meta = await chat_with_meta(
                messages, temperature=temperature, json_mode=json_mode,
            )
        record = usage_record(
            agent_name,
            meta.get("channel"),
            meta.get("model"),
            estimate_tokens(content) + estimate_tokens(self._prompt_chars(messages)),
        )
        record["prompt_chars"] = self._prompt_chars(messages)
        _last_usage.set(record)
        logger.debug("AgentLLM.chat 完成 agent=%s thread=%s channel=%s model=%s",
                     agent_name, thread_id, record["channel"], record["model"])
        return content

    async def chat_stream(
        self,
        state_aware: Any,
        messages: List[Dict[str, str]],
        *,
        agent_name: str,
        json_mode: bool = False,
        temperature: float = 0.7,
    ) -> AsyncGenerator[str, None]:
        """流式对话补全（状态感知入口，内部走 chat_stream_with_meta 多渠道降级）。

        Args:
            state_aware: 调用方所在的图状态（AgentState）或 None；仅用于日志上下文。
            messages: OpenAI 格式消息列表（role/content）。
            agent_name: 智能体名（写入用量记录的 agent 字段）。
            json_mode: True 时启用 JSON 输出模式。
            temperature: 采样温度。

        Yields:
            str: 每次产出的文本增量片段（拼接后为完整回答）。

        Raises:
            openai.APIError: 首块前全部渠道失败抛最后一个异常；输出中途失败
                抛当前渠道异常（与 providers.llm.chat_stream 语义一致）。
        """
        _last_usage.set(None)  # 调用前置空，防止失败路径读到陈旧记录
        recorded = False
        prompt_chars = self._prompt_chars(messages)
        # 自有渠道前置注入：语义与非流式一致（自有失败降级平台默认链）；
        # 无自有渠道时不传 channels（保持原调用签名，兼容测试替身）
        user_channels = await get_user_llm_channel_tuples(_user_id_from_state(state_aware))
        if user_channels:
            stream_iter = chat_stream_with_meta(
                messages, temperature=temperature, json_mode=json_mode,
                channels=user_channels + _default_channels(),
            )
        else:
            stream_iter = chat_stream_with_meta(
                messages, temperature=temperature, json_mode=json_mode,
            )
        async for piece, meta in stream_iter:
            # 仅首块携带渠道元信息，此时记录用量（与渠道降级语义对齐）
            if not recorded and meta:
                record = usage_record(
                    agent_name,
                    meta.get("channel"),
                    meta.get("model"),
                    estimate_tokens(piece),
                )
                record["prompt_chars"] = prompt_chars
                _last_usage.set(record)
                recorded = True
            yield piece

    def attach_usage(self, artifacts: dict) -> dict:
        """把最近一次 LLM 调用的用量记录追加到产出物字典（MA-19/20 观测位）。

        Args:
            artifacts: 节点产出物字典（将原样并入 artifacts["_usage"]）。

        Returns:
            dict: 传入的同一字典（就地追加 _usage 键）；当前上下文无用量记录
            （调用失败/未发生调用）时不追加。

        Raises:
            不抛出异常：artifacts 非字典时按空字典兜底返回。
        """
        target = artifacts if isinstance(artifacts, dict) else {}
        usage = get_last_usage()
        if usage:
            target["_usage"] = dict(usage)
        return target


# 模块级单例：全部智能体节点共用
llm = AgentLLM()
