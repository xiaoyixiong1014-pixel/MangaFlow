"""内容审核服务模块（文件级说明）。

实现内容机审的模拟逻辑（RD-34 三级审核闸门的闸门一辅助）：
- BANNED_WORDS / find_banned_word()：演示用机审词表与命中检测；
- create_review_record()：统一写入 review_records 审核记录（机审/人工复核状态）。
"""

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ReviewRecord

# 演示用机审词表：命中即判违规（真实场景应替换为审核服务/大模型机审）
BANNED_WORDS = ["枪击", "血腥", "赌博"]


def find_banned_word(text: str) -> Optional[str]:
    """检测文本是否命中违规词表（机审模拟，RD-34 闸门一）。

    Args:
        text: 待检测文本（提示词/对白等）。

    Returns:
        Optional[str]: 命中的第一个违规词；未命中返回 None。

    Raises:
        无：纯字符串匹配，不产生异常路径。
    """
    if not text:
        return None
    for word in BANNED_WORDS:
        if word in text:
            return word
    return None


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
) -> ReviewRecord:
    """写入一条审核记录（立即提交）。

    Args:
        db: 异步数据库会话（函数内部提交事务）。
        project_id: 归属项目 ID（可空）。
        gate: 审核闸门 generate/compose/publish（RD-34 三级闸门）。
        target_type: 目标类型 text/image/video/film。
        target_id: 目标 ID（多态引用，如 storyboard_id / task 关联对象 ID）。
        result: 机审结果 pass/suspect/violate。
        violation_type: 违规类型（如 banned_word）。
        suggestion: 可操作建议（RD-36）。
        manual_status: 人工复核状态 pending/approved/rejected（默认 NULL 表示未进入人工复核）。

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
    )
    db.add(record)
    await db.commit()
    return record
