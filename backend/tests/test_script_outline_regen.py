"""P3 单集大纲重生成接口测试模块（文件级说明，redesign-script-workspace Task 4）。

覆盖链路（全部 mock LLM：monkeypatch app.agents.script_pipeline 模块级
_llm_chat 包装函数，返回预置合法 JSON 字符串；角色生成 / 整卷 plan / 单集
重生成三段 LLM 均走同一挂点，按调用先后替换不同 payload）：
- 前置：走真实角色接口链（mock 角色 LLM）生成并确认 2 角色 → phase=outline，
  再 mock 整卷 plan 生成 2 集；
- 单集重生成仅影响目标集：eid1 重写为「第一集：重写版」，eid2 内容不变、
  phase 保持 outline、写 episode_outline_regenerated 快照（summary 带
  episode_id/sequence/title/instruction）；
- instruction 透传进 prompt（mock 断言 messages：system 含「只重写第 1 集」、
  user 含现有大纲与前后集钩子上下文、用户调整要求原文）；
- 正文存在提醒：scripts 行存在时 body_exists=true 且正文行不被删除；
  无正文集 body_exists=false；
- 守卫：越权（用户 B 访问用户 A 项目）404、非本项目集（跨项目 eid）404、
  该集大纲未生成 409；
- 阶段锁定口径：body 阶段整卷 plan 重生成 409 而单集重生成放行 200；
- LLM 失败：3 次尝试均解析失败 → 502。
"""

import json
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select

from app.agents import script_pipeline as sp_mod
from app.models import Episode, Script, VersionSnapshot
from tests.conftest import create_project, register_user

# 保存输入时的原始文本（idea 模式一句话创意，与分集大纲测试同款）
_SAMPLE_RAW = "一句话创意：两个少女约定一起成为漫画家的故事。"


def _two_characters_payload() -> str:
    """构造含 2 个角色的合法角色 LLM 输出 JSON 字符串（内部函数，同大纲域测试）。

    Returns:
        str: {"characters": [林小满(lead), 陈默(support)]} 的 JSON 文本。

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
                "relations": [],
            },
            {
                "name": "陈默",
                "role_type": "support",
                "age": "约18岁",
                "personality": "沉稳寡言，内心炽热",
                "appearance": "高瘦少年，黑框眼镜",
                "motivation": "守护小满的梦想",
                "arc": "从旁观者到并肩者",
                "relations": [],
            },
        ]
    }, ensure_ascii=False)


def _episodes_payload(count: int = 2) -> str:
    """构造整卷分集大纲 LLM 输出 JSON 字符串（内部函数，与大纲域测试同模板）。

    Args:
        count: 输出集数（1~N；多于模板时按「第 N 集起」补位）。

    Returns:
        str: {"episodes": [...]} 的 JSON 文本（恰好 count 集）。

    Raises:
        无：纯结构构造。
    """
    templates = [
        {
            "title": "第一集：天台之约",
            "logline": "两个少女在天台相遇并约定一起成为漫画家",
            "core_event": "天台立约，共同报名新人奖",
            "characters": ["林小满", "陈默"],
            "emotion_curve": "平静→相遇→热血",
            "duration_sec": 90,
        },
        {
            "title": "第二集：初稿风波",
            "logline": "初稿被毙，两人陷入低谷后互相打气",
            "core_event": "初稿被评审否决，深夜天台和解",
            "characters": ["林小满"],
            "emotion_curve": "压抑→爆发→释然",
            "duration_sec": 120,
        },
    ]
    episodes = []
    for idx in range(count):
        if idx < len(templates):
            episodes.append(dict(templates[idx]))
        else:
            episodes.append({
                "title": f"第{idx + 1}集：新的考验",
                "logline": f"第{idx + 1}集的连续剧情推进",
                "core_event": f"第{idx + 1}集核心事件",
                "characters": ["陈默"],
                "emotion_curve": "平稳→波折→收束",
                "duration_sec": 100,
            })
    return json.dumps({"episodes": episodes}, ensure_ascii=False)


def _regen_payload() -> str:
    """构造单集大纲重生成 LLM 输出 JSON 字符串（内部函数，恰好 1 条）。

    Returns:
        str: {"episodes": [第一集：重写版…]} 的 JSON 文本（角色名取自已确认名单）。

    Raises:
        无：纯结构构造。
    """
    return json.dumps({"episodes": [{
        "title": "第一集：重写版",
        "logline": "重写后的第一集梗概",
        "core_event": "天台之约改为雨夜偶遇",
        "characters": ["林小满"],
        "emotion_curve": "压抑→反转→释然",
        "duration_sec": 150,
    }]}, ensure_ascii=False)


def _mock_llm_chat(payload: str, calls: Optional[List[Dict[str, Any]]] = None,
                   message_calls: Optional[List[List[dict]]] = None):
    """构造可替换 script_pipeline._llm_chat 的异步假函数（内部工厂）。

    Args:
        payload: 每次 LLM 调用返回的固定文本。
        calls: 可选收集器列表；传入时每次调用追加
            {"temperature": 温度, "agent_name": Agent 标识} 供断言。
        message_calls: 可选收集器列表；传入时每次调用追加完整 messages
            （system+user）供 prompt 内容断言。

    Returns:
        Callable: 签名兼容 _llm_chat(messages, *, temperature, agent_name)
            的异步函数。

    Raises:
        无。
    """

    async def fake_chat(messages, *, temperature, **kwargs):
        """替换 _llm_chat：恒返回预置文本（内部函数）。

        Args:
            messages: OpenAI 格式消息列表（记录进 message_calls）。
            temperature: 采样温度（记录进 calls）。
            **kwargs: 其余关键字参数（agent_name 等，记录进 calls）。

        Returns:
            str: 预置的 payload 文本。

        Raises:
            无。
        """
        if calls is not None:
            calls.append({"temperature": temperature,
                          "agent_name": kwargs.get("agent_name")})
        if message_calls is not None:
            message_calls.append(messages)
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
    project = await create_project(client, auth["headers"], title="单集重生成项目")
    resp = await client.put(
        f"/api/projects/{project['id']}/script/input",
        headers=auth["headers"],
        json={"mode": "idea", "raw_text": _SAMPLE_RAW},
    )
    assert resp.status_code == 200, resp.text
    return project


async def _setup_two_episodes(client: httpx.AsyncClient, auth: Dict[str, Any],
                              project_id: int, monkeypatch=None) -> Dict[int, int]:
    """辅助：mock 角色 LLM 与整卷大纲 LLM，构建「2 角色 + 2 集大纲」项目（内部函数）。

    链路：POST characters/generate（mock 2 角色）→ POST characters/confirm →
    POST episodes/plan（mock 2 集）→ 返回 {sequence: episode_id} 映射。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        project_id: 项目 ID。
        monkeypatch: pytest 夹具；传入时经其替换 _llm_chat。

    Returns:
        Dict[int, int]: 序号 → 剧集 ID 映射（{1: eid1, 2: eid2}）。

    Raises:
        AssertionError: 任一接口返回非 200 时中断。
    """
    if monkeypatch is not None:
        monkeypatch.setattr(sp_mod, "_llm_chat",
                            _mock_llm_chat(_two_characters_payload()))
    resp = await client.post(
        f"/api/projects/{project_id}/script/characters/generate",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    confirm = await client.post(
        f"/api/projects/{project_id}/script/characters/confirm",
        headers=auth["headers"],
        json={"characters": [{"name": "林小满"}, {"name": "陈默"}]},
    )
    assert confirm.status_code == 200, confirm.text
    if monkeypatch is not None:
        monkeypatch.setattr(sp_mod, "_llm_chat",
                            _mock_llm_chat(_episodes_payload(2)))
    plan = await client.post(
        f"/api/projects/{project_id}/script/episodes/plan",
        headers=auth["headers"],
        json={"target_episodes": 2},
    )
    assert plan.status_code == 200, plan.text
    return {e["sequence"]: e["episode_id"] for e in plan.json()["episodes"]}


# ---------------------------------------------------------------------------
# 1. 单集重生成仅影响目标集
# ---------------------------------------------------------------------------
async def test_regen_only_affects_target_episode(client, auth, db, monkeypatch):
    """单集重生成：eid1 重写为新大纲、eid2 内容不变、phase 保持 outline、
    写 episode_outline_regenerated 快照（summary 带 episode_id/sequence/title）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直接断言 episodes 落库与快照）。
        monkeypatch: 依次替换 _llm_chat（角色/整卷/单集三段）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)
    eids = await _setup_two_episodes(client, auth, project["id"], monkeypatch)
    eid1, eid2 = eids[1], eids[2]

    # 单集重生成第 1 集（无 instruction）
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_regen_payload()))
    resp = await client.post(
        f"/api/projects/{project['id']}/script/episodes/{eid1}/outline/regenerate",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["episode_id"] == eid1
    assert body["title"] == "第一集：重写版"
    assert body["logline"] == "重写后的第一集梗概"
    assert body["core_event"] == "天台之约改为雨夜偶遇"
    assert body["characters"] == ["林小满"]
    assert body["emotion_curve"] == "压抑→反转→释然"
    assert body["duration_sec"] == 150
    assert body["sequence"] == 1
    assert body["body_exists"] is False

    # 落库：目标集 outline 已替换、其余集不动、阶段保持 outline
    row1 = await db.get(Episode, eid1)
    row2 = await db.get(Episode, eid2)
    assert row1.title == "第一集：重写版"
    assert row1.logline == "重写后的第一集梗概"
    assert row1.outline == {
        "core_event": "天台之约改为雨夜偶遇",
        "characters": ["林小满"],
        "emotion_curve": "压抑→反转→释然",
        "duration_sec": 150,
    }
    assert row2.title == "第二集：初稿风波"
    assert row2.outline["core_event"] == "初稿被评审否决，深夜天台和解"

    got = await client.get(
        f"/api/projects/{project['id']}/script/episodes/plan",
        headers=auth["headers"],
    )
    assert got.status_code == 200, got.text
    got_body = got.json()
    assert got_body["phase"] == "outline"
    assert [e["title"] for e in got_body["episodes"]] == [
        "第一集：重写版", "第二集：初稿风波"]

    # 快照留痕：episode_outline_regenerated 写入（summary 带 episode_id 等）
    snapshots = (
        await db.execute(
            select(VersionSnapshot).where(
                VersionSnapshot.project_id == project["id"],
                VersionSnapshot.stage == "episode_outline_regenerated",
            )
        )
    ).scalars().all()
    assert len(snapshots) == 1
    assert snapshots[0].summary["episode_id"] == eid1
    assert snapshots[0].summary["sequence"] == 1
    assert snapshots[0].summary["title"] == "第一集：重写版"
    assert snapshots[0].summary["instruction"] is None


# ---------------------------------------------------------------------------
# 2. instruction 透传进 prompt（mock 断言）
# ---------------------------------------------------------------------------
async def test_instruction_passed_to_prompt(client, auth, monkeypatch):
    """instruction 透传：mock 捕获 messages，断言 system 含「只重写第 N 集」、
    user 含该集现有大纲 / 前后集钩子上下文 / 用户调整要求原文。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 替换 _llm_chat 并捕获 messages。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)
    eids = await _setup_two_episodes(client, auth, project["id"], monkeypatch)
    eid1 = eids[1]

    message_calls: List[List[dict]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat",
                        _mock_llm_chat(_regen_payload(), message_calls=message_calls))
    resp = await client.post(
        f"/api/projects/{project['id']}/script/episodes/{eid1}/outline/regenerate",
        headers=auth["headers"],
        json={"instruction": "把冲突提前到开场"},
    )
    assert resp.status_code == 200, resp.text

    # 恰好 1 次 LLM 调用（成功不重试），system/user 各一条
    assert len(message_calls) == 1
    system_text = message_calls[0][0]["content"]
    user_text = message_calls[0][1]["content"]
    # system：作用域收窄为第 1 集
    assert "只重写第 1 集" in system_text
    # user：该集现有大纲上下文
    assert "第一集：天台之约" in user_text
    assert "天台立约，共同报名新人奖" in user_text
    # user：前后集钩子上下文（第 1 集有后一集、无前一集）
    assert "后一集（第 2 集）" in user_text
    assert "前一集" not in user_text
    # user：已确认角色强约束 + 用户调整要求原文
    assert "已确认角色名单" in user_text
    assert "把冲突提前到开场" in user_text


# ---------------------------------------------------------------------------
# 3. 正文存在提醒 body_exists
# ---------------------------------------------------------------------------
async def test_body_exists_flag(client, auth, db, monkeypatch):
    """body_exists 提醒：该集已有 scripts 行时重生成响应 body_exists=true 且
    正文行保留（不自动删正文）；无正文集 body_exists=false。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（插入 scripts 行并断言其保留）。
        monkeypatch: 依次替换 _llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)
    eids = await _setup_two_episodes(client, auth, project["id"], monkeypatch)
    eid1, eid2 = eids[1], eids[2]

    # 直接插入第 2 集正文行（绕过 SSE 生成链路，聚焦 body_exists 语义）
    db.add(Script(project_id=project["id"], episode_id=eid2,
                  content={}, raw_text="第二集已有正文", version=1))
    await db.commit()

    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_regen_payload()))
    # 第 2 集：已有正文 → body_exists=true
    resp2 = await client.post(
        f"/api/projects/{project['id']}/script/episodes/{eid2}/outline/regenerate",
        headers=auth["headers"],
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["body_exists"] is True
    # 正文行未被删除、内容未被改动
    row = (
        await db.execute(
            select(Script).where(
                Script.project_id == project["id"], Script.episode_id == eid2)
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.raw_text == "第二集已有正文"

    # 第 1 集：无正文 → body_exists=false
    resp1 = await client.post(
        f"/api/projects/{project['id']}/script/episodes/{eid1}/outline/regenerate",
        headers=auth["headers"],
    )
    assert resp1.status_code == 200, resp1.text
    assert resp1.json()["body_exists"] is False


# ---------------------------------------------------------------------------
# 4. 越权 / 非本项目集 404
# ---------------------------------------------------------------------------
async def test_forbidden_and_cross_project_404(client, auth, db, monkeypatch):
    """守卫：用户 B 访问用户 A 项目 404；eid 属于同用户另一项目时 404
    （不泄露存在性）；LLM mock 保证即便通过守卫也不会真调 LLM。

    Args:
        client: 内存 HTTP 客户端。
        auth: 用户 A（项目归属者）。
        db: 测试库会话（插入跨项目剧集行）。
        monkeypatch: 替换 _llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 任一状态码非 404 时中断。
    """
    project = await _make_project_with_input(client, auth)
    eids = await _setup_two_episodes(client, auth, project["id"], monkeypatch)
    eid1 = eids[1]

    # 用户 B 注册并访问用户 A 的项目 → 404
    other = await register_user(client, prefix="other")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    forbidden = await client.post(
        f"/api/projects/{project['id']}/script/episodes/{eid1}/outline/regenerate",
        headers=other_headers,
    )
    assert forbidden.status_code == 404, forbidden.text

    # 用户 A 的另一个项目：直接插入 1 集（含大纲），拿其 eid 打到项目 1 → 404
    project2 = await create_project(client, auth["headers"], title="另一个项目")
    db.add(Episode(project_id=project2["id"], title="别家第 1 集", sequence=1,
                   outline={"core_event": "不属于项目 1 的大纲"}))
    await db.commit()
    foreign_eid = (
        await db.execute(
            select(Episode).where(Episode.project_id == project2["id"])
        )
    ).scalar_one()
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_regen_payload()))
    cross = await client.post(
        f"/api/projects/{project['id']}/script/episodes/{foreign_eid.id}/outline/regenerate",
        headers=auth["headers"],
    )
    assert cross.status_code == 404, cross.text


# ---------------------------------------------------------------------------
# 5. 阶段锁定口径 + 大纲未生成守卫
# ---------------------------------------------------------------------------
async def test_phase_lock_semantics(client, auth, db, monkeypatch):
    """阶段锁定口径：body 阶段整卷 plan 重生成 409（大纲锁定）而单集大纲
    重生成放行 200（单集保持可编辑口径）；outline 阶段下无大纲的新集 409。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（插入无大纲剧集行）。
        monkeypatch: 依次替换 _llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)
    eids = await _setup_two_episodes(client, auth, project["id"], monkeypatch)
    eid1 = eids[1]

    # outline 阶段插入第 3 集（无大纲 JSONB）→ 单集重生成 409「请先生成分集大纲」
    db.add(Episode(project_id=project["id"], title="第 3 集", sequence=3))
    await db.commit()
    bare_eid = (
        await db.execute(
            select(Episode).where(
                Episode.project_id == project["id"], Episode.sequence == 3)
        )
    ).scalar_one()
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_regen_payload()))
    bare = await client.post(
        f"/api/projects/{project['id']}/script/episodes/{bare_eid.id}/outline/regenerate",
        headers=auth["headers"],
    )
    assert bare.status_code == 409, bare.text
    assert "请先生成分集大纲" in bare.json()["detail"]

    # 确认大纲推进 body；整卷 plan 重生成 409、单集大纲重生成 200（锁定口径）
    confirm = await client.post(
        f"/api/projects/{project['id']}/script/episodes/plan/confirm",
        headers=auth["headers"],
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["phase"] == "body"

    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_episodes_payload(2)))
    plan_locked = await client.post(
        f"/api/projects/{project['id']}/script/episodes/plan",
        headers=auth["headers"],
        json={"target_episodes": 2},
    )
    assert plan_locked.status_code == 409, plan_locked.text

    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_regen_payload()))
    regen = await client.post(
        f"/api/projects/{project['id']}/script/episodes/{eid1}/outline/regenerate",
        headers=auth["headers"],
    )
    assert regen.status_code == 200, regen.text
    assert regen.json()["episode_id"] == eid1
    assert regen.json()["title"] == "第一集：重写版"


# ---------------------------------------------------------------------------
# 6. LLM 失败 502
# ---------------------------------------------------------------------------
async def test_llm_failure_502(client, auth, monkeypatch):
    """LLM 失败：3 次尝试均返回非法 JSON → 502，detail 提示单集大纲重生成失败。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 替换 _llm_chat（恒返回非 JSON 文本）。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 502 时中断。
    """
    project = await _make_project_with_input(client, auth)
    eids = await _setup_two_episodes(client, auth, project["id"], monkeypatch)
    eid1 = eids[1]

    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat",
                        _mock_llm_chat("这不是JSON输出", calls=calls))
    resp = await client.post(
        f"/api/projects/{project['id']}/script/episodes/{eid1}/outline/regenerate",
        headers=auth["headers"],
    )
    assert resp.status_code == 502, resp.text
    assert "单集大纲重生成失败" in resp.json()["detail"]
    # 重试 3 次（温度 0.8 → 1.0 → 1.0）后放弃
    assert len(calls) == 3
