"""P-1 范式级重构 Task 11 测试模块（文件级说明）。

覆盖两组能力：
1. RD-08 影响面预览（GET /script/entities/impact）：
   - 角色在剧本 scenes（含对白）与分镜文本中的引用计数；
   - 无引用实体计数为 0；非法 entity_type 400；他人项目/不存在实体 404；
   - episode_id 过滤生效（分镜按集收敛）。
2. 剧本导出（GET /script/export，txt / fountain）：
   - TXT 单集 raw_text 命中、多集以集标题分隔；
   - Fountain 结构化渲染（INT./EXT. 场景标题、大写角色名、对白紧随、空行）；
   - 非法 format 400、无内容 404、Content-Disposition 文件名 URL 编码。
3. render_fountain 纯函数单测（int_ext/time_of_day 缺失回退、type=旁白）。
"""

from urllib.parse import quote

from app.models import Character, Episode, Script, Storyboard
from app.services.script_export_service import render_fountain
from tests.conftest import create_project, register_user

# 剧本 content 样例：整体包装形态（source=user），含 1 场、对白引用「林小满」
_SCRIPT_CONTENT = {
    "value": {
        "title": "天台之约",
        "logline": "两个少女的约定",
        "scenes": [
            {
                "scene_no": 1,
                "title": "天台立约",
                "plot": "林小满在天台立下约定",
                "action": "林小满握紧拳头远眺城市",
                "dialogue": [
                    {"character": "林小满", "type": "对白", "line": "我们一起拿金奖吧。"},
                ],
                "duration_sec": 45,
            },
        ],
    },
    "source": "user",
}

# Fountain 导出样例 content：ext + dusk + 对白 + 旁白
_FOUNTAIN_CONTENT = {
    "value": {
        "title": "天台之约",
        "logline": "两个少女的约定",
        "scenes": [
            {
                "scene_no": 2,
                "title": "天台立约",
                "plot": "立约",
                "action": "两人站上天台边缘远眺城市",
                "location": "学校天台",
                "time_of_day": "dusk",
                "int_ext": "ext",
                "dialogue": [
                    {"character": "林小满", "type": "对白", "line": "我们一起拿金奖吧。"},
                    {"character": "", "type": "旁白", "line": "风把约定吹向城市上空。"},
                ],
                "duration_sec": 45,
            },
        ],
    },
    "source": "user",
}


async def _seed_impact_project(client, auth, db):
    """创建影响面用例的前置数据（内部函数）。

    建项目 + 2 角色（林小满/陈默）+ 1 个引用林小满的剧本行 + 2 条对白含
    林小满的分镜，返回 (project, 林小满, 陈默)。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        tuple: (项目 dict, 角色 A ORM, 角色 B ORM)。

    Raises:
        AssertionError: 造数失败（接口非 200 / 角色缺失）时中断。
    """
    project = await create_project(client, auth["headers"], title="影响面项目")
    char_a = Character(project_id=project["id"], name="林小满", visual_prompt="x")
    char_b = Character(project_id=project["id"], name="陈默", visual_prompt="x")
    db.add_all([char_a, char_b])
    await db.commit()
    await db.refresh(char_a)
    await db.refresh(char_b)
    db.add(Script(
        project_id=project["id"], content=_SCRIPT_CONTENT, raw_text="", version=1))
    db.add_all([
        Storyboard(project_id=project["id"], sequence=1, duration=5,
                   dialogue="林小满：你好", status="pending"),
        Storyboard(project_id=project["id"], sequence=2, duration=5,
                   dialogue="林小满：加油", status="pending"),
    ])
    await db.commit()
    return project, char_a, char_b


async def test_impact_character_refs(client, auth, db):
    """影响面预览：角色 A 剧本引用 ≥1、分镜 2 条；无引用角色 B 计数为 0。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project, char_a, char_b = await _seed_impact_project(client, auth, db)
    url = f"/api/projects/{project['id']}/script/entities/impact"

    resp = await client.get(
        url, headers=auth["headers"],
        params={"entity_type": "character", "entity_id": char_a.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entity_name"] == "林小满"
    assert body["script_refs"] >= 1
    assert body["storyboard_refs"] == 2
    assert body["summary"]
    assert body["storyboard_ids"]
    assert body["script_locations"]

    resp_b = await client.get(
        url, headers=auth["headers"],
        params={"entity_type": "character", "entity_id": char_b.id})
    assert resp_b.status_code == 200
    assert resp_b.json()["script_refs"] == 0
    assert resp_b.json()["storyboard_refs"] == 0


async def test_impact_invalid_and_not_found(client, auth, db):
    """影响面预览异常分支：非法 entity_type 400、他人项目 404、实体不存在 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码不符时中断。
    """
    project, char_a, _ = await _seed_impact_project(client, auth, db)
    url = f"/api/projects/{project['id']}/script/entities/impact"

    # 非法 entity_type → 400
    bad = await client.get(
        url, headers=auth["headers"],
        params={"entity_type": "vehicle", "entity_id": char_a.id})
    assert bad.status_code == 400

    # 不存在的实体 → 404
    missing = await client.get(
        url, headers=auth["headers"],
        params={"entity_type": "character", "entity_id": 99999999})
    assert missing.status_code == 404

    # 他人项目 → 404（不泄露存在性）
    other = await register_user(client, prefix="other")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    forbidden = await client.get(
        url, headers=other_headers,
        params={"entity_type": "character", "entity_id": char_a.id})
    assert forbidden.status_code == 404


async def test_impact_episode_filter(client, auth, db):
    """影响面预览：episode_id 过滤后仅统计该集分镜。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 计数不符时中断。
    """
    project = await create_project(client, auth["headers"], title="集过滤项目")
    pid = project["id"]
    ep1 = Episode(project_id=pid, title="第一集", sequence=1)
    ep2 = Episode(project_id=pid, title="第二集", sequence=2)
    db.add_all([ep1, ep2])
    await db.commit()
    await db.refresh(ep1)
    await db.refresh(ep2)

    char = Character(project_id=pid, name="沈青", visual_prompt="x")
    db.add(char)
    await db.commit()
    await db.refresh(char)

    db.add_all([
        Storyboard(project_id=pid, episode_id=ep1.id, sequence=1, duration=5,
                   dialogue="沈青：甲", status="pending"),
        Storyboard(project_id=pid, episode_id=ep2.id, sequence=1, duration=5,
                   dialogue="沈青：乙", status="pending"),
    ])
    await db.commit()

    url = f"/api/projects/{pid}/script/entities/impact"
    resp = await client.get(
        url, headers=auth["headers"],
        params={"entity_type": "character", "entity_id": char.id, "episode_id": ep1.id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["storyboard_refs"] == 1


async def test_export_txt(client, auth, db):
    """TXT 导出：单集 raw_text 命中；多集含集标题分隔。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(client, auth["headers"], title="导出项目")
    pid = project["id"]
    ep1 = Episode(project_id=pid, title="第一集", sequence=1)
    ep2 = Episode(project_id=pid, title="第二集", sequence=2)
    db.add_all([ep1, ep2])
    await db.commit()
    await db.refresh(ep1)
    await db.refresh(ep2)
    db.add_all([
        Script(project_id=pid, episode_id=ep1.id, content={"raw": "第一集内容"},
               raw_text="这是第一集的正文。", version=1),
        Script(project_id=pid, episode_id=ep2.id, content={"raw": "第二集内容"},
               raw_text="这是第二集的正文。", version=1),
    ])
    await db.commit()

    url = f"/api/projects/{pid}/script/export"
    # 单集导出：仅命中该集正文
    single = await client.get(
        url, headers=auth["headers"], params={"format": "txt", "episode_id": ep1.id})
    assert single.status_code == 200, single.text
    assert "这是第一集的正文。" in single.text
    assert "这是第二集的正文。" not in single.text

    # 全量导出：多集带集标题分隔
    multi = await client.get(url, headers=auth["headers"], params={"format": "txt"})
    assert multi.status_code == 200, multi.text
    assert "===== 第1集 · 第一集 =====" in multi.text
    assert "===== 第2集 · 第二集 =====" in multi.text


async def test_export_fountain(client, auth, db):
    """Fountain 导出：含 INT./EXT. 场景标题、大写角色名、对白紧随、空行结构。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(client, auth["headers"], title="fountain项目")
    pid = project["id"]
    ep = Episode(project_id=pid, title="第一集", sequence=1)
    db.add(ep)
    await db.commit()
    await db.refresh(ep)
    db.add(Script(project_id=pid, episode_id=ep.id, content=_FOUNTAIN_CONTENT,
                  raw_text="", version=1))
    await db.commit()

    url = f"/api/projects/{pid}/script/export"
    resp = await client.get(
        url, headers=auth["headers"], params={"format": "fountain", "episode_id": ep.id})
    assert resp.status_code == 200, resp.text
    text = resp.text
    # 场景标题：EXT. + 地点 - 时段（大写），前空行、标题后空行
    assert "EXT. 学校天台 - DUSK" in text
    # 场景标题后空行接动作行
    assert "EXT. 学校天台 - DUSK\n\n两人站上天台边缘远眺城市" in text
    # 动作行
    assert "两人站上天台边缘远眺城市" in text
    # 角色行大写紧接对白行
    assert "林小满\n我们一起拿金奖吧。" in text
    # 旁白类型渲染为「旁白」
    assert "旁白\n风把约定吹向城市上空。" in text


async def test_export_errors_and_headers(client, auth, db):
    """导出异常分支与响应头：非法 format 400、无内容 404、文件名 URL 编码。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码或响应头不符时中断。
    """
    project = await create_project(client, auth["headers"], title="标题项目")
    pid = project["id"]
    url = f"/api/projects/{pid}/script/export"

    # 无任何剧本内容 → 404
    empty = await client.get(url, headers=auth["headers"], params={"format": "txt"})
    assert empty.status_code == 404

    # 非法 format → 400
    bad = await client.get(url, headers=auth["headers"], params={"format": "docx"})
    assert bad.status_code == 400

    # 有内容时校验文件名 URL 编码
    db.add(Script(project_id=pid, content={"raw": "内容"}, raw_text="正文内容", version=1))
    await db.commit()
    ok = await client.get(url, headers=auth["headers"], params={"format": "txt"})
    assert ok.status_code == 200, ok.text
    disposition = ok.headers["content-disposition"]
    assert "attachment" in disposition
    assert quote("标题项目.txt", safe="") in disposition


def test_render_fountain_pure():
    """render_fountain 纯函数单测：缺 int_ext/time_of_day 回退、type=旁白。

    Returns:
        None。

    Raises:
        AssertionError: 渲染结果不符时中断。
    """
    episodes = [{
        "episode_no": None,
        "title": "单篇",
        "plain": {"scenes": [{
            "scene_no": 3,
            "title": "无名场景",
            "plot": "剧情展开",
            "dialogue": [
                {"character": "沈青", "type": "对白", "line": "你好。"},
                {"character": "", "type": "旁白", "line": "内心独白。"},
            ],
        }]},
    }]
    text = render_fountain(episodes)
    # 缺 int_ext/time_of_day → 回退「镜头N 标题」
    assert "镜头3 无名场景" in text
    # 动作行取 plot（无 action 时）
    assert "剧情展开" in text
    # 角色名大写 + 对白紧随
    assert "沈青\n你好。" in text
    # type=旁白 → 角色行「旁白」
    assert "旁白\n内心独白。" in text
