"""计费接口测试模块（文件级说明）。

覆盖链路：
- 充值订单支付后余额增加（5000 + 1000 = 6000）
- 重复支付幂等（余额不变、订单保持 paid）
- 流水记录存在（type=recharge，amount=+1000）
- 订单列表可查到已支付订单
- 余额不足冻结场景（可用积分 < 10 时发起定妆照生成返回 409）
"""

import pytest
from sqlalchemy import update

from app.models import Wallet
from tests.conftest import create_project, get_wallet_model


async def _create_character(client, auth, title_suffix: str) -> int:
    """辅助：创建项目并建一个角色资产，返回角色 ID（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        title_suffix: 项目标题后缀（防冲突）。

    Returns:
        int: 新建角色资产 ID。

    Raises:
        AssertionError: 任一接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"计费项目_{title_suffix}")
    resp = await client.post(
        f"/api/projects/{project['id']}/assets/characters",
        headers=auth["headers"],
        json={"name": "林小满", "description": "测试角色"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def test_chongzhi_dingdan_pay_hou_yue_zengjia(client, auth):
    """充值订单支付后：余额 5000 → 6000，订单置 paid。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    resp = await client.post(
        "/api/billing/orders", headers=auth["headers"], json={"package_id": "p_small"}
    )
    assert resp.status_code == 200, resp.text
    order = resp.json()
    assert order["status"] == "pending"
    assert order["points"] == 1000

    resp2 = await client.post(
        f"/api/billing/orders/{order['order_no']}/pay", headers=auth["headers"]
    )
    assert resp2.status_code == 200, resp2.text
    body = resp2.json()
    assert body["order"]["status"] == "paid"
    assert body["points_balance"] == 6000


async def test_chongfu_pay_mide_deng_yue_bubian(client, auth):
    """重复支付同一订单：幂等返回 200，余额不再增加。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 二次支付后余额发生变化时中断。
    """
    resp = await client.post(
        "/api/billing/orders", headers=auth["headers"], json={"package_id": "p_mid"}
    )
    order = resp.json()
    first = await client.post(
        f"/api/billing/orders/{order['order_no']}/pay", headers=auth["headers"]
    )
    assert first.status_code == 200
    assert first.json()["points_balance"] == 5000 + 5500

    # 重复支付：幂等
    second = await client.post(
        f"/api/billing/orders/{order['order_no']}/pay", headers=auth["headers"]
    )
    assert second.status_code == 200
    assert second.json()["points_balance"] == 5000 + 5500
    assert second.json()["order"]["status"] == "paid"


async def test_liushui_jilu_type_recharge(client, auth):
    """支付后流水记录存在：type=recharge、amount=+1000。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 未查到 recharge 流水时中断。
    """
    resp = await client.post(
        "/api/billing/orders", headers=auth["headers"], json={"package_id": "p_small"}
    )
    order_no = resp.json()["order_no"]
    pay = await client.post(f"/api/billing/orders/{order_no}/pay", headers=auth["headers"])
    assert pay.status_code == 200

    resp2 = await client.get(
        "/api/billing/transactions", headers=auth["headers"], params={"type": "recharge"}
    )
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["total"] >= 1
    tx = body["items"][0]
    assert tx["type"] == "recharge"
    assert tx["amount"] == 1000

    # 订单列表也能查到已支付订单
    resp3 = await client.get(
        "/api/billing/orders", headers=auth["headers"], params={"status": "paid"}
    )
    assert resp3.status_code == 200
    assert resp3.json()["total"] >= 1


async def test_yue_buzu_dongjie_409(client, auth, db):
    """余额不足冻结场景：可用积分 < 10 时发起定妆照生成返回 409。

    直接把钱包可用积分改为 5，再走资产生成接口验证冻结预检。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话（用于篡改钱包余额）。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 409 时中断。
    """
    character_id = await _create_character(client, auth, "yuebuzu")

    # 直接把可用积分压到 5（低于 10 积分定妆照单价）
    await db.execute(
        update(Wallet).where(Wallet.user_id == auth["user_id"]).values(points_balance=5)
    )
    await db.commit()

    project_id = None
    # 找到该项目 ID（由 _create_character 创建的项目即用户当前唯一项目）
    resp_list = await client.get("/api/projects", headers=auth["headers"])
    project_id = resp_list.json()["items"][0]["id"]

    resp = await client.post(
        f"/api/projects/{project_id}/assets/characters/{character_id}/generate",
        headers=auth["headers"],
    )
    assert resp.status_code == 409, resp.text
    assert "余额不足" in resp.json()["detail"]

    # 余额与冻结均未被改动
    wallet = await get_wallet_model(db, auth["user_id"])
    assert wallet.points_balance == 5
    assert wallet.frozen_points == 0
