"""智能体子图模块（文件级说明）。

对应里程碑 MA-09（Subgraph 局部编排复用）：把审核与一致性分析两个「多步
局部流程」下沉为可独立编译、独立测试的子图，主图的 review/consistency 节点
仅做输入提取与产出格式化，核心判定逻辑全部由子图承担。

- build_review_subgraph()：内容审核子图——
  节点：machine_review（词表机审，一票否决）→ llm_review（LLM 辅助复核）；
  路由：机审命中违禁词 → 直接输出 violate（跳过 LLM）；未命中 → LLM 复核。
- build_consistency_subgraph()：资产一致性子图——
  节点：collect_features（特征采集/输入规整）→ evaluate_similarity（相似度评估）。

两个子图均不带 checkpointer 编译（局部流程无跨轮持久化需求），并以
模块级单例 + run_review()/run_consistency() 门面函数暴露：
- run_review(text) -> dict：{"result","reason","banned","degraded"}
- run_consistency(basis) -> dict：{"analysis","degraded"}

artifacts 输出结构与改造前的 review_agent/consistency_agent 完全兼容
（LLM 不可用时保持原降级语义：机审命中仍判 violate，否则 suspect 转人工）。
"""

import json
import logging
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.llm_adapter import llm

logger = logging.getLogger(__name__)

# 机审违禁词表（与 services.review_service 保持一致的任务规定词表）
BANNED_WORDS = ["枪击", "血腥", "赌博"]

# LLM 辅助审核系统提示词：强制 JSON 输出三级结论
_REVIEW_LLM_PROMPT = (
    "你是漫序 MangaFlow 的内容安全审核员。请判断以下文本是否存在违反平台内容规范的"
    "风险（暴力/色情/赌博/违法等）。仅输出 JSON（不要输出其他内容）：\n"
    '{"result":"pass|suspect|violate","reason":"判定理由（中文，简要）"}\n'
    "判定标准：pass=无明显风险；suspect=存在擦边或需要人工复核的内容；violate=明确违规。"
)

# 一致性分析系统提示词：强制 JSON 输出三部分结论
_CONSISTENCY_LLM_PROMPT = (
    "你是漫序 MangaFlow 的资产一致性审查员，负责判断两个视觉资产（角色/场景）"
    "在多镜头中能否保持形象统一。请使用中文，仅输出 JSON（不要输出其他内容）：\n"
    '{"similarities":"两者相同的特征（外形/服装/色调等）",'
    '"differences":"两者存在冲突或漂移的差异点",'
    '"suggestions":"为保持一致性给出的可执行建议"}'
)


# ---------------------------------------------------------------------------
# 审核子图（MA-09）
# ---------------------------------------------------------------------------
class ReviewSubState(TypedDict, total=False):
    """内容审核子图内部状态。

    Attributes:
        text: 待审文本。
        hits: 机审命中的违禁词列表。
        verdict: 审核结论 {"result","reason"}。
        degraded: 是否走了 LLM 降级路径。
        usage: 最近一次 LLM 调用用量记录（contextvar 不跨任务回传，经状态带出）。
    """

    text: str
    hits: List[str]
    verdict: Dict[str, Any]
    degraded: bool
    usage: Optional[Dict[str, Any]]


def _banned_scan(text: str) -> List[str]:
    """词表机审：扫描待审文本中的违禁词。

    Args:
        text: 待审文本。

    Returns:
        List[str]: 命中的违禁词列表（未命中返回空列表）。

    Raises:
        不抛出异常：text 非字符串时按空串处理。
    """
    text = text if isinstance(text, str) else ""
    return [word for word in BANNED_WORDS if word in text]


async def review_machine_node(state: ReviewSubState) -> dict:
    """审核子图节点 1：词表机审（一票否决，命中即 violate）。

    Args:
        state: 子图内部状态（读 text）。

    Returns:
        dict: {"hits": 命中词列表, "verdict": 命中时的 violate 结论或空}。

    Raises:
        不抛出异常：纯本地词表扫描。
    """
    hits = _banned_scan(state.get("text") or "")
    if hits:
        return {
            "hits": hits,
            "verdict": {"result": "violate", "reason": f"机审命中违禁词：{'、'.join(hits)}"},
        }
    return {"hits": [], "verdict": {}}


def _route_after_machine(state: ReviewSubState) -> str:
    """审核子图条件路由：机审命中直接结束，否则进入 LLM 复核。

    Args:
        state: 子图内部状态。

    Returns:
        str: "__end__"（已出结论）或 "llm_review"。

    Raises:
        不抛出异常。
    """
    return "__end__" if state.get("hits") else "llm_review"


async def review_llm_node(state: ReviewSubState) -> dict:
    """审核子图节点 2：LLM 辅助复核（不可用时降级 suspect 转人工）。

    Args:
        state: 子图内部状态（读 text）。

    Returns:
        dict: {"verdict": {"result","reason"}, "degraded": 是否降级,
        "usage": 用量记录（成功时）}。

    Raises:
        不抛出异常：LLM/解析异常捕获后输出固定降级结论。
    """
    text = state.get("text") or ""
    try:
        raw = await llm.chat(
            None,
            [
                {"role": "system", "content": _REVIEW_LLM_PROMPT},
                {"role": "user", "content": f"待审文本：{text}"},
            ],
            agent_name="review_llm",
            temperature=0,
            json_mode=True,
        )
        data = json.loads(raw) if isinstance(raw, str) else {}
        result = str(data.get("result", "")).strip().lower()
        if result not in {"pass", "suspect", "violate"}:
            result = "suspect"
        usage = get_last_usage()
        return {"verdict": {"result": result, "reason": str(data.get("reason", "模型未给出理由"))},
                "degraded": False,
                "usage": dict(usage) if usage else None}
    except Exception as exc:  # noqa: BLE001 —— LLM 异常降级为固定结论
        logger.warning("审核子图 LLM 复核失败，降级转人工：%s", exc)
        return {
            "verdict": {"result": "suspect", "reason": "LLM 辅助审核不可用，已转人工复审建议"},
            "degraded": True,
            "usage": None,
        }


def build_review_subgraph():
    """构建内容审核子图（词表机审 → 条件路由 → LLM 复核）。

    Returns:
        CompiledStateGraph: 不带 checkpointer 的编译子图
        （START → machine_review → (命中→END | 未命中→llm_review) → END）。

    Raises:
        Exception: 节点/边定义非法时由 LangGraph 抛出（属开发期错误）。
    """
    builder = StateGraph(ReviewSubState)
    builder.add_node("machine_review", review_machine_node)
    builder.add_node("llm_review", review_llm_node)
    builder.add_edge(START, "machine_review")
    builder.add_conditional_edges("machine_review", _route_after_machine, ["llm_review", END])
    builder.add_edge("llm_review", END)
    return builder.compile()


async def run_review(text: str) -> Dict[str, Any]:
    """执行审核子图（门面函数）：返回与原 review_node 产出兼容的结构。

    Args:
        text: 待审文本。

    Returns:
        Dict[str, Any]: {"result": "pass|suspect|violate", "reason": str,
        "banned": List[str], "degraded": bool, "usage": Optional[dict]}。

    Raises:
        不抛出异常：子图执行异常时输出 suspect 降级结论。
    """
    subgraph = get_review_subgraph()
    try:
        out = await subgraph.ainvoke({"text": text})
        verdict = out.get("verdict") or {}
        return {
            "result": str(verdict.get("result") or "suspect"),
            "reason": str(verdict.get("reason") or ""),
            "banned": list(out.get("hits") or []),
            "degraded": bool(out.get("degraded")),
            "usage": out.get("usage"),
        }
    except Exception as exc:  # noqa: BLE001 —— 子图异常兜底为 suspect
        logger.warning("审核子图执行异常，降级输出 suspect：%s", exc)
        return {"result": "suspect", "reason": "审核子图执行异常，已转人工复审建议",
                "banned": [], "degraded": True, "usage": None}


# ---------------------------------------------------------------------------
# 一致性子图（MA-09）
# ---------------------------------------------------------------------------
class ConsistencySubState(TypedDict, total=False):
    """资产一致性子图内部状态。

    Attributes:
        basis: 拼接后的「资产 A / 资产 B」比对输入文本。
        analysis: 结构化分析结论 {"similarities","differences","suggestions"}。
        degraded: 是否走了 LLM 降级路径。
        usage: 最近一次 LLM 调用用量记录（contextvar 不跨任务回传，经状态带出）。
    """

    basis: str
    analysis: Dict[str, Any]
    degraded: bool
    usage: Optional[Dict[str, Any]]


async def consistency_collect_node(state: ConsistencySubState) -> dict:
    """一致性子图节点 1：特征采集——规整比对输入（去空白、兜底占位）。

    Args:
        state: 子图内部状态（读 basis）。

    Returns:
        dict: {"basis": 规整后的比对输入文本}。

    Raises:
        不抛出异常：空输入以占位文本兜底。
    """
    basis = str(state.get("basis") or "").strip()
    if not basis:
        basis = "资产 A：（未提供）；资产 B：（未提供）"
    return {"basis": basis}


async def consistency_similarity_node(state: ConsistencySubState) -> dict:
    """一致性子图节点 2：相似度评估（LLM 比对，不可用时固定模板降级）。

    Args:
        state: 子图内部状态（读 basis）。

    Returns:
        dict: {"analysis": {"similarities","differences","suggestions"},
        "degraded": 是否降级}。

    Raises:
        不抛出异常：LLM/解析异常捕获后输出固定分析模板。
    """
    basis = state.get("basis") or ""
    try:
        raw = await llm.chat(
            None,
            [
                {"role": "system", "content": _CONSISTENCY_LLM_PROMPT},
                {"role": "user", "content": basis},
            ],
            agent_name="consistency_llm",
            temperature=0.3,
            json_mode=True,
        )
        data = json.loads(raw) if isinstance(raw, str) else {}
        data = data if isinstance(data, dict) else {}
        usage = get_last_usage()
        return {"analysis": data, "degraded": False, "usage": dict(usage) if usage else None}
    except Exception as exc:  # noqa: BLE001 —— LLM 异常必须降级为固定模板
        logger.warning("一致性子图 LLM 评估失败，降级固定模板：%s", exc)
        fallback = {
            "similarities": "主体身份、核心服饰与主色调保持一致。",
            "differences": "发型细节、光照方向与背景元素可能存在镜头间漂移。",
            "suggestions": "锁定同一段角色视觉提示词与 seed，统一画风关键词，逐镜头比对参考帧。",
        }
        return {"analysis": fallback, "degraded": True, "usage": None}


def build_consistency_subgraph():
    """构建资产一致性子图（特征采集 → 相似度评估）。

    Returns:
        CompiledStateGraph: 不带 checkpointer 的编译子图
        （START → collect_features → evaluate_similarity → END）。

    Raises:
        Exception: 节点/边定义非法时由 LangGraph 抛出（属开发期错误）。
    """
    builder = StateGraph(ConsistencySubState)
    builder.add_node("collect_features", consistency_collect_node)
    builder.add_node("evaluate_similarity", consistency_similarity_node)
    builder.add_edge(START, "collect_features")
    builder.add_edge("collect_features", "evaluate_similarity")
    builder.add_edge("evaluate_similarity", END)
    return builder.compile()


async def run_consistency(basis: str) -> Dict[str, Any]:
    """执行一致性子图（门面函数）：返回与原 consistency_node 产出兼容的结构。

    Args:
        basis: 拼接好的「资产 A…；资产 B…」比对输入文本。

    Returns:
        Dict[str, Any]: {"analysis": {"similarities","differences","suggestions"},
        "degraded": bool, "usage": Optional[dict]}。

    Raises:
        不抛出异常：子图执行异常时输出固定降级分析。
    """
    subgraph = get_consistency_subgraph()
    try:
        out = await subgraph.ainvoke({"basis": basis})
        return {
            "analysis": out.get("analysis") or {},
            "degraded": bool(out.get("degraded")),
            "usage": out.get("usage"),
        }
    except Exception as exc:  # noqa: BLE001 —— 子图异常兜底
        logger.warning("一致性子图执行异常，降级固定分析：%s", exc)
        return {
            "analysis": {
                "similarities": "主体身份、核心服饰与主色调保持一致。",
                "differences": "细节与光照可能存在镜头间漂移。",
                "suggestions": "锁定视觉提示词与 seed，逐镜头比对参考帧。",
            },
            "degraded": True,
            "usage": None,
        }


# ---------------------------------------------------------------------------
# 模块级单例（懒编译，线程内复用）
# ---------------------------------------------------------------------------
_review_subgraph: Optional[Any] = None
_consistency_subgraph: Optional[Any] = None


def get_review_subgraph():
    """获取审核子图编译单例（首次调用时编译）。

    Returns:
        CompiledStateGraph: 审核子图（不带 checkpointer）。

    Raises:
        Exception: 子图构建非法时由 LangGraph 抛出（属开发期错误）。
    """
    global _review_subgraph
    if _review_subgraph is None:
        _review_subgraph = build_review_subgraph()
    return _review_subgraph


def get_consistency_subgraph():
    """获取一致性子图编译单例（首次调用时编译）。

    Returns:
        CompiledStateGraph: 一致性子图（不带 checkpointer）。

    Raises:
        Exception: 子图构建非法时由 LangGraph 抛出（属开发期错误）。
    """
    global _consistency_subgraph
    if _consistency_subgraph is None:
        _consistency_subgraph = build_consistency_subgraph()
    return _consistency_subgraph
