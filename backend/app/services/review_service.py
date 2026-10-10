"""内容审核服务模块（文件级说明）。

实现内容机审与分级处罚（RD-34 三级审核闸门的闸门一辅助 / C-04 分级处罚）：
- STRICT_WORDS / SENSITIVE_WORDS：分级机审词表（严禁级→violate、敏感级→suspect），
  可由站点设置扩展（本阶段以模块级常量承载）；
- classify_text()：分级词表机审判定，返回 (结果, 命中词, 级别)；
- find_banned_word()：旧版兼容包装（等价于 classify_text 的命中词）；
- llm_review_content()：LLM 内容机审（JSON 判定），失败抛异常由调用方降级词表；
- apply_violation_penalty()：机审 violate 后的分级处罚（违规累计 1-2 次受限、
  ≥3 次禁用，处罚落 system 通知）；
- create_review_record()：统一写入 review_records 审核记录（机审/人工复核状态）。
"""

import asyncio
import json
import logging
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ReviewRecord, User
from app.services import notification_service

logger = logging.getLogger("mangaflow.services.review")

# 严禁级词表（命中即判 violate，RD-34 闸门一拦截退款）：赌博/枪械/毒品/暴恐/色情等。
# 真实场景应由站点设置（site_settings KV）扩展，本阶段以模块级常量承载。
STRICT_WORDS = [
    "赌博", "赌场", "赌资", "押注", "枪击", "枪战", "枪械", "贩枪",
    "血腥", "屠杀", "虐杀", "毒品", "吸毒", "贩毒", "冰毒",
    "恐怖袭击", "爆炸袭击", "自杀式袭击", "色情", "淫秽",
]

# 敏感级词表（命中判 suspect，写记录等人工复核）：政治人物/集会/低俗/暴力暗示等。
SENSITIVE_WORDS = [
    "领导人", "政治", "游行", "示威", "罢工", "暴动",
    "流血", "斗殴", "持刀", "凶器", "尸体", "死亡",
    "自杀", "灵异", "迷信", "裸露", "低俗", "性暗示",
    "烟酒", "酗酒",
]

# 兼容别名：旧版机审词表（保持既有导入方可用，语义为全量词表的并集引用）
BANNED_WORDS = STRICT_WORDS + SENSITIVE_WORDS

# LLM 机审单次调用超时（秒）：超时视为 LLM 审核失败，由调用方降级词表判定
_LLM_REVIEW_TIMEOUT = 12.0

# LLM 机审系统提示词：要求只输出 JSON 判定（Task 4.1 图像生成后复审）
_LLM_REVIEW_SYSTEM_PROMPT = (
    "你是内容安全审核员。请对给定的 AI 生成素材描述（提示词/画面描述）做内容安全判定，"
    '只输出 JSON：{"verdict": "pass|suspect|violate", "reason": "一句话理由"}。'
    "判定标准：涉及赌博、枪械暴力、毒品、暴恐、色情等严禁内容 → violate；"
    "涉及政治敏感、低俗暗示、轻度暴力等可疑内容 → suspect；其余正常创作内容 → pass。"
)


def classify_text(text: str) -> Tuple[str, Optional[str], Optional[str]]:
    """分级词表机审判定（RD-34 闸门一，Task 4.1 分级词表）。

    判定优先级：先查严禁级词表（violate），再查敏感级词表（suspect），
    均未命中判 pass。纯子串匹配，与旧版 find_banned_word 同口径。

    Args:
        text: 待检测文本（提示词/对白/生成描述等）。

    Returns:
        Tuple[str, Optional[str], Optional[str]]: (result, hit_word, level)。
            result ∈ pass/suspect/violate；hit_word 为命中的第一个词（未命中 None）；
            level ∈ strict/sensitive/None。

    Raises:
        无：纯字符串匹配，不产生异常路径。
    """
    if not text:
        return "pass", None, None
    for word in STRICT_WORDS:
        if word in text:
            return "violate", word, "strict"
    for word in SENSITIVE_WORDS:
        if word in text:
            return "suspect", word, "sensitive"
    return "pass", None, None


def find_banned_word(text: str) -> Optional[str]:
    """检测文本是否命中违规词表（旧版兼容包装）。

    语义已随分级词表扩展：等价于 classify_text 的命中词（严禁级优先）。
    新调用方推荐直接使用 classify_text 以获取分级信息。

    Args:
        text: 待检测文本（提示词/对白等）。

    Returns:
        Optional[str]: 命中的第一个违规词；未命中返回 None。

    Raises:
        无：纯字符串匹配，不产生异常路径。
    """
    _result, word, _level = classify_text(text)
    return word


def _strip_json_fences(text: str) -> str:
    """剥离 LLM 输出中的 Markdown 代码围栏（内部函数）。

    Args:
        text: 模型原始输出文本。

    Returns:
        str: 去除 ```json ... ``` 围栏后的纯文本。

    Raises:
        无：纯字符串处理，不产生异常路径。
    """
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # 去掉首行围栏标记与末行围栏
        if len(lines) >= 2:
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
    return stripped


async def llm_review_content(text: str) -> Tuple[str, str]:
    """LLM 内容机审（生成成功后复审，Task 4.1 图像审核链）。

    调用 providers.llm.chat（JSON 模式）对「生成提示词 + 资产描述」做内容
    安全判定；整体受 asyncio.wait_for 超时保护，任何异常（网络/鉴权/解析/
    超时）向上抛出，由调用方降级为纯词表判定（method="keyword"）。

    Args:
        text: 待审文本（提示词 + 资产描述拼接）。

    Returns:
        Tuple[str, str]: (verdict, reason)。verdict ∈ pass/suspect/violate
            （模型输出未知取值时保守判 suspect）；reason 为一句话理由。

    Raises:
        Exception: LLM 全渠道失败、超时或 JSON 解析失败时向上抛出。
    """
    from app.providers.llm import chat

    content = await asyncio.wait_for(
        chat(
            [
                {"role": "system", "content": _LLM_REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": (text or "")[:2000]},
            ],
            temperature=0.1,
            json_mode=True,
        ),
        timeout=_LLM_REVIEW_TIMEOUT,
    )
    data = json.loads(_strip_json_fences(content))
    if not isinstance(data, dict):
        raise ValueError(f"LLM 审核输出非 JSON 对象：{content[:100]}")
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in ("pass", "suspect", "violate"):
        # 未知判定结果按可疑处理（保守口径，交人工复核）
        verdict = "suspect"
    reason = str(data.get("reason") or "")[:200]
    return verdict, reason


async def apply_violation_penalty(db: AsyncSession, user_id: int) -> str:
    """分级处罚（C-04）：机审 violate 后累计违规计数并降级账号状态。

    规则：violation_count += 1；累计 ≥3 次置 banned（禁用）；1-2 次置
    restricted（受限，生成接口按日限流）。状态变化时向用户推送 system
    类型站内通知（通知写入与处罚同一事务，由调用方提交）。

    Args:
        db: 异步数据库会话（调用方持有并负责提交）。
        user_id: 被处罚用户 ID（不存在时跳过并返回 unknown）。

    Returns:
        str: 处罚后的账号状态（active/restricted/banned；用户不存在为 unknown）。

    Raises:
        Exception: 数据库读写异常向上抛出（通知失败内部吞掉只记日志）。
    """
    user = (
        await db.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    if user is None:
        logger.warning("分级处罚跳过：用户 %s 不存在", user_id)
        return "unknown"
    user.violation_count = (user.violation_count or 0) + 1
    if user.violation_count >= 3:
        user.account_status = "banned"
        title = "账号已被禁用"
        content = f"您的账号因多次内容违规（累计 {user.violation_count} 次）已被禁用，如有疑问请联系运营方申诉。"
    else:
        user.account_status = "restricted"
        title = "账号已被限制"
        content = (
            f"您的账号因内容违规（累计 {user.violation_count} 次）已被限制："
            f"每日生成任务数上限 10 次，累计违规 3 次将被禁用。"
        )
    await db.flush()
    # 处罚通知（system 类型）：失败不影响处罚主流程
    try:
        await notification_service.create_notification(
            db, user_id=user_id, type="system", title=title, content=content,
            related_type="review",
        )
    except Exception:  # noqa: BLE001 —— 通知失败不影响处罚
        logger.warning("用户 %s 处罚通知发送失败", user_id, exc_info=True)
    logger.info("用户 %s 分级处罚：第 %d 次违规，状态置为 %s",
                user_id, user.violation_count, user.account_status)
    return user.account_status


async def create_review_record(
    db: AsyncSession,
    *,
    project_id: Optional[int],
    gate: str,
    target_type: str,
    target_id: Optional[int] = None,
    result: str = "pass",
    violation_type: Optional[str] = None,
    suggestion: Optional[str] = None,
    manual_status: Optional[str] = None,
    method: Optional[str] = None,
) -> ReviewRecord:
    """写入一条审核记录（立即提交）。

    Args:
        db: 异步数据库会话（函数内部提交事务）。
        project_id: 归属项目 ID（可空）。
        gate: 审核闸门 generate/compose/publish（RD-34 三级闸门）。
        target_type: 目标类型 text/image/video/film。
        target_id: 目标 ID（多态引用，如 storyboard_id / task 关联对象 ID）。
        result: 机审结果 pass/suspect/violate。
        violation_type: 违规类型（如 banned_word/sensitive_word/llm_review）。
        suggestion: 可操作建议（RD-36）。
        manual_status: 人工复核状态 pending/approved/rejected（默认 NULL 表示未进入人工复核，
            NULL 与 pending 同属"待人工"口径）。
        method: 机审方式 keyword/llm-text/gate（Task 4.1 新增，可空）。

    Returns:
        ReviewRecord: 新建并已落库的审核记录对象。

    Raises:
        Exception: 数据库写入异常向上抛出，由调用方处理。
    """
    record = ReviewRecord(
        project_id=project_id,
        gate=gate,
        target_type=target_type,
        target_id=target_id,
        result=result,
        violation_type=violation_type,
        suggestion=suggestion,
        manual_status=manual_status,
        method=method,
    )
    db.add(record)
    await db.commit()
    return record
