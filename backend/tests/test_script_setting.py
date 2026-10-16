"""P2 故事设定产物接口测试模块（文件级说明，redesign-script-workspace Task 2）。

覆盖链路（全部 mock LLM：monkeypatch app.agents.script_pipeline 模块级
_llm_chat 包装函数，返回预置合法 JSON 字符串；与 test_script_characters.py
同风格，fake 签名兼容 agent_name 关键字参数）：
- generate 成功：保存输入后生成 → story_setting 落三段（source=ai）、
  config.script_characters 落候选、phase NULL→characters、写
  setting_generated 快照；
- generate 无输入：409（提示先保存剧本输入）；
- GET 空态：未生成时 setting=None 且 planned/confirmed 均为空列表（不 404）；
- PUT 编辑：合并保存（未传字段保留 AI 值）、source 置 user、写
  setting_edited 快照、phase 不变；
- regenerate 带 instruction：设定段重出（source=ai）、confirmed=True 的
  已确认候选原样保留、同名新候选丢弃、characters 表行数不变、phase 不回退；
- 越权：用户 B 访问用户 A 的项目 4 个接口一律 404；
- 阶段守卫：phase=body 时 generate/regenerate 一律 409（正文阶段设定锁定）。
"""

import json
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import script_pipeline as sp_mod
from app.models import Character, Project, VersionSnapshot
from tests.conftest import create_project, register_user

# 保存输入时的原始文本（idea 模式一句话创意）
_SAMPLE_RAW = "一句话创意：两个少女约定一起成为漫画家的故事。"

# AI 生成（mock A）的设定三段
_LOGELINE_A = "两个性格互补的少女约定一起成为漫画家。"
_CONFLICT_A = "热情与天赋的错位：小满有热情无天赋，陈默有天赋无热情。"
_WORLDVIEW_A = "现代都市漫画行业，新人漫画家靠新人奖出道。"


def _setting_payload(
    logline: str = _LOGELINE_A,
    core_conflict: str = _CONFLICT_A,
    worldview: str = _WORLDVIEW_A,
    character_names: Optional[List[str]] = None,
) -> str:
    """构造合法的 setting_planner LLM 输出 JSON 字符串（内部函数）。

    Args:
        logline: 一句话故事。
        core_conflict: 核心冲突描述。
        worldview: 世界观设定。
        character_names: 人物候选名单（None 时缺省为 [林小满, 陈默]）。

    Returns:
        str: {"logline", "core_conflict", "worldview", "characters":[...]}
            的 JSON 文本（角色人设按名单自动填充占位设定）。

    Raises:
        无：纯结构构造。
    """
    names = character_names if character_names is not None else ["林小满", "陈默"]
    role_by_name = {"林小满": "lead", "陈默": "support", "王婆": "villain"}
    characters = [
        {
            "name": name,
            "role_type": role_by_name.get(name, "support"),
            "age": "约17岁",
            "personality": f"{name}的性格设定",
            "appearance": f"{name}的外观设定",
            "motivation": f"{name}的行为动机",
            "arc": f"{name}的人物弧光",
            "relations": [],
        }
        for name in names
    ]
    return json.dumps({
        "logline": logline,
        "core_conflict": core_conflict,
        "worldview": worldview,
        "characters": characters,
    }, ensure_ascii=False)


def _mock_llm_chat(payload: str, calls: Optional[List[Dict[str, Any]]] = None):
    """构造可替换 script_pipeline._llm_chat 的异步假函数（内部工厂）。

    签名兼容 plan_story_setting 的调用形态（messages, *, temperature,
    agent_name）——plan_story_setting 显式传 agent_name="setting_planner"。

    Args:
        payload: 每次 LLM 调用返回的固定文本。
        calls: 可选收集器列表；传入时每次调用追加
            {"temperature": 温度, "agent_name": 智能体名} 供断言。

    Returns:
        Callable: 签名兼容 _llm_chat 的异步函数。

    Raises:
        无。
    """

    async def fake_chat(messages, *, temperature, agent_name=None):
        """替换 _llm_chat：恒返回预置文本（内部函数）。

        Args:
            messages: OpenAI 格式消息列表（忽略）。
            temperature: 采样温度（记录进 calls）。
            agent_name: 调用方 Agent 标识（记录进 calls）。

        Returns:
            str: 预置的 payload 文本。

        Raises:
            无。
        """
        if calls is not None:
            calls.append({"temperature": temperature, "agent_name": agent_name})
        return payload

    return fake_chat


async def _make_project_with_input(client: httpx.AsyncClient, auth: Dict[str, Any]) -> dict:
    """辅助：创建项目并保存一句话创意输入（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        dict: 项目响应 JSON（含 id）。

    Raises:
        AssertionError: 任一接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title="故事设定项目")
    resp = await client.put(
        f"/api/projects/{project['id']}/script/input",
        headers=auth["headers"],
        json={"mode": "idea", "raw_text": _SAMPLE_RAW},
    )
    assert resp.status_code == 200, resp.text
    return project


async def _generate_setting(client: httpx.AsyncClient, auth: Dict[str, Any],
                            project_id: int, monkeypatch=None,
                            payload: Optional[str] = None) -> dict:
    """辅助：mock LLM 后调用 setting/generate 接口并断言 200（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        project_id: 项目 ID。
        monkeypatch: pytest 夹具；传入时经其替换 _llm_chat（用例结束自动还原）。
        payload: 可选 mock 输出文本（None 时用缺省设定 payload）。

    Returns:
        dict: generate 响应 JSON（{"setting", "characters", "phase"}）。

    Raises:
        AssertionError: 接口返回非 200 时中断。
    """
    if monkeypatch is not None:
        monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(payload or _setting_payload()))
    resp = await client.post(
        f"/api/projects/{project_id}/script/setting/generate",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _fetch_project(db: AsyncSession, project_id: int) -> Project:
    """辅助：重新查询项目 ORM 行（内部函数，绕开会话缓存）。

    Args:
        db: 测试库会话。
        project_id: 项目 ID。

    Returns:
        Project: 项目 ORM 对象。

    Raises:
        AssertionError: 项目不存在时中断。
    """
    return (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()


# ---------------------------------------------------------------------------
# 1. generate 成功：落库 story_setting + 候选 + 阶段流转 + 快照
# ---------------------------------------------------------------------------
async def test_generate_success(client, auth, db, monkeypatch):
    """generate 成功：200、story_setting 落三段（source=ai）、候选写 config、
    phase NULL→characters、setting_generated 快照写入。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直接断言落库）。
        monkeypatch: 替换 _llm_chat 为恒返设定 JSON 的假函数。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_setting_payload(), calls))
    project = await _make_project_with_input(client, auth)

    resp = await client.post(
        f"/api/projects/{project['id']}/script/setting/generate",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 响应：设定三段 + 人物候选 + 阶段推进
    assert body["phase"] == "characters"
    setting = body["setting"]
    assert setting["logline"] == _LOGELINE_A
    assert setting["core_conflict"] == _CONFLICT_A
    assert setting["worldview"] == _WORLDVIEW_A
    assert setting["source"] == "ai"
    assert setting["updated_at"]
    assert len(body["characters"]) == 2
    for c in body["characters"]:
        assert c["source"] == "planned"
        assert c["candidate_id"] and len(c["candidate_id"]) == 8

    # LLM 调用恰好 1 次（成功不重试）：温度 0.8、agent_name=setting_planner
    assert len(calls) == 1
    assert calls[0]["temperature"] == 0.8
    assert calls[0]["agent_name"] == "setting_planner"

    # 落库：story_setting 三段 + source=ai；config 候选 2 条；phase 已持久化
    proj_row = await _fetch_project(db, project["id"])
    assert proj_row.story_setting["logline"] == _LOGELINE_A
    assert proj_row.story_setting["source"] == "ai"
    assert proj_row.script_phase == "characters"
    stage = (proj_row.config or {})["script_characters"]
    assert len(stage) == 2
    assert all(item["confirmed"] is False for item in stage)

    # 版本快照：setting_generated 已写入
    snapshots = (
        await db.execute(
            select(VersionSnapshot).where(
                VersionSnapshot.project_id == project["id"],
                VersionSnapshot.stage == "setting_generated",
            )
        )
    ).scalars().all()
    assert len(snapshots) == 1
    assert snapshots[0].summary["character_count"] == 2


async def test_generate_no_input_409(client, auth):
    """generate 无输入：项目未保存剧本输入时 409，detail 提示先保存输入。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 409 时中断。
    """
    project = await create_project(client, auth["headers"], title="设定空输入项目")
    resp = await client.post(
        f"/api/projects/{project['id']}/script/setting/generate",
        headers=auth["headers"],
    )
    assert resp.status_code == 409, resp.text
    assert "请先保存剧本输入" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 2. GET 空态：未生成时返回空对象（不 404）
# ---------------------------------------------------------------------------
async def test_get_empty(client, auth):
    """GET 空态：未生成设定时 200 且 setting=None、planned/confirmed 为空。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 状态码或空态字段不符时中断。
    """
    project = await create_project(client, auth["headers"], title="设定空态项目")
    resp = await client.get(
        f"/api/projects/{project['id']}/script/setting", headers=auth["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["setting"] is None
    assert body["planned"] == []
    assert body["confirmed"] == []
    assert body["phase"] is None


# ---------------------------------------------------------------------------
# 3. PUT 编辑：source=user + 快照 + phase 不变
# ---------------------------------------------------------------------------
async def test_put_edit_source_user(client, auth, db, monkeypatch):
    """PUT 编辑：合并保存（未传字段保留 AI 值）、source 置 user、
    setting_edited 快照写入、phase 保持 characters 不变。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言 story_setting 落库与快照）。
        monkeypatch: 替换 _llm_chat（generate 用）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)
    await _generate_setting(client, auth, project["id"], monkeypatch)

    resp = await client.put(
        f"/api/projects/{project['id']}/script/setting",
        headers=auth["headers"],
        json={"logline": "用户改写的一句话故事。"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 响应：logline 已更新、其余字段保留 AI 值、source=user
    assert body["setting"]["logline"] == "用户改写的一句话故事。"
    assert body["setting"]["core_conflict"] == _CONFLICT_A
    assert body["setting"]["worldview"] == _WORLDVIEW_A
    assert body["setting"]["source"] == "user"
    assert body["phase"] == "characters"  # 编辑不改变阶段

    # 落库：source=user + updated_at 刷新
    proj_row = await _fetch_project(db, project["id"])
    assert proj_row.story_setting["source"] == "user"
    assert proj_row.story_setting["logline"] == "用户改写的一句话故事。"
    assert proj_row.story_setting["updated_at"]
    assert proj_row.script_phase == "characters"

    # 版本快照：setting_edited 已写入
    snapshots = (
        await db.execute(
            select(VersionSnapshot).where(
                VersionSnapshot.project_id == project["id"],
                VersionSnapshot.stage == "setting_edited",
            )
        )
    ).scalars().all()
    assert len(snapshots) == 1

    # 编辑请求体三字段全 None（空编辑）：422（Pydantic 校验）
    empty = await client.put(
        f"/api/projects/{project['id']}/script/setting",
        headers=auth["headers"],
        json={},
    )
    assert empty.status_code == 422, empty.text


# ---------------------------------------------------------------------------
# 4. regenerate 带 instruction：已确认角色不回退
# ---------------------------------------------------------------------------
async def test_regenerate_keeps_confirmed(client, auth, db, monkeypatch):
    """regenerate 带 instruction：设定段重出（source=ai）、confirmed=True
    候选原样保留、与已确认同名的新候选丢弃、characters 表行数不变、
    phase 不回退（outline 保持）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言 config 候选与 characters 表）。
        monkeypatch: 先后替换 _llm_chat（generate 用 A、regenerate 用 B）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)
    await _generate_setting(client, auth, project["id"], monkeypatch)

    # 确认两角色入池（同名幂等）：phase 推进到 outline
    confirm = await client.post(
        f"/api/projects/{project['id']}/script/characters/confirm",
        headers=auth["headers"],
        json={"characters": [{"name": "林小满"}, {"name": "陈默"}]},
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["phase"] == "outline"

    # regenerate 用 mock B：新设定 + 人物名单含已确认的「林小满」（应被丢弃）
    # 与全新角色「王婆」（应作为未确认候选新增）
    logline_b = "重生成后的一句话故事。"
    monkeypatch.setattr(
        sp_mod, "_llm_chat",
        _mock_llm_chat(_setting_payload(
            logline=logline_b, core_conflict="更强的核心冲突。",
            worldview="重生成后的世界观。", character_names=["林小满", "王婆"])))

    resp = await client.post(
        f"/api/projects/{project['id']}/script/setting/regenerate",
        headers=auth["headers"],
        json={"instruction": "冲突更强一些"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 设定段重出（source=ai）；phase 保持 outline 不回退
    assert body["setting"]["logline"] == logline_b
    assert body["setting"]["source"] == "ai"
    assert body["phase"] == "outline"

    # 落库：story_setting 更新；characters 表仍 2 行（已入池角色不回退）
    proj_row = await _fetch_project(db, project["id"])
    assert proj_row.story_setting["logline"] == logline_b
    assert proj_row.story_setting["source"] == "ai"
    assert proj_row.script_phase == "outline"
    char_rows = (
        await db.execute(select(Character).where(Character.project_id == project["id"]))
    ).scalars().all()
    assert len(char_rows) == 2

    # config 候选：confirmed=True 的林小满/陈默原样保留（人设字段不变），
    # 新增未确认的王婆；与已确认同名的重生成林小满被丢弃（不出现重复）
    stage = (proj_row.config or {})["script_characters"]
    by_name = {c["name"]: c for c in stage}
    assert set(by_name) == {"林小满", "陈默", "王婆"}
    assert by_name["林小满"]["confirmed"] is True
    assert by_name["林小满"]["personality"] == "林小满的性格设定"
    assert by_name["陈默"]["confirmed"] is True
    assert by_name["王婆"]["confirmed"] is False
    assert by_name["王婆"]["role_type"] == "villain"
    lin_count = sum(1 for c in stage if c["name"] == "林小满")
    assert lin_count == 1

    # 版本快照：第二条 setting_generated（regenerate），summary 带保留/新增计数
    snapshots = (
        await db.execute(
            select(VersionSnapshot).where(
                VersionSnapshot.project_id == project["id"],
                VersionSnapshot.stage == "setting_generated",
            ).order_by(VersionSnapshot.id)
        )
    ).scalars().all()
    assert len(snapshots) == 2
    assert snapshots[1].summary["confirmed_kept"] == 2
    assert snapshots[1].summary["fresh_candidates"] == 1


# ---------------------------------------------------------------------------
# 5. 越权：用户 B 访问用户 A 的项目一律 404
# ---------------------------------------------------------------------------
async def test_forbidden_404(client, auth, monkeypatch):
    """越权：用户 B 访问用户 A 的项目（generate/GET/PUT/regenerate）一律 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 用户 A（项目归属者）。
        monkeypatch: 替换 _llm_chat（保证即便通过归属校验也不会真调 LLM）。

    Returns:
        None。

    Raises:
        AssertionError: 任一状态码非 404 时中断。
    """
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_setting_payload()))
    project = await _make_project_with_input(client, auth)

    other = await register_user(client, prefix="other")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    gen = await client.post(
        f"/api/projects/{project['id']}/script/setting/generate",
        headers=other_headers,
    )
    assert gen.status_code == 404, gen.text
    got = await client.get(
        f"/api/projects/{project['id']}/script/setting", headers=other_headers)
    assert got.status_code == 404, got.text
    put = await client.put(
        f"/api/projects/{project['id']}/script/setting",
        headers=other_headers,
        json={"logline": "越权改写"},
    )
    assert put.status_code == 404, put.text
    regen = await client.post(
        f"/api/projects/{project['id']}/script/setting/regenerate",
        headers=other_headers,
        json={"instruction": "越权重生成"},
    )
    assert regen.status_code == 404, regen.text


# ---------------------------------------------------------------------------
# 6. 阶段守卫：phase=body 时 generate/regenerate 一律 409
# ---------------------------------------------------------------------------
async def test_body_phase_locked_409(client, auth, monkeypatch, db):
    """正文阶段设定锁定：script_phase='body' 的项目调 generate/regenerate
    一律 409（中文提示），LLM 不被调用、story_setting 不被写入。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文（项目归属者）。
        db: 测试库会话（直插 body 阶段项目并断言 story_setting 未写入）。
        monkeypatch: 替换 _llm_chat（若被调用即失败——记录调用次数）。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 409 时中断。
    """
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_setting_payload(), calls))

    # 直插一个归属 auth 用户的 body 阶段项目（无需剧本输入：阶段守卫先于输入校验）
    project = Project(user_id=auth["user_id"], title="正文阶段项目",
                      status="draft", script_phase="body")
    db.add(project)
    await db.commit()
    project_id = project.id

    gen = await client.post(
        f"/api/projects/{project_id}/script/setting/generate",
        headers=auth["headers"],
    )
    assert gen.status_code == 409, gen.text
    assert "故事设定已锁定" in gen.json()["detail"]

    regen = await client.post(
        f"/api/projects/{project_id}/script/setting/regenerate",
        headers=auth["headers"],
        json={"instruction": "尝试重生"},
    )
    assert regen.status_code == 409, regen.text
    assert "故事设定已锁定" in regen.json()["detail"]

    # LLM 未被调用、story_setting 未写入
    assert calls == []
    row = await _fetch_project(db, project_id)
    assert row.story_setting is None
