"""剧集管理接口测试模块（文件级说明，S-06）。

覆盖链路：
- 剧集 CRUD：创建（sequence 自动 max+1）/ 列表排序 / PATCH（title/status，
  非法状态 400）/ DELETE（硬删剧集行，scripts/storyboards 引用行 episode_id
  置 NULL 且数据保留）
- 越权：用户 B 操作用户 A 项目的剧集统一 404（不泄露存在性）
- reorder：乱序提交后 sequence 正确；跨项目剧集 ID / 未覆盖全部剧集拒绝
- 链路过滤：分镜列表与剧本查询按 episode_id 过滤；不传参数时兼容存量
  NULL 剧本行语义（默认剧本行可见）
"""

import uuid
from typing import Optional

from sqlalchemy import select
from tests.conftest import create_project, register_user


async def _create_episode(
    client, headers: dict, project_id: int, title: Optional[str] = None
) -> dict:
    """创建剧集的辅助封装（断言 200 后返回响应 JSON）。

    Args:
        client: 内存 HTTP 客户端。
        headers: 认证请求头。
        project_id: 项目 ID。
        title: 剧集标题（缺省随机生成）。

    Returns:
        dict: 剧集响应 JSON。

    Raises:
        AssertionError: 创建返回非 200 时中断。
    """
    payload = {"title": title or f"剧集_{uuid.uuid4().hex[:6]}"}
    resp = await client.post(
        f"/api/projects/{project_id}/episodes", headers=headers, json=payload
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_crud_quan_liucheng(client, auth, db):
    """CRUD 全流程：sequence 自动递增、列表按序、PATCH 生效、DELETE 解绑引用行。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（用于直接造分镜/剧本数据与断言行保留）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from app.models import Episode, Script, Storyboard

    project = await create_project(client, auth["headers"])

    # 创建：sequence 自动取项目内 max+1
    ep1 = await _create_episode(client, auth["headers"], project["id"], title="第一集")
    ep2 = await _create_episode(client, auth["headers"], project["id"], title="第二集")
    assert ep1["sequence"] == 1
    assert ep2["sequence"] == 2
    assert ep1["status"] == "draft"

    # 列表按 sequence 升序
    resp = await client.get(f"/api/projects/{project['id']}/episodes", headers=auth["headers"])
    assert resp.status_code == 200
    items = resp.json()
    assert [i["id"] for i in items] == [ep1["id"], ep2["id"]]

    # PATCH：改名 + 状态流转；非法状态 400
    resp = await client.patch(
        f"/api/projects/{project['id']}/episodes/{ep1['id']}",
        headers=auth["headers"],
        json={"title": "第一集（改名）", "status": "scripting"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "第一集（改名）"
    assert resp.json()["status"] == "scripting"
    resp_bad = await client.patch(
        f"/api/projects/{project['id']}/episodes/{ep1['id']}",
        headers=auth["headers"],
        json={"status": "bogus"},
    )
    assert resp_bad.status_code == 400

    # 项目详情内嵌剧集摘要（S-06 切换器数据源）
    detail = await client.get(f"/api/projects/{project['id']}", headers=auth["headers"])
    assert detail.status_code == 200
    assert [e["id"] for e in detail.json()["episodes"]] == [ep1["id"], ep2["id"]]

    # 造引用数据：默认剧本行 + 绑定 ep1 的剧本行 + 绑定 ep1 的分镜
    sb = Storyboard(
        project_id=project["id"], episode_id=ep1["id"], sequence=1, duration=5,
        visual_description="测试画面", status="pending",
    )
    script = Script(
        project_id=project["id"], episode_id=ep1["id"],
        content={}, raw_text="绑定剧集的剧本", version=1,
    )
    db.add_all([sb, script])
    await db.commit()
    sb_id, script_id = sb.id, script.id

    # DELETE：硬删剧集行，引用行 episode_id 置 NULL 且数据保留
    resp = await client.delete(
        f"/api/projects/{project['id']}/episodes/{ep1['id']}", headers=auth["headers"]
    )
    assert resp.status_code == 200, resp.text

    # 本 session 的 identity map 缓存了造数对象（API 在独立 session 中提交），
    # 过期全部对象以强制从数据库重载，确保断言的是落库终态而非缓存旧值
    db.expire_all()

    gone = await db.get(Episode, ep1["id"])
    assert gone is None
    kept_script = (
        await db.execute(select(Script).where(Script.id == script_id))
    ).scalar_one()
    assert kept_script.episode_id is None
    assert kept_script.raw_text == "绑定剧集的剧本"
    kept_sb = (
        await db.execute(select(Storyboard).where(Storyboard.id == sb_id))
    ).scalar_one()
    assert kept_sb.episode_id is None

    # 删除后再查列表只剩 ep2
    resp2 = await client.get(f"/api/projects/{project['id']}/episodes", headers=auth["headers"])
    assert [i["id"] for i in resp2.json()] == [ep2["id"]]


async def test_yuequan_404(client, auth):
    """越权：用户 B 对用户 A 项目的剧集增/改/删/查一律 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文（项目归属者）。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 404 时中断。
    """
    project = await create_project(client, auth["headers"])
    episode = await _create_episode(client, auth["headers"], project["id"])

    other = await register_user(client, prefix="other")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    resp_create = await client.post(
        f"/api/projects/{project['id']}/episodes", headers=other_headers, json={"title": "越权"}
    )
    assert resp_create.status_code == 404
    resp_list = await client.get(
        f"/api/projects/{project['id']}/episodes", headers=other_headers
    )
    assert resp_list.status_code == 404
    resp_patch = await client.patch(
        f"/api/projects/{project['id']}/episodes/{episode['id']}",
        headers=other_headers, json={"title": "篡改"},
    )
    assert resp_patch.status_code == 404
    resp_delete = await client.delete(
        f"/api/projects/{project['id']}/episodes/{episode['id']}", headers=other_headers
    )
    assert resp_delete.status_code == 404
    resp_reorder = await client.put(
        f"/api/projects/{project['id']}/episodes/reorder",
        headers=other_headers, json={"ordered_ids": [episode["id"]]},
    )
    assert resp_reorder.status_code == 404


async def test_reorder_luanxu_yu_kuaxiangmu(client, auth):
    """reorder：乱序提交后 sequence 按提交顺序重写；跨项目/未覆盖清单 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(client, auth["headers"])
    ep1 = await _create_episode(client, auth["headers"], project["id"])
    ep2 = await _create_episode(client, auth["headers"], project["id"])
    ep3 = await _create_episode(client, auth["headers"], project["id"])

    # 乱序提交：ep3 → 1，ep1 → 2，ep2 → 3
    resp = await client.put(
        f"/api/projects/{project['id']}/episodes/reorder",
        headers=auth["headers"],
        json={"ordered_ids": [ep3["id"], ep1["id"], ep2["id"]]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 3

    items = (
        await client.get(f"/api/projects/{project['id']}/episodes", headers=auth["headers"])
    ).json()
    seq_by_id = {i["id"]: i["sequence"] for i in items}
    assert seq_by_id[ep3["id"]] == 1
    assert seq_by_id[ep1["id"]] == 2
    assert seq_by_id[ep2["id"]] == 3

    # 跨项目剧集 ID（用户 A 的另一个项目）拒绝
    other_project = await create_project(client, auth["headers"])
    foreign_ep = await _create_episode(client, auth["headers"], other_project["id"])
    resp_cross = await client.put(
        f"/api/projects/{project['id']}/episodes/reorder",
        headers=auth["headers"],
        json={"ordered_ids": [ep1["id"], ep2["id"], ep3["id"], foreign_ep["id"]]},
    )
    assert resp_cross.status_code == 400

    # 未恰好覆盖全部剧集拒绝
    resp_partial = await client.put(
        f"/api/projects/{project['id']}/episodes/reorder",
        headers=auth["headers"],
        json={"ordered_ids": [ep1["id"]]},
    )
    assert resp_partial.status_code == 400


async def test_fenjing_juben_an_jidiguolv(client, auth, db):
    """分镜/剧本按 episode_id 过滤；不传参数时兼容存量 NULL 行语义。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直接造分镜数据）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from app.models import Storyboard

    project = await create_project(client, auth["headers"])
    ep1 = await _create_episode(client, auth["headers"], project["id"])

    # 直接造分镜：1 条绑定 ep1，1 条不绑剧集（普通创作流产物）
    sb_ep = Storyboard(
        project_id=project["id"], episode_id=ep1["id"], sequence=1, duration=5,
        visual_description="剧集分镜", status="pending",
    )
    sb_free = Storyboard(
        project_id=project["id"], episode_id=None, sequence=2, duration=5,
        visual_description="无剧集分镜", status="pending",
    )
    db.add_all([sb_ep, sb_free])
    await db.commit()

    # 按 episode_id 过滤：仅返回绑定行
    resp_ep = await client.get(
        f"/api/projects/{project['id']}/storyboards",
        headers=auth["headers"], params={"episode_id": ep1["id"]},
    )
    assert resp_ep.status_code == 200
    assert [b["id"] for b in resp_ep.json()] == [sb_ep.id]

    # 不传参数：返回全部（向后兼容）
    resp_all = await client.get(
        f"/api/projects/{project['id']}/storyboards", headers=auth["headers"]
    )
    assert len(resp_all.json()) == 2

    # 剧本：默认行（episode_id NULL）与剧集行互相隔离
    resp_input = await client.put(
        f"/api/projects/{project['id']}/script/input",
        headers=auth["headers"], json={"mode": "novel", "raw_text": "默认剧本输入"},
    )
    assert resp_input.status_code == 200
    assert resp_input.json()["content"]["input_mode"] == "novel"

    resp_input_ep = await client.put(
        f"/api/projects/{project['id']}/script/input",
        headers=auth["headers"],
        json={"mode": "outline", "raw_text": "剧集一输入", "episode_id": ep1["id"]},
    )
    assert resp_input_ep.status_code == 200, resp_input_ep.text

    # 按剧集查询 → 剧集行；不传 → 默认行（存量 NULL 行仍可见）
    resp_script_ep = await client.get(
        f"/api/projects/{project['id']}/script",
        headers=auth["headers"], params={"episode_id": ep1["id"]},
    )
    assert resp_script_ep.status_code == 200
    assert resp_script_ep.json()["raw_text"] == "剧集一输入"

    resp_script_default = await client.get(
        f"/api/projects/{project['id']}/script", headers=auth["headers"]
    )
    assert resp_script_default.status_code == 200
    assert resp_script_default.json()["raw_text"] == "默认剧本输入"
