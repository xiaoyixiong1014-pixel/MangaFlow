"""画布路由模块（文件级说明）。

实现 AI 画布域全部接口（prefix=/api/canvases，tags=canvases，画布归属校验 user_id）：
1. GET    ""                         —— 我的画布分页列表（含节点/连线计数）
2. POST   ""                         —— 创建画布（支持三种预置模板）
3. GET    /{canvas_id}               —— 画布完整图（节点 + 连线）
4. PUT    /{canvas_id}               —— 更新画布基础信息（标题/视口/缩略图）
5. DELETE /{canvas_id}               —— 软删除画布
6. PUT    /{canvas_id}/graph         —— 自动保存全量同步（upsert + 删除未提交项 +
                                        脏标记 + 自环/成环校验，RD-45/RD-48）
7. POST   /{canvas_id}/export        —— 画布 JSON 导出（前端下载）
8. POST   /import                    —— 画布 JSON 导入（成环校验同图同步）
9. POST   /{canvas_id}/nodes/{node_id}/execute —— 单节点执行
10. POST  /{canvas_id}/execute       —— DAG 增量执行（all/dirty/node，RD-45）
11. GET   /{canvas_id}/stream        —— SSE 实时推送节点状态与任务进度
12. POST  /{canvas_id}/assistant/chat —— 画布助手对话（LLM 失败离线降级）

注意：LangGraph 全局对话由并行任务实现，本模块画布助手直接调用
providers.llm.chat 获取结构化画布操作指令。
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.sse import sse_event, sse_stream
from app.core.security import get_current_user
from app.models import Canvas, CanvasEdge, CanvasNode, Task, User
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
    CanvasUpdateRequest,
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
    outcome = await execute_node_once(db, canvas, node)
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
