"""认证接口测试模块（文件级说明）。

覆盖链路：
- 注册成功（返回 token + user，钱包赠送初始积分）
- 重复用户名注册 409
- 登录成功 / 错误密码 401
- refresh 令牌换新 access
- 无令牌访问受保护接口 401
"""

import pytest

from tests.conftest import register_user


async def test_zhuce_chenggong_fanhui_token_yu_qianbao(client):
    """注册成功：返回令牌对与用户信息，且 /me 显示初始赠送 5000 积分。

    Args:
        client: 内存 HTTP 客户端。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    data = await register_user(client, prefix="regok")
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["token_type"] == "bearer"
    assert data["user"]["role"] == "user"
    assert data["user"]["status"] == 1

    # 用注册返回的 access_token 访问受保护接口
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    resp = await client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["username"] == data["_payload"]["username"]
    # 注册赠送积分（注册接口固定发放 5000）
    assert body["wallet"]["points_balance"] == 5000
    assert body["wallet"]["frozen_points"] == 0


async def test_zhuce_chongfu_yonghuming_409(client):
    """重复用户名注册返回 409 冲突。

    Args:
        client: 内存 HTTP 客户端。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 409 时中断。
    """
    data = await register_user(client, prefix="dup")
    username = data["_payload"]["username"]
    resp = await client.post(
        "/api/auth/register",
        json={"username": username, "email": None, "password": "abc12345"},
    )
    assert resp.status_code == 409
    assert "占用" in resp.json()["detail"]


async def test_denglu_chenggong(client):
    """注册后使用用户名与密码登录成功，返回新令牌对。

    Args:
        client: 内存 HTTP 客户端。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    data = await register_user(client, prefix="login")
    resp = await client.post(
        "/api/auth/login",
        json={"username": data["_payload"]["username"], "password": data["_payload"]["password"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["user"]["id"] == data["user"]["id"]


async def test_denglu_cuowu_mima_401(client):
    """密码错误时登录返回 401。

    Args:
        client: 内存 HTTP 客户端。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 401 时中断。
    """
    data = await register_user(client, prefix="badpw")
    resp = await client.post(
        "/api/auth/login",
        json={"username": data["_payload"]["username"], "password": "wrong-password"},
    )
    assert resp.status_code == 401
    assert "密码" in resp.json()["detail"] or "用户名" in resp.json()["detail"]


async def test_refresh_huanxin_access_token(client):
    """refresh 令牌可换发新 access 令牌，且新令牌可正常访问受保护接口。

    Args:
        client: 内存 HTTP 客户端。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    data = await register_user(client, prefix="refresh")
    resp = await client.post(
        "/api/auth/refresh", json={"refresh_token": data["refresh_token"]}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"] == data["refresh_token"]  # refresh 不换新

    # 新 access 令牌可用且指向同一用户
    resp2 = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert resp2.status_code == 200
    assert resp2.json()["user"]["id"] == data["user"]["id"]


async def test_wu_token_fangwen_shoubaohu_jiekou_401(client):
    """未携带令牌访问受保护接口返回 401。

    Args:
        client: 内存 HTTP 客户端。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 401 时中断。
    """
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401

    # 伪造令牌同样 401
    resp2 = await client.get(
        "/api/auth/me", headers={"Authorization": "Bearer not-a-valid-token"}
    )
    assert resp2.status_code == 401
