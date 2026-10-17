"""P1 决议对齐测试模块（文件级说明，P-1 范式级重构 Task 8）。

覆盖 RD-02 体检规则重写 / 时长口径统一 / RD-03 场次级 source /
RD-01 忠实度差异化策略：

1. 角色出场断层：已确认角色零出场 → warning；全部出场 → 无；
2. 对白过长：单条对白 > 120 字 → warning（带场次定位）；
3. 缺动作描写：新 Schema（含 action 键）空 action → warning；
   旧 Schema（全无 action 键）→ 不报（避免大面积误报）；
4. 缺高潮点：末场 emotion 无峰值词且 plot 无感叹号 → info；
5. 时长阈值收紧到 ±10%：偏差 12% → error 命中；偏差 8% → 不报；
6. 对白语速交叉校验：对白 40 字（≈10s）vs 场景时长 60s → warning；
7. 按集体检：不传 episode_id 时聚合各集且 location 带集号前缀；传 episode_id
   时仅体检该集；
8. 场次级 source：快速模式生成 3 场后改 1 场保存 → 该场 user、其余 ai；
   content_plain / unwrap_scenes 解包正常；
9. 忠实度策略：free/outline/faithful 三档 directive 文案互异；faithful 下
   mock LLM 返回含名单外角色的集大纲 → 触发重试（调用 ≥2 次）。
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
from sqlalchemy import select

from app.agents import llm_adapter
from app.agents import script_pipeline as sp_mod
from app.agents.script_pipeline import build_fidelity_directive
from app.models import Episode, Script
from app.services.script_service import (
    content_plain,
    run_health_check,
    unwrap_scenes,
)
from tests.conftest import create_project, parse_sse_events

# 保存输入时的原始文本（idea 模式一句话创意）
_SAMPLE_RAW = "一句话创意：两个少女约定一起成为漫画家的故事。"


def _project(target_duration: int = 180) -> SimpleNamespace:
    """构造仅供 run_health_check 使用的轻量项目对象（内部函数）。

    run_health_check 仅读取 project.target_duration，故用 SimpleNamespace
    避免依赖数据库会话。

    Args:
        target_duration: 目标时长（秒）。

    Returns:
        SimpleNamespace: 含 target_duration 属性的轻量对象。

    Raises:
        无：纯结构构造。
    """
    return SimpleNamespace(target_duration=target_duration)


def _script(scenes: List[Dict[str, Any]]) -> SimpleNamespace:
    """构造仅含 content 的轻量剧本对象（内部函数）。

    Args:
        scenes: 场次列表（裸结构 {"scenes": scenes} 亦可被 unwrap_scenes 解包）。

    Returns:
        SimpleNamespace: 含 content 属性的轻量对象。

    Raises:
        无：纯结构构造。
    """
    return SimpleNamespace(content={"scenes": scenes})


def _issue_messages(report: Dict[str, Any]) -> List[str]:
    """提取报告中的问题文案列表（内部函数）。

    Args:
        report: run_health_check 返回的报告 dict。

    Returns:
        List[str]: issues 的 message 列表。

    Raises:
        无：结构异常按空列表处理。
    """
    return [str(it.get("message") or "") for it in report.get("issues", [])]


# ---------------------------------------------------------------------------
# 1. 角色出场断层
# ---------------------------------------------------------------------------
def test_jiaose_chuchang_duanceng():
    """角色出场断层：2 个已确认角色仅 1 个出场 → warning；全部出场 → 无。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    scenes = [
        {"scene_no": 1, "title": "天台", "plot": "相遇", "emotion": "爆发",
         "dialogue": [{"character": "林小满", "line": "一起加油。"}], "duration_sec": 90},
        {"scene_no": 2, "title": "画室", "plot": "赶稿", "emotion": "爆发",
         "dialogue": [{"character": "林小满", "line": "画完了。"}], "duration_sec": 90},
    ]
    # 仅 1 个角色出场：陈默零出场 → warning
    report = run_health_check(_project(), _script(scenes), ["林小满", "陈默"])
    assert any("角色「陈默」未在任何场次出场" in m for m in _issue_messages(report))

    # 全部出场 → 无「未在任何场次出场」告警
    scenes_full = [
        {**scenes[0], "dialogue": [{"character": "林小满", "line": "一起加油。"}]},
        {**scenes[1], "dialogue": [{"character": "陈默", "line": "我来帮你。"}]},
    ]
    report2 = run_health_check(_project(), _script(scenes_full), ["林小满", "陈默"])
    assert not any("未在任何场次出场" in m for m in _issue_messages(report2))


# ---------------------------------------------------------------------------
# 2. 对白过长
# ---------------------------------------------------------------------------
def test_duibai_guochang():
    """对白过长：单条 150 字对白 → warning 且带场次定位。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    long_line = "字" * 150
    scenes = [
        {"scene_no": 1, "title": "天台", "plot": "相遇", "action": "远眺",
         "emotion": "爆发", "dialogue": [{"character": "林小满", "line": long_line}],
         "duration_sec": 180},
    ]
    report = run_health_check(_project(), _script(scenes))
    hits = [it for it in report["issues"] if "建议拆分" in it["message"]]
    assert hits and hits[0]["location"] == "镜头1"
    assert "第1条对白" in hits[0]["message"]


# ---------------------------------------------------------------------------
# 3. 缺动作描写（新/旧 Schema 差异）
# ---------------------------------------------------------------------------
def test_que_dongzuo_miaoxie():
    """缺动作描写：新 Schema 空 action → warning；旧 Schema（无 action 键）不报。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    # 新 Schema：一场 action 有值、一场 action 为空 → 空的那场命中
    new_scenes = [
        {"scene_no": 1, "title": "A", "action": "推门而入", "plot": "p", "emotion": "爆发",
         "dialogue": [{"character": "林小满", "line": "你好。"}], "duration_sec": 90},
        {"scene_no": 2, "title": "B", "action": "", "plot": "p", "emotion": "爆发",
         "dialogue": [{"character": "林小满", "line": "再见。"}], "duration_sec": 90},
    ]
    report = run_health_check(_project(), _script(new_scenes))
    hits = [it for it in report["issues"] if "缺少动作描写" in it["message"]]
    assert len(hits) == 1 and hits[0]["location"] == "镜头2"

    # 旧 Schema：全部场次无 action 键 → 自动跳过该规则
    old_scenes = [
        {"scene_no": 1, "title": "A", "plot": "p", "emotion": "爆发",
         "dialogue": [{"character": "林小满", "line": "你好。"}], "duration_sec": 90},
        {"scene_no": 2, "title": "B", "plot": "p", "emotion": "爆发",
         "dialogue": [{"character": "林小满", "line": "再见。"}], "duration_sec": 90},
    ]
    report_old = run_health_check(_project(), _script(old_scenes))
    assert not any("缺少动作描写" in m for m in _issue_messages(report_old))


# ---------------------------------------------------------------------------
# 4. 缺高潮点
# ---------------------------------------------------------------------------
def test_que_gaochao_dian():
    """缺高潮点：末场 emotion 无峰值词且 plot 无感叹号 → info。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    scenes = [
        {"scene_no": 1, "title": "A", "plot": "平淡叙述", "action": "x", "emotion": "平静",
         "dialogue": [{"character": "林小满", "line": "嗯。"}], "duration_sec": 90},
        {"scene_no": 2, "title": "B", "plot": "收尾叙述", "action": "y", "emotion": "平静",
         "dialogue": [{"character": "林小满", "line": "好。"}], "duration_sec": 90},
    ]
    report = run_health_check(_project(), _script(scenes))
    infos = [it for it in report["issues"] if it["level"] == "info"]
    assert any("缺少情绪高潮点" in it["message"] for it in infos)


# ---------------------------------------------------------------------------
# 5. 时长阈值收紧到 ±10%
# ---------------------------------------------------------------------------
def test_shichang_yuzhi():
    """时长阈值：偏差 12% → error 命中（旧 20% 会漏报）；偏差 8% → 不报。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    def _scenes_with_total(total: int) -> List[Dict[str, Any]]:
        """构造合计时长为 total 的两场（内部函数）。

        Args:
            total: 场次时长合计（秒）。

        Returns:
            List[Dict[str, Any]]: 两场次列表。

        Raises:
            无：纯结构构造。
        """
        half = total // 2
        return [
            {"scene_no": 1, "title": "A", "plot": "p!", "action": "x", "emotion": "爆发",
             "dialogue": [], "duration_sec": half},
            {"scene_no": 2, "title": "B", "plot": "q!", "action": "y", "emotion": "爆发",
             "dialogue": [], "duration_sec": total - half},
        ]

    # 偏差 12%：100 → 112 → error
    report = run_health_check(_project(100), _script(_scenes_with_total(112)))
    errors = [it for it in report["issues"] if it["level"] == "error"]
    assert errors and "超过 10%" in errors[0]["message"]

    # 偏差 8%：100 → 108 → 无 error
    report2 = run_health_check(_project(100), _script(_scenes_with_total(108)))
    assert not [it for it in report2["issues"] if it["level"] == "error"]


# ---------------------------------------------------------------------------
# 6. 对白语速交叉校验
# ---------------------------------------------------------------------------
def test_duibai_yusu_jiaocha():
    """对白语速交叉校验：对白 40 字（≈10s）vs 场景 60s → warning。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    scenes = [
        {"scene_no": 1, "title": "A", "plot": "p!", "action": "x", "emotion": "爆发",
         "dialogue": [{"character": "林小满", "line": "字" * 40}], "duration_sec": 60},
    ]
    report = run_health_check(_project(60), _script(scenes))
    assert any("配音时长可能不符" in m for m in _issue_messages(report))


# ---------------------------------------------------------------------------
# 7. 按集体检
# ---------------------------------------------------------------------------
async def test_anji_tijian(client: httpx.AsyncClient, auth, db):
    """按集体检：不传 episode_id 聚合各集且 location 带集号；传 episode_id
    仅体检该集（location 不带前缀）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直插 episodes / scripts 造数）。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    project = await create_project(client, auth["headers"], target_duration=180)
    pid = project["id"]

    ep1 = Episode(project_id=pid, sequence=1, title="第一集", status="scripted")
    ep2 = Episode(project_id=pid, sequence=2, title="第二集", status="scripted")
    db.add_all([ep1, ep2])
    await db.flush()
    # 第 1 集：对白 150 字 → 「第1集 · 镜头1」对白过长
    db.add(Script(project_id=pid, episode_id=ep1.id, version=1, content={
        "scenes": [{"scene_no": 1, "title": "A", "plot": "p", "emotion": "爆发",
                    "dialogue": [{"character": "林小满", "line": "字" * 150}],
                    "duration_sec": 180}]}))
    # 第 2 集：action 为空（新 Schema）→ 「第2集 · 镜头1」缺动作描写
    db.add(Script(project_id=pid, episode_id=ep2.id, version=1, content={
        "scenes": [{"scene_no": 1, "title": "B", "plot": "p", "action": "", "emotion": "爆发",
                    "dialogue": [], "duration_sec": 180}]}))
    await db.commit()

    # 聚合体检（不传 episode_id）：两个集的问题都汇总，location 带集号前缀
    resp = await client.post(f"/api/projects/{pid}/script/health", headers=auth["headers"])
    assert resp.status_code == 200, resp.text
    locations = [it["location"] for it in resp.json()["issues"]]
    assert "第1集 · 镜头1" in locations
    assert "第2集 · 镜头1" in locations

    # 指定 episode_id：仅体检该集，location 无集号前缀
    resp_one = await client.post(
        f"/api/projects/{pid}/script/health",
        headers=auth["headers"],
        params={"episode_id": ep1.id},
    )
    assert resp_one.status_code == 200, resp_one.text
    one_locations = [it["location"] for it in resp_one.json()["issues"]]
    assert "镜头1" in one_locations
    assert not any(loc.startswith("第") for loc in one_locations)


# ---------------------------------------------------------------------------
# 8. 场次级 source 合并（快速模式）
# ---------------------------------------------------------------------------
def _valid_script_3scenes() -> Dict[str, Any]:
    """构造可通过 ScriptContent 校验的 3 场剧本（内部函数）。

    Returns:
        Dict[str, Any]: 含 title/logline/outline（3 幕）/scenes（3 场）的结构。

    Raises:
        无：纯结构构造。
    """
    return {
        "title": "天台之约",
        "logline": "两位少女约定一起成为漫画家",
        "outline": [
            {"act": "第一幕", "summary": "天台相遇立下约定"},
            {"act": "第二幕", "summary": "共同赶稿磨合分歧"},
            {"act": "第三幕", "summary": "赛场获奖兑现约定"},
        ],
        "scenes": [
            {"scene_no": 1, "title": "天台", "plot": "两人初遇并约定",
             "dialogue": [{"character": "林小满", "line": "我们一起拿金奖吧。"}],
             "duration_sec": 60},
            {"scene_no": 2, "title": "画室", "plot": "深夜赶稿互相扶持",
             "dialogue": [{"character": "陈默", "line": "分镜我来帮你。"}],
             "duration_sec": 60},
            {"scene_no": 3, "title": "赛场", "plot": "作品获奖",
             "dialogue": [{"character": "林小满", "line": "我们做到了。"}],
             "duration_sec": 60},
        ],
    }


async def test_changci_source_hebing(client: httpx.AsyncClient, auth, monkeypatch):
    """场次级 source 合并：快速模式生成 3 场（全 ai）→ 改 1 场保存 →
    该场 user、其余 ai；content_plain / unwrap_scenes 解包正常。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 用例级补丁（mock llm.chat_stream 输出合法剧本）。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    async def fake_chat_stream(state_aware, messages, **kwargs):
        """替换 AgentLLM.chat_stream：单块输出合法剧本 JSON（内部函数）。

        Args:
            state_aware: 适配器占位参数（None）。
            messages: OpenAI 格式消息列表（忽略）。
            **kwargs: 其余关键字参数（忽略）。

        Yields:
            str: 完整剧本 JSON 文本。

        Raises:
            无。
        """
        yield json.dumps(_valid_script_3scenes(), ensure_ascii=False)

    monkeypatch.setattr(llm_adapter.llm, "chat_stream", fake_chat_stream)

    project = await create_project(client, auth["headers"], title="快速模式项目")
    pid = project["id"]
    resp_input = await client.put(
        f"/api/projects/{pid}/script/input",
        headers=auth["headers"],
        json={"mode": "idea", "raw_text": _SAMPLE_RAW},
    )
    assert resp_input.status_code == 200, resp_input.text

    gen = await client.post(
        f"/api/projects/{pid}/script/generate/stream", headers=auth["headers"])
    assert gen.status_code == 200, gen.text
    done = next(d for n, d in parse_sse_events(gen.text) if n == "done" and d)
    gen_scenes = done["content"]["scenes"]["value"]
    assert [s["source"] for s in gen_scenes] == ["ai", "ai", "ai"]

    # 改第 1 场 plot，其余两场原样；顶层字段用生成值
    plain = {
        "title": done["content"]["title"]["value"],
        "logline": done["content"]["logline"]["value"],
        "outline": done["content"]["outline"]["value"],
        "scenes": gen_scenes,
    }
    plain["scenes"][0]["plot"] = "用户改写：两人在雨中初遇"

    saved = await client.put(
        f"/api/projects/{pid}/script", headers=auth["headers"],
        json={"content": plain, "base_version": done["version"]})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["content"]["source"] == "user"

    # content_plain 解包 + unwrap_scenes 提取：场次级 source 差异化
    unpacked = content_plain(body["content"])
    scenes_out = unwrap_scenes(unpacked)
    assert len(scenes_out) == 3
    assert scenes_out[0]["source"] == "user"
    assert scenes_out[1]["source"] == "ai"
    assert scenes_out[2]["source"] == "ai"
    assert scenes_out[0]["plot"] == "用户改写：两人在雨中初遇"


# ---------------------------------------------------------------------------
# 9. 忠实度策略
# ---------------------------------------------------------------------------
def test_fidelity_directive_three_levels():
    """free/outline/faithful 三档 directive 文案互异。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    free = build_fidelity_directive(SimpleNamespace(fidelity="free"))
    outline = build_fidelity_directive(SimpleNamespace(fidelity="outline"))
    faithful = build_fidelity_directive(SimpleNamespace(fidelity="faithful"))
    assert len({free, outline, faithful}) == 3
    assert "自由发挥创意" in free
    assert "大纲脉络" in outline
    assert "不得新增主要角色" in faithful


async def test_faithful_episodes_retry(client: httpx.AsyncClient, auth, monkeypatch):
    """faithful 下 mock LLM 返回含名单外角色的集大纲 → 触发重试（调用 ≥2 次）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 用例级补丁（依次替换角色/大纲 LLM 输出）。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    project = await create_project(
        client, auth["headers"], title="忠实改编项目", fidelity="faithful")
    pid = project["id"]
    resp_input = await client.put(
        f"/api/projects/{pid}/script/input",
        headers=auth["headers"],
        json={"mode": "idea", "raw_text": _SAMPLE_RAW},
    )
    assert resp_input.status_code == 200, resp_input.text

    # 角色生成 + 确认（2 角色）
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_chat(_two_characters_payload()))
    gen = await client.post(
        f"/api/projects/{pid}/script/characters/generate", headers=auth["headers"])
    assert gen.status_code == 200, gen.text
    confirm = await client.post(
        f"/api/projects/{pid}/script/characters/confirm", headers=auth["headers"],
        json={"characters": [{"name": "林小满"}, {"name": "陈默"}]})
    assert confirm.status_code == 200, confirm.text

    # 大纲 LLM 返回含 3 个名单外角色的集大纲（超过阈值 2 → 触发重试）
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        sp_mod, "_llm_chat", _mock_chat(_episodes_with_extra_payload(), calls))
    plan = await client.post(
        f"/api/projects/{pid}/script/episodes/plan", headers=auth["headers"],
        json={"target_episodes": 1})
    assert plan.status_code == 200, plan.text
    # 触发重试：大纲 LLM 调用 ≥2 次
    assert len(calls) >= 2
    # 规整后名单外角色被过滤，仅保留名单内角色
    characters = plan.json()["episodes"][0]["characters"]
    assert all(name in ("林小满", "陈默") for name in characters)


def _two_characters_payload() -> str:
    """构造含 2 个角色的合法角色 LLM 输出 JSON（内部函数）。

    Returns:
        str: {"characters": [...]} 的 JSON 文本。

    Raises:
        无：纯结构构造。
    """
    return json.dumps({"characters": [
        {"name": "林小满", "role_type": "lead", "personality": "倔强善良"},
        {"name": "陈默", "role_type": "support", "personality": "沉稳寡言"},
    ]}, ensure_ascii=False)


def _episodes_with_extra_payload() -> str:
    """构造含 3 个名单外角色的单集大纲 LLM 输出 JSON（内部函数）。

    Returns:
        str: {"episodes": [1 集]} 的 JSON 文本（characters 含 3 个未知角色）。

    Raises:
        无：纯结构构造。
    """
    return json.dumps({"episodes": [{
        "title": "第一集：天台之约",
        "logline": "两个少女在天台相遇",
        "core_event": "天台立约",
        "characters": ["林小满", "陈默", "张大炮", "李铁柱", "王二"],
        "emotion_curve": "平静→爆发→释然",
        "duration_sec": 90,
    }]}, ensure_ascii=False)


def _mock_chat(payload: str, calls: List[Dict[str, Any]] | None = None):
    """构造可替换 script_pipeline._llm_chat 的异步假函数（内部工厂）。

    Args:
        payload: 每次 LLM 调用返回的固定文本。
        calls: 可选收集器；传入时每次调用追加 {"agent_name": ...}。

    Returns:
        Callable: 签名兼容 _llm_chat(messages, *, temperature, agent_name)。

    Raises:
        无。
    """
    async def fake_chat(messages, *, temperature, **kwargs):
        """替换 _llm_chat：恒返回预置文本（内部函数）。

        Args:
            messages: OpenAI 格式消息列表（忽略）。
            temperature: 采样温度（忽略）。
            **kwargs: 其余关键字参数（记录 agent_name）。

        Returns:
            str: 预置 payload。

        Raises:
            无。
        """
        if calls is not None:
            calls.append({"agent_name": kwargs.get("agent_name")})
        return payload

    return fake_chat
