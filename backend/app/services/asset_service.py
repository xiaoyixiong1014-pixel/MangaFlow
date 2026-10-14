"""资产域业务服务模块（文件级说明）。

承载四类资产（角色 / 场景 / 道具 / 风格）的 CRUD 编排、默认风格互斥、
角色定妆照生成任务（计费冻结 + 任务入队，RD-28）、面部锁定快照、
资产变体（RD-14 / RD-19）、一致性校验（embedding 余弦真实算法）、
资产向量化写入（asset_embeddings upsert）与跨项目资产总览 / 语义搜索。

路由层（app/api/assets.py）只做参数校验与编排，事务细节统一收敛在本模块。
"""

from datetime import datetime, timezone
from uuid import uuid4
from typing import Any, Dict, List, Optional, Tuple

import logging
from fastapi import HTTPException, status
from sqlalchemy import func, literal, select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("mangaflow.services.asset_service")

from app.models import (
    AssetEmbedding,
    AssetVariant,
    Character,
    Project,
    Prop,
    Scene,
    Style,
    Task,
    VersionSnapshot,
)
from app.providers.embedding import cosine_similarity, embed_text, hash_bag_vector
from app.schemas.asset import (
    CHARACTER_VARIANT_TYPES,
    PROP_VARIANT_TYPES,
    SCENE_VARIANT_TYPES,
)
from app.services import vector_store
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
      任务执行器 runner 据此回写资产状态）；角色配置了参考图（A-04）时
      meta 追加 reference_image_url，执行器把该参考图透传给图像渠道。

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
    # A-04 参考图锚点：角色配置了参考图时写入任务 meta（执行器透传给 provider）
    task_meta: Dict[str, Any] = {
        "target_type": "character", "target_id": character.id, "prompt": final_prompt,
    }
    if character.reference_image_url:
        task_meta["reference_image_url"] = character.reference_image_url
    task = await freeze_and_create_task(
        db,
        project=project,
        task_type="image",
        phase="full",
        cost_points=CHARACTER_IMAGE_COST,
        meta=task_meta,
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


# ---------------------------------------------------------------------------
# 资产向量化（asset_embeddings upsert）与语义搜索
# ---------------------------------------------------------------------------

# asset_embeddings.embedding JSONB 的承载结构约定：
#   {"vector": [float...], "text_preview": 前200字, "updated_at": ISO时间}
# JSONB 为主存储（迁移期安全）；pgvector 可用时由 vector_store 幂等维护影子
# 列 embedding_vec（vector(dim)）并双写，检索侧按维度匹配自动分流 SQL/Python
# 两条路径（见 vector_store 模块说明与 semantic_search_assets）。

# 一致性校验通过阈值（保持与原占位实现一致）
CONSISTENCY_THRESHOLD = 0.85
# 一致性综合评分权重：visual_prompt 0.5 + 描述文本 0.3 + 锁定加权 0.2
_CONSISTENCY_WEIGHTS = (0.5, 0.3, 0.2)


def _embedding_vector(payload: Any) -> Optional[List[float]]:
    """从 asset_embeddings.embedding JSONB 载荷中提取向量列表（内部函数）。

    兼容两种历史形态：纯 list[float]（旧约定）与 dict（{"vector": [...]}）。

    Args:
        payload: embedding 列的 JSONB 值（list / dict / None）。

    Returns:
        Optional[List[float]]: 向量列表；结构不合法时返回 None。

    Raises:
        无。
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        vec = payload.get("vector")
        if isinstance(vec, list):
            return vec
    return None


def _embedding_model_tag(text: str, vector: List[float]) -> str:
    """判断本次向量化实际使用的模型来源（内部函数）。

    embed_text 静默降级的设计使调用方无法直接得知来源；此处以
    「256 维且与本地 hash_bag_vector 输出完全一致」判定为 hash-bag，
    否则视为 remote（远程模型维度通常非 256，且取值碰撞概率可忽略）。

    Args:
        text: 参与向量化的原文。
        vector: embed_text 返回的向量。

    Returns:
        str: "hash-bag" 或 "remote"。

    Raises:
        无。
    """
    if len(vector) == 256 and vector == hash_bag_vector(text):
        return "hash-bag"
    return "remote"


async def upsert_asset_embedding(
    db: AsyncSession, asset_type: str, asset_id: int, text: str
) -> None:
    """将资产文本向量化并 upsert 到 asset_embeddings 表（RD-15）。

    - 远程 /embeddings 失败时 embed_text 自动降级 hash-bag，任何环境可用；
    - asset_embeddings 表无 (asset_type, asset_id) 唯一约束，采用「先查后写」；
    - embedding JSONB 存 {"vector", "text_preview", "updated_at"} 结构，
      model 列存 "remote" | "hash-bag"；
    - pgvector 可用时同步双写影子列 embedding_vec（SAVEPOINT 隔离，失败仅
      告警，不影响 JSONB 主存储）；
    - 自带独立 commit 且全程吞异常：失败仅告警，绝不影响调用方主流程。

    Args:
        db: 异步数据库会话（调用方主数据已提交后调用）。
        asset_type: 资产类型 character / scene / prop / style。
        asset_id: 资产 ID。
        text: 用于向量化的资产文本（名称/描述/视觉提示词拼合）。

    Returns:
        None。

    Raises:
        无：任何异常（含向量降级与写库失败）均在函数内捕获并记录告警。
    """
    try:
        text = (text or "").strip()
        if not text:
            return  # 空文本无可向量化内容，静默跳过
        vector = await embed_text(text)
        payload = {
            "vector": vector,
            "text_preview": text[:200],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        row = (
            await db.execute(
                select(AssetEmbedding).where(
                    AssetEmbedding.asset_type == asset_type,
                    AssetEmbedding.asset_id == asset_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            obj = AssetEmbedding(
                asset_type=asset_type,
                asset_id=asset_id,
                embedding=payload,
                model=_embedding_model_tag(text, vector),
            )
            db.add(obj)
            # flush 取得自增 id 并先发出 INSERT，影子列 UPDATE 才能在同一事务内命中该行
            await db.flush()
        else:
            row.embedding = payload  # 赋新 dict 触发 JSONB 更新
            row.model = _embedding_model_tag(text, vector)
        # pgvector 双写：影子列与 JSONB 同事务提交（内部 SAVEPOINT 隔离失败）
        await vector_store.sync_asset_embedding_vec(db, asset_type, asset_id, vector)
        await db.commit()
    except Exception as exc:  # noqa: BLE001 —— 向量化写入失败不影响资产主流程
        logger.warning(
            "资产向量 upsert 失败（%s/%s）：%s", asset_type, asset_id, exc
        )
        await db.rollback()


async def _semantic_search_assets_pgvector(
    db: AsyncSession,
    user_id: int,
    qvec: List[float],
    types: List[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """pgvector SQL 余弦 TopK 检索（内部函数，影子列路径）。

    逐类型执行 `1 - (embedding_vec <=> :qv)`（ivfflat 的 <=> 为余弦距离语义，
    1-距离即余弦相似度），仅命中影子列非空行；归属过滤（项目属于当前用户、
    项目未删）与软删过滤与 Python 路径保持一致。每类型取 limit 条后全局
    排序截断，与全量余弦 TopK 等价。注意：ivfflat 为近似检索（probes=1），
    极小召回差异由近似语义接受。

    Args:
        db: 异步数据库会话。
        user_id: 当前用户 ID（仅搜索其名下项目资产）。
        qvec: 查询向量（维度已与影子列一致，由 can_use_pgvector 保证）。
        types: 资产类型列表。
        limit: 每类型返回条数上限（TopK）。

    Returns:
        List[Dict[str, Any]]: 与 Python 路径结构完全一致的条目列表
            [{"asset_type", "asset_id", "name", "score", "preview"}]，按 score 降序。

    Raises:
        Exception: SQL 执行失败向上抛出，由调用方捕获后自愈降级回 Python 路径。
    """
    qv = vector_store.vector_literal(qvec)
    results: List[Dict[str, Any]] = []
    for t in types:
        cls = Style if t == "style" else _ASSET_CLS[t]
        # style 无软删字段，其余类型排除软删行（与 Python 路径条件一致）
        del_cond = "" if t == "style" else " AND c.deleted_at IS NULL"
        sql = sa_text(
            "SELECT ae.asset_id AS asset_id, c.name AS name, ae.embedding AS embedding, "
            "c.visual_prompt AS visual_prompt, "
            "1 - (ae.embedding_vec <=> CAST(:qv AS vector)) AS score "
            f"FROM asset_embeddings ae "
            f"JOIN {cls.__tablename__} c ON ae.asset_type = :t AND ae.asset_id = c.id "
            "JOIN projects p ON c.project_id = p.id "
            f"WHERE p.user_id = :uid AND p.deleted_at IS NULL{del_cond} "
            "AND ae.embedding_vec IS NOT NULL "
            "ORDER BY ae.embedding_vec <=> CAST(:qv AS vector) "
            "LIMIT :k"
        )
        rows = (await db.execute(sql, {"qv": qv, "t": t, "uid": user_id, "k": limit})).all()
        for r in rows:
            payload = r.embedding if isinstance(r.embedding, dict) else {}
            results.append({
                "asset_type": t,
                "asset_id": r.asset_id,
                "name": r.name,
                "score": round(float(r.score), 4),
                "preview": payload.get("text_preview") or (r.visual_prompt or "")[:200],
            })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


async def semantic_search_assets(
    db: AsyncSession,
    user_id: int,
    query: str,
    asset_type: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """资产语义搜索：查询文本向量化后做余弦 TopK（pgvector 双模分流）。

    - pgvector 可用且查询向量维度与影子列一致 → SQL 余弦 TopK（ivfflat 索引，
      见 _semantic_search_assets_pgvector）；SQL 失败自愈降级（关闭本进程
      pgvector 开关）并回退 Python 路径；
    - 否则走原有「全表扫 + Python 侧余弦」降级路径（行为不变，维度截断对齐）；
    - 两种模式返回结构完全一致，前端无感知。

    Args:
        db: 异步数据库会话。
        user_id: 当前用户 ID（仅搜索其名下项目资产，软删行排除）。
        query: 查询文本。
        asset_type: 可选类型过滤 character / scene / prop / style。
        limit: 返回条数上限（TopK）。

    Returns:
        List[Dict[str, Any]]:
            [{"asset_type", "asset_id", "name", "score", "preview"}]，按 score 降序。

    Raises:
        HTTPException: 400 —— asset_type 取值非法。
    """
    types = [asset_type] if asset_type else ["character", "scene", "prop", "style"]
    for t in types:
        if t not in _ASSET_CLS and t != "style":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="type 仅支持 character/scene/prop/style")

    qvec = await embed_text(query)
    # ---------- pgvector 双模分流：影子列可用且维度一致时走 SQL 余弦 TopK ----------
    if vector_store.can_use_pgvector(len(qvec)):
        try:
            return await _semantic_search_assets_pgvector(db, user_id, qvec, types, limit)
        except Exception as exc:  # noqa: BLE001 —— SQL 路径失败自愈降级
            vector_store.mark_pgvector_unavailable(exc)
            await db.rollback()  # 释放 aborted 事务，保证后续只读查询可用

    # ---------- Python 降级路径：全量遍历 + 余弦（行为与既有实现一致） ----------
    results: List[Dict[str, Any]] = []
    for t in types:
        cls = Style if t == "style" else _ASSET_CLS[t]
        # 归属过滤：项目属于当前用户且项目未删；资产软删行排除（style 无软删字段）
        conds = [Project.user_id == user_id, Project.deleted_at.is_(None)]
        if t != "style":
            conds.append(cls.deleted_at.is_(None))
        rows = (
            await db.execute(
                select(AssetEmbedding, cls)
                .join(cls, (AssetEmbedding.asset_type == t)
                      & (AssetEmbedding.asset_id == cls.id))
                .join(Project, cls.project_id == Project.id)
                .where(*conds)
            )
        ).all()
        for ae, asset in rows:
            vec = _embedding_vector(ae.embedding)
            if not vec:
                continue
            payload = ae.embedding if isinstance(ae.embedding, dict) else {}
            score = round(cosine_similarity(qvec, vec), 4)
            results.append({
                "asset_type": t,
                "asset_id": asset.id,
                "name": asset.name,
                "score": score,
                "preview": payload.get("text_preview")
                or (asset.visual_prompt or "")[:200],
            })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


async def consistency_check(
    db: AsyncSession, character_id: int, variant_id: Optional[int] = None
) -> Dict[str, Any]:
    """一致性校验真实化（embedding 余弦 v1）：主形象与目标（变体或自身）文本向量比对。

    分项与综合公式：
    - visual_prompt_sim：主形象 visual_prompt 与目标 visual_prompt 的余弦相似度；
    - description_sim：主形象「名称+描述」拼合文本与目标描述文本的余弦相似度；
    - locked_bonus：主形象 face_locked=True 记 1.0，否则 0.0；
    - score = round(0.5*visual + 0.3*desc + 0.2*locked, 4)，阈值 0.85。

    继承语义（RD-14）：变体未填写 visual_prompt / 描述时视为继承主形象，
    对应分项按 1.0（同文本向量完全一致）计，不惩罚缺省变体。
    两资产无存储 embedding 时现场 embed_text 计算并顺手 upsert 刷新。

    Args:
        db: 异步数据库会话。
        character_id: 主形象角色资产 ID。
        variant_id: 目标变体 ID（可空；为空时校验主形象与自身，分数仅由锁定状态影响）。

    Returns:
        Dict[str, Any]: {"similarity", "passed", "report"}（旧字段兼容）+
            {"score", "threshold", "details", "algorithm": "embedding-cosine-v1"}。

    Raises:
        HTTPException: 404 —— 角色不存在；404 —— 变体不存在或不属于该角色。
    """
    character = (
        await db.execute(select(Character).where(Character.id == character_id))
    ).scalar_one_or_none()
    if character is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="character 资产不存在")

    variant: Optional[AssetVariant] = None
    if variant_id is not None:
        variant = (
            await db.execute(
                select(AssetVariant).where(
                    AssetVariant.id == variant_id,
                    AssetVariant.owner_type == "character",
                    AssetVariant.owner_id == character.id,
                )
            )
        ).scalar_one_or_none()
        if variant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="变体不存在或不属于该角色")

    # ---------- 分项一：visual_prompt 余弦 ----------
    main_visual = (character.visual_prompt or "").strip()
    # 变体未填 visual_prompt → 继承主形象（同文本相似度 1.0）
    target_visual = ((variant.visual_prompt if variant else None) or main_visual).strip()
    visual_sim = cosine_similarity(await embed_text(main_visual),
                                   await embed_text(target_visual))

    # ---------- 分项二：描述类文本拼合余弦 ----------
    main_desc = " ".join(x for x in (character.name, character.description) if x).strip()
    # 变体侧无独立描述字段 → 继承主形象描述文本
    target_desc = main_desc if variant is None else (
        " ".join(x for x in (variant.name, variant.variant_type) if x).strip() or main_desc
    )
    desc_sim = cosine_similarity(await embed_text(main_desc),
                                 await embed_text(target_desc))

    # ---------- 分项三：锁定加权 ----------
    locked_bonus = 1.0 if character.face_locked else 0.0

    # ---------- 综合评分 ----------
    w_visual, w_desc, w_locked = _CONSISTENCY_WEIGHTS
    score = round(w_visual * visual_sim + w_desc * desc_sim + w_locked * locked_bonus, 4)
    passed = score >= CONSISTENCY_THRESHOLD
    target_label = f"变体 {variant_id}" if variant_id else "主形象"
    details = {
        "visual_prompt_sim": round(visual_sim, 4),
        "description_sim": round(desc_sim, 4),
        "locked_bonus": locked_bonus,
        "target": target_label,
    }
    report = (
        f"角色 {character_id} 与{target_label}的一致性校验（embedding 余弦 v1）："
        f"相似度 {score}，{'通过' if passed else '未通过'}（阈值 {CONSISTENCY_THRESHOLD}）。"
    )

    # 顺手刷新主形象向量（幂等 upsert，失败内部吞掉不影响校验结果）
    await upsert_asset_embedding(
        db, "character", character.id,
        " ".join(x for x in (character.name, character.description, main_visual) if x),
    )

    return {
        # 旧字段兼容：similarity/score 同值，report 保留
        "similarity": score,
        "passed": passed,
        "report": report,
        "score": score,
        "threshold": CONSISTENCY_THRESHOLD,
        "details": details,
        "algorithm": "embedding-cosine-v1",
    }


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
