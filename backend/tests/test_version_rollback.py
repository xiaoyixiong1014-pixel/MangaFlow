"""版本快照回退测试模块（文件级说明，V-01 / Task 10.2）。

覆盖链路：
1. 主链路：两次抽取确认产生含实体状态的快照 → 回退到第一次快照 →
   断言资产恢复一致 + 自动创建「回退前快照」（pre_rollback）+
   operation_logs 留痕（action=version_rollback, risk_level=medium）；
2. 撤销链路：对回退前快照再次回退 → 恢复到回退前状态（回退本身可撤销）；
3. 越权：非归属用户回退他人项目 → 404（项目归属统一 404，防泄露存在性）；
4. 404：快照不存在 / 项目不存在；
5. 400：无实体状态的手动快照不可回退。
"""

import pytest
from sqlalchemy import select

from app.models import Character, OperationLog, VersionSnapshot
from tests.conftest import create_project

# 第一次抽取确认请求（快照 v1 状态）
_CONFIRM_V1 = {
    "characters": [{"name": "林小满", "aliases": ["小满"], "description": "第一版描述"}],
    "scenes": [{"name": "天台", "description": "学校天台"}],
    "props": [],
}
# 第二次抽取确认请求（快照 v2 状态：更新林小满 + 新增陆远/怀表）
_CONFIRM_V2 = {
    "characters": [
        {"name": "林小满", "aliases": [], "description": "第二版描述"},
        {"name": "陆远", "aliases": ["阿远"], "description": "男主角"},
    ],
    "scenes": [{"name": "天台", "description": "学校天台"}],
    "props": [{"name": "怀表", "description": "祖传怀表"}],
}


async def _confirm(client, headers, project_id: int, payload: dict):
    """调用抽取确认接口并断言成功（辅助函数）。

    Args:
        client: 内存 HTTP 客户端。
        headers: 认证请求头。
        project_id: 项目 ID。
        payload: ExtractConfirmRequest 请求体。

    Returns:
        dict: 确认结果（各类资产总数）。

    Raises:
        AssertionError: 接口返回非 200 时中断用例。
    """
    resp = await client.post(
        f"/api/projects/{project_id}/script/extract/confirm", headers=headers, json=payload
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _valid_assets(db, project_id: int):
    """查询项目当前有效资产（辅助函数，返回按类型分组的名字集合与角色详情）。

    说明：expire_all 清缓存后，过期对象直接访问属性会触发同步懒加载
    （MissingGreenlet），因此查回的每一行都显式 await refresh 刷新。

    Args:
        db: 测试库会话。
        project_id: 项目 ID。

    Returns:
        tuple: (character_names 集合, prop_names 集合, char_map {name: ORM})。
    """
    chars = (
        await db.execute(
            select(Character).where(
                Character.project_id == project_id, Character.deleted_at.is_(None)
            )
        )
    ).scalars().all()
    from app.models import Prop

    props = (
        await db.execute(
            select(Prop).where(Prop.project_id == project_id, Prop.deleted_at.is_(None))
        )
    ).scalars().all()
    for row in (*chars, *props):
        await db.refresh(row)
    return ({c.name for c in chars}, {p.name for p in props}, {c.name: c for c in chars})


async def _get_snapshots(db, project_id: int):
    """查询项目全部快照并按 stage 返回（辅助函数）。

    Args:
        db: 测试库会话。
        project_id: 项目 ID。

    Returns:
        list[VersionSnapshot]: 该项目全部快照行。
    """
    return (
        await db.execute(
            select(VersionSnapshot).where(VersionSnapshot.project_id == project_id)
        )
    ).scalars().all()


@pytest.mark.asyncio
async def test_rollback_restores_assets_and_audits(client, auth, db):
    """主链路：建快照 → 改数据 → 回退 → 数据恢复一致 + 回退前快照 + 审计留痕。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    headers = auth["headers"]
    project = await create_project(client, headers)
    pid = project["id"]

    # 两次抽取确认 → 两个含实体状态的快照（v1 / v2）
    await _confirm(client, headers, pid, _CONFIRM_V1)
    await _confirm(client, headers, pid, _CONFIRM_V2)

    # v2 当前状态：林小满（第二版描述）+ 陆远 + 怀表 + 天台
    char_names, prop_names, char_map = await _valid_assets(db, pid)
    assert char_names == {"林小满", "陆远"}
    assert prop_names == {"怀表"}
    assert char_map["林小满"].description == "第二版描述"

    # 找到第一次确认产生的快照（最早创建的 extract_confirmed）
    snaps = [s for s in await _get_snapshots(db, pid) if s.stage == "extract_confirmed"]
    assert len(snaps) == 2
    snaps.sort(key=lambda s: s.id)
    v1 = snaps[0]
    v1_id = v1.id  # expire_all 后旧 ORM 对象过期，提前留存 ID 供断言使用
    assert v1.impact_scope and v1.impact_scope.get("entity") == "assets"
    assert {c["name"] for c in v1.impact_scope["characters"]} == {"林小满"}

    # 回退到 v1
    resp = await client.post(f"/api/projects/{pid}/versions/{v1_id}/rollback", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restored"] is True
    assert isinstance(body["new_snapshot_id"], int)
    assert body["restored_at"]

    db.expire_all()  # 清身份映射缓存，读取回退后的最新提交值

    # 断言资产恢复为 v1 状态：林小满描述回滚、陆远/怀表不再有效、天台保留
    char_names, prop_names, char_map = await _valid_assets(db, pid)
    assert char_names == {"林小满"}
    assert prop_names == set()
    assert char_map["林小满"].description == "第一版描述"
    assert char_map["林小满"].aliases == ["小满"]

    # 断言「回退前快照」自动创建（stage=pre_rollback，记录 v2 状态，本身可回退）
    pre = (
        await db.execute(
            select(VersionSnapshot).where(VersionSnapshot.id == body["new_snapshot_id"])
        )
    ).scalar_one()
    await db.refresh(pre)  # expire_all 后该行已过期，显式刷新避免同步懒加载
    assert pre.stage == "pre_rollback"
    assert pre.impact_scope["entity"] == "assets"
    assert {c["name"] for c in pre.impact_scope["characters"]} == {"林小满", "陆远"}

    # 断言操作日志留痕（action=version_rollback，risk_level=medium）
    logs = (
        await db.execute(
            select(OperationLog).where(
                OperationLog.project_id == pid, OperationLog.action == "version_rollback"
            )
        )
    ).scalars().all()
    assert len(logs) == 1
    assert logs[0].risk_level == "medium"
    assert logs[0].detail["snapshot_id"] == v1_id


@pytest.mark.asyncio
async def test_rollback_is_revertible(client, auth, db):
    """撤销链路：对「回退前快照」再回退 → 回到回退前状态（回退可撤销）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    headers = auth["headers"]
    project = await create_project(client, headers)
    pid = project["id"]

    await _confirm(client, headers, pid, _CONFIRM_V1)
    await _confirm(client, headers, pid, _CONFIRM_V2)
    v1 = min((s for s in await _get_snapshots(db, pid) if s.stage == "extract_confirmed"),
             key=lambda s: s.id)

    # 回退到 v1，拿到回退前快照
    resp = await client.post(f"/api/projects/{pid}/versions/{v1.id}/rollback", headers=headers)
    assert resp.status_code == 200, resp.text
    pre_snapshot_id = resp.json()["new_snapshot_id"]

    # 对回退前快照再回退 → 撤销本次回退
    resp = await client.post(
        f"/api/projects/{pid}/versions/{pre_snapshot_id}/rollback", headers=headers
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    # 恢复到回退前（v2）状态：陆远复活且描述为 v2、林小满为第二版描述、怀表有效
    char_names, prop_names, char_map = await _valid_assets(db, pid)
    assert char_names == {"林小满", "陆远"}
    assert prop_names == {"怀表"}
    assert char_map["林小满"].description == "第二版描述"
    assert char_map["陆远"].aliases == ["阿远"]


@pytest.mark.asyncio
async def test_rollback_other_user_project_404(client, auth, db):
    """越权：非归属用户回退他人项目 → 404（统一 404 防泄露存在性）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文（项目归属者）。
        db: 测试库会话。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    headers = auth["headers"]
    project = await create_project(client, headers)
    pid = project["id"]
    await _confirm(client, headers, pid, _CONFIRM_V1)
    v1 = min((s for s in await _get_snapshots(db, pid) if s.stage == "extract_confirmed"),
             key=lambda s: s.id)

    # 另一个用户尝试回退他人项目
    other = await client.post(
        "/api/auth/register",
        json={"username": "intruder_x", "email": "intruder_x@mangaflow-test.com",
              "password": "Passw0rd!123"},
    )
    assert other.status_code == 200, other.text
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

    resp = await client.post(f"/api/projects/{pid}/versions/{v1.id}/rollback",
                             headers=other_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rollback_404_and_400(client, auth):
    """404/400：快照不存在 404；项目不存在 404；无实体状态快照 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    headers = auth["headers"]
    project = await create_project(client, headers)
    pid = project["id"]

    # 404：快照 ID 不存在
    resp = await client.post(f"/api/projects/{pid}/versions/999999/rollback", headers=headers)
    assert resp.status_code == 404
    # 404：项目不存在
    resp = await client.post("/api/projects/999999/versions/1/rollback", headers=headers)
    assert resp.status_code == 404

    # 400：手动快照（仅统计摘要，无 impact_scope 实体状态）不可回退
    resp = await client.post(
        f"/api/projects/{pid}/snapshots", headers=headers,
        json={"stage": "manual", "summary": {"note": "手动快照"}},
    )
    assert resp.status_code == 200, resp.text
    manual_id = resp.json()["id"]
    resp = await client.post(f"/api/projects/{pid}/versions/{manual_id}/rollback",
                             headers=headers)
    assert resp.status_code == 400
