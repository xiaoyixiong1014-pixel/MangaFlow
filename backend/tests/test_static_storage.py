"""静态存储挂载与生成产物可访问性测试模块（文件级说明）。

回归覆盖 C-BUG-2（生成产物 404）：
- /storage 挂载点必须使用 settings.storage_path()（PROJECT_ROOT 基准的绝对
  路径），与产物落盘目录（providers/tasks 统一传入 storage_path()/generated）
  严格一致 —— 历史缺陷：挂载用了相对 settings.storage_dir，服务以 cwd=backend
  启动时解析为 backend/backend/storage，所有 /storage/* 产物一律 404；
- mock 与真实渠道两条生成路径的 URL 前缀常量均与挂载前缀一致；
- 产物落盘后经 TestClient 访问 /storage/... 必须 200（媒体记录 url 与挂载
  联动端到端验证）。

说明：conftest 的内存客户端基于 ASGITransport，不触发 lifespan，但
StaticFiles 挂载在 create_app()（模块导入时）即已注册，请求可正常命中。
"""

from pathlib import Path

from starlette.routing import Mount

from app.core.config import settings
from app.main import app as fastapi_app
from tests.conftest import create_project


def _get_storage_mount() -> Mount:
    """获取应用的 /storage 静态挂载路由（内部函数）。

    Returns:
        Mount: name="storage" 的挂载路由实例。

    Raises:
        AssertionError: 应用未注册 /storage 挂载时中断。
    """
    for route in fastapi_app.routes:
        if isinstance(route, Mount) and route.name == "storage":
            return route
    raise AssertionError("应用未注册 /storage 静态挂载（name='storage'）")


def test_guazai_mulu_yu_storage_path_yizhi():
    """挂载目录必须与 settings.storage_path() 一致（C-BUG-2 根因锁定）。

    历史缺陷：挂载用相对 settings.storage_dir，cwd=backend 启动时解析为
    backend/backend/storage（与落盘目录脱节）。本断言保证挂载目录与全部
    落盘调用方（runner/providers/services）使用的 settings.storage_path()
    严格一致，且 URL 前缀 /storage 与 DB url 字段前缀一致。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 挂载前缀非 /storage 或目录解析后不一致时中断。
    """
    mount = _get_storage_mount()
    # URL 前缀：DB url 字段（/storage/...）与挂载 path 严格一致
    assert mount.path == "/storage"
    # 物理目录：挂载目录与 storage_path() 解析为同一绝对路径（与 cwd 无关）
    mounted_dir = Path(mount.app.directory).resolve()
    assert mounted_dir == settings.storage_path().resolve(), (
        f"静态挂载目录 {mounted_dir} 与 settings.storage_path() "
        f"{settings.storage_path().resolve()} 不一致，/storage 产物将 404"
    )


def test_provider_url_qianzhui_yu_guazai_yizhi():
    """全部 provider 的产物 URL 前缀常量与挂载前缀一致（mock + 真实渠道）。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 任一 provider 前缀与 /storage/generated 不符时中断。
    """
    from app.providers import image_agnes, image_mock, tts_bailian, tts_mock, video_agnes, video_mock

    for mod in (image_mock, image_agnes, tts_mock, tts_bailian, video_mock, video_agnes):
        prefix = getattr(mod, "_URL_PREFIX", None)
        assert prefix == "/storage/generated", (
            f"{mod.__name__} 的 _URL_PREFIX={prefix!r} 与挂载前缀 /storage/generated 不一致"
        )


async def test_mock_chanpin_luopan_hou_static_200(client, auth, db):
    """mock 渠道产物端到端：落盘 → 媒体记录入库 → GET /storage/... 返回 200。

    模拟 runner 成功回写链路：调用 mock 图像 provider 落盘到
    storage_path()/generated（与 runner 传入的 out_dir 一致），按 runner
    的写库规则存一条 MediaAsset（url=/storage/generated/...），再经
    TestClient 访问该 url 断言 200。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册普通用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: url 前缀不符、物理文件缺失或 GET 非 200 时中断。
    """
    from app.models import MediaAsset
    from app.providers.image_mock import generate_image

    # 1. provider 落盘（out_dir 与 tasks/runner.py 传参一致）
    out_dir = settings.storage_path() / "generated"
    result = await generate_image("测试提示词：少女在天台远眺", out_dir, seed=1)
    assert result is not None, "mock 图像生成失败"
    url = result["url"]

    # 2. DB url 前缀与挂载一致（相对路径，无 /api 前缀）
    assert url.startswith("/storage/generated/")
    rel = url.removeprefix("/storage/")

    # 3. 媒体记录入库（模拟 runner 成功回写）
    project = await create_project(client, auth["headers"])
    db.add(MediaAsset(
        user_id=auth["user_id"],
        project_id=project["id"],
        media_type="image",
        source="studio",
        url=url,
        meta={"task_id": None, "target_type": "storyboard_keyframe", "target_id": None},
    ))
    await db.commit()

    # 4. 物理文件位于挂载目录内，TestClient 访问 200
    assert (settings.storage_path() / rel).is_file(), f"产物未落盘：{rel}"
    resp = await client.get(url)
    assert resp.status_code == 200, f"产物 {url} 访问失败：{resp.status_code}"
    assert resp.headers["content-type"].startswith("image/svg")
    assert len(resp.content) > 0


async def test_zhenqudao_chanpin_luopan_hou_static_200(client, auth):
    """真实渠道落盘约定端到端：agnes 产物（png）落盘后 GET /storage/... 200。

    真实渠道 generate_image_real 不调外部 API（无法在单测中出图），但落盘
    与 URL 拼装约定可验证：与 provider 相同的 out_dir（storage_path()/
    generated）写入 png、按 _URL_PREFIX 拼 url，经挂载访问必须 200 ——
    保证真实渠道产物路径与 mock 走同一挂载（C-BUG-2 两条路径均通）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册普通用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: url 前缀不符或 GET 非 200 时中断。
    """
    from app.providers.image_agnes import _URL_PREFIX

    # 与 generate_image_real 相同的落盘约定：out_dir 由 runner 传入
    out_dir = settings.storage_path() / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = "keyframe_999_1700000000.png"
    (out_dir / filename).write_bytes(b"\x89PNG\r\n\x1a\nfake")

    url = f"{_URL_PREFIX}/{filename}"
    assert url.startswith("/storage/generated/")
    resp = await client.get(url)
    assert resp.status_code == 200, f"真实渠道产物 {url} 访问失败：{resp.status_code}"
    assert resp.headers["content-type"].startswith("image/png")
