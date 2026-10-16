"""P-6 AI 优化四粒度 + diff 接受/拒绝接口测试模块（文件级说明，Task 7）。

覆盖链路（全部 mock LLM：正文流 monkeypatch 模块级 _llm_chat_stream、
其余结构化/优化 monkeypatch 模块级 _llm_chat）：
- 前置链：走真实接口链（mock 角色 LLM）生成并确认 2 角色 → mock 大纲 LLM
  生成 1 集并确认（phase=body）→ mock 正文流 + scenes 生成 2 场正文；
- optimize grain=segment：mock change（path=scenes[0].action）→ 返回 1 片段
  含 diff_lines（op 序列含 insert）、original 以当前库值覆盖、**DB 未变**；
- optimize grain=field：仅返回该字段片段；非法 target → 400；
- optimize 守卫：未确认大纲 → 409；无正文 → 400；
- apply：携带 1 条 change → applied=1、version+1、该场次 action 更新且
  元素 source=ai_optimized、未选中场次保持 ai；
- apply 并发：original 与库值不符 → applied=0、skipped=1、版本不变；
- apply base_version 不匹配 → 409；
- 非法 path：optimize 阶段丢弃（count 减少）、apply 阶段计入 skipped；
- 越权：用户 B 访问用户 A 的项目一律 404。

SSE 请求使用 httpx POST 且 headers 携带认证令牌，响应用
tests.conftest.parse_sse_events 解析为 (event, data) 列表后断言。
"""

import json
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select

from app.agents import script_pipeline as sp_mod
from app.models import Project, Script
from tests.conftest import create_project, parse_sse_events, register_user

# 保存输入时的原始文本（idea 模式一句话创意）
_SAMPLE_RAW = "一句话创意：两个少女约定一起成为漫画家的故事。"

# 正文流单批输出的固定增量块
_STREAM_CHUNKS = ["天台的风吹过，", "林小满握紧了拳头。"]


def _two_characters_payload() -> str:
    """构造含 2 个角色的合法角色 LLM 输出 JSON 字符串（内部函数）。

    Returns:
        str: {"characters": [林小满(lead), 陈默(support)]} 的 JSON 文本。

    Raises:
        无：纯结构构造。
    """
    return json.dumps({
        "characters": [
            {"name": "林小满", "role_type": "lead", "age": "约17岁",
             "personality": "倔强善良，遇挫不折", "appearance": "短发少女，戴红色发卡",
             "motivation": "和挚友一起成为漫画家", "arc": "从畏缩退让到坚定追梦",
             "relations": [{"target": "陈默", "relation": "漫画搭档"}]},
            {"name": "陈默", "role_type": "support", "age": "约18岁",
             "personality": "沉稳寡言，内心炽热", "appearance": "高瘦少年，黑框眼镜",
             "motivation": "守护小满的梦想", "arc": "从旁观者到并肩者",
             "relations": [{"target": "林小满", "relation": "漫画搭档"}]},
        ]
    }, ensure_ascii=False)


def _episode_plan_payload() -> str:
    """构造单集大纲 LLM 输出 JSON 字符串（内部函数）。

    Returns:
        str: {"episodes": [1 集]} 的 JSON 文本。

    Raises:
        无：纯结构构造。
    """
    return json.dumps({"episodes": [{
        "title": "第一集：天台之约",
        "logline": "两个少女在天台相遇并约定一起成为漫画家",
        "core_event": "天台立约，共同报名新人奖",
        "characters": ["林小满", "陈默"],
        "emotion_curve": "热血",
        "duration_sec": 90,
    }]}, ensure_ascii=False)


def _scenes_payload() -> str:
    """构造结构化场次 LLM 输出 JSON 字符串（内部函数，2 场）。

    Returns:
        str: {"locations": [...], "scenes": [...2 场...]} 的 JSON 文本。

    Raises:
        无：纯结构构造。
    """
    return json.dumps({
        "locations": [
            {"name": "学校天台", "description": "黄昏的学校天台", "time_of_day": "dusk"},
        ],
        "scenes": [
            {"scene_no": 1, "title": "天台立约", "action": "两人站上天台边缘远眺城市",
             "plot": "立约", "location": "学校天台", "time_of_day": "dusk",
             "int_ext": "ext", "sfx": ["风声"], "emotion": "热血",
             "dialogue": [{"character": "林小满", "character_id": 1, "type": "对白",
                           "emotion": "坚定", "line": "我们一起拿金奖吧。"}],
             "duration_sec": 45},
            {"scene_no": 2, "title": "画室赶稿", "action": "深夜画室两人赶稿",
             "plot": "赶稿", "location": "学校天台", "time_of_day": "night",
             "int_ext": "int", "sfx": ["笔尖沙沙"], "emotion": "温情",
             "dialogue": [{"character": "陈默", "character_id": 999, "type": "旁白",
                           "emotion": "平静", "line": "分镜我来帮你。"}],
             "duration_sec": 45},
        ],
    }, ensure_ascii=False)


def _mock_llm_chat(payload: str, calls: Optional[List[Dict[str, Any]]] = None):
    """构造可替换 script_pipeline._llm_chat 的异步假函数（内部工厂）。

    Args:
        payload: 每次 LLM 调用返回的固定文本。
        calls: 可选收集器列表；传入时每次调用追加
            {"temperature": 温度, "agent_name": Agent 标识} 供断言。

    Returns:
        Callable: 签名兼容 _llm_chat(messages, *, temperature, agent_name)
            的异步函数。

    Raises:
        无。
    """

    async def fake_chat(messages, *, temperature, **kwargs):
        """替换 _llm_chat：恒返回预置文本（内部函数）。

        Args:
            messages: OpenAI 格式消息列表（忽略）。
            temperature: 采样温度（记录进 calls）。
            **kwargs: 其余关键字参数（记录进 calls）。

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


def _mock_llm_stream(chunks: List[str]):
    """构造可替换 script_pipeline._llm_chat_stream 的异步假函数（内部工厂）。

    Args:
        chunks: 单次调用的增量块列表。

    Returns:
        Callable: 签名兼容 _llm_chat_stream(messages, *, agent_name)
            的异步生成器函数。

    Raises:
        无。
    """

    async def fake_stream(messages, *, agent_name="episode_writer", **kwargs):
        """替换 _llm_chat_stream：按预置块序列产出增量（内部函数）。

        Args:
            messages: OpenAI 格式消息列表（忽略）。
            agent_name: Agent 标识（忽略）。
            **kwargs: 其余关键字参数（忽略）。

        Yields:
            str: 预置的增量块（顺序产出）。

        Raises:
            无。
        """
        for chunk in chunks:
            yield chunk

    return fake_stream


def _changes_payload(changes: List[dict]) -> str:
    """构造优化 LLM 输出 JSON 字符串（内部函数）。

    Args:
        changes: changes 数组元素列表（path/original/proposed/reason）。

    Returns:
        str: {"changes": [...]} 的 JSON 文本。

    Raises:
        无：纯结构构造。
    """
    return json.dumps({"changes": changes}, ensure_ascii=False)


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
    project = await create_project(client, auth["headers"], title="AI 优化项目")
    resp = await client.put(
        f"/api/projects/{project['id']}/script/input",
        headers=auth["headers"],
        json={"mode": "idea", "raw_text": _SAMPLE_RAW},
    )
    assert resp.status_code == 200, resp.text
    return project


async def _setup_body_phase(client: httpx.AsyncClient, auth: Dict[str, Any],
                            monkeypatch) -> tuple:
    """辅助：走真实接口链建单集项目并推进到 phase=body（内部函数）。

    链路：建项目+输入 → mock 角色 LLM 生成并确认 2 角色（phase=outline）→
    mock 大纲 LLM 生成 1 集并确认（phase=body）。不生成正文。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: pytest 夹具（替换 _llm_chat）。

    Returns:
        tuple: (project_id, episode_id) 二元组。

    Raises:
        AssertionError: 任一接口返回非 200 或阶段未达 body 时中断。
    """
    project = await _make_project_with_input(client, auth)
    pid = project["id"]
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_two_characters_payload()))
    resp = await client.post(
        f"/api/projects/{pid}/script/characters/generate", headers=auth["headers"])
    assert resp.status_code == 200, resp.text
    confirm = await client.post(
        f"/api/projects/{pid}/script/characters/confirm",
        headers=auth["headers"],
        json={"characters": [{"name": "林小满"}, {"name": "陈默"}]},
    )
    assert confirm.status_code == 200, confirm.text

    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_episode_plan_payload()))
    plan = await client.post(
        f"/api/projects/{pid}/script/episodes/plan",
        headers=auth["headers"], json={"target_episodes": 1},
    )
    assert plan.status_code == 200, plan.text
    eid = plan.json()["episodes"][0]["episode_id"]
    confirm_plan = await client.post(
        f"/api/projects/{pid}/script/episodes/plan/confirm", headers=auth["headers"])
    assert confirm_plan.status_code == 200, confirm_plan.text
    assert confirm_plan.json()["phase"] == "body", confirm_plan.text
    return pid, eid


async def _generate_body(client: httpx.AsyncClient, auth: Dict[str, Any],
                         project_id: int, eid: int, monkeypatch) -> dict:
    """辅助：mock 正文流与结构化 LLM 后调生成 SSE 并返回 done 负载（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        project_id: 项目 ID。
        eid: 剧集 ID。
        monkeypatch: pytest 夹具（替换 _llm_chat_stream/_llm_chat）。

    Returns:
        dict: done 事件负载（episode_id/script_id/version/raw_text/content）。

    Raises:
        AssertionError: 事件序列或 done 缺失时中断。
    """
    monkeypatch.setattr(sp_mod, "_llm_chat_stream", _mock_llm_stream(_STREAM_CHUNKS))
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_scenes_payload()))
    resp = await client.post(
        f"/api/projects/{project_id}/script/episodes/{eid}/body/generate/stream",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    events = parse_sse_events(resp.text)
    done = next((d for n, d in events if n == "done" and d), None)
    assert done is not None, resp.text
    return done


async def _setup_body_generated(client: httpx.AsyncClient, auth: Dict[str, Any],
                                monkeypatch) -> tuple:
    """辅助：推进到 body 阶段并生成一集正文，返回 (pid, eid, done)（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: pytest 夹具。

    Returns:
        tuple: (project_id, episode_id, done 负载)。

    Raises:
        AssertionError: 前置链任一环节失败时中断。
    """
    pid, eid = await _setup_body_phase(client, auth, monkeypatch)
    done = await _generate_body(client, auth, pid, eid, monkeypatch)
    return pid, eid, done


async def _preview(client: httpx.AsyncClient, auth: Dict[str, Any], monkeypatch,
                   pid: int, eid: int, changes: List[dict],
                   grain: str = "segment", target: Any = 1) -> httpx.Response:
    """辅助：mock 优化 LLM 后调 optimize 预览接口（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: pytest 夹具（替换 _llm_chat）。
        pid: 项目 ID。
        eid: 剧集 ID。
        changes: mock 优化 LLM 返回的 changes 数组。
        grain: 优化粒度。
        target: 粒度目标。

    Returns:
        httpx.Response: optimize 接口响应。

    Raises:
        无：响应状态由调用方断言。
    """
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_changes_payload(changes)))
    return await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/optimize",
        headers=auth["headers"],
        json={"grain": grain, "target": target},
    )


# ---------------------------------------------------------------------------
# 1. optimize grain=segment：片段化 diff + 不落库
# ---------------------------------------------------------------------------
async def test_optimize_segment_preview(client, auth, db, monkeypatch):
    """segment 粒度优化：返回 1 片段含 diff_lines（op 含 insert）、original
    以当前库值覆盖、DB content 未变（不落库）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言落库未变）。
        monkeypatch: 替换 _llm_chat / _llm_chat_stream。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, eid, done = await _setup_body_generated(client, auth, monkeypatch)
    assert done["version"] == 1

    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(
        _changes_payload([{
            "path": "scenes[0].action",
            "original": "LLM 乱写的原文",
            "proposed": "夜风呼啸，两人并肩站上天台边缘远眺城市。",
            "reason": "增强画面感",
        }]), calls))
    resp = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/optimize",
        headers=auth["headers"], json={"grain": "segment", "target": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["episode_id"] == eid
    assert body["grain"] == "segment"
    assert body["count"] == 1
    change = body["changes"][0]
    assert change["path"] == "scenes[0].action"
    assert change["kind"] == "field"
    # original 以当前库值为准（覆盖 LLM 乱写的原文）
    assert change["original"] == "两人站上天台边缘远眺城市"
    assert change["proposed"].startswith("夜风呼啸")
    assert change["id"]
    ops = [line["op"] for line in change["diff_lines"]]
    assert "insert" in ops, ops
    # LLM 挂点：agent_name=script_optimize、temperature=0.5
    assert calls and calls[0]["agent_name"] == "script_optimize"
    assert calls[0]["temperature"] == 0.5

    # 不落库：DB 行版本与 action 均未变
    row = await db.get(Script, done["script_id"])
    assert row.version == 1
    assert row.content["scenes"]["value"][0]["action"] == "两人站上天台边缘远眺城市"


# ---------------------------------------------------------------------------
# 2. optimize grain=field：仅该字段片段 / 非法 target 400
# ---------------------------------------------------------------------------
async def test_optimize_field_grain_and_invalid_target(client, auth, monkeypatch):
    """field 粒度优化：仅返回目标字段片段；target 非法（越界/非白名单/缺失）
    一律 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 替换 _llm_chat / _llm_chat_stream。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, eid, _done = await _setup_body_generated(client, auth, monkeypatch)

    resp = await _preview(
        client, auth, monkeypatch, pid, eid,
        [{"path": "scenes[1].plot", "proposed": "深夜画室里，两人为赶稿全力以赴。"}],
        grain="field", target="scenes[1].plot")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    assert body["changes"][0]["path"] == "scenes[1].plot"
    assert body["changes"][0]["original"] == "赶稿"

    # 越界路径 target → 400
    out_of_range = await _preview(
        client, auth, monkeypatch, pid, eid, [], grain="field", target="scenes[9].plot")
    assert out_of_range.status_code == 400, out_of_range.text
    # 非白名单 target → 400
    not_allowed = await _preview(
        client, auth, monkeypatch, pid, eid, [], grain="field", target="title")
    assert not_allowed.status_code == 400, not_allowed.text
    # segment 缺 target → 400
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_changes_payload([])))
    missing = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/optimize",
        headers=auth["headers"], json={"grain": "segment"})
    assert missing.status_code == 400, missing.text


# ---------------------------------------------------------------------------
# 3. optimize 守卫：未确认大纲 409 / 无正文 400
# ---------------------------------------------------------------------------
async def test_optimize_guards(client, auth, db, monkeypatch):
    """守卫：phase 非 body → 409「请先确认分集大纲」；无正文 → 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直改 project.script_phase 造数）。
        monkeypatch: 替换 _llm_chat / _llm_chat_stream。

    Returns:
        None。

    Raises:
        AssertionError: 任一状态码不符时中断。
    """
    pid, eid = await _setup_body_phase(client, auth, monkeypatch)
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_changes_payload([])))

    # phase=body 但无正文 → 400
    no_body = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/optimize",
        headers=auth["headers"], json={"grain": "global"})
    assert no_body.status_code == 400, no_body.text
    assert "尚未生成正文" in no_body.json()["detail"]

    # 生成正文后把 phase 置回 characters → 409
    await _generate_body(client, auth, pid, eid, monkeypatch)
    row = await db.get(Project, pid)
    row.script_phase = "characters"
    await db.commit()
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_changes_payload([])))
    bad_phase = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/optimize",
        headers=auth["headers"], json={"grain": "global"})
    assert bad_phase.status_code == 409, bad_phase.text
    assert "请先确认分集大纲" in bad_phase.json()["detail"]


# ---------------------------------------------------------------------------
# 4. apply：应用片段 → version+1、source=ai_optimized、未选中场次原样
# ---------------------------------------------------------------------------
async def test_apply_optimize_changes(client, auth, db, monkeypatch):
    """apply：携带 1 条 change → applied=1、version+1、该场次 action 更新且
    元素 source=ai_optimized、未改动场次保持 ai、content 仍可解包。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言落库）。
        monkeypatch: 替换 _llm_chat / _llm_chat_stream。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, eid, done = await _setup_body_generated(client, auth, monkeypatch)
    script_id = done["script_id"]

    preview = await _preview(
        client, auth, monkeypatch, pid, eid,
        [{"path": "scenes[0].action", "proposed": "夜风呼啸，两人并肩站上天台边缘远眺城市。"}],
        grain="segment", target=1)
    assert preview.status_code == 200, preview.text
    frag = preview.json()["changes"][0]

    apply_payload = {
        "accept_ids": [frag["id"]],
        "changes": [{"id": frag["id"], "path": frag["path"],
                     "original": frag["original"], "proposed": frag["proposed"]}],
        "base_version": 1,
    }
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_changes_payload([])))
    resp = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/optimize/apply",
        headers=auth["headers"], json=apply_payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] == 1
    assert body["skipped"] == 0
    assert body["version"] == 2

    content = body["content"]
    # 一级字段仍为 {value, source} 字段级包装（content_plain 可解包）
    assert content["scenes"]["value"][0]["action"] == "夜风呼啸，两人并肩站上天台边缘远眺城市。"
    # 被改动场次元素 source=ai_optimized，未改动场次保持 ai
    assert content["scenes"]["value"][0]["source"] == "ai_optimized"
    assert content["scenes"]["value"][1]["source"] == "ai"
    assert content["scenes"]["value"][1]["action"] == "深夜画室两人赶稿"

    db.expire_all()
    row = await db.get(Script, script_id)
    assert row.version == 2
    assert row.content["scenes"]["value"][0]["action"] == "夜风呼啸，两人并肩站上天台边缘远眺城市。"
    assert row.content["scenes"]["value"][0]["source"] == "ai_optimized"


# ---------------------------------------------------------------------------
# 5. apply 并发：original 与库值不符 → skipped / 不落库
# ---------------------------------------------------------------------------
async def test_apply_optimize_concurrent_skipped(client, auth, db, monkeypatch):
    """apply 并发：original 与当前库值不符（模拟并发改过）→ applied=0、
    skipped=1、版本不变。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言版本未变）。
        monkeypatch: 替换 _llm_chat / _llm_chat_stream。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, eid, done = await _setup_body_generated(client, auth, monkeypatch)
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_changes_payload([])))
    resp = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/optimize/apply",
        headers=auth["headers"],
        json={
            "accept_ids": ["c1"],
            "changes": [{
                "id": "c1",
                "path": "scenes[0].action",
                "original": "被并发改动过的旧内容",
                "proposed": "新的动作描述。",
            }],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] == 0
    assert body["skipped"] == 1
    assert body["version"] == 1

    row = await db.get(Script, done["script_id"])
    assert row.version == 1
    assert row.content["scenes"]["value"][0]["action"] == "两人站上天台边缘远眺城市"


# ---------------------------------------------------------------------------
# 6. apply base_version 不匹配 → 409
# ---------------------------------------------------------------------------
async def test_apply_optimize_base_version_conflict(client, auth, monkeypatch):
    """apply base_version 与当前 version 不一致 → 409。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 替换 _llm_chat / _llm_chat_stream。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 409 时中断。
    """
    pid, eid, _done = await _setup_body_generated(client, auth, monkeypatch)
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_changes_payload([])))
    resp = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/optimize/apply",
        headers=auth["headers"],
        json={
            "accept_ids": ["c1"],
            "changes": [{"id": "c1", "path": "scenes[0].action",
                         "original": "两人站上天台边缘远眺城市",
                         "proposed": "新的动作描述。"}],
            "base_version": 99,
        },
    )
    assert resp.status_code == 409, resp.text
    assert "剧本已被其他操作修改" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 7. 非法 path：optimize 丢弃（count 减少）/ apply 计入 skipped
# ---------------------------------------------------------------------------
async def test_illegal_path_dropped_and_skipped(client, auth, monkeypatch):
    """非法 path：optimize 阶段被丢弃使 count 减少；apply 阶段计入 skipped。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 替换 _llm_chat / _llm_chat_stream。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, eid, _done = await _setup_body_generated(client, auth, monkeypatch)

    # optimize：3 条 change（1 合法 + 1 越界 + 1 非白名单）→ count 减少为 1
    resp = await _preview(
        client, auth, monkeypatch, pid, eid,
        [
            {"path": "scenes[0].action", "proposed": "合法优化内容。"},
            {"path": "scenes[9].action", "proposed": "越界内容。"},
            {"path": "title", "proposed": "非白名单内容。"},
        ],
        grain="global", target=None)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    assert body["changes"][0]["path"] == "scenes[0].action"

    # apply：1 合法 + 1 越界 → applied=1、skipped=1
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_changes_payload([])))
    apply_resp = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/optimize/apply",
        headers=auth["headers"],
        json={
            "accept_ids": ["ok", "bad"],
            "changes": [
                {"id": "ok", "path": "scenes[0].action",
                 "original": "两人站上天台边缘远眺城市", "proposed": "合法优化内容。"},
                {"id": "bad", "path": "scenes[9].action",
                 "original": "任意", "proposed": "越界内容。"},
            ],
        },
    )
    assert apply_resp.status_code == 200, apply_resp.text
    applied_body = apply_resp.json()
    assert applied_body["applied"] == 1
    assert applied_body["skipped"] == 1
    assert applied_body["version"] == 2


# ---------------------------------------------------------------------------
# 8. 越权 404
# ---------------------------------------------------------------------------
async def test_forbidden_access_404(client, auth, monkeypatch):
    """越权：用户 B 访问用户 A 的项目（optimize / apply）一律 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 用户 A（项目归属者）。
        monkeypatch: 替换 _llm_chat / _llm_chat_stream。

    Returns:
        None。

    Raises:
        AssertionError: 任一状态码非 404 时中断。
    """
    pid, eid, _done = await _setup_body_generated(client, auth, monkeypatch)

    other = await register_user(client, prefix="other")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    preview = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/optimize",
        headers=other_headers, json={"grain": "global"})
    assert preview.status_code == 404, preview.text

    apply_resp = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/optimize/apply",
        headers=other_headers,
        json={"accept_ids": [], "changes": []})
    assert apply_resp.status_code == 404, apply_resp.text
