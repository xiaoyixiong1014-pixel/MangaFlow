"""小说解析器纯函数测试模块（文件级说明，P-1 范式级重构 Task 9 / P-7）。

覆盖 novel_parser 的纯函数（无需 DB / 无需 HTTP）：
- split_txt_chapters：中英文章节标题混排切分、无标记启发式切分、空输入；
- parse_epub：以 zipfile 现场构造最小 epub，校验 spine 顺序、剥标签与标题；
- parse_novel：扩展名分派、非法扩展名 ValueError、超长单章切子段；
- summarize_chapters：摘要截取字数与正文字数统计。
"""

import io
import zipfile

import pytest

from app.services.novel_parser import (
    parse_epub,
    parse_novel,
    split_txt_chapters,
    summarize_chapters,
)

# 中英文章节标题混排样本（三种标题格式混合）
_MIXED_SAMPLE = (
    "第一章 开端\n"
    "少年走出村庄，踏上未知的旅途。\n"
    "\n"
    "第2章 转折\n"
    "他遇见了改变一生的师父。\n"
    "\n"
    "Chapter 3 The End\n"
    "故事在此收束，一切归于平静。\n"
)


def _build_minimal_epub() -> bytes:
    """现场构造最小合法 epub（mimetype + container.xml + OPF + 2 篇 XHTML）。

    Returns:
        bytes: epub 文件二进制内容（含 2 个章节）。

    Raises:
        无：纯内存构造。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
            'unique-identifier="bookid">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>样本小说</dc:title></metadata>'
            '<manifest>'
            '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest>'
            '<spine><itemref idref="c1"/><itemref idref="c2"/></spine>'
            '</package>',
        )
        archive.writestr(
            "OEBPS/c1.xhtml",
            '<html><head><title>第一章 启程</title></head>'
            '<body><h1>第一章 启程</h1><p>少年出发了。</p></body></html>',
        )
        archive.writestr(
            "OEBPS/c2.xhtml",
            '<html><head><title>第二章 归来</title></head>'
            '<body><h1>第二章 归来</h1><p>他回到了故乡。</p></body></html>',
        )
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 1. split_txt_chapters：混排标题 / 无标记 / 空输入
# ---------------------------------------------------------------------------
def test_split_txt_chapters_mixed_titles():
    """中英文章节标题混排：切分 3 章，标题与序号正确，正文归属正确。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    chapters = split_txt_chapters(_MIXED_SAMPLE)
    assert [c["title"] for c in chapters] == [
        "第一章 开端", "第2章 转折", "Chapter 3 The End"]
    assert [c["index"] for c in chapters] == [1, 2, 3]
    assert "少年走出村庄" in chapters[0]["content"]
    assert "师父" in chapters[1]["content"]
    assert "故事在此收束" in chapters[2]["content"]


def test_split_txt_chapters_heuristic_without_markers():
    """无章节标记的长文本：走段落启发式切分，结果非空且标题规范。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    text = "\n\n".join(f"第{i}段内容。" * 30 for i in range(12))
    chapters = split_txt_chapters(text)
    assert len(chapters) >= 1
    assert all(c["title"].startswith("第") for c in chapters)
    assert all(c["content"].strip() for c in chapters)


def test_split_txt_chapters_empty_input():
    """空输入 / 纯空白：返回空列表。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    assert split_txt_chapters("") == []
    assert split_txt_chapters("   \n  ") == []


# ---------------------------------------------------------------------------
# 2. parse_epub：最小 epub → 2 章、剥标签、标题取 h1
# ---------------------------------------------------------------------------
def test_parse_epub_minimal():
    """解析最小 epub：按 spine 顺序返回 2 章，正文已剥 HTML 标签。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    chapters = parse_epub(_build_minimal_epub())
    assert len(chapters) == 2
    assert [c["title"] for c in chapters] == ["第一章 启程", "第二章 归来"]
    assert "少年出发了" in chapters[0]["content"]
    assert "他回到了故乡" in chapters[1]["content"]
    # 标签已剥离
    assert "<p>" not in chapters[0]["content"]
    assert "<h1>" not in chapters[0]["content"]


# ---------------------------------------------------------------------------
# 3. parse_novel：分派 / 非法扩展名 / 超长切子段
# ---------------------------------------------------------------------------
def test_parse_novel_txt_dispatch():
    """parse_novel(.txt)：按文本切分章节（与 split_txt_chapters 一致）。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    chapters = parse_novel("novel.txt", _MIXED_SAMPLE.encode("utf-8"))
    assert len(chapters) == 3


def test_parse_novel_invalid_extension():
    """parse_novel 非法扩展名：抛 ValueError（message 提示仅支持 .txt/.epub）。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 未抛 ValueError 或消息不符时中断。
    """
    with pytest.raises(ValueError, match=r"仅支持 \.txt/\.epub"):
        parse_novel("novel.pdf", b"some-bytes")


def test_parse_novel_oversize_chapter_split():
    """超长单章（>8000 字）：按段落边界切分为多个子段并标注「（i/n）」。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    body = "段落内容。\n" * 5000  # 约 15000 字，超 8000 阈值
    text = f"第一章 长章\n{body}"
    chapters = parse_novel("long.txt", text.encode("utf-8"))
    assert len(chapters) >= 2
    assert "（1/" in chapters[0]["title"]
    assert all(len(c["content"]) <= 8000 for c in chapters)


# ---------------------------------------------------------------------------
# 4. summarize_chapters：摘要截取与字数统计
# ---------------------------------------------------------------------------
def test_summarize_chapters():
    """摘要生成：summary 取正文首 200 字，char_count 为正文总字数。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 断言失败时中断。
    """
    summaries = summarize_chapters([
        {"index": 1, "title": "第1章", "content": "甲" * 500},
        {"index": 2, "title": "第2章", "content": "乙" * 50},
    ])
    assert summaries[0]["summary"] == "甲" * 200
    assert summaries[0]["char_count"] == 500
    assert summaries[1]["summary"] == "乙" * 50
    assert summaries[1]["char_count"] == 50
    assert summaries[0]["title"] == "第1章"
