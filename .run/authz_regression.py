# -*- coding: utf-8 -*-
"""MangaFlow 越权防护回归脚本(文件级注释)。

面向已启动在 127.0.0.1:8300 的后端服务(可 argv 覆盖基址),验证水平/垂直越权防护:
1. 注册随机用户 A 与用户 B;
2. A 建项目并造资源(剧本输入/角色/定妆任务/画布/上传媒体);
3. B 逐一 GET/PUT/DELETE 访问 A 的资源端点,断言 404/403(不允许 200 或数据泄漏);
4. B 调用 /api/admin/* 断言 403(垂直越权);
5. 附加:无认证访问受保护端点断言 401;
6. 汇总打印 N 项 PASS/FAIL,存在 FAIL 时退出码非 0。

说明:
- 接口路径以实际代码为准;静态确认不存在的端点(projects/{id}/export、
  outputs download)或请求体无法静态确定的端点(projects/{id}/script PUT)
  跳过并在对应位置注释;
- B 的 DELETE 越权请求放在同资源组末尾,即使某步异常也不影响其余断言。

使用 backend venv 中的 python 运行(依赖 httpx)。
"""

import sys
import time

import httpx

# 基址:默认 8300,可由第一个命令行参数覆盖
BASE = "http://127.0.0.1:8300"
if len(sys.argv) > 1:
    BASE = sys.argv[1].rstrip("/")

FAILS = []
PASSES = []
TS = int(time.time())
USER_A = f"authz_a_{TS}"
USER_B = f"authz_b_{TS}"
PASSWORD = "authz123"

# 越权访问的合法安全响应:403(明确拒绝) / 404(统一不存在,防资源枚举)
DENIED_STATUSES = (403, 404)


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
    if cond:
        PASSES.append(name)
    else:
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


def expect_denied(client: httpx.Client, method: str, path: str, token: str, name: str, **kw) -> None:
    """以 B 的令牌访问 A 的资源端点,断言被拒绝(403/404)。

    Args:
        client: httpx 客户端。
        method: HTTP 方法。
        path: 目标路径(A 的资源)。
        token: 用户 B 的访问令牌。
        name: 断言名称。
        **kw: 透传给 httpx 的其余参数(json/files 等)。
    """
    r = req(client, method, path, token, **kw)
    ok = r.status_code in DENIED_STATUSES
    check(name, ok, f"http={r.status_code} body={r.text[:150]}")


def main() -> int:
    """执行越权防护回归主流程。

    Returns:
        int: 0 全部通过;1 存在失败。
    """
    log(f"== MangaFlow 越权防护回归开始(基址 {BASE})==")
    with httpx.Client() as c:
        # ---------- 1. 注册用户 A 与用户 B(随机邮箱) ----------
        tokens = {}
        for who in ("a", "b"):
            username = USER_A if who == "a" else USER_B
            r = req(c, "POST", "/api/auth/register", json={
                "username": username, "email": f"{username}@test.local", "password": PASSWORD})
            check(f"注册用户 {username}", r.status_code == 200,
                  f"http={r.status_code} body={r.text[:150]}")
            tokens[who] = r.json().get("access_token")
            check(f"用户 {username} 获得 access_token", bool(tokens[who]))
        token_a, token_b = tokens.get("a"), tokens.get("b")
        if not (token_a and token_b):
            log("注册失败,无法继续")
            return 1

        # ---------- 2. 用户 A 建项目并造资源 ----------
        r = req(c, "POST", "/api/projects", token_a, json={"title": f"越权回归-{TS}"})
        check("A 建项目", r.status_code in (200, 201), f"http={r.status_code} body={r.text[:150]}")
        pid = r.json().get("id")
        check("A 项目 id 有效", bool(pid), f"project_id={pid}")

        # 2.1 剧本输入(script 资源)
        r = req(c, "PUT", f"/api/projects/{pid}/script/input", token_a,
                json={"mode": "idea", "raw_text": f"越权回归剧本输入 {TS}。"})
        check("A 保存 script/input", r.status_code == 200, f"http={r.status_code} body={r.text[:150]}")

        # 2.2 角色资产
        r = req(c, "POST", f"/api/projects/{pid}/assets/characters", token_a,
                json={"name": f"越权角色{TS}"})
        check("A 建角色", r.status_code in (200, 201), f"http={r.status_code} body={r.text[:150]}")
        cid = r.json().get("id")
        check("A 角色 id 有效", bool(cid), f"character_id={cid}")

        # 2.3 触发定妆生成(拿到 A 的任务 id;runner 异步执行,存在即可)
        task_id = None
        if cid:
            r = req(c, "POST", f"/api/projects/{pid}/assets/characters/{cid}/generate", token_a,
                    json={"prompt": None})
            check("A 触发角色定妆任务", r.status_code == 200, f"http={r.status_code} body={r.text[:150]}")
            task_id = r.json().get("id")
            check("A 任务 id 有效", bool(task_id), f"task_id={task_id}")

        # 2.4 画布(挂到 A 的项目;响应为 {canvas: {...}} 或平铺结构,均兼容)
        r = req(c, "POST", "/api/canvases", token_a,
                json={"title": f"越权画布{TS}", "project_id": pid})
        check("A 建画布", r.status_code in (200, 201), f"http={r.status_code} body={r.text[:150]}")
        _canvas_body = r.json() if r.status_code in (200, 201) else {}
        canvas_id = (_canvas_body.get("canvas") or {}).get("id") or _canvas_body.get("id")
        check("A 画布 id 有效", bool(canvas_id), f"canvas_id={canvas_id}")

        # 2.5 上传媒体文件
        r = req(c, "POST", "/api/media/upload", token_a,
                files={"file": ("probe.png", b"\x89PNG\r\n\x1a\nauthz-probe", "image/png")},
                data={"project_id": str(pid)})
        check("A 上传媒体", r.status_code == 200, f"http={r.status_code} body={r.text[:150]}")
        media_id = r.json().get("id")
        check("A 媒体 id 有效", bool(media_id), f"media_id={media_id}")

        # 2.6 A 的分镜(需要 decompose 依赖 LLM,此处仅尝试读取;无数据则跳过对应越权项)
        sid = None
        r = req(c, "GET", f"/api/projects/{pid}/storyboards", token_a)
        if r.status_code == 200:
            boards = r.json()
            if isinstance(boards, list) and boards:
                sid = boards[0].get("id")
        log(f"A 分镜数据: {'id=' + str(sid) if sid else '无(跳过分镜详情越权项)'}")

        # ---------- 3. 用户 B 水平越权访问 A 的资源 ----------
        # 说明:实际代码中不存在 GET /api/projects/{id}/export 端点(项目导出未实现),跳过;
        # PUT /api/projects/{id}/script 请求体 schema 无法静态确认(避免 422 假阳性),
        # 其越权防护由 script/input PUT 同路径前缀覆盖,跳过。
        expect_denied(c, "GET", f"/api/projects/{pid}", token_b, "B GET projects/{id}")
        expect_denied(c, "PUT", f"/api/projects/{pid}", token_b, "B PUT projects/{id}",
                      json={"title": "hijack"})
        expect_denied(c, "DELETE", f"/api/projects/{pid}", token_b, "B DELETE projects/{id}")
        expect_denied(c, "POST", f"/api/projects/{pid}/duplicate", token_b, "B POST projects/{id}/duplicate")
        expect_denied(c, "GET", f"/api/projects/{pid}/snapshots", token_b, "B GET projects/{id}/snapshots")

        # script 相关
        expect_denied(c, "GET", f"/api/projects/{pid}/script", token_b, "B GET script")
        expect_denied(c, "PUT", f"/api/projects/{pid}/script/input", token_b, "B PUT script/input",
                      json={"mode": "idea", "raw_text": "hijack"})

        # assets 相关
        expect_denied(c, "GET", f"/api/projects/{pid}/assets", token_b, "B GET assets 列表")
        if cid:
            expect_denied(c, "PUT", f"/api/projects/{pid}/assets/characters/{cid}", token_b,
                          "B PUT characters/{id}", json={"description": "hijack"})
            expect_denied(c, "POST", f"/api/projects/{pid}/assets/characters/{cid}/generate", token_b,
                          "B POST characters/{id}/generate", json={"prompt": None})
            expect_denied(c, "DELETE", f"/api/projects/{pid}/assets/characters/{cid}", token_b,
                          "B DELETE characters/{id}")

        # storyboards 相关
        expect_denied(c, "GET", f"/api/projects/{pid}/storyboards", token_b, "B GET storyboards 列表")
        if sid:
            expect_denied(c, "PUT", f"/api/projects/{pid}/storyboards/{sid}", token_b,
                          "B PUT storyboards/{id}", json={"visual_description": "hijack"})

        # tasks 相关
        expect_denied(c, "GET", f"/api/projects/{pid}/tasks", token_b, "B GET projects/{id}/tasks")
        if task_id:
            expect_denied(c, "GET", f"/api/tasks/{task_id}", token_b, "B GET tasks/{id}")
            expect_denied(c, "POST", f"/api/tasks/{task_id}/cancel", token_b, "B POST tasks/{id}/cancel")

        # canvases 相关
        if canvas_id:
            expect_denied(c, "GET", f"/api/canvases/{canvas_id}", token_b, "B GET canvases/{id}")
            expect_denied(c, "PUT", f"/api/canvases/{canvas_id}", token_b, "B PUT canvases/{id}",
                          json={"title": "hijack"})
            expect_denied(c, "PUT", f"/api/canvases/{canvas_id}/graph", token_b, "B PUT canvases/{id}/graph",
                          json={"nodes": [], "edges": []})
            expect_denied(c, "POST", f"/api/canvases/{canvas_id}/export", token_b, "B POST canvases/{id}/export")
            expect_denied(c, "DELETE", f"/api/canvases/{canvas_id}", token_b, "B DELETE canvases/{id}")

        # media 相关
        if media_id:
            expect_denied(c, "GET", f"/api/media/{media_id}", token_b, "B GET media/{id}")
            expect_denied(c, "PUT", f"/api/media/{media_id}/meta", token_b, "B PUT media/{id}/meta",
                          json={"meta": {"hijack": True}})
            expect_denied(c, "DELETE", f"/api/media/{media_id}", token_b, "B DELETE media/{id}")

        # outputs 相关
        # 说明:实际代码中不存在 outputs download 端点(publish.py 仅有 outputs 列表/
        # set-current,A 侧无成片数据时 set-current 无法构造有效 output_id),跳过。
        expect_denied(c, "GET", f"/api/projects/{pid}/outputs", token_b, "B GET outputs 列表")

        # ---------- 4. 用户 B 垂直越权访问管理端(期望 403) ----------
        admin_paths = [
            ("GET", "/api/admin/dashboard/overview"),
            ("GET", "/api/admin/dashboard/alerts"),
            ("GET", "/api/admin/system-metrics"),
            ("GET", "/api/admin/users"),
            ("GET", "/api/admin/models/channels"),
            ("GET", "/api/admin/orders"),
            ("GET", "/api/admin/content/announcements"),
            ("GET", "/api/admin/review"),
            ("GET", "/api/admin/settings"),
        ]
        for method, path in admin_paths:
            r = req(c, method, path, token_b)
            check(f"B {method} {path} 403", r.status_code == 403,
                  f"http={r.status_code} body={r.text[:120]}")

        # ---------- 5. 附加:无认证访问受保护端点(期望 401) ----------
        for path in ("/api/projects", "/api/canvases", "/api/media", "/api/auth/me"):
            r = req(c, "GET", path)
            check(f"无认证 GET {path} 401", r.status_code == 401,
                  f"http={r.status_code} body={r.text[:120]}")

    log("== 越权回归结束 ==")
    log(f"汇总: {len(PASSES)} 项 PASS, {len(FAILS)} 项 FAIL")
    if FAILS:
        log("失败项:")
        for f in FAILS:
            log(f"  - {f}")
        return 1
    log("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
