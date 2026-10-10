# -*- coding: utf-8 -*-
"""MangaFlow Task 11 全链路集成冒烟脚本 v2(文件级注释)。

面向已启动在 127.0.0.1:8300 的后端服务,串起注册-建项目-剧本 SSE(真实 LLM)-
实体提取(含 HITL pending_confirm→resume approved 分支)-分镜-关键帧任务
(校验 task.meta.provider/fallback_chain 溯源)-合成(字幕+BGM 参数)-ZIP 导出-
分镜 xlsx 导出-通知未读数-管理端死信列表-/api/metrics 的完整链路;
逐步 PASS/FAIL 输出,任何一步失败记录并在结尾汇总退出码。

与 smoke_final.py 互补(不改动原文件):本版聚焦 Task 11 集成验证新增面
(HITL 恢复、任务 meta 溯源、双导出、metrics)。

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
USERNAME = f"smoke_v2_{TS}"


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


def req(client: httpx.Client, method: str, path: str, token: str = None, timeout: int = 90, **kw) -> httpx.Response:
    """发起带可选 Bearer 认证的 HTTP 请求。

    Args:
        client: httpx 客户端。
        method: HTTP 方法。
        path: 以 /api 开头的路径。
        token: 访问令牌(可空)。
        timeout: 请求超时秒数(真实 LLM 同步接口建议放宽至 300)。
        **kw: 透传给 httpx 的其余参数。

    Returns:
        httpx.Response: 响应对象。
    """
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.request(method, BASE + path, headers=headers, timeout=timeout, **kw)


def poll_task(client: httpx.Client, token: str, task_id: int, timeout: int = 45, label: str = "task") -> dict:
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
    check(f"{label} 终态", last.get("status") == "done",
          f"status={last.get('status')} err={last.get('error')}")
    return last


def sse_collect(client: httpx.Client, method: str, path: str, token: str, json_body: dict,
                timeout: int = 240) -> dict:
    """调用 SSE 流式接口并收集事件(通用辅助)。

    Args:
        client: httpx 客户端。
        method: HTTP 方法(POST)。
        path: 以 /api 开头的路径。
        token: 用户令牌。
        json_body: 请求体。
        timeout: 流读取超时秒数。

    Returns:
        dict: {"http": 状态码, "events": [(event, payload_str)], "error": str|None}。
    """
    out = {"http": 0, "events": [], "error": None}
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    event = None
    try:
        with client.stream(method, BASE + path, json=json_body, headers=headers,
                           timeout=timeout) as r:
            out["http"] = r.status_code
            if r.status_code != 200:
                out["error"] = r.read().decode("utf-8", "ignore")[:300]
                return out
            for line in r.iter_lines():
                if line is None:
                    continue
                line = line.strip()
                if line.startswith("event:"):
                    event = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    out["events"].append((event, line[len("data:"):].strip()))
                    event = None
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"stream exception: {exc}"
    return out


def db_query(query: str, *params):
    """同步执行一条只读 SQL 并返回行(asyncpg,直查主库用于 meta 溯源校验)。

    Args:
        query: SQL 语句(仅 SELECT)。
        *params: 绑定参数。

    Returns:
        list[asyncpg.Record]: 查询结果行;连接失败返回 None。
    """
    loop = asyncio.new_event_loop()
    try:
        async def _q():
            conn = await asyncpg.connect(DB_DSN)
            try:
                return await conn.fetch(query, *params)
            finally:
                await conn.close()
        return loop.run_until_complete(_q())
    except Exception as exc:  # noqa: BLE001
        log(f"      (DB 直查异常: {exc})")
        return None
    finally:
        loop.close()


def main() -> int:
    """执行 Task 11 集成冒烟主流程。

    Returns:
        int: 0 全部通过;1 存在失败。

    Raises:
        无:单步异常均收敛为 FAIL 记录。
    """
    log(f"== MangaFlow Task11 集成冒烟开始(用户 {USERNAME})==")
    with httpx.Client() as c:
        # ---------- 1. 注册 ----------
        r = req(c, "POST", "/api/auth/register", json={
            "username": USERNAME, "email": f"{USERNAME}@test.local", "password": "smoke123"})
        check("注册 smoke_v2 用户", r.status_code == 200, f"http={r.status_code} body={r.text[:200]}")
        token = r.json().get("access_token")
        check("注册返回 access_token", bool(token))

        # ---------- 2. 建项目 ----------
        r = req(c, "POST", "/api/projects", token, json={"title": f"Task11集成验证{TS}"})
        check("建项目", r.status_code in (200, 201), f"http={r.status_code} body={r.text[:200]}")
        pid = r.json().get("id")
        check("项目 id 有效", bool(pid), f"project_id={pid}")

        # ---------- 3. 剧本 SSE(真实 LLM,DeepSeek 失败自动降级 Agnes) ----------
        raw_text = "少年阿澜在渔村码头捡到一枚会发光的螺壳,夜里海妖的歌声引他驶向迷雾深处的沉船。"
        r = req(c, "PUT", f"/api/projects/{pid}/script/input", token,
                json={"mode": "idea", "raw_text": raw_text})
        check("script/input 保存", r.status_code == 200, f"http={r.status_code} body={r.text[:200]}")

        sse = sse_collect(c, "POST", f"/api/projects/{pid}/script/generate/stream", token, {})
        check("剧本 SSE HTTP 200", sse["http"] == 200, f"http={sse['http']} err={sse['error']}")
        names = [e for e, _ in sse["events"]]
        deltas = names.count("delta")
        check("剧本 SSE 收到 done", "done" in names, f"事件={names[:8]}")
        check("剧本 SSE 有产出(delta/message)", deltas > 0 or "message" in names,
              f"delta={deltas} (LLM 主渠道欠费时走 Agnes 兜底或离线模板降级)")

        # ---------- 4. 实体提取(若触发 HITL pending_confirm 则 resume approved) ----------
        r = req(c, "POST", f"/api/projects/{pid}/script/extract", token, json={})
        check("script/extract", r.status_code == 200, f"http={r.status_code} body={r.text[:300]}")
        ex = r.json()
        check("抽取包含角色", len(ex.get("characters", [])) > 0,
              f"characters={[e.get('name') for e in ex.get('characters', [])]}")
        confirm = {
            "characters": [{"name": e.get("name"), "aliases": e.get("aliases") or [],
                            "description": e.get("description") or ""}
                           for e in ex.get("characters", [])],
            "scenes": [{"name": e.get("name"), "description": e.get("description") or ""}
                       for e in ex.get("scenes", [])],
            "props": [{"name": e.get("name"), "description": e.get("description") or ""}
                      for e in ex.get("props", [])],
        }
        r = req(c, "POST", f"/api/projects/{pid}/script/extract/confirm", token, json=confirm)
        check("extract/confirm 入池", r.status_code == 200, f"http={r.status_code} body={r.text[:300]}")

        # ---------- 4b. 画布助手 HITL:script 意图触发 interrupt → resume approved ----------
        log("      (画布助手 HITL 链路:发送剧本意图消息…)")
        hitl = sse_collect(c, "POST", "/api/agent/chat/stream", token,
                           {"message": "帮我把这个渔村故事写成完整剧本", "project_id": pid},
                           timeout=300)
        check("agent chat/stream HTTP 200", hitl["http"] == 200,
              f"http={hitl['http']} err={hitl['error']}")
        hnames = [e for e, _ in hitl["events"]]
        pc_payload = next((p for e, p in hitl["events"] if e == "pending_confirm"), None)
        if pc_payload:
            # 触发 HITL:POST resume approved=True 继续
            try:
                pc = json.loads(pc_payload)
            except (json.JSONDecodeError, ValueError):
                pc = {}
            thread_id = pc.get("thread_id") or ""
            check("HITL pending_confirm 带 thread_id", bool(thread_id), f"payload={pc_payload[:200]}")
            resume = sse_collect(c, "POST", "/api/agent/chat/resume", token,
                                 {"thread_id": thread_id, "approved": True}, timeout=300)
            rnames = [e for e, _ in resume["events"]]
            check("HITL resume approved 收到 done", "done" in rnames,
                  f"http={resume['http']} 事件={rnames[:8]} err={resume['error']}")
        else:
            # 未触发 interrupt(意图路由/渠道降级差异):仅要求流正常结束
            check("agent chat 正常完成(未触发 HITL,条件分支)", "done" in hnames,
                  f"事件={hnames[:8]}")

        # ---------- 5. 分镜拆解 ----------
        r = req(c, "POST", f"/api/projects/{pid}/storyboards/decompose", token, json={}, timeout=300)
        check("storyboards/decompose", r.status_code == 200, f"http={r.status_code} body={r.text[:300]}")
        r = req(c, "GET", f"/api/projects/{pid}/storyboards", token)
        boards = r.json() if r.status_code == 200 else []
        check("分镜至少 1 个", isinstance(boards, list) and len(boards) > 0,
              f"http={r.status_code} 镜头数={len(boards) if isinstance(boards, list) else 'N/A'}")
        sid = boards[0]["id"] if isinstance(boards, list) and boards else None

        # ---------- 6. 提交 1 张关键帧任务并校验 meta.provider/fallback_chain ----------
        r = req(c, "POST", f"/api/projects/{pid}/storyboards/{sid}/keyframes", token,
                json={"phase": "trial"})
        check("keyframes trial 提交", r.status_code == 200, f"http={r.status_code} body={r.text[:300]}")
        kf_tasks = r.json().get("tasks", []) if r.status_code == 200 else []
        check("keyframes 返回 1 个任务", len(kf_tasks) == 1, f"任务数={len(kf_tasks)}")
        if kf_tasks:
            kf = poll_task(c, token, kf_tasks[0]["id"], 150, "关键帧 image 任务")
            rows = db_query("SELECT meta FROM tasks WHERE id=$1", kf_tasks[0]["id"])
            meta = {}
            if rows:
                raw = rows[0]["meta"]
                meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
            check("task.meta 记录 provider", bool(meta.get("provider")),
                  f"provider={meta.get('provider')} (mock 链尾兜底为 mock)")
            check("task.meta 记录 fallback_chain", "fallback_chain" in meta,
                  f"fallback_chain={json.dumps(meta.get('fallback_chain'), ensure_ascii=False)[:200]}")
            check("关键帧结果地址回写", bool(kf.get("result_url")), f"result_url={kf.get('result_url')}")

        # ---------- 7. 合成(字幕 + BGM 参数) ----------
        r = req(c, "POST", f"/api/projects/{pid}/compose", token, json={
            "resolution": "720P", "mode": "full",
            "subtitles": True, "subtitle_style": {"font_size": 24, "position": "bottom"},
            "bgm": {"url": None, "volume": 0.3},
        })
        check("compose 提交(字幕+BGM 参数)", r.status_code == 200,
              f"http={r.status_code} body={r.text[:300]}")
        if r.status_code == 200:
            compose_task = r.json()
            poll_task(c, token, compose_task.get("id"), 90, "compose 任务")
            r = req(c, "GET", f"/api/projects/{pid}/outputs", token)
            outs = r.json().get("items", []) if r.status_code == 200 else []
            check("outputs 有当前版本", any(o.get("is_current") for o in outs),
                  f"版本数={len(outs)}")

        # ---------- 8. ZIP 导出下载 ----------
        r = req(c, "GET", f"/api/projects/{pid}/export", token)
        body = r.content if r.status_code == 200 else b""
        check("项目 ZIP 导出", r.status_code == 200 and body[:2] == b"PK",
              f"http={r.status_code} content-type={r.headers.get('content-type')} size={len(body)}")

        # ---------- 9. 分镜 xlsx 导出(episode_id=0 全量) ----------
        r = req(c, "GET", f"/api/projects/{pid}/storyboards/episodes/0/export?format=xlsx", token)
        body = r.content if r.status_code == 200 else b""
        check("分镜 xlsx 导出", r.status_code == 200 and body[:2] == b"PK",
              f"http={r.status_code} size={len(body)}")

        # ---------- 10. 通知 unread-count ----------
        r = req(c, "GET", "/api/notifications/unread-count", token)
        unread = r.json().get("unread_count") if r.status_code == 200 else None
        check("通知 unread-count", r.status_code == 200 and unread is not None,
              f"http={r.status_code} unread_count={unread}")

        # ---------- 11. 管理端死信列表 ----------
        r = req(c, "POST", "/api/auth/login", json={"username": "admin", "password": "admin123"})
        admin_token = r.json().get("access_token") if r.status_code == 200 else None
        check("admin 登录", bool(admin_token), f"http={r.status_code}")
        r = req(c, "GET", "/api/admin/tasks/dead-letters?page=1&page_size=10", admin_token)
        check("admin 死信列表", r.status_code == 200 and "total" in r.json(),
              f"http={r.status_code} body={r.text[:200]}")

        # ---------- 12. Prometheus 指标 ----------
        r = c.get(f"{BASE}/api/metrics", timeout=30)
        text = r.text if r.status_code == 200 else ""
        check("GET /api/metrics", r.status_code == 200 and len(text) > 50,
              f"http={r.status_code} bytes={len(text)}")

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
