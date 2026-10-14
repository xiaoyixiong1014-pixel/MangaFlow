"""画布节点沉淀入资产库接口测试模块（文件级说明）。

覆盖链路（A-10/CV-24 画布 → 资产库回流）：
- 图像节点无产物 URL → 400
- 归属越权（沉淀他人画布节点）→ 404
- 正常沉淀（角色）：资产创建、source='canvas'、visual_prompt 为 LLM 生成值、
  节点 meta 回写幂等记录
- LLM 失败降级（RD-47）：mock LLM 抛异常时沉淀仍成功，visual_prompt 使用
  节点描述降级文本
- 重复沉淀：duplicated=true 返回既有资产 ID，且不重复建资产

说明：LLM 调用一律 monkeypatch 到 app.providers.llm.chat，保证用例确定性
（不依赖真实渠道可用性）。
"""

from typing import Optional

import pytest
from sqlalchemy import select

from app.models import Character, Scene
from tests.conftest import create_project, register_user, unique_suffix


async def _make_image_node_canvas(
    client, auth, tag: str, *, with_output: bool = True, data_extra: Optional[dict] = None
) -> dict:
    """辅助：创建项目 + 空白画布 + 图同步插入一个 image 节点（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        tag: 标题/名称防冲突后缀。
        with_output: 是否为节点写入产物 URL（data.output）。
        data_extra: 追加写入节点 data 的额外字段（如 description/label）。

    Returns:
        dict: {"project": 项目 JSON, "canvas_id": 画布 ID, "node": 节点 JSON}。

    Raises:
        AssertionError: 任一接口返回非预期状态码时中断。
    """
    project = await create_project(client, auth["headers"], title=f"沉淀项目_{tag}")
    resp = await client.post(
        "/api/canvases",
        headers=auth["headers"],
        json={"title": f"沉淀画布_{tag}", "project_id": project["id"]},
    )
    assert resp.status_code == 201, resp.text
    canvas = resp.json()

    data: dict = dict(data_extra or {})
    if with_output:
        data["output"] = "/storage/uploads/sink_test.png"
    data.setdefault("label", "主角立绘")
    resp = await client.put(
        f"/api/canvases/{canvas['canvas']['id']}/graph",
        headers=auth["headers"],
        json={
            "nodes": [
                {
                    "node_type": "image",
                    "position_x": 120,
                    "position_y": 80,
                    "data": data,
                    "config": {},
                }
            ],
            "edges": [],
        },
    )
    assert resp.status_code == 200, resp.text
    graph = resp.json()
    return {"project": project, "canvas_id": canvas["canvas"]["id"], "node": graph["nodes"][0]}


def _mock_llm_chat(
    monkeypatch, *, value: str = "标准化视觉描述：动漫风格全身立绘", error: Optional[Exception] = None
):
    """辅助：将 LLM chat 函数替换为可控 fake（内部函数）。

    Args:
        monkeypatch: pytest monkeypatch 夹具。
        value: fake 成功时返回的视觉描述文本。
        error: 非 None 时 fake 调用抛出该异常（模拟 LLM 失败/超时）。

    Returns:
        list: 记录每次 fake 调用的 messages 参数（供断言调用发生与否）。

    Raises:
        无：fake 内部按参数决定是否抛出外部传入的异常。
    """
    from app.providers import llm as llm_mod

    calls: list = []

    async def fake_chat(messages, **kwargs):
        """fake LLM 补全：记录调用并按 error 参数决定行为（内部函数）。

        Args:
            messages: OpenAI 格式消息列表。
            **kwargs: 其余补全参数（temperature/json_mode 等）。

        Returns:
            str: 预置视觉描述文本。

        Raises:
            Exception: error 非 None 时原样抛出（模拟渠道失败）。
        """
        calls.append(messages)
        if error is not None:
            raise error
        return value

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    return calls


async def test_image_jiedian_wu_chanchu_400(client, auth):
    """图像节点无产物 URL：沉淀返回 400（中文错误信息）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 接口未返回 400 或错误信息不符时中断。
    """
    ctx = await _make_image_node_canvas(client, auth, unique_suffix(), with_output=False)
    resp = await client.post(
        f"/api/canvas/nodes/{ctx['node']['id']}/sink",
        headers=auth["headers"],
        json={"asset_type": "character"},
    )
    assert resp.status_code == 400, resp.text
    assert "产物" in resp.json()["detail"]


async def test_gui_shu_yuequan_404(client, auth):
    """归属越权：沉淀他人画布的节点返回 404（不泄露存在性）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文（画布属主）。

    Returns:
        None。

    Raises:
        AssertionError: 接口未返回 404 时中断。
    """
    ctx = await _make_image_node_canvas(client, auth, unique_suffix())
    other = await register_user(client)
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    resp = await client.post(
        f"/api/canvas/nodes/{ctx['node']['id']}/sink",
        headers=other_headers,
        json={"asset_type": "character"},
    )
    assert resp.status_code == 404, resp.text


async def test_normal_sink_cheng_gong(client, auth, db, monkeypatch):
    """正常沉淀（角色）：资产创建、source='canvas'、visual_prompt 为 LLM 生成值。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（ORM 断言）。
        monkeypatch: LLM mock 夹具。

    Returns:
        None。

    Raises:
        AssertionError: 响应或落库字段与预期不符时中断。
    """
    _mock_llm_chat(monkeypatch)
    ctx = await _make_image_node_canvas(
        client, auth, unique_suffix(), data_extra={"description": "白发少女，红瞳，和服装束"}
    )
    resp = await client.post(
        f"/api/canvas/nodes/{ctx['node']['id']}/sink",
        headers=auth["headers"],
        json={"asset_type": "character"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["asset_id"] > 0
    assert body["asset_type"] == "character"
    assert body["duplicated"] is False

    # 落库断言：来源标记 / 视觉提示词 / 主照 URL / 名称缺省取节点 label
    row = await db.get(Character, body["asset_id"])
    assert row is not None
    assert row.source == "canvas"
    assert row.visual_prompt == "标准化视觉描述：动漫风格全身立绘"
    assert row.asset_url == "/storage/uploads/sink_test.png"
    assert row.name == "主角立绘"
    assert row.project_id == ctx["project"]["id"]


async def test_llm_shibai_jiangji(client, auth, db, monkeypatch):
    """LLM 失败降级（RD-47）：mock LLM 抛异常时沉淀仍成功且降级文本生效。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（ORM 断言）。
        monkeypatch: LLM mock 夹具。

    Returns:
        None。

    Raises:
        AssertionError: 沉淀失败或降级文本不符时中断。
    """
    _mock_llm_chat(monkeypatch, error=RuntimeError("渠道全部失败"))
    ctx = await _make_image_node_canvas(
        client, auth, unique_suffix(), data_extra={"description": "雨夜天台，霓虹灯下的少年"}
    )
    resp = await client.post(
        f"/api/canvas/nodes/{ctx['node']['id']}/sink",
        headers=auth["headers"],
        json={"asset_type": "scene"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["duplicated"] is False

    row = await db.get(Scene, body["asset_id"])
    assert row is not None
    assert row.source == "canvas"
    # 降级链：节点 description 文本兜底为 visual_prompt（LLM 未参与）
    assert row.visual_prompt == "雨夜天台，霓虹灯下的少年"


async def test_repeat_sink_duplicated(client, auth, db, monkeypatch):
    """重复沉淀：duplicated=true 返回既有资产 ID 且不重复建资产。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（数量断言）。
        monkeypatch: LLM mock 夹具。

    Returns:
        None。

    Raises:
        AssertionError: 二次沉淀未命中幂等或重复建资产时中断。
    """
    _mock_llm_chat(monkeypatch)
    ctx = await _make_image_node_canvas(client, auth, unique_suffix())
    url = f"/api/canvas/nodes/{ctx['node']['id']}/sink"
    first = await client.post(url, headers=auth["headers"], json={"asset_type": "character"})
    assert first.status_code == 201, first.text
    second = await client.post(url, headers=auth["headers"], json={"asset_type": "character"})
    assert second.status_code == 201, second.text

    assert second.json()["duplicated"] is True
    assert second.json()["asset_id"] == first.json()["asset_id"]

    rows = (await db.execute(select(Character))).scalars().all()
    assert len(rows) == 1  # 未重复建资产
