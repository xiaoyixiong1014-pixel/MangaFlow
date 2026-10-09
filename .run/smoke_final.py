# -*- coding: utf-8 -*-
"""MangaFlow 后端全链路冒烟脚本(文件级注释)。

面向已启动在 127.0.0.1:8300 的后端服务,串起注册-建项目-剧本-LLM 流式生成-
实体抽取确认-角色定妆-分镜拆解-关键帧打样-TTS-成片合成-发布分享-
管理端公告/积分/权限的全链路;任何一步失败会记录并在结尾汇总退出码。

使用 backend venv 中的 python 运行(依赖 httpx 与 asyncpg)。
"""

import asyncio
import json
import time

import asyncpg
import httpx

BASE = "http://127.0.0.1:8300"
DB_DSN = "postgresql://postgres:123456@localhost:5432/mangaflow"
FAILS = []
TS = int(time.time())
USERNAME = f"smoke_final_{TS}"
ANN_TITLE = f"集成验证公告 {TS}"


def log(msg: str) -> None:
    """打印带时间戳的日志行。"""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    """记录单步断言结果。

    Args:
        name: 步骤名。
        cond: 是否通过。
        detail: 附加信息。
    """
    tag = "PASS" if cond else "FAIL"
    log(f"[{tag}] {name}{(' | ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(f"{name}: {detail}")


def req(client: httpx.Client, method: str, path: str, token: str = None, **kw) -> httpx.Response:
    """发起带可选 Bearer 认证的 HTTP 请求。

    Args:
        client: httpx 客户端。
        method: HTTP 方法。
        path: 以 /api 开头的路径。
        token: 访问令牌(可空)。
        **kw: 透传给 httpx 的其余参数。

    Returns:
        httpx.Response: 响应对象。
    """
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.request(method, BASE + path, headers=headers, timeout=90, **kw)


def poll_task(client: httpx.Client, token: str, task_id: int, timeout: int = 30, label: str = "task") -> dict:
    """轮询任务直到终态或超时。

    Args:
        client: httpx 客户端。
        token: 用户令牌。
        task_id: 任务 ID。
        timeout: 最长轮询秒数。
        label: 日志标签。

    Returns:
        dict: 终态任务 JSON(超时则返回最后一次查询结果)。
    """
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        r = req(client, "GET", f"/api/tasks/{task_id}", token)
        if r.status_code == 200:
            last = r.json()
            if last.get("status") in ("done", "failed", "canceled"):
                break
        time.sleep(1)
    check(f"{label} 轮询终态", last.get("status") == "done", f"status={last.get('status')} err={last.get('error')}")
    return last


def sse_generate(client: httpx.Client, token: str, project_id: int) -> dict:
    """调用剧本 SSE 生成接口,收集 delta 与 done 事件。

    Args:
        client: httpx 客户端。
        token: 用户令牌。
        project_id: 项目 ID。

    Returns:
        dict: {"deltas": int, "done": dict|None, "error": str|None, "http": int}
    """
    out = {"deltas": 0, "done": None, "error": None, "http": 0}
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    try:
        with client.stream(
            "POST", f"{BASE}/api/projects/{project_id}/script/generate/stream",
            json={}, headers=headers, timeout=180,
        ) as r:
            out["http"] = r.status_code
            if r.status_code != 200:
                out["error"] = r.read().decode("utf-8", "ignore")[:300]
                return out
            event = None
            for line in r.iter_lines():
                if line is None:
                    continue
                line = line.strip()
                if line.startswith("event:"):
                    event = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    if event == "delta":
                        out["deltas"] += 1
                    elif event == "error":
                        out["error"] = payload
                    elif event == "done":
                        out["done"] = json.loads(payload)
                    event = None
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"stream exception: {exc}"
    return out


def main() -> int:
    """执行全链路冒烟主流程。

    Returns:
        int: 0 全部通过;1 存在失败。
    """
    log(f"== MangaFlow 全链路冒烟开始(用户 {USERNAME})==")
    with httpx.Client() as c:
        # 1. 注册
        r = req(c, "POST", "/api/auth/register", json={
            "username": USERNAME, "email": f"{USERNAME}@test.local", "password": "smoke123"})
        check("注册 smoke_final", r.status_code == 200, f"http={r.status_code} body={r.text[:200]}")
        user_token = r.json().get("access_token")
        check("注册返回 access_token", bool(user_token))

        # 2. 建项目
        r = req(c, "POST", "/api/projects", user_token, json={"title": "集成验证"})
        check("建项目(集成验证)", r.status_code in (200, 201), f"http={r.status_code} body={r.text[:200]}")
        project_id = r.json().get("id")
        check("项目 id 有效", bool(project_id), f"project_id={project_id}")

        # 3. 剧本输入
        raw_text = "林小雨在江南小镇经营一家濒临倒闭的旧书店,某个雨夜,一本书里掉出的老照片让她踏上寻找往事的旅程。"
        r = req(c, "PUT", f"/api/projects/{project_id}/script/input", user_token,
                json={"mode": "idea", "raw_text": raw_text})
        check("script/input 保存", r.status_code == 200, f"http={r.status_code} body={r.text[:200]}")

        # 4. SSE 流式生成
        sse = sse_generate(c, user_token, project_id)
        check("script/generate/stream HTTP 200", sse["http"] == 200, f"http={sse['http']} err={sse['error']}")
        check("SSE 收到 delta 增量", sse["deltas"] > 0, f"delta 帧数={sse['deltas']}")
        check("SSE 收到 done", sse["done"] is not None, f"error={sse['error']}")
        if sse["done"]:
            content = json.dumps(sse["done"].get("content", {}), ensure_ascii=False)
            check("done 携带剧本内容", len(content) > 50, f"len={len(content)}")

        # 5. 实体抽取
        r = req(c, "POST", f"/api/projects/{project_id}/script/extract", user_token, json={})
        check("script/extract", r.status_code == 200, f"http={r.status_code} body={r.text[:300]}")
        ex = r.json()
        check("抽取包含角色", len(ex.get("characters", [])) > 0,
              f"characters={[e.get('name') for e in ex.get('characters', [])]}")
        confirm = {
            "characters": [
                {"name": e.get("name"), "aliases": e.get("aliases") or [],
                 "description": e.get("description") or ""}
                for e in ex.get("characters", [])
            ],
            "scenes": [
                {"name": e.get("name"), "description": e.get("description") or ""}
                for e in ex.get("scenes", [])
            ],
            "props": [
                {"name": e.get("name"), "description": e.get("description") or ""}
                for e in ex.get("props", [])
            ],
        }
        r = req(c, "POST", f"/api/projects/{project_id}/script/extract/confirm", user_token, json=confirm)
        check("extract/confirm 入池", r.status_code == 200, f"http={r.status_code} body={r.text[:300]}")

        # 6. 资产有角色
        r = req(c, "GET", f"/api/projects/{project_id}/assets", user_token)
        check("assets 列表", r.status_code == 200, f"http={r.status_code}")
        assets = r.json()
        chars = assets.get("characters", [])
        check("assets 有角色", len(chars) > 0, f"角色数={len(chars)}")

        # 7. 角色定妆生成(冻结 10 积分)
        cid = chars[0]["id"]
        r = req(c, "POST", f"/api/projects/{project_id}/assets/characters/{cid}/generate", user_token,
                json={"prompt": None})
        check("characters/{id}/generate", r.status_code == 200, f"http={r.status_code} body={r.text[:300]}")
        gen_task = r.json()
        check("定妆任务冻结 10 积分", gen_task.get("cost_points") == 10, f"cost_points={gen_task.get('cost_points')}")
        poll_task(c, user_token, gen_task.get("id"), 30, "定妆 image 任务")

        # 8. 分镜拆解
        r = req(c, "POST", f"/api/projects/{project_id}/storyboards/decompose", user_token, json={})
        check("storyboards/decompose", r.status_code == 200, f"http={r.status_code} body={r.text[:300]}")

        r = req(c, "GET", f"/api/projects/{project_id}/storyboards", user_token)
        check("GET storyboards 列表", r.status_code == 200, f"http={r.status_code}")
        boards = r.json()
        check("分镜至少 1 个", len(boards) > 0, f"镜头数={len(boards)}")
        sid = boards[0]["id"] if boards else None

        # 9. 关键帧打样
        r = req(c, "POST", f"/api/projects/{project_id}/storyboards/{sid}/keyframes", user_token,
                json={"phase": "trial"})
        check("keyframes trial", r.status_code == 200, f"http={r.status_code} body={r.text[:300]}")
        kf = r.json()
        kf_tasks = kf.get("tasks", []) if isinstance(kf, dict) else []
        check("keyframes 返回任务", len(kf_tasks) > 0, f"keys={list(kf.keys()) if isinstance(kf, dict) else type(kf)}")
        for t in kf_tasks:
            poll_task(c, user_token, t.get("id"), 30, f"关键帧任务#{t.get('id')}")

        # 10. TTS 配音
        r = req(c, "POST", f"/api/projects/{project_id}/storyboards/{sid}/tts", user_token, json={})
        check("storyboards tts", r.status_code == 200, f"http={r.status_code} body={r.text[:300]}")
        tts_task = r.json()
        poll_task(c, user_token, tts_task.get("id"), 30, "tts 任务")

        # 11. actual_duration 回写校验(直查 DB)
        try:
            loop = asyncio.new_event_loop()
            try:
                async def _q():
                    _c = await asyncpg.connect(DB_DSN)
                    try:
                        return await _c.fetchrow(
                            "SELECT id, actual_duration FROM storyboards WHERE id=$1", sid)
                    finally:
                        await _c.close()
                row = loop.run_until_complete(_q())
            finally:
                loop.close()
            check("TTS 回写 actual_duration", row is not None and row["actual_duration"] is not None,
                  f"actual_duration={row['actual_duration'] if row else None}")
        except Exception as exc:  # noqa: BLE001
            check("TTS 回写 actual_duration", False, f"DB 查询异常: {exc}")

        # 12. 成片合成
        r = req(c, "POST", f"/api/projects/{project_id}/compose", user_token,
                json={"resolution": "720P", "mode": "full"})
        check("compose", r.status_code == 200, f"http={r.status_code} body={r.text[:300]}")
        compose_task = r.json()
        poll_task(c, user_token, compose_task.get("id"), 30, "compose 任务")

        # 13. outputs 有 is_current
        r = req(c, "GET", f"/api/projects/{project_id}/outputs", user_token)
        check("GET outputs", r.status_code == 200, f"http={r.status_code} body={r.text[:200]}")
        outs = r.json().get("items", [])
        check("outputs 存在", len(outs) > 0, f"版本数={len(outs)}")
        current = [o for o in outs if o.get("is_current")]
        check("outputs 有 is_current", len(current) == 1, f"is_current 数={len(current)}")

        # 14. 发布
        r = req(c, "POST", f"/api/projects/{project_id}/publish", user_token,
                json={"output_id": outs[0]["id"], "share_expires_days": 7})
        check("publish", r.status_code == 200, f"http={r.status_code} body={r.text[:300]}")
        share = r.json().get("share_url", "")
        token_part = share.rstrip("/").split("/")[-1]
        check("publish 返回 share_url", bool(token_part), f"share_url={share}")

        # 15. 无认证访问分享页
        r = c.get(f"{BASE}/api/share/{token_part}", timeout=30)
        check("GET /api/share/{token} 无认证 200", r.status_code == 200,
              f"http={r.status_code} body={r.text[:200]}")

        # 16. 管理端登录
        r = req(c, "POST", "/api/auth/login", json={"username": "admin", "password": "admin123"})
        check("admin 登录", r.status_code == 200, f"http={r.status_code} body={r.text[:200]}")
        admin_token = r.json().get("access_token")

        # 17. 建公告
        r = req(c, "POST", "/api/admin/content/announcements", admin_token,
                json={"title": ANN_TITLE, "content": "全链路集成验证自动公告", "is_pinned": False,
                      "status": "published"})
        check("admin 建公告", r.status_code in (200, 201), f"http={r.status_code} body={r.text[:300]}")

        # 18. 用户端公告可见(响应可能为 list 或 PageResult,均兼容)
        r = req(c, "GET", "/api/announcements", user_token)
        body = r.json() if r.status_code == 200 else []
        items = body if isinstance(body, list) else body.get("items", [])
        titles = [a.get("title") for a in items if isinstance(a, dict)]
        check("GET /api/announcements 可见新公告", ANN_TITLE in titles,
              f"http={r.status_code} titles={titles[:5]}")

        # 19. 当前用户 id(admin 积分调整;/api/auth/me 返回 {user, wallet})
        r = req(c, "GET", "/api/auth/me", user_token)
        uid = (r.json().get("user") or {}).get("id") if r.status_code == 200 else None
        check("GET /api/auth/me 拿到用户 id", bool(uid), f"uid={uid}")

        # 20. admin 积分调整
        r = req(c, "POST", f"/api/admin/users/{uid}/points", admin_token,
                json={"amount": 100, "reason": "集成验证积分调整"})
        check("admin/users 积分调整", r.status_code == 200, f"http={r.status_code} body={r.text[:300]}")

        # 21. 普通用户访问管理后台应 403
        r = req(c, "GET", "/api/admin/dashboard/overview", user_token)
        check("end_user 访问 admin overview 403", r.status_code == 403, f"http={r.status_code}")

    log("== 冒烟结束 ==")
    if FAILS:
        log(f"共 {len(FAILS)} 项失败:")
        for f in FAILS:
            log(f"  - {f}")
        return 1
    log("全部步骤通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
