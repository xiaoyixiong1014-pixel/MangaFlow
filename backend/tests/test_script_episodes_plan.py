"""P-3 分集大纲阶段接口测试模块（文件级说明，P-1 范式级重构 Task 4）。

覆盖链路（全部 mock LLM：monkeypatch app.agents.script_pipeline 模块级
_llm_chat 包装函数，返回预置合法 JSON 字符串；角色生成与大纲生成两段
LLM 均走同一挂点，按阶段先后替换不同 payload）：
- 前置：走真实角色接口链（mock 角色 LLM）生成并确认 2 角色 → phase=outline；
- plan 生成成功：episodes 表落 2 行（title/logline 列 + outline JSONB）、
  GET 返回 2 集有序（sequence 1..n）、phase=outline、agent_name=episode_planner；
- 未确认角色时 plan：409（提示先确认角色设定）；
- plan 重生成缩为 1 集：多余行硬删、sequence 重写连续；
- PUT 单集编辑：outline JSONB 更新（core_event/emotion_curve）、
  sequence 互换成功、未知 eid 404；
- confirm：全部 confirmed_at 非空、写 outline_confirmed 快照、phase=body；
  重复 confirm 幂等；
- body 阶段：plan 重生成 409（大纲锁定）、sequence 重排 409、文案微调放行；
- 越权：用户 B 访问用户 A 的项目一律 404。
"""

import json
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select

from app.agents import script_pipeline as sp_mod
from app.models import Episode, VersionSnapshot
from tests.conftest import create_project, register_user

# 保存输入时的原始文本（idea 模式一句话创意）
_SAMPLE_RAW = "一句话创意：两个少女约定一起成为漫画家的故事。"


def _two_characters_payload() -> str:
    """构造含 2 个角色的合法 LLM 输出 JSON 字符串（内部函数，同 Task 3）。

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


def _episodes_payload(count: int = 2) -> str:
    """构造含指定集数的合法分集大纲 LLM 输出 JSON 字符串（内部函数）。

    模板集：第 1 集天台立约（双主角），第 2 集初稿风波（单主角）；
    请求集数多于模板时按「第三集起」自动补位（角色名取自已确认名单）。

    Args:
        count: 输出集数（1~N）。

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


def _mock_llm_chat(payload: str, calls: Optional[List[Dict[str, Any]]] = None):
    """构造可替换 script_pipeline._llm_chat 的异步假函数（内部工厂）。

    Args:
        payload: 每次 LLM 调用返回的固定文本。
        calls: 可选收集器列表；传入时每次调用追加
            {"temperature": 温度, "agent_name": Agent 标识} 记录供断言。

    Returns:
        Callable: 签名兼容 _llm_chat(messages, *, temperature, agent_name)
            的异步函数（**kwargs 兼容 agent_name 关键字）。

    Raises:
        无。
    """

    async def fake_chat(messages, *, temperature, **kwargs):
        """替换 _llm_chat：恒返回预置文本（内部函数）。

        Args:
            messages: OpenAI 格式消息列表（忽略）。
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
    project = await create_project(client, auth["headers"], title="分集大纲项目")
    resp = await client.put(
        f"/api/projects/{project['id']}/script/input",
        headers=auth["headers"],
        json={"mode": "idea", "raw_text": _SAMPLE_RAW},
    )
    assert resp.status_code == 200, resp.text
    return project


async def _confirm_two_characters(client: httpx.AsyncClient, auth: Dict[str, Any],
                                  project_id: int, monkeypatch=None) -> dict:
    """辅助：mock 角色 LLM 后走真实链路生成并确认 2 个角色（内部函数）。

    链路：POST characters/generate（mock 2 角色）→ POST characters/confirm
    （确认清单仅传 name，人设字段缺省沿用候选值）→ phase 推进到 outline。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        project_id: 项目 ID。
        monkeypatch: pytest 夹具；传入时经其替换 _llm_chat（用例结束自动还原）。

    Returns:
        dict: confirm 响应 JSON（{"confirmed_count": 2, "phase": "outline"}）。

    Raises:
        AssertionError: 任一接口返回非 200 或阶段未达 outline 时中断。
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
    body = confirm.json()
    assert body["phase"] == "outline", confirm.text
    return body


async def _plan_episodes(client: httpx.AsyncClient, auth: Dict[str, Any],
                         project_id: int, count: int = 2,
                         monkeypatch=None) -> dict:
    """辅助：mock 大纲 LLM 后调用 POST episodes/plan 并断言 200（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        project_id: 项目 ID。
        count: mock 返回的目标集数。
        monkeypatch: pytest 夹具；传入时经其替换 _llm_chat（用例结束自动还原）。

    Returns:
        dict: plan 响应 JSON（{"episodes": [...], "phase": ...}）。

    Raises:
        AssertionError: 接口返回非 200 时中断。
    """
    if monkeypatch is not None:
        monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_episodes_payload(count)))
    resp = await client.post(
        f"/api/projects/{project_id}/script/episodes/plan",
        headers=auth["headers"],
        json={"target_episodes": count},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 1. plan 生成成功
# ---------------------------------------------------------------------------
async def test_plan_success(client, auth, db, monkeypatch):
    """plan 生成成功：episodes 落 2 行（logline/outline 正确）、GET 返回
    2 集有序、phase=outline、LLM 以 agent_name=episode_planner 调用 1 次。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直接断言 episodes 落库）。
        monkeypatch: 依次替换 _llm_chat（角色生成/大纲生成）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)
    await _confirm_two_characters(client, auth, project["id"], monkeypatch)

    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_episodes_payload(2), calls))
    resp = await client.post(
        f"/api/projects/{project['id']}/script/episodes/plan",
        headers=auth["headers"],
        json={"target_episodes": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 响应：2 集、sequence 有序、phase=outline、未确认
    assert body["phase"] == "outline"
    assert len(body["episodes"]) == 2
    assert [e["sequence"] for e in body["episodes"]] == [1, 2]
    first = body["episodes"][0]
    assert first["title"] == "第一集：天台之约"
    assert first["logline"] == "两个少女在天台相遇并约定一起成为漫画家"
    assert first["core_event"] == "天台立约，共同报名新人奖"
    assert first["characters"] == ["林小满", "陈默"]
    assert first["emotion_curve"] == "平静→相遇→热血"
    assert first["duration_sec"] == 90
    assert first["confirmed_at"] is None
    assert first["episode_id"] > 0

    # LLM 调用恰好 1 次（成功不重试），episode_planner / json_mode / 温度 0.8
    assert len(calls) == 1
    assert calls[0]["temperature"] == 0.8
    assert calls[0]["agent_name"] == "episode_planner"

    # 落库：episodes 表 2 行，logline 列与 outline JSONB 字段正确
    rows = (
        await db.execute(
            select(Episode)
            .where(Episode.project_id == project["id"])
            .order_by(Episode.sequence)
        )
    ).scalars().all()
    assert len(rows) == 2
    assert rows[0].logline == "两个少女在天台相遇并约定一起成为漫画家"
    assert rows[0].outline == {
        "core_event": "天台立约，共同报名新人奖",
        "characters": ["林小满", "陈默"],
        "emotion_curve": "平静→相遇→热血",
        "duration_sec": 90,
    }
    assert rows[1].outline["duration_sec"] == 120
    assert rows[0].confirmed_at is None and rows[1].confirmed_at is None

    # GET 返回 2 集有序
    got = await client.get(
        f"/api/projects/{project['id']}/script/episodes/plan",
        headers=auth["headers"],
    )
    assert got.status_code == 200, got.text
    got_body = got.json()
    assert got_body["phase"] == "outline"
    assert [e["sequence"] for e in got_body["episodes"]] == [1, 2]
    assert [e["title"] for e in got_body["episodes"]] == [
        "第一集：天台之约", "第二集：初稿风波"]


# ---------------------------------------------------------------------------
# 2. 未确认角色时 plan → 409
# ---------------------------------------------------------------------------
async def test_plan_without_confirmed_characters_409(client, auth):
    """plan 前置校验：项目无任何已确认角色时 409，detail 提示先确认角色。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 409 时中断。
    """
    project = await _make_project_with_input(client, auth)
    resp = await client.post(
        f"/api/projects/{project['id']}/script/episodes/plan",
        headers=auth["headers"],
        json={"target_episodes": 2},
    )
    assert resp.status_code == 409, resp.text
    assert "请先确认角色设定" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 3. plan 重生成缩为 1 集：多余行删除、sequence 连续
# ---------------------------------------------------------------------------
async def test_plan_regenerate_shrink(client, auth, db, monkeypatch):
    """plan 重生成：先生成 2 集再缩为 1 集 → 多余行硬删、sequence 重写为
    [1] 连续、保留行内容被新大纲覆盖。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言 episodes 行数与 sequence）。
        monkeypatch: 依次替换 _llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)
    await _confirm_two_characters(client, auth, project["id"], monkeypatch)

    first = await _plan_episodes(client, auth, project["id"], count=2, monkeypatch=monkeypatch)
    old_ids = [e["episode_id"] for e in first["episodes"]]
    assert len(old_ids) == 2

    # 重生成：缩为 1 集（mock 返回单集）
    shrunk = await _plan_episodes(client, auth, project["id"], count=1, monkeypatch=monkeypatch)
    assert len(shrunk["episodes"]) == 1
    assert shrunk["episodes"][0]["sequence"] == 1
    assert shrunk["episodes"][0]["title"] == "第一集：天台之约"

    # 落库：仅剩 1 行（原第 1 行复用被覆盖），sequence 连续为 [1]
    rows = (
        await db.execute(
            select(Episode)
            .where(Episode.project_id == project["id"])
            .order_by(Episode.sequence)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == old_ids[0]
    assert rows[0].sequence == 1
    assert rows[0].outline["core_event"] == "天台立约，共同报名新人奖"

    # GET 同步为 1 集
    got = await client.get(
        f"/api/projects/{project['id']}/script/episodes/plan",
        headers=auth["headers"],
    )
    assert got.status_code == 200, got.text
    assert [e["sequence"] for e in got.json()["episodes"]] == [1]


# ---------------------------------------------------------------------------
# 4. PUT 单集编辑：outline 更新 / sequence 互换 / 未知 eid 404
# ---------------------------------------------------------------------------
async def test_edit_outline_and_swap_sequence(client, auth, db, monkeypatch):
    """PUT 单集编辑：改 core_event/emotion_curve → outline JSONB 更新且
    其余字段保留；sequence 互换成功（GET 顺序变化）；未知 eid → 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言 outline JSONB 落库）。
        monkeypatch: 依次替换 _llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)
    await _confirm_two_characters(client, auth, project["id"], monkeypatch)
    planned = await _plan_episodes(client, auth, project["id"], count=2, monkeypatch=monkeypatch)
    eid1, eid2 = planned["episodes"][0]["episode_id"], planned["episodes"][1]["episode_id"]

    # sequence 互换：第 1 集与第 2 集交换（须在保存大纲自动推进 body 之前——
    # 新语义：outline 阶段保存大纲即推进 body，body 阶段禁止重排）
    swap = await client.put(
        f"/api/projects/{project['id']}/script/episodes/{eid1}/outline",
        headers=auth["headers"],
        json={"sequence": 2},
    )
    assert swap.status_code == 200, swap.text
    assert swap.json()["sequence"] == 2

    # 编辑第 1 集的大纲字段（其余字段保留）；保存自动推进 phase outline→body
    resp = await client.put(
        f"/api/projects/{project['id']}/script/episodes/{eid1}/outline",
        headers=auth["headers"],
        json={"core_event": "改为深夜天台重立誓约", "emotion_curve": "压抑→顿悟→燃起"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["episode_id"] == eid1
    assert body["core_event"] == "改为深夜天台重立誓约"
    assert body["emotion_curve"] == "压抑→顿悟→燃起"
    assert body["title"] == "第一集：天台之约"  # 未传字段保持
    assert body["duration_sec"] == 90
    assert body["phase"] == "body"  # 保存即定稿：自动推进并回执最新阶段

    # outline JSONB 落库：仅覆盖传入字段
    row = await db.get(Episode, eid1)
    assert row.outline["core_event"] == "改为深夜天台重立誓约"
    assert row.outline["emotion_curve"] == "压抑→顿悟→燃起"
    assert row.outline["characters"] == ["林小满", "陈默"]
    assert row.outline["duration_sec"] == 90
    got = await client.get(
        f"/api/projects/{project['id']}/script/episodes/plan",
        headers=auth["headers"],
    )
    order = {e["episode_id"]: e["sequence"] for e in got.json()["episodes"]}
    assert order == {eid1: 2, eid2: 1}  # 互换成功

    # 未知 eid → 404
    missing = await client.put(
        f"/api/projects/{project['id']}/script/episodes/999999/outline",
        headers=auth["headers"],
        json={"title": "不存在的集"},
    )
    assert missing.status_code == 404, missing.text


# ---------------------------------------------------------------------------
# 5. confirm：确认 + 快照 + 推进 + 幂等
# ---------------------------------------------------------------------------
async def test_confirm_and_idempotent(client, auth, db, monkeypatch):
    """confirm：全部 episodes.confirmed_at 非空、写 outline_confirmed 快照
    （summary 带集数与标题）、phase 推进到 body；重复 confirm 幂等。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直接查表断言）。
        monkeypatch: 依次替换 _llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)
    await _confirm_two_characters(client, auth, project["id"], monkeypatch)
    await _plan_episodes(client, auth, project["id"], count=2, monkeypatch=monkeypatch)

    resp = await client.post(
        f"/api/projects/{project['id']}/script/episodes/plan/confirm",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["confirmed_count"] == 2
    assert body["phase"] == "body"

    # 落库：全部 confirmed_at 非空
    rows = (
        await db.execute(
            select(Episode)
            .where(Episode.project_id == project["id"])
            .order_by(Episode.sequence)
        )
    ).scalars().all()
    assert len(rows) == 2
    assert all(r.confirmed_at is not None for r in rows)

    # 快照：outline_confirmed 已写入（summary 带集数与标题）
    snapshots = (
        await db.execute(
            select(VersionSnapshot).where(
                VersionSnapshot.project_id == project["id"],
                VersionSnapshot.stage == "outline_confirmed",
            )
        )
    ).scalars().all()
    assert len(snapshots) >= 1
    assert snapshots[0].summary["episode_count"] == 2
    assert set(snapshots[0].summary["titles"]) == {
        "第一集：天台之约", "第二集：初稿风波"}

    # 重复 confirm：幂等——仍 200、confirmed_count/phase 不变、confirmed_at 保持
    again = await client.post(
        f"/api/projects/{project['id']}/script/episodes/plan/confirm",
        headers=auth["headers"],
    )
    assert again.status_code == 200, again.text
    assert again.json() == {"confirmed_count": 2, "phase": "body"}
    rows_again = (
        await db.execute(select(Episode).where(Episode.project_id == project["id"]))
    ).scalars().all()
    assert len(rows_again) == 2
    assert all(r.confirmed_at is not None for r in rows_again)


async def test_confirm_without_episodes_400(client, auth):
    """confirm 无大纲：项目从未生成任何剧集时 400。

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
        f"/api/projects/{project['id']}/script/episodes/plan/confirm",
        headers=auth["headers"],
    )
    assert resp.status_code == 400, resp.text
    assert "请先生成" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 6. body 阶段：plan 重生成 409 / sequence 重排 409 / 文案微调放行
# ---------------------------------------------------------------------------
async def test_plan_locked_in_body_phase(client, auth, db, monkeypatch):
    """body 阶段锁定：plan 重生成 409（大纲已锁定）；PUT sequence 重排 409；
    PUT 文案微调（core_event）放行——保持已确认语义但允许编辑文案。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言微调落库）。
        monkeypatch: 依次替换 _llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)
    await _confirm_two_characters(client, auth, project["id"], monkeypatch)
    planned = await _plan_episodes(client, auth, project["id"], count=2, monkeypatch=monkeypatch)
    eid1 = planned["episodes"][0]["episode_id"]

    # confirm 后 phase=body
    confirm = await client.post(
        f"/api/projects/{project['id']}/script/episodes/plan/confirm",
        headers=auth["headers"],
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["phase"] == "body"

    # body 阶段 plan 重生成 → 409（大纲已锁定）
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_episodes_payload(2)))
    regen = await client.post(
        f"/api/projects/{project['id']}/script/episodes/plan",
        headers=auth["headers"],
        json={"target_episodes": 2},
    )
    assert regen.status_code == 409, regen.text
    assert "正文已开始生成" in regen.json()["detail"]

    # body 阶段 sequence 重排 → 409
    reorder = await client.put(
        f"/api/projects/{project['id']}/script/episodes/{eid1}/outline",
        headers=auth["headers"],
        json={"sequence": 2},
    )
    assert reorder.status_code == 409, reorder.text
    assert "禁止调整分集顺序" in reorder.json()["detail"]

    # body 阶段文案微调 → 200（outline 字段仍可编辑）
    tweak = await client.put(
        f"/api/projects/{project['id']}/script/episodes/{eid1}/outline",
        headers=auth["headers"],
        json={"core_event": "确认后的微调版本"},
    )
    assert tweak.status_code == 200, tweak.text
    assert tweak.json()["core_event"] == "确认后的微调版本"


# ---------------------------------------------------------------------------
# 7. 越权
# ---------------------------------------------------------------------------
async def test_forbidden_access_404(client, auth, monkeypatch):
    """越权：用户 B 访问用户 A 的项目（plan / GET / PUT / confirm）一律 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 用户 A（项目归属者）。
        monkeypatch: 替换 _llm_chat（保证即便通过归属校验也不会真调 LLM）。

    Returns:
        None。

    Raises:
        AssertionError: 任一状态码非 404 时中断。
    """
    project = await _make_project_with_input(client, auth)
    await _confirm_two_characters(client, auth, project["id"], monkeypatch)
    planned = await _plan_episodes(client, auth, project["id"], count=2, monkeypatch=monkeypatch)
    eid1 = planned["episodes"][0]["episode_id"]

    # 用户 B 注册并登录
    other = await register_user(client, prefix="other")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    plan = await client.post(
        f"/api/projects/{project['id']}/script/episodes/plan",
        headers=other_headers,
        json={"target_episodes": 2},
    )
    assert plan.status_code == 404, plan.text
    got = await client.get(
        f"/api/projects/{project['id']}/script/episodes/plan",
        headers=other_headers,
    )
    assert got.status_code == 404, got.text
    put = await client.put(
        f"/api/projects/{project['id']}/script/episodes/{eid1}/outline",
        headers=other_headers,
        json={"title": "越权改标题"},
    )
    assert put.status_code == 404, put.text
    confirm = await client.post(
        f"/api/projects/{project['id']}/script/episodes/plan/confirm",
        headers=other_headers,
    )
    assert confirm.status_code == 404, confirm.text
