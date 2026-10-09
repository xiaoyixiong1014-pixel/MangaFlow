"""资产域业务服务模块（文件级说明）。

承载四类资产（角色 / 场景 / 道具 / 风格）的 CRUD 编排、默认风格互斥、
角色定妆照生成任务（计费冻结 + 任务入队，RD-28）、面部锁定快照、
资产变体（RD-14 / RD-19）、一致性占位校验与跨项目资产总览。

路由层（app/api/assets.py）只做参数校验与编排，事务细节统一收敛在本模块。
"""

from uuid import uuid4
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AssetVariant,
    Character,
    Project,
    Prop,
    Scene,
    Style,
    Task,
    VersionSnapshot,
)
from app.schemas.asset import (
    CHARACTER_VARIANT_TYPES,
    PROP_VARIANT_TYPES,
    SCENE_VARIANT_TYPES,
)
from app.services.generation_service import freeze_and_create_task
from app.services.script_service import DEFAULT_VISUAL_TEMPLATE, get_owned_project

# 角色定妆照生成单价（积分/张，与 generation_service.IMAGE_COST_PER_FRAME 保持一致）
CHARACTER_IMAGE_COST = 10

# 各资产类型 -> (ORM 类, 允许的变体类型集合)
_ASSET_CLS = {"character": Character, "scene": Scene, "prop": Prop}
_VARIANT_TYPES = {
    "character": CHARACTER_VARIANT_TYPES,
    "scene": SCENE_VARIANT_TYPES,
    "prop": PROP_VARIANT_TYPES,
}


async def get_asset_or_404(
    db: AsyncSession,
    project_id: int,
    asset_type: str,
    asset_id: int,
    include_deleted: bool = False,
) -> Any:
    """获取属于指定项目的资产对象，不存在时抛 404。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。
        asset_type: 资产类型 character / scene / prop / style。
        asset_id: 资产 ID。
        include_deleted: 是否包含已软删除行（编辑已删资产时传 True 可恢复语义）。

    Returns:
        Any: 对应资产 ORM 对象（Character / Scene / Prop / Style）。

    Raises:
        HTTPException: 404 —— 资产不存在、不属于该项目或已软删除。
    """
    cls = _ASSET_CLS.get(asset_type)
    if cls is None:
        if asset_type == "style":
            cls = Style  # 风格资产无软删字段，独立处理
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="资产类型非法")
    conds = [cls.project_id == project_id, cls.id == asset_id]
    if asset_type != "style" and not include_deleted:
        conds.append(cls.deleted_at.is_(None))  # 软删行默认不可见
    row = (await db.execute(select(cls).where(*conds))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{asset_type} 资产不存在")
    return row


async def ensure_unique_name(
    db: AsyncSession, project_id: int, asset_type: str, name: str, exclude_id: Optional[int] = None
) -> None:
    """校验项目内资产名唯一（软删行除外）。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。
        asset_type: 资产类型。
        name: 待校验名称。
        exclude_id: 更新场景下排除自身 ID。

    Raises:
        HTTPException: 409 —— 同名资产已存在。
    """
    cls = _ASSET_CLS.get(asset_type)
    if cls is None:
        return
    conds = [cls.project_id == project_id, cls.name == name, cls.deleted_at.is_(None)]
    if exclude_id:
        conds.append(cls.id != exclude_id)
    existed = (
        await db.execute(select(func.count()).select_from(cls).where(*conds))
    ).scalar_one_or_none()
    if existed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"同名{asset_type}资产已存在")


async def set_default_style(db: AsyncSession, project_id: int, style: Style) -> None:
    """将指定风格设为项目默认：先把该项目其他默认风格清掉（uk_styles_default
    部分唯一索引要求项目内至多一个默认，故采用「先清后设」的互斥写法）。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。
        style: 要置默认的风格 ORM 对象。
    """
    others = (
        await db.execute(
            select(Style).where(
                Style.project_id == project_id,
                Style.is_default.is_(True),
                Style.id != style.id,
            )
        )
    ).scalars().all()
    for row in others:
        row.is_default = False  # 清掉其他默认（部分唯一索引互斥要求）
    style.is_default = True


async def create_character_image_task(
    db: AsyncSession, project: Project, character: Character, prompt: Optional[str]
) -> Dict[str, Any]:
    """角色定妆照生成：冻结积分 + 建任务入队 + 置 generating 状态。

    计费与任务约定：
    - cost = 10 积分/张；
    - 幂等键 img_{character_id}_{uuid8}，同时作为任务幂等键与冻结流水幂等键；
    - 任务 meta 携带 target_type/target_id/prompt（tasks.meta JSONB 字段，
      任务执行器 runner 据此回写资产状态）。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        character: 角色 ORM 对象。
        prompt: 覆盖用提示词（空则用角色 visual_prompt）。

    Returns:
        Dict[str, Any]: 落库后的任务字典（id/type/phase/status/cost_points 等）。

    Raises:
        HTTPException: 409 —— 积分余额不足；400 —— 参数非法（透传自 billing_service）。
    """
    final_prompt = (prompt or character.visual_prompt or "").strip()
    idem = f"img_{character.id}_{uuid4().hex[:8]}"
    task = await freeze_and_create_task(
        db,
        project=project,
        task_type="image",
        phase="full",
        cost_points=CHARACTER_IMAGE_COST,
        meta={"target_type": "character", "target_id": character.id, "prompt": final_prompt},
        idempotency_key=idem,
    )
    # 冻结成功后再置生成状态（freeze_and_create_task 内部已提交事务）
    character.generation_status = "generating"
    await db.commit()
    return {
        "id": task.id,
        "project_id": task.project_id,
        "type": task.type,
        "phase": task.phase,
        "status": task.status,
        "cost_points": task.cost_points,
        "idempotency_key": task.idempotency_key,
        "meta": task.meta,
        "progress": task.progress,
    }


async def create_variant(
    db: AsyncSession,
    project_id: int,
    owner_type: str,
    owner_id: int,
    req: Any,
) -> AssetVariant:
    """创建资产变体（RD-14 显式继承声明 / RD-19 场景光照变体）。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID（用于归属校验）。
        owner_type: 归属类型 character / scene / prop。
        owner_id: 归属资产 ID。
        req: VariantCreateRequest（variant_type/name/inherit_mask/visual_prompt/is_active）。

    Returns:
        AssetVariant: 新建变体 ORM 对象（已提交）。

    Raises:
        HTTPException: 400 —— inherit_mask 缺失或 variant_type 不被该资产类型允许；
            404 —— 归属资产不存在。
    """
    await get_asset_or_404(db, project_id, owner_type, owner_id)
    # inherit_mask 必填（RD-14：显式继承/独立声明，禁止隐式默认）
    if not req.inherit_mask or not isinstance(req.inherit_mask, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="inherit_mask 必填（dict）")
    allowed = _VARIANT_TYPES[owner_type]
    if req.variant_type not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{owner_type} 变体类型仅允许 {'/'.join(allowed)}",
        )
    variant = AssetVariant(
        owner_type=owner_type,
        owner_id=owner_id,
        variant_type=req.variant_type,
        name=req.name.strip(),
        inherit_mask=req.inherit_mask,
        visual_prompt=req.visual_prompt,
        is_active=req.is_active,
    )
    db.add(variant)
    await db.commit()
    await db.refresh(variant)
    return variant


async def list_variants(
    db: AsyncSession, project_id: int, owner_type: str, owner_id: int
) -> List[AssetVariant]:
    """列出某资产的全部变体（按创建时间升序）。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID（用于归属校验）。
        owner_type: 归属类型。
        owner_id: 归属资产 ID。

    Returns:
        List[AssetVariant]: 变体列表。

    Raises:
        HTTPException: 404 —— 归属资产不存在。
    """
    await get_asset_or_404(db, project_id, owner_type, owner_id)
    rows = (
        await db.execute(
            select(AssetVariant)
            .where(AssetVariant.owner_type == owner_type, AssetVariant.owner_id == owner_id)
            .order_by(AssetVariant.id.asc())
        )
    ).scalars().all()
    return list(rows)


def consistency_check(character_id: int, variant_id: Optional[int]) -> Dict[str, Any]:
    """一致性占位校验（mock）：以资产 ID 派生确定性伪相似度，不依赖真实向量。

    伪相似度公式：0.88 + (id * 7 % 12) / 100，范围 [0.88, 0.99]，
    同一 ID 恒定返回同值（mock，后续接入 feature_vector 余弦相似度替换）。

    Args:
        character_id: 角色资产 ID。
        variant_id: 变体 ID（可空；仅写入报告文本，不参与计算）。

    Returns:
        Dict[str, Any]: {"similarity", "passed"(>=0.85), "report"}。
    """
    similarity = round(0.88 + (character_id * 7 % 12) / 100, 2)  # 确定性伪相似度（mock）
    passed = similarity >= 0.85
    target = f"变体 {variant_id}" if variant_id else "主形象"
    report = (
        f"角色 {character_id} 与{target}的一致性校验（占位算法）："
        f"伪相似度 {similarity}，{'通过' if passed else '未通过'}（阈值 0.85）。"
        "正式实现将基于面部特征向量余弦相似度。"
    )
    return {"similarity": similarity, "passed": passed, "report": report}


async def cross_project_assets(
    db: AsyncSession,
    user_id: int,
    search: Optional[str],
    project_id: Optional[int],
    page: int,
    page_size: int,
) -> Tuple[List[Dict[str, Any]], int]:
    """跨项目资产总览：当前用户全部项目的 characters + scenes 联合查询（含项目标题）。

    Args:
        db: 异步数据库会话。
        user_id: 当前用户 ID。
        search: 名称/描述模糊搜索关键字（可空）。
        project_id: 项目过滤（可空；仅搜索该用户自己的项目）。
        page: 页码（>=1）。
        page_size: 每页条数（1~100）。

    Returns:
        Tuple[List[Dict[str, Any]], int]: (当前页行列表, 总数)。
    """
    # 公共过滤条件：项目归属当前用户且未删除
    conds = [Project.user_id == user_id, Project.deleted_at.is_(None)]
    if project_id:
        conds.append(Project.id == project_id)

    like = f"%{search}%" if search else None
    char_conds = [Character.deleted_at.is_(None)]
    scene_conds: list = []
    if like:
        char_conds.append(Character.name.ilike(like) | Character.description.ilike(like))
        scene_conds.append(Scene.name.ilike(like) | Scene.description.ilike(like))

    # characters 分支
    char_part = (
        select(
            Character.id.label("id"),
            literal("character").label("asset_type"),
            Character.project_id.label("project_id"),
            Project.title.label("project_title"),
            Character.name.label("name"),
            Character.description.label("description"),
            Character.visual_prompt.label("visual_prompt"),
            Character.asset_url.label("asset_url"),
            Character.created_at.label("created_at"),
        )
        .join(Project, Project.id == Character.project_id)
        .where(*conds, *char_conds)
    )
    # scenes 分支（列对齐 union）
    scene_part = (
        select(
            Scene.id.label("id"),
            literal("scene").label("asset_type"),
            Scene.project_id.label("project_id"),
            Project.title.label("project_title"),
            Scene.name.label("name"),
            Scene.description.label("description"),
            Scene.visual_prompt.label("visual_prompt"),
            Scene.asset_url.label("asset_url"),
            Scene.created_at.label("created_at"),
        )
        .join(Project, Project.id == Scene.project_id)
        .where(*conds, *scene_conds)
    )

    # 总数 = 两类 count 相加
    n_char = await db.scalar(
        select(func.count()).select_from(char_part.subquery())
    )
    n_scene = await db.scalar(
        select(func.count()).select_from(scene_part.subquery())
    )
    total = (n_char or 0) + (n_scene or 0)

    # union 后统一排序分页（子查询化再查询）
    sub = char_part.union_all(scene_part).subquery()
    rows = await db.execute(
        select(sub)
        .order_by(sub.c.created_at.desc(), sub.c.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [dict(r._mapping) for r in rows.all()]
    return items, total


async def lock_character(db: AsyncSession, project_id: int, character: Character) -> Character:
    """锁定角色面部层（RD-12）：face_locked=True 并写版本快照（stage='asset_locked'）。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。
        character: 角色 ORM 对象。

    Returns:
        Character: 更新后的角色 ORM 对象（已提交）。
    """
    character.face_locked = True
    db.add(VersionSnapshot(
        project_id=project_id,
        stage="asset_locked",
        summary={"character_id": character.id, "name": character.name},
    ))
    await db.commit()
    await db.refresh(character)
    return character


async def unlock_character(db: AsyncSession, character: Character) -> Character:
    """解锁角色面部层（RD-12）：face_locked=False。

    Args:
        db: 异步数据库会话。
        character: 角色 ORM 对象。

    Returns:
        Character: 更新后的角色 ORM 对象（已提交）。
    """
    character.face_locked = False
    await db.commit()
    await db.refresh(character)
    return character
