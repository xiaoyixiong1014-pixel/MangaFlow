"""项目管理接口测试模块（文件级说明）。

覆盖链路：
- 创建项目（初始 draft）
- 列表分页 / 搜索
- 更新非法状态 400
- 他人项目 404（不泄露存在性）
- 软删除后列表不含、详情 404
- duplicate 复制
- 快照创建与列表
"""

import uuid

from tests.conftest import create_project, register_user


async def test_chuangjian_xiangmu_chushi_zhuangtai(client, auth):
    """创建项目：初始状态为 draft，字段回显正确。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(
        client, auth["headers"], title="成长题材测试", genre="热血", style="日漫", target_duration=90
    )
    assert project["id"] > 0
    assert project["title"] == "成长题材测试"
    assert project["status"] == "draft"
    assert project["aspect_ratio"] == "9:16"
    assert project["target_duration"] == 90


async def test_liebiao_fenye_yu_sousuo(client, auth):
    """列表分页与标题搜索：分页条数正确、搜索仅命中匹配标题。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    token = uuid.uuid4().hex[:8]
    for i in range(3):
        await create_project(client, auth["headers"], title=f"分页项目_{token}_{i}")
    # 干扰数据：不同标题
    await create_project(client, auth["headers"], title="无关项目")

    # 分页：page_size=2
    resp = await client.get(
        "/api/projects",
        headers=auth["headers"],
        params={"page": 1, "page_size": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4
    assert len(body["items"]) == 2
    assert body["page"] == 1

    # 搜索唯一关键字
    resp2 = await client.get(
        "/api/projects", headers=auth["headers"], params={"search": token}
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["total"] == 3
    assert all(token in item["title"] for item in body2["items"])


async def test_gengxin_feifa_zhuangtai_400(client, auth):
    """更新项目状态传入非法值返回 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 400 时中断。
    """
    project = await create_project(client, auth["headers"])
    resp = await client.put(
        f"/api/projects/{project['id']}", headers=auth["headers"], json={"status": "bogus_status"}
    )
    assert resp.status_code == 400
    assert "status" in resp.json()["detail"]


async def test_taren_xiangmu_404(client, auth):
    """访问他人项目详情返回 404（统一不泄露存在性）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文（项目归属者）。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 404 时中断。
    """
    project = await create_project(client, auth["headers"])
    other = await register_user(client, prefix="other")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    resp = await client.get(f"/api/projects/{project['id']}", headers=other_headers)
    assert resp.status_code == 404
    # 他人更新同样 404
    resp2 = await client.put(
        f"/api/projects/{project['id']}", headers=other_headers, json={"title": "篡改"}
    )
    assert resp2.status_code == 404


async def test_ruan_shanchu_hou_liebiao_bu_han(client, auth):
    """软删除项目后列表不含该行、详情 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    keep = await create_project(client, auth["headers"], title="保留项目")
    gone = await create_project(client, auth["headers"], title="待删除项目")

    resp = await client.delete(f"/api/projects/{gone['id']}", headers=auth["headers"])
    assert resp.status_code == 200

    resp2 = await client.get("/api/projects", headers=auth["headers"])
    titles = [item["title"] for item in resp2.json()["items"]]
    assert "待删除项目" not in titles
    assert "保留项目" in titles

    # 已删项目详情 404
    resp3 = await client.get(f"/api/projects/{gone['id']}", headers=auth["headers"])
    assert resp3.status_code == 404


async def test_fuzhi_xiangmu(client, auth):
    """复制项目：标题追加 (副本)、状态重置 draft、生成新 ID。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(client, auth["headers"], title="源项目", genre="悬疑")
    # 先把源项目推进到 scripting，验证副本仍重置为 draft
    resp_status = await client.put(
        f"/api/projects/{project['id']}", headers=auth["headers"], json={"status": "scripting"}
    )
    assert resp_status.status_code == 200

    resp = await client.post(f"/api/projects/{project['id']}/duplicate", headers=auth["headers"])
    assert resp.status_code == 200, resp.text
    dup = resp.json()
    assert dup["id"] != project["id"]
    assert dup["title"] == "源项目 (副本)"
    assert dup["status"] == "draft"
    assert dup["genre"] == "悬疑"


async def test_kuaizhao_chuangjian_yu_liebiao(client, auth):
    """手动创建版本快照后可在快照列表中查到（含 stage 与摘要）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(client, auth["headers"])
    resp = await client.post(
        f"/api/projects/{project['id']}/snapshots",
        headers=auth["headers"],
        json={"stage": "manual_test", "summary": {"note": "测试快照"}},
    )
    assert resp.status_code == 200, resp.text
    snapshot = resp.json()
    assert snapshot["stage"] == "manual_test"

    resp2 = await client.get(
        f"/api/projects/{project['id']}/snapshots", headers=auth["headers"]
    )
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["total"] >= 1
    assert any(item["id"] == snapshot["id"] for item in body["items"])


async def test_xiangmu_xiangqing_jishu(client, auth):
    """项目详情返回 episode_count / script_count 概要计数。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(client, auth["headers"])
    resp = await client.get(f"/api/projects/{project['id']}", headers=auth["headers"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["episode_count"] == 0
    assert body["script_count"] == 0
