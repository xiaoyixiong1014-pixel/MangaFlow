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
- POST   /api/projects/{project_id}/assets/{asset_type}/batch-delete  批量软删除（A-05，复数类型路由）
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
    AssetBatchDeleteRequest,
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
    create_asset_image_task,
    create_variant,
    ensure_unique_name,
    get_asset_or_404,
    get_owned_project,
    list_variants,
    lock_character,
    cross_project_assets,
    semantic_search_assets,
    set_default_style,
    suggest_asset_prompt,
    unlock_character,
    upsert_asset_embedding,
)
from app.services.script_service import DEFAULT_VISUAL_TEMPLATE

router = APIRouter(tags=["assets"])

# GET /assets 支持的 type 过滤取值
_ASSET_TYPES = ("character", "scene", "prop", "style")

# A-05 批量删除路由的资产类型取值（复数形式，与单删路由 /assets/characters/{id} 同风格）
_BATCH_ASSET_TYPES = ("characters", "scenes", "props", "styles")
# 批量删除：单次允许的 ID 数量上下限
_BATCH_IDS_MIN, _BATCH_IDS_MAX = 1, 100


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
    tag: Optional[str] = Query(default=None, max_length=20,
                               description="按标签过滤（A-05：JSONB 数组包含匹配 @>）"),
    episode_id: Optional[int] = Query(default=None,
                                      description="按集工作流当前集 ID（传值时响应带该集启用名单 episode_enabled）"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """查询项目资产：type 缺省返回四类聚合；指定 type 返回分页列表。

    Args:
        project_id: 项目 ID。
        type: 资产类型 character/scene/prop/style（可空）。
        tag: 标签过滤（A-05，可空；命中条件为 tags JSONB 数组包含该标签）。
        episode_id: 按集工作流当前集 ID（可空；传值时聚合响应额外携带
            episode_enabled={"characters":[名...],...}——该集经「启用资产」确认
            入池的资产名名单，前端按集视图据此过滤，未启用过的集显示空态）。
        page: 页码（仅指定 type 时生效）。
        page_size: 每页条数（仅指定 type 时生效）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: type 缺省 → {"characters","scenes","props","styles"[,"episode_enabled"]}；
            指定 type → {"items","total","page","page_size"}。

    Raises:
        HTTPException: 400 —— type 取值非法；404 —— 项目不存在/非本人项目。
    """
    project = await get_owned_project(db, project_id, user.id)
    page, page_size = normalize_pagination(page, page_size)

    # 按集启用名单（聚合响应附带）：读该集 episodes.enabled_assets，
    # 规范化输出四键（未启用过的集各键为空数组）→ 前端按集视图显示空态
    # （引导「启用资产」）；键缺失与空数组语义不同，必须统一为空数组
    episode_enabled: Optional[dict] = None
    if type is None and episode_id is not None:
        from app.models import Episode  # 局部导入避免模块级导入顺序耦合
        ep = await db.get(Episode, episode_id)
        if ep is not None and ep.project_id == project.id:
            raw = dict(ep.enabled_assets or {})
            episode_enabled = {
                key: list(raw.get(key) or [])
                for key in ("characters", "scenes", "props", "styles")
            }

    if type is None:
        # 四类资产聚合返回（各自全量，软删行除外；tag 过滤对四类同时生效）
        tag_conds = {"characters": Character.tags.contains([tag]),
                     "scenes": Scene.tags.contains([tag]),
                     "props": Prop.tags.contains([tag]),
                     "styles": Style.tags.contains([tag])} if tag else {}
        chars = (
            await db.execute(
                select(Character)
                .where(Character.project_id == project.id, Character.deleted_at.is_(None),
                       # 按集工作流：角色设定确认的角色（enabled_at=NULL）不进资产管理，
                       # 仅显示经「启用资产」确认入池（enabled_at 非空）的角色
                       Character.enabled_at.is_not(None),
                       *([tag_conds["characters"]] if tag_conds else []))
                .order_by(Character.id.asc())
            )
        ).scalars().all()
        scenes = (
            await db.execute(
                select(Scene)
                .where(Scene.project_id == project.id, Scene.deleted_at.is_(None),
                       *([tag_conds["scenes"]] if tag_conds else []))
                .order_by(Scene.id.asc())
            )
        ).scalars().all()
        props = (
            await db.execute(
                select(Prop)
                .where(Prop.project_id == project.id, Prop.deleted_at.is_(None),
                       *([tag_conds["props"]] if tag_conds else []))
                .order_by(Prop.id.asc())
            )
        ).scalars().all()
        styles = (
            await db.execute(
                select(Style)
                .where(Style.project_id == project.id, Style.deleted_at.is_(None),
                       *([tag_conds["styles"]] if tag_conds else []))
                .order_by(Style.id.asc())
            )
        ).scalars().all()
        group = AssetsGroupOut(
            characters=[CharacterOut.model_validate(r) for r in chars],
            scenes=[SceneOut.model_validate(r) for r in scenes],
            props=[PropOut.model_validate(r) for r in props],
            styles=[StyleOut.model_validate(r) for r in styles],
        )
        out = group.model_dump()
        if episode_id is not None:
            out["episode_enabled"] = episode_enabled
        return out

    if type not in _ASSET_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="type 仅支持 character/scene/prop/style")

    cls = {"character": Character, "scene": Scene, "prop": Prop, "style": Style}[type]
    out_cls = {"character": CharacterOut, "scene": SceneOut, "prop": PropOut, "style": StyleOut}[type]
    conds = [cls.project_id == project.id]
    # P2 软删统一：四类资产（含 style）均排除软删行
    conds.append(cls.deleted_at.is_(None))
    if type == "character":
        # 按集工作流：仅显示经「启用资产」确认入池的角色
        conds.append(Character.enabled_at.is_not(None))
    if tag:
        # A-05 标签包含匹配：JSONB @>（等价 tags @> '["tag"]'::jsonb）
        conds.append(cls.tags.contains([tag]))
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
        # 手工创建即启用（按集工作流：enabled_at=NULL 仅用于角色设定确认的流水线数据）
        enabled_at=datetime.now(timezone.utc),
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
    if req.outfit is not None:
        character.outfit = req.outfit
    if req.style_note is not None:
        character.style_note = req.style_note
    if req.aliases is not None:
        character.aliases = req.aliases
    if req.reference_image_url is not None:
        # A-04 参考图锚点：上传替换后回写，生成定妆照时透传图像渠道
        character.reference_image_url = req.reference_image_url
    if req.tags is not None:
        # A-05 标签：数组整体替换（Pydantic 已校验/去重/规范化）
        character.tags = req.tags
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

    create_character_image_task 通用化为 create_asset_image_task 后按类型分派，
    scene/prop 由同构 generate 端点提供生成入口。

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
    return await create_asset_image_task(db, project, "character", character, body.prompt)


@router.post("/api/projects/{project_id}/assets/scenes/{scene_id}/generate")
async def generate_scene_image(
    project_id: int,
    scene_id: int,
    req: Optional[CharacterGenerateRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """场景概念图生成：冻结 10 积分 + 建 image 任务入队（与角色定妆照同构）。

    Args:
        project_id: 项目 ID。
        scene_id: 场景 ID。
        req: 生成请求（prompt 可缺省，缺省用场景 visual_prompt）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: 任务字典（id/type/phase/status/cost_points/idempotency_key/meta/progress）。

    Raises:
        HTTPException: 404 —— 项目/场景不存在；409 —— 积分不足或已有进行中任务。
    """
    project = await get_owned_project(db, project_id, user.id)
    scene = await get_asset_or_404(db, project.id, "scene", scene_id)
    body = req or CharacterGenerateRequest()
    return await create_asset_image_task(db, project, "scene", scene, body.prompt)


@router.post("/api/projects/{project_id}/assets/props/{prop_id}/generate")
async def generate_prop_image(
    project_id: int,
    prop_id: int,
    req: Optional[CharacterGenerateRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """道具标准图生成：冻结 10 积分 + 建 image 任务入队（与角色定妆照同构）。

    Args:
        project_id: 项目 ID。
        prop_id: 道具 ID。
        req: 生成请求（prompt 可缺省，缺省用道具 visual_prompt）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: 任务字典（id/type/phase/status/cost_points/idempotency_key/meta/progress）。

    Raises:
        HTTPException: 404 —— 项目/道具不存在；409 —— 积分不足或已有进行中任务。
    """
    project = await get_owned_project(db, project_id, user.id)
    prop = await get_asset_or_404(db, project.id, "prop", prop_id)
    body = req or CharacterGenerateRequest()
    return await create_asset_image_task(db, project, "prop", prop, body.prompt)


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
    if req.reference_image_url is not None:
        # A-04 参考图锚点：上传替换后回写，生成场景图时透传图像渠道
        scene.reference_image_url = req.reference_image_url
    if req.tags is not None:
        # A-05 标签：数组整体替换（Pydantic 已校验/去重/规范化）
        scene.tags = req.tags
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
    if req.tags is not None:
        # A-05 标签：数组整体替换（Pydantic 已校验/去重/规范化）
        prop.tags = req.tags
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


@router.post("/api/projects/{project_id}/assets/{asset_type}/{asset_id}/suggest-prompt")
async def suggest_prompt(
    project_id: int,
    asset_type: str,
    asset_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """AI 识别场景/道具提示词六要素（agent 从剧本正文提取，用户确认后用于生成图片）。

    仅支持 scene/prop（角色提示词直接取自角色设定，无需识别）：
    - 场景六要素：画风基底 / 空间类型 / 核心锚点(3~5) / 光照氛围 / 构图视角 / 负面约束；
    - 道具六要素：尺度参照 / 核心锚点(3~5) / 材质工艺 / 唯一标记 / 功能操作 / 可读文字政策。

    Args:
        project_id: 项目 ID。
        asset_type: 资产类型 scene/prop。
        asset_id: 资产 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"type": asset_type, "elements": {六要素键: 值}}。

    Raises:
        HTTPException: 400 —— asset_type 非法或剧本正文无相关内容；
            404 —— 项目/资产不存在；502 —— LLM 识别失败。
    """
    if asset_type not in ("scene", "prop"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="仅支持 scene/prop 的提示词识别")
    project = await get_owned_project(db, project_id, user.id)
    asset = await get_asset_or_404(db, project.id, asset_type, asset_id)
    return await suggest_asset_prompt(db, project, asset_type, asset)


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
    if req.tags is not None:
        # A-05 标签：数组整体替换（Pydantic 已校验/去重/规范化）
        style.tags = req.tags
    await db.commit()
    await db.refresh(style)
    # 向量化接线（Task 6.1）：visual_prompt 变更后刷新风格向量
    await upsert_asset_embedding(
        db, "style", style.id,
        " ".join(x for x in (style.name, style.visual_prompt) if x),
    )
    return StyleOut.model_validate(style)


@router.delete("/api/projects/{project_id}/assets/styles/{style_id}")
async def delete_style(
    project_id: int,
    style_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """软删除风格（P2 软删统一后与四类资产单删语义一致：deleted_at=now）。

    Args:
        project_id: 项目 ID。
        style_id: 风格 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"message": "风格已删除"}。

    Raises:
        HTTPException: 404 —— 项目/风格不存在。
    """
    project = await get_owned_project(db, project_id, user.id)
    style = await get_asset_or_404(db, project.id, "style", style_id)
    style.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "风格已删除"}


# ---------------------------------------------------------------------------
# 资产批量删除（A-05）
# ---------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/assets/{asset_type}/batch-delete")
async def batch_delete_assets(
    project_id: int,
    asset_type: str,
    req: AssetBatchDeleteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """批量软删除资产（A-05），逐条复用单删语义。

    与单条删除行为完全一致（单删无引用保护，仅置 deleted_at=now）：
    - 命中（属于当前项目且未软删）→ 软删除计入 deleted；
    - 未命中（不存在/不属于当前项目/已软删）→ 不中断整体操作，计入
      skipped 并给出原因（等价于单删的 404 场景）。
    P2 软删统一：四类资产（含 styles）一律软删除（styles 原物理删除特判移除）。

    Args:
        project_id: 项目 ID。
        asset_type: 资产类型 characters/scenes/props/styles（复数，与单删路由同风格）。
        req: 批量删除请求（ids 去重后 1~100 个，越界 400）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"deleted": 成功删除条数, "skipped": [{"id", "reason"}]}。

    Raises:
        HTTPException: 400 —— asset_type 非法或 ids 数量为 0/超 100；
            404 —— 项目不存在/非本人项目。
    """
    if asset_type not in _BATCH_ASSET_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="asset_type 仅支持 characters/scenes/props/styles")
    # 去重保序后校验数量（1~100，越界 400；空列表按需求返回 400 而非 422）
    ids = list(dict.fromkeys(req.ids))
    if len(ids) < _BATCH_IDS_MIN or len(ids) > _BATCH_IDS_MAX:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="ids 数量须在 1~100 之间")

    project = await get_owned_project(db, project_id, user.id)
    cls = {"characters": Character, "scenes": Scene, "props": Prop, "styles": Style}[asset_type]

    deleted = 0
    skipped: list = []
    now = datetime.now(timezone.utc)
    for asset_id in ids:
        conds = [cls.id == asset_id, cls.project_id == project.id,
                 cls.deleted_at.is_(None)]  # 已软删行视为未命中（单删此时 404）
        row = (await db.execute(select(cls).where(*conds))).scalar_one_or_none()
        if row is None:
            skipped.append({"id": asset_id,
                            "reason": "资产不存在、不属于当前项目或已删除"})
            continue
        row.deleted_at = now  # 与单删一致：仅置 deleted_at，不做引用保护
        deleted += 1
    await db.commit()
    return {"deleted": deleted, "skipped": skipped}


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


# ---------------------------------------------------------------------------
# 跨项目资产复用（S-06：选中他人/自己项目的资产复制到当前项目）
# ---------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/assets/{asset_type}/{asset_id}/copy")
async def copy_asset_to_project(
    project_id: int,
    asset_type: str,
    asset_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """把一个已有资产复制到当前项目（S-06「复用资产」）。

    规则：
    - 源资产须属于当前用户自己的未删除项目（跨用户复制一律 404）；
    - 复制为新行（新 ID），名称追加 " (副本)"，项目内同名 409；
    - 角色 generation_status 重置为 pending（未生成态）、outfit_variant_id
      清空（变体归属源项目）；asset_url 保留以复用已有形象图；
    - 风格副本 is_default=False。

    Args:
        project_id: 目标项目 ID。
        asset_type: 资产类型 character/scene/prop/style。
        asset_id: 源资产 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: 新建资产的输出（对应 CharacterOut/SceneOut/PropOut/StyleOut 结构）。

    Raises:
        HTTPException: 400 —— asset_type 非法；404 —— 目标项目/源资产不存在
            或源资产不属于当前用户；409 —— 目标项目存在同名资产。
    """
    out_map = {
        "character": (Character, CharacterOut),
        "scene": (Scene, SceneOut),
        "prop": (Prop, PropOut),
        "style": (Style, StyleOut),
    }
    if asset_type not in out_map:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="type 仅支持 character/scene/prop/style")
    src_cls, out_cls = out_map[asset_type]

    project = await get_owned_project(db, project_id, user.id)

    # 源资产归属链校验：资产 → 源项目 → 当前用户（任一失败统一 404）
    src = await db.get(src_cls, asset_id)
    if src is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="源资产不存在")
    src_project = await db.get(Project, src.project_id)
    if src_project is None or src_project.user_id != user.id or src_project.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="源资产不存在")

    new_name = f"{src.name} (副本)"
    # P2 软删统一后 ensure_unique_name 覆盖四类（含 style，软删行除外）
    await ensure_unique_name(db, project.id, asset_type, new_name)

    # 四类资产共有列：project_id / name / description / visual_prompt
    # （asset_url 仅 character/scene/prop 有，style 为 reference_url，分支内赋值）
    dup = src_cls(
        project_id=project.id,
        name=new_name,
        description=src.description,
        visual_prompt=src.visual_prompt,
    )
    if asset_type == "character":
        dup.aliases = src.aliases
        dup.face_locked = src.face_locked
        dup.feature_vector = getattr(src, "feature_vector", None)
        dup.asset_url = src.asset_url
        dup.generation_status = "pending"  # 生成状态置回未生成
        dup.outfit_variant_id = None  # 变体归属源项目，副本不继承
        dup.enabled_at = datetime.now(timezone.utc)  # 复制入当前项目即启用
    elif asset_type == "scene":
        dup.asset_url = src.asset_url
        dup.space_signature = getattr(src, "space_signature", None)
        dup.is_locked = False
    elif asset_type == "prop":
        dup.asset_url = src.asset_url
        dup.category = src.category
        dup.ownership = src.ownership
        dup.is_locked = False
    else:  # style
        dup.preset_code = src.preset_code
        dup.reference_url = src.reference_url
        dup.is_default = False  # 副本不抢占默认风格
    db.add(dup)
    await db.commit()
    await db.refresh(dup)
    return out_cls.model_validate(dup).model_dump()
