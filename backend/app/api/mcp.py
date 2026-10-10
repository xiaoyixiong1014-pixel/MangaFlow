"""MCP 集成路由模块（文件级说明）。

MCP server 配置存于站点设置 KV（langgraph_store，namespace='site_settings'，
key='mcp_servers'，value 包装为 {"value": [server, ...]}，与 admin/settings.py
的写入形态一致），避免偏离 31 表架构新增表（CV-19）：
- GET    /api/mcp/servers                 读取 server 列表（登录用户）
- POST   /api/mcp/servers                 添加 server（运营方）
- DELETE /api/mcp/servers/{sid}           删除 server（运营方）
- POST   /api/mcp/servers/{sid}/tools     现连列举工具（登录用户）
- POST   /api/mcp/servers/{sid}/call      现连调用工具（登录用户）

本 router 挂载于 main.py。
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_operator
from app.models import LanggraphStore, User
from app.schemas.plugin import McpCallRequest, McpServerIn, McpServerOut, McpServersOut
from app.services.mcp_client import McpError, call_tool, list_tools

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

# MCP server 配置的 KV 命名空间与键（与站点设置复用同一命名空间）
_KV_NAMESPACE = "site_settings"
_KV_KEY = "mcp_servers"


async def _load_servers(db: AsyncSession) -> List[Dict[str, Any]]:
    """从站点设置 KV 读取 MCP server 配置列表（内部函数）。

    兼容两种历史形态：{"value": [...]} 包装（本模块 PUT 形态）与直接
    list（手工写入）。

    Args:
        db: 请求级异步数据库会话。

    Returns:
        List[Dict[str, Any]]: server 配置列表（无记录时为空列表）。
    """
    row = (
        await db.execute(
            select(LanggraphStore).where(
                LanggraphStore.namespace == _KV_NAMESPACE, LanggraphStore.key == _KV_KEY
            )
        )
    ).scalar_one_or_none()
    value = getattr(row, "value", None)
    if isinstance(value, dict) and isinstance(value.get("value"), list):
        return [s for s in value["value"] if isinstance(s, dict)]
    if isinstance(value, list):
        return [s for s in value if isinstance(s, dict)]
    return []


async def _save_servers(db: AsyncSession, servers: List[Dict[str, Any]]) -> None:
    """把 MCP server 配置列表 upsert 回站点设置 KV（内部函数）。

    Args:
        db: 请求级异步数据库会话（由调用方提交）。
        servers: server 配置列表。

    Returns:
        None
    """
    stmt = pg_insert(LanggraphStore).values(
        namespace=_KV_NAMESPACE,
        key=_KV_KEY,
        value={"value": servers},
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["namespace", "key"],
        set_={"value": {"value": servers}},
    )
    await db.execute(stmt)


async def _find_server(db: AsyncSession, sid: str) -> Dict[str, Any]:
    """按 sid 查找 server 配置（内部函数）。

    Args:
        db: 请求级异步数据库会话。
        sid: server 标识。

    Returns:
        Dict[str, Any]: server 配置。

    Raises:
        HTTPException: 404 —— server 不存在。
    """
    for server in await _load_servers(db):
        if str(server.get("id")) == sid:
            return server
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server 不存在")


def _server_out(server: Dict[str, Any]) -> McpServerOut:
    """把 KV 里的 server dict 转为出参模型（内部函数）。

    Args:
        server: server 配置 dict。

    Returns:
        McpServerOut: 出参模型（容忍字段缺失）。
    """
    headers = server.get("headers")
    return McpServerOut(
        id=str(server.get("id") or ""),
        name=str(server.get("name") or ""),
        url=str(server.get("url") or ""),
        headers=headers if isinstance(headers, dict) else None,
    )


@router.get("/servers", response_model=McpServersOut)
async def get_servers(
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> McpServersOut:
    """读取 MCP server 列表（站点设置 KV）。

    Args:
        _user: 当前登录用户（仅鉴权）。
        db: 请求级异步数据库会话。

    Returns:
        McpServersOut: {"servers": [...]}。

    Raises:
        HTTPException: 401 —— 未登录。
    """
    return McpServersOut(servers=[_server_out(s) for s in await _load_servers(db)])


@router.post("/servers", response_model=McpServersOut, status_code=201)
async def add_server(
    req: McpServerIn,
    _operator: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
) -> McpServersOut:
    """添加 MCP server（运营方）：生成短随机 id 后追加进 KV。

    Args:
        req: 添加请求（name/url/headers）。
        _operator: 运营方身份。
        db: 请求级异步数据库会话。

    Returns:
        McpServersOut: 添加后的完整 server 列表。

    Raises:
        HTTPException: 403 —— 非运营方；401 —— 未登录。
    """
    from uuid import uuid4

    servers = await _load_servers(db)
    servers.append({
        "id": uuid4().hex[:12],
        "name": req.name,
        "url": req.url,
        "headers": req.headers or None,
    })
    await _save_servers(db, servers)
    await db.commit()
    return McpServersOut(servers=[_server_out(s) for s in servers])


@router.delete("/servers/{sid}", response_model=McpServersOut)
async def delete_server(
    sid: str,
    _operator: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
) -> McpServersOut:
    """删除 MCP server（运营方）。

    Args:
        sid: server 标识。
        _operator: 运营方身份。
        db: 请求级异步数据库会话。

    Returns:
        McpServersOut: 删除后的完整 server 列表。

    Raises:
        HTTPException: 404 —— server 不存在；403 —— 非运营方。
    """
    servers = await _load_servers(db)
    remaining = [s for s in servers if str(s.get("id")) != sid]
    if len(remaining) == len(servers):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server 不存在")
    await _save_servers(db, remaining)
    await db.commit()
    return McpServersOut(servers=[_server_out(s) for s in remaining])


@router.post("/servers/{sid}/tools")
async def list_server_tools(
    sid: str,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """现连列举 MCP server 工具（tools/list）。

    Args:
        sid: server 标识。
        _user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        Dict[str, Any]: {"tools": [工具描述列表]}。

    Raises:
        HTTPException: 404 —— server 不存在；400 —— MCP 调用失败；401 —— 未登录。
    """
    server = await _find_server(db, sid)
    try:
        tools = await list_tools(server.get("url") or "", server.get("headers"))
    except McpError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
    return {"tools": tools}


@router.post("/servers/{sid}/call")
async def call_server_tool(
    sid: str,
    req: McpCallRequest,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """现连调用 MCP server 工具（tools/call）。

    Args:
        sid: server 标识。
        req: 调用请求（tool/arguments）。
        _user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        Dict[str, Any]: tools/call 的 result（content/isError 等）。

    Raises:
        HTTPException: 404 —— server 不存在；400 —— MCP 调用失败；401 —— 未登录。
    """
    server = await _find_server(db, sid)
    try:
        return await call_tool(
            server.get("url") or "", req.tool, req.arguments, server.get("headers")
        )
    except McpError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc
