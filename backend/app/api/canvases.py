"""画布路由模块（文件级说明）。

实现 AI 画布域全部接口（prefix=/api/canvases，tags=canvases，画布归属校验 user_id）：
1. GET    ""                         —— 我的画布分页列表（含节点/连线计数）
2. POST   ""                         —— 创建画布（支持三种预置模板）
3. GET    /{canvas_id}               —— 画布完整图（节点 + 连线）
4. PUT    /{canvas_id}               —— 更新画布基础信息（标题/视口/缩略图）
5. DELETE /{canvas_id}               —— 软删除画布
6. PUT    /{canvas_id}/graph         —— 自动保存全量同步（upsert + 删除未提交项 +
                                        脏标记 + 自环/成环校验，RD-45/RD-48）
7. POST  /{canvas_id}/export        —— 画布 JSON 导出（前端下载）
8. POST  /import                    —— 画布 JSON 导入（成环校验同图同步）
9. POST  /{canvas_id}/nodes/{node_id}/execute —— 单节点执行
10. POST  /{canvas_id}/execute       —— DAG 增量执行（all/dirty/node，RD-45）
11. GET   /{canvas_id}/stream        —— SSE 实时推送节点状态与任务进度
12. POST  /{canvas_id}/assistant/chat —— 画布助手对话（LLM 失败离线降级）
13. POST  /api/canvas/nodes/{node_id}/sink —— 节点沉淀入资产库（A-10/CV-24，
        挂载于独立 sink_router，prefix=/api/canvas）

注意：LangGraph 全局对话由并行任务实现，本模块画布助手直接调用
providers.llm.chat 获取结构化画布操作指令。
"""

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.sse import sse_event, sse_stream
from app.core.security import get_current_user
from app.models import Canvas, CanvasEdge, CanvasNode, CanvasPlugin, Character, Prop, Scene, Task, User
from app.providers import llm as llm_provider
from app.schemas.canvas import (
    AssistantChatRequest,
    AssistantChatResponse,
    CanvasCreateRequest,
    CanvasEdgeOut,
    CanvasExecuteRequest,
    CanvasExecuteResult,
    CanvasGraphOut,
    CanvasImportRequest,
    CanvasListItemOut,
    CanvasListResponse,
    CanvasNodeOut,
    CanvasOut,
    CanvasSinkRequest,
    CanvasSinkResult,
    CanvasUpdateRequest,
    CloneSubtreeRequest,
    CloneSubtreeResult,
    GraphSyncRequest,
    NodeExecuteResult,
)
from app.services.canvas_service import (
    apply_assistant_actions,
    build_graph_out,
    create_canvas_with_template,
    ensure_project_owned,
    execute_node_once,
    get_owned_canvas,
    load_graph,
    run_canvas_execution,
    sync_graph,
)

router = APIRouter(prefix="/api/canvases", tags=["canvases"])


# ---------------------------------------------------------------------------
# 画布 CRUD
# ---------------------------------------------------------------------------
@router.get("", response_model=CanvasListResponse)
async def list_canvases(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    search: Optional[str] = Query(None, description="按画布名称模糊搜索"),
    type: Optional[str] = Query(None, description="画布类型过滤 standalone/studio_linked"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """我的画布分页列表（软删除画布不可见，含节点/连线计数等缩略字段）。

    Args:
        page: 页码（>=1）。
        page_size: 每页条数（1~100）。
        search: 名称模糊搜索词（可空）。
        type: 画布类型过滤（可空）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        Dict: {items, total, page, page_size} 分页结构。

    Raises:
        HTTPException: 401 —— 未登录。
    """
    conditions = [Canvas.user_id == user.id, Canvas.deleted_at.is_(None)]
    if search:
        conditions.append(Canvas.title.ilike(f"%{search}%"))
    if type:
        conditions.append(Canvas.type == type)

    total = await db.scalar(select(func.count()).select_from(Canvas).where(*conditions))
    rows = (
        await db.execute(
            select(Canvas)
            .where(*conditions)
            .order_by(Canvas.updated_at.desc(), Canvas.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    # 批量统计本页画布的节点/连线数量（避免 N+1 查询）
    canvas_ids = [c.id for c in rows]
    node_counts: Dict[int, int] = {}
    edge_counts: Dict[int, int] = {}
    if canvas_ids:
        node_rows = (
            await db.execute(
                select(CanvasNode.canvas_id, func.count())
                .where(CanvasNode.canvas_id.in_(canvas_ids))
                .group_by(CanvasNode.canvas_id)
            )
        ).all()
        node_counts = {cid: cnt for cid, cnt in node_rows}
        edge_rows = (
            await db.execute(
                select(CanvasEdge.canvas_id, func.count())
                .where(CanvasEdge.canvas_id.in_(canvas_ids))
                .group_by(CanvasEdge.canvas_id)
            )
        ).all()
        edge_counts = {cid: cnt for cid, cnt in edge_rows}

    items = [
        CanvasListItemOut(
            id=c.id,
            project_id=c.project_id,
            title=c.title,
            type=c.type,
            thumbnail_url=c.thumbnail_url,
            node_count=node_counts.get(c.id, 0),
            edge_count=edge_counts.get(c.id, 0),
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in rows
    ]
    return {"items": items, "total": int(total or 0), "page": page, "page_size": page_size}


@router.post("", response_model=CanvasGraphOut, status_code=201)
async def create_canvas(
    req: CanvasCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CanvasGraphOut:
    """创建画布；template 指定预置模板时自动预置节点与连线。

    模板节点构成（位置按 x=120*i, y=80 错开排布）：
    - script_to_storyboard：text(剧本段落) → generate(image) → video
    - character_sheet：text(角色描述) → generate(image) → asset_ref 占位
    - style_experiment：text(风格描述) → generate(image) ×2 并列

    Args:
        req: 创建请求（title / project_id / template）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        CanvasGraphOut: 新画布完整图（含预置节点/连线，带真实 id）。

    Raises:
        HTTPException: 400 —— template 非法或关联项目无权限；401 —— 未登录。
    """
    await ensure_project_owned(db, user.id, req.project_id)
    canvas = await create_canvas_with_template(db, user.id, req.title, req.project_id, req.template)
    await db.commit()
    nodes, edges = await load_graph(db, canvas.id)
    return build_graph_out(canvas, nodes, edges)


@router.get("/{canvas_id}", response_model=CanvasGraphOut)
async def get_canvas_graph(
    canvas_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CanvasGraphOut:
    """获取画布完整图（画布基础信息 + 全量节点 + 全量连线）。

    Args:
        canvas_id: 画布 ID。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        CanvasGraphOut: {canvas, nodes, edges} 完整图结构。

    Raises:
        HTTPException: 404 —— 画布不存在或无权访问。
    """
    canvas = await get_owned_canvas(db, user.id, canvas_id)
    nodes, edges = await load_graph(db, canvas.id)
    return build_graph_out(canvas, nodes, edges)


@router.put("/{canvas_id}", response_model=CanvasOut)
async def update_canvas(
    canvas_id: int,
    req: CanvasUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CanvasOut:
    """更新画布基础信息（标题 / 视口状态 / 缩略图 URL，None 字段不修改）。

    Args:
        canvas_id: 画布 ID。
        req: 更新请求。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        CanvasOut: 更新后的画布基础信息。

    Raises:
        HTTPException: 404 —— 画布不存在或无权访问。
    """
    canvas = await get_owned_canvas(db, user.id, canvas_id)
    if req.title is not None:
        canvas.title = req.title
    if req.viewport is not None:
        canvas.viewport = req.viewport
    if req.thumbnail_url is not None:
        canvas.thumbnail_url = req.thumbnail_url
    await db.commit()
    # commit 触发 onupdate 后 updated_at 列在实例上已过期，必须显式 refresh
    # （隐式延迟加载会在 Pydantic 同步序列化时触发 MissingGreenlet）
    await db.refresh(canvas)
    return CanvasOut.model_validate(canvas)


@router.delete("/{canvas_id}")
async def delete_canvas(
    canvas_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, str]:
    """软删除画布（写入 deleted_at，列表与详情即刻不可见）。

    Args:
        canvas_id: 画布 ID。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        Dict[str, str]: {"message": "画布已删除"}。

    Raises:
        HTTPException: 404 —— 画布不存在或无权访问。
    """
    canvas = await get_owned_canvas(db, user.id, canvas_id)
    canvas.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "画布已删除"}


# ---------------------------------------------------------------------------
# 图同步 / 导入导出
# ---------------------------------------------------------------------------
@router.put("/{canvas_id}/graph", response_model=CanvasGraphOut)
async def sync_canvas_graph(
    canvas_id: int,
    req: GraphSyncRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CanvasGraphOut:
    """自动保存全量同步：事务内 upsert 节点/连线 + 删除本次未提交项。

    校验：节点类型 10 类白名单；自环 400；应用层 DFS 成环检测
    （成环 400 "连线将形成环，已拒绝保存"，事务回滚数据不变）；
    data/config 深度变化自动置脏（RD-45）。

    Args:
        canvas_id: 画布 ID。
        req: 全量图（nodes/edges，节点 id 可空 = 插入）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        CanvasGraphOut: 同步后的完整图（全部带真实 id）。

    Raises:
        HTTPException: 400 —— 类型非法 / 引用缺失 / 自环 / 成环；404 —— 画布不存在。
    """
    canvas = await get_owned_canvas(db, user.id, canvas_id)
    await sync_graph(db, canvas, req.nodes, req.edges)
    await db.commit()
    nodes, edges = await load_graph(db, canvas.id)
    return build_graph_out(canvas, nodes, edges)


@router.post("/{canvas_id}/export", response_model=CanvasGraphOut)
async def export_canvas(
    canvas_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CanvasGraphOut:
    """画布 JSON 导出（结构同完整图接口，供前端下载为文件）。

    Args:
        canvas_id: 画布 ID。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        CanvasGraphOut: {canvas, nodes, edges} 导出负载。

    Raises:
        HTTPException: 404 —— 画布不存在或无权访问。
    """
    canvas = await get_owned_canvas(db, user.id, canvas_id)
    nodes, edges = await load_graph(db, canvas.id)
    return build_graph_out(canvas, nodes, edges)


@router.post("/import", response_model=CanvasGraphOut, status_code=201)
async def import_canvas(
    req: CanvasImportRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CanvasGraphOut:
    """JSON 导入建画布：先建画布行，再走图同步校验（自环/成环 400）。

    校验失败时整条事务回滚（含新建画布行），不残留半成品数据；
    graph 内节点可携带客户端临时 id 供连线引用，服务端自动映射真实 id。

    Args:
        req: 导入请求（title / project_id / graph）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        CanvasGraphOut: 新建画布完整图。

    Raises:
        HTTPException: 400 —— 模板图非法或成环；404 —— 画布/项目归属异常。
    """
    await ensure_project_owned(db, user.id, req.project_id)
    canvas = Canvas(user_id=user.id, title=req.title, project_id=req.project_id, type="standalone")
    db.add(canvas)
    await db.flush()  # 取画布 ID（校验失败时随事务回滚）
    await sync_graph(db, canvas, req.graph.nodes, req.graph.edges)
    await db.commit()
    nodes, edges = await load_graph(db, canvas.id)
    return build_graph_out(canvas, nodes, edges)


# ---------------------------------------------------------------------------
# 子图克隆（Task 9 画布增强）
# ---------------------------------------------------------------------------
@router.post("/{canvas_id}/clone-subtree", response_model=CloneSubtreeResult, status_code=201)
async def clone_subtree(
    canvas_id: int,
    req: CloneSubtreeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CloneSubtreeResult:
    """子图克隆：收集指定节点 + 两端均在集合内的边，克隆到目标画布（默认自身）。

    规则：
    - 节点须全部属于源画布（缺失返回 400）；
    - 仅克隆两端均在 node_ids 集合内的连线；
    - 克隆节点坐标整体 +40/+40 偏移，data/config 深拷贝，置脏待执行；
    - parent_group_id 不在克隆集合内时置 None（分组随原画布保留）；
    - 目标画布须归属当前用户（默认 = 源画布）。

    Args:
        canvas_id: 源画布 ID。
        req: 克隆请求（node_ids / target_canvas_id）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        CloneSubtreeResult: {nodes, edges} 克隆出的子图（带真实 id）。

    Raises:
        HTTPException: 400 —— 节点不属于源画布或目标画布无权限；
            404 —— 源/目标画布不存在。
    """
    source = await get_owned_canvas(db, user.id, canvas_id)
    target = source if req.target_canvas_id is None else await get_owned_canvas(
        db, user.id, req.target_canvas_id
    )

    src_nodes, src_edges = await load_graph(db, source.id)
    node_map = {n.id: n for n in src_nodes}
    id_set = set(req.node_ids)
    missing = id_set - set(node_map)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"节点不存在或不属于该画布: {sorted(missing)[:5]}",
        )

    # 克隆节点：坐标 +40/+40，data/config 深拷贝，置脏待执行
    cloned_nodes: List[CanvasNode] = []
    for nid in req.node_ids:  # 保持请求顺序，输出顺序可预期
        src = node_map[nid]
        clone = CanvasNode(
            canvas_id=target.id,
            node_type=src.node_type,
            position_x=src.position_x + 40,
            position_y=src.position_y + 40,
            size_w=src.size_w,
            size_h=src.size_h,
            z_index=src.z_index,
            data=json.loads(json.dumps(src.data, ensure_ascii=False, default=str)) if src.data else None,
            config=json.loads(json.dumps(src.config, ensure_ascii=False, default=str)) if src.config else None,
            is_dirty=True,
            status="idle",
        )
        db.add(clone)
        cloned_nodes.append(clone)
    await db.flush()  # 取克隆节点真实 id
    # 源节点 id → 克隆节点 id 映射（连线端点重映射用）
    id_mapping = {nid: clone.id for nid, clone in zip(req.node_ids, cloned_nodes)}

    # 克隆连线：两端均在集合内
    cloned_edges: List[CanvasEdge] = []
    for edge in src_edges:
        if edge.source_node_id in id_set and edge.target_node_id in id_set:
            clone = CanvasEdge(
                canvas_id=target.id,
                source_node_id=id_mapping[edge.source_node_id],
                target_node_id=id_mapping[edge.target_node_id],
                source_port=edge.source_port,
                target_port=edge.target_port,
            )
            db.add(clone)
            cloned_edges.append(clone)
    await db.flush()
    await db.commit()
    return CloneSubtreeResult(
        nodes=[CanvasNodeOut.model_validate(n) for n in cloned_nodes],
        edges=[CanvasEdgeOut.model_validate(e) for e in cloned_edges],
    )


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
@router.post("/{canvas_id}/nodes/{node_id}/execute", response_model=NodeExecuteResult)
async def execute_canvas_node(
    canvas_id: int,
    node_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NodeExecuteResult:
    """单节点执行：text/logic 即时执行；generate 转生成任务（冻结积分）；
    image/video/audio 等容器类仅标记完成（占位实现，仅作展示容器）。

    Args:
        canvas_id: 画布 ID。
        node_id: 节点 ID（必须属于该画布）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        NodeExecuteResult: 节点新状态（status/is_dirty/cache_ref/data/task_id）。

    Raises:
        HTTPException: 404 —— 画布或节点不存在；400 —— task_type 非法；409 —— 积分不足。
    """
    canvas = await get_owned_canvas(db, user.id, canvas_id)
    node = (
        await db.execute(
            select(CanvasNode).where(CanvasNode.id == node_id, CanvasNode.canvas_id == canvas.id)
        )
    ).scalar_one_or_none()
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="节点不存在或不属于该画布")
    # 加载全画布节点映射（mcp_tool 节点的 {{node:<id>.output}} 占位替换用）
    all_nodes, _all_edges = await load_graph(db, canvas.id)
    node_map = {n.id: n for n in all_nodes}
    outcome = await execute_node_once(db, canvas, node, node_map)
    await db.commit()
    return NodeExecuteResult(
        id=node.id,
        node_type=node.node_type,
        status=node.status,
        is_dirty=node.is_dirty,
        cache_ref=node.cache_ref,
        data=node.data,
        task_id=outcome.get("task_id"),
    )


@router.post("/{canvas_id}/execute", response_model=CanvasExecuteResult)
async def execute_canvas(
    canvas_id: int,
    req: CanvasExecuteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CanvasExecuteResult:
    """DAG 增量执行（RD-45）：按拓扑序执行 all/dirty/node 三种模式的节点集合。

    - all：全部节点；dirty：脏节点及其全部下游（强制重算），其余按缓存跳过；
      node：指定节点及其下游闭包（显式指定无视缓存）；
    - 非强制节点若"非脏且已有 cache_ref"→ 跳过（上游未变更复用缓存）；
    - text/logic 即时执行；generate 转 tasks（冻结积分）；容器类标记完成。

    Args:
        canvas_id: 画布 ID。
        req: 执行请求（mode，mode=node 时须带 node_id）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        CanvasExecuteResult: {executed, skipped, tasks} 节点/任务 ID 列表。

    Raises:
        HTTPException: 400 —— mode 非法 / node_id 缺失 / 图存在环；409 —— 积分不足。
    """
    canvas = await get_owned_canvas(db, user.id, canvas_id)
    result = await run_canvas_execution(db, canvas, req.mode, req.node_id)
    return CanvasExecuteResult(**result)


# ---------------------------------------------------------------------------
# SSE 实时推送
# ---------------------------------------------------------------------------
@router.get("/{canvas_id}/stream")
async def stream_canvas(
    canvas_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """SSE 实时推送画布节点状态与关联任务状态。

    每 1s 轮询一次：事件 `nodes`（{"nodes":[{id,status,is_dirty,cache_ref,data_output}]}）；
    当画布无 queued/running 任务且无执行中节点时推送 `done` 并结束；
    最长推送 300s 超时兜底退出。

    Args:
        canvas_id: 画布 ID。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        StreamingResponse: text/event-stream SSE 流。

    Raises:
        HTTPException: 404 —— 画布不存在或无权访问。
    """
    canvas = await get_owned_canvas(db, user.id, canvas_id)

    async def _event_gen() -> AsyncGenerator[str, None]:
        """SSE 事件生成器：轮询节点与任务状态并按需结束（内部函数）。

        Yields:
            str: SSE 文本帧（nodes / done 事件）。
        """
        for _ in range(300):  # 最长 300s（1s × 300 次轮询）
            nodes, _edges = await load_graph(db, canvas.id)
            node_ids = [n.id for n in nodes]
            active_tasks = 0
            if node_ids:
                active_tasks = int(
                    await db.scalar(
                        select(func.count())
                        .select_from(Task)
                        .where(
                            Task.canvas_node_id.in_(node_ids),
                            Task.status.in_(("queued", "running")),
                        )
                    ) or 0
                )
            executing = any(n.status in ("queued", "running") for n in nodes)
            payload = [
                {
                    "id": n.id,
                    "status": n.status,
                    "is_dirty": n.is_dirty,
                    "cache_ref": n.cache_ref,
                    "data_output": (n.data or {}).get("output") if isinstance(n.data, dict) else None,
                }
                for n in nodes
            ]
            yield sse_event("nodes", {"nodes": payload})
            if active_tasks == 0 and not executing:
                # 无排队/运行中任务且无执行中节点 → 推送完成事件并结束
                yield sse_event("done", {"canvas_id": canvas.id})
                return
            await asyncio.sleep(1)
        # 超时兜底：达到 300s 上限后强制结束推送
        yield sse_event("done", {"canvas_id": canvas.id, "reason": "timeout"})

    return await sse_stream(_event_gen())


# ---------------------------------------------------------------------------
# 画布助手
# ---------------------------------------------------------------------------
def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出文本中尽力提取 JSON 对象（内部函数）。

    依次尝试：直接解析 → 剥离 markdown 代码围栏 → 截取首尾大括号片段。

    Args:
        raw: LLM 原始输出文本。

    Returns:
        Optional[Dict[str, Any]]: 解析成功的字典；全部失败返回 None。
    """
    text = (raw or "").strip()
    candidates = [text]
    if text.startswith("```"):
        stripped = text.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
        candidates.append(stripped)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


@router.post("/{canvas_id}/assistant/chat", response_model=AssistantChatResponse)
async def assistant_chat(
    canvas_id: int,
    req: AssistantChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssistantChatResponse:
    """画布助手对话（CV 画布助手 Agent 的画布内入口）。

    流程：构造上下文（画布节点摘要 + 用户消息）→ 要求 LLM 输出
    {"reply", "actions":[add_node/add_edge/update_node]} → 解析后直接应用
    （add_edge 带成环校验，引入环的动作丢弃）→ 返回回复与应用后的图。
    LLM 调用失败或输出不可解析时离线降级：预置一个图像生成节点并应用。

    注意：LangGraph 全局对话由并行任务实现，此处直接调用 providers.llm.chat。

    Args:
        canvas_id: 画布 ID。
        req: 对话请求（message）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        AssistantChatResponse: {reply, applied, nodes, edges}。

    Raises:
        HTTPException: 404 —— 画布不存在或无权访问。
    """
    canvas = await get_owned_canvas(db, user.id, canvas_id)
    nodes, _edges = await load_graph(db, canvas.id)

    # 构造上下文：节点摘要（类型 + 状态 + data 截断）
    summaries: List[str] = []
    for n in nodes:
        data_str = json.dumps(n.data, ensure_ascii=False, default=str) if n.data is not None else ""
        summaries.append(f"#{n.id} [{n.node_type}] status={n.status} data={data_str[:80]}")
    context = "\n".join(summaries) if summaries else "（空画布）"

    system_prompt = (
        "你是漫序 MangaFlow 的 AI 画布助手，帮助用户编排漫画/漫剧生产节点。"
        '只输出一个 JSON 对象：{"reply": "给用户的中文回复", "actions": [动作列表]}。'
        "动作支持三种："
        '{"op":"add_node","node_type":"text|image|video|audio|generate|asset_ref|shot|compose|logic|group",'
        '"data":{},"config":{},"position_x":数字,"position_y":数字}；'
        '{"op":"add_edge","source_index":i,"target_index":j}（下标指向本次 actions 中 add_node 的顺序，从 0 开始）；'
        '{"op":"update_node","node_id":数字,"data":{}}（合并进节点 data）。'
        "generate 节点的 config.task_type 可为 image/video/tts，提示词放 data.prompt。"
    )
    user_content = f"当前画布节点：\n{context}\n\n用户消息：{req.message}"

    try:
        raw = await llm_provider.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
            json_mode=True,
        )
        parsed = _extract_json(raw)
        if parsed is None:
            raise ValueError("LLM 输出不是合法 JSON 对象")
        reply = str(parsed.get("reply") or "已按你的要求处理画布。")
        actions = parsed.get("actions")
        applied, out_nodes, out_edges = 0, nodes, _edges
        if isinstance(actions, list) and actions:
            applied, out_nodes, out_edges = await apply_assistant_actions(db, canvas, actions)
    except Exception:  # noqa: BLE001 —— LLM 失败/解析失败统一走离线降级
        # 离线降级：预置一个图像生成节点并应用，保证用户始终有可用产出
        fallback_actions: List[Dict[str, Any]] = [
            {
                "op": "add_node",
                "node_type": "generate",
                "data": {"prompt": req.message[:200]},
                "config": {"task_type": "image", "label": "图像生成（降级预置）"},
            }
        ]
        applied, out_nodes, out_edges = await apply_assistant_actions(db, canvas, fallback_actions)
        reply = "当前处于离线降级模式：已为你预置一个图像生成节点，可编辑提示词后执行。"

    await db.commit()
    return AssistantChatResponse(
        reply=reply,
        applied=applied,
        nodes=[CanvasNodeOut.model_validate(n) for n in out_nodes],
        edges=[CanvasEdgeOut.model_validate(e) for e in out_edges],
    )


# ---------------------------------------------------------------------------
# 画布沉淀回流（A-10/CV-24：画布 → 资产库）
# ---------------------------------------------------------------------------
# 节点级沉淀路由独立成 router（prefix=/api/canvas）：既有画布路由均以
# canvas_id 为路径前缀（/{canvas_id}/...），而沉淀以节点为操作主体，
# 归属经由 node → canvas → user 链路校验，无需客户端传画布 ID。
sink_router = APIRouter(prefix="/api/canvas", tags=["canvases"])

# 沉淀支持的资产类型白名单 → ORM 模型映射
_SINK_ASSET_MODELS = {"character": Character, "scene": Scene, "prop": Prop}

# 产物 URL 判定：http(s) 绝对地址或站内根路径（/storage/... 等）
_ARTIFACT_URL_RE = re.compile(r"^(https?://|/)")

# 视觉描述 LLM 补全超时（秒，RD-47：超时不阻断沉淀）
_VISUAL_PROMPT_TIMEOUT = 15.0


def _extract_artifact_url(node: CanvasNode) -> Optional[str]:
    """从图像节点中提取产物 URL（内部函数）。

    与前端 previewUrl 口径一致：优先取 data.output（执行产出），
    其次取 data.url（手动填写），仅接受 http(s):// 或 / 开头的字符串。

    Args:
        node: 画布节点 ORM 对象。

    Returns:
        Optional[str]: 产物 URL（无产物或格式不符时返回 None）。
    """
    data = node.data if isinstance(node.data, dict) else {}
    for key in ("output", "url"):
        val = data.get(key)
        if isinstance(val, str) and _ARTIFACT_URL_RE.match(val.strip()):
            return val.strip()
    return None


def _node_description_text(node: CanvasNode) -> str:
    """从节点 data/config 中提取描述类文本（内部函数，LLM 降级兜底用）。

    依次尝试 data.description / data.text / data.prompt，均缺失时回退
    label（data.label 优先，config.label 次之）。

    Args:
        node: 画布节点 ORM 对象。

    Returns:
        str: 描述文本（可能为空串）。
    """
    data = node.data if isinstance(node.data, dict) else {}
    config = node.config if isinstance(node.config, dict) else {}
    for src in (data.get("description"), data.get("text"), data.get("prompt"),
                data.get("label"), config.get("label")):
        if isinstance(src, str) and src.strip():
            return src.strip()
    return ""


async def _generate_visual_prompt(node: CanvasNode, name: str) -> str:
    """生成资产标准化视觉描述（RD-47 特征描述自动补全，内部函数）。

    调用 providers.llm.chat 依据名称与节点描述生成中文视觉描述；
    LLM 失败 / 超时（15s）时降级为节点描述或 label 文本，保证
    visual_prompt（NOT NULL 列）始终有值且不阻断沉淀主流程。

    Args:
        node: 画布节点 ORM 对象（提取描述上下文）。
        name: 资产名称。

    Returns:
        str: 视觉描述提示词（非空）。

    Raises:
        无：内部捕获全部 LLM 异常并降级。
    """
    desc_text = _node_description_text(node)
    fallback = desc_text or f"画布图像「{name}」：漫画风格，构图完整，光影统一。"
    try:
        raw = await asyncio.wait_for(
            llm_provider.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是漫序 MangaFlow 的资产整理助手，负责为资产库生成"
                            "标准化的中文视觉描述提示词。只输出一段视觉描述本身，"
                            "不要任何解释、引号或前后缀。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"资产名称：{name}\n节点描述：{desc_text or '（无）'}\n"
                            "请生成 80 字以内、可直接用于漫画风格图像生成的"
                            "标准化视觉描述（涵盖外观/构图/光影/风格要素）。"
                        ),
                    },
                ],
                temperature=0.4,
            ),
            timeout=_VISUAL_PROMPT_TIMEOUT,
        )
        return (raw or "").strip() or fallback
    except Exception:  # noqa: BLE001 —— RD-47：LLM 失败/超时统一降级，不阻断沉淀
        return fallback


async def _dedupe_asset_name(db: AsyncSession, model: type, project_id: int, name: str) -> str:
    """为资产名称去重（内部函数）：项目内同名时追加数字后缀。

    characters/scenes 存在 (project_id, name) 唯一索引，重名插入会触发
    IntegrityError，故沉淀前主动探测并生成可用名称（name 列 String(50)）。

    Args:
        db: 异步数据库会话。
        model: 资产 ORM 模型（Character/Scene/Prop）。
        project_id: 目标项目 ID。
        name: 期望名称。

    Returns:
        str: 项目内未占用的名称（长度不超过 50）。

    Raises:
        无：探测冲突自动加后缀重试。
    """
    base = name[:46]
    candidate, seq = name, 1
    while await db.scalar(select(model.id).where(model.project_id == project_id, model.name == candidate)):
        seq += 1
        candidate = f"{base}_{seq}"[:50]
    return candidate


@sink_router.post("/nodes/{node_id}/sink", response_model=CanvasSinkResult, status_code=201)
async def sink_node_to_asset(
    node_id: int,
    req: CanvasSinkRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CanvasSinkResult:
    """画布节点沉淀入资产库（A-10/CV-24 画布 → 资产库回流通道）。

    流程：归属校验（node → canvas → user）→ 类型与产物校验 → 幂等检查
    （canvas_nodes.meta.sinked_asset_*）→ RD-47 视觉描述 LLM 补全（15s 超时
    降级为节点描述/label）→ 按 asset_type 建 characters/scenes/props 记录
    （asset_url 指向画布产物，source='canvas'）→ 回写节点 meta → 提交。

    Args:
        node_id: 画布节点 ID。
        req: 沉淀请求（asset_type 必填；name 可选，缺省取节点 label 或
            "[画布] 节点{id}"）。
        user: 当前登录用户。
        db: 请求级异步数据库会话。

    Returns:
        CanvasSinkResult: {asset_id, asset_type, duplicated}；重复沉淀时
            duplicated=True 并返回既有资产 ID，不重复建资产。

    Raises:
        HTTPException: 400 —— 资产类型非法 / 非 image 节点 / 节点无产物 /
            独立画布未关联项目；404 —— 节点不存在或画布不属于当前用户；
            401 —— 未登录。
    """
    model = _SINK_ASSET_MODELS.get(req.asset_type)
    if model is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="资产类型非法，须为 character/scene/prop 之一")

    # 归属校验：节点 join 画布，画布须归属当前用户且未软删除
    row = (
        await db.execute(
            select(CanvasNode, Canvas)
            .join(Canvas, CanvasNode.canvas_id == Canvas.id)
            .where(
                CanvasNode.id == node_id,
                Canvas.user_id == user.id,
                Canvas.deleted_at.is_(None),
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="节点不存在或无权访问")
    node, canvas = row

    if node.node_type != "image":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="仅图像（image）类型节点支持沉淀入资产库")

    artifact_url = _extract_artifact_url(node)
    if artifact_url is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="该图像节点暂无产物 URL，无法沉淀入资产库")

    if canvas.project_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="独立画布暂不支持沉淀，请先将画布关联到项目")

    # 幂等：meta 已记录同类型沉淀且资产仍存在 → 直接返回既有 ID
    meta = dict(node.meta) if isinstance(node.meta, dict) else {}
    sinked_type = meta.get("sinked_asset_type")
    sinked_id = meta.get("sinked_asset_id")
    if sinked_type == req.asset_type and isinstance(sinked_id, int):
        existing = await db.get(model, sinked_id)
        if existing is not None and getattr(existing, "deleted_at", None) is None:
            return CanvasSinkResult(asset_id=sinked_id, asset_type=req.asset_type, duplicated=True)

    # 名称：请求指定 > 节点 label（data/config）> "[画布] 节点{id}"；项目内去重
    data = node.data if isinstance(node.data, dict) else {}
    config = node.config if isinstance(node.config, dict) else {}
    label = data.get("label") if isinstance(data.get("label"), str) else None
    label = label or (config.get("label") if isinstance(config.get("label"), str) else None)
    name = (req.name or (label.strip() if label and label.strip() else "") or f"[画布] 节点{node.id}").strip()[:50]
    name = await _dedupe_asset_name(db, model, canvas.project_id, name)

    # RD-47：视觉描述 LLM 补全（失败/超时降级为节点描述或 label，不阻断）
    visual_prompt = await _generate_visual_prompt(node, name)
    desc_text = _node_description_text(node)

    # 建资产记录：主照指向画布产物 URL，来源标记 canvas
    if req.asset_type == "character":
        asset = Character(
            project_id=canvas.project_id, name=name, visual_prompt=visual_prompt,
            asset_url=artifact_url, description=desc_text or None, source="canvas",
        )
    elif req.asset_type == "scene":
        asset = Scene(
            project_id=canvas.project_id, name=name, visual_prompt=visual_prompt,
            asset_url=artifact_url, description=desc_text or None, source="canvas",
        )
    else:
        asset = Prop(
            project_id=canvas.project_id, name=name, visual_prompt=visual_prompt,
            asset_url=artifact_url, description=desc_text or None, source="canvas",
        )
    db.add(asset)
    await db.flush()  # 取资产真实 ID 供 meta 回写

    # 幂等落点：整 dict 重新赋值确保 JSONB 变更被 ORM 跟踪
    node.meta = {**meta, "sinked_asset_type": req.asset_type, "sinked_asset_id": asset.id}
    await db.commit()
    return CanvasSinkResult(asset_id=asset.id, asset_type=req.asset_type, duplicated=False)
