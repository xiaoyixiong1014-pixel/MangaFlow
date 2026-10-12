"""版本快照回退路由模块（文件级说明）。

V-01 版本快照回退：
- POST /api/projects/{project_id}/versions/{snapshot_id}/rollback：
  把指定快照 impact_scope 中记录的实体状态（characters / scenes / props
  名单与描述，见 services/script_service.serialize_project_assets）恢复到
  对应资产表。
- 回退语义（最小可行）：
  1. 仅支持 impact_scope.entity == "assets" 的快照（抽取确认快照与回退前
     快照），其余快照只存统计摘要、无实体状态可恢复，返回 400；
  2. 恢复采用「软删现有行 + 同名行复活/更新 + 缺失行新建」：不物理删除，
     保留审计链且不会撞 (project_id, name) 唯一索引，也不会破坏分镜等
     下游外键引用；
  3. 回退执行前自动创建一个 stage='pre_rollback' 的「回退前快照」（记录
     回退时刻的完整实体状态），保证回退操作本身可再被回退撤销；
  4. 写 operation_logs(action='version_rollback', risk_level='medium') 留痕。
- 归属校验沿用项目统一口径：项目不存在/非本人/快照不属于该项目均返回
  404（不泄露他人资源存在性）。
"""

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Character, OperationLog, Prop, Scene, User, VersionSnapshot
from app.services.script_service import (
    DEFAULT_VISUAL_TEMPLATE,
    get_owned_project,
    serialize_project_assets,
)

router = APIRouter(tags=["versions"])


async def _restore_assets(db: AsyncSession, project_id: int, scope: Dict[str, Any]) -> Dict[str, int]:
    """把快照 impact_scope 记录的资产状态恢复到资产表（事务内执行，不提交）。

    恢复流程（三类资产同一套逻辑）：
    1. 软删当前项目内全部有效行（deleted_at=now，可追溯、不动下游外键）；
    2. 遍历快照条目：按 name 查同名行（含软删行，唯一索引保证至多一行）——
       存在则复活（deleted_at=None）并更新描述字段，不存在则新建。

    Args:
        db: 异步数据库会话（由调用方负责 commit/rollback）。
        project_id: 项目 ID。
        scope: 快照 impact_scope（须含 entity='assets' 与 characters/scenes/props 列表）。

    Returns:
        Dict[str, int]: 恢复后的各类资产数量 {"characters","scenes","props"}。

    Raises:
        Exception: 数据库写入异常向上抛出（由调用方事务回滚兜底）。
    """
    now = datetime.now(timezone.utc)

    # ---------- 1. 软删当前全部有效资产行 ----------
    for model in (Character, Scene, Prop):
        rows = (
            await db.execute(
                select(model).where(model.project_id == project_id, model.deleted_at.is_(None))
            )
        ).scalars().all()
        for row in rows:
            row.deleted_at = now

    # ---------- 2. 角色：复活/更新/新建 ----------
    n_characters = 0
    for item in scope.get("characters") or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        visual_prompt = item.get("visual_prompt") or DEFAULT_VISUAL_TEMPLATE.format(
            name=name, description=item.get("description") or name)
        row = (
            await db.execute(
                select(Character).where(Character.project_id == project_id, Character.name == name)
            )
        ).scalar_one_or_none()
        if row is None:
            db.add(Character(
                project_id=project_id, name=name,
                aliases=list(item.get("aliases") or []),
                description=item.get("description") or None,
                visual_prompt=visual_prompt,
            ))
        else:
            row.deleted_at = None
            row.aliases = list(item.get("aliases") or [])
            row.description = item.get("description") or None
            row.visual_prompt = visual_prompt
        n_characters += 1

    # ---------- 3. 场景：复活/更新/新建 ----------
    n_scenes = 0
    for item in scope.get("scenes") or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        visual_prompt = item.get("visual_prompt") or DEFAULT_VISUAL_TEMPLATE.format(
            name=name, description=item.get("description") or name)
        row = (
            await db.execute(
                select(Scene).where(Scene.project_id == project_id, Scene.name == name)
            )
        ).scalar_one_or_none()
        if row is None:
            db.add(Scene(
                project_id=project_id, name=name,
                description=item.get("description") or None,
                visual_prompt=visual_prompt,
            ))
        else:
            row.deleted_at = None
            row.description = item.get("description") or None
            row.visual_prompt = visual_prompt
        n_scenes += 1

    # ---------- 4. 道具：复活/更新/新建 ----------
    n_props = 0
    for item in scope.get("props") or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        visual_prompt = item.get("visual_prompt") or DEFAULT_VISUAL_TEMPLATE.format(
            name=name, description=item.get("description") or name)
        row = (
            await db.execute(
                select(Prop).where(Prop.project_id == project_id, Prop.name == name)
            )
        ).scalar_one_or_none()
        if row is None:
            db.add(Prop(
                project_id=project_id, name=name,
                category=item.get("category") or None,
                description=item.get("description") or None,
                visual_prompt=visual_prompt,
            ))
        else:
            row.deleted_at = None
            row.category = item.get("category") or None
            row.description = item.get("description") or None
            row.visual_prompt = visual_prompt
        n_props += 1

    return {"characters": n_characters, "scenes": n_scenes, "props": n_props}


@router.post("/api/projects/{project_id}/versions/{snapshot_id}/rollback")
async def rollback_version(
    project_id: int,
    snapshot_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """回退到指定版本快照（V-01）：恢复快照记录的资产实体状态。

    Args:
        project_id: 项目 ID（须归属当前用户）。
        snapshot_id: 目标快照 ID（须属于该项目且含可回退实体状态）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"restored": True, "new_snapshot_id": 回退前快照 ID,
        "restored_at": 回退完成时间（UTC ISO8601）}。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人项目，或快照不存在/不属于该项目；
            400 —— 快照未包含可回退的实体状态（如手动快照仅存统计摘要）。
    """
    # ---------- 归属校验（统一 404，不泄露他人项目存在性） ----------
    project = await get_owned_project(db, project_id, user.id)

    snapshot = (
        await db.execute(
            select(VersionSnapshot).where(
                VersionSnapshot.id == snapshot_id,
                VersionSnapshot.project_id == project.id,
            )
        )
    ).scalar_one_or_none()
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="版本快照不存在")

    # ---------- 可回退校验：仅支持含实体状态的快照 ----------
    scope = snapshot.impact_scope or {}
    if scope.get("entity") != "assets":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该快照未包含可回退的实体状态（仅抽取确认与回退前快照支持回退）",
        )

    # ---------- 回退前快照：记录回退时刻状态，保证回退本身可撤销 ----------
    pre_state = await serialize_project_assets(db, project.id)
    pre_snapshot = VersionSnapshot(
        project_id=project.id,
        stage="pre_rollback",
        summary={
            "characters": len(pre_state["characters"]),
            "scenes": len(pre_state["scenes"]),
            "props": len(pre_state["props"]),
        },
        impact_scope=pre_state,
    )
    db.add(pre_snapshot)
    await db.flush()  # 取得 pre_snapshot.id 供审计详情引用

    # ---------- 恢复实体状态 + 审计留痕（同一事务，任一失败整体回滚） ----------
    restored = await _restore_assets(db, project.id, scope)
    db.add(OperationLog(
        user_id=user.id,
        project_id=project.id,
        action="version_rollback",
        risk_level="medium",
        detail={
            "snapshot_id": snapshot.id,
            "snapshot_stage": snapshot.stage,
            "pre_rollback_snapshot_id": pre_snapshot.id,
            "restored": restored,
        },
    ))
    await db.commit()

    return {
        "restored": True,
        "new_snapshot_id": pre_snapshot.id,
        "restored_at": datetime.now(timezone.utc).isoformat(),
    }
