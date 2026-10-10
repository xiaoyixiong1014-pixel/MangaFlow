"""关键帧生成智能体模块（文件级说明）。

职责（MA-08 HITL + MA-10 Send 并行）：
- generation_node：关键帧生成入口节点。
  - 非批量模式（现状保留）：基于分镜/角色产出生成「风格 + 主体 + 动作 + 环境 +
    构图」五要素关键帧提示词，并写入 artifacts["cost_estimate"] 供后继 cost
    节点播报，体现「生成 → 成本校验」链路；
  - 批量模式（artifacts["batch_items"] 非空，或用户消息含「批量/全部」意图）：
    先经 MA-08 interrupt（confirm_generation）等待用户确认，拒绝则标记
    artifacts["aborted"]=True 并直达 END（真实退款由 tasks.runner 路径负责，
    Agent 侧仅中止流程）；确认后经 Command(goto=[Send(...)]) 把每个待生成项
    并行分发到 generation_item 节点（MA-10 Map 分发，LangGraph 原生 Send）。
- generation_item：单条生成子任务节点（Send 目标），逐条调用 LLM，结果写入
  带累加合并器的 generation_results 通道（并行写入互不覆盖）。
- generation_reduce：汇总节点，把 generation_results 合入 artifacts[
  "keyframe_prompt"]（items 列表结构）并重写 cost_estimate，随后进入 cost 节点。
"""

import json
import logging
import uuid
from typing import Any, Dict, List

from langchain_core.messages import AIMessage
from langgraph.types import Command, Send, interrupt

from app.agents.cost_agent import estimate_cost
from app.agents.llm_adapter import llm
from app.agents.state import AgentState, last_human_text

logger = logging.getLogger(__name__)

# 批量意图触发词（MA-10：last_human_text 命中即视为批量/全量生成请求）
_BATCH_KEYWORDS = ("批量", "全部", "所有镜头")

# 关键帧提示词系统提示词：强制 JSON 输出五要素
_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的关键帧美术指导，负责产出可直接用于图像模型的关键帧提示词。"
    "请使用中文，按「风格 + 主体 + 动作 + 环境 + 构图」五要素生成，仅输出 JSON（不要输出其他内容）：\n"
    '{"style":"画风与光影基调","subject":"画面主体（人物/物件及其特征）",'
    '"action":"主体动作/姿态","environment":"环境场景与氛围","composition":"构图与镜头语言"}'
)


def _format_five_parts(data: dict) -> str:
    """把五要素 JSON 拼装为一行式提示词文本。

    Args:
        data: 含 style/subject/action/environment/composition 键的字典。

    Returns:
        str: 「风格：…｜主体：…｜动作：…｜环境：…｜构图：…」格式文本。

    Raises:
        不抛出异常：data 非字典或键缺失时以占位符兜底。
    """
    data = data if isinstance(data, dict) else {}
    return (
        f"风格：{data.get('style', '待定')}｜主体：{data.get('subject', '待定')}｜"
        f"动作：{data.get('action', '待定')}｜环境：{data.get('environment', '待定')}｜"
        f"构图：{data.get('composition', '待定')}"
    )


def _extract_batch_items(state: AgentState) -> List[str]:
    """提取批量生成的逐项输入（内部函数）。

    优先级：
    1. artifacts["batch_items"]（上游显式给定的批量条目）；
    2. artifacts["storyboard"]["shots"] 逐镜头 visual（每个镜头 1 张关键帧）；
    3. 兜底：以既有生成依据文本作为唯一条目。

    Args:
        state: 当前图状态。

    Returns:
        List[str]: 批量条目文本列表（至少 1 项）。

    Raises:
        不抛出异常：结构异常时回退单条目。
    """
    artifacts = state.get("artifacts") or {}
    # 1) 显式批量条目
    items_raw = artifacts.get("batch_items")
    if isinstance(items_raw, (list, tuple)) and items_raw:
        items = [str(x) for x in items_raw if str(x or "").strip()]
        if items:
            return items
    # 2) 分镜镜头展开
    sb = artifacts.get("storyboard") or {}
    shots = sb.get("shots") if isinstance(sb, dict) else None
    if isinstance(shots, (list, tuple)) and shots:
        items = []
        for s in shots:
            if isinstance(s, dict) and str(s.get("visual") or "").strip():
                items.append(str(s["visual"]))
        if items:
            return items
    # 3) 兜底单条目（沿用既有生成依据）
    basis = (
        (sb.get("text") if isinstance(sb, dict) else None)
        or (artifacts.get("asset_prompt") or {}).get("text")
        or last_human_text(state)
        or "校园天台黄昏，主角回眸的悬念关键帧"
    )
    return [str(basis)]


def _generation_basis(state: AgentState) -> str:
    """提取非批量模式下的生成依据文本（内部函数，与原逻辑一致）。

    Args:
        state: 当前图状态。

    Returns:
        str: 生成依据（storyboard > asset_prompt > 用户消息 > 默认需求）。

    Raises:
        不抛出异常。
    """
    artifacts = state.get("artifacts") or {}
    sb = artifacts.get("storyboard") or {}
    return (
        (sb.get("text") if isinstance(sb, dict) else None)
        or (artifacts.get("asset_prompt") or {}).get("text")
        or last_human_text(state)
        or "校园天台黄昏，主角回眸的悬念关键帧"
    )


def _is_batch_request(state: AgentState) -> bool:
    """判定是否为批量/全量生成请求（内部函数）。

    Args:
        state: 当前图状态。

    Returns:
        bool: artifacts["batch_items"] 非空，或用户消息命中批量关键词时为 True。

    Raises:
        不抛出异常。
    """
    artifacts = state.get("artifacts") or {}
    if isinstance(artifacts.get("batch_items"), (list, tuple)) and artifacts.get("batch_items"):
        return True
    text = last_human_text(state) or ""
    return any(word in text for word in _BATCH_KEYWORDS)


def _common_fields(state: AgentState) -> Dict[str, Any]:
    """构造 Send 子任务公共字段（内部函数）。

    Args:
        state: 当前图状态。

    Returns:
        Dict[str, Any]: {thread_id, user_id, project_id}（供子任务上下文透传）。

    Raises:
        不抛出异常。
    """
    return {
        "thread_id": state.get("thread_id"),
        "user_id": state.get("user_id"),
        "project_id": state.get("project_id"),
    }


async def generation_node(state: AgentState) -> Any:
    """关键帧生成入口节点：非批量单帧生成 / 批量确认后 Send 并行分发。

    Args:
        state: 当前图状态。

    Returns:
        Any: 三种形态——
        1. 非批量：dict 部分状态更新（keyframe_prompt / cost_estimate，
           与改造前行为一致）；
        2. 批量被拒绝：dict 更新（artifacts["aborted"]=True、refund_pending
           退款标记），由条件边直达 END；
        3. 批量已确认：Command(update={"batch_dispatch": ...},
           goto=[Send("generation_item", ...)...]) 并行分发（LangGraph 原生
           Send 机制，各子任务并行执行）。

    Raises:
        Exception: checkpointer 缺失时 interrupt 由 LangGraph 抛出
            （生产路径 get_compiled_graph 恒带检查点，不会触发）；
        不抛出异常：LLM 调用失败走离线降级模板。
    """
    artifacts = dict(state.get("artifacts") or {})
    if _is_batch_request(state):
        items = _extract_batch_items(state)
        frame_count = len(items)
        points = estimate_cost("image", {"count": frame_count})
        # MA-08 HITL 确认点：全量生成前等待用户确认（拒绝 → 中止 + 退款标记）
        decision = interrupt({
            "type": "confirm_generation",
            "payload": {"count": frame_count, "cost_points": points},
        })
        approved = bool(isinstance(decision, dict) and decision.get("approved"))
        if not approved:
            artifacts.pop("aborted", None)
            artifacts["aborted"] = True
            # 退款标记：Agent 侧仅中止流程，真实退款由现有 runner 路径负责
            artifacts["refund_pending"] = True
            return {
                "messages": [AIMessage(
                    content=f"❌ 已取消批量生成（{frame_count} 张，约 {points} 积分），未执行扣费。"
                )],
                "artifacts": artifacts,
                "error": "",
            }
        # MA-10 并行分发：每个条目一个 Send 子任务；dispatch_id 供汇总节点
        # 区分批次（同线程多轮生成时只聚合本次派发的结果）
        dispatch_id = uuid.uuid4().hex[:12]
        return Command(
            update={"batch_dispatch": dispatch_id},
            goto=[
                Send("generation_item", {
                    "item": item, "index": idx, "dispatch_id": dispatch_id,
                    **_common_fields(state),
                })
                for idx, item in enumerate(items)
            ],
        )
    # ---------- 非批量路径（保持原行为） ----------
    basis = _generation_basis(state)
    sb = artifacts.get("storyboard") or {}
    try:
        shot_count = len((sb or {}).get("shots") or []) if isinstance(sb, dict) else 0
    except Exception:  # noqa: BLE001 —— 结构异常按缺省张数
        shot_count = 0
    frame_count = shot_count or 3
    try:
        raw = await llm.chat(
            state,
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"请为以下内容生成关键帧提示词（共 {frame_count} 个镜头各 1 张的统一风格基调）：{basis}"},
            ],
            agent_name="generation",
            temperature=0.7,
            json_mode=True,
        )
        data = json.loads(raw)
        content = _format_five_parts(data)
        artifacts["keyframe_prompt"] = {"parts": data, "text": content, "count": frame_count}
        artifacts = llm.attach_usage(artifacts)
        error = ""
    except Exception as exc:  # noqa: BLE001 —— LLM/解析异常必须降级
        logger.warning("generation_node LLM 调用失败，降级输出离线模板：%s", exc)
        content = (
            "（离线降级）当前无法连接生成模型，以下为预置关键帧提示词模板：\n"
            "风格：日系新海诚风，黄昏暖色逆光｜主体：十七岁黑发少年，校服微卷袖口｜"
            "动作：立于天台栏杆前回眸｜环境：空旷校园天台，远处操场剪影｜构图：三分法，留白天空压住悬念。"
        )
        artifacts["keyframe_prompt"] = {"text": content, "count": frame_count}
        error = "llm_unavailable"
    # 直接调用 cost_agent 的估算纯函数（成本校验链路：估算结果交给后继 cost 节点播报）
    params = {"count": frame_count}
    artifacts["cost_estimate"] = {
        "task_type": "image",
        "params": params,
        "points": estimate_cost("image", params),
    }
    return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": error}


async def generation_item(payload: dict) -> dict:
    """单条生成子任务节点（MA-10 Send 目标，并行执行）。

    注意：本节点的输入不是完整 AgentState，而是 Send 携带的逐项 payload
    （item/index/dispatch_id/thread_id/user_id/project_id）。

    Args:
        payload: Send 分发的子任务载荷（item 为该条生成依据文本）。

    Returns:
        dict: {"generation_results": [单条结果]}；结果含 index/dispatch_id/
        parts/text/ok 字段，经累加合并器并行写入互不覆盖。

    Raises:
        不抛出异常：LLM/解析异常时以 ok=False + 降级文本占位，保证汇总完整。
    """
    item = str(payload.get("item") or "")
    index = int(payload.get("index") or 0)
    dispatch_id = str(payload.get("dispatch_id") or "")
    try:
        raw = await llm.chat(
            None,
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"请为以下内容生成关键帧提示词（第 {index + 1} 张）：{item}"},
            ],
            agent_name="generation_item",
            temperature=0.7,
            json_mode=True,
        )
        data = json.loads(raw)
        content = _format_five_parts(data)
        result = {"index": index, "dispatch_id": dispatch_id, "basis": item,
                  "parts": data, "text": content, "ok": True}
    except Exception as exc:  # noqa: BLE001 —— 单条失败不阻断整批
        logger.warning("generation_item 第 %d 条生成失败，降级占位：%s", index, exc)
        result = {"index": index, "dispatch_id": dispatch_id, "basis": item,
                  "parts": {}, "text": f"（第 {index + 1} 张生成失败：模型暂不可用）", "ok": False}
    return {"generation_results": [result]}


async def generation_reduce(state: AgentState) -> dict:
    """批量生成汇总节点：合并并行子任务结果并重写成本估算参数。

    Args:
        state: 当前图状态（generation_results 为累加合并后的全量结果）。

    Returns:
        dict: 部分状态更新 {"artifacts": 合并后产出物（keyframe_prompt.items /
        cost_estimate）, "messages": [AIMessage(汇总播报)], "error": ""（全部
        成功）或 "llm_unavailable"（存在失败子任务）, "generation_results": None
        （重置累加通道，避免污染同线程下一轮生成）}。

    Raises:
        不抛出异常：结果结构异常时按空列表兜底。
    """
    artifacts = dict(state.get("artifacts") or {})
    dispatch_id = str(state.get("batch_dispatch") or "")
    results = state.get("generation_results") or []
    # 仅聚合本次派发的结果（同线程历史轮次结果已被 reduce 重置，此处再按
    # dispatch_id 双保险过滤）
    if dispatch_id:
        results = [r for r in results if isinstance(r, dict) and r.get("dispatch_id") == dispatch_id]
    items: List[Dict[str, Any]] = [
        {
            "index": r.get("index"),
            "text": str(r.get("text") or ""),
            "parts": r.get("parts") or {},
            "ok": bool(r.get("ok")),
        }
        for r in results
        if isinstance(r, dict)
    ]
    items.sort(key=lambda x: int(x.get("index") or 0))
    ok_count = sum(1 for it in items if it["ok"])
    combined_text = "\n".join(f"第 {it['index'] + 1} 张：{it['text']}" for it in items)
    artifacts["keyframe_prompt"] = {"items": items, "text": combined_text, "count": len(items)}
    params = {"count": max(len(items), 1)}
    artifacts["cost_estimate"] = {
        "task_type": "image",
        "params": params,
        "points": estimate_cost("image", params),
    }
    artifacts.pop("batch_dispatch", None)
    content = (
        f"🎞️ 批量生成完成：共 {len(items)} 张关键帧提示词，成功 {ok_count} 张、"
        f"失败 {len(items) - ok_count} 张；预计消耗 {artifacts['cost_estimate']['points']} 积分。"
    )
    return {
        "messages": [AIMessage(content=content)],
        "artifacts": artifacts,
        "error": "" if ok_count == len(items) and items else "llm_unavailable",
        # None 哨兵：经累加合并器重置通道，避免污染同线程下一轮批量生成
        "generation_results": None,
    }
