"""双语字幕测试模块（文件级说明，P-16）。

覆盖双语字幕「翻译 → ASS 双样式生成 → 失败降级」全链路（ffmpeg 相关验证
以参数/产物文本为主，真实转码不在断言范围）：
- _translate_bilingual_lines：mock LLM 成功返回 JSON 数组 → 等长译文列表；
  mock 失败 / 返回非 JSON / 超时 → None（整体降级单行）；返回数组偏短 →
  缺失位 None（逐条降级）；
- build_ass_subtitles：双语两行（Style Main 原文在上 + Style Sub 译文在下，
  译文灰色小字），译文缺失位仅 Main 单行，全空分镜返回 None；
- _prepare_subtitle_file：bilingual + LLM 失败 → SRT 单行文件仍生成（不抛
  异常）；bilingual + LLM 成功 → ASS 文件生成且含译文；非 bilingual → SRT；
- 合成任务级：subtitle_style.bilingual 透传任务 meta，且测试环境 LLM 失败
  不影响合成任务正常完成（done）。
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.models import Task
from app.services import compose_service
from tests.conftest import create_project, insert_storyboard, unique_suffix, wait_task_terminal


def _sb(duration: float = 5.0, dialogue: str = "林小满：你好", visual: str = "画面A"):
    """构造分镜鸭子类型对象（build_ass_subtitles / _subtitle_texts 用）。

    Args:
        duration: 分镜时长（秒）。
        dialogue: 对白文本（None 视为空）。
        visual: 画面描述文本。

    Returns:
        SimpleNamespace: 含 actual_duration/duration/dialogue/visual_description 的对象。

    Raises:
        无。
    """
    return SimpleNamespace(actual_duration=None, duration=duration, dialogue=dialogue, visual_description=visual)


def test_fanyi_chenggong_dengchang_yiwen(monkeypatch):
    """_translate_bilingual_lines：LLM 返回合法 JSON 数组 → 等长译文列表。

    Args:
        monkeypatch: pytest 夹具（替换 compose_service.chat）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    async def fake_chat(messages, **kwargs):
        """伪 chat：返回与输入等长的英文译文 JSON 数组。

        Args:
            messages: 消息列表（仅提取 user content 用于断言提示词包含原文）。
            **kwargs: 其余参数（temperature/json_mode）。

        Returns:
            str: JSON 数组文本。

        Raises:
            无。
        """
        assert "中文字幕数组" in messages[0]["content"]
        return json.dumps(["Hello", "World"], ensure_ascii=False)

    monkeypatch.setattr(compose_service, "chat", fake_chat)
    result = asyncio.run(compose_service._translate_bilingual_lines(["你好", "世界"]))
    assert result == ["Hello", "World"]


def test_fanyi_shibai_fanhui_none_jiangji(monkeypatch):
    """_translate_bilingual_lines：LLM 抛异常 / 返回非 JSON → 返回 None（整体降级）。

    Args:
        monkeypatch: pytest 夹具（替换 compose_service.chat）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    async def broken_chat(messages, **kwargs):
        """伪 chat：模拟全渠道失败（抛异常）。

        Args:
            messages: 消息列表。
            **kwargs: 其余参数。

        Returns:
            无（直接抛出）。

        Raises:
            RuntimeError: 恒定抛出，模拟 LLM 全渠道失败。
        """
        raise RuntimeError("LLM 全渠道失败")

    monkeypatch.setattr(compose_service, "chat", broken_chat)
    assert asyncio.run(compose_service._translate_bilingual_lines(["你好"])) is None

    async def garbage_chat(messages, **kwargs):
        """伪 chat：返回非 JSON 文本（模拟模型输出不可解析）。

        Args:
            messages: 消息列表。
            **kwargs: 其余参数。

        Returns:
            str: 非 JSON 文本。

        Raises:
            无。
        """
        return "抱歉，我无法翻译"

    monkeypatch.setattr(compose_service, "chat", garbage_chat)
    assert asyncio.run(compose_service._translate_bilingual_lines(["你好"])) is None


def test_fanyi_chaoshi_fanhui_none(monkeypatch):
    """_translate_bilingual_lines：LLM 超过 15s 超时 → 返回 None（不阻断调用方）。

    通过把 _TRANSLATE_TIMEOUT 压缩为 0.05s 模拟超时，避免真实等待。

    Args:
        monkeypatch: pytest 夹具（替换 chat 与超时常量）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """

    async def slow_chat(messages, **kwargs):
        """伪 chat：慢响应（1s，远超压缩后的超时阈值）。

        Args:
            messages: 消息列表。
            **kwargs: 其余参数。

        Returns:
            str: JSON 数组文本（实际不会返回）。

        Raises:
            无。
        """
        await asyncio.sleep(1.0)
        return json.dumps(["Hello"])

    monkeypatch.setattr(compose_service, "chat", slow_chat)
    monkeypatch.setattr(compose_service, "_TRANSLATE_TIMEOUT", 0.05)
    assert asyncio.run(compose_service._translate_bilingual_lines(["你好"])) is None


def test_fanyi_shuzu_pianduan_queshiwei_none(monkeypatch):
    """_translate_bilingual_lines：LLM 返回数组偏短 → 等长填充，缺失位 None（该镜单行）。

    Args:
        monkeypatch: pytest 夹具（替换 compose_service.chat）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """

    async def short_chat(messages, **kwargs):
        """伪 chat：仅返回 1 条译文（输入 2 条）。

        Args:
            messages: 消息列表。
            **kwargs: 其余参数。

        Returns:
            str: 单元素 JSON 数组。

        Raises:
            无。
        """
        return json.dumps(["Hello"])

    monkeypatch.setattr(compose_service, "chat", short_chat)
    result = asyncio.run(compose_service._translate_bilingual_lines(["你好", "世界"]))
    assert result == ["Hello", None]


def test_ass_shuanghang_liangshi_yangshi():
    """build_ass_subtitles：双语两行——Main 原文在上（主字号/主色），
    Sub 译文在下（0.7 倍字号、灰色）；每个条目两条 Dialogue。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    storyboards = [_sb(5.0, "林小满：你好", ""), _sb(3.0, "", "旁白画面")]
    style = {"font_size": 24, "color": "&H00FFFFFF", "position": "bottom"}
    ass = compose_service.build_ass_subtitles(storyboards, ["Hello", "Narration"], style)
    assert ass is not None
    # 双样式：Main（原文）+ Sub（译文，字号 16 = 24*0.7，灰色 &H00B4B4B4）
    assert "Style: Main,Microsoft YaHei,24,&H00FFFFFF" in ass
    assert "Style: Sub,Microsoft YaHei,16,&H00B4B4B4" in ass
    # 原文与译文各占一行（共 4 条 Dialogue），原文 Main、译文 Sub
    assert ass.count("Dialogue: 0,") == 4
    assert ",Main,,0,0,0,,林小满：你好" in ass
    assert ",Main,,0,0,0,,旁白画面" in ass
    assert ",Sub,,0,0,0,,Hello" in ass
    assert ",Sub,,0,0,0,,Narration" in ass
    # 译文行紧跟对应原文行（顺序校验）
    assert ass.index("林小满：你好") < ass.index("Hello")


def test_ass_queyi_danyun_jiangji():
    """build_ass_subtitles：译文缺失位（None）仅生成 Main 单行；translations=None 全单行。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    storyboards = [_sb(5.0, "林小满：你好", ""), _sb(3.0, "", "旁白画面")]
    style = {"font_size": 24, "color": "&H00FFFFFF", "position": "bottom"}
    # 第 2 条译文缺失 → 该条仅 Main 单行
    ass = compose_service.build_ass_subtitles(storyboards, ["Hello", None], style)
    assert ass is not None
    assert ass.count("Dialogue: 0,") == 3
    assert ",Sub,,0,0,0,,Narration" not in ass
    # translations=None：全部单行（降级口径与 SRT 一致）
    ass_single = compose_service.build_ass_subtitles(storyboards, None, style)
    assert ass_single is not None
    assert ass_single.count("Dialogue: 0,") == 2
    assert ",Sub,," not in ass_single


def test_ass_quanbu_weikong_fanhui_none():
    """build_ass_subtitles：全部分镜无文本时返回 None（不产出空字幕文件）。

    Returns:
        None。

    Raises:
        AssertionError: 返回值非 None 时中断。
    """
    storyboards = [_sb(5.0, "", "")]
    assert compose_service.build_ass_subtitles(storyboards, ["Hi"], None) is None


def test_prepare_bilingual_fanyi_shibai_srt_jiangji(tmp_path, monkeypatch):
    """_prepare_subtitle_file：bilingual=true 且 LLM 失败 → SRT 单行文件仍生成。

    测试环境 LLM 会失败：必须保证字幕产物不缺、合成任务不受影响。

    Args:
        tmp_path: pytest 临时目录夹具。
        monkeypatch: pytest 夹具（替换 compose_service.chat 为抛异常）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """

    async def broken_chat(messages, **kwargs):
        """伪 chat：模拟 LLM 失败。

        Args:
            messages: 消息列表。
            **kwargs: 其余参数。

        Returns:
            无（直接抛出）。

        Raises:
            RuntimeError: 恒定抛出。
        """
        raise RuntimeError("LLM 不可用")

    monkeypatch.setattr(compose_service, "chat", broken_chat)
    storyboards = [_sb(5.0, "林小满：你好", "")]
    path = asyncio.run(
        compose_service._prepare_subtitle_file(
            storyboards, {"bilingual": True, "font_size": 24}, 1, tmp_path
        )
    )
    assert path is not None and path.suffix == ".srt"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "林小满：你好" in content


def test_prepare_bilingual_fanyi_chenggong_ass_chanwu(tmp_path, monkeypatch):
    """_prepare_subtitle_file：bilingual=true 且 LLM 成功 → 双语 ASS 文件落盘含译文。

    Args:
        tmp_path: pytest 临时目录夹具。
        monkeypatch: pytest 夹具（替换 compose_service.chat）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """

    async def ok_chat(messages, **kwargs):
        """伪 chat：返回合法译文 JSON 数组。

        Args:
            messages: 消息列表。
            **kwargs: 其余参数。

        Returns:
            str: JSON 数组文本。

        Raises:
            无。
        """
        return json.dumps(["Hello"], ensure_ascii=False)

    monkeypatch.setattr(compose_service, "chat", ok_chat)
    storyboards = [_sb(5.0, "林小满：你好", "")]
    path = asyncio.run(
        compose_service._prepare_subtitle_file(
            storyboards, {"bilingual": True, "font_size": 24}, 2, tmp_path
        )
    )
    assert path is not None and path.suffix == ".ass"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    # meta/产物文本含译文标记：双语两行（原文 Main + 译文 Sub）
    assert ",Main,,0,0,0,,林小满：你好" in content
    assert ",Sub,,0,0,0,,Hello" in content
    assert "Style: Sub,Microsoft YaHei" in content


def test_prepare_feibilingual_srt_zhijie(tmp_path):
    """_prepare_subtitle_file：非 bilingual 直接生成 SRT 单行（不触发翻译调用）。

    Args:
        tmp_path: pytest 临时目录夹具。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    storyboards = [_sb(5.0, "林小满：你好", "")]
    path = asyncio.run(
        compose_service._prepare_subtitle_file(
            storyboards, {"bilingual": False, "font_size": 24}, 3, tmp_path
        )
    )
    assert path is not None and path.suffix == ".srt"
    assert path.exists()
    assert "林小满：你好" in path.read_text(encoding="utf-8")


async def test_hecheng_renwu_bilingual_meta_touchuan_renwu_bushibai(client, auth, db, monkeypatch):
    """合成任务级：subtitle_style.bilingual 透传任务 meta；LLM 失败不影响任务 done。

    测试环境 LLM 渠道全部失败（显式 mock 保证），占位片段走降级合成路径，
    合成任务应正常完成（done）——验证双语翻译失败绝不阻断合成。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        monkeypatch: pytest 夹具（替换 compose_service.chat 为恒失败）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """

    async def broken_chat(messages, **kwargs):
        """伪 chat：模拟测试环境 LLM 全渠道失败。

        Args:
            messages: 消息列表。
            **kwargs: 其余参数。

        Returns:
            无（直接抛出）。

        Raises:
            RuntimeError: 恒定抛出。
        """
        raise RuntimeError("测试环境 LLM 不可用")

    monkeypatch.setattr(compose_service, "chat", broken_chat)

    project = await create_project(client, auth["headers"], title=f"双语项目_{unique_suffix()}")
    for seq in (1, 2):
        await insert_storyboard(
            db, project["id"], sequence=seq, duration=5,
            video_url=f"/storage/generated/fake_bilingual_{seq}.mp4",
        )

    resp = await client.post(
        f"/api/projects/{project['id']}/compose",
        headers=auth["headers"],
        json={
            "resolution": "720P", "mode": "full",
            "subtitles": True,
            "subtitle_style": {"font_size": 24, "position": "bottom", "bilingual": True},
        },
    )
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]

    task = await db.get(Task, task_id)
    assert task.meta["subtitles"] is True
    assert task.meta["subtitle_style"]["bilingual"] is True

    # LLM 失败（占位片段降级路径下字幕块不触发）任务仍应正常完成
    final = await wait_task_terminal(client, auth["headers"], task_id, timeout=30.0)
    assert final["status"] == "done", final


@pytest.mark.parametrize(
    "style,expect_size",
    [
        ({"font_size": 24, "position": "bottom"}, 16),
        (None, 16),
        ({"font_size": 12, "position": "top"}, 10),
    ],
)
def test_ass_yiwen_zihao_guize(style: Any, expect_size: int):
    """build_ass_subtitles：译文 Sub 字号 = 主字号 × 0.7（下限 10，内部四舍五入取整）。

    Args:
        style: 字幕样式 dict（None 用默认样式）。
        expect_size: 期望的译文字号。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    storyboards = [_sb(5.0, "台词", "")]
    ass = compose_service.build_ass_subtitles(storyboards, ["Line"], style)
    assert ass is not None
    assert f"Style: Sub,Microsoft YaHei,{expect_size}," in ass
