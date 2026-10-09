"""提示词库路由模块（文件级说明）。

实现提示词浏览 / 个人提示词 CRUD / 批量导入：
- GET    /api/prompts            分页浏览（scope=public 平台公共库 | private 我的私有库）
- POST   /api/prompts            新建私有提示词
- PUT    /api/prompts/{id}       更新（仅自己的私有提示词）
- DELETE /api/prompts/{id}       删除（仅自己的，物理删除）
- POST   /api/prompts/import     批量导入为私有提示词
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Prompt, User
from app.schemas.common import PageResult
from app.schemas.prompt import (
    PromptCreateRequest,
    PromptImportOut,
    PromptImportRequest,
    PromptOut,
    PromptUpdateRequest,
)
from app.services import normalize_pagination

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


async def _get_owned_prompt(db: AsyncSession, prompt_id: int, user: User) -> Prompt:
    """获取归属于当前用户的私有提示词（内部函数）。

    Args:
        db: 请求级异步数据库会话。
        prompt_id: 提示词 ID。
        user: 当前用户。

    Returns:
        Prompt: 提示词 ORM 对象。

    Raises:
        HTTPException: 404 —— 不存在、非本人或为公共提示词。
    """
    result = await db.execute(
        select(Prompt).where(Prompt.id == prompt_id, Prompt.user_id == user.id)
    )
    prompt = result.scalar_one_or_none()
    if prompt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="提示词不存在")
    return prompt


@router.get("", response_model=PageResult[PromptOut])
async def list_prompts(
    scope: str = Query(default="public"),
    capability: Optional[str] = Query(default=None, max_length=20),
    category: Optional[str] = Query(default=None, max_length=30),
    search: Optional[str] = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PageResult[PromptOut]:
    """分页浏览提示词库。

    scope=public：平台公共库（user_id IS NULL 且 scope='public'）；
    scope=private：当前用户私有提示词（user_id=me）。

    Args:
        scope: 范围（public / private，非法返回 400）。
        capability: 按适用能力过滤（可空）。
        category: 按分类过滤（可空）。
        search: 按标题/内容模糊搜索（可空）。
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PageResult[PromptOut]: 提示词分页列表（创建时间倒序）。

    Raises:
        HTTPException: 400 —— scope 取值非法；401 —— 未认证。
    """
    if scope not in ("public", "private"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="scope 仅支持 public/private")
    page, page_size = normalize_pagination(page, page_size)

    conditions = []
    if scope == "public":
        # 公共库仅返回上架状态（status='published'），管理端下架后用户端不可见
        conditions += [Prompt.user_id.is_(None), Prompt.scope == "public", Prompt.status == "published"]
    else:
        conditions += [Prompt.user_id == user.id, Prompt.scope == "private"]
    if capability:
        conditions.append(Prompt.capability == capability)
    if category:
        conditions.append(Prompt.category == category)
    if search:
        conditions.append(or_(Prompt.title.ilike(f"%{search}%"), Prompt.content.ilike(f"%{search}%")))

    total = await db.scalar(select(func.count()).select_from(Prompt).where(*conditions))
    rows = await db.execute(
        select(Prompt).where(*conditions)
        .order_by(Prompt.created_at.desc(), Prompt.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return PageResult(
        total=total or 0, page=page, page_size=page_size,
        items=[PromptOut.model_validate(r) for r in rows.scalars().all()],
    )


@router.post("", response_model=PromptOut)
async def create_prompt(
    req: PromptCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PromptOut:
    """新建私有提示词（scope='private'，归属当前用户）。

    Args:
        req: 提示词创建请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PromptOut: 新建的提示词。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）。
    """
    prompt = Prompt(
        user_id=user.id,
        scope="private",
        capability=req.capability,
        title=req.title.strip(),
        content=req.content,
        category=req.category,
        tags=req.tags,
    )
    db.add(prompt)
    await db.commit()
    return PromptOut.model_validate(prompt)


@router.put("/{prompt_id}", response_model=PromptOut)
async def update_prompt(
    prompt_id: int,
    req: PromptUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PromptOut:
    """更新自己的私有提示词（仅更新传入字段）。

    Args:
        prompt_id: 提示词 ID。
        req: 更新请求（title/content/capability/category/tags）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PromptOut: 更新后的提示词。

    Raises:
        HTTPException: 404 —— 不存在或非本人提示词。
    """
    prompt = await _get_owned_prompt(db, prompt_id, user)
    for field in ("title", "content", "capability", "category", "tags"):
        value = getattr(req, field)
        if value is not None:
            setattr(prompt, field, value if field != "title" else value.strip())
    await db.commit()
    return PromptOut.model_validate(prompt)


@router.delete("/{prompt_id}")
async def delete_prompt(
    prompt_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除自己的私有提示词（物理删除）。

    Args:
        prompt_id: 提示词 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"message": "提示词已删除"}。

    Raises:
        HTTPException: 404 —— 不存在或非本人提示词。
    """
    prompt = await _get_owned_prompt(db, prompt_id, user)
    await db.delete(prompt)
    await db.commit()
    return {"message": "提示词已删除"}


@router.post("/import", response_model=PromptImportOut)
async def import_prompts(
    req: PromptImportRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PromptImportOut:
    """批量导入提示词（全部导入为当前用户的私有提示词）。

    Args:
        req: 导入请求（prompts 列表，单次上限 200 条）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PromptImportOut: 成功导入的条数。

    Raises:
        HTTPException: 401 —— 未认证（由依赖抛出）。
    """
    for item in req.prompts:
        db.add(Prompt(
            user_id=user.id,
            scope="private",
            capability=item.capability,
            title=item.title.strip(),
            content=item.content,
            category=item.category,
            tags=item.tags,
        ))
    await db.commit()
    return PromptImportOut(imported=len(req.prompts))
