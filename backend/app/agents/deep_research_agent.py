"""长文深度解析智能体模块（文件级说明）。

对应里程碑 MA-17 / MA-18（Deep Agents 模式：规划-执行-反思循环 + 虚拟文件系统）。
把原单节点 research_node 升级为四节点子链路（由主图注册，research 节点即
规划入口）：

- research_plan_node（规划）：LLM 产出子任务 JSON 列表写入 state["research_plan"]，
  并经 Command(goto=[Send(...)]) 并行分发到 research_exec（旧分块摘要逻辑
  迁移为长文本降级路径）；
- research_exec_node（执行）：单子任务执行（Send 目标，并行），结果写入
  research_results 累加通道（虚拟文件系统按文件名归档）；
- research_reflect_node（反思）：评估完成度，未完成且轮次 < 2 时经 Command
  再 Send 补充任务（最多 2 轮防循环），否则转入汇总；
- research_finalize_node（汇总）：合并 VFS 笔记产出最终报告（兼容旧
  artifacts["research"] 的 outline/characters/worldview 结构），并把虚拟文件
  系统持久化到 langgraph_store（namespace = "vfs:{thread_id}"）。

VFS 语义：artifacts["vfs"] = {filename: content}；每份子任务笔记以独立
文件名落档，汇总报告另存 report.md。
"""

import json
import logging
from typing import Any, Dict, List

from langchain_core.messages import AIMessage
from langgraph.types import Command, Send

from app.agents.llm_adapter import llm
from app.agents.state import AgentState, last_human_text

logger = logging.getLogger(__name__)

# 长文降级路径的分块阈值与块大小（字）——与旧版 research_node 一致
_CHUNK_THRESHOLD = 6000
_CHUNK_SIZE = 6000

# 反思循环上限（防死循环：最多 2 轮补充任务）
_MAX_REFLECT_ROUNDS = 2
# 每轮反思最多补充的子任务数
_MAX_EXTRA_TASKS = 3

# 规划提示词：强制 JSON 输出子任务列表
_PLAN_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的深度调研规划师。请针对给定的长文解析需求，"
    "拆解为 1~5 个可独立执行的子任务（每个子任务聚焦一个维度）。"
    "仅输出 JSON（不要输出其他内容）：\n"
    '{"tasks":[{"title":"子任务名（如 大纲梳理/角色清单/世界观设定）",'
    '"question":"该子任务要回答的具体问题","filename":"笔记文件名（如 outline.md）"}]}\n'
    "要求：子任务覆盖 故事大纲、主要角色、世界观设定 三个核心维度，全部使用中文。"
)

# 执行提示词：单子任务笔记产出
_EXEC_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的调研执行员。请针对给定的子任务问题，基于提供的"
    "素材完成分析，以 Markdown 笔记形式输出要点（中文，300 字以内），"
    "保留关键事实与结构。"
)

# 反思提示词：评估完成度并列出缺失任务
_REFLECT_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的调研复盘员。请对照原始解析需求与已完成的笔记清单，"
    "判断是否已覆盖 大纲/角色/世界观 三个核心维度；若存在明显缺失，"
    "给出最多 2 个补充子任务。仅输出 JSON（不要输出其他内容）：\n"
    '{"complete": true|false, "missing_tasks":[{"title":"补充任务名",'
    '"question":"要回答的问题","filename":"extra.md"}]}'
)

# 汇总提示词：强制 JSON 输出三要素（兼容旧 artifacts.research 结构）
_MERGE_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的长文拆解分析师。请把多份调研笔记整合为结构化解析结论（中文），"
    "仅输出 JSON（不要输出其他内容）：\n"
    '{"outline":"故事大纲（起承转合，200 字以内）",'
    '"characters":"主要角色清单与设定（含关系）",'
    '"worldview":"世界观设定（时代/规则/核心冲突）"}'
)


def _split_chunks(text: str) -> List[str]:
    """把长文本按固定块长切分（长文降级路径用，内部函数）。

    Args:
        text: 原始长文本。

    Returns:
        List[str]: 依次排列的文本块（每块最长 _CHUNK_SIZE 字）。

    Raises:
        不抛出异常：text 非字符串时返回空列表。
    """
    text = text if isinstance(text, str) else ""
    return [text[i:i + _CHUNK_SIZE] for i in range(0, len(text), _CHUNK_SIZE)]


def _research_input(state: AgentState) -> str:
    """提取调研对象文本（内部函数）。

    Args:
        state: 当前图状态。

    Returns:
        str: 调研文本（artifacts["research_input"] > 最后一条人类消息）。

    Raises:
        不抛出异常。
    """
    return (state.get("artifacts") or {}).get("research_input") or last_human_text(state) or ""


def _common_fields(state: AgentState) -> Dict[str, Any]:
    """构造 Send 子任务公共字段（内部函数）。

    Args:
        state: 当前图状态。

    Returns:
        Dict[str, Any]: {thread_id, user_id, project_id}。

    Raises:
        不抛出异常。
    """
    return {
        "thread_id": state.get("thread_id"),
        "user_id": state.get("user_id"),
        "project_id": state.get("project_id"),
    }


def _normalize_tasks(raw_tasks: Any, prefix: str) -> List[Dict[str, Any]]:
    """把 LLM 产出的子任务列表规整为统一结构（内部函数）。

    Args:
        raw_tasks: LLM 返回的 tasks 数组（结构容错）。
        prefix: 文件名前缀（规划路用 "note"，补充路用 "extra{轮次}"）。

    Returns:
        List[Dict[str, Any]]: [{"title","question","filename"}]；非法条目丢弃，
        filename 冲突时自动加序号。

    Raises:
        不抛出异常：任何结构异常按空列表/占位符兜底。
    """
    tasks: List[Dict[str, Any]] = []
    seen_names = set()
    for idx, t in enumerate(raw_tasks if isinstance(raw_tasks, (list, tuple)) else []):
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or f"子任务{idx + 1}").strip()[:50]
        filename = str(t.get("filename") or f"{prefix}_{idx + 1}.md").strip()[:80]
        if not filename.endswith(".md"):
            filename += ".md"
        if filename in seen_names:
            filename = f"{prefix}_{idx + 1}_{filename}"
        seen_names.add(filename)
        tasks.append({
            "title": title,
            "question": str(t.get("question") or title).strip()[:500],
            "filename": filename,
        })
    return tasks


async def research_plan_node(state: AgentState) -> Command:
    """深度调研规划节点（MA-18 规划层，主图 research 意图入口）。

    LLM 产出子任务计划（失败降级：长文按旧分块逻辑拆任务，短文单任务），
    写入 state["research_plan"] 后经 Command(goto=[Send(...)]) 并行分发执行。

    Args:
        state: 当前图状态。

    Returns:
        Command: update={"research_plan", "research_round": 0}；
        goto=[Send("research_exec", {task...})...] 并行分发。

    Raises:
        不抛出异常：LLM/解析异常走长文分块 / 单任务降级路径。
    """
    text = _research_input(state)
    tasks: List[Dict[str, Any]] = []
    try:
        raw = await llm.chat(
            state,
            [
                {"role": "system", "content": _PLAN_SYSTEM_PROMPT},
                {"role": "user", "content": f"解析需求与素材：\n{text[:6000]}"},
            ],
            agent_name="research_plan",
            temperature=0.3,
            json_mode=True,
        )
        data = json.loads(raw) if isinstance(raw, str) else {}
        tasks = _normalize_tasks(data.get("tasks"), "note")
        logger.info("research_plan 产出 %d 个子任务", len(tasks))
    except Exception as exc:  # noqa: BLE001 —— 规划失败走确定性降级路径
        logger.warning("research_plan LLM 调用失败，降级分块任务：%s", exc)
    if not tasks:
        # 降级路径 1：长文按块拆任务（沿用旧 Map-Reduce 逻辑语义）
        if len(text) > _CHUNK_THRESHOLD:
            chunks = _split_chunks(text)
            tasks = [
                {"title": f"第 {i} 段摘要", "question": f"摘要第 {i} 段的要点（人物/关系/设定/情节）",
                 "filename": f"chunk_{i}.md"}
                for i, _ in enumerate(chunks, start=1)
            ]
        else:
            # 降级路径 2：短文单任务全文解析
            tasks = [{"title": "全文解析", "question": "解析全文的大纲、角色与世界观",
                      "filename": "fulltext.md"}]
    # 原文可能超长：分块后每块单独作为执行素材（存入子任务 question 之外，
    # 由 exec 按任务序号取块；此处把块文本挂在 task["chunk"] 上）
    if len(text) > _CHUNK_THRESHOLD and tasks and tasks[0].get("filename", "").startswith("chunk_"):
        for idx, task in enumerate(tasks):
            task["chunk"] = _split_chunks(text)[idx] if idx < len(_split_chunks(text)) else ""
    return Command(
        update={"research_plan": tasks, "research_round": 0},
        goto=[
            Send("research_exec", {"task": task, "index": idx, "round": 0,
                                   "text": text, **_common_fields(state)})
            for idx, task in enumerate(tasks)
        ],
    )


async def research_exec_node(payload: dict) -> dict:
    """深度调研执行节点（MA-18 执行层，Send 目标，并行执行）。

    注意：本节点的输入不是完整 AgentState，而是 Send 携带的子任务载荷
    （task/index/round/text/thread_id/user_id/project_id）。

    Args:
        payload: Send 分发的子任务载荷。

    Returns:
        dict: {"research_results": [单条笔记]}——笔记含 title/filename/content/
        ok 字段，构成虚拟文件系统的一个文件。

    Raises:
        不抛出异常：LLM 异常时以 ok=False + 降级占位笔记，保证汇总完整。
    """
    task = payload.get("task") or {}
    index = int(payload.get("index") or 0)
    title = str(task.get("title") or f"子任务{index + 1}")
    filename = str(task.get("filename") or f"note_{index + 1}.md")
    # 执行素材：长文分块任务用块文本，其余用全局素材
    material = str(task.get("chunk") or payload.get("text") or "")
    try:
        content = await llm.chat(
            None,
            [
                {"role": "system", "content": _EXEC_SYSTEM_PROMPT},
                {"role": "user", "content": f"子任务：{title}\n问题：{task.get('question', '')}\n素材：\n{material[:6000]}"},
            ],
            agent_name="research_exec",
            temperature=0.3,
        )
        ok = True
    except Exception as exc:  # noqa: BLE001 —— 单任务失败不阻断整体
        logger.warning("research_exec 子任务 %s 执行失败：%s", title, exc)
        content = f"（离线降级）子任务「{title}」暂无法完成，待服务恢复后重试。"
        ok = False
    return {"research_results": [{
        "title": title, "filename": filename, "content": content, "ok": ok,
        "round": int(payload.get("round") or 0),
    }]}


async def research_reflect_node(state: AgentState) -> Command:
    """深度调研反思节点（MA-18 反思层）：评估完成度，决定补充任务或汇总。

    未完成且 research_round < 2 时经 Command(goto=[Send(...)]) 补充派发
    （最多 2 轮防循环）；否则转入 research_finalize 汇总。

    Args:
        state: 当前图状态（读 research_results / research_round / 调研输入）。

    Returns:
        Command: 补充轮 update={"research_round": r+1, "research_plan": [...]} +
        goto=[Send("research_exec", ...)...]；或 goto="research_finalize"。

    Raises:
        不抛出异常：LLM 异常时视为已完成，直接汇总。
    """
    results = state.get("research_results") or []
    round_no = int(state.get("research_round") or 0)
    if round_no >= _MAX_REFLECT_ROUNDS:
        return Command(goto="research_finalize")
    done_titles = [str(r.get("title") or "") for r in results if isinstance(r, dict)]
    try:
        raw = await llm.chat(
            state,
            [
                {"role": "system", "content": _REFLECT_SYSTEM_PROMPT},
                {"role": "user", "content":
                    f"原始需求：{_research_input(state)[:2000]}\n已完成笔记：{'、'.join(done_titles) or '（无）'}"},
            ],
            agent_name="research_reflect",
            temperature=0.2,
            json_mode=True,
        )
        data = json.loads(raw) if isinstance(raw, str) else {}
        complete = bool(data.get("complete", True))
        missing = _normalize_tasks(data.get("missing_tasks"), f"extra{round_no + 1}")[:_MAX_EXTRA_TASKS]
    except Exception as exc:  # noqa: BLE001 —— 反思失败按已完成处理
        logger.warning("research_reflect LLM 调用失败，按已完成处理：%s", exc)
        complete, missing = True, []
    if not complete and missing:
        plan = list(state.get("research_plan") or []) + missing
        return Command(
            update={"research_round": round_no + 1, "research_plan": plan},
            goto=[
                Send("research_exec", {"task": t, "index": idx, "round": round_no + 1,
                                       "text": _research_input(state), **_common_fields(state)})
                for idx, t in enumerate(missing)
            ],
        )
    return Command(goto="research_finalize")


async def _persist_vfs(thread_id: Any, vfs: Dict[str, str]) -> None:
    """把虚拟文件系统持久化到 langgraph_store（MA-18 VFS 落档）。

    以 namespace="vfs:{thread_id}"、key="notes" upsert {"files": vfs}；
    数据库不可用时仅记录 warning，不影响主流程产出。

    Args:
        thread_id: 会话线程 ID（为空时跳过持久化）。
        vfs: 虚拟文件系统 {filename: content}。

    Returns:
        None。

    Raises:
        不抛出异常：写库异常捕获后降级告警。
    """
    if not thread_id or not vfs:
        return
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from app.core import database as db_mod  # 延迟绑定：兼容测试期 SessionLocal 重定向
        from app.models.agent import LanggraphStore

        payload = {"files": vfs}
        async with db_mod.SessionLocal() as session, session.begin():
            stmt = pg_insert(LanggraphStore).values(
                namespace=f"vfs:{thread_id}", key="notes", value=payload,
            ).on_conflict_do_update(
                index_elements=["namespace", "key"],
                set_={"value": payload},
            )
            await session.execute(stmt)
        logger.info("调研 VFS 已持久化（namespace=vfs:%s，%d 个文件）", thread_id, len(vfs))
    except Exception as exc:  # noqa: BLE001 —— 持久化失败不影响图流程
        logger.warning("调研 VFS 持久化失败（thread_id=%s）：%s", thread_id, exc)


async def research_finalize_node(state: AgentState) -> dict:
    """深度调研汇总节点（MA-18 汇总层）：合并 VFS 笔记产出最终报告。

    Args:
        state: 当前图状态（读 research_results / thread_id）。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(最终报告)],
        "artifacts": 合并后产出物（research 键兼容旧结构 outline/characters/
        worldview + notes；vfs 键为虚拟文件系统；成功路径附 _usage），
        "error": "" 或 "llm_unavailable", "research_results": None（重置累加
        通道，避免污染同线程下一轮调研）}。

    Raises:
        不抛出异常：LLM/解析异常降级为固定占位报告；VFS 持久化失败仅告警。
    """
    artifacts = dict(state.get("artifacts") or {})
    results = state.get("research_results") or []
    # 虚拟文件系统：每份笔记一个文件 + 汇总报告 report.md
    vfs: Dict[str, str] = {}
    for r in results:
        if isinstance(r, dict) and str(r.get("filename") or "").strip():
            vfs[str(r["filename"])] = str(r.get("content") or "")
    notes_text = "\n\n".join(f"## {fn}\n{ct}" for fn, ct in vfs.items())
    try:
        merged_raw = await llm.chat(
            state,
            [
                {"role": "system", "content": _MERGE_SYSTEM_PROMPT},
                {"role": "user", "content": f"调研笔记：\n{notes_text[:12000] or '（无笔记）'}"},
            ],
            agent_name="research_finalize",
            temperature=0.3,
            json_mode=True,
        )
        data = json.loads(merged_raw) if isinstance(merged_raw, str) else {}
        data = data if isinstance(data, dict) else {}
        error = ""
    except Exception as exc:  # noqa: BLE001 —— 汇总 LLM 异常必须降级
        logger.warning("research_finalize LLM 调用失败，降级固定模板：%s", exc)
        data = {}
        error = "llm_unavailable"
    result = {
        "outline": str(data.get("outline", "")) or "（待补充）",
        "characters": str(data.get("characters", "")) or "（待补充）",
        "worldview": str(data.get("worldview", "")) or "（待补充）",
        "notes": list(vfs.keys()),
    }
    report = (
        f"# 调研报告\n\n## 大纲\n{result['outline']}\n\n## 角色\n{result['characters']}\n\n"
        f"## 世界观\n{result['worldview']}\n"
    )
    vfs["report.md"] = report
    artifacts["research"] = result
    artifacts["vfs"] = vfs
    artifacts = llm.attach_usage(artifacts)
    # VFS 持久化到 langgraph_store（失败不阻断）
    await _persist_vfs(state.get("thread_id"), vfs)
    failed = sum(1 for r in results if isinstance(r, dict) and not r.get("ok"))
    content = (
        "📖 深度调研完成（" + f"{len(vfs) - 1} 份笔记" + "）：\n"
        f"【大纲】{result['outline']}\n"
        f"【角色】{result['characters']}\n"
        f"【世界观】{result['worldview']}"
        + (f"\n⚠️ {failed} 个子任务执行失败，结果可能不完整。" if failed else "")
    )
    return {
        "messages": [AIMessage(content=content)],
        "artifacts": artifacts,
        "error": error,
        # None 哨兵：经累加合并器重置通道，避免污染同线程下一轮调研
        "research_results": None,
    }
