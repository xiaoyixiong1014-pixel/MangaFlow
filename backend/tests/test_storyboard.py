"""分镜工作流接口测试模块（文件级说明）。

覆盖链路：
- AI 拆解（decompose）：LLM 可达时逐场景生成 1~3 镜，不可达时规则降级
  每场景 2 镜；sequence 连续、项目状态推进 storyboard、角色强绑
- 重排（reorder）：sequence 按新顺序重写 1..n；非法重排 400
- 告警检查（warnings）：响应结构与总时长口径
- 他人项目 404
"""

import pytest
from sqlalchemy import select

from app.models import Character, Script
from tests.conftest import create_project, register_user, unique_suffix

# 两个测试场景（写入剧本 content.scenes，结构与 AI 生成结果同构）
_SCENES = [
    {
        "scene_no": 1,
        "title": "放学后的天台",
        "plot": "林小满在天台向陈默展示第一稿漫画",
        "dialogue": [
            {"character": "林小满", "line": "你看，这就是我的第一稿！"},
            {"character": "陈默", "line": "画得真不错。"},
        ],
        "duration_sec": 30,
    },
    {
        "scene_no": 2,
        "title": "夜晚的街道",
        "plot": "两人并肩走在回家的路上聊起梦想",
        "dialogue": [
            {"character": "林小满", "line": "总有一天我们会做出自己的动画。"},
        ],
        "duration_sec": 30,
    },
]


async def _make_scripted_project(client, auth, db, *, with_assets: bool = True) -> dict:
    """辅助：造一个带剧本场景数据的可拆解项目（内部函数）。

    流程：API 建项目 + 保存剧本输入 → ORM 以生产环境 AI 生成的字段级包装
    结构写入 content.scenes → 可选创建同名角色/场景资产供强绑匹配。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        with_assets: 是否创建与场景/对白同名的资产（用于强绑与实体链接）。

    Returns:
        dict: 项目响应 JSON。

    Raises:
        AssertionError: 接口返回非 200 时中断。
    """
    tag = unique_suffix()
    project = await create_project(
        client, auth["headers"], title=f"分镜项目_{tag}", style="日漫", target_duration=60
    )
    pid = project["id"]

    resp = await client.put(
        f"/api/projects/{pid}/script/input",
        headers=auth["headers"],
        json={"mode": "idea", "raw_text": "林小满：我们一起做动画吧。\n陈默：好。"},
    )
    assert resp.status_code == 200, resp.text

    # 以 wrap_content_fields 同构的字段级包装结构写入剧本内容
    script = (
        await db.execute(select(Script).where(Script.project_id == pid))
    ).scalar_one()
    script.content = {
        "title": {"value": "测试剧本", "source": "ai"},
        "logline": {"value": "两个少年的动画梦", "source": "ai"},
        "outline": {"value": [], "source": "ai"},
        "scenes": {"value": _SCENES, "source": "ai"},
    }
    await db.commit()

    if with_assets:
        char_resp = await client.post(
            f"/api/projects/{pid}/assets/characters",
            headers=auth["headers"],
            json={"name": "林小满", "description": "主角"},
        )
        assert char_resp.status_code == 200, char_resp.text
        # P0-1 锁定门禁：拆解测试假设资产已就绪（定妆完成并锁定），
        # ORM 直置就绪态避免门禁 409 干扰拆解逻辑本身
        char_orm = (
            await db.execute(
                select(Character).where(
                    Character.project_id == pid,
                    Character.name == "林小满",
                )
            )
        ).scalar_one()
        char_orm.face_locked = True
        char_orm.generation_status = "done"
        char_orm.asset_url = "/storage/generated/test_portrait.png"
        await db.commit()
        for scene in _SCENES:
            scene_resp = await client.post(
                f"/api/projects/{pid}/assets/scenes",
                headers=auth["headers"],
                json={"name": scene["title"], "description": scene["plot"]},
            )
            assert scene_resp.status_code == 200, scene_resp.text
    return project


async def test_chaijie_chansheng_fenjing_xulie_lianxu(client, auth, db):
    """拆解产出分镜：数量 >= 场景数、sequence 连续 1..n、状态推进、角色强绑。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_scripted_project(client, auth, db)
    pid = project["id"]

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/decompose", headers=auth["headers"]
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] >= len(_SCENES)  # 每场景至少 1 镜
    assert isinstance(data["degraded"], bool)
    if data["degraded"]:
        # 规则降级：每场景固定 2 镜
        assert data["total"] == 2 * len(_SCENES)

    # 列表 sequence 连续且与拆解总数一致
    listing = await client.get(f"/api/projects/{pid}/storyboards", headers=auth["headers"])
    assert listing.status_code == 200
    items = listing.json()
    assert len(items) == data["total"]
    assert [i["sequence"] for i in items] == list(range(1, len(items) + 1))

    # 场景强绑：剧本场景名与场景资产同名 → scene_id 命中
    assert all(i["scene_id"] is not None for i in items)

    # 角色强绑：对白角色「林小满」应链接到分镜
    linked_names = {c["name"] for item in items for c in item["characters"]}
    assert "林小满" in linked_names

    # 项目状态推进
    detail = await client.get(f"/api/projects/{pid}", headers=auth["headers"])
    assert detail.json()["status"] == "storyboard"


async def test_chongpai_xulie_lianxu(client, auth, db):
    """重排分镜：按逆序提交后 sequence 重写 1..n；缺漏提交返回 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_scripted_project(client, auth, db)
    pid = project["id"]

    decompose = await client.post(
        f"/api/projects/{pid}/storyboards/decompose", headers=auth["headers"]
    )
    assert decompose.status_code == 200
    total = decompose.json()["total"]

    listing = await client.get(f"/api/projects/{pid}/storyboards", headers=auth["headers"])
    items = listing.json()
    ids = [i["id"] for i in items]

    # 非法重排：漏掉一个 ID → 400
    bad = await client.post(
        f"/api/projects/{pid}/storyboards/reorder",
        headers=auth["headers"],
        json={"ordered_ids": ids[:-1]},
    )
    assert bad.status_code == 400

    # 合法重排：整体逆序
    reversed_ids = list(reversed(ids))
    resp = await client.post(
        f"/api/projects/{pid}/storyboards/reorder",
        headers=auth["headers"],
        json={"ordered_ids": reversed_ids},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == total

    listing2 = await client.get(f"/api/projects/{pid}/storyboards", headers=auth["headers"])
    items2 = listing2.json()
    assert [i["sequence"] for i in items2] == list(range(1, len(ids) + 1))
    assert [i["id"] for i in items2] == reversed_ids


async def test_gaojing_jiegou_yu_shichang_koujing(client, auth, db):
    """告警检查：返回 total_duration/target_duration/over_ratio/warnings 结构。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_scripted_project(client, auth, db)
    pid = project["id"]

    decompose = await client.post(
        f"/api/projects/{pid}/storyboards/decompose", headers=auth["headers"]
    )
    assert decompose.status_code == 200

    resp = await client.post(
        f"/api/projects/{pid}/storyboards/warnings", headers=auth["headers"]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ("total_duration", "target_duration", "over_ratio", "warnings"):
        assert key in body
    assert body["total_duration"] > 0
    assert body["target_duration"] == 60
    assert isinstance(body["warnings"], list)

    # 总时长口径 = 各分镜 duration 之和
    listing = await client.get(f"/api/projects/{pid}/storyboards", headers=auth["headers"])
    expected_total = sum(i["duration"] for i in listing.json())
    assert body["total_duration"] == expected_total


async def test_taren_xiangmu_chaijie_404(client, auth, db):
    """他人项目拆解返回 404（不泄露存在性）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文（项目归属者）。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 404 时中断。
    """
    project = await _make_scripted_project(client, auth, db)
    other = await register_user(client, prefix="sborder")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    resp = await client.post(
        f"/api/projects/{project['id']}/storyboards/decompose", headers=other_headers
    )
    assert resp.status_code == 404
