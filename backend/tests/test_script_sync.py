"""剧本一键同步接口测试模块（文件级说明，S-08）。

覆盖链路：
- send-to-agent：新建会话（首条剧本摘要消息落库 state.messages）+ 幂等
  （同剧本版本二次调用返回既有 thread_id 与 duplicated=true）
- send-to-canvas：建「剧本画布」+ text 节点（meta.script_sync 落库）+
  幂等（二次调用返回既有 node_id）
- 越权访问（他人项目）一律 404
- 剧本内容为空时 400
"""

import pytest
from sqlalchemy import select

from app.models import AgentThread, Canvas, CanvasNode, Script
from tests.conftest import create_project, register_user, unique_suffix


async def _make_project_with_script(client, auth, tag: str):
    """辅助：创建项目并保存剧本原始输入，返回 (project_id, raw_text)（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        tag: 标题防冲突后缀。

    Returns:
        Tuple[int, str]: (项目 ID, 剧本原始输入文本)。

    Raises:
        AssertionError: 任一接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"同步项目_{tag}")
    raw_text = (
        "深夜便利店，失眠的都市女孩林小满与值夜班店员陈默相遇。"
        "场景一《便利店》：两人因一盒关东煮展开对话。"
    )
    resp = await client.put(
        f"/api/projects/{project['id']}/script/input",
        headers=auth["headers"],
        json={"mode": "novel", "raw_text": raw_text},
    )
    assert resp.status_code == 200, resp.text
    return project["id"], raw_text


async def _insert_empty_script(db, project_id: int) -> None:
    """辅助：ORM 直插一个「无原文且无结构化内容」的空剧本行（内部函数）。

    Args:
        db: 测试库会话。
        project_id: 归属项目 ID。

    Returns:
        None。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    db.add(Script(project_id=project_id, content={}, raw_text=None, version=1))
    await db.commit()


async def test_songdai_agent_xinjian_yu_mideng(client, auth, db):
    """send-to-agent：首次调用新建会话并写入剧本摘要消息；同版本二次调用
    幂等返回既有 thread_id 与 duplicated=true（不新建会话行）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直查 agent_threads 断言落库结构）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, raw_text = await _make_project_with_script(client, auth, unique_suffix())

    # 首次调用：新建会话
    first = await client.post(
        f"/api/projects/{pid}/script/send-to-agent", headers=auth["headers"], json={}
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["duplicated"] is False
    assert body["thread_id"]

    # 落库校验：会话行存在，state 携带 script_sync 标记与首条 user 摘要消息
    row = (
        await db.execute(select(AgentThread).where(AgentThread.thread_id == body["thread_id"]))
    ).scalar_one_or_none()
    assert row is not None
    assert row.project_id == pid
    assert row.state["script_sync"]["project_id"] == pid
    assert row.state["script_sync"]["script_version"] == 1
    messages = row.state["messages"]
    assert messages and messages[0]["role"] == "user"
    assert "剧本同步" in messages[0]["content"]
    assert raw_text[:50] in messages[0]["content"]  # 摘要携带原文节选

    # 二次调用（同剧本版本）：幂等命中既有会话
    second = await client.post(
        f"/api/projects/{pid}/script/send-to-agent", headers=auth["headers"], json={}
    )
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["duplicated"] is True
    assert body2["thread_id"] == body["thread_id"]

    # 不产生第二个会话行
    cnt = len((await db.execute(select(AgentThread).where(AgentThread.project_id == pid))).scalars().all())
    assert cnt == 1


async def test_songdai_agent_yuequan_404(client, auth):
    """send-to-agent 越权：他人项目统一 404（不泄露存在性）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    pid, _ = await _make_project_with_script(client, auth, unique_suffix())

    other = await register_user(client, prefix="intruder")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    resp = await client.post(
        f"/api/projects/{pid}/script/send-to-agent", headers=other_headers, json={}
    )
    assert resp.status_code == 404


async def test_songdai_agent_kong_juben_400(client, auth, db):
    """send-to-agent：剧本无原始输入且无结构化内容时返回 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直插空剧本行）。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    project = await create_project(client, auth["headers"], title=f"空剧本_{unique_suffix()}")
    await _insert_empty_script(db, project["id"])

    resp = await client.post(
        f"/api/projects/{project['id']}/script/send-to-agent", headers=auth["headers"], json={}
    )
    assert resp.status_code == 400
    assert "剧本内容为空" in resp.json()["detail"]

    # 剧本行不存在时保持既有 404 语义
    project2 = await create_project(client, auth["headers"], title=f"无剧本_{unique_suffix()}")
    resp2 = await client.post(
        f"/api/projects/{project2['id']}/script/send-to-agent", headers=auth["headers"], json={}
    )
    assert resp2.status_code == 404


async def test_toudi_huabu_jiedian_yu_mideng(client, auth, db):
    """send-to-canvas：首次投递建「剧本画布」+ text 节点（meta.script_sync 落库）；
    同剧本版本二次调用幂等返回既有 node_id 与 duplicated=true。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直查 canvas_nodes 断言落库结构）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, _ = await _make_project_with_script(client, auth, unique_suffix())

    # 首次投递：自动创建「剧本画布」并写入 text 节点
    first = await client.post(
        f"/api/projects/{pid}/script/send-to-canvas", headers=auth["headers"], json={}
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["duplicated"] is False
    assert body["canvas_id"] and body["node_id"]

    canvas = await db.get(Canvas, body["canvas_id"])
    assert canvas is not None
    assert canvas.title == "剧本画布"
    assert canvas.project_id == pid
    node = await db.get(CanvasNode, body["node_id"])
    assert node is not None
    assert node.node_type == "text"
    assert node.meta["script_sync"]["project_id"] == pid
    assert node.meta["script_sync"]["script_version"] == 1
    assert "剧本同步" in node.data["text"]
    assert node.data["label"]

    # 二次投递（同剧本版本）：幂等返回既有节点，不新建
    second = await client.post(
        f"/api/projects/{pid}/script/send-to-canvas", headers=auth["headers"], json={}
    )
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["duplicated"] is True
    assert body2["node_id"] == body["node_id"]
    assert body2["canvas_id"] == body["canvas_id"]

    # 节点数保持 1
    cnt = len(
        (
            await db.execute(
                select(CanvasNode).where(CanvasNode.canvas_id == body["canvas_id"])
            )
        ).scalars().all()
    )
    assert cnt == 1


async def test_toudi_huabu_yuequan_yu_kong_juben(client, auth, db):
    """send-to-canvas：越权 404；指定不存在/他人画布 404；空剧本 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直插空剧本行）。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    pid, _ = await _make_project_with_script(client, auth, unique_suffix())

    # 越权：他人对别人项目投递 → 404
    other = await register_user(client, prefix="intruder")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    resp = await client.post(
        f"/api/projects/{pid}/script/send-to-canvas", headers=other_headers, json={}
    )
    assert resp.status_code == 404

    # 指定不存在的画布 ID → 404
    resp2 = await client.post(
        f"/api/projects/{pid}/script/send-to-canvas",
        headers=auth["headers"],
        json={"canvas_id": 999999},
    )
    assert resp2.status_code == 404

    # 空剧本 → 400
    project_empty = await create_project(
        client, auth["headers"], title=f"空剧本画布_{unique_suffix()}"
    )
    await _insert_empty_script(db, project_empty["id"])
    resp3 = await client.post(
        f"/api/projects/{project_empty['id']}/script/send-to-canvas",
        headers=auth["headers"],
        json={},
    )
    assert resp3.status_code == 400
    assert "剧本内容为空" in resp3.json()["detail"]


@pytest.mark.parametrize("endpoint", ["send-to-agent", "send-to-canvas"])
async def test_tongbu_jiekou_weidenglu_401(client, endpoint):
    """同步接口未登录访问一律 401（Bearer 鉴权，get_current_user 抛出）。

    Args:
        client: 内存 HTTP 客户端。
        endpoint: 同步端点路径段。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    resp = await client.post(f"/api/projects/1/script/{endpoint}", json={})
    assert resp.status_code == 401
