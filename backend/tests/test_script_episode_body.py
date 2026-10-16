"""P-4 逐集正文阶段接口测试模块（文件级说明，P-1 范式级重构 Task 5）。

覆盖链路（全部 mock LLM：正文流 monkeypatch 模块级 _llm_chat_stream、
结构化场次 monkeypatch 模块级 _llm_chat，两段式各走各的挂点）：
- 前置链：走真实接口链（mock 角色 LLM）生成并确认 2 角色 → mock 大纲 LLM
  生成 1 集大纲并确认 → phase=body；
- 正文 SSE 生成成功：mock 正文流 2 块 delta + scenes JSON 2 场 → 事件序列
  delta×2 + status + done、done.raw_text 拼接正确、scripts 行落库
  （episode_id 绑定、scene_no 重排、locations 去重、dialogue.character_id
  按名单回填——LLM 给错的以名单为准、场次带 action/sfx/type）、version=1；
- 阶段守卫：phase 被手动置回 characters / NULL（快速模式）→ 409；
- 保存合并：改 1 场 action → 该场 source=user、未改场次保持 ai；首次保存
  无旧数据全 ai；base_version 不匹配 409；
- regenerate：无正文 400；有正文重生成成功 version+1 且 raw_text 更新；
- 长文分段：多段 emotion_curve（3 段）触发 3 批衔接调用（第 2 批起携带
  前文结尾衔接上下文），delta 全量拼接为完整正文；
- 越权：用户 B 访问用户 A 的项目一律 404。

SSE 请求统一使用 httpx POST 且 headers 携带认证令牌，响应用
tests.conftest.parse_sse_events 解析为 (event, data) 列表后断言。
"""

import json
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select

from app.agents import script_pipeline as sp_mod
from app.models import Character, Episode, Project, Script
from tests.conftest import create_project, parse_sse_events, register_user

# 保存输入时的原始文本（idea 模式一句话创意）
_SAMPLE_RAW = "一句话创意：两个少女约定一起成为漫画家的故事。"

# 正文流单批输出的固定增量块（2 块：测试 2/4/5/7 复用）
_STREAM_CHUNKS = ["天台的风吹过，", "林小满握紧了拳头。"]


def _two_characters_payload() -> str:
    """构造含 2 个角色的合法角色 LLM 输出 JSON 字符串（内部函数，同 Task 4）。

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


def _episode_plan_payload(curve: str = "热血") -> str:
    """构造单集大纲 LLM 输出 JSON 字符串（内部函数，emotion_curve 可定制）。

    Args:
        curve: 情绪曲线字符串（单段如「热血」不分批；「平静→爆发→释然」
            分 3 批，供长文分段用例触发多批衔接）。

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
        "emotion_curve": curve,
        "duration_sec": 90,
    }]}, ensure_ascii=False)


def _scenes_payload() -> str:
    """构造结构化场次 LLM 输出 JSON 字符串（内部函数，2 场含 P-4 扩展字段）。

    刻意制造三类规整点供断言：scene_no 为 5/9（应重排 1/2）、locations
    含同名重复条目（应按 name 去重）、第 2 场 dialogue.character_id=999
    （LLM 给错，应按名单回填陈默真实 ID）。

    Returns:
        str: {"locations": [...], "scenes": [...]} 的 JSON 文本。

    Raises:
        无：纯结构构造。
    """
    return json.dumps({
        "locations": [
            {"name": "学校天台", "description": "黄昏的学校天台", "time_of_day": "dusk"},
            {"name": "学校天台", "description": "重复条目应被去重", "time_of_day": "day"},
        ],
        "scenes": [
            {"scene_no": 5, "title": "天台立约", "action": "两人站上天台边缘远眺城市",
             "plot": "立约", "location": "学校天台", "time_of_day": "dusk",
             "int_ext": "ext", "sfx": ["风声"], "emotion": "热血",
             "dialogue": [{"character": "林小满", "character_id": 1, "type": "对白",
                           "emotion": "坚定", "line": "我们一起拿金奖吧。"}],
             "duration_sec": 45},
            {"scene_no": 9, "title": "画室赶稿", "action": "深夜画室两人赶稿",
             "plot": "赶稿", "location": "学校天台", "time_of_day": "night",
             "int_ext": "int", "sfx": ["笔尖沙沙"], "emotion": "温情",
             "dialogue": [{"character": "陈默", "character_id": 999, "type": "旁白",
                           "line": "分镜我来帮你。"}],
             "duration_sec": 45},
        ],
    }, ensure_ascii=False)


def _mock_llm_chat(payload: str, calls: Optional[List[Dict[str, Any]]] = None):
    """构造可替换 script_pipeline._llm_chat 的异步假函数（内部工厂，同 Task 4）。

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


def _mock_llm_stream(chunks: List[str], calls: Optional[List[Dict[str, Any]]] = None):
    """构造可替换 script_pipeline._llm_chat_stream 的异步假函数（内部工厂）。

    每次（每批）调用均依次产出 chunks 中的全部增量块。

    Args:
        chunks: 单次调用的增量块列表。
        calls: 可选收集器列表；传入时每次调用追加
            {"agent_name": Agent 标识, "user": 末条 user 消息全文} 供断言
            （长文分段用例据此验证衔接上下文）。

    Returns:
        Callable: 签名兼容 _llm_chat_stream(messages, *, agent_name)
            的异步生成器函数。

    Raises:
        无。
    """

    async def fake_stream(messages, *, agent_name="episode_writer", **kwargs):
        """替换 _llm_chat_stream：按预置块序列产出增量（内部函数）。

        Args:
            messages: OpenAI 格式消息列表（user 取末条记录进 calls）。
            agent_name: Agent 标识（记录进 calls）。
            **kwargs: 其余关键字参数（忽略）。

        Yields:
            str: 预置的增量块（顺序产出）。

        Raises:
            无。
        """
        if calls is not None:
            calls.append({
                "agent_name": agent_name,
                "user": messages[-1]["content"] if messages else "",
            })
        for chunk in chunks:
            yield chunk

    return fake_stream


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
    project = await create_project(client, auth["headers"], title="逐集正文项目")
    resp = await client.put(
        f"/api/projects/{project['id']}/script/input",
        headers=auth["headers"],
        json={"mode": "idea", "raw_text": _SAMPLE_RAW},
    )
    assert resp.status_code == 200, resp.text
    return project


async def _setup_body_phase(client: httpx.AsyncClient, auth: Dict[str, Any],
                            monkeypatch=None, curve: str = "热血") -> tuple:
    """辅助：走真实接口链建单集项目并推进到 phase=body（内部函数）。

    链路：建项目+输入 → mock 角色 LLM 生成并确认 2 角色（phase=outline）→
    mock 大纲 LLM 生成 1 集（emotion_curve=curve）并确认（phase=body）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: pytest 夹具；传入时经其替换 _llm_chat（用例结束自动还原）。
        curve: mock 大纲的情绪曲线字符串。

    Returns:
        tuple: (project_id, episode_id) 二元组。

    Raises:
        AssertionError: 任一接口返回非 200 或阶段未达 body 时中断。
    """
    project = await _make_project_with_input(client, auth)
    pid = project["id"]
    if monkeypatch is not None:
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
    assert confirm.json()["phase"] == "outline", confirm.text

    if monkeypatch is not None:
        monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_episode_plan_payload(curve)))
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
                         project_id: int, eid: int, monkeypatch=None) -> dict:
    """辅助：mock 正文流与结构化 LLM 后调生成 SSE 并返回 done 负载（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        project_id: 项目 ID。
        eid: 剧集 ID。
        monkeypatch: pytest 夹具（替换 _llm_chat_stream/_llm_chat）。

    Returns:
        dict: done 事件负载（episode_id/script_id/version/raw_text/content/usage）。

    Raises:
        AssertionError: 事件序列或 done 缺失时中断。
    """
    if monkeypatch is not None:
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


# ---------------------------------------------------------------------------
# 1. 正文 SSE 生成成功
# ---------------------------------------------------------------------------
async def test_generate_body_stream_success(client, auth, db, monkeypatch):
    """正文 SSE 生成成功：事件序列 delta×2 + status + done；raw_text 为增量
    拼接；scripts 行落库（episode_id 绑定、scene_no 重排 1..2、locations
    按 name 去重、character_id 按名单回填、场次带 action/sfx/type）；
    version=1；episode.status 置 scripted。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直接断言落库行）。
        monkeypatch: 替换 _llm_chat_stream/_llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, eid = await _setup_body_phase(client, auth, monkeypatch)

    stream_calls: List[Dict[str, Any]] = []
    chat_calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat_stream",
                        _mock_llm_stream(_STREAM_CHUNKS, stream_calls))
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_scenes_payload(), chat_calls))
    resp = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/body/generate/stream",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text

    # 事件序列：delta×2（单批 2 块增量）→ status → done
    events = parse_sse_events(resp.text)
    names = [n for n, _ in events]
    assert names == ["delta", "delta", "status", "done"], names
    deltas = [d["text"] for n, d in events if n == "delta"]
    assert deltas == _STREAM_CHUNKS

    # LLM 挂点：正文流 1 批 1 次（agent_name=episode_writer）；结构化 1 次成功不重试
    assert len(stream_calls) == 1
    assert stream_calls[0]["agent_name"] == "episode_writer"
    assert len(chat_calls) == 1
    assert chat_calls[0]["agent_name"] == "episode_writer"
    assert chat_calls[0]["temperature"] == 0.8

    # done 负载：raw_text 拼接正确、version=1、episode_id 绑定
    done = next(d for n, d in events if n == "done" and d)
    assert done["episode_id"] == eid
    assert done["script_id"] > 0
    assert done["version"] == 1
    assert done["raw_text"] == "".join(_STREAM_CHUNKS)

    # content 字段级包装：scenes/locations 取 value
    scenes = done["content"]["scenes"]["value"]
    assert len(scenes) == 2
    # scene_no 重排 1..n（LLM 给的 5/9 被忽略）
    assert [s["scene_no"] for s in scenes] == [1, 2]
    # 场次带 action（P-4 扩展）与 sfx
    assert scenes[0]["action"] == "两人站上天台边缘远眺城市"
    assert scenes[0]["sfx"] == ["风声"]
    # locations 按 name 去重（重复的「学校天台」只保留 1 条）
    locations = done["content"]["locations"]["value"]
    assert len(locations) == 1
    assert locations[0]["name"] == "学校天台"
    assert locations[0]["time_of_day"] == "dusk"
    # dialogue 带 type（白名单内保留）与 emotion
    assert scenes[0]["dialogue"][0]["type"] == "对白"
    assert scenes[0]["dialogue"][0]["emotion"] == "坚定"
    assert scenes[1]["dialogue"][0]["type"] == "旁白"
    # content.title/logline 取剧集值且 source=ai
    assert done["content"]["title"]["value"] == "第一集：天台之约"
    assert done["content"]["title"]["source"] == "ai"

    # character_id 按名单回填（LLM 给错的 999 以名单为准）
    char_rows = (
        await db.execute(select(Character).where(Character.project_id == pid))
    ).scalars().all()
    ids = {c.name: c.id for c in char_rows}
    assert scenes[0]["dialogue"][0]["character_id"] == ids["林小满"]
    assert scenes[1]["dialogue"][0]["character_id"] == ids["陈默"]

    # 落库：scripts 行 episode_id 绑定该集、version=1、raw_text 一致
    row = (
        await db.execute(
            select(Script).where(
                Script.project_id == pid, Script.episode_id == eid)
        )
    ).scalar_one()
    assert row.version == 1
    assert row.raw_text == "".join(_STREAM_CHUNKS)
    # 落库 content 的 scenes 与 done 一致（character_id 已回填）
    row_scenes = row.content["scenes"]["value"]
    assert row_scenes[0]["dialogue"][0]["character_id"] == ids["林小满"]

    # episode.status 置 scripted
    episode = await db.get(Episode, eid)
    assert episode.status == "scripted"


# ---------------------------------------------------------------------------
# 2. 阶段守卫：非 body 阶段 409（characters / NULL 快速模式）
# ---------------------------------------------------------------------------
async def test_generate_body_requires_body_phase(client, auth, db, monkeypatch):
    """阶段守卫：phase 被手动置回 characters / NULL（快速模式）时正文生成
    均 409，detail 提示先确认分集大纲（NULL 不放行正文接口）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直改 project.script_phase 造数）。
        monkeypatch: 替换 _llm_chat（保证即便通过校验也不会真调 LLM）。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 409 时中断。
    """
    pid, eid = await _setup_body_phase(client, auth, monkeypatch)
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_scenes_payload()))

    # phase 置回 characters（未确认大纲）
    row = await db.get(Project, pid)
    row.script_phase = "characters"
    await db.commit()
    resp = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/body/generate/stream",
        headers=auth["headers"],
    )
    assert resp.status_code == 409, resp.text
    assert "请先确认分集大纲" in resp.json()["detail"]

    # phase 置 NULL（快速模式）同样 409
    row.script_phase = None
    await db.commit()
    resp_null = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/body/generate/stream",
        headers=auth["headers"],
    )
    assert resp_null.status_code == 409, resp_null.text
    assert "请先确认分集大纲" in resp_null.json()["detail"]


# ---------------------------------------------------------------------------
# 3. 保存合并：场次级 source 合并 / 首次保存全 ai / base_version 409
# ---------------------------------------------------------------------------
async def test_save_body_merges_scene_sources(client, auth, db, monkeypatch):
    """保存合并：生成后改 1 场 action 保存 → 该场 source=user、未改场次
    保持 ai；base_version 不匹配 409；首次保存（无旧行）全 ai 且 version=1。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言落库 version 与 source）。
        monkeypatch: 替换 _llm_chat_stream/_llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, eid = await _setup_body_phase(client, auth, monkeypatch)
    done = await _generate_body(client, auth, pid, eid, monkeypatch)
    assert done["version"] == 1

    # 解包 done.content 为纯结构后改第 1 场 action（第 2 场不动）
    plain = {
        "title": done["content"]["title"]["value"],
        "logline": done["content"]["logline"]["value"],
        "locations": done["content"]["locations"]["value"],
        "scenes": done["content"]["scenes"]["value"],
    }
    plain["scenes"][0]["action"] = "用户改过的动作：小满攥紧栏杆俯瞰操场"

    # base_version 不匹配 → 409
    conflict = await client.put(
        f"/api/projects/{pid}/script/episodes/{eid}/body",
        headers=auth["headers"],
        json={"raw_text": "改过的正文", "content": plain, "base_version": 99},
    )
    assert conflict.status_code == 409, conflict.text
    assert "剧本已被其他操作修改" in conflict.json()["detail"]

    # 正确保存：base_version=1 匹配
    resp = await client.put(
        f"/api/projects/{pid}/script/episodes/{eid}/body",
        headers=auth["headers"],
        json={"raw_text": "用户修改后的正文", "content": plain, "base_version": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["episode_id"] == eid
    assert body["script_id"] == done["script_id"]
    assert body["version"] == 2
    assert body["raw_text"] == "用户修改后的正文"
    # 整体包装 source=user；场次级 source：改动场 user、未改场保持 ai
    assert body["content"]["source"] == "user"
    saved_scenes = body["content"]["value"]["scenes"]
    assert saved_scenes[0]["source"] == "user"
    assert saved_scenes[0]["action"] == "用户改过的动作：小满攥紧栏杆俯瞰操场"
    assert saved_scenes[1]["source"] == "ai"

    # 落库核验：version=2、content 为整体包装
    row = await db.get(Script, done["script_id"])
    assert row.version == 2
    assert row.content["source"] == "user"
    assert row.raw_text == "用户修改后的正文"


async def test_save_body_first_time_all_ai(client, auth, monkeypatch):
    """首次保存：该集从未生成正文（无 scripts 行）时直接 PUT → 新建
    version=1，全部场次标 source=ai。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 替换 _llm_chat（前置链需要）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, eid = await _setup_body_phase(client, auth, monkeypatch)

    resp = await client.put(
        f"/api/projects/{pid}/script/episodes/{eid}/body",
        headers=auth["headers"],
        json={
            "raw_text": "手写的文章体正文。",
            "content": {
                "title": "第一集：天台之约",
                "logline": "两个少女在天台相遇",
                "scenes": [
                    {"scene_no": 1, "title": "天台", "plot": "相遇立约",
                     "dialogue": [{"character": "林小满", "line": "走吧。"}],
                     "duration_sec": 45},
                    {"scene_no": 2, "title": "画室", "plot": "赶稿",
                     "duration_sec": 45},
                ],
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 1
    assert body["content"]["source"] == "user"
    saved_scenes = body["content"]["value"]["scenes"]
    assert len(saved_scenes) == 2
    # 首次保存无旧数据：全部场次标 ai（AI 生成后未改动直接保存的口径）
    assert all(s["source"] == "ai" for s in saved_scenes)


# ---------------------------------------------------------------------------
# 4. regenerate：无正文 400 / 有正文重生成 version+1 且 raw_text 更新
# ---------------------------------------------------------------------------
async def test_regenerate_body(client, auth, db, monkeypatch):
    """regenerate：无正文 400「该集尚未生成正文」；生成后重生成成功
    version+1、raw_text 覆盖为新文本、事件序列与生成接口同款。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言落库 version/raw_text）。
        monkeypatch: 替换 _llm_chat_stream/_llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, eid = await _setup_body_phase(client, auth, monkeypatch)

    # 无正文：400
    empty = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/body/regenerate",
        headers=auth["headers"],
    )
    assert empty.status_code == 400, empty.text
    assert "该集尚未生成正文" in empty.json()["detail"]

    # 首次生成 version=1
    done = await _generate_body(client, auth, pid, eid, monkeypatch)
    assert done["version"] == 1
    old_script_id = done["script_id"]

    # 重生成：换一批正文增量 → version=2、raw_text 更新
    monkeypatch.setattr(sp_mod, "_llm_chat_stream", _mock_llm_stream(["第二版正文，重新生成。"]))
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_scenes_payload()))
    resp = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/body/regenerate",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    events = parse_sse_events(resp.text)
    names = [n for n, _ in events]
    assert names == ["delta", "status", "done"], names
    done2 = next(d for n, d in events if n == "done" and d)
    assert done2["script_id"] == old_script_id  # 复用同一行（upsert）
    assert done2["version"] == 2
    assert done2["raw_text"] == "第二版正文，重新生成。"

    row = await db.get(Script, old_script_id)
    assert row.version == 2
    assert row.raw_text == "第二版正文，重新生成。"


# ---------------------------------------------------------------------------
# 5. 长文分段：多段 emotion_curve 触发多批衔接调用
# ---------------------------------------------------------------------------
async def test_stream_body_multi_batch(client, auth, db, monkeypatch):
    """长文分段：大纲 emotion_curve 3 段（平静→爆发→释然）→ 3 批流式调用
    （衔接调用次数 ≥2），第 2 批起 user 消息携带前文结尾衔接上下文；delta
    全量拼接为完整正文并落库。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言落库 raw_text 拼接）。
        monkeypatch: 替换 _llm_chat_stream/_llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, eid = await _setup_body_phase(client, auth, monkeypatch, curve="平静→爆发→释然")

    batch_chunks = ["本批第一句。", "本批第二句。", "本批第三句。"]
    stream_calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat_stream",
                        _mock_llm_stream(batch_chunks, stream_calls))
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_scenes_payload()))
    resp = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/body/generate/stream",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    events = parse_sse_events(resp.text)
    names = [n for n, _ in events]

    # 3 批 × 每批 3 块 = 9 个 delta，随后 status/done
    assert names == ["delta"] * 9 + ["status", "done"], names
    deltas = [d["text"] for n, d in events if n == "delta"]
    assert deltas == batch_chunks * 3

    # 衔接调用次数 = 3 批（≥2）；第 1 批无衔接、第 2/3 批携带前文结尾
    assert len(stream_calls) == 3
    assert "前文结尾" not in stream_calls[0]["user"]
    assert "前文结尾" in stream_calls[1]["user"]
    # 第 2 批的衔接上下文包含第 1 批正文末尾
    assert batch_chunks[-1] in stream_calls[1]["user"]
    assert "前文结尾" in stream_calls[2]["user"]
    # 各批均为 episode_writer 出入口
    assert all(c["agent_name"] == "episode_writer" for c in stream_calls)

    done = next(d for n, d in events if n == "done" and d)
    full_text = "".join(batch_chunks) * 3
    assert done["raw_text"] == full_text
    assert done["version"] == 1
    row = (
        await db.execute(
            select(Script).where(
                Script.project_id == pid, Script.episode_id == eid)
        )
    ).scalar_one()
    assert row.raw_text == full_text


# ---------------------------------------------------------------------------
# 6. 越权
# ---------------------------------------------------------------------------
async def test_forbidden_access_404(client, auth, monkeypatch):
    """越权：用户 B 访问用户 A 的项目（生成 / 保存 / 重生成）一律 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 用户 A（项目归属者）。
        monkeypatch: 替换 _llm_chat（保证即便通过归属校验也不会真调 LLM）。

    Returns:
        None。

    Raises:
        AssertionError: 任一状态码非 404 时中断。
    """
    pid, eid = await _setup_body_phase(client, auth, monkeypatch)

    other = await register_user(client, prefix="other")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    gen = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/body/generate/stream",
        headers=other_headers,
    )
    assert gen.status_code == 404, gen.text
    save = await client.put(
        f"/api/projects/{pid}/script/episodes/{eid}/body",
        headers=other_headers,
        json={"raw_text": "越权保存", "content": {"scenes": []}},
    )
    assert save.status_code == 404, save.text
    regen = await client.post(
        f"/api/projects/{pid}/script/episodes/{eid}/body/regenerate",
        headers=other_headers,
    )
    assert regen.status_code == 404, regen.text
