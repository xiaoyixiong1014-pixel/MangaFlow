"""图像/视频真实视觉机审测试模块（文件级说明，Task 7 / 合规红线 R-08、C-01）。

覆盖内容：
- review_image 判定解析：纯 JSON 输出 / 代码块围栏包裹 / JSON 混在文字中的
  容错提取 / 完全非法输出抛异常（由调用方降级）；请求契约（端点、鉴权头、
  模型名、图文混合 content 结构）；
- 图像审核降级链：视觉机审异常 → 降级 LLM 文本机审（method=llm-text）→
  LLM 再失败降级纯词表（method=keyword）；
- 图像任务端到端：视觉机审判定 violate → 任务 failed（content_violation），
  review_records.method == "vision"；
- 视频首帧机审：无 ffmpeg 抽帧返回 None；抽帧失败降级 LLM 文本机审；
  视频任务端到端首帧视觉 violate 拦截（method=vision）。

视觉模型响应统一以 httpx.MockTransport 注入（monkeypatch
vision_review._new_http_client），不产生真实网络调用。
"""

import asyncio
import base64
import json

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import ReviewRecord, Task
from app.providers import vision_review
from app.tasks import runner as runner_mod
from tests.conftest import (
    create_project,
    insert_storyboard,
    unique_suffix,
    wait_task_terminal,
)


def _vision_body(content: str) -> dict:
    """构造 OpenAI chat/completions 假响应体（内部函数）。

    Args:
        content: 模拟模型输出的文本内容。

    Returns:
        dict: 可被 httpx.Response(json=...) 直接使用的响应结构。

    Raises:
        无。
    """
    return {"choices": [{"message": {"content": content}}]}


def _patch_vision_client(monkeypatch, content: str, captured: list) -> None:
    """注入 MockTransport 假视觉模型客户端并捕获请求（内部函数）。

    Args:
        monkeypatch: pytest 属性替换夹具。
        content: 假模型返回的文本内容。
        captured: 捕获到的 httpx.Request 追加列表（调用方用于请求契约断言）。

    Returns:
        None。

    Raises:
        无。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        """MockTransport 回调：记录请求并返回假响应（内部函数）。"""
        captured.append(request)
        return httpx.Response(200, json=_vision_body(content))

    def factory() -> httpx.AsyncClient:
        """客户端工厂：返回挂载 MockTransport 的客户端（内部函数）。"""
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=15.0)

    monkeypatch.setattr(vision_review, "_new_http_client", factory)


# ---------------------------------------------------------------------------
# review_image 判定解析与请求契约
# ---------------------------------------------------------------------------
async def test_review_image_chunjie_json(monkeypatch):
    """纯 JSON 输出解析成功，且请求契约（端点/鉴权/模型/图文结构）正确。

    Args:
        monkeypatch: pytest 属性替换夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    captured: list = []
    _patch_vision_client(
        monkeypatch, '{"verdict": "violate", "reason": "画面含赌博场景"}', captured
    )
    result = await vision_review.review_image(
        image_b64=base64.b64encode(b"fakepng").decode(), context="测试上下文"
    )
    assert result["verdict"] == "violate"
    assert result["reason"] == "画面含赌博场景"
    assert result["model"] == settings.agnes_vision_model

    # 请求契约断言
    assert len(captured) == 1
    req = captured[0]
    assert req.url.path.endswith("/chat/completions")
    assert req.headers["authorization"] == f"Bearer {settings.agnes_llm_api_key}"
    payload = json.loads(req.content)
    assert payload["model"] == settings.agnes_vision_model
    assert payload["messages"][0]["role"] == "system"
    image_part = payload["messages"][1]["content"][1]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


async def test_review_image_daimakuai_baoguo(monkeypatch):
    """代码块围栏包裹的 JSON 输出剥离围栏后正常解析。

    Args:
        monkeypatch: pytest 属性替换夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    _patch_vision_client(
        monkeypatch, '```json\n{"verdict": "pass", "reason": "正常画面"}\n```', []
    )
    result = await vision_review.review_image(image_url="https://example.com/a.png")
    assert result["verdict"] == "pass"
    assert result["reason"] == "正常画面"


async def test_review_image_hunzai_wenzi_rongcuo(monkeypatch):
    """JSON 混在说明文字中间：截取首尾大括号容错提取成功。

    Args:
        monkeypatch: pytest 属性替换夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    _patch_vision_client(
        monkeypatch, '审核结论：{"verdict": "suspect", "reason": "低俗擦边"} 以上。', []
    )
    result = await vision_review.review_image(image_b64="abc")
    assert result["verdict"] == "suspect"


async def test_review_image_feifa_shuchu_paochu(monkeypatch):
    """完全非法输出（无可解析 JSON）→ 抛异常由调用方降级文本机审。

    Args:
        monkeypatch: pytest 属性替换夹具。

    Returns:
        None。

    Raises:
        pytest.raises.Exception: 预期抛出异常。
    """
    _patch_vision_client(monkeypatch, "无法判定这张图的内容", [])
    with pytest.raises(Exception):
        await vision_review.review_image(image_b64="abc")


# ---------------------------------------------------------------------------
# 图像审核降级链（单元级：直接调用 runner 内部函数）
# ---------------------------------------------------------------------------
async def _make_image_task(client, auth, db, tag: str) -> tuple:
    """辅助：建项目/分镜并直插一条带产物 URL 的图像任务（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        tag: 幂等键防冲突后缀。

    Returns:
        tuple[int, Task]: (项目 ID, 已落库的图像任务对象)。

    Raises:
        AssertionError: 项目创建返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"视觉降级项目_{tag}")
    sb = await insert_storyboard(db, project["id"], sequence=1)
    task = Task(
        project_id=project["id"], storyboard_id=sb.id, type="image", phase="trial",
        status="running", cost_points=10, progress=70,
        result_url="/storage/generated/fake_vision_test.png",
        idempotency_key=f"vision_test_{tag}",
        meta={
            "prompt": "白发少年站在天台远眺", "target_type": "storyboard_keyframe",
            "target_id": sb.id, "storyboard_id": sb.id,
        },
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return project["id"], task


async def test_shijue_yichang_jiangji_llm_text(client, auth, db, monkeypatch):
    """视觉机审异常 → 降级 LLM 文本机审，审核记录 method=llm-text。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        monkeypatch: pytest 属性替换夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    async def _boom(*args, **kwargs):
        """模拟视觉模型不可用（内部函数）。"""
        raise RuntimeError("视觉模型不可用")

    async def _fake_llm(text):
        """模拟 LLM 文本机审返回可疑判定（内部函数）。"""
        return "suspect", "文本机审判定可疑"

    monkeypatch.setattr(vision_review, "review_image", _boom)
    monkeypatch.setattr(runner_mod, "llm_review_content", _fake_llm)

    pid, task = await _make_image_task(client, auth, db, unique_suffix())
    hit = await runner_mod._review_generated_image(db, task, auth["user_id"], task.meta)
    assert hit is False  # suspect 不拦截任务

    records = (
        await db.execute(
            select(ReviewRecord).where(ReviewRecord.project_id == pid)
        )
    ).scalars().all()
    assert len(records) == 1
    assert records[0].method == "llm-text"
    assert records[0].result == "suspect"
    assert records[0].target_type == "image"
    assert records[0].gate == "generate"


async def test_llm_text_zai_shibai_jiangji_keyword(client, auth, db, monkeypatch):
    """视觉异常 + LLM 文本机审再失败 → 降级纯词表判定（method=keyword）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        monkeypatch: pytest 属性替换夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    async def _boom(*args, **kwargs):
        """模拟视觉模型不可用（内部函数）。"""
        raise RuntimeError("视觉模型不可用")

    async def _boom_llm(text):
        """模拟 LLM 文本机审也不可用（内部函数）。"""
        raise RuntimeError("LLM 不可用")

    monkeypatch.setattr(vision_review, "review_image", _boom)
    monkeypatch.setattr(runner_mod, "llm_review_content", _boom_llm)

    pid, task = await _make_image_task(client, auth, db, unique_suffix())
    hit = await runner_mod._review_generated_image(db, task, auth["user_id"], task.meta)
    assert hit is False

    records = (
        await db.execute(
            select(ReviewRecord).where(ReviewRecord.project_id == pid)
        )
    ).scalars().all()
    assert len(records) == 1
    assert records[0].method == "keyword"
    assert records[0].result == "pass"  # 提示词无命中词，词表判定通过


# ---------------------------------------------------------------------------
# 图像任务端到端：视觉机审 violate 拦截
# ---------------------------------------------------------------------------
async def test_image_renwu_shijue_violate_lanjie(client, auth, db, monkeypatch):
    """图像任务端到端：视觉机审判定 violate → 任务 failed（content_violation）。

    审核记录 method=="vision"，且视觉请求携带产物图像（data URI）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        monkeypatch: pytest 属性替换夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    captured: list = []
    _patch_vision_client(
        monkeypatch, '{"verdict": "violate", "reason": "画面含色情内容"}', captured
    )
    project = await create_project(client, auth["headers"], title=f"视觉机审项目_{unique_suffix()}")
    sb = await insert_storyboard(db, project["id"], sequence=1)

    resp = await client.post(
        f"/api/projects/{project['id']}/storyboards/{sb.id}/keyframes",
        headers=auth["headers"], json={"phase": "trial"},
    )
    assert resp.status_code == 200, resp.text
    final = await wait_task_terminal(
        client, auth["headers"], resp.json()["tasks"][0]["id"], timeout=30.0
    )
    assert final["status"] == "failed", final
    assert final["error_code"] == "content_violation"

    records = (
        await db.execute(
            select(ReviewRecord).where(
                ReviewRecord.project_id == project["id"], ReviewRecord.gate == "generate"
            )
        )
    ).scalars().all()
    vision_records = [r for r in records if r.method == "vision"]
    assert vision_records, records
    assert vision_records[0].result == "violate"
    assert vision_records[0].target_type == "image"

    # 视觉请求确实送审了产物图像（本地文件读取转 data URI）
    assert captured, "未发起视觉机审请求"
    payload = json.loads(captured[0].content)
    image_url = payload["messages"][1]["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/")


# ---------------------------------------------------------------------------
# 视频首帧机审
# ---------------------------------------------------------------------------
async def test_ffmpeg_missing_chouzhen_fanhui_none(monkeypatch, tmp_path):
    """环境无 ffmpeg（子进程创建抛 FileNotFoundError）→ 抽帧返回 None。

    Args:
        monkeypatch: pytest 属性替换夹具。
        tmp_path: pytest 临时目录。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"not-a-real-mp4")

    def _raiser(*args, **kwargs):
        """模拟 ffmpeg 命令不存在的 FileNotFoundError（内部函数）。"""
        raise FileNotFoundError("ffmpeg not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raiser)
    result = await runner_mod._extract_video_first_frame(fake_video)
    assert result is None


async def test_shipin_chouzhen_shibai_jiangji_text(client, auth, db, monkeypatch):
    """抽帧失败（产物不存在）+ 视觉异常 → 视频降级 LLM 文本机审（method=llm-text）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        monkeypatch: pytest 属性替换夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    async def _boom(*args, **kwargs):
        """模拟视觉模型不可用（内部函数）。"""
        raise RuntimeError("视觉模型不可用")

    async def _fake_llm(text):
        """模拟 LLM 文本机审通过（内部函数）。"""
        return "pass", "文本机审通过"

    monkeypatch.setattr(vision_review, "review_image", _boom)
    monkeypatch.setattr(runner_mod, "llm_review_content", _fake_llm)

    project = await create_project(client, auth["headers"], title=f"视频降级项目_{unique_suffix()}")
    sb = await insert_storyboard(db, project["id"], sequence=1)
    task = Task(
        project_id=project["id"], storyboard_id=sb.id, type="video", phase="full",
        status="running", cost_points=100, progress=70,
        result_url="/storage/generated/nonexistent_video_test.mp4",
        idempotency_key=f"video_vision_{unique_suffix()}",
        meta={"prompt": "少女奔跑穿过向日葵花田", "storyboard_id": sb.id, "duration": 5},
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    hit = await runner_mod._review_video_first_frame(db, task, auth["user_id"], task.meta)
    assert hit is False

    records = (
        await db.execute(
            select(ReviewRecord).where(ReviewRecord.project_id == project["id"])
        )
    ).scalars().all()
    assert len(records) == 1
    assert records[0].method == "llm-text"
    assert records[0].result == "pass"
    assert records[0].target_type == "video"


async def test_video_renwu_shouzhen_violate_lanjie(client, auth, db, monkeypatch):
    """视频任务端到端：首帧视觉机审 violate → 任务 failed（content_violation）。

    审核记录 method=="vision"（target_type=video），违规产物 URL 不回写分镜。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        monkeypatch: pytest 属性替换夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    _patch_vision_client(
        monkeypatch, '{"verdict": "violate", "reason": "首帧含暴力画面"}', []
    )

    async def _fake_frame(video_path):
        """模拟 ffmpeg 抽帧成功，返回伪 PNG 字节（内部函数）。"""
        return b"fake-first-frame-bytes"

    monkeypatch.setattr(runner_mod, "_extract_video_first_frame", _fake_frame)

    project = await create_project(client, auth["headers"], title=f"视频机审项目_{unique_suffix()}")
    sb = await insert_storyboard(db, project["id"], sequence=1)

    resp = await client.post(
        f"/api/projects/{project['id']}/storyboards/{sb.id}/video",
        headers=auth["headers"], json={"use_end_frame": False},
    )
    assert resp.status_code == 200, resp.text
    final = await wait_task_terminal(
        client, auth["headers"], resp.json()["id"], timeout=30.0
    )
    assert final["status"] == "failed", final
    assert final["error_code"] == "content_violation"

    records = (
        await db.execute(
            select(ReviewRecord).where(
                ReviewRecord.project_id == project["id"], ReviewRecord.target_type == "video"
            )
        )
    ).scalars().all()
    vision_records = [r for r in records if r.method == "vision"]
    assert vision_records, records
    assert vision_records[0].result == "violate"
    assert vision_records[0].gate == "generate"

    # 违规产物 URL 不回写分镜
    await db.refresh(sb)
    assert sb.video_url is None
