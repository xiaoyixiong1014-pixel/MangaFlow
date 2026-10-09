"""管理端站点设置路由模块（文件级说明）。

实现站点设置的 KV 读写与数据备份：
- GET  /api/admin/settings          读取站点设置（langgraph_store，namespace='site_settings'）
- PUT  /api/admin/settings          逐 key upsert 设置项（value 为 JSONB）
- GET  /api/admin/settings/backups  备份文件列表
- POST /api/admin/settings/backups  生成备份 JSON（演示环境本地备份，生产走对象存储）

存储说明：站点设置复用 langgraph_store 表（namespace='site_settings'）。
- 读取：将该命名空间下全部 KV 合并为一个 dict（无记录时返回默认值）；
- 写入：请求体 settings 字典的每个键 upsert 一行，value 统一包装为
  {"value": <原始值>} 以满足 JSONB dict 形态；读取时自动解包。
- 兼容：种子数据以单行 key='site' 存整 dict，读取时按 dict 直接展开合并。

全部接口要求运营方角色，普通用户 403。
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import require_operator
from app.models import LanggraphStore, Order, Project, User, Wallet
from app.schemas.admin import BackupCreateResponse, BackupFileItem, SettingsPutRequest

router = APIRouter(
    prefix="/api/admin/settings",
    tags=["admin-settings"],
    dependencies=[Depends(require_operator)],  # 路由级鉴权：仅运营方可访问，普通用户 403
)

# 站点设置 KV 命名空间（与 init_db 种子约定一致）
_SITE_SETTINGS_NAMESPACE = "site_settings"
# 默认站点设置（库中无任何记录时的兜底返回值）
_DEFAULT_SITE_SETTINGS: Dict[str, Any] = {"registration_enabled": True, "site_name": "漫序 MangaFlow"}


@router.get("")
async def get_settings(
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> Dict[str, Any]:
    """读取站点设置：合并 namespace='site_settings' 下全部 KV。

    合并规则：
    - value 为 {"value": x} 单键包装（本接口 PUT 写入形态）→ 解包为 {key: x}；
    - value 为多键 dict（如种子 key='site' 整 dict）→ 直接展开合并；
    - 无任何记录 → 返回默认设置。
    优先级：先合并整 dict 行（种子形态），再合并单键包装行（PUT 精确键覆盖），
    保证 PUT 写入的键值始终最终生效且不受遍历顺序影响。

    Args:
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        Dict[str, Any]: 合并后的站点设置字典。

    Raises:
        无：查询异常向上抛出由全局处理器兜底。
    """
    rows = (
        await db.execute(
            select(LanggraphStore).where(LanggraphStore.namespace == _SITE_SETTINGS_NAMESPACE)
        )
    ).scalars().all()

    merged: Dict[str, Any] = dict(_DEFAULT_SITE_SETTINGS)
    wrapped: List[tuple] = []
    for row in rows:
        value = row.value
        if isinstance(value, dict) and set(value.keys()) == {"value"}:
            wrapped.append((row.key, value["value"]))
        elif isinstance(value, dict):
            merged.update(value)  # 兼容种子整 dict 形态（先合并，低优先级）
    for key, value in wrapped:
        merged[key] = value  # PUT 单键包装行后合并，精确覆盖
    return merged


@router.put("")
async def put_settings(
    req: SettingsPutRequest,
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> Dict[str, Any]:
    """写入站点设置：请求字典逐 key upsert（value JSONB，包装为 {"value": x}）。

    Args:
        req: 设置写入请求（settings 字典）。
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        Dict[str, Any]: 写入后的完整站点设置（重新读取合并）。

    Raises:
        无：数据库异常向上抛出由全局处理器兜底。
    """
    for key, value in req.settings.items():
        stmt = pg_insert(LanggraphStore).values(
            namespace=_SITE_SETTINGS_NAMESPACE,
            key=key,
            value={"value": value},
        )
        # (namespace, key) 唯一索引冲突时更新 value，实现 upsert
        stmt = stmt.on_conflict_do_update(
            index_elements=["namespace", "key"],
            set_={"value": {"value": value}},
        )
        await db.execute(stmt)
    await db.commit()
    return await get_settings(db=db, _operator=_operator)


@router.get("/backups", response_model=List[BackupFileItem])
async def list_backups(
    _operator: User = Depends(require_operator),
) -> List[BackupFileItem]:
    """列出备份目录下的全部 JSON 备份文件（含大小与修改时间，新文件在前）。

    Args:
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        List[BackupFileItem]: 备份文件列表（目录不存在时返回空列表）。

    Raises:
        无：目录不可读等异常向上抛出由全局处理器兜底。
    """
    backup_dir = settings.storage_path() / "backups"
    if not backup_dir.is_dir():
        return []
    items: List[BackupFileItem] = []
    for file in backup_dir.glob("*.json"):
        stat = file.stat()
        items.append(BackupFileItem(
            filename=file.name,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        ))
    items.sort(key=lambda i: i.modified_at, reverse=True)
    return items


@router.post("/backups", response_model=BackupCreateResponse)
async def create_backup(
    db: AsyncSession = Depends(get_db),
    _operator: User = Depends(require_operator),
) -> BackupCreateResponse:
    """生成数据备份 JSON：users/orders/projects/wallets 计数与平台总积分。

    注：演示环境本地备份（写入 {storage_dir}/backups/），生产环境应走对象存储。

    Args:
        db: 请求级异步数据库会话。
        _operator: 运营方身份（依赖注入校验）。

    Returns:
        BackupCreateResponse: 生成结果与备份文件名。

    Raises:
        无：统计或写文件异常向上抛出由全局处理器兜底。
    """
    users_total = await db.scalar(select(func.count()).select_from(User)) or 0
    orders_total = await db.scalar(select(func.count()).select_from(Order)) or 0
    projects_total = await db.scalar(select(func.count()).select_from(Project)) or 0
    wallets_total = await db.scalar(select(func.count()).select_from(Wallet)) or 0
    points_available = await db.scalar(
        select(func.coalesce(func.sum(Wallet.points_balance), 0))
    ) or 0
    points_frozen = await db.scalar(
        select(func.coalesce(func.sum(Wallet.frozen_points), 0))
    ) or 0

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "users": int(users_total),
            "orders": int(orders_total),
            "projects": int(projects_total),
            "wallets": int(wallets_total),
        },
        "points_total": {
            "available": int(points_available),
            "frozen": int(points_frozen),
        },
    }

    backup_dir = settings.storage_path() / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{ts}.json"
    (backup_dir / filename).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return BackupCreateResponse(message="备份已生成", filename=filename)
