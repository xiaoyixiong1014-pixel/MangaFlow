"""AI 画布接口测试模块（文件级说明）。

覆盖链路：
- 模板建画布（预置节点与连线）
- 图全量同步（graph 保存：修改节点 data 后持久化）
- 自环连线 400、成环连线 400 且数据未变（事务回滚）
- DAG 执行 all：text 节点产出 output、generate 节点转任务（冻结积分）
- dirty 模式复用缓存（第二次执行全部 skipped）
- 画布助手对话：LLM 真实回复或离线降级均 200
"""

import pytest

from tests.conftest import create_project, unique_suffix


async def _make_template_canvas(client, auth, tag: str) -> dict:
    """辅助：创建项目与 script_to_storyboard 模板画布（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        tag: 标题防冲突后缀。

    Returns:
        dict: 画布完整图 JSON（含 canvas/nodes/edges）。

    Raises:
        AssertionError: 接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"画布项目_{tag}")
    resp = await client.post(
        "/api/canvases",
        headers=auth["headers"],
        json={
            "title": f"测试画布_{tag}",
            "project_id": project["id"],
            "template": "script_to_storyboard",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_muban_jian_huabu_yuzhi_jiedian(client, auth):
    """模板建画布：预置 3 节点 2 连线、类型 standalone。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    graph = await _make_template_canvas(client, auth, unique_suffix())
    assert graph["canvas"]["type"] == "standalone"
    assert len(graph["nodes"]) == 3
    assert len(graph["edges"]) == 2
    types = [n["node_type"] for n in graph["nodes"]]
    assert types == ["text", "generate", "video"]
    # 模板连线：text→generate、generate→video
    id_of = {n["node_type"]: n["id"] for n in graph["nodes"]}
    pairs = [(e["source_node_id"], e["target_node_id"]) for e in graph["edges"]]
    assert (id_of["text"], id_of["generate"]) in pairs
    assert (id_of["generate"], id_of["video"]) in pairs


async def test_graph_baocun_jiedian_data(client, auth):
    """图全量同步：修改 text 节点 data 后保存并回读持久化生效。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    graph = await _make_template_canvas(client, auth, unique_suffix())
    canvas_id = graph["canvas"]["id"]

    nodes_in = [
        {
            "id": n["id"],
            "node_type": n["node_type"],
            "position_x": n["position_x"],
            "position_y": n["position_y"],
            "data": {"text": "画一个夕阳下的天台", "label": "剧本段落"}
            if n["node_type"] == "text"
            else n["data"],
            "config": n["config"],
        }
        for n in graph["nodes"]
    ]
    edges_in = [
        {
            "id": e["id"],
            "source_node_id": e["source_node_id"],
            "target_node_id": e["target_node_id"],
        }
        for e in graph["edges"]
    ]
    resp = await client.put(
        f"/api/canvases/{canvas_id}/graph", headers=auth["headers"],
        json={"nodes": nodes_in, "edges": edges_in},
    )
    assert resp.status_code == 200, resp.text

    got = await client.get(f"/api/canvases/{canvas_id}", headers=auth["headers"])
    assert got.status_code == 200
    text_node = next(n for n in got.json()["nodes"] if n["node_type"] == "text")
    assert text_node["data"]["text"] == "画一个夕阳下的天台"
    assert text_node["is_dirty"] is True  # data 变化自动置脏（RD-45）


async def test_zihuan_yu_chenghuan_400_shujububian(client, auth):
    """自环与成环连线均 400；成环请求事务回滚、画布数据保持不变。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    graph = await _make_template_canvas(client, auth, unique_suffix())
    canvas_id = graph["canvas"]["id"]
    nodes = graph["nodes"]
    edges = graph["edges"]

    def _nodes_payload():
        """构造提交侧全量节点负载（内部闭包函数）。

        Returns:
            list[dict]: 节点负载列表。
        """
        return [
            {
                "id": n["id"],
                "node_type": n["node_type"],
                "position_x": n["position_x"],
                "position_y": n["position_y"],
                "data": n["data"],
                "config": n["config"],
            }
            for n in nodes
        ]

    # ---- 自环：source == target → 400 ----
    self_loop = await client.put(
        f"/api/canvases/{canvas_id}/graph",
        headers=auth["headers"],
        json={
            "nodes": _nodes_payload(),
            "edges": [{"source_node_id": nodes[0]["id"], "target_node_id": nodes[0]["id"]}],
        },
    )
    assert self_loop.status_code == 400
    assert "自环" in self_loop.json()["detail"]

    # ---- 成环：video→text 回边 → 400 且数据不变 ----
    edges_in = [
        {"id": e["id"], "source_node_id": e["source_node_id"], "target_node_id": e["target_node_id"]}
        for e in edges
    ]
    edges_in.append({"source_node_id": nodes[2]["id"], "target_node_id": nodes[0]["id"]})
    cycle = await client.put(
        f"/api/canvases/{canvas_id}/graph",
        headers=auth["headers"],
        json={"nodes": _nodes_payload(), "edges": edges_in},
    )
    assert cycle.status_code == 400
    assert "环" in cycle.json()["detail"]

    # 回滚校验：连线仍为原有 2 条，节点仍为 3 个
    got = await client.get(f"/api/canvases/{canvas_id}", headers=auth["headers"])
    body = got.json()
    assert len(body["nodes"]) == 3
    assert len(body["edges"]) == 2


async def test_zhixing_all_wancheng_yu_dirty_huancun(client, auth, db):
    """DAG 执行：all 模式 text 节点产出 output 且 generate 转任务；
    第二次 dirty 执行全部按缓存 skipped。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    graph = await _make_template_canvas(client, auth, unique_suffix())
    canvas_id = graph["canvas"]["id"]
    node_ids = [n["id"] for n in graph["nodes"]]

    # 先把 text 节点写入提示词（保存后节点置脏）
    text_node = next(n for n in graph["nodes"] if n["node_type"] == "text")
    save = await client.put(
        f"/api/canvases/{canvas_id}/graph",
        headers=auth["headers"],
        json={
            "nodes": [
                {
                    "id": n["id"],
                    "node_type": n["node_type"],
                    "position_x": n["position_x"],
                    "position_y": n["position_y"],
                    "data": {"text": "画一个夕阳下的天台", "label": "剧本段落"}
                    if n["node_type"] == "text"
                    else n["data"],
                    "config": n["config"],
                }
                for n in graph["nodes"]
            ],
            "edges": [
                {"id": e["id"], "source_node_id": e["source_node_id"], "target_node_id": e["target_node_id"]}
                for e in graph["edges"]
            ],
        },
    )
    assert save.status_code == 200

    # 第一次：all 模式全量执行
    first = await client.post(
        f"/api/canvases/{canvas_id}/execute",
        headers=auth["headers"],
        json={"mode": "all"},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert sorted(body["executed"]) == sorted(node_ids)
    assert body["skipped"] == []
    # generate 节点转生成任务（冻结 10 积分）
    assert len(body["tasks"]) == 1

    # text 节点产出 output = 输入文本
    got = await client.get(f"/api/canvases/{canvas_id}", headers=auth["headers"])
    text_node_after = next(
        n for n in got.json()["nodes"] if n["node_type"] == "text"
    )
    assert text_node_after["data"]["output"] == "画一个夕阳下的天台"
    assert text_node_after["status"] == "done"
    assert text_node_after["cache_ref"]

    # 第二次：dirty 模式——无脏节点，全部复用缓存 skipped
    second = await client.post(
        f"/api/canvases/{canvas_id}/execute",
        headers=auth["headers"],
        json={"mode": "dirty"},
    )
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["executed"] == []
    assert sorted(body2["skipped"]) == sorted(node_ids)


async def test_zhushou_duihua_ke_yong(client, auth):
    """画布助手对话：LLM 真实回复或离线降级均返回 200 与可用产出。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    graph = await _make_template_canvas(client, auth, unique_suffix())
    canvas_id = graph["canvas"]["id"]

    resp = await client.post(
        f"/api/canvases/{canvas_id}/assistant/chat",
        headers=auth["headers"],
        json={"message": "帮我把主角的立绘生成出来"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["reply"], str) and body["reply"]
    assert isinstance(body["applied"], int)
    assert len(body["nodes"]) >= 3  # 至少保留模板原有节点（降级会新增 generate 节点）
