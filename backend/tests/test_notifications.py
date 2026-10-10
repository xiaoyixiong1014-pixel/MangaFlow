"""通知中心测试模块（文件级说明）。

覆盖链路：
- 便捷函数创建通知（task_done/task_failed）→ 列表接口倒序返回 + 未读数；
- 未读数接口；
- 标记已读：按 ID 批量 / 全部 / 参数缺失 400；
- 配额预警当日去重（未读同类跳过，已读后可再次发送）；
- 进程内 SSE 广播总线 subscribe/_bus_publish 单元行为。

说明：便捷函数族自管理会话，其 SessionLocal 已由 conftest.task_runner
重定向到测试库，直接 await 调用即可。
"""

import asyncio
import uuid

from app.services import notification_service


async def test_task_done_failed_tongzhi_liebiao_yu_weidu(client, auth):
    """便捷函数创建完成/失败通知：列表倒序返回，未读数正确。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    user_id = auth["user_id"]
    await notification_service.notify_task_done(user_id, 101, task_type="image")
    await notification_service.notify_task_failed(user_id, 102, task_type="tts", reason="供应商超时")

    resp = await client.get("/api/notifications", headers=auth["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert body["unread_count"] == 2
    # 创建时间倒序：后创建的 task_failed 排在前面
    assert body["items"][0]["type"] == "task_failed"
    assert body["items"][1]["type"] == "task_done"
    assert body["items"][0]["related_type"] == "task"
    assert "供应商超时" in body["items"][0]["content"]

    unread = await client.get("/api/notifications/unread-count", headers=auth["headers"])
    assert unread.status_code == 200, unread.text
    assert unread.json()["unread_count"] == 2


async def test_tongzhi_yonghu_isolation(client, auth):
    """通知用户隔离：A 用户的通知不出现在 B 用户列表。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文（A）。

    Returns:
        None。

    Raises:
        AssertionError: B 用户能看到 A 的通知时中断。
    """
    other = await client.post(
        "/api/auth/register",
        json={
            "username": f"other_{uuid.uuid4().hex[:10]}",
            "email": f"other_{uuid.uuid4().hex[:10]}@mangaflow-test.com",
            "password": "Passw0rd!123",
        },
    )
    assert other.status_code == 200, other.text
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

    await notification_service.notify_task_done(auth["user_id"], 201, task_type="image")

    mine = await client.get("/api/notifications", headers=auth["headers"])
    assert mine.json()["total"] == 1
    theirs = await client.get("/api/notifications", headers=other_headers)
    assert theirs.json()["total"] == 0
    assert theirs.json()["unread_count"] == 0


async def test_biaoji_yidu_by_ids_and_all(client, auth):
    """标记已读：按 ID 批量与全部标记均生效，幂等重复标记 updated=0。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    user_id = auth["user_id"]
    await notification_service.notify_task_done(user_id, 301, task_type="image")
    await notification_service.notify_task_done(user_id, 302, task_type="video")

    listing = await client.get("/api/notifications", headers=auth["headers"])
    ids = [item["id"] for item in listing.json()["items"]]

    # 按 ID 标记第一条已读
    resp = await client.post(
        "/api/notifications/read", headers=auth["headers"], json={"ids": [ids[0]]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1
    unread = await client.get("/api/notifications/unread-count", headers=auth["headers"])
    assert unread.json()["unread_count"] == 1

    # 幂等：重复标记同一条 updated=0
    resp2 = await client.post(
        "/api/notifications/read", headers=auth["headers"], json={"ids": [ids[0]]}
    )
    assert resp2.status_code == 200
    assert resp2.json()["updated"] == 0

    # 全部标记
    resp3 = await client.post("/api/notifications/read", headers=auth["headers"], json={"all": True})
    assert resp3.status_code == 200, resp3.text
    assert resp3.json()["updated"] == 1
    unread2 = await client.get("/api/notifications/unread-count", headers=auth["headers"])
    assert unread2.json()["unread_count"] == 0

    # 已读通知 read_at 非空
    listing2 = await client.get("/api/notifications", headers=auth["headers"])
    assert all(item["read_at"] is not None for item in listing2.json()["items"])


async def test_biaoji_yidu_canshu_missing_400(client, auth):
    """标记已读请求缺参：ids 与 all 均未提供返回 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 响应码非 400 时中断。
    """
    resp = await client.post("/api/notifications/read", headers=auth["headers"], json={})
    assert resp.status_code == 400, resp.text

    # 空列表同样视为缺参
    resp2 = await client.post("/api/notifications/read", headers=auth["headers"], json={"ids": []})
    assert resp2.status_code == 400, resp2.text


async def test_weidu_requires_auth(client):
    """未认证访问通知接口返回 401。

    Args:
        client: 内存 HTTP 客户端。

    Returns:
        None。

    Raises:
        AssertionError: 响应码非 401 时中断。
    """
    resp = await client.get("/api/notifications")
    assert resp.status_code == 401, resp.text


async def test_quota_warning_dangri_dedup(client, auth):
    """配额预警当日去重：未读同类存在时跳过，标记已读后可再次发送。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 去重失效（出现 2 条未读同类）时中断。
    """
    user_id = auth["user_id"]
    await notification_service.notify_quota_warning(user_id, 150)
    await notification_service.notify_quota_warning(user_id, 120)

    listing = await client.get(
        "/api/notifications", headers=auth["headers"], params={"type": "quota_warning"}
    )
    body = listing.json()
    assert body["total"] == 1, f"期望去重后仅 1 条，实际 {body['total']}"
    assert body["items"][0]["type"] == "quota_warning"

    # 标记已读后再次预警：允许再发一条
    await client.post("/api/notifications/read", headers=auth["headers"], json={"all": True})
    await notification_service.notify_quota_warning(user_id, 90)
    listing2 = await client.get("/api/notifications", headers=auth["headers"])
    quota_items = [i for i in listing2.json()["items"] if i["type"] == "quota_warning"]
    assert len(quota_items) == 2


async def test_bus_subscribe_publish_fanout():
    """广播总线单元行为：subscribe 后 _bus_publish 扇出负载到队列。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 队列未收到负载或字段缺失时中断。
    """
    queue = notification_service.subscribe(9527)
    try:
        notification_service._bus_publish(9527, {"id": 1, "type": "task_done", "title": "t"})
        payload = queue.get_nowait()
        assert payload["id"] == 1
        assert payload["type"] == "task_done"
        # 未订阅用户发布不抛异常（无人接收）
        notification_service._bus_publish(8888, {"id": 2})
    finally:
        notification_service.unsubscribe(9527, queue)
    # 注销后再次发布不入队
    notification_service._bus_publish(9527, {"id": 3})
    assert queue.empty()


async def test_notify_type_yanzhi_yu_bianjie(client, auth):
    """通知类型校验与便捷函数：非法类型触发 ValueError，便捷函数路径正常落库。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 非法类型未抛 ValueError，或审核结果通知未落库时中断。
    """
    async def _boom():
        """以非法类型调用 create_notification（内部函数）。

        Raises:
            ValueError: 非法通知类型。
        """
        async with notification_service.SessionLocal() as session:
            await notification_service.create_notification(
                session, user_id=auth["user_id"], type="bad_type", title="t", content="c"
            )

    try:
        await _boom()
        raised = False
    except ValueError:
        raised = True
    assert raised is True, "非法类型应触发 ValueError"

    # 便捷函数路径：审核结果通知正常落库
    await notification_service.notify_review_result(
        auth["user_id"], 401, result="approved", suggestion=None
    )
    listing = await client.get("/api/notifications", headers=auth["headers"])
    types = [i["type"] for i in listing.json()["items"]]
    assert "review_result" in types


async def test_sse_stream_tuijin_xiaoxi(client, auth, db):
    """SSE 实时推送：订阅广播总线后创建的通知以 message 事件帧推送。

    实现说明：httpx ASGITransport 为缓冲式实现（收齐整个响应体才返回），
    无法对常驻 SSE 流做端到端流式请求；故直接驱动路由函数返回的
    StreamingResponse.body_iterator，等价覆盖「subscribe → 广播 → SSE 帧」
    全链路（与 agent SSE 测试同属进程内验证）。

    Args:
        client: 内存 HTTP 客户端（用于注册用户上下文）。
        auth: 随机注册用户上下文。
        db: 测试库会话（获取 User ORM 对象直传路由函数）。

    Returns:
        None。

    Raises:
        AssertionError: 未收到 message 帧、帧格式错误或含他人数据时中断。
    """
    from app.api.notifications import stream_notifications
    from app.models import User

    user = await db.get(User, auth["user_id"])
    response = await stream_notifications(user=user)
    body_iter = response.body_iterator

    received: bytes = b""
    stream_task = asyncio.create_task(body_iter.__anext__())
    try:
        # 等待生成器首次迭代完成 subscribe，再触发广播
        await asyncio.sleep(0.3)
        await notification_service.notify_task_done(auth["user_id"], 501, task_type="image")
        received = await asyncio.wait_for(stream_task, timeout=5)
    finally:
        if not stream_task.done():
            stream_task.cancel()
            try:
                await stream_task
            except (asyncio.CancelledError, Exception):
                pass
        await body_iter.aclose()  # 触发生成器 finally，注销订阅

    assert response.media_type == "text/event-stream"
    assert b"event: message" in received, f"SSE 首帧非 message 事件：{received!r}"
    assert b"task_done" in received
    # 注销后广播总线中不应残留该用户的订阅
    assert notification_service._SUBSCRIBERS.get(auth["user_id"]) is None
