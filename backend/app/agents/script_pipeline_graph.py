"""剧本创作流水线 LangGraph 子图模块（文件级说明，P-1 范式级重构 Task 6 / P-5）。

职责（与 agents/script_pipeline.py 的阶段 Agent 集构成「编排层 / 算子层」分工，
算子层细节见该模块 docstring，此处只做子图编排）：
- build_script_pipeline_subgraph(): 编译剧本四阶段「整跑 + 断点续跑」子图
  （START → sp_input → sp_character_planner → sp_episode_planner →
  sp_body_dispatch --Send--> sp_episode_writer → sp_body_reduce → END），
  支持 auto_confirm=True 一键串跑（跳过三处人工确认闸口）；
- get_compiled_script_pipeline(): 带检查点的编译子图模块级单例（复用
  app.agents.graph.get_checkpointer 的三级降级策略，与主图共享同一
  checkpointer 实例；惰性初始化，导入期不建任何 DB 连接）。

人工确认闸口与退回语义（三处默认 interrupt，RD-04：每阶段人工确认）：
- confirm_characters（阶段 characters）→ confirm_episodes（阶段 outline）
  → confirm_body（阶段 body）；
- resume 决策形态兼容 {"decision": "approved"|"rejected", "reason"?, 编辑后清单?}
  与 {"approved": bool} 与 True（详见 _decision_approved）；
- 确认通过：落库（confirm_character_entries / save_episode_plan / scripts
  upsert）并推进阶段（advance_phase）；若 resume 携带编辑后清单
  （characters / episodes）则以编辑稿落库（人工修稿优先）；
- 退回（rejected）：条件边回退到上游生成节点，并在 project.config 的
  `_sp_pipeline` 标记中写入 regen=退回意见；回退后的再次生成以退回意见
  作为 instruction 重新调 LLM，避免「用户退回后内容原样重放」。

与 REST 分步接口（app/api/script_pipeline.py，Task 3/4/5）的关系（共存说明）：
- REST 三阶段接口是「分步驱动」路径：前端 Studio 四步页面逐阶段显式调用，
  每阶段独立生成/编辑/确认；
- 本子图是「整跑 + 断点续跑」路径：Agent 会话（api/agent_chat.py 的
  script_pipeline 意图）一次发起后按 角色→大纲→正文 顺序自动推进，三个
  interrupt（confirm_characters/confirm_episodes/confirm_body）作为人工
  确认闸口，断线后可凭 thread_id 经 GET /script/pipeline/state（或
  GET /script/pipeline/status）回放断点；
- 两者共享同一数据落地（project.config 候选 / episodes 行 / scripts 行 /
  阶段状态机与里程碑快照），落库逻辑统一收敛在
  app/services/script_pipeline_service.py；前端（Task 11）按场景选择。

interrupt 恢复语义（重要）：LangGraph 恢复时被中断节点从头重跑，interrupt()
之前的副作用会重复执行。本模块以 project.config 的 `_sp_pipeline` 进度标记
（{"thread_id", "stage", "regen"?}）实现幂等：恢复重跑时标记命中（同 thread_id
同阶段）即复用已落库产物（config 候选 / episodes 行 / scripts 行），不再重复
调 LLM、不重复落库，避免「用户确认内容与最终产出不一致」；标记中的 regen
字段承载「用户退回意见」，仅被消费一次（生成后即重写为无 regen 的标记），
因此回退重生不会在后续 resume 重放时反复触发。

State 设计：各 key 默认 LangGraph 覆盖语义；唯一例外 bodies 采用
Annotated[list, _merge_bodies] 累加合并器——Send 并行的 sp_episode_writer
叶子在同一超步各自写入，互不覆盖（同 generation_results 的 merge 约定）；
写入 None 时重置为空列表，供 sp_body_dispatch 在「正文退回重生」时清空
上一轮残留的 bodies。
"""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Annotated, Any, Dict, List, Optional

from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, Send, interrupt
from sqlalchemy import select, update
from typing_extensions import TypedDict

from app.agents.script_pipeline import (
    plan_characters,
    plan_episodes,
    plan_episode_scenes,
    stream_episode_body,
)
from app.models import Character, Episode, Project, Script
from app.schemas.script import validate_script_body_content
from app.services.script_pipeline_service import (
    CANDIDATES_KEY,
    advance_phase,
    confirm_character_entries,
    list_project_episodes,
    save_character_candidates,
    save_episode_plan,
    write_stage_snapshot,
)
from app.services.script_service import (
    build_extract_source_text,
    content_has_payload,
    content_plain,
    wrap_content_fields,
)

logger = logging.getLogger(__name__)

# 子图节点名（编译注册与 Send 分发目标一致，避免魔法串散落）
NODE_INPUT = "sp_input"
NODE_CHARACTER_PLANNER = "sp_character_planner"
NODE_EPISODE_PLANNER = "sp_episode_planner"
NODE_BODY_DISPATCH = "sp_body_dispatch"
NODE_EPISODE_WRITER = "sp_episode_writer"
NODE_BODY_REDUCE = "sp_body_reduce"

# 子图流水线进度标记在 project.config 中的存储键（interrupt 恢复幂等依据）
_SP_MARKER_KEY = "_sp_pipeline"


def _merge_bodies(current: Optional[List[Any]], update: Optional[List[Any]]) -> List[Any]:
    """正文结果通道合并器（Send 并行 append + None 重置）。

    语义：
    - 常规更新（update 为 list）：与既有列表拼接累加——Send 并行的
      sp_episode_writer 叶子在同一超步各自写入，互不覆盖；
    - None 哨兵更新：重置为空列表——sp_body_dispatch 在「正文退回重生」时
      清空上一轮残留 bodies，避免重生成结果与旧结果混杂。

    Args:
        current: 通道当前值（可能为 None）。
        update: 节点写入的新值（list 追加 / None 重置）。

    Returns:
        List[Any]: 合并后的新列表（总是返回新列表，保持通道不可变语义）。

    Raises:
        不抛出异常：update 非 list 且非 None 时按空列表兜底。
    """
    if update is None:
        return []
    if not isinstance(update, (list, tuple)):
        return list(current or [])
    return list(current or []) + list(update)


class ScriptPipelineState(TypedDict, total=False):
    """剧本子图内部状态（total=False：全部通道可缺省，节点按需读写）。

    Attributes:
        project_id: 项目 ID（子图唯一必填入口，Agent 会话/恢复 API 注入）。
        user_id: 发起用户 ID（REST 驱动入口写入，供 thread 归属双重校验与
            断线重连鉴权；Agent 会话路径可缺省）。
        auto_confirm: 一键串跑开关（True 时三处确认闸口自动放行，跳过
            interrupt，供「一键写完」场景与全流程串跑测试使用）。
        raw_text: 剧本输入源文本（sp_input 节点从 scripts 表读取后写入）。
        instruction: 可选补充指令（Agent 会话把用户消息透传至此）。
        confirmed_characters: 已确认角色轻量 dict 列表（sp_input 从 characters
            表读取 / 角色确认后回写；元素含 id/name/role_type/personality 等）。
        character_candidates: 本轮生成的候选角色列表（approved 后转正式）。
        episodes_plan: 已确认分集大纲列表（元素含 episode_id/sequence/title/
            logline/outline，sp_body_dispatch 据此 Send 逐集分发）。
        bodies: 逐集正文结果累加通道（Annotated _merge_bodies；元素
            {episode_id, sequence, title, raw_text, locations, scenes, ok}）。
        confirmed_flags: 三阶段确认标记 {"characters","episodes","body": bool}。
        retry_stage: 退回重生的目标阶段（characters/episodes/body；空串表示
            无回退，条件边据此回退到对应生成节点）。
        reject_reason: 最近一次人工退回的意见（作为重生时的补充指令传入
            算子层，并随消息通道播报给用户）。
        error: 错误标记；空串表示正常，非空表示流程被退回/中止的原因。
        messages: 播报消息流（add_messages；Agent 会话经 updates 流推送前端）。
    """

    project_id: int
    user_id: int
    auto_confirm: bool
    raw_text: str
    instruction: Optional[str]
    confirmed_characters: List[dict]
    character_candidates: List[dict]
    episodes_plan: List[dict]
    bodies: Annotated[List[Any], _merge_bodies]
    confirmed_flags: dict
    retry_stage: str
    reject_reason: str
    error: str
    messages: Annotated[List[AnyMessage], add_messages]


# ---------------------------------------------------------------------------
# 内部工具（会话获取 / thread_id 提取 / 决策解析 / 轻量角色转换）
# ---------------------------------------------------------------------------
def _db_session():
    """获取异步数据库会话工厂实例（运行时延迟导入 SessionLocal）。

    延迟到调用时导入（而非模块顶层）是为了与 conftest/测试环境的
    app.core.database.SessionLocal 重定向机制兼容（运行时属性查找总能取到
    当前生效的工厂）。

    Args:
        无参数。

    Returns:
        AsyncSession: 新建的独立数据库会话（调用方以 async with 管理生命周期）。

    Raises:
        Exception: 数据库连接失败时向上抛出（由节点统一捕获为 error 通道）。
    """
    from app.core.database import SessionLocal  # 运行时导入：兼容测试期工厂重定向

    return SessionLocal()


def _thread_id_of(config: Optional[RunnableConfig]) -> str:
    """从运行配置提取检查点线程 ID（内部函数）。

    Args:
        config: LangGraph 运行配置（含 configurable.thread_id；可能为 None）。

    Returns:
        str: 线程 ID；缺失时返回空串（此时进度标记幂等复用不生效）。

    Raises:
        不抛出异常：结构异常安全返回空串。
    """
    try:
        return str(((config or {}).get("configurable") or {}).get("thread_id") or "")
    except Exception:  # noqa: BLE001 —— 配置结构异常兜底
        return ""


def _marker_matches(marker: Any, stage: str, thread_id: str) -> bool:
    """判断进度标记是否命中（同线程 + 同阶段，内部函数）。

    Args:
        marker: project.config 中的 _sp_pipeline 标记（可能为 None/非 dict）。
        stage: 期望阶段（characters/episodes/body）。
        thread_id: 当前检查点线程 ID。

    Returns:
        bool: True 表示本轮流水线产物已在库中，恢复重跑可直接复用。

    Raises:
        不抛出异常：非 dict 标记视为未命中。
    """
    if not thread_id or not isinstance(marker, dict):
        return False
    return marker.get("stage") == stage and marker.get("thread_id") == thread_id


def _decision_approved(decision: Any) -> bool:
    """解析 interrupt 决策是否为「确认通过」（内部函数，多形态兼容）。

    兼容三种决策形态（与 resume 端点/测试的透传方式对应）：
    - True（Command(resume=True)）；
    - {"approved": True}（既有 POST /chat/resume 的 resume 值）；
    - {"decision": "approved"}（扩展 decision 字段透传）。

    Args:
        decision: interrupt() 返回的决策值（任意形态）。

    Returns:
        bool: True 表示用户确认通过。

    Raises:
        不抛出异常：未知形态一律按拒绝处理。
    """
    if decision is True:
        return True
    if isinstance(decision, dict):
        if decision.get("approved"):
            return True
        if str(decision.get("decision") or "").strip().lower() == "approved":
            return True
    return False


def _regen_reason(marker: Any, stage: str) -> Optional[str]:
    """提取进度标记中待消费的退回意见（内部函数）。

    标记中的 regen 字段由「人工退回」分支写入，仅被消费一次：生成节点命中
    该字段时重新调 LLM（退回意见非空则作为 instruction 传入），随后重写
    标记（去掉 regen），因此后续 resume 重放不会再触发重生。

    Args:
        marker: project.config 中的 _sp_pipeline 标记（可能为 None/非 dict）。
        stage: 期望阶段（characters/episodes/body）。

    Returns:
        Optional[str]: 本阶段的退回意见（可能为空串——用户退回但未填意见，
        此时仍需重生）；非本阶段标记或非退回标记时返回 None（表示可复用已
        落库产物）。

    Raises:
        不抛出异常：结构异常安全返回 None。
    """
    if not isinstance(marker, dict) or marker.get("stage") != stage:
        return None
    if "regen" not in marker:
        return None
    return str(marker.get("regen") or "").strip()


def _decision_reason(decision: Any) -> str:
    """解析 interrupt 决策中携带的退回意见（内部函数）。

    Args:
        decision: interrupt() 返回的决策值（任意形态）。

    Returns:
        str: 退回意见（reason 字段，或 feedback 字段兜底）；无则返回空串。

    Raises:
        不抛出异常：非 dict 决策返回空串。
    """
    if not isinstance(decision, dict):
        return ""
    return str(decision.get("reason") or decision.get("feedback") or "").strip()


def _decision_payload(decision: Any, key: str) -> List[dict]:
    """解析 interrupt 决策中携带的人工编辑清单（内部函数）。

    用户在确认卡片中直接编辑后提交时，resume 值形如
    {"decision": "approved", "characters": [...]} / {"episodes": [...]}；
    本函数按 key 提取其中的 dict 元素列表（其余形态视为未编辑）。

    Args:
        decision: interrupt() 返回的决策值（任意形态）。
        key: 清单字段名（characters/episodes）。

    Returns:
        List[dict]: 编辑后的清单条目；未携带编辑稿时返回空列表。

    Raises:
        不抛出异常：结构异常安全返回空列表。
    """
    if not isinstance(decision, dict):
        return []
    raw = decision.get(key)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _normalize_edited_plan(raw_items: List[dict]) -> List[dict]:
    """把人工编辑的分集大纲清单规整为落库结构（内部函数）。

    与算子层 plan_episodes 的规整口径一致（title 必填否则丢弃、字符串截长、
    duration_sec 数值化），保证 save_episode_plan 可安全消费前端编辑稿。

    Args:
        raw_items: 编辑稿条目列表（元素含 title/logline/core_event/
            characters/emotion_curve/duration_sec）。

    Returns:
        List[dict]: 规整后的分集大纲列表（无合法条目时为空列表，调用方
            应回退到已落库的 episodes 行）。

    Raises:
        不抛出异常：结构异常一律按缺数据处理。
    """
    items: List[dict] = []
    for item in raw_items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue  # title 必填否则丢弃（与算子层口径一致）
        try:
            duration = int(item.get("duration_sec"))
        except (TypeError, ValueError):
            duration = None
        items.append({
            "title": title[:100],
            "logline": str(item.get("logline") or "").strip()[:200],
            "core_event": str(item.get("core_event") or "").strip()[:2000],
            "characters": [
                str(c) for c in (item.get("characters") or []) if str(c or "").strip()
            ],
            "emotion_curve": str(item.get("emotion_curve") or "").strip()[:100],
            "duration_sec": duration,
        })
    return items


def _as_namespace_list(characters: Optional[List[dict]]) -> List[SimpleNamespace]:
    """把轻量角色 dict 列表转为属性对象列表（内部函数）。

    算子层 plan_episodes/stream_episode_body/plan_episode_scenes 经
    getattr(ch, "name") 等访问角色属性（面向 ORM 对象设计）；dict 缺属性时
    getattr 会静默返回默认值导致提示词退化，故统一包装为 SimpleNamespace。

    Args:
        characters: 轻量角色 dict 列表（可空）。

    Returns:
        List[SimpleNamespace]: 属性对象列表（输入为空时返回空列表）。

    Raises:
        不抛出异常：非 dict 元素跳过。
    """
    return [
        SimpleNamespace(**{k: v for k, v in ch.items() if isinstance(k, str) and k.isidentifier()})
        for ch in (characters or [])
        if isinstance(ch, dict)
    ]


def _confirmed_rows_to_dicts(rows: List[Character]) -> List[dict]:
    """把 characters 表 ORM 行转为轻量 dict 列表（内部函数）。

    Args:
        rows: 已确认角色 ORM 对象列表。

    Returns:
        List[dict]: [{id, name, role_type, personality, motivation, arc}, ...]
        （供 state 通道与 Send payload 传递，经 _as_namespace_list 供算子层消费）。

    Raises:
        无：纯属性读取。
    """
    return [
        {
            "id": row.id,
            "name": row.name,
            "role_type": row.role_type,
            "personality": row.personality,
            "motivation": row.motivation,
            "arc": row.arc,
        }
        for row in rows
    ]


def _message(text: str) -> AIMessage:
    """构造节点播报消息（内部函数）。

    Args:
        text: 播报文本。

    Returns:
        AIMessage: 单条 AI 消息（经 add_messages 通道追加）。

    Raises:
        无。
    """
    return AIMessage(content=text)


# ---------------------------------------------------------------------------
# 节点 1：sp_input —— 输入采集（项目 / 源文本 / 已确认角色）
# ---------------------------------------------------------------------------
async def sp_input_node(state: ScriptPipelineState) -> dict:
    """输入采集节点：读项目、剧本输入源文本与已确认角色并写入 state。

    LangGraph 节点内不做重 IO 的约定在此放宽为「一次性轻量读取」（3 张表各
    1 次查询），换取子图输入的最小化（调用方只需传 project_id）。前置不满足
    （缺 project_id / 项目不存在 / 无剧本输入）时把原因写入 error 通道，
    由条件边 _route_after_input 据此路由到 END。

    Args:
        state: 当前子图状态（须含 project_id；可选 user_id/instruction/
            auto_confirm，由入口调用方注入）。

    Returns:
        dict: 部分状态更新（project_id/user_id/auto_confirm/raw_text/
        confirmed_characters/messages 或 error）。

    Raises:
        不抛出异常：数据库异常捕获后转 error 通道终止。
    """
    project_id = state.get("project_id")
    if not project_id:
        return {"error": "缺少 project_id，无法启动剧本流水线"}
    try:
        async with _db_session() as db:
            project = await db.get(Project, int(project_id))
            if project is None or project.deleted_at is not None:
                return {"error": "项目不存在"}
            script = (
                await db.execute(select(Script).where(Script.project_id == project.id))
            ).scalar_one_or_none()
            raw_text = ""
            if script is not None:
                # 源文本取源优先级与 REST 接口一致：raw_text 优先 → 结构化剧本解包
                raw_text = (script.raw_text or "").strip()
                if not raw_text:
                    plain = content_plain(script.content)
                    if content_has_payload(plain):
                        try:
                            raw_text = build_extract_source_text(script).strip()
                        except Exception:  # noqa: BLE001 —— 仅 raw 载荷等边缘形态兜底
                            raw_text = str(plain.get("raw") or "").strip()
            if not raw_text:
                return {"error": "请先保存剧本输入"}
            rows = (
                await db.execute(
                    select(Character)
                    .where(
                        Character.project_id == project.id,
                        Character.deleted_at.is_(None),
                        Character.confirmed_at.is_not(None),
                    )
                    .order_by(Character.id)
                )
            ).scalars().all()
            confirmed = _confirmed_rows_to_dicts(list(rows))
    except Exception as exc:  # noqa: BLE001 —— 输入采集失败不拖垮整个图
        logger.warning("sp_input_node 读取项目输入失败（project_id=%s）：%s", project_id, exc)
        return {"error": "读取项目输入失败，请稍后重试"}
    return {
        "project_id": int(project_id),
        # 入口通道回显：user_id 供 REST 侧 thread 归属双重校验；auto_confirm
        # 决定三处确认闸口是否跳过 interrupt（一键串跑）
        "user_id": state.get("user_id"),
        "auto_confirm": bool(state.get("auto_confirm")),
        "raw_text": raw_text,
        "confirmed_characters": confirmed,
        "error": "",
        "messages": [_message("📖 已读取项目输入与已确认角色，开始生成角色班底…")],
    }


# ---------------------------------------------------------------------------
# 节点 2：sp_character_planner —— 角色班底生成 + 确认闸口（interrupt 1）
# ---------------------------------------------------------------------------
async def sp_character_planner_node(
    state: ScriptPipelineState, config: Optional[RunnableConfig] = None
) -> dict:
    """角色班底节点：plan_characters 生成候选 → 落 config → interrupt 确认。

    流程（含 interrupt 恢复幂等与退回重生）：
    1. 进度标记未命中或标记携带待消费退回意见（regen）→ 调 plan_characters
       （LLM，内部失败重试；退回重生时以退回意见作为 instruction）→
       save_character_candidates 写 config 候选 → 更新标记（消费 regen）；
       标记命中且无退回意见（interrupt 恢复重跑）→ 直接复用 config 候选，
       不再调 LLM；
    2. 确认闸口：auto_confirm=True 时跳过 interrupt 直接放行；否则
       interrupt({"type": "confirm_characters", "stage": "characters", ...})
       暂停等用户决策；
    3. 拒绝 → 写回退标记（regen=退回意见）并置 retry_stage="characters"，
       条件边回退到本节点按意见重新生成；
    4. 确认 → confirm_character_entries 幂等入池 characters 表（若 resume
       携带人工编辑后清单则以编辑稿入池）+ characters_confirmed 快照 +
       阶段推进（characters→outline / NULL,idea 特例跨格）→
       confirmed_flags["characters"]=True，条件边路由 sp_episode_planner。

    路由约定：节点统一返回 dict，前进/回退/终止由条件边
    _route_after_characters 依 confirmed_flags 与 retry_stage 判定。

    Args:
        state: 当前子图状态（读 project_id/raw_text/instruction/auto_confirm）。
        config: LangGraph 运行配置（提取 thread_id 供进度标记幂等判断）。

    Returns:
        dict: 部分状态更新（confirmed_characters/confirmed_flags/retry_stage/
        reject_reason/messages 或 error）。

    Raises:
        HTTPException: plan_characters 3 轮重试耗尽抛 502（LLM 多次失败，
            属运行期故障，由调用方/会话层兜底提示）。
    """
    thread_id = _thread_id_of(config)
    project_id = int(state.get("project_id"))
    raw_text = state.get("raw_text") or ""
    # 1) 生成或复用候选（独立事务：interrupt 暂停前必须提交落库）
    async with _db_session() as db, db.begin():
        project = await db.get(Project, project_id)
        if project is None or project.deleted_at is not None:
            return {"error": "项目不存在"}
        marker = (project.config or {}).get(_SP_MARKER_KEY)
        regen_reason = _regen_reason(marker, "characters")
        candidates: List[dict] = []
        if regen_reason is None and _marker_matches(marker, "characters", thread_id):
            raw = (project.config or {}).get(CANDIDATES_KEY)
            if isinstance(raw, list):
                candidates = [dict(c) for c in raw if isinstance(c, dict)]
        if not candidates:
            # 退回重生：优先以退回意见为补充指令（无退回意见时用入口指令）
            instruction = regen_reason or state.get("instruction") or None
            candidates = await plan_characters(project, raw_text, instruction=instruction)
            save_character_candidates(db, project, candidates)
            project.config = {
                **(project.config or {}),
                # 重写标记即消费退回意见（后续 resume 重放不再触发重生）
                _SP_MARKER_KEY: {"thread_id": thread_id, "stage": "characters"},
            }
    # 2) 确认闸口：auto_confirm（一键串跑）跳过 interrupt 直接放行
    if state.get("auto_confirm"):
        decision: Any = {"decision": "approved", "auto": True}
    else:
        decision = interrupt({
            "type": "confirm_characters",
            "stage": "characters",
            "payload": {
                "message": "请确认角色设定",
                "count": len(candidates),
                "characters": [
                    {
                        "name": c.get("name"),
                        "role_type": c.get("role_type"),
                        "personality": str(c.get("personality") or "")[:60],
                    }
                    for c in candidates
                ],
            },
        })
    # 3) 退回：写回退标记（regen=退回意见）→ 条件边回退本节点按意见重新生成
    if not _decision_approved(decision):
        reason = _decision_reason(decision)
        async with _db_session() as db, db.begin():
            project = await db.get(Project, project_id)
            if project is not None:
                project.config = {
                    **(project.config or {}),
                    _SP_MARKER_KEY: {
                        "thread_id": thread_id, "stage": "characters", "regen": reason},
                }
        return {
            "character_candidates": candidates,
            "confirmed_flags": {"characters": False},
            "retry_stage": "characters",
            "reject_reason": reason,
            "error": "角色设定已退回，按退回意见重新生成中",
            "messages": [_message("↩️ 角色设定已退回，正在按意见重新生成…")],
        }
    # 4) 确认：幂等入池 + 里程碑快照 + 阶段推进（新事务）；resume 携带编辑稿时
    # 以人工编辑清单入池（同名幂等口径不变）
    edited = _decision_payload(decision, "characters")
    async with _db_session() as db, db.begin():
        project = await db.get(Project, project_id)
        if project is None or project.deleted_at is not None:
            return {"error": "项目不存在"}
        confirmed_names = await confirm_character_entries(
            db, project, edited or candidates)
        await write_stage_snapshot(
            db, project, "characters_confirmed",
            summary={"confirmed_count": len(confirmed_names), "names": confirmed_names},
        )
        # 阶段推进：characters → outline（一格校验）；NULL/idea 特例直接置
        if project.script_phase == "characters":
            await advance_phase(db, project, "outline")
        elif project.script_phase in (None, "idea"):
            project.script_phase = "outline"
        rows = (
            await db.execute(
                select(Character)
                .where(
                    Character.project_id == project.id,
                    Character.deleted_at.is_(None),
                    Character.confirmed_at.is_not(None),
                )
                .order_by(Character.id)
            )
        ).scalars().all()
        confirmed = _confirmed_rows_to_dicts(list(rows))
    return {
        "character_candidates": candidates,
        "confirmed_characters": confirmed,
        "confirmed_flags": {"characters": True},
        "retry_stage": "",
        "reject_reason": "",
        "error": "",
        "messages": [_message("✅ 角色设定已确认，开始生成分集大纲…")],
    }


# ---------------------------------------------------------------------------
# 节点 3：sp_episode_planner —— 分集大纲生成 + 确认闸口（interrupt 2）
# ---------------------------------------------------------------------------
async def sp_episode_planner_node(
    state: ScriptPipelineState, config: Optional[RunnableConfig] = None
) -> dict:
    """分集大纲节点：plan_episodes 生成 → 落 episodes 行 → interrupt 确认。

    流程（含 interrupt 恢复幂等与退回重生）：
    1. 进度标记未命中或标记携带待消费退回意见（regen）→ 按项目目标时长
       启发式决定 target_episodes（≥600 秒建议 3 集，否则 1 集，与 REST
       缺省口径一致）→ plan_episodes（已确认角色经 SimpleNamespace 注入
       提示词）→ save_episode_plan 落 episodes 行（多退少补）→ 更新标记
       （消费 regen）；标记命中且无退回意见 → 跳过生成直接读行；
    2. 确认闸口：auto_confirm=True 时跳过 interrupt 直接放行；否则
       interrupt({"type": "confirm_episodes", "stage": "outline", ...})；
    3. 退回 → 写回退标记（regen=退回意见）并置 retry_stage="episodes"，
       条件边回退本节点按意见重新生成；
    4. 确认 → 全部 episodes 置 confirmed_at（幂等；resume 携带编辑稿时先按
       编辑稿重写 episodes 行）+ outline_confirmed 快照 + 阶段推进
       （outline→body / NULL,idea,characters 特例跨格）→
       confirmed_flags["episodes"]=True 并写入 episodes_plan，条件边路由
       sp_body_dispatch。

    路由约定：节点统一返回 dict，前进/回退/终止由条件边
    _route_after_episodes 依 confirmed_flags 与 retry_stage 判定。

    Args:
        state: 当前子图状态（读 project_id/raw_text/confirmed_characters/
            auto_confirm）。
        config: LangGraph 运行配置（提取 thread_id）。

    Returns:
        dict: 部分状态更新（episodes_plan/confirmed_flags/retry_stage/
        reject_reason/messages 或 error）。

    Raises:
        HTTPException: plan_episodes 3 轮重试耗尽抛 502。
    """
    thread_id = _thread_id_of(config)
    project_id = int(state.get("project_id"))
    raw_text = state.get("raw_text") or ""
    confirmed_ns = _as_namespace_list(state.get("confirmed_characters") or [])
    # 1) 生成或复用分集（独立事务）
    async with _db_session() as db, db.begin():
        project = await db.get(Project, project_id)
        if project is None or project.deleted_at is not None:
            return {"error": "项目不存在"}
        marker = (project.config or {}).get(_SP_MARKER_KEY)
        regen_reason = _regen_reason(marker, "episodes")
        if regen_reason is not None or not _marker_matches(marker, "episodes", thread_id):
            # 目标集数缺省启发式：多集体量（≥600 秒）建议 3 集，否则 1 集
            target = 3 if (project.target_duration or 0) >= 600 else 1
            # 说明：算子层 plan_episodes 无 instruction 通道（Task 4 签名保持
            # 不变），退回意见仅随消息通道播报并留存 state.reject_reason；
            # 重生由温度采样带来差异，人工修稿可经 resume 编辑稿直接落库
            plan_items = await plan_episodes(project, raw_text, confirmed_ns, target)
            await save_episode_plan(db, project, plan_items)
            project.config = {
                **(project.config or {}),
                # 重写标记即消费退回意见（后续 resume 重放不再触发重生）
                _SP_MARKER_KEY: {"thread_id": thread_id, "stage": "episodes"},
            }
        episodes_rows = await list_project_episodes(db, project.id)
        payload = {
            "message": "请确认分集大纲",
            "count": len(episodes_rows),
            "episodes": [
                {"sequence": e.sequence, "title": e.title, "logline": e.logline}
                for e in episodes_rows
            ],
        }
    # 2) 确认闸口：auto_confirm（一键串跑）跳过 interrupt 直接放行
    if state.get("auto_confirm"):
        decision: Any = {"decision": "approved", "auto": True}
    else:
        decision = interrupt({
            "type": "confirm_episodes", "stage": "outline", "payload": payload})
    # 3) 退回：写回退标记（regen=退回意见）→ 条件边回退本节点按意见重新生成
    if not _decision_approved(decision):
        reason = _decision_reason(decision)
        async with _db_session() as db, db.begin():
            project = await db.get(Project, project_id)
            if project is not None:
                project.config = {
                    **(project.config or {}),
                    _SP_MARKER_KEY: {
                        "thread_id": thread_id, "stage": "episodes", "regen": reason},
                }
        return {
            "confirmed_flags": {"characters": True, "episodes": False},
            "retry_stage": "episodes",
            "reject_reason": reason,
            "error": "分集大纲已退回，按退回意见重新生成中",
            "messages": [_message("↩️ 分集大纲已退回，正在按意见重新生成…")],
        }
    # 4) 确认：编辑稿优先重写 episodes 行 → 幂等置 confirmed_at + 快照 +
    # 阶段推进 + 组装 episodes_plan
    edited_items = _normalize_edited_plan(_decision_payload(decision, "episodes"))
    async with _db_session() as db, db.begin():
        project = await db.get(Project, project_id)
        if project is None or project.deleted_at is not None:
            return {"error": "项目不存在"}
        if edited_items:
            # 人工编辑稿落库（多退少补口径与生成路径一致）
            await save_episode_plan(db, project, edited_items)
        episodes_rows = await list_project_episodes(db, project.id)
        if not episodes_rows:
            return {
                "error": "分集大纲为空，无法继续",
                "messages": [_message("❌ 分集大纲为空，流水线中止。")],
            }
        confirmed_at = datetime.now(timezone.utc)
        for episode in episodes_rows:
            if episode.confirmed_at is None:
                episode.confirmed_at = confirmed_at
        await write_stage_snapshot(
            db, project, "outline_confirmed",
            summary={
                "episode_count": len(episodes_rows),
                "titles": [e.title for e in episodes_rows],
            },
        )
        # 阶段推进：outline → body（一格校验）；NULL/idea/characters 特例直接置
        if project.script_phase == "outline":
            await advance_phase(db, project, "body")
        elif project.script_phase in (None, "idea", "characters"):
            project.script_phase = "body"
        plan = [
            {
                "episode_id": e.id,
                "sequence": e.sequence,
                "title": e.title,
                "logline": e.logline,
                "outline": dict(e.outline or {}),
            }
            for e in episodes_rows
        ]
    return {
        "episodes_plan": plan,
        "confirmed_flags": {"characters": True, "episodes": True},
        "retry_stage": "",
        "reject_reason": "",
        "error": "",
        "messages": [_message("✅ 分集大纲已确认，开始逐集生成正文…")],
    }


# ---------------------------------------------------------------------------
# 节点 4：sp_body_dispatch —— Send 并行分发（map）
# ---------------------------------------------------------------------------
async def sp_body_dispatch_node(
    state: ScriptPipelineState, config: Optional[RunnableConfig] = None
) -> Any:
    """正文分发节点：按已确认大纲经 Send 并行派发逐集写作子任务（MA-10 同款）。

    进入本节点即把进度标记重置为 "body_dispatching"：sp_body_reduce 以
    stage=="body" 作为「本轮正文已落库」的幂等依据，重置标记使「正文退回
    重生」后的 reduce 重新落库（版本乐观锁 +1 覆盖语义）；同时以
    {"bodies": None} 清空上一轮残留的并行结果通道。

    Args:
        state: 当前子图状态（读 episodes_plan/confirmed_characters/project_id）。
        config: LangGraph 运行配置（提取 thread_id 供标记重置）。

    Returns:
        Any: Command(goto=[Send("sp_episode_writer", {...}), ...], update=
        {"bodies": None})（每集一个 Send，payload 自包含供叶子节点独立消费）；
        大纲为空时返回 error 更新（正常流程不可达）。

    Raises:
        不抛出异常：标记重置失败仅记 warning，不阻断分发。
    """
    plan = [ep for ep in (state.get("episodes_plan") or []) if isinstance(ep, dict)]
    if not plan:
        return {"error": "分集大纲为空，无法生成正文"}
    project_id = state.get("project_id")
    thread_id = _thread_id_of(config)
    if project_id and thread_id:
        try:
            async with _db_session() as db, db.begin():
                project = await db.get(Project, int(project_id))
                if project is not None:
                    project.config = {
                        **(project.config or {}),
                        _SP_MARKER_KEY: {"thread_id": thread_id, "stage": "body_dispatching"},
                    }
        except Exception as exc:  # noqa: BLE001 —— 标记重置失败不阻断分发
            logger.warning("sp_body_dispatch 重置进度标记失败：%s", exc)
    confirmed = [c for c in (state.get("confirmed_characters") or []) if isinstance(c, dict)]
    return Command(goto=[
        Send(NODE_EPISODE_WRITER, {
            "project_id": project_id,
            "episode": {
                "episode_id": ep.get("episode_id"),
                "sequence": ep.get("sequence"),
                "title": ep.get("title"),
                "logline": ep.get("logline"),
                "outline": ep.get("outline") or {},
            },
            "confirmed_characters": confirmed,
        })
        for ep in plan
    ], update={"bodies": None})


# ---------------------------------------------------------------------------
# 节点 5：sp_episode_writer —— 单集正文叶子节点（Send 目标，两段式生成）
# ---------------------------------------------------------------------------
async def sp_episode_writer(payload: dict) -> dict:
    """单集正文写作叶子节点（Send 目标，并行执行）：正文流式生成 + 场次结构化。

    消费 Send payload（非完整 state，与 generation_item 同风格）：
    - 两段式第一段：stream_episode_body 分段流式产出文章体正文（增量拼接）；
    - 两段式第二段：plan_episode_scenes 把正文结构化为 {locations, scenes}；
    - 结果以单元素列表写入 bodies 通道（Annotated add 合并器，并行互不覆盖）。

    单集失败不阻断整批（与 generation_item 的降级语义一致）：异常以
    ok=False 占位返回，由汇总节点计数并在确认 payload 中提示。

    Args:
        payload: Send 分发的子任务载荷（project_id / episode / confirmed_characters）。

    Returns:
        dict: {"bodies": [{episode_id, sequence, title, raw_text, locations,
        scenes, ok}]}。

    Raises:
        不抛出异常：LLM/结构化异常捕获后降级为 ok=False 占位。
    """
    episode_info = payload.get("episode") or {}
    episode_id = int(episode_info.get("episode_id") or 0)
    sequence = episode_info.get("sequence")
    title = str(episode_info.get("title") or "")
    project_id = int(payload.get("project_id") or 0)
    confirmed_ns = _as_namespace_list(payload.get("confirmed_characters") or [])
    raw_text = ""
    structured: Dict[str, Any] = {"locations": [], "scenes": []}
    ok = False
    try:
        async with _db_session() as db:
            project = await db.get(Project, project_id)
            episode = await db.get(Episode, episode_id)
            if project is None or episode is None:
                logger.warning(
                    "sp_episode_writer 数据缺失（project_id=%s, episode_id=%s）",
                    project_id, episode_id,
                )
            else:
                # 第一段：文章体正文（分段流式，增量拼接为整串）
                parts: List[str] = []
                async for kind, chunk in stream_episode_body(project, episode, confirmed_ns):
                    if kind == "delta":
                        parts.append(chunk)
                raw_text = "".join(parts)
                # 第二段：结构化场次（非流式 json_mode，仅在有正文时调用）
                if raw_text.strip():
                    structured = await plan_episode_scenes(
                        project, episode, confirmed_ns, raw_text)
                    ok = bool(structured.get("scenes"))
    except Exception as exc:  # noqa: BLE001 —— 单集失败不阻断整批
        logger.warning("sp_episode_writer 第 %s 集生成失败：%s", episode_id, exc)
        raw_text = ""
        structured = {"locations": [], "scenes": []}
        ok = False
    return {"bodies": [{
        "episode_id": episode_id,
        "sequence": sequence,
        "title": title,
        "raw_text": raw_text,
        "locations": list(structured.get("locations") or []),
        "scenes": list(structured.get("scenes") or []),
        "ok": ok,
    }]}


# ---------------------------------------------------------------------------
# 节点 6：sp_body_reduce —— 正文汇总落库 + 确认闸口（interrupt 3）
# ---------------------------------------------------------------------------
async def sp_body_reduce_node(
    state: ScriptPipelineState, config: Optional[RunnableConfig] = None
) -> dict:
    """正文汇总节点：bodies 汇聚 → scripts 行逐集 upsert → interrupt 终确认。

    流程（含 interrupt 恢复幂等与退回重生）：
    1. 读取并行汇聚的 bodies（按 sequence 排序；失败条目剔除落库、保留计数）；
    2. 进度标记未命中 → 逐集落库：scripts 行按 (project_id, episode_id)
       upsert（首次新建 version=1；已存在行乐观锁口径 UPDATE WHERE
       version=旧值 后 version+1）+ RD-03 字段级 ai 来源包装（经
       validate_script_body_content 结构门槛校验）+ episode.status='scripted'
       + body_generated 快照 → 更新标记；标记命中 → 跳过落库（恢复重跑不
       重复自增版本）；「正文退回重生」后标记已被 sp_body_dispatch 重置为
       body_dispatching，故本节点重新落库（覆盖语义）；
    3. 确认闸口：auto_confirm=True 时跳过 interrupt 直接放行；否则
       interrupt({"type": "confirm_body", "stage": "body", ...})；
    4. 确认 → confirmed_flags["body"]=True（条件边直达 END，success）；
       退回 → 置 retry_stage="body"（条件边回退 sp_body_dispatch 逐集重生；
       已落库正文在重生覆盖前保持可用）。

    Args:
        state: 当前子图状态（bodies 已由 _merge_bodies 合并器汇聚、project_id）。
        config: LangGraph 运行配置（提取 thread_id）。

    Returns:
        dict: 部分状态更新（confirmed_flags/retry_stage/reject_reason/
        error/messages）。

    Raises:
        不抛出异常：落库/校验异常逐集捕获跳过（记 warning），不阻断确认闸口。
    """
    thread_id = _thread_id_of(config)
    project_id = int(state.get("project_id"))
    bodies = [b for b in (state.get("bodies") or []) if isinstance(b, dict)]
    bodies.sort(key=lambda b: (b.get("sequence") or 0, b.get("episode_id") or 0))
    ok_bodies = [b for b in bodies if b.get("ok") and str(b.get("raw_text") or "").strip()]
    failed_bodies = [b for b in bodies if not (b.get("ok") and str(b.get("raw_text") or "").strip())]
    # 1) 汇总落库（独立事务；标记命中=恢复重跑时跳过，避免 version 重复自增）
    if ok_bodies:
        async with _db_session() as db, db.begin():
            project = await db.get(Project, project_id)
            if project is None or project.deleted_at is not None:
                return {
                    "error": "项目不存在",
                    "messages": [_message("❌ 项目不存在，流水线中止。")],
                }
            marker = (project.config or {}).get(_SP_MARKER_KEY)
            if not _marker_matches(marker, "body", thread_id):
                for item in ok_bodies:
                    try:
                        episode = await db.get(Episode, int(item.get("episode_id") or 0))
                        if episode is None or episode.project_id != project.id:
                            continue
                        content_payload = {
                            "title": episode.title,
                            "logline": episode.logline or "",
                            "locations": item.get("locations") or [],
                            "scenes": item.get("scenes") or [],
                        }
                        # 结构门槛（ScriptBodyContent：scenes ≥1、字段白名单）
                        validated, _err = validate_script_body_content(content_payload)
                        if validated is None:
                            logger.warning(
                                "sp_body_reduce 第 %s 集结构校验失败，跳过落库",
                                item.get("episode_id"))
                            continue
                        # RD-03 字段级来源标记：正文各一级字段包装为 ai 来源
                        new_content = wrap_content_fields(validated, "ai")
                        script = (
                            await db.execute(
                                select(Script).where(
                                    Script.project_id == project.id,
                                    Script.episode_id == episode.id,
                                )
                            )
                        ).scalar_one_or_none()
                        if script is None:
                            db.add(Script(
                                project_id=project.id,
                                episode_id=episode.id,
                                content=new_content,
                                raw_text=item.get("raw_text"),
                                version=1,
                            ))
                        else:
                            # 乐观锁口径：UPDATE WHERE version=旧值，冲突放弃本次覆盖
                            start_version = script.version or 0
                            lock_result = await db.execute(
                                update(Script)
                                .where(Script.id == script.id,
                                       Script.version == start_version)
                                .values(content=new_content,
                                        raw_text=item.get("raw_text"),
                                        version=start_version + 1)
                            )
                            if lock_result.rowcount == 0:
                                logger.warning(
                                    "sp_body_reduce 第 %s 集乐观锁冲突，跳过覆盖",
                                    episode.id)
                        episode.status = "scripted"
                    except Exception as exc:  # noqa: BLE001 —— 单集落库失败不阻断整批
                        logger.warning(
                            "sp_body_reduce 第 %s 集落库失败：%s",
                            item.get("episode_id"), exc)
                await write_stage_snapshot(
                    db, project, "body_generated",
                    summary={
                        "episode_count": len(ok_bodies),
                        "titles": [b.get("title") for b in ok_bodies],
                    },
                )
                project.config = {
                    **(project.config or {}),
                    _SP_MARKER_KEY: {"thread_id": thread_id, "stage": "body"},
                }
    # 2) 终确认闸口：auto_confirm（一键串跑）跳过 interrupt 直接放行
    if state.get("auto_confirm"):
        decision: Any = {"decision": "approved", "auto": True}
    else:
        decision = interrupt({
            "type": "confirm_body",
            "stage": "body",
            "payload": {
                "message": "请确认剧本正文",
                "count": len(ok_bodies),
                "episodes": [
                    {
                        "episode_id": b.get("episode_id"),
                        "title": b.get("title"),
                        "chars": len(str(b.get("raw_text") or "")),
                        "scenes": len(b.get("scenes") or []),
                    }
                    for b in ok_bodies
                ],
                "failed": [
                    {"episode_id": b.get("episode_id"), "title": b.get("title")}
                    for b in failed_bodies
                ],
            },
        })
    # 3) 退回：置 retry_stage="body" → 条件边回退 sp_body_dispatch 逐集重生
    if not _decision_approved(decision):
        return {
            "error": "剧本正文已退回，按退回意见重新生成中",
            "confirmed_flags": {"characters": True, "episodes": True, "body": False},
            "retry_stage": "body",
            "reject_reason": _decision_reason(decision),
            "messages": [_message("↩️ 剧本正文已退回，正在逐集重新生成…")],
        }
    return {
        "confirmed_flags": {"characters": True, "episodes": True, "body": True},
        "retry_stage": "",
        "reject_reason": "",
        "error": "",
        "messages": [_message("✅ 全部剧本正文已生成并确认，流水线完成。")],
    }


# ---------------------------------------------------------------------------
# 子图编译与模块级单例
# ---------------------------------------------------------------------------
def _route_after_input(state: ScriptPipelineState) -> str:
    """sp_input 之后的条件路由：error 非空（前置不满足）→ END，否则进入角色班底。

    Args:
        state: sp_input 节点更新后的子图状态。

    Returns:
        str: "sp_character_planner" 或 "__end__"。

    Raises:
        不抛出异常。
    """
    return END if state.get("error") else NODE_CHARACTER_PLANNER


def _route_after_characters(state: ScriptPipelineState) -> str:
    """角色确认后的条件路由：确认通过 → 大纲节点；退回 → 本节点重生；否则 END。

    Args:
        state: sp_character_planner 节点更新后的子图状态。

    Returns:
        str: "sp_episode_planner"（确认）/ "sp_character_planner"（退回重生）/
        "__end__"（前置异常等终止）。

    Raises:
        不抛出异常。
    """
    if (state.get("confirmed_flags") or {}).get("characters"):
        return NODE_EPISODE_PLANNER
    if state.get("retry_stage") == "characters":
        return NODE_CHARACTER_PLANNER
    return END


def _route_after_episodes(state: ScriptPipelineState) -> str:
    """大纲确认后的条件路由：确认通过 → 分发节点；退回 → 本节点重生；否则 END。

    Args:
        state: sp_episode_planner 节点更新后的子图状态。

    Returns:
        str: "sp_body_dispatch"（确认）/ "sp_episode_planner"（退回重生）/
        "__end__"（大纲为空等终止）。

    Raises:
        不抛出异常。
    """
    if (state.get("confirmed_flags") or {}).get("episodes"):
        return NODE_BODY_DISPATCH
    if state.get("retry_stage") == "episodes":
        return NODE_EPISODE_PLANNER
    return END


def _route_after_reduce(state: ScriptPipelineState) -> str:
    """正文终确认后的条件路由：退回 → 重新分发逐集重生；否则 END。

    Args:
        state: sp_body_reduce 节点更新后的子图状态。

    Returns:
        str: "sp_body_dispatch"（正文退回重生）或 "__end__"（完成/中止）。

    Raises:
        不抛出异常。
    """
    return NODE_BODY_DISPATCH if state.get("retry_stage") == "body" else END


def build_script_pipeline_subgraph(
    checkpointer: Optional[BaseCheckpointSaver] = None, auto_confirm: bool = False
):
    """构建并编译剧本流水线子图。

    边设计（interrupt 内嵌于节点；确认闸口的「前进/退回」用条件边而非
    Command(goto=...)——本版本 LangGraph 的 Command.goto 与静态边叠加触发，
    无法单独表达终止与回退）：
    - START → sp_input --(条件：error 为空)--> sp_character_planner；
    - sp_character_planner --(条件：confirmed_flags.characters →
      sp_episode_planner / retry_stage=characters → 自身重生 / 否则 END)；
    - sp_episode_planner --(条件：confirmed_flags.episodes → sp_body_dispatch /
      retry_stage=episodes → 自身重生 / 否则 END)；
    - sp_body_dispatch --Send--> sp_episode_writer（map 并行，LangGraph 原生
      Send 目标路由，不经静态边）；
    - sp_episode_writer → sp_body_reduce --(条件：retry_stage=body →
      sp_body_dispatch 逐集重生 / 否则 END)。

    auto_confirm（一键串跑，决策点 3 的 A 选项保留）：True 时在入口节点注入
    state.auto_confirm=True，三处确认闸口自动放行（不产生 interrupt），
    供「一键写完」场景与全流程串跑测试；默认 False（RD-04：每阶段人工确认）。

    Args:
        checkpointer: LangGraph 检查点实例（interrupt 依赖检查点持久化暂停态；
            生产路径 get_compiled_script_pipeline 恒传入，None 仅供无中断的
            局部测试）。
        auto_confirm: 是否一键串跑（跳过三处人工确认闸口），默认 False。

    Returns:
        CompiledStateGraph: 可执行的编译子图。

    Raises:
        Exception: 节点/边定义非法时由 LangGraph 抛出（属开发期错误，不捕获）。
    """
    builder = StateGraph(ScriptPipelineState)

    if auto_confirm:
        async def _auto_input_node(
            state: ScriptPipelineState, config: Optional[RunnableConfig] = None
        ) -> dict:
            """一键串跑入口包装：注入 auto_confirm=True 后交原输入节点（内部函数）。

            Args:
                state: 图输入（含 project_id 等）。
                config: LangGraph 运行配置（透传，当前未使用）。

            Returns:
                dict: sp_input_node 的状态更新（已带 auto_confirm=True）。

            Raises:
                不抛出异常：透传 sp_input_node 的返回（其内部已捕获异常）。
            """
            return await sp_input_node({**state, "auto_confirm": True})

        builder.add_node(NODE_INPUT, _auto_input_node)
    else:
        builder.add_node(NODE_INPUT, sp_input_node)
    builder.add_node(NODE_CHARACTER_PLANNER, sp_character_planner_node)
    builder.add_node(NODE_EPISODE_PLANNER, sp_episode_planner_node)
    builder.add_node(NODE_BODY_DISPATCH, sp_body_dispatch_node)
    builder.add_node(NODE_EPISODE_WRITER, sp_episode_writer)
    builder.add_node(NODE_BODY_REDUCE, sp_body_reduce_node)
    builder.add_edge(START, NODE_INPUT)
    # 确认闸口条件路由：确认 → 前进；退回 → 回退上游生成节点重生；前置异常 → END
    builder.add_conditional_edges(
        NODE_INPUT, _route_after_input, [NODE_CHARACTER_PLANNER, END])
    builder.add_conditional_edges(
        NODE_CHARACTER_PLANNER, _route_after_characters,
        [NODE_CHARACTER_PLANNER, NODE_EPISODE_PLANNER, END])
    builder.add_conditional_edges(
        NODE_EPISODE_PLANNER, _route_after_episodes,
        [NODE_EPISODE_PLANNER, NODE_BODY_DISPATCH, END])
    # Send 并行分发不经静态边；writer 全部完成后汇聚到 reduce
    builder.add_edge(NODE_EPISODE_WRITER, NODE_BODY_REDUCE)
    builder.add_conditional_edges(
        NODE_BODY_REDUCE, _route_after_reduce, [NODE_BODY_DISPATCH, END])
    return builder.compile(checkpointer=checkpointer)


# 模块级编译子图缓存（懒加载单例，与主图共享 checkpointer）
_compiled_script_pipeline = None


async def get_compiled_script_pipeline():
    """获取带检查点的剧本子图模块级单例（线程/协程安全懒加载）。

    checkpointer 复用 app.agents.graph.get_checkpointer() 的单例（三级降级
    策略：AsyncPostgresSaver → 同步 PostgresSaver 线程池适配 → InMemorySaver），
    与主图共享同一连接与检查点表，以 thread_id 维度隔离；惰性初始化，
    导入期不建任何 DB 连接。

    Returns:
        CompiledStateGraph: 编译完成的剧本子图单例（含 checkpointer）。

    Raises:
        不抛出异常：检查点初始化失败自动降级；图构建本身无运行期外部依赖。
    """
    global _compiled_script_pipeline
    if _compiled_script_pipeline is not None:
        return _compiled_script_pipeline
    from app.agents.graph import get_checkpointer  # 延迟导入避免循环依赖

    checkpointer = await get_checkpointer()
    _compiled_script_pipeline = build_script_pipeline_subgraph(checkpointer=checkpointer)
    return _compiled_script_pipeline
