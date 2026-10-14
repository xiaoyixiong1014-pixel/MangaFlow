"""参考图锚点接口测试模块（文件级说明，A-04）。

覆盖链路：
- PATCH reference_image_url：角色/场景更新接口回写参考图并透出（Out schema）
- 生成定妆照：任务 meta 携带 reference_image_url（mock 渠道下创建响应与
  终态任务 meta 均断言；provider meta 留痕不影响 placeholder 打标）
- 越权访问（他人资产）一律 404
"""

import pytest

from tests.conftest import (
    create_project,
    register_user,
    unique_suffix,
    wait_task_terminal,
)


async def _make_project_with_character(client, auth, tag: str):
    """辅助：创建项目与角色资产，返回 (project_id, character_id)（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        tag: 标题防冲突后缀。

    Returns:
        Tuple[int, int]: (项目 ID, 角色 ID)。

    Raises:
        AssertionError: 接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"参考图项目_{tag}")
    resp = await client.post(
        f"/api/projects/{project['id']}/assets/characters",
        headers=auth["headers"],
        json={"name": "林小满", "description": "元气少女漫画家"},
    )
    assert resp.status_code == 200, resp.text
    return project["id"], resp.json()["id"]


async def _make_project_with_scene(client, auth, tag: str):
    """辅助：创建项目与场景资产，返回 (project_id, scene_id)（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        tag: 标题防冲突后缀。

    Returns:
        Tuple[int, int]: (项目 ID, 场景 ID)。

    Raises:
        AssertionError: 接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"场景参考图_{tag}")
    resp = await client.post(
        f"/api/projects/{project['id']}/assets/scenes",
        headers=auth["headers"],
        json={"name": "深夜便利店", "description": "暖黄灯光下的便利店"},
    )
    assert resp.status_code == 200, resp.text
    return project["id"], resp.json()["id"]


async def test_patch_canKaoTu_jiaose_yu_changjing(client, auth):
    """PATCH reference_image_url 落库：角色与场景更新接口均可回写并透出。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    ref_url = "/storage/uploads/character_ref_test.png"

    # 角色：PATCH 后响应与列表均透出该字段
    pid, cid = await _make_project_with_character(client, auth, unique_suffix())
    resp = await client.put(
        f"/api/projects/{pid}/assets/characters/{cid}",
        headers=auth["headers"],
        json={"reference_image_url": ref_url},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reference_image_url"] == ref_url

    listed = await client.get(f"/api/projects/{pid}/assets", headers=auth["headers"])
    assert listed.status_code == 200
    char_row = next(c for c in listed.json()["characters"] if c["id"] == cid)
    assert char_row["reference_image_url"] == ref_url

    # 场景：PATCH 后响应透出该字段
    spid, sid = await _make_project_with_scene(client, auth, unique_suffix())
    resp2 = await client.put(
        f"/api/projects/{spid}/assets/scenes/{sid}",
        headers=auth["headers"],
        json={"reference_image_url": ref_url},
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["reference_image_url"] == ref_url

    # 覆盖替换：再次 PATCH 更新为新值
    resp3 = await client.put(
        f"/api/projects/{pid}/assets/characters/{cid}",
        headers=auth["headers"],
        json={"reference_image_url": "/storage/uploads/character_ref_new.png"},
    )
    assert resp3.status_code == 200
    assert resp3.json()["reference_image_url"] == "/storage/uploads/character_ref_new.png"


async def test_dingzhuangzhao_meta_xieDai_canKaoTu(client, auth, db):
    """生成定妆照：角色配置参考图后，任务 meta 携带 reference_image_url
    （TaskOut 不透出整体 meta，经数据库直查断言；mock 渠道下 provider meta
    平铺留痕，placeholder 打标不受影响）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直查 tasks.meta）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from app.models import Task

    ref_url = "/storage/uploads/character_ref_gen.png"
    pid, cid = await _make_project_with_character(client, auth, unique_suffix())

    patch = await client.put(
        f"/api/projects/{pid}/assets/characters/{cid}",
        headers=auth["headers"],
        json={"reference_image_url": ref_url},
    )
    assert patch.status_code == 200

    gen = await client.post(
        f"/api/projects/{pid}/assets/characters/{cid}/generate", headers=auth["headers"]
    )
    assert gen.status_code == 200, gen.text
    task = gen.json()
    assert task["type"] == "image"
    # 创建后直查 meta：参考图已随任务落库（asset_service 写入 tasks.meta）
    created = await db.get(Task, task["id"])
    assert (created.meta or {}).get("reference_image_url") == ref_url
    assert (created.meta or {}).get("target_type") == "character"

    # 轮询至终态：执行器平铺 provider meta 后参考图键保留；mock 占位打标不变
    final = await wait_task_terminal(client, auth["headers"], task["id"], timeout=30.0)
    assert final["status"] == "done", final
    db.expire_all()
    done_task = await db.get(Task, task["id"])
    assert (done_task.meta or {}).get("reference_image_url") == ref_url
    assert final["placeholder"] is True


async def test_canKaoTu_yueQuan_404(client, auth):
    """越权：他人 PATCH 别人角色/场景的参考图一律 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    pid, cid = await _make_project_with_character(client, auth, unique_suffix())

    other = await register_user(client, prefix="intruder")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    resp = await client.put(
        f"/api/projects/{pid}/assets/characters/{cid}",
        headers=other_headers,
        json={"reference_image_url": "/storage/uploads/evil.png"},
    )
    assert resp.status_code == 404

    # 未登录同样 401
    resp2 = await client.put(
        f"/api/projects/{pid}/assets/characters/{cid}",
        json={"reference_image_url": "/storage/uploads/evil.png"},
    )
    assert resp2.status_code == 401


@pytest.mark.parametrize("asset_kind", ["characters", "scenes"])
async def test_canKaoTu_chaoChang_422(client, auth, asset_kind):
    """超长参考图 URL（>500 字符）被 schema 校验拒绝返回 422。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        asset_kind: 资产路径段（characters / scenes）。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    if asset_kind == "characters":
        pid, aid = await _make_project_with_character(client, auth, unique_suffix())
    else:
        pid, aid = await _make_project_with_scene(client, auth, unique_suffix())

    resp = await client.put(
        f"/api/projects/{pid}/assets/{asset_kind}/{aid}",
        headers=auth["headers"],
        json={"reference_image_url": "https://example.com/" + "x" * 600},
    )
    assert resp.status_code == 422
