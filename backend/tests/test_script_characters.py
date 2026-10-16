"""P-2 角色设定阶段接口测试模块（文件级说明，P-1 范式级重构 Task 3）。

覆盖链路（全部 mock LLM：monkeypatch app.agents.script_pipeline 模块级
_llm_chat 包装函数，返回预置合法 JSON 字符串）：
- generate 成功：保存输入后生成候选 → config.script_characters 落 2 条、
  phase 推进到 characters；
- generate 无输入：409（提示先保存剧本输入）；
- GET 列表：空候选 planned=[]，生成后 planned=2，confirmed 生成前为空；
- PUT 批量编辑：按 id 改名 + 按 name 删除 + 新增 → config 同步；
- regenerate 候选：仅目标候选被替换、其余不动；未知 cid 404；
- confirm：幂等入池 characters 表（人设字段/confirmed_at/relations）、
  写 characters_confirmed 快照、阶段推进 outline；重复确认不重复建行；
- 越权：用户 B 访问用户 A 的项目一律 404；
- 解析容错：围栏包裹 + 无名条目丢弃 + 白名单外 role_type 归 support。
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


def _two_characters_payload() -> str:
    """构造含 2 个角色的合法 LLM 输出 JSON 字符串（内部函数）。

    Returns:
        str: {"characters": [林小满(lead, 带关系), 陈默(support)]} 的 JSON 文本。

    Raises:
        无：纯结构构造。
    """
    return json.dumps({
        "characters": [
            {
                "name": "林小满",
                "role_type": "lead",
                "age": "约17岁",
                "personality": "倔强善良，遇挫不折",
                "appearance": "短发少女，戴红色发卡",
                "motivation": "和挚友一起成为漫画家",
                "arc": "从畏缩退让到坚定追梦",
                "relations": [{"target": "陈默", "relation": "漫画搭档"}],
            },
            {
                "name": "陈默",
                "role_type": "support",
                "age": "约18岁",
                "personality": "沉稳寡言，内心炽热",
                "appearance": "高瘦少年，黑框眼镜",
                "motivation": "守护小满的梦想",
                "arc": "从旁观者到并肩者",
                "relations": [{"target": "林小满", "relation": "漫画搭档"}],
            },
        ]
    }, ensure_ascii=False)


def _mock_llm_chat(payload: str, calls: Optional[List[Dict[str, Any]]] = None):
    """构造可替换 script_pipeline._llm_chat 的异步假函数（内部工厂）。

    Args:
        payload: 每次 LLM 调用返回的固定文本。
        calls: 可选收集器列表；传入时每次调用追加
            {"temperature": 温度} 记录供断言。

    Returns:
        Callable: 签名兼容 _llm_chat(messages, *, temperature) 的异步函数。

    Raises:
        无。
    """

    async def fake_chat(messages, *, temperature):
        """替换 _llm_chat：恒返回预置文本（内部函数）。

        Args:
            messages: OpenAI 格式消息列表（忽略）。
            temperature: 采样温度（记录进 calls）。

        Returns:
            str: 预置的 payload 文本。

        Raises:
            无。
        """
        if calls is not None:
            calls.append({"temperature": temperature})
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
    project = await create_project(client, auth["headers"], title="角色流水线项目")
    resp = await client.put(
        f"/api/projects/{project['id']}/script/input",
        headers=auth["headers"],
        json={"mode": "idea", "raw_text": _SAMPLE_RAW},
    )
    assert resp.status_code == 200, resp.text
    return project


async def _generate_candidates(client: httpx.AsyncClient, auth: Dict[str, Any],
                               project_id: int, monkeypatch=None) -> dict:
    """辅助：mock LLM 后调用 generate 接口并断言 200（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        project_id: 项目 ID。
        monkeypatch: pytest 夹具；传入时经其替换 _llm_chat（用例结束自动
            还原），未传入时仅发出请求（不替换，供已替换场景复用）。

    Returns:
        dict: generate 响应 JSON（{"characters": [...], "phase": ...}）。

    Raises:
        AssertionError: 接口返回非 200 时中断。
    """
    if monkeypatch is not None:
        monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_two_characters_payload()))
    resp = await client.post(
        f"/api/projects/{project_id}/script/characters/generate",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _fetch_project_config(db: AsyncSession, project_id: int) -> Dict[str, Any]:
    """辅助：重新查询项目并返回其 config（内部函数，绕开会话缓存）。

    Args:
        db: 测试库会话。
        project_id: 项目 ID。

    Returns:
        Dict[str, Any]: 项目 config JSONB（无则空 dict）。

    Raises:
        AssertionError: 项目不存在时中断。
    """
    row = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one()
    return dict(row.config or {})


# ---------------------------------------------------------------------------
# 1. generate 成功
# ---------------------------------------------------------------------------
async def test_generate_success(client, auth, db, monkeypatch):
    """generate 成功：200、config.script_characters 落 2 条、phase='characters'。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直接断言 config 与 phase 落库）。
        monkeypatch: 替换 _llm_chat 为恒返 2 角色的假函数。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_two_characters_payload(), calls))
    project = await _make_project_with_input(client, auth)

    resp = await client.post(
        f"/api/projects/{project['id']}/script/characters/generate",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 响应：2 个候选（带 8 位 hex 临时 id），phase 推进到 characters
    assert body["phase"] == "characters"
    assert len(body["characters"]) == 2
    names = {c["name"] for c in body["characters"]}
    assert names == {"林小满", "陈默"}
    for c in body["characters"]:
        assert c["source"] == "planned"
        assert c["candidate_id"] and len(c["candidate_id"]) == 8

    # LLM 调用恰好 1 次（成功不重试），json_mode 温度 0.8
    assert len(calls) == 1 and calls[0]["temperature"] == 0.8

    # 落库：config.script_characters 2 条、confirmed=False；phase 已持久化
    config = await _fetch_project_config(db, project["id"])
    stage = config["script_characters"]
    assert len(stage) == 2
    assert all(item["confirmed"] is False for item in stage)
    assert {item["name"] for item in stage} == {"林小满", "陈默"}
    proj_row = (
        await db.execute(select(Project).where(Project.id == project["id"]))
    ).scalar_one()
    assert proj_row.script_phase == "characters"


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
    project = await create_project(client, auth["headers"], title="空输入项目")
    resp = await client.post(
        f"/api/projects/{project['id']}/script/characters/generate",
        headers=auth["headers"],
    )
    assert resp.status_code == 409, resp.text
    assert "请先保存剧本输入" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 2. GET 列表
# ---------------------------------------------------------------------------
async def test_list_empty_then_two(client, auth, monkeypatch):
    """GET 列表：未生成时 planned=[] 且 confirmed=[]；generate 后 planned=2。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 替换 _llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)

    # 未生成候选：planned/confirmed 均为空列表
    empty = await client.get(
        f"/api/projects/{project['id']}/script/characters", headers=auth["headers"])
    assert empty.status_code == 200, empty.text
    empty_body = empty.json()
    assert empty_body["planned"] == []
    assert empty_body["confirmed"] == []

    # 生成后：planned=2，confirmed 仍为空（尚未确认入池）
    await _generate_candidates(client, auth, project["id"], monkeypatch)
    got = await client.get(
        f"/api/projects/{project['id']}/script/characters", headers=auth["headers"])
    assert got.status_code == 200, got.text
    body = got.json()
    assert len(body["planned"]) == 2
    assert body["confirmed"] == []
    assert body["phase"] == "characters"


# ---------------------------------------------------------------------------
# 3. PUT 批量编辑
# ---------------------------------------------------------------------------
async def test_batch_edit(client, auth, db, monkeypatch):
    """PUT 批量编辑：按 id 改 1 个角色名 + 按 name 删 1 个 + 新增 1 个 →
    config 候选同步为 2 条且内容正确。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言 config 落库）。
        monkeypatch: 替换 _llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)
    generated = await _generate_candidates(client, auth, project["id"], monkeypatch)
    candidates = generated["characters"]
    cid_xiaoman = next(c["candidate_id"] for c in candidates if c["name"] == "林小满")

    resp = await client.put(
        f"/api/projects/{project['id']}/script/characters",
        headers=auth["headers"],
        json={
            "upsert": [
                # 按 id 匹配改名（其余字段不动）
                {"id": cid_xiaoman, "name": "林小满（改）"},
                # 全新条目：新增候选
                {"name": "王婆", "role_type": "villain", "age": "约60岁"},
            ],
            "remove_names": ["陈默"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phase"] == "characters"

    # 编辑结果：改名后的林小满 + 新增的王婆；陈默被删除
    by_name = {c["name"]: c for c in body["characters"]}
    assert set(by_name) == {"林小满（改）", "王婆"}
    # 改名候选保留原 id 与原人设字段
    assert by_name["林小满（改）"]["candidate_id"] == cid_xiaoman
    assert by_name["林小满（改）"]["personality"] == "倔强善良，遇挫不折"
    assert by_name["王婆"]["role_type"] == "villain"

    # config 同步落库
    config = await _fetch_project_config(db, project["id"])
    stage_names = {item["name"] for item in config["script_characters"]}
    assert stage_names == {"林小满（改）", "王婆"}


# ---------------------------------------------------------------------------
# 4. regenerate 候选
# ---------------------------------------------------------------------------
async def test_regenerate_candidate(client, auth, db, monkeypatch):
    """regenerate 候选：mock 返回替换角色 → 仅该候选更新，另一条不动；
    未知 cid → 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言 config 候选变更）。
        monkeypatch: 先后替换 _llm_chat（generate 用与 regenerate 用）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)
    generated = await _generate_candidates(client, auth, project["id"], monkeypatch)
    candidates = generated["characters"]
    cid_xiaoman = next(c["candidate_id"] for c in candidates if c["name"] == "林小满")

    # regenerate 用 mock：返回替换后的角色（仅 1 个）
    replaced = json.dumps({
        "characters": [{
            "name": "林小满·重塑",
            "role_type": "lead",
            "age": "约18岁",
            "personality": "更加果敢",
            "appearance": "马尾少女",
            "motivation": "证明自己",
            "arc": "从依赖到独当一面",
            "relations": [{"target": "陈默", "relation": "竞争对手"}],
        }]
    }, ensure_ascii=False)
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(replaced))

    resp = await client.post(
        f"/api/projects/{project['id']}/script/characters/{cid_xiaoman}/regenerate",
        headers=auth["headers"],
        json={"instruction": "更成熟一些"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["character"]["name"] == "林小满·重塑"
    assert body["character"]["candidate_id"] == cid_xiaoman  # 保留原候选 id

    # 仅目标候选被替换；另一条（陈默）原样不动
    config = await _fetch_project_config(db, project["id"])
    stage = config["script_characters"]
    assert len(stage) == 2
    updated = next(c for c in stage if c["id"] == cid_xiaoman)
    assert updated["name"] == "林小满·重塑"
    assert updated["personality"] == "更加果敢"
    untouched = next(c for c in stage if c["id"] != cid_xiaoman)
    assert untouched["name"] == "陈默"
    assert untouched["personality"] == "沉稳寡言，内心炽热"

    # 未知 cid：非数字串 → 404
    missing = await client.post(
        f"/api/projects/{project['id']}/script/characters/nonexistent/regenerate",
        headers=auth["headers"],
    )
    assert missing.status_code == 404, missing.text


# ---------------------------------------------------------------------------
# 5. confirm 确认入池
# ---------------------------------------------------------------------------
async def test_confirm_and_idempotent(client, auth, db, monkeypatch):
    """confirm：characters 表 2 行（人设字段非空、confirmed_at 非空、
    relations JSONB 正确、source=script_planner）、version_snapshots 有
    characters_confirmed、phase='outline'；重复 confirm 幂等不重复建行。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直接查表断言）。
        monkeypatch: 替换 _llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)
    await _generate_candidates(client, auth, project["id"], monkeypatch)

    # 确认清单：林小满带编辑后的人设；陈默全空（验证缺省沿用候选值）
    confirm_body = {
        "characters": [
            {
                "name": "林小满",
                "role_type": "lead",
                "age": "约17岁",
                "personality": "倔强善良，遇挫不折",
                "appearance": "短发少女，戴红色发卡",
                "motivation": "和挚友一起成为漫画家",
                "arc": "从畏缩退让到坚定追梦",
                "relations": [{"target": "陈默", "relation": "漫画搭档"}],
            },
            {"name": "陈默"},
        ]
    }
    resp = await client.post(
        f"/api/projects/{project['id']}/script/characters/confirm",
        headers=auth["headers"],
        json=confirm_body,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["confirmed_count"] == 2
    assert body["phase"] == "outline"

    # characters 表：2 行，人设字段/confirmed_at/source 落值正确
    rows = (
        await db.execute(
            select(Character)
            .where(Character.project_id == project["id"])
            .order_by(Character.id)
        )
    ).scalars().all()
    assert len(rows) == 2
    by_name = {r.name: r for r in rows}
    xiaoman = by_name["林小满"]
    chenmo = by_name["陈默"]
    # 林小满：人设字段非空 + relations JSONB 正确 + visual_prompt 用模板
    for field in ("role_type", "age", "personality", "motivation", "arc",
                  "description", "visual_prompt"):
        assert getattr(xiaoman, field), f"林小满.{field} 应非空"
    assert xiaoman.role_type == "lead"
    assert xiaoman.relations == [{"target": "陈默", "relation": "漫画搭档"}]
    assert xiaoman.confirmed_at is not None
    assert xiaoman.source == "script_planner"
    assert "统一风格插画" in xiaoman.visual_prompt and "林小满" in xiaoman.visual_prompt
    assert "性格：" in xiaoman.description
    # 陈默：None 字段沿用候选值（mock 候选的人设）
    assert chenmo.role_type == "support"
    assert chenmo.personality == "沉稳寡言，内心炽热"
    assert chenmo.confirmed_at is not None

    # 里程碑快照：characters_confirmed 已写入（summary 带数量与名单）
    snapshots = (
        await db.execute(
            select(VersionSnapshot).where(
                VersionSnapshot.project_id == project["id"],
                VersionSnapshot.stage == "characters_confirmed",
            )
        )
    ).scalars().all()
    assert len(snapshots) >= 1
    assert snapshots[0].summary["confirmed_count"] == 2
    assert set(snapshots[0].summary["names"]) == {"林小满", "陈默"}

    # config 候选回写 confirmed=True
    config = await _fetch_project_config(db, project["id"])
    assert all(item["confirmed"] is True for item in config["script_characters"])

    # 重复 confirm：幂等——仍 200、仍 2 行、phase 保持 outline
    again = await client.post(
        f"/api/projects/{project['id']}/script/characters/confirm",
        headers=auth["headers"],
        json=confirm_body,
    )
    assert again.status_code == 200, again.text
    assert again.json()["phase"] == "outline"
    rows_again = (
        await db.execute(
            select(Character).where(Character.project_id == project["id"])
        )
    ).scalars().all()
    assert len(rows_again) == 2


async def test_confirm_without_candidates_400(client, auth):
    """confirm 无候选：项目从未生成候选时 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 400 时中断。
    """
    project = await _make_project_with_input(client, auth)
    resp = await client.post(
        f"/api/projects/{project['id']}/script/characters/confirm",
        headers=auth["headers"],
        json={"characters": [{"name": "无中生有"}]},
    )
    assert resp.status_code == 400, resp.text
    assert "生成角色" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 6. 越权
# ---------------------------------------------------------------------------
async def test_forbidden_access_404(client, auth, monkeypatch):
    """越权：用户 B 访问用户 A 的项目（generate / GET / PUT）一律 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 用户 A（项目归属者）。
        monkeypatch: 替换 _llm_chat（保证即便通过归属校验也不会真调 LLM）。

    Returns:
        None。

    Raises:
        AssertionError: 任一状态码非 404 时中断。
    """
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_two_characters_payload()))
    project = await _make_project_with_input(client, auth)

    # 用户 B 注册并登录
    other = await register_user(client, prefix="other")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    gen = await client.post(
        f"/api/projects/{project['id']}/script/characters/generate",
        headers=other_headers,
    )
    assert gen.status_code == 404, gen.text
    got = await client.get(
        f"/api/projects/{project['id']}/script/characters", headers=other_headers)
    assert got.status_code == 404, got.text
    put = await client.put(
        f"/api/projects/{project['id']}/script/characters",
        headers=other_headers,
        json={"upsert": [{"name": "越权者"}]},
    )
    assert put.status_code == 404, put.text
    regen = await client.post(
        f"/api/projects/{project['id']}/script/characters/deadbeef/regenerate",
        headers=other_headers,
    )
    assert regen.status_code == 404, regen.text
    confirm = await client.post(
        f"/api/projects/{project['id']}/script/characters/confirm",
        headers=other_headers,
        json={"characters": [{"name": "越权者"}]},
    )
    assert confirm.status_code == 404, confirm.text


# ---------------------------------------------------------------------------
# 7. 解析容错
# ---------------------------------------------------------------------------
async def test_parse_tolerant(client, auth, monkeypatch):
    """解析容错：mock 返回围栏包裹 JSON（含一个无名条目与一个白名单外
    role_type 条目）→ 规整后仅剩合法角色，白名单外 role_type 归 support。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 替换 _llm_chat 为围栏 + 脏数据输出。

    Returns:
        None。

    Raises:
        AssertionError: 规整结果不符时中断。
    """
    fenced = (
        "```json\n"
        + json.dumps({
            "characters": [
                {"name": "林小满", "role_type": "lead", "age": "约17岁",
                 "personality": "倔强善良", "relations": [{"target": "陈默", "relation": "搭档"}]},
                {"role_type": "lead", "age": "40"},  # 无 name：应被丢弃
                {"name": "路人甲", "role_type": "boss"},  # 白名单外：应归 support
            ]
        }, ensure_ascii=False)
        + "\n以上为角色设定，请查收。\n```"  # 围栏内尾随杂文本（不含大括号）
    )
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(fenced))
    project = await _make_project_with_input(client, auth)

    resp = await client.post(
        f"/api/projects/{project['id']}/script/characters/generate",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    characters = resp.json()["characters"]
    # 无名条目被丢弃：仅剩 2 个合法角色
    assert [c["name"] for c in characters] == ["林小满", "路人甲"]
    # 白名单外 role_type 归 support
    assert characters[1]["role_type"] == "support"
    assert characters[0]["role_type"] == "lead"
    # 关系数组规整保留
    assert characters[0]["relations"] == [{"target": "陈默", "relation": "搭档"}]


# ---------------------------------------------------------------------------
# 8. generate 失败重试与 502
# ---------------------------------------------------------------------------
async def test_generate_retries_then_502(client, auth, db, monkeypatch):
    """generate 失败重试：LLM 恒返回非法 JSON → 恰好 3 次尝试（0.8/1.0/1.0）
    后 502，config 不写入候选。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言 config 未写入候选）。
        monkeypatch: 替换 _llm_chat 为恒返非 JSON 文本。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 502 或重试次数不符时中断。
    """
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat("这不是 JSON", calls))
    project = await _make_project_with_input(client, auth)

    resp = await client.post(
        f"/api/projects/{project['id']}/script/characters/generate",
        headers=auth["headers"],
    )
    assert resp.status_code == 502, resp.text
    assert "角色生成失败" in resp.json()["detail"]
    # 共 3 次尝试：温度 0.8 → 1.0 → 1.0
    assert [c["temperature"] for c in calls] == [0.8, 1.0, 1.0]
    # 失败不落候选、阶段不推进
    config = await _fetch_project_config(db, project["id"])
    assert "script_characters" not in config
    proj_row = (
        await db.execute(select(Project).where(Project.id == project["id"]))
    ).scalar_one()
    assert proj_row.script_phase is None
