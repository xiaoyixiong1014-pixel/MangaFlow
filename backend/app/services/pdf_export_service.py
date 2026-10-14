"""分镜表 PDF 导出服务模块（文件级说明，SB-08）。

基于 fpdf2 生成 A4 横向表格式分镜表 PDF：
- 中文支持：探测系统中文字体（候选列表与 compose_service 的 ffmpeg drawtext
  字体探测同源，另补充 Linux 常见 Noto CJK 路径）并注册 TTF/OTF；
- 降级策略（绝不因字体缺失 500）：字体探测失败、或中文版渲染过程抛出任何
  异常（如 fpdf2 遇非拉丁字符缺字体）时，自动改用内置 helvetica 重试英文
  兜底版（全部文本 latin-1 净化，非拉丁字符替换为 ?），并由路由层在响应头
  标记 X-PDF-Font-Fallback: true；
- 表格列：镜号 / 景别 / 运镜 / 画面描述 / 台词 / 时长（秒）；
  表头行翻页自动重复，数据行自动分页。
"""

import io
from pathlib import Path
from typing import Any, List, Optional, Tuple

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

# 中文字体候选（按顺序探测，首个存在且可被 fpdf2 注册者生效）：
# Windows 微软雅黑 / 黑体在前，Linux 常见 Noto CJK 路径在后。
# 注意 .ttc（字体集合）若 fpdf2 无法加载会抛异常，由调用方逐候选降级到下一项。
_CJK_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
)

# 中文表头（与列数据一一对应）
_HEADERS_CN = ("镜号", "景别", "运镜", "画面描述", "台词", "时长（秒）")
# 英文表头（字体缺失降级兜底版）
_HEADERS_EN = ("No.", "Shot", "Camera", "Description", "Dialogue", "Duration(s)")
# 各列宽度（mm）：A4 横向可用宽约 277mm（297 - 左右边距 10×2）
_COL_WIDTHS = (14, 20, 18, 110, 95, 20)
# 表格行高与正文字号
_LINE_HEIGHT = 5.5
_BODY_FONT_SIZE = 9
# 标题字号
_TITLE_FONT_SIZE = 14


def detect_cjk_font() -> Optional[str]:
    """探测本机可用的中文字体文件（模块级函数，便于测试 monkeypatch）。

    逐个候选检查文件存在性；候选文件顺序与 compose_service 的水印字体
    探测同源（msyh / simhei 优先），并补充 Linux Noto CJK 常见安装路径。

    Returns:
        Optional[str]: 首个存在的中文字体文件路径；全部缺失时返回 None
        （调用方据此走英文兜底版渲染）。

    Raises:
        无：单候选探测异常（如权限问题）时跳过继续，不向上抛出。
    """
    for candidate in _CJK_FONT_CANDIDATES:
        try:
            if Path(candidate).is_file():
                return candidate
        except OSError:
            # 个别路径不可访问（权限/挂载异常）时跳过，继续下一候选
            continue
    return None


def _register_cjk_font(pdf: FPDF) -> bool:
    """按候选顺序逐个尝试把中文字体注册进 PDF 文档（内部函数）。

    与 detect_cjk_font 的差异：本函数做「真实注册」验证（首个注册成功的
    候选生效），fpdf2 无法加载的 .ttc 集合等异常场景自动落到下一候选
    （如 msyh.ttc 失败 → simhei.ttf）；全部失败返回 False 走英文兜底版。
    同一字体文件同时注册 regular 与 bold 两种样式（表头加粗复用同一 TTF）。

    Args:
        pdf: 目标 PDF 文档对象。

    Returns:
        bool: 是否有候选字体注册成功。

    Raises:
        无：单候选注册异常时捕获并继续下一候选，不向上抛出。
    """
    for candidate in _CJK_FONT_CANDIDATES:
        try:
            if not Path(candidate).is_file():
                continue
            pdf.add_font(family="cjk", style="", fname=candidate)
            pdf.add_font(family="cjk", style="B", fname=candidate)
            return True
        except Exception:  # noqa: BLE001 —— ttc 集合不支持等异常逐候选降级
            continue
    return False


def _new_pdf() -> FPDF:
    """构造 A4 横向 PDF 文档对象并统一页边距与自动分页设置（内部函数）。

    Returns:
        FPDF: 已配置方向 / 单位 / 尺寸 / 自动分页的空白文档。

    Raises:
        无：纯对象构造，不产生异常路径。
    """
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(10, 12, 10)
    return pdf


def _render_title(pdf: FPDF, title: str, subtitle: str) -> None:
    """渲染表格上方标题与副标题行（内部函数）。

    Args:
        pdf: 目标文档（须已完成 add_page 并设置正文字体）。
        title: 标题文本。
        subtitle: 副标题文本（镜数 / 总时长等摘要）。

    Raises:
        无：纯绘制操作，不产生异常路径。
    """
    pdf.set_font_size(_TITLE_FONT_SIZE)
    pdf.cell(0, 10, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font_size(_BODY_FONT_SIZE)
    pdf.cell(0, 6, subtitle, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)


def _render_table(pdf: FPDF, headers: Tuple[str, ...], rows: List[List[Any]]) -> None:
    """渲染表头与数据行表格（内部函数）。

    使用 fpdf2 Table：首行作为表头（加粗、灰底），翻页时表头自动重复，
    数据行超出页面高度时自动分页。

    Args:
        pdf: 目标文档。
        headers: 表头文本元组（6 列）。
        rows: 数据行列表（每行 6 个单元格值，None 以空串呈现）。

    Raises:
        Exception: 字体不支持单元格文本字符集时由 fpdf2 抛出
           （调用方负责捕获并降级英文兜底版重试）。
    """
    pdf.set_font_size(_BODY_FONT_SIZE)
    with pdf.table(
        col_widths=_COL_WIDTHS,
        text_align=("CENTER", "CENTER", "CENTER", "LEFT", "LEFT", "CENTER"),
        line_height=_LINE_HEIGHT,
        repeat_headings=1,
        headings_style=FontFace(emphasis="BOLD", fill_color=(232, 232, 232)),
    ) as table:
        head = table.row()
        for h in headers:
            head.cell(h)
        for row_values in rows:
            row = table.row()
            for value in row_values:
                row.cell("" if value is None else str(value))


def _sanitize_latin(value: Any) -> str:
    """把单元格值净化为 latin-1 可编码文本（英文兜底版专用，内部函数）。

    内置 helvetica 字体仅支持 latin-1；中文等非拉丁字符统一替换为 ?，
    保证降级版渲染不抛字符集异常。

    Args:
        value: 单元格原始值（None / int / str 等）。

    Returns:
        str: latin-1 安全文本；None 映射为空串。

    Raises:
        无：编码替换策略保证任何输入均可编码，不产生异常路径。
    """
    if value is None:
        return ""
    return str(value).encode("latin-1", errors="replace").decode("latin-1")


def build_storyboard_pdf(items: List[dict]) -> Tuple[bytes, bool]:
    """生成分镜表 PDF 字节流（表格式，A4 横向）。

    渲染策略：
    1. 字体探测成功时注册中文字体并渲染中文版（标题/表头/数据均为中文）；
    2. 中文版渲染途中抛出任何异常（含 fpdf2 字体缺字异常）时整体废弃，
       用内置 helvetica 重试英文兜底版（非拉丁字符替换 ?）；
    3. 字体探测失败时直接渲染英文兜底版。

    Args:
        items: list_storyboards 返回的分镜输出 dict 列表（按 sequence 升序），
            取 sequence/shot_type/camera_movement/visual_description/dialogue/
            duration 六个字段组表。

    Returns:
        Tuple[bytes, bool]: (PDF 字节流, 是否走了英文兜底降级)。

    Raises:
        无：内部捕获全部渲染异常，保证任何环境下均可产出 PDF（不 500）。
    """
    rows: List[List[Any]] = [
        [
            it.get("sequence"),
            it.get("shot_type") or "",
            it.get("camera_movement") or "",
            it.get("visual_description") or "",
            it.get("dialogue") or "",
            it.get("duration"),
        ]
        for it in items
    ]

    # 中文版尝试：探测入口（可被测试 monkeypatch 强制失败）命中任一候选文件时，
    # 再逐候选真实注册；注册与渲染任一环节失败均整体废弃、落英文兜底版
    if detect_cjk_font() is not None:
        cn_pdf = _new_pdf()
        if _register_cjk_font(cn_pdf):
            try:
                cn_pdf.add_page()
                cn_pdf.set_font("cjk", size=_BODY_FONT_SIZE)
                total_duration = sum(int(it.get("duration") or 0) for it in items)
                _render_title(
                    cn_pdf,
                    "分镜表",
                    f"共 {len(items)} 镜 / 总时长 {total_duration} 秒",
                )
                _render_table(cn_pdf, _HEADERS_CN, rows)
                return bytes(cn_pdf.output()), False
            except Exception:  # noqa: BLE001 —— 缺字形/渲染异常必须降级兜底
                pass

    # 英文兜底版：内置 helvetica（latin-1），全部文本净化后重渲染
    pdf = _new_pdf()
    pdf.add_page()
    pdf.set_font("helvetica", size=_BODY_FONT_SIZE)
    total_duration = sum(int(it.get("duration") or 0) for it in items)
    _render_title(
        pdf,
        "Storyboard Sheet",
        f"{len(items)} shots / total {total_duration} s "
        "(note: CJK font unavailable, non-latin chars shown as '?')",
    )
    rows_en = [[_sanitize_latin(v) for v in row] for row in rows]
    _render_table(pdf, _HEADERS_EN, rows_en)
    return bytes(pdf.output()), True
