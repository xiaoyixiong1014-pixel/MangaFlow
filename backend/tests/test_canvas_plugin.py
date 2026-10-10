"""画布插件 / MCP 桥 / 子图克隆测试模块（文件级说明，Task 9）。

覆盖：
- 插件全流程：安装（reviewed=pending）→ 运营方审批（留痕）→ enable →
  手动 invoke 内置 text-enhance 并断言规则增强输出（CV-20/21/22）；
- 未审批/未启用插件 invoke 被拒（400）；
- 内置插件直调端点 POST /api/plugins/builtin/text-enhance；
- 画布 plugin 节点执行分派：execute 后 status=done、data.output 为增强文本；
- MCP server KV 配置：operator 增删、普通用户 403、删除不存在 404（CV-19）；
- 子图克隆：节点/边收集、坐标 +40、跨画布与自身克隆、非法节点 400。

全部走内存 HTTP 客户端（conftest 夹具），不触主库、不发起真实外联。
"""

import uuid
from typing import Any, Dict

import httpx
import pytest

from tests.conftest import unique_suffix


async def _install_plugin(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    endpoint_url: str = "builtin:text-enhance",
    name: str = "测试增强",
) -> Dict[str, Any]:
    """安装一个测试插件并返回响应 JSON（用例内部辅助函数）。

    Args:
        client: 内存 HTTP 客户端。
        headers: 安装者认证头。
        endpoint_url: 插件地址（默认内置 text-enhance）。
        name: 插件名。

    Returns:
        Dict[str, Any]: 创建后的插件 JSON。
    """
    resp = await client.post("/api/canvas-plugins", headers=headers, json={
        "name": name,
        "version": "1.0",
        "description": "测试用插件",
        "endpoint_url": endpoint_url,
        "inputs": [{"name": "text", "type": "text"}],
        "outputs": [{"name": "text", "type": "text"}],
        "permissions": ["text:enhance"],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_plugin_review_enable_invoke_flow(client: httpx.AsyncClient, auth: dict, admin_auth: dict):
    """插件全流程：安装 → 审批 → 启用 → 列表可见 → invoke 输出断言。"""
    # 安装：默认 pending（reviewed=False）、停用（enabled=False）
    plugin = await _install_plugin(client, auth["headers"], name=f"增强_{unique_suffix()}")
    assert plugin["reviewed"] is False
    assert plugin["enabled"] is False
    assert plugin["endpoint_url"] == "builtin:text-enhance"
    assert plugin["inputs"][0]["name"] == "text"

    pid = plugin["id"]
    # 普通用户列表不可见（未审核未启用）
    resp = await client.get("/api/canvas-plugins", headers=auth["headers"])
    assert resp.status_code == 200
    assert all(p["id"] != pid for p in resp.json())

    # 运营方审批通过 + 启用
    resp = await client.post(f"/api/canvas-plugins/{pid}/review",
                             headers=admin_auth["headers"], json={"approved": True, "comment": "ok"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["reviewed"] is True
    resp = await client.post(f"/api/canvas-plugins/{pid}/enable", headers=admin_auth["headers"])
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True

    # 普通用户列表可见；operator all=true 可见未审核插件
    resp = await client.get("/api/canvas-plugins", headers=auth["headers"])
    assert any(p["id"] == pid for p in resp.json())
    # 另装一个未审核插件（endpoint_url 按用户+地址唯一，需用不同地址）
    resp = await client.post("/api/canvas-plugins", headers=auth["headers"], json={
        "name": "未审插件", "endpoint_url": f"builtin:unreviewed-{unique_suffix()}"})
    assert resp.status_code == 201
    resp = await client.get("/api/canvas-plugins?all=true", headers=admin_auth["headers"])
    assert resp.status_code == 200
    assert any(p["name"] == "未审插件" for p in resp.json())

    # 手动 invoke：内置 text-enhance 规则增强断言
    resp = await client.post(f"/api/canvas-plugins/{pid}/invoke",
                             headers=auth["headers"], json={"payload": {"text": "文本  测试！！！"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"] == "文本  测试！"


@pytest.mark.asyncio
async def test_invoke_unapproved_or_disabled_rejected(client: httpx.AsyncClient, auth: dict, admin_auth: dict):
    """未审批 invoke 被拒（400）；审批后未启用 invoke 仍被拒（400）。"""
    plugin = await _install_plugin(client, auth["headers"], name=f"未审_{unique_suffix()}")
    pid = plugin["id"]
    # 未审批：400
    resp = await client.post(f"/api/canvas-plugins/{pid}/invoke",
                             headers=auth["headers"], json={"payload": {"text": "hi"}})
    assert resp.status_code == 400, resp.text
    # 审批通过但未启用：仍 400
    resp = await client.post(f"/api/canvas-plugins/{pid}/review",
                             headers=admin_auth["headers"], json={"approved": True})
    assert resp.status_code == 200
    resp = await client.post(f"/api/canvas-plugins/{pid}/invoke",
                             headers=auth["headers"], json={"payload": {"text": "hi"}})
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_builtin_text_enhance_endpoint(client: httpx.AsyncClient, auth: dict):
    """内置插件直调端点：重复标点折叠 + 叠词折叠 + 补句号。"""
    resp = await client.post("/api/plugins/builtin/text-enhance",
                             headers=auth["headers"], json={"text": "好好好 学习！！"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == "好 学习！"
    # 未登录 401
    resp = await client.post("/api/plugins/builtin/text-enhance", json={"text": "x"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_canvas_plugin_node_execution(client: httpx.AsyncClient, auth: dict, admin_auth: dict, db):
    """画布 plugin 节点执行分派：执行后 status=done 且 data.output 为增强文本。"""
    # 准备一个已审批已启用的内置插件
    plugin = await _install_plugin(client, auth["headers"], name=f"画布执行_{unique_suffix()}")
    await client.post(f"/api/canvas-plugins/{plugin['id']}/review",
                      headers=admin_auth["headers"], json={"approved": True})
    await client.post(f"/api/canvas-plugins/{plugin['id']}/enable", headers=admin_auth["headers"])

    # 建画布并同步一个 plugin 节点（config.plugin_id 绑定）
    resp = await client.post("/api/canvases", headers=auth["headers"], json={"title": "插件执行画布"})
    assert resp.status_code == 201, resp.text
    canvas_id = resp.json()["canvas"]["id"]
    resp = await client.put(f"/api/canvases/{canvas_id}/graph", headers=auth["headers"], json={
        "nodes": [{
            "node_type": "plugin",
            "position_x": 100, "position_y": 80,
            "data": {"label": "文本增强", "text": "文本  测试！！！"},
            "config": {"plugin_id": plugin["id"], "plugin_name": plugin["name"]},
        }],
        "edges": [],
    })
    assert resp.status_code == 200, resp.text
    node_id = resp.json()["nodes"][0]["id"]

    # 单节点执行：走 plugin 分派
    resp = await client.post(f"/api/canvases/{canvas_id}/nodes/{node_id}/execute", headers=auth["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "done"
    assert body["data"]["output"] == "文本  测试！"


@pytest.mark.asyncio
async def test_mcp_server_kv_crud(client: httpx.AsyncClient, auth: dict, admin_auth: dict):
    """MCP server 配置 KV：operator 增删；普通用户写 403；删除不存在 404。"""
    # 普通用户添加 → 403
    resp = await client.post("/api/mcp/servers", headers=auth["headers"],
                             json={"name": "demo", "url": "http://127.0.0.1:9/mcp"})
    assert resp.status_code == 403
    # operator 添加
    resp = await client.post("/api/mcp/servers", headers=admin_auth["headers"],
                             json={"name": "demo-mcp", "url": "http://127.0.0.1:9/mcp",
                                   "headers": {"Authorization": "Bearer x"}})
    assert resp.status_code == 201, resp.text
    servers = resp.json()["servers"]
    assert len(servers) == 1 and servers[0]["name"] == "demo-mcp"
    sid = servers[0]["id"]
    # 登录用户可读
    resp = await client.get("/api/mcp/servers", headers=auth["headers"])
    assert resp.status_code == 200 and len(resp.json()["servers"]) == 1
    # 删除不存在 → 404
    resp = await client.delete("/api/mcp/servers/ghost", headers=admin_auth["headers"])
    assert resp.status_code == 404
    # operator 删除
    resp = await client.delete(f"/api/mcp/servers/{sid}", headers=admin_auth["headers"])
    assert resp.status_code == 200
    resp = await client.get("/api/mcp/servers", headers=auth["headers"])
    assert resp.json()["servers"] == []


async def _create_canvas_with_chain(client: httpx.AsyncClient, headers: Dict[str, str], title: str) -> int:
    """创建带 3 节点 2 边链式子图的画布并返回画布 ID（用例内部辅助函数）。

    Args:
        client: 内存 HTTP 客户端。
        headers: 认证头。
        title: 画布标题。

    Returns:
        int: 画布 ID。
    """
    resp = await client.post("/api/canvases", headers=headers, json={"title": title})
    assert resp.status_code == 201, resp.text
    canvas_id = resp.json()["canvas"]["id"]
    resp = await client.put(f"/api/canvases/{canvas_id}/graph", headers=headers, json={
        "nodes": [
            {"id": 1, "node_type": "text", "position_x": 0, "position_y": 0, "data": {"text": "A"}},
            {"id": 2, "node_type": "logic", "position_x": 200, "position_y": 0, "data": {"action": "noop"}},
            {"id": 3, "node_type": "image", "position_x": 400, "position_y": 0, "data": {"url": ""}},
        ],
        "edges": [
            {"source_node_id": 1, "target_node_id": 2},
            {"source_node_id": 2, "target_node_id": 3},
        ],
    })
    assert resp.status_code == 200, resp.text
    return canvas_id


@pytest.mark.asyncio
async def test_clone_subtree_to_other_canvas(client: httpx.AsyncClient, auth: dict):
    """克隆子图到另一画布：节点/边数量正确、坐标 +40。"""
    source_id = await _create_canvas_with_chain(client, auth["headers"], f"源画布_{unique_suffix()}")
    target_id = await _create_canvas_with_chain(client, auth["headers"], f"目标画布_{unique_suffix()}")

    # 取源图真实节点 id（链路 1→2→3，克隆前两个节点与内部边）
    graph = (await client.get(f"/api/canvases/{source_id}", headers=auth["headers"])).json()
    ids = [n["id"] for n in graph["nodes"]]
    resp = await client.post(f"/api/canvases/{source_id}/clone-subtree", headers=auth["headers"], json={
        "node_ids": ids[:2], "target_canvas_id": target_id,
    })
    assert resp.status_code == 201, resp.text
    result = resp.json()
    assert len(result["nodes"]) == 2
    assert len(result["edges"]) == 1
    # 坐标 +40 偏移
    src_by_id = {n["id"]: n for n in graph["nodes"]}
    src_first = src_by_id[ids[0]]
    assert result["nodes"][0]["position_x"] == src_first["position_x"] + 40
    assert result["nodes"][0]["position_y"] == src_first["position_y"] + 40
    # 目标画布图核对
    target_graph = (await client.get(f"/api/canvases/{target_id}", headers=auth["headers"])).json()
    assert len(target_graph["nodes"]) == 5  # 原 3 + 克隆 2
    assert len(target_graph["edges"]) == 3  # 原 2 + 克隆 1


@pytest.mark.asyncio
async def test_clone_subtree_to_self_and_invalid_nodes(client: httpx.AsyncClient, auth: dict):
    """克隆到自身：节点数翻倍；携带不存在节点 → 400；目标画布非本人 → 404。"""
    source_id = await _create_canvas_with_chain(client, auth["headers"], f"自克隆_{unique_suffix()}")
    graph = (await client.get(f"/api/canvases/{source_id}", headers=auth["headers"])).json()
    ids = [n["id"] for n in graph["nodes"]]

    # 克隆到自身（缺省 target）→ 3+3
    resp = await client.post(f"/api/canvases/{source_id}/clone-subtree", headers=auth["headers"],
                             json={"node_ids": ids})
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["nodes"]) == 3
    after = (await client.get(f"/api/canvases/{source_id}", headers=auth["headers"])).json()
    assert len(after["nodes"]) == 6

    # 非法节点 → 400
    resp = await client.post(f"/api/canvases/{source_id}/clone-subtree", headers=auth["headers"],
                             json={"node_ids": [ids[0], 99999999]})
    assert resp.status_code == 400

    # 目标画布非本人 → 404（他人画布）
    other = await client.post("/api/auth/register", json={
        "username": f"other_{uuid.uuid4().hex[:8]}", "email": f"other_{uuid.uuid4().hex[:8]}@t.com",
        "password": "Passw0rd!123",
    })
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    resp = await client.post(f"/api/canvases/{source_id}/clone-subtree", headers=other_headers,
                             json={"node_ids": ids})
    assert resp.status_code == 404
