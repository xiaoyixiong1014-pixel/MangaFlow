"""剧本创作智能体模块（文件级说明）。

- script_node：根据创作简报（artifacts.brief）或用户消息，生成漫剧剧本创意，
  输出结构固定为「标题 + Logline + 三幕大纲 + 主角设定」的中文创作内容；
- script_confirm_node：MA-08 HITL 确认点（LangGraph interrupt）——剧本产出后
  暂停图执行并向客户端推送 confirm_entities 确认请求；恢复时按 decision
  决定放行（approved=True，后续流程照常）或拒绝（artifacts 标记 rejected 后
  直达 END）。拆分为独立确认节点的原因：interrupt 恢复时节点函数会从头重跑，
  把「LLM 生成」与「interrupt 等待」拆开可避免恢复后重复调用 LLM 导致用户
  确认内容与最终产出不一致。
"""

import logging

from langchain_core.messages import AIMessage
from langgraph.types import interrupt

from app.agents.llm_adapter import llm
from app.agents.state import AgentState, last_human_text

logger = logging.getLogger(__name__)

# 剧本创作系统提示词（角色化 + 输出结构约束）
_SYSTEM_PROMPT = (
    "你是漫序 MangaFlow 的剧本创作专家，擅长为 AI 漫剧（动态漫/竖屏短剧）撰写剧本。"
    "请全程使用中文，按以下固定结构输出：\n"
    "【标题】一个吸引人、符合短剧调性的片名\n"
    "【Logline】一句话故事梗概（30 字以内，突出核心冲突）\n"
    "【三幕大纲】\n第一幕（建置）：……\n第二幕（对抗）：……\n第三幕（结局）：……\n"
    "【主角设定】姓名 / 年龄 / 性格 / 核心动机 / 成长弧光\n"
    "要求：冲突鲜明、反转有力，节奏适配 3-5 分钟漫剧体量。"
)


async def script_node(state: AgentState) -> dict:
    """剧本创作节点：生成标题 + Logline + 三幕大纲 + 主角设定。

    Args:
        state: 当前图状态；输入优先级为 artifacts["brief"] > 最后一条人类消息 > 默认需求。

    Returns:
        dict: 部分状态更新 {"messages": [AIMessage(剧本内容)],
        "artifacts": 合并后产出物（script 键），"error": ""（成功）或 "llm_unavailable"（降级）}。

    Raises:
        不抛出异常：LLM 调用失败时捕获并输出「（离线降级）」预置剧本框架模板。
    """
    brief = (state.get("artifacts") or {}).get("brief")
    user_text = brief or last_human_text(state) or "请创作一个原创校园题材短剧剧本"
    artifacts = dict(state.get("artifacts") or {})
    try:
        content = await llm.chat(
            state,
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"创作需求：{user_text}"},
            ],
            agent_name="script",
            temperature=0.8,
        )
        artifacts["script"] = content
        artifacts = llm.attach_usage(artifacts)
        return {"messages": [AIMessage(content=content)], "artifacts": artifacts, "error": ""}
    except Exception as exc:  # noqa: BLE001 —— LLM 异常必须降级，保证图不崩溃
        logger.warning("script_node LLM 调用失败，降级输出离线模板：%s", exc)
        fallback = (
            "（离线降级）当前无法连接创作模型，以下为预置剧本框架模板：\n"
            "【标题】《未命名的叙事》\n"
            "【Logline】主人公在校园里发现了一个改变命运的秘密。\n"
            "【三幕大纲】第一幕（建置）：平静日常中出现异常线索，悬念埋下；"
            "第二幕（对抗）：主人公追查真相，与阻力正面冲突，误会加深；"
            "第三幕（结局）：真相揭晓，完成反转，人物完成成长。\n"
            "【主角设定】姓名待定 / 17 岁 / 敏感内向 / 寻找真相 / 从逃避到直面。"
        )
        artifacts["script"] = fallback
        return {"messages": [AIMessage(content=fallback)], "artifacts": artifacts, "error": "llm_unavailable"}


def _script_summary(script_text: str) -> dict:
    """从剧本全文提取用于人工确认的实体摘要（内部函数）。

    Args:
        script_text: script_node 产出的剧本全文（标题/Logline/三幕大纲/主角设定）。

    Returns:
        dict: {"title", "logline", "preview"} —— 按【标题】/【Logline】标签行
        抽取，缺省时以占位符补齐；preview 为全文前 200 字。

    Raises:
        不抛出异常：任何解析异常均以空串/占位符兜底。
    """
    title, logline = "", ""
    try:
        for line in (script_text or "").splitlines():
            stripped = line.strip()
            if not title and stripped.startswith("【标题】"):
                title = stripped[len("【标题】"):].strip()
            elif not logline and stripped.startswith("【Logline】"):
                logline = stripped[len("【Logline】"):].strip()
            if title and logline:
                break
    except Exception:  # noqa: BLE001 —— 摘要失败不阻塞确认点
        pass
    return {
        "title": title or "（待确认）",
        "logline": logline or "（待确认）",
        "preview": (script_text or "")[:200],
    }


async def script_confirm_node(state: AgentState) -> dict:
    """剧本实体确认节点（MA-08 HITL）：interrupt 暂停等待用户确认/拒绝。

    在 script_node 产出剧本后调用 interrupt() 暂停图执行，检查点持久化当前
    状态；客户端经 POST /api/agent/chat/resume 以 Command(resume={"approved":...})
    恢复。恢复时本节点从头重跑（无 LLM 副作用，幂等），interrupt 返回决策：
    - approved=True：放行（后续节点照常；本链路直接 END）；
    - approved=False：artifacts 标记 rejected，直达 END。

    Args:
        state: 当前图状态；摘要取自 artifacts["script"]。

    Returns:
        dict: 部分状态更新 {"artifacts": 合并后产出物（rejected 标记仅拒绝时写入），
        "messages": [AIMessage(确认/取消播报)]}。

    Raises:
        Exception: checkpointer 缺失时 interrupt 由 LangGraph 抛出
            （生产路径 get_compiled_graph 恒带检查点，不会触发）。
    """
    artifacts = dict(state.get("artifacts") or {})
    script_text = str(artifacts.get("script") or "")
    # HITL 确认点：payload 为实体提取摘要，供前端渲染确认卡片
    decision = interrupt({
        "type": "confirm_entities",
        "payload": {"summary": _script_summary(script_text)},
    })
    approved = bool(isinstance(decision, dict) and decision.get("approved"))
    if not approved:
        # 拒绝：标记 rejected 并直达 END（不影响 artifacts 中已有草稿）
        artifacts["rejected"] = True
        artifacts["rejected_type"] = "confirm_entities"
        return {
            "messages": [AIMessage(content="❌ 已取消本次剧本方案，欢迎调整需求后再次发起。")],
            "artifacts": artifacts,
            "error": "",
        }
    return {
        "messages": [AIMessage(content="✅ 剧本方案已确认，可继续发起角色定妆 / 分镜拆解。")],
        "artifacts": artifacts,
        "error": "",
    }
