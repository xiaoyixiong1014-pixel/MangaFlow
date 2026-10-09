"""画布业务服务模块（文件级说明）。

实现 AI 画布域核心逻辑，供路由层与测试复用：
- 图论算法：topological_sort（Kahn 拓扑排序）/ detect_cycle（迭代 DFS 成环检测，
  RD-48）/ collect_downstream（下游闭包）
- 图同步：sync_graph —— 自动保存全量同步（upsert + 删除未提交项 + 临时 id 映射 +
  脏标记 + 自环/成环校验，RD-45）
- 执行引擎：execute_node_once（单节点即时执行）与 run_canvas_execution
  （DAG 增量执行：all/dirty/node 三模式，脏闭包强制重算，其余复用缓存）
- 任务转换：create_generate_task —— generate 节点转 tasks 生成任务并冻结积分
- 画布助手：apply_assistant_actions —— 应用 LLM 产出的 add_node / add_edge /
  update_node 动作（add_edge 带成环校验）

约定：
- 所有校验失败以 HTTPException（400/404/409）抛出；
- 成环校验在事务提交前进行，抛出后整条请求回滚、数据不变；
- tasks 表的 meta JSONB 字段由并行任务补齐，本模块通过 hasattr(Task, "meta")
  运行时探测：已具备则写 meta，否则降级将附加信息 JSON 串写入 provider_task_id。
"""

import json
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Canvas, CanvasEdge, CanvasNode, Project, Task
from app.schemas.canvas import (
    CANVAS_TEMPLATES,
    NODE_TYPES,
    CanvasEdgeIn,
    CanvasEdgeOut,
    CanvasGraphOut,
    CanvasNodeIn,
    CanvasNodeOut,
    CanvasOut,
)
from app.services.billing_service import freeze_points

# ---------------------------------------------------------------------------
# 预置画布模板定义（节点构成与连线以中文注释说明）
# ---------------------------------------------------------------------------

# 模板节点规格：node_type + data + config；位置按 x=120*i, y=80 错开排布
_TEMPLATE_SPECS: Dict[str, List[Dict[str, Any]]] = {
    # 剧本转分镜模板：剧本段落(text) → 图像生成(generate, task_type=image) → 视频容器(video)
    "script_to_storyboard": [
        {"node_type": "text", "data": {"text": "", "label": "剧本段落"}, "config": None},
        {"node_type": "generate", "data": {"prompt": ""}, "config": {"task_type": "image", "label": "画面生成"}},
        {"node_type": "video", "data": {"label": "视频结果"}, "config": None},
    ],
    # 角色设定表模板：角色描述(text) → 图像生成(generate, task_type=image) → 资产引用占位(asset_ref)
    "character_sheet": [
        {"node_type": "text", "data": {"text": "", "label": "角色描述"}, "config": None},
        {"node_type": "generate", "data": {"prompt": ""}, "config": {"task_type": "image", "label": "角色立绘生成"}},
        {"node_type": "asset_ref", "data": {"label": "资产占位（生成完成后引用）"}, "config": None},
    ],
    # 风格实验模板：风格描述(text) → 两个并列图像生成(generate, task_type=image ×2)
    "style_experiment": [
        {"node_type": "text", "data": {"text": "", "label": "风格描述"}, "config": None},
        {"node_type": "generate", "data": {"prompt": "风格方案 A"}, "config": {"task_type": "image", "label": "风格 A"}},
        {"node_type": "generate", "data": {"prompt": "风格方案 B"}, "config": {"task_type": "image", "label": "风格 B"}},
    ],
}

# 模板连线（节点下标对，跟随 _TEMPLATE_SPECS 顺序）
_TEMPLATE_EDGES: Dict[str, List[Tuple[int, int]]] = {
    "script_to_storyboard": [(0, 1), (1, 2)],
    "character_sheet": [(0, 1), (1, 2)],
    "style_experiment": [(0, 1), (0, 2)],
}


# ---------------------------------------------------------------------------
# 图论算法
# ---------------------------------------------------------------------------
def detect_cycle(node_ids: Iterable[int], edge_pairs: Sequence[Tuple[int, int]]) -> bool:
    """检测有向图是否存在环（应用层 DFS 成环检测，RD-48）。

    采用迭代式三色 DFS（白=未访问/灰=在栈/黑=已完成），遇到回边（灰节点）
    即判定成环；迭代实现避免超大画布触发 Python 递归深度上限。

    Args:
        node_ids: 全部节点 ID 集合（可迭代）。
        edge_pairs: 有向边 (source_id, target_id) 列表。

    Returns:
        bool: True 表示存在环；False 表示无环。
    """
    adjacency: Dict[int, List[int]] = {nid: [] for nid in node_ids}
    for src, dst in edge_pairs:
        if src in adjacency:
            adjacency[src].append(dst)
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[int, int] = {nid: WHITE for nid in adjacency}
    for start in adjacency:
        if color[start] != WHITE:
            continue
        color[start] = GRAY
        stack: List[Tuple[int, Any]] = [(start, iter(adjacency[start]))]
        while stack:
            current, neighbors = stack[-1]
            advanced = False
            for nxt in neighbors:
                if color.get(nxt, BLACK) == GRAY:
                    return True  # 回边指向栈中灰节点 → 成环
                if color.get(nxt, BLACK) == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, iter(adjacency.get(nxt, []))))
                    advanced = True
                    break
            if not advanced:
                color[current] = BLACK
                stack.pop()
    return False


def topological_sort(node_ids: Iterable[int], edge_pairs: Sequence[Tuple[int, int]]) -> Optional[List[int]]:
    """对有向图做 Kahn 拓扑排序。

    Args:
        node_ids: 全部节点 ID 集合（可迭代）。
        edge_pairs: 有向边 (source_id, target_id) 列表。

    Returns:
        Optional[List[int]]: 拓扑序节点 ID 列表；图中存在环时返回 None。
    """
    id_list = list(node_ids)
    indegree: Dict[int, int] = {nid: 0 for nid in id_list}
    adjacency: Dict[int, List[int]] = {nid: [] for nid in id_list}
    for src, dst in edge_pairs:
        if src in adjacency and dst in indegree:
            adjacency[src].append(dst)
            indegree[dst] += 1
    queue = deque(nid for nid in id_list if indegree[nid] == 0)
    order: List[int] = []
    while queue:
        current = queue.popleft()
        order.append(current)
        for nxt in adjacency[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(id_list):
        return None  # 存在环，无法完成拓扑排序
    return order


def collect_downstream(seeds: Set[int], edge_pairs: Sequence[Tuple[int, int]]) -> Set[int]:
    """计算种子节点的全部下游（传递闭包，不含种子自身）。

    Args:
        seeds: 种子节点 ID 集合。
        edge_pairs: 有向边 (source_id, target_id) 列表。

    Returns:
        Set[int]: 所有可从种子沿边方向到达的节点 ID 集合。
    """
    adjacency: Dict[int, Set[int]] = {}
    for src, dst in edge_pairs:
        adjacency.setdefault(src, set()).add(dst)
    seen: Set[int] = set()
    stack = list(seeds)
    while stack:
        current = stack.pop()
        for nxt in adjacency.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def deep_changed(old: Any, new: Any) -> bool:
    """深度比较两个 JSON 值是否不同（排序键序列化后比较，忽略键序差异）。

    Args:
        old: 库中既有值（任意 JSON 兼容对象，可为 None）。
        new: 提交的新值（任意 JSON 兼容对象，可为 None）。

    Returns:
        bool: True 表示两者深度不同（需要置脏）。
    """

    def _dump(value: Any) -> str:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)

    return _dump(old) != _dump(new)


# ---------------------------------------------------------------------------
# 画布与图的加载 / 归属校验
# ---------------------------------------------------------------------------
async def get_owned_canvas(db: AsyncSession, user_id: int, canvas_id: int) -> Canvas:
    """按 ID 获取当前用户名下未删除的画布（归属校验）。

    Args:
        db: 异步数据库会话。
        user_id: 当前用户 ID。
        canvas_id: 画布 ID。

    Returns:
        Canvas: 归属校验通过的画布 ORM 对象。

    Raises:
        HTTPException: 404 —— 画布不存在、已软删或不属于当前用户。
    """
    canvas = (
        await db.execute(
            select(Canvas).where(
                Canvas.id == canvas_id,
                Canvas.user_id == user_id,
                Canvas.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if canvas is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="画布不存在或无权访问")
    return canvas


async def ensure_project_owned(db: AsyncSession, user_id: int, project_id: Optional[int]) -> None:
    """校验关联项目归属（project_id 为空时直接放行）。

    Args:
        db: 异步数据库会话。
        user_id: 当前用户 ID。
        project_id: 待校验的项目 ID（可空）。

    Returns:
        None

    Raises:
        HTTPException: 400 —— 项目不存在或不属于当前用户。
    """
    if project_id is None:
        return
    project = (
        await db.execute(select(Project).where(Project.id == project_id, Project.user_id == user_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="关联项目不存在或无权限")


async def load_graph(db: AsyncSession, canvas_id: int) -> Tuple[List[CanvasNode], List[CanvasEdge]]:
    """加载画布全部节点与连线（按 id 升序，保证输出顺序稳定）。

    Args:
        db: 异步数据库会话。
        canvas_id: 画布 ID。

    Returns:
        Tuple[List[CanvasNode], List[CanvasEdge]]: (节点列表, 连线列表)。
    """
    nodes = (
        await db.execute(
            select(CanvasNode).where(CanvasNode.canvas_id == canvas_id).order_by(CanvasNode.id)
        )
    ).scalars().all()
    edges = (
        await db.execute(
            select(CanvasEdge).where(CanvasEdge.canvas_id == canvas_id).order_by(CanvasEdge.id)
        )
    ).scalars().all()
    return list(nodes), list(edges)


def build_graph_out(canvas: Canvas, nodes: List[CanvasNode], edges: List[CanvasEdge]) -> CanvasGraphOut:
    """把画布 ORM 对象组装为完整图响应模型。

    Args:
        canvas: 画布 ORM 对象。
        nodes: 节点 ORM 列表。
        edges: 连线 ORM 列表。

    Returns:
        CanvasGraphOut: {canvas, nodes, edges} 完整图结构。
    """
    return CanvasGraphOut(
        canvas=CanvasOut.model_validate(canvas),
        nodes=[CanvasNodeOut.model_validate(n) for n in nodes],
        edges=[CanvasEdgeOut.model_validate(e) for e in edges],
    )


# ---------------------------------------------------------------------------
# 画布创建（含模板预置）
# ---------------------------------------------------------------------------
async def create_canvas_with_template(
    db: AsyncSession, user_id: int, title: str, project_id: Optional[int], template: Optional[str]
) -> Canvas:
    """创建画布；指定模板时预置节点与连线。

    模板节点构成（位置按 x=120*i, y=80 错开排布，全部置脏待执行）：
    - script_to_storyboard：text(剧本段落) → generate(image) → video
    - character_sheet：text(角色描述) → generate(image) → asset_ref 占位
    - style_experiment：text(风格描述) → generate(image) ×2 并列

    Args:
        db: 异步数据库会话（不提交，由调用方统一提交/回滚）。
        user_id: 拥有者用户 ID。
        title: 画布名称。
        project_id: 关联项目 ID（可空）。
        template: 模板名（None 表示空白画布）。

    Returns:
        Canvas: 新建的画布 ORM 对象（已 flush，含自增 ID）。

    Raises:
        HTTPException: 400 —— template 取值不在允许范围内。
    """
    if template is not None and template not in CANVAS_TEMPLATES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"template 非法，允许：{', '.join(CANVAS_TEMPLATES)} 或 null",
        )
    canvas = Canvas(user_id=user_id, title=title, project_id=project_id, type="standalone")
    db.add(canvas)
    await db.flush()  # 取画布自增 ID 供节点外键使用

    if template is None:
        return canvas

    specs = _TEMPLATE_SPECS[template]
    created: List[CanvasNode] = []
    for index, spec in enumerate(specs):
        node = CanvasNode(
            canvas_id=canvas.id,
            node_type=spec["node_type"],
            position_x=120 * index,  # 模板节点横向错开排布
            position_y=80,
            data=spec.get("data"),
            config=spec.get("config"),
            is_dirty=True,  # 预置节点默认置脏，等待首次执行
            status="idle",
        )
        db.add(node)
        created.append(node)
    await db.flush()  # 取节点自增 ID 供连线外键使用
    for src_idx, dst_idx in _TEMPLATE_EDGES[template]:
        db.add(CanvasEdge(
            canvas_id=canvas.id,
            source_node_id=created[src_idx].id,
            target_node_id=created[dst_idx].id,
        ))
    await db.flush()
    return canvas


# ---------------------------------------------------------------------------
# 图全量同步（自动保存）
# ---------------------------------------------------------------------------
async def sync_graph(
    db: AsyncSession,
    canvas: Canvas,
    nodes_in: List[CanvasNodeIn],
    edges_in: List[CanvasEdgeIn],
) -> Tuple[List[CanvasNode], List[CanvasEdge]]:
    """图全量同步（自动保存核心，供 PUT graph 与 JSON 导入复用）。

    处理流程（单事务，任何校验失败抛 400 后由调用方回滚、数据不变）：
    1. 节点 upsert：有 id 且为本画布已有节点 → 更新；否则插入新节点
       （提交 id 视为客户端临时 id，可被连线引用并自动映射真实 id）；
       data/config 与库中值深度比较，不同则 is_dirty=True，新增节点默认脏；
    2. 删除本次未提交的连线；
    3. 连线端点解析（真实 id / 临时 id），source==target 判自环 400；
    4. 应用层 DFS 成环检测，成环 400（"连线将形成环，已拒绝保存"）；
    5. 连线 upsert；
    6. 删除本次未提交的节点（连线已先行处理，避免外键冲突；DB 层
       ck_ce_no_self_loop / FK 级联提供双保险）。

    Args:
        db: 异步数据库会话（不提交，由调用方统一提交/回滚）。
        canvas: 目标画布 ORM 对象。
        nodes_in: 提交的全量节点列表。
        edges_in: 提交的全量连线列表。

    Returns:
        Tuple[List[CanvasNode], List[CanvasEdge]]: 同步后的（节点列表, 连线列表），
        全部为带真实 id 的 ORM 对象。

    Raises:
        HTTPException: 400 —— 节点类型非法 / 引用了不存在或未提交的节点 /
            自环 / 成环（"连线将形成环，已拒绝保存"）。
    """
    existing_nodes, existing_edges = await load_graph(db, canvas.id)
    existing_node_map = {n.id: n for n in existing_nodes}
    existing_edge_map = {e.id: e for e in existing_edges}

    kept_node_ids: Set[int] = set()
    updated_nodes: List[CanvasNode] = []
    new_pairs: List[Tuple[Optional[int], CanvasNode]] = []  # (提交侧临时 id 或 None, 新节点)
    parent_pending: List[Tuple[CanvasNode, Optional[int]]] = []  # (节点, 提交的 parent_group_id)

    # ---------- 1) 节点 upsert ----------
    for item in nodes_in:
        if item.node_type not in NODE_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"节点类型非法: {item.node_type}（允许：{'/'.join(NODE_TYPES)}）",
            )
        if item.id is not None and item.id in existing_node_map:
            # 更新既有节点
            node = existing_node_map[item.id]
            node.position_x = item.position_x
            node.position_y = item.position_y
            node.size_w = item.size_w
            node.size_h = item.size_h
            node.z_index = item.z_index
            # data/config 深度比较：内容变化即置脏（RD-45 增量执行依据）
            if deep_changed(node.data, item.data) or deep_changed(node.config, item.config):
                node.is_dirty = True
            node.data = item.data
            node.config = item.config
            kept_node_ids.add(node.id)
            updated_nodes.append(node)
        else:
            # 插入新节点（提交 id 视为客户端临时 id，仅用于同批连线引用）
            node = CanvasNode(
                canvas_id=canvas.id,
                node_type=item.node_type,
                position_x=item.position_x,
                position_y=item.position_y,
                size_w=item.size_w,
                size_h=item.size_h,
                z_index=item.z_index,
                data=item.data,
                config=item.config,
                is_dirty=True,  # 新增节点默认置脏
                status="idle",
            )
            db.add(node)
            new_pairs.append((item.id, node))
        parent_pending.append((node, item.parent_group_id))

    if new_pairs:
        await db.flush()  # 取自增真实 id
    # 临时 id → 真实 id 映射表
    id_map: Dict[int, int] = {temp: n.id for temp, n in new_pairs if temp is not None}
    kept_node_ids.update(n.id for _, n in new_pairs)

    def _resolve_ref(ref: Optional[int]) -> Optional[int]:
        """把提交侧节点引用解析为真实节点 id（内部闭包函数）。

        Args:
            ref: 提交侧引用（真实 id / 临时 id / None）。

        Returns:
            Optional[int]: 真实节点 id；入参为 None 时返回 None。

        Raises:
            HTTPException: 400 —— 引用了不存在或本次未提交（将被删除）的节点。
        """
        if ref is None:
            return None
        if ref in id_map:
            return id_map[ref]
        if ref in existing_node_map and ref in kept_node_ids:
            return ref
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"引用了不存在或本次未提交的节点: {ref}",
        )

    # 分组父节点引用解析（支持临时 id；指向被删节点则 400）
    for node, ref in parent_pending:
        node.parent_group_id = _resolve_ref(ref)

    # ---------- 2) 删除本次未提交的连线 ----------
    kept_edge_ids = {e.id for e in edges_in if e.id is not None}
    for edge in existing_edges:
        if edge.id not in kept_edge_ids:
            await db.delete(edge)
    await db.flush()

    # ---------- 3) 连线端点解析与自环校验 ----------
    resolved: List[Tuple[Optional[int], int, int, Optional[str], Optional[str]]] = []
    for e in edges_in:
        src = _resolve_ref(e.source_node_id)
        dst = _resolve_ref(e.target_node_id)
        if src == dst:
            # 自环校验（DB 层另有 ck_ce_no_self_loop CHECK 约束，双保险）
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="连线不允许自环（source_node_id 与 target_node_id 相同）",
            )
        resolved.append((e.id, src, dst, e.source_port, e.target_port))

    # ---------- 4) 应用层 DFS 成环检测（RD-48）----------
    if detect_cycle(kept_node_ids, [(s, t) for _, s, t, _, _ in resolved]):
        # 抛出后整条请求事务回滚，画布数据保持不变
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="连线将形成环，已拒绝保存")

    # ---------- 5) 连线 upsert ----------
    final_edges: List[CanvasEdge] = []
    for edge_id, src, dst, src_port, dst_port in resolved:
        if edge_id is not None and edge_id in existing_edge_map:
            edge = existing_edge_map[edge_id]
            edge.source_node_id = src
            edge.target_node_id = dst
            edge.source_port = src_port
            edge.target_port = dst_port
        else:
            edge = CanvasEdge(
                canvas_id=canvas.id,
                source_node_id=src,
                target_node_id=dst,
                source_port=src_port,
                target_port=dst_port,
            )
            db.add(edge)
        final_edges.append(edge)
    await db.flush()

    # ---------- 6) 删除本次未提交的节点 ----------
    stale_ids = {n.id for n in existing_nodes if n.id not in kept_node_ids}
    if stale_ids:
        # 父分组被删除的保留子节点就地脱离分组（DB 层 ondelete=SET NULL 兜底）
        for node, _ in parent_pending:
            if node.parent_group_id in stale_ids:
                node.parent_group_id = None
        for node in existing_nodes:
            if node.id in stale_ids:
                await db.delete(node)
        await db.flush()

    final_nodes = sorted([*updated_nodes, *(n for _, n in new_pairs)], key=lambda n: n.id)
    final_edges.sort(key=lambda e: e.id)
    return final_nodes, final_edges


# ---------------------------------------------------------------------------
# 节点执行
# ---------------------------------------------------------------------------
def mark_node_done(node: CanvasNode) -> None:
    """把节点标记为执行成功：status=done、写入缓存引用、清除脏标记（内部通用）。

    缓存引用格式 node_{id}_{时间戳}：节点模型未单独映射 updated_at，
    以执行时刻 UTC 时间戳保证每次执行的引用值不同（RD-45 缓存复用依据）。

    Args:
        node: 目标节点 ORM 对象（就地修改，不提交）。

    Returns:
        None
    """
    node.status = "done"
    node.is_dirty = False
    node.cache_ref = f"node_{node.id}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"


async def create_generate_task(db: AsyncSession, canvas: Canvas, node: CanvasNode) -> Task:
    """把 generate 节点转换为 tasks 生成任务并冻结积分。

    规则：
    - 任务类型由 config.task_type 决定（image/video/tts，缺省 image，非法 400）；
    - 成本冻结：image=10；video=duration*20（无 duration 按 60）；tts=5；
    - 幂等键 canvas_{node_id}_{uuid}；任务行挂 canvas_node_id；
    - 附加信息 {"target_type","canvas_node_id","prompt"} 写入 tasks.meta
      （模型尚无 meta 字段时降级 JSON 串写入 provider_task_id 并截断到 100 字符）；
    - 节点置 status=queued、is_dirty=False，并写任务级缓存引用
      task_{task_id}，供增量执行按缓存规则跳过重复提交。

    Args:
        db: 异步数据库会话（freeze_points 内部会提交事务）。
        canvas: 所属画布 ORM 对象（取 user_id / project_id）。
        node: generate 类型节点 ORM 对象。

    Returns:
        Task: 新建的生成任务 ORM 对象（已 flush 并完成积分冻结）。

    Raises:
        HTTPException: 400 —— config.task_type 非法；409 —— 积分余额不足。
    """
    config = node.config or {}
    data = node.data or {}
    task_type = config.get("task_type") or "image"
    if task_type not in ("image", "video", "tts"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"config.task_type 非法: {task_type}（允许 image/video/tts）",
        )
    prompt = data.get("prompt") or config.get("prompt") or ""
    # 成本冻结规则：image=10；video=duration*20（无/非法 duration 按 60）；tts=5
    if task_type == "image":
        cost = 10
    elif task_type == "video":
        duration = config.get("duration") or data.get("duration")
        try:
            cost = int(duration) * 20 if duration and int(duration) > 0 else 60
        except (TypeError, ValueError):
            cost = 60
    else:
        cost = 5

    idem = f"canvas_{node.id}_{uuid4().hex}"
    meta_info = {"target_type": "canvas_node", "canvas_node_id": node.id, "prompt": str(prompt)[:200]}
    task_kwargs: Dict[str, Any] = dict(
        project_id=canvas.project_id,
        canvas_node_id=node.id,
        type=task_type,
        phase="full",
        status="queued",
        cost_points=cost,
        idempotency_key=idem,
    )
    if hasattr(Task, "meta"):
        # tasks 表已具备 meta JSONB 字段（并行任务补齐）→ 附加信息直接写 meta
        task_kwargs["meta"] = meta_info
    else:
        # 当前 Task 模型尚无 meta 字段：附加信息 JSON 序列化后放入 provider_task_id
        # （String(100)，超长截断）；meta 字段落库后 hasattr 探测自动切换为写 meta。
        task_kwargs["provider_task_id"] = json.dumps(meta_info, ensure_ascii=False)[:100]

    task = Task(**task_kwargs)
    db.add(task)
    await db.flush()  # 取任务自增 ID 供积分冻结流水关联
    # 冻结积分（幂等键与任务一致；余额不足抛 409，整请求回滚）
    await freeze_points(db, canvas.user_id, cost, ref_task_id=task.id, idempotency_key=idem)
    # 节点进入排队态并写任务级缓存引用（排队期间重复执行按缓存规则跳过）
    node.status = "queued"
    node.is_dirty = False
    node.cache_ref = f"task_{task.id}"
    return task


async def execute_node_once(db: AsyncSession, canvas: Canvas, node: CanvasNode) -> Dict[str, Any]:
    """执行单个画布节点（单节点执行接口与 DAG 执行引擎共用的原子操作）。

    分派语义：
    - text：即时执行，data.output = data.text（演示语义）；
    - logic：按 data.action 对 data.input 做演示变换写入 data.output
      （uppercase=转大写 / reverse=反转字符串 / noop=原样透传，未知动作按 noop）；
    - generate：转 tasks 生成任务（冻结积分），节点进入 queued；
    - image/video/audio/asset_ref/shot/compose/group：仅作结果展示容器，
      当前为占位实现（不产生真实生成），直接标记完成。

    Args:
        db: 异步数据库会话（generate 场景内部可能提交事务）。
        canvas: 所属画布 ORM 对象。
        node: 目标节点 ORM 对象（就地修改）。

    Returns:
        Dict[str, Any]: {"status": 节点执行后状态, "task_id": 生成的任务 ID 或 None}。

    Raises:
        HTTPException: 400 —— generate 节点 config.task_type 非法；409 —— 积分不足。
    """
    node_type = node.node_type
    if node_type == "text":
        # 文本节点：即时执行，输出 = 输入文本
        data = dict(node.data or {})
        data["output"] = data.get("text", "")
        node.data = data
        mark_node_done(node)
        return {"status": node.status, "task_id": None}
    if node_type == "logic":
        # 逻辑节点：演示语义的确定性变换（uppercase / reverse / noop）
        data = dict(node.data or {})
        action = data.get("action", "noop")
        raw = data.get("input", "")
        if action == "uppercase":
            data["output"] = str(raw).upper()
        elif action == "reverse":
            data["output"] = str(raw)[::-1]
        else:
            data["output"] = raw  # noop：原样透传
        node.data = data
        mark_node_done(node)
        return {"status": node.status, "task_id": None}
    if node_type == "generate":
        # 生成类节点：转生成任务（节点最终由任务完成回调置 done，此处入队）
        task = await create_generate_task(db, canvas, node)
        return {"status": node.status, "task_id": task.id}
    # 其余类型（image/video/audio/asset_ref/shot/compose/group）均为展示容器占位
    mark_node_done(node)
    return {"status": node.status, "task_id": None}


async def run_canvas_execution(
    db: AsyncSession,
    canvas: Canvas,
    mode: str,
    node_id: Optional[int] = None,
) -> Dict[str, List[int]]:
    """画布 DAG 增量执行主逻辑（RD-45 核心，供 API 与测试复用）。

    执行模型：
    - 拓扑排序（Kahn）后按序遍历；
    - mode='all'：遍历全部节点；mode='dirty'：脏节点及其全部下游强制重算
      （must_run 闭包优先于缓存），其余节点按缓存规则判定；
      mode='node'：指定节点及其下游闭包全部强制执行（显式指定无视缓存）；
    - 逐节点判定：非 must_run 且"非脏且已有 cache_ref"→ 跳过
      （上游未变更复用缓存，RD-45 增量语义）；否则执行：
      text/logic 即时执行，generate 转 tasks（冻结积分），容器类标记完成；
    - 全程在一个请求事务内，结束时统一提交（generate 冻结积分会中途提交，
      失败时已执行部分保留，符合增量执行语义）。

    Args:
        db: 异步数据库会话（函数内部提交事务）。
        canvas: 目标画布 ORM 对象。
        mode: 执行模式 all/dirty/node。
        node_id: mode='node' 时的起点节点 ID（其余模式忽略）。

    Returns:
        Dict[str, List[int]]: {"executed": 实际执行节点 ID 列表（拓扑序）,
        "skipped": 缓存复用跳过的节点 ID 列表, "tasks": 生成的任务 ID 列表}。

    Raises:
        HTTPException: 400 —— mode 非法 / mode=node 缺少有效 node_id /
            画布存在环无法执行；409 —— generate 节点冻结积分余额不足。
    """
    if mode not in ("all", "dirty", "node"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mode 仅支持 all/dirty/node")
    result: Dict[str, List[int]] = {"executed": [], "skipped": [], "tasks": []}

    nodes, edges = await load_graph(db, canvas.id)
    if not nodes:
        return result
    node_ids = [n.id for n in nodes]
    edge_pairs = [(e.source_node_id, e.target_node_id) for e in edges]
    order = topological_sort(node_ids, edge_pairs)
    if order is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="画布存在环，无法执行")
    node_map = {n.id: n for n in nodes}

    if mode == "node":
        # 显式指定：起点及其下游闭包强制执行（无视缓存）
        if node_id is None or node_id not in node_map:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="mode=node 需提供画布内有效的 node_id",
            )
        closure = {node_id} | collect_downstream({node_id}, edge_pairs)
        traverse = [nid for nid in order if nid in closure]
        must_run: Set[int] = set(closure)
    elif mode == "dirty":
        # 脏节点 + 其全部下游强制重算；其余节点按缓存规则跳过
        dirty_seeds = {n.id for n in nodes if n.is_dirty}
        must_run = dirty_seeds | collect_downstream(dirty_seeds, edge_pairs)
        traverse = order
    else:  # all
        must_run = set()
        traverse = order

    for nid in traverse:
        node = node_map[nid]
        if nid not in must_run and not node.is_dirty and node.cache_ref:
            # 上游未变更：复用缓存（RD-45），计为 skipped
            result["skipped"].append(nid)
            continue
        outcome = await execute_node_once(db, canvas, node)
        if outcome.get("task_id"):
            result["tasks"].append(outcome["task_id"])
        result["executed"].append(nid)
    await db.commit()
    return result


# ---------------------------------------------------------------------------
# 画布助手动作应用
# ---------------------------------------------------------------------------
async def apply_assistant_actions(
    db: AsyncSession, canvas: Canvas, actions: List[Dict[str, Any]]
) -> Tuple[int, List[CanvasNode], List[CanvasEdge]]:
    """解析并应用画布助手产出的动作列表（add_node / add_edge / update_node）。

    - add_node：node_type 须为 10 类之一（非法动作静默丢弃），新节点置脏；
      未提供坐标时按 x=120*序号, y=80 顺延排布；
    - add_edge：source_index/target_index 指向本次动作批内 add_node 的顺序
      下标（从 0 开始）；下标越界、自环或引入环的动作被丢弃（不成环才落库）；
    - update_node：按 node_id 定位节点，data 与既有值浅合并，内容变化置脏。

    Args:
        db: 异步数据库会话（不提交，由调用方统一提交）。
        canvas: 目标画布 ORM 对象。
        actions: 动作字典列表（来自 LLM 解析或离线降级预置）。

    Returns:
        Tuple[int, List[CanvasNode], List[CanvasEdge]]:
        (成功应用的动作数, 应用后的节点列表, 应用后的连线列表)。
    """
    nodes, edges = await load_graph(db, canvas.id)
    all_node_ids = [n.id for n in nodes]
    all_pairs = [(e.source_node_id, e.target_node_id) for e in edges]
    added_nodes: List[CanvasNode] = []
    applied = 0
    slot = len(nodes)  # 新节点默认排布序号

    for action in actions or []:
        if not isinstance(action, dict):
            continue
        op = action.get("op")
        if op == "add_node":
            node_type = action.get("node_type")
            if node_type not in NODE_TYPES:
                continue
            payload_data = action.get("data") if isinstance(action.get("data"), dict) else None
            payload_config = action.get("config") if isinstance(action.get("config"), dict) else None
            try:
                pos_x = float(action.get("position_x", 120 * slot))
                pos_y = float(action.get("position_y", 80))
            except (TypeError, ValueError):
                pos_x, pos_y = 120 * slot, 80
            node = CanvasNode(
                canvas_id=canvas.id,
                node_type=node_type,
                position_x=pos_x,
                position_y=pos_y,
                data=payload_data,
                config=payload_config,
                is_dirty=True,  # 助手新增节点默认置脏
                status="idle",
            )
            db.add(node)
            await db.flush()  # 取真实 id 供后续 add_edge 引用
            added_nodes.append(node)
            all_node_ids.append(node.id)
            slot += 1
            applied += 1
        elif op == "add_edge":
            try:
                src = added_nodes[int(action.get("source_index"))].id
                dst = added_nodes[int(action.get("target_index"))].id
            except (TypeError, ValueError, IndexError):
                continue  # 下标非法，丢弃该动作
            if src == dst:
                continue  # 自环动作丢弃
            if detect_cycle(all_node_ids, [*all_pairs, (src, dst)]):
                continue  # 引入环的动作丢弃（不中断其余动作）
            db.add(CanvasEdge(canvas_id=canvas.id, source_node_id=src, target_node_id=dst))
            all_pairs.append((src, dst))
            applied += 1
        elif op == "update_node":
            target = next((n for n in nodes if n.id == action.get("node_id")), None)
            new_data = action.get("data")
            if target is None or not isinstance(new_data, dict):
                continue
            merged = {**(target.data or {}), **new_data}
            if deep_changed(target.data, merged):
                target.is_dirty = True
            target.data = merged
            applied += 1

    await db.flush()
    final_nodes, final_edges = await load_graph(db, canvas.id)
    return applied, final_nodes, final_edges
