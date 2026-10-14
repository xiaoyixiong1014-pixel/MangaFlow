"""资产标签与批量删除接口测试模块（文件级说明，A-05）。

覆盖链路：
- tags 编辑落库（PUT 更新）与 Out 透出（聚合 + 分类型列表）
- GET 列表 ?tag= 过滤：命中 / 不命中 / 不过滤
- 非法 tags：超过 10 个 / 单项超 20 字 → 422
- 批量删除：正常软删多条 / 与单删一致的越权与重复删除跳过语义 / 空 ids 400
- 项目列表 sort=recent（updated_at 倒序别名）与默认排序生效（直改 DB 模拟编辑时间）
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.models import Character, Project
from tests.conftest import create_project, register_user, unique_suffix


async def _make_character(client, auth, name: str, tags=None):
    """辅助：创建项目与角色资产（可选写入标签），返回 (project_id, character_id)。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        name: 角色名（调用方保证防冲突）。
        tags: 创建后经 PUT 更新写入的标签数组（None = 不写）。

    Returns:
        Tuple[int, int]: (项目 ID, 角色 ID)。

    Raises:
        AssertionError: 任一接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"标签项目_{unique_suffix()}")
    resp = await client.post(
        f"/api/projects/{project['id']}/assets/characters",
        headers=auth["headers"],
        json={"name": name, "description": "标签测试角色"},
    )
    assert resp.status_code == 200, resp.text
    character = resp.json()
    if tags is not None:
        resp2 = await client.put(
            f"/api/projects/{project['id']}/assets/characters/{character['id']}",
            headers=auth["headers"],
            json={"tags": tags},
        )
        assert resp2.status_code == 200, resp2.text
    return project["id"], character["id"]


async def test_bianqian_put_luoku_touping(client, auth):
    """tags PUT 落库并在聚合与分类型列表 Out 中透出；缺省为空数组。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, cid = await _make_character(
        client, auth, f"林小满_{unique_suffix()}", tags=["主角", "元气少女"]
    )
    # 聚合接口透出 tags
    group = await client.get(f"/api/projects/{pid}/assets", headers=auth["headers"])
    assert group.status_code == 200
    char = next(c for c in group.json()["characters"] if c["id"] == cid)
    assert char["tags"] == ["主角", "元气少女"]

    # 分类型列表透出 tags
    typed = await client.get(
        f"/api/projects/{pid}/assets", params={"type": "character"}, headers=auth["headers"]
    )
    assert typed.status_code == 200
    assert typed.json()["items"][0]["tags"] == ["主角", "元气少女"]

    # 未设置标签的资产 Out 透出空数组（tags 列为 NULL → 缺省 []，同项目内再建一个）
    untagged = await client.post(
        f"/api/projects/{pid}/assets/characters",
        headers=auth["headers"],
        json={"name": f"阿澄_{unique_suffix()}", "description": "未打标角色"},
    )
    assert untagged.status_code == 200, untagged.text
    cid2 = untagged.json()["id"]
    group2 = await client.get(f"/api/projects/{pid}/assets", headers=auth["headers"])
    char2 = next(c for c in group2.json()["characters"] if c["id"] == cid2)
    assert char2["tags"] == []

    # 清空标签：传空数组整体替换
    cleared = await client.put(
        f"/api/projects/{pid}/assets/characters/{cid}",
        headers=auth["headers"],
        json={"tags": []},
    )
    assert cleared.status_code == 200
    assert cleared.json()["tags"] == []


async def test_bianqian_tag_guolv(client, auth):
    """?tag= 过滤：命中包含该标签的资产，不命中返回空，不带 tag 返回全部。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    suffix = unique_suffix()
    pid = (await create_project(client, auth["headers"], title=f"过滤项目_{suffix}"))["id"]
    # 同项目内建两个角色：一个打标、一个不打标
    hit_resp = await client.post(
        f"/api/projects/{pid}/assets/characters",
        headers=auth["headers"],
        json={"name": f"命中角色_{suffix}", "description": "过滤测试"},
    )
    assert hit_resp.status_code == 200, hit_resp.text
    cid_hit = hit_resp.json()["id"]
    tag_resp = await client.put(
        f"/api/projects/{pid}/assets/characters/{cid_hit}",
        headers=auth["headers"],
        json={"tags": ["主角", "便装"]},
    )
    assert tag_resp.status_code == 200, tag_resp.text
    miss_resp = await client.post(
        f"/api/projects/{pid}/assets/characters",
        headers=auth["headers"],
        json={"name": f"未打标角色_{suffix}", "description": "过滤测试"},
    )
    assert miss_resp.status_code == 200, miss_resp.text
    cid_miss = miss_resp.json()["id"]

    # 命中：tag=主角 只返回打标角色
    hit = await client.get(
        f"/api/projects/{pid}/assets",
        params={"type": "character", "tag": "主角"},
        headers=auth["headers"],
    )
    assert hit.status_code == 200
    body = hit.json()
    assert body["total"] == 1
    assert [i["id"] for i in body["items"]] == [cid_hit]

    # 不命中：返回空列表
    miss = await client.get(
        f"/api/projects/{pid}/assets",
        params={"type": "character", "tag": "不存在的标签"},
        headers=auth["headers"],
    )
    assert miss.status_code == 200
    assert miss.json()["total"] == 0
    assert miss.json()["items"] == []

    # 不过滤：返回全部两条
    all_rows = await client.get(
        f"/api/projects/{pid}/assets", params={"type": "character"}, headers=auth["headers"]
    )
    assert all_rows.status_code == 200
    assert all_rows.json()["total"] == 2
    assert {i["id"] for i in all_rows.json()["items"]} == {cid_hit, cid_miss}


async def test_bianqian_feifa_tags_422(client, auth):
    """非法 tags 校验：超过 10 个 / 单项超 20 字 → 422。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 422 时中断。
    """
    pid, cid = await _make_character(client, auth, f"校验角色_{unique_suffix()}")

    # 超过 10 个
    too_many = await client.put(
        f"/api/projects/{pid}/assets/characters/{cid}",
        headers=auth["headers"],
        json={"tags": [f"标签{i}" for i in range(11)]},
    )
    assert too_many.status_code == 422

    # 单项超 20 字
    too_long = await client.put(
        f"/api/projects/{pid}/assets/characters/{cid}",
        headers=auth["headers"],
        json={"tags": ["x" * 21]},
    )
    assert too_long.status_code == 422


async def test_piliang_shanchu_changgui(client, auth, db):
    """批量删除：命中多条正常软删；响应含 deleted/skipped；列表查询不再返回。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    suffix = unique_suffix()
    pid = (await create_project(client, auth["headers"], title=f"批量项目_{suffix}"))["id"]
    ids = []
    for name in (f"甲_{suffix}", f"乙_{suffix}", f"丙_{suffix}"):
        resp = await client.post(
            f"/api/projects/{pid}/assets/characters",
            headers=auth["headers"],
            json={"name": name, "description": "批量删除测试"},
        )
        assert resp.status_code == 200, resp.text
        ids.append(resp.json()["id"])

    resp = await client.post(
        f"/api/projects/{pid}/assets/characters/batch-delete",
        headers=auth["headers"],
        json={"ids": ids[:2]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == 2
    assert body["skipped"] == []

    # 软删落库校验：deleted_at 已置值
    rows = (
        await db.execute(
            select(Character).where(Character.id.in_(ids)).order_by(Character.id)
        )
    ).scalars().all()
    deleted = {r.id: r for r in rows}
    assert deleted[ids[0]].deleted_at is not None
    assert deleted[ids[1]].deleted_at is not None
    assert deleted[ids[2]].deleted_at is None

    # 软删过滤器：列表不再返回被删角色
    listing = await client.get(
        f"/api/projects/{pid}/assets", params={"type": "character"}, headers=auth["headers"]
    )
    assert listing.status_code == 200
    assert [i["id"] for i in listing.json()["items"]] == [ids[2]]


async def test_piliang_shanchu_yu_shandan_yizhi(client, auth, db):
    """批量删除与单删行为一致：无引用保护软删、越权/不存在/重复删除按 skipped 跳过。

    单删对「不属于本人项目 / 已软删 / 不存在」统一 404 且无引用保护（仅置
    deleted_at）；批量接口把这些 404 场景折算为 skipped 不中断整体操作。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    suffix = unique_suffix()
    pid, cid_mine = await _make_character(client, auth, f"本人角色_{suffix}")

    # 越权构造：注册另一个用户，其项目与角色对当前用户不可见
    other = await register_user(client)
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    other_project = await create_project(client, other_headers, title=f"他人项目_{suffix}")
    other_resp = await client.post(
        f"/api/projects/{other_project['id']}/assets/characters",
        headers=other_headers,
        json={"name": f"他人角色_{suffix}", "description": "越权测试"},
    )
    assert other_resp.status_code == 200
    other_cid = other_resp.json()["id"]

    # 先软删一个自身角色，再对其重复删除（单删语义：已软删 → 404）
    await client.delete(
        f"/api/projects/{pid}/assets/characters/{cid_mine}", headers=auth["headers"]
    )
    ghost_id = 999999999  # 不存在的 ID

    resp = await client.post(
        f"/api/projects/{pid}/assets/characters/batch-delete",
        headers=auth["headers"],
        json={"ids": [other_cid, cid_mine, ghost_id]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == 0
    skipped_ids = {s["id"] for s in body["skipped"]}
    assert skipped_ids == {other_cid, cid_mine, ghost_id}
    assert all("不存在" in s["reason"] or "不属于" in s["reason"] or "已删除" in s["reason"]
               for s in body["skipped"])

    # 越权角色未被删除（deleted_at 仍为 None）
    other_row = (await db.execute(
        select(Character).where(Character.id == other_cid)
    )).scalar_one()
    assert other_row.deleted_at is None


async def test_piliang_shanchu_kong_ids_400(client, auth):
    """批量删除空 ids 与超量 ids 返回 400（路由层数量校验）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 400 时中断。
    """
    pid, _ = await _make_character(client, auth, f"空删角色_{unique_suffix()}")
    empty = await client.post(
        f"/api/projects/{pid}/assets/characters/batch-delete",
        headers=auth["headers"],
        json={"ids": []},
    )
    assert empty.status_code == 400

    too_many = await client.post(
        f"/api/projects/{pid}/assets/characters/batch-delete",
        headers=auth["headers"],
        json={"ids": list(range(1, 102))},
    )
    assert too_many.status_code == 400


async def test_xiangmu_paixu_recent_shengxiao(client, auth, db):
    """项目列表 sort=recent 与默认排序生效（updated_at 倒序，D-06）。

    创建两个项目后把先创建项目的 updated_at 直改 DB 为最新，断言：
    sort=recent 与缺省请求均把该项目排在最前。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    first = await create_project(client, auth["headers"], title=f"排序旧项目_{unique_suffix()}")
    second = await create_project(client, auth["headers"], title=f"排序新项目_{unique_suffix()}")

    # 直改 DB：把先创建项目的 updated_at 拨到未来（模拟「最近编辑」）
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    await db.execute(
        update(Project).where(Project.id == first["id"]).values(updated_at=future)
    )
    await db.commit()

    # sort=recent：先创建（updated_at 更新）的项目排最前
    recent = await client.get(
        "/api/projects", params={"sort": "recent", "order": "desc"}, headers=auth["headers"]
    )
    assert recent.status_code == 200, recent.text
    items = recent.json()["items"]
    assert items[0]["id"] == first["id"]
    assert items[1]["id"] == second["id"]

    # 缺省请求（不带 sort）：默认即 updated_at 倒序，行为与 recent 一致
    default = await client.get("/api/projects", headers=auth["headers"])
    assert default.status_code == 200
    assert default.json()["items"][0]["id"] == first["id"]

    # 升序校验：recent + asc 排序反转
    asc = await client.get(
        "/api/projects", params={"sort": "recent", "order": "asc"}, headers=auth["headers"]
    )
    assert asc.status_code == 200
    assert asc.json()["items"][0]["id"] == second["id"]


async def test_piliang_shanchu_wei_rumen_400(client, auth):
    """批量删除 asset_type 非法值返回 400（characters/scenes/props/styles 白名单）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 400 时中断。
    """
    pid, _ = await _make_character(client, auth, f"白名单角色_{uuid.uuid4().hex[:6]}")
    bad = await client.post(
        f"/api/projects/{pid}/assets/episodes/batch-delete",
        headers=auth["headers"],
        json={"ids": [1]},
    )
    assert bad.status_code == 400
