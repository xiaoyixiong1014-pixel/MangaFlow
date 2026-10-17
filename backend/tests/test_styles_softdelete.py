"""styles 软删统一测试模块（文件级说明，P2）。

覆盖链路：
- batch-delete 对 styles 一律软删除（原物理删除特判移除）；
- 软删后的 style 不再出现在聚合列表与 type=style 分页中；
- 同名 style 可在软删后重建（ensure_unique_name 排除软删行）。
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Style
from tests.conftest import create_project, unique_suffix


async def _create_style(client, auth, pid: int, name: str, **overrides) -> dict:
    """辅助：创建风格资产并返回响应 JSON。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        pid: 项目 ID。
        name: 风格名。
        **overrides: 其余创建字段（visual_prompt 必填）。

    Returns:
        dict: 风格响应 JSON。

    Raises:
        AssertionError: 创建返回非 200 时中断。
    """
    payload = {"name": name, "visual_prompt": "统一日漫赛璐璐画风", **overrides}
    resp = await client.post(
        f"/api/projects/{pid}/assets/styles", headers=auth["headers"], json=payload
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_styles_pishan_ruanhua(client, auth, db: AsyncSession):
    """styles 批删软删化：deleted_at 落库、列表不再返回、可重建同名。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(client, auth["headers"], title=f"风格软删_{unique_suffix()}")
    pid = project["id"]
    style = await _create_style(client, auth, pid, "赛璐璐")

    # 批量删除（软删）
    resp = await client.post(
        f"/api/projects/{pid}/assets/styles/batch-delete",
        headers=auth["headers"], json={"ids": [style["id"]]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] == 1
    assert body["skipped"] == []

    # 数据库行为：软删（deleted_at 非空），行仍在
    row = await db.get(Style, style["id"])
    await db.refresh(row)
    assert row.deleted_at is not None

    # 聚合列表不再返回
    resp = await client.get(f"/api/projects/{pid}/assets", headers=auth["headers"])
    assert all(s["id"] != style["id"] for s in resp.json()["styles"])

    # type=style 分页同样排除
    resp = await client.get(
        f"/api/projects/{pid}/assets", headers=auth["headers"],
        params={"type": "style"},
    )
    assert resp.status_code == 200
    assert all(s["id"] != style["id"] for s in resp.json()["items"])

    # 软删后可重建同名（唯一性校验排除软删行）
    rebuilt = await _create_style(client, auth, pid, "赛璐璐")
    assert rebuilt["id"] != style["id"]


async def test_changjing_jvhe_liebiao_paichu_ruanshan(client, auth, db):
    """场景聚合列表排除软删行（回归：聚合分支曾漏 deleted_at 过滤）。

    场景单删后 GET /assets 聚合响应的 scenes 不得再包含该行
    （type=scene 分页分支此前已验证，本用例专测聚合分支）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(client, auth["headers"], title=f"场景软删_{unique_suffix()}")
    pid = project["id"]
    resp = await client.post(
        f"/api/projects/{pid}/assets/scenes",
        headers=auth["headers"],
        json={"name": "咖啡馆", "description": "街角咖啡馆"},
    )
    assert resp.status_code == 200, resp.text
    scene_id = resp.json()["id"]

    # 单删（软删）
    resp = await client.delete(
        f"/api/projects/{pid}/assets/scenes/{scene_id}", headers=auth["headers"]
    )
    assert resp.status_code == 200, resp.text

    # 聚合列表不再返回该场景
    resp = await client.get(f"/api/projects/{pid}/assets", headers=auth["headers"])
    assert resp.status_code == 200
    assert all(s["id"] != scene_id for s in resp.json()["scenes"])
