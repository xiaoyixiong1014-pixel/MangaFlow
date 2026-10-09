"""剧本工作流接口测试模块（文件级说明）。

覆盖链路：
- 尚无剧本时取剧本 404
- 保存剧本输入（input）成功并可重复覆盖
- 实体抽取（extract）：LLM 可达时走真实调用，不可达时规则降级；
  断言响应含 characters/scenes/props/conflicts 键且不产生 5xx
- 抽取确认（confirm）后角色资产出现、项目状态推进为 asset
"""

import pytest

from tests.conftest import create_project

# 剧本原始输入样例（含「角色名：台词」结构，保证规则降级路也能抽到角色）
_SAMPLE_RAW = "林小满：我要成为最强的漫画家。\n陈默：那我们一起加油吧。\n两人在天台许下约定。"


async def _make_project_with_input(client, auth) -> dict:
    """辅助：创建项目并保存剧本输入，返回项目 dict（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        dict: 项目响应 JSON。

    Raises:
        AssertionError: 接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title="剧本项目")
    resp = await client.put(
        f"/api/projects/{project['id']}/script/input",
        headers=auth["headers"],
        json={"mode": "idea", "raw_text": _SAMPLE_RAW},
    )
    assert resp.status_code == 200, resp.text
    return project


async def test_weijian_juben_shi_404(client, auth):
    """项目尚未创建剧本时，GET 剧本返回 404。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 404 时中断。
    """
    project = await create_project(client, auth["headers"])
    resp = await client.get(f"/api/projects/{project['id']}/script", headers=auth["headers"])
    assert resp.status_code == 404


async def test_baocun_shuru_chenggong(client, auth):
    """保存剧本输入：首次建行、重复保存覆盖 raw_text 与 input_mode。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(client, auth["headers"])
    url = f"/api/projects/{project['id']}/script/input"

    first = await client.put(
        url, headers=auth["headers"], json={"mode": "idea", "raw_text": _SAMPLE_RAW}
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["raw_text"].startswith("林小满")
    assert body["content"]["input_mode"] == "idea"
    assert body["version"] == 1

    # 第二次保存（outline 模式）覆盖原文本
    second = await client.put(
        url, headers=auth["headers"], json={"mode": "outline", "raw_text": "新的大纲文本"}
    )
    assert second.status_code == 200
    body2 = second.json()
    assert body2["raw_text"] == "新的大纲文本"
    assert body2["content"]["input_mode"] == "outline"

    # 保存后 GET 剧本可读
    got = await client.get(f"/api/projects/{project['id']}/script", headers=auth["headers"])
    assert got.status_code == 200
    assert got.json()["raw_text"] == "新的大纲文本"


async def test_zhaiti_chouqu_jiangji_jian(client, auth):
    """实体抽取：响应必含 characters/scenes/props/conflicts 四个键（列表）。

    LLM 可达时走真实 DeepSeek 调用（结果较慢属正常）；不可达时代码自动
    降级为规则抽取——两条路径均不应产生 5xx。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 结构断言失败或状态码为 5xx 时中断。
    """
    project = await _make_project_with_input(client, auth)
    resp = await client.post(
        f"/api/projects/{project['id']}/script/extract", headers=auth["headers"]
    )
    # 无论 LLM 是否可达都不允许 5xx
    assert resp.status_code == 200, resp.text
    data = resp.json()
    for key in ("characters", "scenes", "props", "conflicts"):
        assert key in data and isinstance(data[key], list)

    # 规则降级路（或 LLM 路）至少能从对白中抽到角色
    char_names = {c["name"] for c in data["characters"]}
    assert "林小满" in char_names or "陈默" in char_names


async def test_zhaiti_queren_hou_zichan_chuxian(client, auth):
    """抽取确认入池：角色资产出现、项目状态推进为 asset、返回计数。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_input(client, auth)

    # 先抽取（结果可为 LLM 或规则路），再以确认清单入池
    extract = await client.post(
        f"/api/projects/{project['id']}/script/extract", headers=auth["headers"]
    )
    assert extract.status_code == 200, extract.text
    entities = extract.json()
    first_char = entities["characters"][0]["name"] if entities["characters"] else "林小满"

    confirm = await client.post(
        f"/api/projects/{project['id']}/script/extract/confirm",
        headers=auth["headers"],
        json={
            "characters": [{"name": first_char, "description": "抽取确认的角色"}],
            "scenes": [],
            "props": [],
        },
    )
    assert confirm.status_code == 200, confirm.text
    counts = confirm.json()
    assert counts["characters"] >= 1

    # 项目状态推进为 asset
    detail = await client.get(f"/api/projects/{project['id']}", headers=auth["headers"])
    assert detail.status_code == 200
    assert detail.json()["status"] == "asset"

    # 资产列表出现确认的角色
    assets = await client.get(
        f"/api/projects/{project['id']}/assets", headers=auth["headers"]
    )
    assert assets.status_code == 200
    char_names = [c["name"] for c in assets.json()["characters"]]
    assert first_char in char_names
