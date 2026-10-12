"""画布插件路由模块（文件级说明）。

实现插件市场全流程（CV-20/21/22）：
- GET    /api/canvas-plugins              插件列表（普通用户 = reviewed+enabled；
                                            operator 传 all=true 看全部）
- POST   /api/canvas-plugins              安装插件（登录用户；reviewed 默认 False=pending）
- POST   /api/canvas-plugins/{id}/review  运营方审查（approved 置 reviewed，留痕 operation_logs）
- POST   /api/canvas-plugins/{id}/enable  运营方启用
- POST   /api/canvas-plugins/{id}/disable 运营方停用
- POST   /api/canvas-plugins/{id}/invoke  手动调用（登录用户；插件须 reviewed+enabled）

调用统一走 services/plugin_service.invoke_plugin（与画布 plugin 节点执行共用）。

安全（CV-22 轻量加固，2026-10-10）：安装（POST）与 invoke 前均校验
endpoint_url（validate_endpoint_url：仅 http/https + 内网保留地址黑名单 +
可选运营方信任域白名单 plugin_allowed_hosts，SSRF 防护），违规 400；
"builtin:" 前缀进程内插件不受限。运行时隔离沙箱为已知限制（P2），
当前以 SSRF 防护 + 运营审查 + 调用留痕缓解。
"""

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_operator
from app.models import CanvasPlugin, OperationLog, User
from app.schemas.plugin import (
    PluginCreateRequest,
    PluginInvokeRequest,
    PluginOut,
    PluginReviewRequest,
)
from app.services.plugin_service import PluginError, invoke_plugin, validate_endpoint_url

router = APIRouter(prefix="/api/canvas-plugins", tags=["canvas-plugins"])


def _plugin_out(plugin: CanvasPlugin) -> PluginOut:
    """把 CanvasPlugin ORM 对象组装为出参模型（内部函数）。

    permissions JSONB 收纳 {"scopes", "inputs", "outputs", "description"}，
    缺项按空值回显，容忍历史/异构数据。

    Args:
        plugin: 插件 ORM 对象。

    Returns:
        PluginOut: 出参模型。
    """
    perms = plugin.permissions if isinstance(plugin.permissions, dict) else {}
    return PluginOut(
        id=plugin.id,
        user_id=plugin.user_id,
        name=plugin.name,
        version=plugin.version,
        description=perms.get("description"),
        endpoint_url=plugin.plugin_url,
        inputs=perms.get("inputs") if isinstance(perms.get("inputs"), list) else [],
        outputs=perms.get("outputs") if isinstance(perms.get("outputs"), list) else [],
        permissions=perms.get("scopes") if isinstance(perms.get("scopes"), list) else [],
        granted=plugin.granted,
        enabled=plugin.enabled,
        reviewed=plugin.reviewed,
    )


async def _get_plugin(db: AsyncSession, plugin_id: int) -> CanvasPlugin:
    """按 ID 加载插件（内部函数）。

    Args:
        db: 异步数据库会话。
        plugin_id: 插件 ID。

    Returns:
        CanvasPlugin: 插件 ORM 对象。

    Raises:
        HTTPException: 404 —— 插件不存在。
    """
    plugin = await db.get(CanvasPlugin, plugin_id)
    if plugin is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="插件不存在")
    return plugin


@router.get("", response_model=List[PluginOut])
async def list_plugins(
    all: bool = Query(False, description="operator 传 true 查看全部（含未审核/停用）"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[PluginOut]:
    """插件列表：普通用户仅见已审核且启用的插件；operator 可查全部。

    Args:
        all: 是否返回全部（仅 operator 生效，普通用户强制 False）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        List[PluginOut]: 插件列表（按 id 升序）。

    Raises:
        HTTPException: 401 —— 未登录。
    """
    stmt = select(CanvasPlugin).order_by(CanvasPlugin.id)
    if not (all and user.role == "operator"):
        stmt = stmt.where(CanvasPlugin.reviewed.is_(True), CanvasPlugin.enabled.is_(True))
    rows = (await db.execute(stmt)).scalars().all()
    return [_plugin_out(p) for p in rows]


@router.post("", response_model=PluginOut, status_code=201)
async def create_plugin(
    req: PluginCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PluginOut:
    """安装（登记）插件：reviewed 默认 False（待运营方审核）、enabled 默认 False。

    同一用户下 endpoint_url 唯一（uk_plugins_user_url），重复安装返回 400。
    endpoint_url 先经 CV-22 安全校验（validate_endpoint_url）：仅 http/https、
    禁止内网保留地址（SSRF 防护）、可选信任域白名单，违规返回 400。

    Args:
        req: 安装请求（name/version/description/endpoint_url/inputs/outputs/permissions）。
        user: 当前登录用户（安装者）。
        db: 请求级异步数据库会话。

    Returns:
        PluginOut: 新建的插件信息。

    Raises:
        HTTPException: 400 —— endpoint_url 校验不通过或重复安装同一地址；
            401 —— 未登录。
    """
    # CV-22：安装前校验 endpoint_url（SSRF 防护），违规 400
    try:
        validate_endpoint_url(req.endpoint_url)
    except PluginError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    dup = (
        await db.execute(
            select(CanvasPlugin).where(
                CanvasPlugin.user_id == user.id, CanvasPlugin.plugin_url == req.endpoint_url
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该插件地址已安装")
    plugin = CanvasPlugin(
        user_id=user.id,
        plugin_url=req.endpoint_url,
        name=req.name,
        version=req.version,
        # permissions JSONB 统一收纳 scopes/inputs/outputs/description
        permissions={
            "scopes": list(req.permissions),
            "inputs": [p.model_dump() for p in req.inputs],
            "outputs": [p.model_dump() for p in req.outputs],
            "description": req.description or "",
        },
        granted=True,  # 安装即视为用户显式授权（RD-46）
        enabled=False,
        reviewed=False,  # pending，待运营方审查
    )
    db.add(plugin)
    await db.commit()
    await db.refresh(plugin)
    return _plugin_out(plugin)


@router.post("/{plugin_id}/review", response_model=PluginOut)
async def review_plugin(
    plugin_id: int,
    req: PluginReviewRequest,
    operator: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
) -> PluginOut:
    """运营方审查插件：approved=True 通过（reviewed=True）/ False 驳回（reviewed=False）。

    审查动作写入 operation_logs 留痕（V-02 审计）。

    Args:
        plugin_id: 插件 ID。
        req: 审查请求（approved/comment）。
        operator: 运营方身份。
        db: 请求级异步数据库会话。

    Returns:
        PluginOut: 审查后的插件信息。

    Raises:
        HTTPException: 404 —— 插件不存在；403 —— 非运营方。
    """
    plugin = await _get_plugin(db, plugin_id)
    plugin.reviewed = req.approved
    db.add(OperationLog(
        user_id=operator.id,
        action="plugin_review",
        detail={"plugin_id": plugin.id, "plugin_name": plugin.name,
                "approved": req.approved, "comment": req.comment or ""},
        risk_level="normal",
    ))
    await db.commit()
    await db.refresh(plugin)
    return _plugin_out(plugin)


@router.post("/{plugin_id}/enable", response_model=PluginOut)
async def enable_plugin(
    plugin_id: int,
    _operator: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
) -> PluginOut:
    """运营方启用插件。

    Args:
        plugin_id: 插件 ID。
        _operator: 运营方身份。
        db: 请求级异步数据库会话。

    Returns:
        PluginOut: 启用后的插件信息。

    Raises:
        HTTPException: 404 —— 插件不存在；403 —— 非运营方。
    """
    plugin = await _get_plugin(db, plugin_id)
    plugin.enabled = True
    await db.commit()
    await db.refresh(plugin)
    return _plugin_out(plugin)


@router.post("/{plugin_id}/disable", response_model=PluginOut)
async def disable_plugin(
    plugin_id: int,
    _operator: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
) -> PluginOut:
    """运营方停用插件。

    Args:
        plugin_id: 插件 ID。
        _operator: 运营方身份。
        db: 请求级异步数据库会话。

    Returns:
        PluginOut: 停用后的插件信息。

    Raises:
        HTTPException: 404 —— 插件不存在；403 —— 非运营方。
    """
    plugin = await _get_plugin(db, plugin_id)
    plugin.enabled = False
    await db.commit()
    await db.refresh(plugin)
    return _plugin_out(plugin)


@router.post("/{plugin_id}/invoke")
async def invoke_plugin_endpoint(
    plugin_id: int,
    req: PluginInvokeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """手动调用插件（登录用户）：插件须已通过审核（reviewed）且启用（enabled）。

    Args:
        plugin_id: 插件 ID。
        req: 调用请求（payload）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        Dict[str, Any]: 插件返回结果 dict。

    Raises:
        HTTPException: 400 —— 插件未审核/未启用或调用失败；
            404 —— 插件不存在；401 —— 未登录。
    """
    plugin = await _get_plugin(db, plugin_id)
    if not plugin.reviewed or not plugin.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="插件未通过审核或未启用，无法调用",
        )
    try:
        return await invoke_plugin(plugin, req.payload)
    except PluginError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
