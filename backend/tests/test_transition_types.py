"""转场类型映射测试模块（文件级说明，P-17）。

覆盖 xfade 转场类型白名单映射与 ffmpeg 参数构造：
- map_transition_type：白名单四类直通（fade/dissolve/fadeblack/fadewhite）、
  大小写与空白归一化、非法/空值回退 fade；
- _xfade_clips：合成命令 filter_complex 含映射后的 transition=<type>（多级
  xfade 链逐段应用），非法 type 落在命令中为 fade——以 monkeypatch 假命令
  执行器捕获参数，不依赖本机 ffmpeg；
- 合成任务级：transition.type=dissolve 透传任务 meta（沿用现有参数校验风格）。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Dict

import pytest

from app.models import Task
from app.services import compose_service
from tests.conftest import create_project, insert_storyboard, unique_suffix


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("fade", "fade"),
        ("dissolve", "dissolve"),
        ("fadeblack", "fadeblack"),
        ("fadewhite", "fadewhite"),
        # 大小写与前后空白归一化
        ("DISSOLVE", "dissolve"),
        ("  FadeBlack  ", "fadeblack"),
    ],
)
def test_map_transition_baimingdan_zhitong(raw: str, expected: str):
    """map_transition_type：白名单四类转场直通（大小写/空白归一化）。

    Args:
        raw: 请求中的转场类型原值。
        expected: 期望映射到的 xfade 原生滤镜名。

    Returns:
        None。

    Raises:
        AssertionError: 映射结果与期望不符时中断。
    """
    assert compose_service.map_transition_type(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "bogus", "wipeleft", "slideleft", "smoothleft"],
)
def test_map_transition_feifa_huitui_fade(raw):
    """map_transition_type：非法/未收录/空值一律回退 fade，保证 xfade 参数恒合法。

    Args:
        raw: 请求中的转场类型原值（可空）。

    Returns:
        None。

    Raises:
        AssertionError: 映射结果不为 fade 时中断。
    """
    assert compose_service.map_transition_type(raw) == "fade"


class _CaptureRunner:
    """假命令执行器（捕获 ffmpeg 命令参数并模拟成功产物，内部类）。

    Attributes:
        captured: 最近一次捕获的完整命令列表。
    """

    def __init__(self) -> None:
        """初始化捕获容器。

        Raises:
            无。
        """
        self.captured: Dict[str, list] = {}

    async def __call__(self, cmd: list, timeout: int = 30, capture: bool = False):
        """替换 shutil_get_run：记录命令并写出产物文件模拟 ffmpeg 成功。

        Args:
            cmd: ffmpeg 完整命令列表（末位为输出路径）。
            timeout: 子进程超时（原样接收）。
            capture: 是否捕获输出（原样接收）。

        Returns:
            SimpleNamespace: returncode=0 的完成对象（stdout/stderr 为空字节串）。

        Raises:
            无。
        """
        self.captured["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"0")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


async def test_xfade_mingling_han_yingshe_hou_transition_type(tmp_path, monkeypatch):
    """_xfade_clips：type=dissolve 时 ffmpeg 命令 filter_complex 含 transition=dissolve。

    以假命令执行器捕获参数验证，不依赖本机 ffmpeg 可用性。

    Args:
        tmp_path: pytest 临时目录夹具。
        monkeypatch: pytest 夹具（替换 compose_service.shutil_get_run）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    runner = _CaptureRunner()
    monkeypatch.setattr(compose_service, "shutil_get_run", runner)
    out = tmp_path / "xfade_out.mp4"
    ok = await compose_service._xfade_clips(
        "ffmpeg",
        [tmp_path / "c1.mp4", tmp_path / "c2.mp4"],
        [5.0, 5.0],
        out,
        {"type": "dissolve", "duration": 0.5},
    )
    assert ok is True
    joined = " ".join(runner.captured["cmd"])
    assert "transition=dissolve" in joined
    assert "transition=fade:" not in joined


async def test_xfade_feifa_type_huitui_fade_canliang(tmp_path, monkeypatch):
    """_xfade_clips：type 非法（bogus）时命令中落为 transition=fade（回退口径）。

    Args:
        tmp_path: pytest 临时目录夹具。
        monkeypatch: pytest 夹具（替换 compose_service.shutil_get_run）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    runner = _CaptureRunner()
    monkeypatch.setattr(compose_service, "shutil_get_run", runner)
    out = tmp_path / "xfade_out2.mp4"
    ok = await compose_service._xfade_clips(
        "ffmpeg",
        [tmp_path / "c1.mp4", tmp_path / "c2.mp4"],
        [5.0, 5.0],
        out,
        {"type": "bogus", "duration": 0.5},
    )
    assert ok is True
    joined = " ".join(runner.captured["cmd"])
    assert "transition=fade:" in joined
    assert "transition=bogus" not in joined


async def test_xfade_duoji_lian_meiduan_yingyong_tongming_zhuanchang(tmp_path, monkeypatch):
    """_xfade_clips：三片段多级 xfade 链——每段均携带映射后的同名转场（闪黑）。

    Args:
        tmp_path: pytest 临时目录夹具。
        monkeypatch: pytest 夹具（替换 compose_service.shutil_get_run）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    runner = _CaptureRunner()
    monkeypatch.setattr(compose_service, "shutil_get_run", runner)
    out = tmp_path / "xfade_out3.mp4"
    ok = await compose_service._xfade_clips(
        "ffmpeg",
        [tmp_path / "c1.mp4", tmp_path / "c2.mp4", tmp_path / "c3.mp4"],
        [4.0, 5.0, 6.0],
        out,
        {"type": "fadeblack", "duration": 0.8},
    )
    assert ok is True
    joined = " ".join(runner.captured["cmd"])
    # 两段 xfade 逐级拼接，每段均为 fadeblack
    assert joined.count("xfade=transition=fadeblack") == 2


async def test_xfade_sheng_lue_type_moren_fade(tmp_path, monkeypatch):
    """_xfade_clips：transition 缺省 type → 命令含 transition=fade（默认口径）。

    Args:
        tmp_path: pytest 临时目录夹具。
        monkeypatch: pytest 夹具（替换 compose_service.shutil_get_run）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    runner = _CaptureRunner()
    monkeypatch.setattr(compose_service, "shutil_get_run", runner)
    out = tmp_path / "xfade_out4.mp4"
    ok = await compose_service._xfade_clips(
        "ffmpeg",
        [tmp_path / "c1.mp4", tmp_path / "c2.mp4"],
        [5.0, 5.0],
        out,
        {"duration": 0.5},
    )
    assert ok is True
    assert "transition=fade:" in " ".join(runner.captured["cmd"])


async def test_hecheng_renwu_transition_type_touchuan(client, auth, db):
    """合成任务级：transition.type=dissolve 透传任务 meta（闸门校验沿用现有风格）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(client, auth["headers"], title=f"转场项目_{unique_suffix()}")
    await insert_storyboard(
        db, project["id"], sequence=1, duration=5,
        video_url="/storage/generated/fake_transition_1.mp4",
    )
    resp = await client.post(
        f"/api/projects/{project['id']}/compose",
        headers=auth["headers"],
        json={
            "resolution": "720P", "mode": "full",
            "transition": {"type": "dissolve", "duration": 0.6},
        },
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]

    task = await db.get(Task, task_id)
    assert task.meta["transition"] == {"type": "dissolve", "duration": 0.6}
