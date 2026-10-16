"""小说改编链路接口测试模块（文件级说明，P-1 范式级重构 Task 9 / P-7）。

覆盖链路（LLM 全部 mock：monkeypatch app.agents.script_pipeline 模块级
_llm_chat 包装函数，返回预置合法 JSON 字符串）：
- 上传解析：multipart txt 上传 → 章节落 project.config["novel_chapters"]、
  返回 {chapter_count, chapters}；GET 读取一致；raw_text 回写 + input_mode=novel；
- JSON 模式上传（{raw_text, filename}）同样解析成功；
- 非法扩展名（.pdf）→ 400；
- 未解析章节时 POST episodes/plan(from_chapters=true) → 409（请先解析小说章节）；
- from_chapters 生成：3 章 → 缺省每章一集（3 集），章节摘要经 chapter_summaries
  通道注入 LLM 消息（断言 messages 含章节标题）；
- from_chapters 指定 target=1 → 合并相邻章节为 1 集；
- 长文截断消除：3 章各 3000 字 → build_novel_context 输出 > 4000 字且含第三章标题；
- 越权：用户 B 访问用户 A 项目的解析/列表接口一律 404。
"""

import json
from typing import Any, Dict, List, Optional

import httpx

from app.agents import script_pipeline as sp_mod
from app.models import Project
from app.services.script_service import build_novel_context
from tests.conftest import create_project, register_user

# 3 章 txt 样本（标题行长 < 40，命中中文/英文章节标题正则）
_TXT_3_CHAPTERS = (
    "第一章 启程\n"
    + "启程正文，少年踏上旅途。" * 10
    + "\n\n"
    "第2章 转折\n"
    + "转折正文，遇见师父。" * 10
    + "\n\n"
    "Chapter 3 终局\n"
    + "终局正文，故事收束。" * 10
    + "\n"
)


def _characters_payload() -> str:
    """构造含 2 个角色的合法 LLM 输出 JSON 字符串（内部函数）。

    Returns:
        str: {"characters": [林小满, 陈默]} 的 JSON 文本。

    Raises:
        无：纯结构构造。
    """
    return json.dumps({
        "characters": [
            {"name": "林小满", "role_type": "lead", "personality": "倔强善良"},
            {"name": "陈默", "role_type": "support", "personality": "沉稳寡言"},
        ]
    }, ensure_ascii=False)


def _episodes_payload(count: int) -> str:
    """构造含指定集数的合法分集大纲 LLM 输出 JSON 字符串（内部函数）。

    Args:
        count: 输出集数。

    Returns:
        str: {"episodes": [...]} 的 JSON 文本（恰好 count 集）。

    Raises:
        无：纯结构构造。
    """
    episodes = [{
        "title": f"第{i + 1}集：推进",
        "logline": f"第{i + 1}集梗概",
        "core_event": f"第{i + 1}集核心事件",
        "characters": ["林小满", "陈默"],
        "emotion_curve": "平稳→波折→收束",
        "duration_sec": 90,
    } for i in range(count)]
    return json.dumps({"episodes": episodes}, ensure_ascii=False)


def _mock_llm_chat(payload: str, calls: Optional[List[Dict[str, Any]]] = None):
    """构造可替换 script_pipeline._llm_chat 的异步假函数（内部工厂）。

    Args:
        payload: 每次 LLM 调用返回的固定文本。
        calls: 可选收集器；传入时每次调用追加
            {"messages": 消息列表, "temperature": 温度, "agent_name": 标识}。

    Returns:
        Callable: 签名兼容 _llm_chat(messages, *, temperature, **kwargs) 的异步函数。

    Raises:
        无。
    """

    async def fake_chat(messages, *, temperature, **kwargs):
        """替换 _llm_chat：恒返回预置文本并记录调用参数（内部函数）。

        Args:
            messages: OpenAI 格式消息列表（记录进 calls）。
            temperature: 采样温度（记录进 calls）。
            **kwargs: 其余关键字参数（agent_name 等，记录进 calls）。

        Returns:
            str: 预置的 payload 文本。

        Raises:
            无。
        """
        if calls is not None:
            calls.append({
                "messages": messages,
                "temperature": temperature,
                "agent_name": kwargs.get("agent_name"),
            })
        return payload

    return fake_chat


async def _upload_txt(client: httpx.AsyncClient, headers: Dict[str, str],
                      project_id: int, text: str, filename: str = "novel.txt"):
    """辅助：以 multipart 方式上传小说 txt（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        headers: 认证请求头。
        project_id: 项目 ID。
        text: 小说文本。
        filename: 上传文件名（决定扩展名分派）。

    Returns:
        httpx.Response: 解析接口响应。

    Raises:
        无：由调用方断言状态码。
    """
    return await client.post(
        f"/api/projects/{project_id}/script/input/novel/parse",
        headers=headers,
        files={"file": (filename, text.encode("utf-8"), "text/plain")},
    )


async def _confirm_two_characters(client: httpx.AsyncClient, headers: Dict[str, str],
                                  project_id: int, monkeypatch) -> None:
    """辅助：mock 角色 LLM 后走真实链路生成并确认 2 个角色（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        headers: 认证请求头。
        project_id: 项目 ID。
        monkeypatch: pytest 夹具；替换 _llm_chat（用例结束自动还原）。

    Returns:
        None。

    Raises:
        AssertionError: 任一接口返回非 200 或阶段未达 outline 时中断。
    """
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_characters_payload()))
    resp = await client.post(
        f"/api/projects/{project_id}/script/characters/generate", headers=headers)
    assert resp.status_code == 200, resp.text
    confirm = await client.post(
        f"/api/projects/{project_id}/script/characters/confirm",
        headers=headers,
        json={"characters": [{"name": "林小满"}, {"name": "陈默"}]},
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["phase"] == "outline", confirm.text


# ---------------------------------------------------------------------------
# 1. 上传解析落库 + 列表读取
# ---------------------------------------------------------------------------
async def test_parse_txt_persist_and_list(client, auth, db):
    """multipart 上传 txt：返回 3 章、config 落库、GET 读取一致、raw_text 回写。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（断言 config 与 raw_text 落库）。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    project = await create_project(client, auth["headers"], title="小说改编项目")
    resp = await _upload_txt(client, auth["headers"], project["id"], _TXT_3_CHAPTERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chapter_count"] == 3
    assert [c["index"] for c in body["chapters"]] == [1, 2, 3]
    assert body["chapters"][0]["title"] == "第一章 启程"
    assert body["chapters"][0]["char_count"] > 0
    assert body["chapters"][0]["summary"]

    # config 落库（novel_chapters 含摘要与前 2000 字预览）
    row = await db.get(Project, project["id"])
    stored = row.config["novel_chapters"]
    assert len(stored) == 3
    assert stored[0]["summary"]
    assert "content_preview" in stored[0]

    # GET 读取一致
    got = await client.get(
        f"/api/projects/{project['id']}/script/input/novel/chapters",
        headers=auth["headers"],
    )
    assert got.status_code == 200, got.text
    got_body = got.json()
    assert got_body["chapter_count"] == 3
    assert [c["title"] for c in got_body["chapters"]] == [
        "第一章 启程", "第2章 转折", "Chapter 3 终局"]

    # raw_text 回写为拼接全文（含正文内容）
    script = await client.get(
        f"/api/projects/{project['id']}/script", headers=auth["headers"])
    assert script.status_code == 200, script.text
    assert "启程正文" in script.json()["raw_text"]
    assert script.json()["content"]["input_mode"] == "novel"


async def test_parse_json_mode(client, auth):
    """JSON 模式上传（{raw_text, filename}）：同样解析出 3 章。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    project = await create_project(client, auth["headers"], title="JSON上传项目")
    resp = await client.post(
        f"/api/projects/{project['id']}/script/input/novel/parse",
        headers=auth["headers"],
        json={"raw_text": _TXT_3_CHAPTERS, "filename": "novel.txt"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["chapter_count"] == 3


# ---------------------------------------------------------------------------
# 2. 非法扩展名 → 400
# ---------------------------------------------------------------------------
async def test_parse_invalid_extension_400(client, auth):
    """非法扩展名（.pdf）：解析接口返回 400，detail 提示仅支持 txt/epub。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 400 时中断。
    """
    project = await create_project(client, auth["headers"], title="非法格式项目")
    resp = await client.post(
        f"/api/projects/{project['id']}/script/input/novel/parse",
        headers=auth["headers"],
        files={"file": ("novel.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert resp.status_code == 400, resp.text
    assert "仅支持" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 3. 未解析章节时 plan from_chapters → 409
# ---------------------------------------------------------------------------
async def test_plan_from_chapters_without_parse_409(client, auth, monkeypatch):
    """from_chapters 但未解析章节：409（请先解析小说章节）。

    前置：先走 idea 输入 + 确认角色（保证越过角色校验，命中章节校验）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 替换 _llm_chat（角色生成）。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 409 时中断。
    """
    project = await create_project(client, auth["headers"], title="未解析项目")
    resp = await client.put(
        f"/api/projects/{project['id']}/script/input",
        headers=auth["headers"],
        json={"mode": "idea", "raw_text": "一句话创意：测试。"},
    )
    assert resp.status_code == 200, resp.text
    await _confirm_two_characters(client, auth["headers"], project["id"], monkeypatch)

    plan = await client.post(
        f"/api/projects/{project['id']}/script/episodes/plan",
        headers=auth["headers"],
        json={"from_chapters": True},
    )
    assert plan.status_code == 409, plan.text
    assert "请先解析小说章节" in plan.json()["detail"]


# ---------------------------------------------------------------------------
# 4. from_chapters 生成：按章节数分集 + 章节摘要注入提示词
# ---------------------------------------------------------------------------
async def test_plan_from_chapters_generates_episodes(client, auth, monkeypatch):
    """上传 3 章 + 确认角色 → from_chapters 缺省每章一集（3 集）；
    LLM 消息注入章节摘要（含章节标题）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 替换 _llm_chat（角色生成 / 大纲生成）。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    project = await create_project(client, auth["headers"], title="章节分集项目")
    upload = await _upload_txt(client, auth["headers"], project["id"], _TXT_3_CHAPTERS)
    assert upload.status_code == 200, upload.text
    await _confirm_two_characters(client, auth["headers"], project["id"], monkeypatch)

    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_episodes_payload(3), calls))
    resp = await client.post(
        f"/api/projects/{project['id']}/script/episodes/plan",
        headers=auth["headers"],
        json={"from_chapters": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["episodes"]) == 3
    assert [e["sequence"] for e in body["episodes"]] == [1, 2, 3]

    # 章节摘要注入：LLM user 消息含章节摘要段与章节标题
    assert len(calls) == 1
    user_content = calls[0]["messages"][-1]["content"]
    assert "章节摘要：" in user_content
    assert "第一章 启程" in user_content
    assert "Chapter 3 终局" in user_content


async def test_plan_from_chapters_merge_target(client, auth, monkeypatch):
    """from_chapters + target_episodes=1：3 章按比例合并为 1 集。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        monkeypatch: 替换 _llm_chat。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    project = await create_project(client, auth["headers"], title="章节合并项目")
    upload = await _upload_txt(client, auth["headers"], project["id"], _TXT_3_CHAPTERS)
    assert upload.status_code == 200, upload.text
    await _confirm_two_characters(client, auth["headers"], project["id"], monkeypatch)

    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(sp_mod, "_llm_chat", _mock_llm_chat(_episodes_payload(1), calls))
    resp = await client.post(
        f"/api/projects/{project['id']}/script/episodes/plan",
        headers=auth["headers"],
        json={"from_chapters": True, "target_episodes": 1},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["episodes"]) == 1
    user_content = calls[0]["messages"][-1]["content"]
    # 合并后单条摘要串联首尾章节标题
    assert "第一章 启程" in user_content
    assert "Chapter 3 终局" in user_content


# ---------------------------------------------------------------------------
# 5. 长文截断消除：build_novel_context 输出 > 4000 字且覆盖末章
# ---------------------------------------------------------------------------
async def test_novel_context_exceeds_4000(client, auth, db):
    """3 章各 3000 字：build_novel_context 输出 > 4000 字且含第三章标题，
    证明 novel 模式不再受 raw_text[:4000] 硬截断约束。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（读取上传后的 project.config）。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    long_txt = (
        "第一章 启程\n" + "启" * 3000 + "\n\n"
        "第二章 转折\n" + "转" * 3000 + "\n\n"
        "第三章 终局\n" + "终" * 3000 + "\n"
    )
    project = await create_project(client, auth["headers"], title="长文本项目")
    upload = await _upload_txt(client, auth["headers"], project["id"], long_txt)
    assert upload.status_code == 200, upload.text
    assert upload.json()["chapter_count"] == 3

    row = await db.get(Project, project["id"])
    context = build_novel_context(row)
    assert len(context) > 4000
    assert "第三章 终局" in context


# ---------------------------------------------------------------------------
# 6. 越权：他人项目解析/列表一律 404
# ---------------------------------------------------------------------------
async def test_forbidden_access_404(client, auth):
    """越权：用户 B 访问用户 A 项目的 novel 解析/列表接口一律 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 用户 A（项目归属者）。

    Returns:
        None。

    Raises:
        AssertionError: 任一状态码非 404 时中断。
    """
    project = await create_project(client, auth["headers"], title="越权项目")
    other = await register_user(client, prefix="other")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    parse = await client.post(
        f"/api/projects/{project['id']}/script/input/novel/parse",
        headers=other_headers,
        json={"raw_text": _TXT_3_CHAPTERS, "filename": "novel.txt"},
    )
    assert parse.status_code == 404, parse.text
    got = await client.get(
        f"/api/projects/{project['id']}/script/input/novel/chapters",
        headers=other_headers,
    )
    assert got.status_code == 404, got.text
