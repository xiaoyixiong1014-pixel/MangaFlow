"""用户记忆检索路由模块（文件级说明）。

实现 MA-07 用户长期记忆的语义检索接口：
- GET /api/memories/search?q=&top_k=  当前用户记忆 TopK 余弦检索

记忆写入入口在 services/memory_service.upsert_memory（由智能体会话
摘要等场景接线调用），本路由只提供只读检索。
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services.memory_service import search_memories

router = APIRouter(prefix="/api/memories", tags=["memories"])


@router.get("/search")
async def search_user_memories(
    q: str = Query(min_length=1, max_length=200, description="查询文本"),
    top_k: int = Query(default=5, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """当前用户记忆语义检索（余弦 TopK）。

    Args:
        q: 查询文本。
        top_k: 返回条数上限（1~50，默认 5）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        Dict[str, Any]: {"items": [{namespace, key, text, score, updated_at}], "total"}。

    Raises:
        HTTPException: 401 —— 未认证。
    """
    rows = await search_memories(db, user.id, q, top_k)
    return {"items": rows, "total": len(rows)}
