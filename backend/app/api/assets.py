"""资产管理路由模块（文件级说明）。

实现资产域全部接口（每个接口先校验项目归属，他人或已删除项目一律 404）：
- GET    /api/characters                                        跨项目资产总览（characters+scenes）
- GET    /api/projects/{project_id}/assets                      四类资产聚合 / 按 type 分页
- POST   /api/projects/{project_id}/assets/characters           角色创建（scenes/props/styles 同构）
- PUT    /api/projects/{project_id}/assets/characters/{id}      角色更新
- DELETE /api/projects/{project_id}/assets/characters/{id}      角色软删除
- POST   /api/projects/{project_id}/assets/characters/{id}/generate   定妆照生成（计费+任务入队）
- POST   /api/projects/{project_id}/assets/characters/{id}/lock       面部锁定（RD-12）
- POST   /api/projects/{project_id}/assets/characters/{id}/unlock     面部解锁
- POST   /api/projects/{project_id}/assets/characters/{id}/variants   角色变体（RD-14）
- GET    /api/projects/{project_id}/assets/characters/{id}/variants   变体列表
- ...    /assets/scenes|props|styles 同构（scenes 变体为 lighting，RD-19）
- POST   /api/projects/{project_id}/assets/consistency-check    一致性校验（embedding 余弦真实算法）
- GET    /api/assets/search                                     资产语义搜索（跨自己项目 TopK）

注意：路由需携带 {project_id} 路径参数，FastAPI router prefix 不支持路径参数，
因此本路由不设 prefix，路由内部书写完整路径。
"""

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Character, Project, Prop, Scene, Style, User
from app.schemas.asset import (
    AssetSearchItem,
    AssetSearchOut,
    AssetsGroupOut,
    CharacterCreateRequest,
    CharacterGenerateRequest,
    CharacterOut,
    CharacterUpdateRequest,
    ConsistencyCheckOut,
    ConsistencyCheckRequest,
    CrossProjectAssetOut,
    PropCreateRequest,
    PropOut,
    PropUpdateRequest,
    SceneCreateRequest,
    SceneOut,
    SceneUpdateRequest,
    StyleCreateRequest,
    StyleOut,
    StyleUpdateRequest,
    VariantCreateRequest,
    VariantOut,
)
from app.services import normalize_pagination
from app.services.asset_service import (
    consistency_check,
    create_character_image_task,
    create_variant,
    ensure_unique_name,
    get_asset_or_404,
    get_owned_project,
    list_variants,
    lock_character,
    cross_project_assets,
    semantic_search_assets,
    set_default_style,
    unlock_character,
    upsert_asset_embedding,
)
from app.services.script_service import DEFAULT_VISUAL_TEMPLATE

router = APIRouter(tags=["assets"])

# GET /assets 支持的 type 过滤取值
_ASSET_TYPES = ("character", "scene", "prop", "style")


@router.get("/api/characters", response_model=None)
async def list_cross_project_characters(
    search: Optional[str] = Query(default=None, max_length=100),
    project_id: Optional[int] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """跨项目资产总览：当前用户全部项目的 characters + scenes 联合查询（含项目标题）。

    Args:
        search: 名称/描述模糊搜索关键字（可空）。
        project_id: 项目过滤（可空；仅匹配该用户自己的项目）。
        page: 页码（从 1 开始）。
        page_size: 每页条数（1~100）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"items": [...], "total", "page", "page_size"}。

    Raises:
        HTTPException: 401 —— 未认证。
    """
    page, page_size = normalize_pagination(page, page_size)
    items, total = await cross_project_assets(db, user.id, search, project_id, page, page_size)
    return {
        "items": [CrossProjectAssetOut.model_validate(i) for i in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/api/projects/{project_id}/assets")
async def list_assets(
    project_id: int,
    type: Optional[str] = Query(default=None, alias="type"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """查询项目资产：type 缺省返回四类聚合；指定 type 返回分页列表。

    Args:
        project_id: 项目 ID。
        type: 资产类型 character/scene/prop/style（可空）。
        page: 页码（仅指定 type 时生效）。
        page_size: 每页条数（仅指定 type 时生效）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: type 缺省 → {"characters","scenes","props","styles"}；
            指定 type → {"items","total","page","page_size"}。

    Raises:
        HTTPException: 400 —— type 取值非法；404 —— 项目不存在/非本人项目。
    """
    project = await get_owned_project(db, project_id, user.id)
    page, page_size = normalize_pagination(page, page_size)

    if type is None:
        # 四类资产聚合返回（各自全量，软删行除外）
        chars = (
            await db.execute(
                select(Character)
                .where(Character.project_id == project.id, Character.deleted_at.is_(None))
                .order_by(Character.id.asc())
            )
        ).scalars().all()
        scenes = (
            await db.execute(
                select(Scene).where(Scene.project_id == project.id).order_by(Scene.id.asc())
            )
        ).scalars().all()
        props = (
            await db.execute(
                select(Prop)
                .where(Prop.project_id == project.id, Prop.deleted_at.is_(None))
                .order_by(Prop.id.asc())
            )
        ).scalars().all()
        styles = (
            await db.execute(
                select(Style).where(Style.project_id == project.id).order_by(Style.id.asc())
            )
        ).scalars().all()
        group = AssetsGroupOut(
            characters=[CharacterOut.model_validate(r) for r in chars],
            scenes=[SceneOut.model_validate(r) for r in scenes],
            props=[PropOut.model_validate(r) for r in props],
            styles=[StyleOut.model_validate(r) for r in styles],
        )
        return group.model_dump()

    if type not in _ASSET_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="type 仅支持 character/scene/prop/style")

    cls = {"character": Character, "scene": Scene, "prop": Prop, "style": Style}[type]
    out_cls = {"character": CharacterOut, "scene": SceneOut, "prop": PropOut, "style": StyleOut}[type]
    conds = [cls.project_id == project.id]
    if type != "style":
        conds.append(cls.deleted_at.is_(None))  # style 无软删字段
    total = (
        await db.execute(select(func.count()).select_from(cls).where(*conds))
    ).scalar_one_or_none() or 0
    rows = (
        await db.execute(
            select(cls).where(*conds).order_by(cls.id.asc())
            .offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return {
        "items": [out_cls.model_validate(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# 角色资产 CRUD
# ---------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/assets/characters", response_model=CharacterOut)
async def create_character(
    project_id: int,
    req: CharacterCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CharacterOut:
    """创建角色资产（项目内同名 409；visual_prompt 缺省用中文模板）。

    Args:
        project_id: 项目 ID。
        req: 角色创建请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        CharacterOut: 新建角色信息。

    Raises:
        HTTPException: 404 —— 项目不存在；409 —— 同名角色已存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    await ensure_unique_name(db, project.id, "character", req.name.strip())
    character = Character(
        project_id=project.id,
        name=req.name.strip(),
        aliases=req.aliases or [],
        description=req.description,
        # visual_prompt 缺省模板（A-06 标准化提示词）
        visual_prompt=req.visual_prompt or DEFAULT_VISUAL_TEMPLATE.format(
            name=req.name.strip(), description=req.description or req.name.strip()),
    )
    db.add(character)
    await db.commit()
    await db.refresh(character)
    # 向量化接线（Task 6.1）：创建资产后写入 asset_embeddings（内部吞异常，不影响主流程）
    await upsert_asset_embedding(
        db, "character", character.id,
        " ".join(x for x in (character.name, character.description, character.visual_prompt) if x),
    )
    return CharacterOut.model_validate(character)


@router.put("/api/projects/{project_id}/assets/characters/{character_id}", response_model=CharacterOut)
async def update_character(
    project_id: int,
    character_id: int,
    req: CharacterUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CharacterOut:
    """更新角色资产（仅更新传入字段；改名时校验项目内唯一）。

    Args:
        project_id: 项目 ID。
        character_id: 角色 ID。
        req: 更新请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        CharacterOut: 更新后的角色信息。

    Raises:
        HTTPException: 404 —— 项目/角色不存在；409 —— 新名与既有角色冲突。
    """
    project = await get_owned_project(db, project_id, user.id)
    character = await get_asset_or_404(db, project.id, "character", character_id)
    if req.name is not None and req.name.strip() != character.name:
        await ensure_unique_name(db, project.id, "character", req.name.strip(), exclude_id=character.id)
    if req.name is not None:
        character.name = req.name.strip()
    if req.description is not None:
        character.description = req.description
    if req.visual_prompt is not None:
        character.visual_prompt = req.visual_prompt
    if req.aliases is not None:
        character.aliases = req.aliases
    await db.commit()
    await db.refresh(character)
    # 向量化接线（Task 6.1）：visual_prompt/description 变更后刷新资产向量（幂等 upsert）
    await upsert_asset_embedding(
        db, "character", character.id,
        " ".join(x for x in (character.name, character.description, character.visual_prompt) if x),
    )
    return CharacterOut.model_validate(character)


@router.delete("/api/projects/{project_id}/assets/characters/{character_id}")
async def delete_character(
    project_id: int,
    character_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """软删除角色（deleted_at=now，可恢复；分镜关联引用不受影响）。

    Args:
        project_id: 项目 ID。
        character_id: 角色 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"message": "角色已删除"}。

    Raises:
        HTTPException: 404 —— 项目/角色不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    character = await get_asset_or_404(db, project.id, "character", character_id)
    character.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "角色已删除"}


# ---------------------------------------------------------------------------
# 角色生成 / 锁定 / 变体
# ---------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/assets/characters/{character_id}/generate")
async def generate_character_image(
    project_id: int,
    character_id: int,
    req: Optional[CharacterGenerateRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """角色定妆照生成：冻结 10 积分（RD-28）+ 建 image 任务入队 + 置 generating。

    Args:
        project_id: 项目 ID。
        character_id: 角色 ID。
        req: 生成请求（prompt 可缺省，缺省用角色 visual_prompt）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: 任务字典（id/type/phase/status/cost_points/idempotency_key/meta/progress）。

    Raises:
        HTTPException: 404 —— 项目/角色不存在；409 —— 积分余额不足。
    """
    project = await get_owned_project(db, project_id, user.id)
    character = await get_asset_or_404(db, project.id, "character", character_id)
    body = req or CharacterGenerateRequest()
    return await create_character_image_task(db, project, character, body.prompt)


@router.post("/api/projects/{project_id}/assets/characters/{character_id}/lock", response_model=CharacterOut)
async def lock_character_face(
    project_id: int,
    character_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CharacterOut:
    """锁定角色面部层（RD-12）：face_locked=True 并写版本快照（stage='asset_locked'）。

    Args:
        project_id: 项目 ID。
        character_id: 角色 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        CharacterOut: 更新后的角色信息。

    Raises:
        HTTPException: 404 —— 项目/角色不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    character = await get_asset_or_404(db, project.id, "character", character_id)
    return CharacterOut.model_validate(await lock_character(db, project.id, character))


@router.post("/api/projects/{project_id}/assets/characters/{character_id}/unlock", response_model=CharacterOut)
async def unlock_character_face(
    project_id: int,
    character_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CharacterOut:
    """解锁角色面部层（RD-12）：face_locked=False。

    Args:
        project_id: 项目 ID。
        character_id: 角色 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        CharacterOut: 更新后的角色信息。

    Raises:
        HTTPException: 404 —— 项目/角色不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    character = await get_asset_or_404(db, project.id, "character", character_id)
    return CharacterOut.model_validate(await unlock_character(db, character))


@router.post("/api/projects/{project_id}/assets/characters/{character_id}/variants", response_model=VariantOut)
async def create_character_variant(
    project_id: int,
    character_id: int,
    req: VariantCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VariantOut:
    """创建角色变体（variant_type 限 outfit/age/other；inherit_mask 必填，RD-14）。

    Args:
        project_id: 项目 ID。
        character_id: 角色 ID。
        req: 变体创建请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        VariantOut: 新建变体信息。

    Raises:
        HTTPException: 400 —— inherit_mask 缺失或 variant_type 非法；
            404 —— 项目/角色不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    variant = await create_variant(db, project.id, "character", character_id, req)
    return VariantOut.model_validate(variant)


@router.get("/api/projects/{project_id}/assets/characters/{character_id}/variants", response_model=List[VariantOut])
async def list_character_variants(
    project_id: int,
    character_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[VariantOut]:
    """列出角色全部变体。

    Args:
        project_id: 项目 ID。
        character_id: 角色 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        List[VariantOut]: 变体列表。

    Raises:
        HTTPException: 404 —— 项目/角色不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    rows = await list_variants(db, project.id, "character", character_id)
    return [VariantOut.model_validate(r) for r in rows]


# ---------------------------------------------------------------------------
# 场景资产 CRUD 与变体（RD-19 光照变体）
# ---------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/assets/scenes", response_model=SceneOut)
async def create_scene(
    project_id: int,
    req: SceneCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SceneOut:
    """创建场景资产（项目内同名 409）。

    Args:
        project_id: 项目 ID。
        req: 场景创建请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        SceneOut: 新建场景信息。

    Raises:
        HTTPException: 404 —— 项目不存在；409 —— 同名场景已存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    await ensure_unique_name(db, project.id, "scene", req.name.strip())
    scene = Scene(
        project_id=project.id,
        name=req.name.strip(),
        description=req.description,
        visual_prompt=req.visual_prompt or DEFAULT_VISUAL_TEMPLATE.format(
            name=req.name.strip(), description=req.description or req.name.strip()),
    )
    db.add(scene)
    await db.commit()
    await db.refresh(scene)
    # 向量化接线（Task 6.1）：创建资产后写入 asset_embeddings
    await upsert_asset_embedding(
        db, "scene", scene.id,
        " ".join(x for x in (scene.name, scene.description, scene.visual_prompt) if x),
    )
    return SceneOut.model_validate(scene)


@router.put("/api/projects/{project_id}/assets/scenes/{scene_id}", response_model=SceneOut)
async def update_scene(
    project_id: int,
    scene_id: int,
    req: SceneUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SceneOut:
    """更新场景资产（仅更新传入字段）。

    Args:
        project_id: 项目 ID。
        scene_id: 场景 ID。
        req: 更新请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        SceneOut: 更新后的场景信息。

    Raises:
        HTTPException: 404 —— 项目/场景不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    scene = await get_asset_or_404(db, project.id, "scene", scene_id)
    if req.name is not None:
        await ensure_unique_name(db, project.id, "scene", req.name.strip(), exclude_id=scene.id)
        scene.name = req.name.strip()
    if req.description is not None:
        scene.description = req.description
    if req.visual_prompt is not None:
        scene.visual_prompt = req.visual_prompt
    if req.is_locked is not None:
        scene.is_locked = req.is_locked
    await db.commit()
    await db.refresh(scene)
    # 向量化接线（Task 6.1）：visual_prompt/description 变更后刷新场景向量
    await upsert_asset_embedding(
        db, "scene", scene.id,
        " ".join(x for x in (scene.name, scene.description, scene.visual_prompt) if x),
    )
    return SceneOut.model_validate(scene)


@router.delete("/api/projects/{project_id}/assets/scenes/{scene_id}")
async def delete_scene(
    project_id: int,
    scene_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """软删除场景（deleted_at=now）。

    Args:
        project_id: 项目 ID。
        scene_id: 场景 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"message": "场景已删除"}。

    Raises:
        HTTPException: 404 —— 项目/场景不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    scene = await get_asset_or_404(db, project.id, "scene", scene_id)
    scene.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "场景已删除"}


@router.post("/api/projects/{project_id}/assets/scenes/{scene_id}/variants", response_model=VariantOut)
async def create_scene_variant(
    project_id: int,
    scene_id: int,
    req: VariantCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VariantOut:
    """创建场景光照变体（RD-19，variant_type 限 lighting）。

    Args:
        project_id: 项目 ID。
        scene_id: 场景 ID。
        req: 变体创建请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        VariantOut: 新建变体信息。

    Raises:
        HTTPException: 400 —— inherit_mask 缺失或 variant_type 非法；
            404 —— 项目/场景不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    variant = await create_variant(db, project.id, "scene", scene_id, req)
    return VariantOut.model_validate(variant)


@router.get("/api/projects/{project_id}/assets/scenes/{scene_id}/variants", response_model=List[VariantOut])
async def list_scene_variants(
    project_id: int,
    scene_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[VariantOut]:
    """列出场景全部变体。

    Args:
        project_id: 项目 ID。
        scene_id: 场景 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        List[VariantOut]: 变体列表。

    Raises:
        HTTPException: 404 —— 项目/场景不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    rows = await list_variants(db, project.id, "scene", scene_id)
    return [VariantOut.model_validate(r) for r in rows]


# ---------------------------------------------------------------------------
# 道具资产 CRUD 与变体
# ---------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/assets/props", response_model=PropOut)
async def create_prop(
    project_id: int,
    req: PropCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PropOut:
    """创建道具资产。

    Args:
        project_id: 项目 ID。
        req: 道具创建请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PropOut: 新建道具信息。

    Raises:
        HTTPException: 404 —— 项目不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    prop = Prop(
        project_id=project.id,
        name=req.name.strip(),
        category=req.category,
        description=req.description,
        visual_prompt=req.visual_prompt or DEFAULT_VISUAL_TEMPLATE.format(
            name=req.name.strip(), description=req.description or req.name.strip()),
    )
    db.add(prop)
    await db.commit()
    await db.refresh(prop)
    # 向量化接线（Task 6.1）：创建资产后写入 asset_embeddings
    await upsert_asset_embedding(
        db, "prop", prop.id,
        " ".join(x for x in (prop.name, prop.category, prop.description, prop.visual_prompt) if x),
    )
    return PropOut.model_validate(prop)


@router.put("/api/projects/{project_id}/assets/props/{prop_id}", response_model=PropOut)
async def update_prop(
    project_id: int,
    prop_id: int,
    req: PropUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PropOut:
    """更新道具资产（仅更新传入字段）。

    Args:
        project_id: 项目 ID。
        prop_id: 道具 ID。
        req: 更新请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        PropOut: 更新后的道具信息。

    Raises:
        HTTPException: 404 —— 项目/道具不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    prop = await get_asset_or_404(db, project.id, "prop", prop_id)
    for field in ("name", "category", "description", "visual_prompt"):
        value = getattr(req, field)
        if value is not None:
            setattr(prop, field, value.strip() if field == "name" else value)
    if req.is_locked is not None:
        prop.is_locked = req.is_locked
    await db.commit()
    await db.refresh(prop)
    # 向量化接线（Task 6.1）：visual_prompt/description 变更后刷新道具向量
    await upsert_asset_embedding(
        db, "prop", prop.id,
        " ".join(x for x in (prop.name, prop.category, prop.description, prop.visual_prompt) if x),
    )
    return PropOut.model_validate(prop)


@router.delete("/api/projects/{project_id}/assets/props/{prop_id}")
async def delete_prop(
    project_id: int,
    prop_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """软删除道具（deleted_at=now）。

    Args:
        project_id: 项目 ID。
        prop_id: 道具 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"message": "道具已删除"}。

    Raises:
        HTTPException: 404 —— 项目/道具不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    prop = await get_asset_or_404(db, project.id, "prop", prop_id)
    prop.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "道具已删除"}


@router.post("/api/projects/{project_id}/assets/props/{prop_id}/variants", response_model=VariantOut)
async def create_prop_variant(
    project_id: int,
    prop_id: int,
    req: VariantCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VariantOut:
    """创建道具变体（variant_type 限 other）。

    Args:
        project_id: 项目 ID。
        prop_id: 道具 ID。
        req: 变体创建请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        VariantOut: 新建变体信息。

    Raises:
        HTTPException: 400 —— inherit_mask 缺失或 variant_type 非法；
            404 —— 项目/道具不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    variant = await create_variant(db, project.id, "prop", prop_id, req)
    return VariantOut.model_validate(variant)


@router.get("/api/projects/{project_id}/assets/props/{prop_id}/variants", response_model=List[VariantOut])
async def list_prop_variants(
    project_id: int,
    prop_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[VariantOut]:
    """列出道具全部变体。

    Args:
        project_id: 项目 ID。
        prop_id: 道具 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        List[VariantOut]: 变体列表。

    Raises:
        HTTPException: 404 —— 项目/道具不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    rows = await list_variants(db, project.id, "prop", prop_id)
    return [VariantOut.model_validate(r) for r in rows]


# ---------------------------------------------------------------------------
# 风格资产 CRUD（is_default 项目内互斥）
# ---------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/assets/styles", response_model=StyleOut)
async def create_style(
    project_id: int,
    req: StyleCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StyleOut:
    """创建风格资产；is_default=True 时先清掉项目内其他默认（部分唯一索引互斥）。

    Args:
        project_id: 项目 ID。
        req: 风格创建请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StyleOut: 新建风格信息。

    Raises:
        HTTPException: 404 —— 项目不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    style = Style(
        project_id=project.id,
        name=req.name.strip(),
        preset_code=req.preset_code,
        reference_url=req.reference_url,
        visual_prompt=req.visual_prompt,
        is_default=False,
    )
    db.add(style)
    await db.flush()  # 取 ID 供置默认逻辑使用
    if req.is_default:
        # 先清掉其他默认再设本行为默认（uk_styles_default 互斥要求）
        await set_default_style(db, project.id, style)
    await db.commit()
    await db.refresh(style)
    # 向量化接线（Task 6.1）：创建资产后写入 asset_embeddings
    await upsert_asset_embedding(
        db, "style", style.id,
        " ".join(x for x in (style.name, style.visual_prompt) if x),
    )
    return StyleOut.model_validate(style)


@router.put("/api/projects/{project_id}/assets/styles/{style_id}", response_model=StyleOut)
async def update_style(
    project_id: int,
    style_id: int,
    req: StyleUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StyleOut:
    """更新风格资产；is_default=True 时先清掉项目内其他默认。

    Args:
        project_id: 项目 ID。
        style_id: 风格 ID。
        req: 更新请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StyleOut: 更新后的风格信息。

    Raises:
        HTTPException: 404 —— 项目/风格不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    style = await get_asset_or_404(db, project.id, "style", style_id)
    if req.name is not None:
        style.name = req.name.strip()
    if req.preset_code is not None:
        style.preset_code = req.preset_code
    if req.reference_url is not None:
        style.reference_url = req.reference_url
    if req.visual_prompt is not None:
        style.visual_prompt = req.visual_prompt
    if req.is_default:
        # 先清掉其他默认再设本行为默认（uk_styles_default 互斥要求）
        await set_default_style(db, project.id, style)
    elif req.is_default is False:
        style.is_default = False
    await db.commit()
    await db.refresh(style)
    # 向量化接线（Task 6.1）：visual_prompt 变更后刷新风格向量
    await upsert_asset_embedding(
        db, "style", style.id,
        " ".join(x for x in (style.name, style.visual_prompt) if x),
    )
    return StyleOut.model_validate(style)


# ---------------------------------------------------------------------------
# 一致性校验（embedding 余弦真实算法）
# ---------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/assets/consistency-check", response_model=ConsistencyCheckOut)
async def run_consistency_check(
    project_id: int,
    req: ConsistencyCheckRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConsistencyCheckOut:
    """角色一致性校验（Task 6.2 真实化）：embedding 余弦相似度分项加权综合。

    评分公式：score = round(0.5*visual_prompt_sim + 0.3*description_sim
    + 0.2*locked_bonus, 4)，阈值 0.85；变体未填文本时按继承主形象语义计。
    旧字段 similarity / report 保持兼容，新增 score / threshold / details / algorithm。

    Args:
        project_id: 项目 ID。
        req: 校验请求（character_id 必填，variant_id 可选）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ConsistencyCheckOut: {"similarity","passed","report","score","threshold","details","algorithm"}。

    Raises:
        HTTPException: 404 —— 项目/角色/变体不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    await get_asset_or_404(db, project.id, "character", req.character_id)
    result = await consistency_check(db, req.character_id, req.variant_id)
    return ConsistencyCheckOut.model_validate(result)


# ---------------------------------------------------------------------------
# 资产语义搜索（Task 6.3）
# ---------------------------------------------------------------------------

@router.get("/api/assets/search", response_model=AssetSearchOut)
async def search_assets_semantic(
    q: str = Query(min_length=1, max_length=200, description="查询文本"),
    type: Optional[str] = Query(default=None, alias="type",
                                description="可选类型过滤 character/scene/prop/style"),
    limit: int = Query(default=10, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetSearchOut:
    """资产语义搜索（登录用户，跨自己项目）：查询文本向量化后与 asset_embeddings 余弦 TopK。

    数据量小的当前阶段为全量遍历 + Python 侧余弦计算，pgvector 升级点见
    semantic_search_assets 注释；软删除资产与已删除项目行均被排除。

    Args:
        q: 查询文本。
        type: 可选资产类型过滤（缺省搜索四类）。
        limit: 返回条数上限（1~50，默认 10）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        AssetSearchOut: {"items": [{asset_type, asset_id, name, score, preview}], "total"}。

    Raises:
        HTTPException: 400 —— type 取值非法；401 —— 未认证。
    """
    rows = await semantic_search_assets(db, user.id, q, type, limit)
    items = [AssetSearchItem.model_validate(r) for r in rows]
    return AssetSearchOut(items=items, total=len(items))
