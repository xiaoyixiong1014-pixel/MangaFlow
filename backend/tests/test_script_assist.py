"""P4 对话式改写通道 assist 双接口测试模块（文件级说明，redesign-script-workspace Task 5）。

覆盖链路（全部 mock LLM：monkeypatch app.agents.script_pipeline 模块级
_llm_chat / _llm_chat_stream，与 test_script_optimize.py 同风格；前置链路
复用 test_script_optimize / test_script_setting 的辅助函数）：
- scope=setting 预览：mock 改写 LLM 返回 1 条 change（path=logline）→
  返回片段（original 以当前库值覆盖、diff_lines 含 insert、summary 透传、
  agent_name=script_assist / temperature=0.5）、**story_setting 库值不变**；
- scope=setting 采纳：accepts 回传片段 → applied=1、logline 更新、
  source=user、写 assist_applied 快照、phase 不变；
- scope=episode_outline 预览+采纳：仅该集变化（core_event 更新，
  title/logline/emotion_curve/duration_sec 原样）、写 assist_applied 快照；
- scope=episode_body 预览：复用 optimize 通道（agent_name=script_optimize）、
  片段口径一致、**不落库**；
- apply 片段 original 与当前库值不符（并发防护）→ applied=0、skipped=1、
  无快照、（episode_body）版本不递增；
- 越权：用户 B 访问用户 A 的项目一律 404；剧集不存在/不归属 404；
- 非法 scope / 空 instruction / episode 作用域 target 缺失或非整数 → 422；
- setting 未生成 409「请先生成故事设定」；phase=body 时 setting 409
  「故事设定已锁定」。
"""

import json
from typing import Any, Dict, List

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import script_pipeline as sp_mod
from app.models import Episode, Project, Script, VersionSnapshot
from tests.conftest import create_project, register_user
from tests.test_script_optimize import (
    _episode_plan_payload,
    _make_project_with_input,
    _mock_llm_chat,
    _setup_body_generated,
    _setup_body_phase,
    _two_characters_payload,
)
from tests.test_script_setting import (
    _CONFLICT_A,
    _LOGELINE_A,
    _WORLDVIEW_A,
    _make_project_with_input as _make_setting_project,
    _setting_payload,
)


def _assist_payload(changes: List[dict], summary: str = "按指令完成改写") -> str:
    """构造改写 LLM 输出 JSON 字符串（内部函数）。

    Args:
        changes: changes 数组元素列表（path/original/proposed/reason）。
        summary: LLM 改动概括文本。

    Returns:
        str: {"changes": [...], "summary": ...} 的 JSON 文本。

    Raises:
        无：纯结构构造。
    """
    return json.dumps({"changes": changes, "summary": summary}, ensure_ascii=False)


async def _setup_setting_phase(client: httpx.AsyncClient, auth: Dict[str, Any],
                               monkeypatch) -> int:
    """辅助：创建项目并生成故事设定（phase=characters），返回项目 ID（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: pytest 夹具（generate 阶段替换 _llm_chat 为设定 payload）。

    Returns:
        int: 项目 ID。

    Raises:
        AssertionError: 链路任一接口返回非 200 时中断。
    """
    project = await _make_setting_project(client, auth)
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_setting_payload()))
    resp = await client.post(
        f"/api/projects/{project['id']}/script/setting/generate",
        headers=auth["headers"],
    )
    assert resp.status_code == 200, resp.text
    return project["id"]


async def _setup_outline_phase(client: httpx.AsyncClient, auth: Dict[str, Any],
                               monkeypatch) -> tuple:
    """辅助：走真实接口链建单集项目并推进到 phase=outline（不确认大纲，内部函数）。

    链路：建项目+输入 → mock 角色 LLM 生成并确认 2 角色 → mock 大纲 LLM
    生成 1 集（不调 plan/confirm，保持 outline 阶段）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: pytest 夹具（替换 _llm_chat）。

    Returns:
        tuple: (project_id, episode_id) 二元组。

    Raises:
        AssertionError: 链路任一接口返回非 200 或阶段未达 outline 时中断。
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
    assert plan.json()["phase"] == "outline", plan.text
    eid = plan.json()["episodes"][0]["episode_id"]
    return pid, eid


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


async def _fetch_snapshots(db: AsyncSession, project_id: int,
                           stage: str = "assist_applied") -> List[VersionSnapshot]:
    """辅助：查询指定阶段的版本快照列表（内部函数）。

    Args:
        db: 测试库会话。
        project_id: 项目 ID。
        stage: 快照阶段（缺省 assist_applied）。

    Returns:
        List[VersionSnapshot]: 快照 ORM 列表（按 id 升序）。

    Raises:
        无：纯查询。
    """
    rows = await db.execute(
        select(VersionSnapshot).where(
            VersionSnapshot.project_id == project_id,
            VersionSnapshot.stage == stage,
        ).order_by(VersionSnapshot.id)
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# 1. scope=setting 预览：片段化 diff + 不落库
# ---------------------------------------------------------------------------
async def test_assist_setting_preview_no_persist(client, auth, db, monkeypatch):
    """setting 预览：返回 1 片段（original 以当前库值覆盖、diff_lines 含
    insert、summary 透传、LLM 挂点 script_assist/0.5），story_setting
    库值不变（不落库）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言落库未变）。
        monkeypatch: 替换 _llm_chat（generate / assist 两次）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid = await _setup_setting_phase(client, auth, monkeypatch)

    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(
        _assist_payload([{
            "path": "logline",
            "original": "LLM 乱写的原文",
            "proposed": "深巷旧宅中，两个性格互补的少女为完成亡友遗作约定参赛。",
            "reason": "加入悬念钩子",
        }]), calls))
    resp = await client.post(
        f"/api/projects/{pid}/script/assist",
        headers=auth["headers"],
        json={"scope": "setting", "target": "all", "instruction": "让故事更悬疑"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "setting"
    assert body["target"] == "all"
    assert body["count"] == 1
    assert body["summary"] == "按指令完成改写"
    change = body["changes"][0]
    assert change["path"] == "logline"
    assert change["kind"] == "field"
    # original 以当前库值为准（覆盖 LLM 乱写的原文）
    assert change["original"] == _LOGELINE_A
    assert change["proposed"].startswith("深巷旧宅")
    assert change["id"]
    ops = [line["op"] for line in change["diff_lines"]]
    assert "insert" in ops, ops
    # LLM 挂点：agent_name=script_assist、temperature=0.5
    assert calls and calls[0]["agent_name"] == "script_assist"
    assert calls[0]["temperature"] == 0.5

    # 不落库：story_setting 三段与 source 均未变
    proj_row = await _fetch_project(db, pid)
    assert proj_row.story_setting["logline"] == _LOGELINE_A
    assert proj_row.story_setting["core_conflict"] == _CONFLICT_A
    assert proj_row.story_setting["worldview"] == _WORLDVIEW_A
    assert proj_row.story_setting["source"] == "ai"
    assert await _fetch_snapshots(db, pid) == []


# ---------------------------------------------------------------------------
# 2. scope=setting 采纳：source=user + assist_applied 快照
# ---------------------------------------------------------------------------
async def test_assist_setting_apply_snapshot(client, auth, db, monkeypatch):
    """setting 采纳：applied=1、logline 更新、source 置 user、
    assist_applied 快照写入、phase 不变。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言落库与快照）。
        monkeypatch: 替换 _llm_chat（generate 用）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid = await _setup_setting_phase(client, auth, monkeypatch)
    new_logline = "采纳后的新一句话故事。"

    resp = await client.post(
        f"/api/projects/{pid}/script/assist/apply",
        headers=auth["headers"],
        json={"scope": "setting", "target": "all",
              "accepts": [{"id": "frag1", "path": "logline",
                           "original": _LOGELINE_A, "proposed": new_logline}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] == 1
    assert body["skipped"] == 0
    assert body["snapshot_id"] is not None
    assert body["phase"] == "characters"  # 采纳不改变阶段
    assert body["version"] is None
    assert body["content"] is None

    # 落库：logline 更新、其余字段保留、source=user、updated_at 刷新
    proj_row = await _fetch_project(db, pid)
    assert proj_row.story_setting["logline"] == new_logline
    assert proj_row.story_setting["core_conflict"] == _CONFLICT_A
    assert proj_row.story_setting["worldview"] == _WORLDVIEW_A
    assert proj_row.story_setting["source"] == "user"
    assert proj_row.story_setting["updated_at"]
    assert proj_row.script_phase == "characters"

    # 版本快照：assist_applied 恰 1 条
    snapshots = await _fetch_snapshots(db, pid)
    assert len(snapshots) == 1
    assert snapshots[0].summary["applied"] == 1
    assert snapshots[0].summary["paths"] == ["logline"]


# ---------------------------------------------------------------------------
# 3. scope=episode_outline 预览 + 采纳：仅该集变化
# ---------------------------------------------------------------------------
async def test_assist_episode_outline_preview_and_apply(client, auth, db, monkeypatch):
    """episode_outline 预览+采纳：core_event 片段（original=当前库值）→
    采纳后仅该集 core_event 变化，title/logline/emotion_curve/duration_sec
    原样，写 assist_applied 快照、phase 保持 outline。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言剧集行落库）。
        monkeypatch: 替换 _llm_chat（角色/大纲/assist 三段）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, eid = await _setup_outline_phase(client, auth, monkeypatch)
    new_core_event = "天台立约当晚遭遇暴雨，两人被迫在画室完成报名。"

    # ---------- 预览 ----------
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_assist_payload(
        [{"path": "core_event", "original": "乱写", "proposed": new_core_event,
          "reason": "强化冲突"}], summary="强化核心事件")))
    preview = await client.post(
        f"/api/projects/{pid}/script/assist",
        headers=auth["headers"],
        json={"scope": "episode_outline", "target": eid, "instruction": "冲突更强"},
    )
    assert preview.status_code == 200, preview.text
    pbody = preview.json()
    assert pbody["scope"] == "episode_outline"
    assert pbody["target"] == str(eid)
    assert pbody["count"] == 1
    change = pbody["changes"][0]
    assert change["path"] == "core_event"
    assert change["original"] == "天台立约，共同报名新人奖"
    assert change["proposed"] == new_core_event

    # ---------- 采纳 ----------
    apply_resp = await client.post(
        f"/api/projects/{pid}/script/assist/apply",
        headers=auth["headers"],
        json={"scope": "episode_outline", "target": eid,
              "accepts": [{"id": change["id"], "path": "core_event",
                           "original": change["original"],
                           "proposed": change["proposed"]}]},
    )
    assert apply_resp.status_code == 200, apply_resp.text
    body = apply_resp.json()
    assert body["applied"] == 1
    assert body["skipped"] == 0
    assert body["snapshot_id"] is not None
    assert body["phase"] == "outline"  # 采纳不改变阶段

    # 落库：仅该集 core_event 变化，其余字段原样
    episode = await db.get(Episode, eid)
    assert episode.project_id == pid
    outline = episode.outline
    assert outline["core_event"] == new_core_event
    assert outline["characters"] == ["林小满", "陈默"]
    assert outline["emotion_curve"] == "热血"
    assert outline["duration_sec"] == 90
    assert episode.title == "第一集：天台之约"
    assert episode.logline == "两个少女在天台相遇并约定一起成为漫画家"

    # 版本快照：assist_applied 恰 1 条
    snapshots = await _fetch_snapshots(db, pid)
    assert len(snapshots) == 1
    assert snapshots[0].summary["scope"] == "episode_outline"


# ---------------------------------------------------------------------------
# 4. scope=episode_body 预览：复用 optimize 口径 + 不落库
# ---------------------------------------------------------------------------
async def test_assist_episode_body_preview_reuses_optimize(client, auth, db, monkeypatch):
    """episode_body 预览：内部复用 optimize 通道（LLM 挂点
    agent_name=script_optimize），返回与 optimize 同口径的片段，
    DB 正文不落库（version 不变）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言 scripts 行未变）。
        monkeypatch: 替换 _llm_chat / _llm_chat_stream（前置链 + optimize）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, eid, done = await _setup_body_generated(client, auth, monkeypatch)
    assert done["version"] == 1

    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(
        json.dumps({"changes": [{
            "path": "scenes[0].action",
            "original": "LLM 乱写的原文",
            "proposed": "夜风呼啸，两人并肩站上天台边缘远眺城市。",
            "reason": "增强画面感",
        }]}, ensure_ascii=False), calls))
    resp = await client.post(
        f"/api/projects/{pid}/script/assist",
        headers=auth["headers"],
        json={"scope": "episode_body", "target": eid, "instruction": "画面感更强"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "episode_body"
    assert body["target"] == str(eid)
    assert body["count"] == 1
    change = body["changes"][0]
    assert change["path"] == "scenes[0].action"
    # original 以当前库值为准（optimize 同口径）
    assert change["original"] == "两人站上天台边缘远眺城市"
    # 复用 optimize 通道：agent_name=script_optimize（非 script_assist）
    assert calls and calls[0]["agent_name"] == "script_optimize"

    # 不落库：scripts 行版本与内容均未变
    db.expire_all()
    row = await db.get(Script, done["script_id"])
    assert row.version == 1
    assert row.content["scenes"]["value"][0]["action"] == "两人站上天台边缘远眺城市"


# ---------------------------------------------------------------------------
# 5. apply 片段 original 不一致 → skipped、无快照、版本不递增
# ---------------------------------------------------------------------------
async def test_assist_apply_mismatch_skipped_no_snapshot(client, auth, db, monkeypatch):
    """并发防护：accepts 的 original 与当前库值不符 → applied=0、skipped=1、
    snapshot_id=None；setting 库值不变且无 assist_applied 快照；
    episode_body 侧版本同样不递增、无快照。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言落库未变与快照缺失）。
        monkeypatch: 替换 _llm_chat / _llm_chat_stream（前置链）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    # ---------- setting 侧 ----------
    pid = await _setup_setting_phase(client, auth, monkeypatch)
    resp = await client.post(
        f"/api/projects/{pid}/script/assist/apply",
        headers=auth["headers"],
        json={"scope": "setting", "target": "all",
              "accepts": [{"id": "c1", "path": "logline",
                           "original": "被并发改动过的旧设定",
                           "proposed": "新的设定。"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] == 0
    assert body["skipped"] == 1
    assert body["snapshot_id"] is None

    proj_row = await _fetch_project(db, pid)
    assert proj_row.story_setting["logline"] == _LOGELINE_A
    assert proj_row.story_setting["source"] == "ai"
    assert await _fetch_snapshots(db, pid) == []

    # ---------- episode_body 侧 ----------
    body_pid, body_eid, done = await _setup_body_generated(client, auth, monkeypatch)
    body_resp = await client.post(
        f"/api/projects/{body_pid}/script/assist/apply",
        headers=auth["headers"],
        json={"scope": "episode_body", "target": body_eid,
              "accepts": [{"id": "c1", "path": "scenes[0].action",
                           "original": "被并发改动过的旧内容",
                           "proposed": "新的动作描述。"}]},
    )
    assert body_resp.status_code == 200, body_resp.text
    bbody = body_resp.json()
    assert bbody["applied"] == 0
    assert bbody["skipped"] == 1
    assert bbody["snapshot_id"] is None
    assert bbody["version"] == 1  # 版本不递增

    db.expire_all()
    row = await db.get(Script, done["script_id"])
    assert row.version == 1
    assert await _fetch_snapshots(db, body_pid) == []


# ---------------------------------------------------------------------------
# 6. 越权 404
# ---------------------------------------------------------------------------
async def test_assist_forbidden_404(client, auth, monkeypatch):
    """越权：用户 B 访问用户 A 的项目（预览/采纳）一律 404；episode 作用域
    的剧集不存在/不归属同样 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 用户 A（项目归属者）。
        monkeypatch: 替换 _llm_chat（即便通过归属校验也不真调 LLM）。

    Returns:
        None。

    Raises:
        AssertionError: 任一状态码非 404 时中断。
    """
    pid = await _setup_setting_phase(client, auth, monkeypatch)

    other = await register_user(client, prefix="other")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    preview = await client.post(
        f"/api/projects/{pid}/script/assist",
        headers=other_headers,
        json={"scope": "setting", "instruction": "越权预览"},
    )
    assert preview.status_code == 404, preview.text

    apply_resp = await client.post(
        f"/api/projects/{pid}/script/assist/apply",
        headers=other_headers,
        json={"scope": "setting", "accepts": []},
    )
    assert apply_resp.status_code == 404, apply_resp.text

    # episode 作用域：已达 outline 阶段的项目上 target 指向不存在的剧集 → 404
    outline_pid, _eid = await _setup_outline_phase(client, auth, monkeypatch)
    missing = await client.post(
        f"/api/projects/{outline_pid}/script/assist",
        headers=auth["headers"],
        json={"scope": "episode_outline", "target": 99999, "instruction": "x"},
    )
    assert missing.status_code == 404, missing.text

    # episode_body 作用域：已达 body 阶段的项目上 target 指向不存在的剧集 → 404
    body_pid, _body_eid = await _setup_body_phase(client, auth, monkeypatch)
    apply_missing = await client.post(
        f"/api/projects/{body_pid}/script/assist/apply",
        headers=auth["headers"],
        json={"scope": "episode_body", "target": 99999, "accepts": []},
    )
    assert apply_missing.status_code == 404, apply_missing.text


# ---------------------------------------------------------------------------
# 7. 非法 scope / target / instruction → 422
# ---------------------------------------------------------------------------
async def test_assist_invalid_scope_and_target_422(client, auth, monkeypatch):
    """Pydantic 校验：scope 非法、instruction 为空、episode 作用域 target
    缺失或非整数 → 一律 422（预览与采纳双侧）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 替换 _llm_chat（保证通过校验的请求也不真调 LLM）。

    Returns:
        None。

    Raises:
        AssertionError: 任一状态码非 422 时中断。
    """
    pid = await _setup_setting_phase(client, auth, monkeypatch)

    bad_scope = await client.post(
        f"/api/projects/{pid}/script/assist",
        headers=auth["headers"],
        json={"scope": "world", "instruction": "x"},
    )
    assert bad_scope.status_code == 422, bad_scope.text

    empty_instruction = await client.post(
        f"/api/projects/{pid}/script/assist",
        headers=auth["headers"],
        json={"scope": "setting", "instruction": ""},
    )
    assert empty_instruction.status_code == 422, empty_instruction.text

    missing_target = await client.post(
        f"/api/projects/{pid}/script/assist",
        headers=auth["headers"],
        json={"scope": "episode_outline", "instruction": "x"},
    )
    assert missing_target.status_code == 422, missing_target.text

    str_target = await client.post(
        f"/api/projects/{pid}/script/assist",
        headers=auth["headers"],
        json={"scope": "episode_outline", "target": "abc", "instruction": "x"},
    )
    assert str_target.status_code == 422, str_target.text

    apply_bad_scope = await client.post(
        f"/api/projects/{pid}/script/assist/apply",
        headers=auth["headers"],
        json={"scope": "world", "accepts": []},
    )
    assert apply_bad_scope.status_code == 422, apply_bad_scope.text


# ---------------------------------------------------------------------------
# 8. setting 未生成 409 / phase=body 锁定 409
# ---------------------------------------------------------------------------
async def test_assist_setting_empty_and_locked_409(client, auth, db, monkeypatch):
    """守卫：未生成设定时 setting 作用域 409「请先生成故事设定」；phase=body
    时 409「故事设定已锁定」（预览与采纳双侧）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（直插 body 阶段项目造数）。
        monkeypatch: 替换 _llm_chat（保证通过守卫的请求也不真调 LLM）。

    Returns:
        None。

    Raises:
        AssertionError: 任一状态码非 409 时中断。
    """
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_assist_payload([]), calls))

    # 未生成设定的项目：409 请先生成故事设定
    project = await create_project(client, auth["headers"], title="无设定项目")
    empty_preview = await client.post(
        f"/api/projects/{project['id']}/script/assist",
        headers=auth["headers"],
        json={"scope": "setting", "instruction": "x"},
    )
    assert empty_preview.status_code == 409, empty_preview.text
    assert "请先生成故事设定" in empty_preview.json()["detail"]

    empty_apply = await client.post(
        f"/api/projects/{project['id']}/script/assist/apply",
        headers=auth["headers"],
        json={"scope": "setting", "accepts": []},
    )
    assert empty_apply.status_code == 409, empty_apply.text
    assert "请先生成故事设定" in empty_apply.json()["detail"]

    # phase=body 的项目：409 故事设定已锁定（LLM 未被调用）
    locked = Project(user_id=auth["user_id"], title="正文阶段项目",
                     status="draft", script_phase="body")
    db.add(locked)
    await db.commit()
    locked_id = locked.id

    locked_preview = await client.post(
        f"/api/projects/{locked_id}/script/assist",
        headers=auth["headers"],
        json={"scope": "setting", "instruction": "x"},
    )
    assert locked_preview.status_code == 409, locked_preview.text
    assert "故事设定已锁定" in locked_preview.json()["detail"]

    locked_apply = await client.post(
        f"/api/projects/{locked_id}/script/assist/apply",
        headers=auth["headers"],
        json={"scope": "setting", "accepts": []},
    )
    assert locked_apply.status_code == 409, locked_apply.text
    assert "故事设定已锁定" in locked_apply.json()["detail"]

    assert calls == []  # 守卫拦截：LLM 未被调用
