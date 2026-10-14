"""分镜表 PDF 导出测试模块（文件级说明，SB-08）。

覆盖 GET /api/projects/{project_id}/storyboards/episodes/{episode_id}/export
的 format=pdf 链路：
- 正常导出：200、Content-Type application/pdf、响应体以 %PDF 开头、
  长度 >= 100 字节；
- 字体降级：monkeypatch 强制字体探测失败（无字体环境模拟）时不 500，
  仍返回合法 PDF（英文兜底版）并携带 X-PDF-Font-Fallback 响应头；
- 归属校验：他人项目导出 404（不泄露存在性）；
- 参数校验：format 非法值 400。
"""

import pytest

from tests.conftest import create_project, insert_storyboard, register_user, unique_suffix


async def _make_project_with_shots(client, auth, db, *, shots: int = 3) -> dict:
    """辅助：建项目并直插若干分镜（导出数据前置，内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        shots: 插入的分镜数量。

    Returns:
        dict: 项目响应 JSON。

    Raises:
        AssertionError: 项目创建返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"PDF项目_{unique_suffix()}")
    for seq in range(1, shots + 1):
        await insert_storyboard(
            db,
            project["id"],
            sequence=seq,
            duration=4 + seq,
            visual_description=f"天台远眺的城市黄昏（第{seq}镜）",
            dialogue="林小满：我们一起做动画吧。",
        )
    return project


async def test_pdf_daochu_200_pdf_tou_bao(client, auth, db):
    """format=pdf 导出：200 + application/pdf + %PDF 魔数 + >= 100 字节。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await _make_project_with_shots(client, auth, db)

    resp = await client.get(
        f"/api/projects/{project['id']}/storyboards/episodes/0/export",
        headers=auth["headers"],
        params={"format": "pdf"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/pdf")
    body = resp.content
    assert body[:4] == b"%PDF"
    assert len(body) >= 100
    # Content-Disposition 指向 .pdf 文件名
    assert ".pdf" in resp.headers.get("content-disposition", "")


async def test_pdf_wu_ziti_jiangji_bu_500(client, auth, db, monkeypatch):
    """字体探测失败（无字体环境）时降级英文兜底版：仍 200 且不 500。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        monkeypatch: pytest monkeypatch 夹具（强制 detect_cjk_font 返回 None）。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 200 或响应体非 PDF 时中断。
    """
    from app.services import pdf_export_service

    project = await _make_project_with_shots(client, auth, db)

    # 强制字体探测失败，模拟无中文字体环境（fpdf2 内置 helvetica 兜底）
    monkeypatch.setattr(pdf_export_service, "detect_cjk_font", lambda: None)

    resp = await client.get(
        f"/api/projects/{project['id']}/storyboards/episodes/0/export",
        headers=auth["headers"],
        params={"format": "pdf"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content[:4] == b"%PDF"
    # 降级时路由层应在响应头标记（前端据此提示）
    assert resp.headers.get("x-pdf-font-fallback") == "true"
    assert len(resp.content) >= 100


async def test_pdf_yuequan_404(client, auth, db):
    """他人项目导出 PDF 返回 404（不泄露存在性）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文（项目归属者）。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 404 时中断。
    """
    project = await _make_project_with_shots(client, auth, db)
    other = await register_user(client, prefix="pdfnoer")

    resp = await client.get(
        f"/api/projects/{project['id']}/storyboards/episodes/0/export",
        headers={"Authorization": f"Bearer {other['access_token']}"},
        params={"format": "pdf"},
    )
    assert resp.status_code == 404


async def test_pdf_format_feifa_400(client, auth, db):
    """format 非法值（docx）返回 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 状态码非 400 时中断。
    """
    project = await _make_project_with_shots(client, auth, db)

    resp = await client.get(
        f"/api/projects/{project['id']}/storyboards/episodes/0/export",
        headers=auth["headers"],
        params={"format": "docx"},
    )
    assert resp.status_code == 400, resp.text
