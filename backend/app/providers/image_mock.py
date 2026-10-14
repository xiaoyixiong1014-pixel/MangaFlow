"""图像生成 Mock 提供方模块（文件级说明）。

无真实图像生成渠道时的占位实现：生成一张竖版 SVG 占位图（深灰底 +
墨青装饰边框 + 「演示占位」标注 + 提示词文本 + 基于 seed 的伪随机几何
图案），落盘到 storage/generated 目录，返回可被 /storage 静态挂载直接
访问的 URL。

打标约定（D3）：产物文件名统一携带 placeholder_ 前缀，返回结果 meta
携带 placeholder=True 标记（执行器平铺进任务 meta 供 API 透出与计费豁免）。

函数签名与真实图像渠道对齐（prompt + out_dir + seed → url + meta），
日后接入真实渠道仅需替换本模块实现，调用方无需改动。
"""

import random
import time
from pathlib import Path
from typing import Dict, Optional
from xml.sax.saxutils import escape

from app.core.config import settings

# 生成结果 URL 前缀：main.py 已将 storage 目录 mount 到 /storage
_URL_PREFIX = "/storage/generated"

# 占位图配色（深灰底 + 墨青主色）
_BG_COLOR = "#15191A"
_ACCENT_COLOR = "#12B3A6"
_TEXT_COLOR = "#E6F2F1"


def render_placeholder_svg(
    text: str,
    seed: int,
    label: str = "",
    width: int = 720,
    height: int = 1280,
) -> str:
    """渲染占位 SVG 字符串（公共函数，供图像/视频占位复用）。

    Args:
        text: 需要展示的提示词文本（取前 40 字，自动做 XML 转义）。
        seed: 随机种子，决定几何图案（圆/三角）的位置与颜色深浅。
        label: 顶部标注文字（如「演示占位」/「演示占位·视频」，可空）。
        width: 画布宽度（像素）。
        height: 画布高度（像素）。

    Returns:
        str: 完整的 SVG 文档字符串。

    Raises:
        无：纯字符串拼装，不产生异常路径。
    """
    # 伪随机图案生成器：以 seed 派生（int 的 hash 即自身，结果稳定可复现）
    rng = random.Random(seed * 2654435761 % (2 ** 32))
    shapes = []
    # 生成若干圆与三角形，位置/半径/透明度均由 seed 决定
    for _ in range(8):
        cx, cy = rng.randint(80, width - 80), rng.randint(160, height - 320)
        r = rng.randint(18, 90)
        opacity = round(rng.uniform(0.08, 0.30), 2)
        if rng.random() < 0.5:
            shapes.append(
                f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{_ACCENT_COLOR}" opacity="{opacity}"/>'
            )
        else:
            # 等边三角形：以 (cx, cy) 为中心近似绘制
            p1 = f"{cx},{cy - r}"
            p2 = f"{cx - r},{cy + r}"
            p3 = f"{cx + r},{cy + r}"
            shapes.append(
                f'<polygon points="{p1} {p2} {p3}" fill="{_ACCENT_COLOR}" opacity="{opacity}"/>'
            )
    # 文本截断：展示前 40 字，多行拆分避免超宽
    shown = (text or "").strip().replace("\n", " ")[:40]
    lines = [shown[i:i + 14] for i in range(0, len(shown), 14)] or ["（空提示词）"]
    text_els = "".join(
        f'<text x="60" y="{height - 180 + idx * 36}" fill="{_TEXT_COLOR}" '
        f'font-size="26" font-family="sans-serif">{escape(line)}</text>'
        for idx, line in enumerate(lines)
    )
    label_el = (
        f'<text x="60" y="110" fill="{_ACCENT_COLOR}" font-size="30" '
        f'font-family="sans-serif">{escape(label)}</text>' if label else ""
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        f'  <rect width="{width}" height="{height}" fill="{_BG_COLOR}"/>\n'
        f'  <rect x="24" y="24" width="{width - 48}" height="{height - 48}" fill="none" '
        f'stroke="{_ACCENT_COLOR}" stroke-width="3"/>\n'
        f'  <rect x="40" y="40" width="{width - 80}" height="{height - 80}" fill="none" '
        f'stroke="{_ACCENT_COLOR}" stroke-width="1" opacity="0.5"/>\n'
        f'  ' + "".join(shapes) + "\n"
        f'  {label_el}\n'
        f'  {text_els}\n'
        f'</svg>\n'
    )


def _write_svg(out_dir: Path, filename: str, content: str) -> Path:
    """将 SVG 内容写入指定目录（目录不存在则自动创建）。

    Args:
        out_dir: 输出目录（应为 storage/generated 的绝对路径）。
        filename: 目标文件名。
        content: SVG 文本内容。

    Returns:
        Path: 写入后的文件完整路径。

    Raises:
        OSError: 磁盘写入失败（权限/空间不足等）时抛出。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


async def generate_image(
    prompt: str,
    out_dir: Path,
    seed: int,
    reference_image_url: Optional[str] = None,
) -> Optional[Dict]:
    """生成一张 SVG 占位关键帧图（Mock 图像渠道，可插拔）。

    Args:
        prompt: 图像提示词（取前 40 字渲染到图上）。
        out_dir: 输出目录（storage/generated 的绝对路径）。
        seed: 随机种子（一般传任务 ID，保证同名任务结果可复现且文件名唯一）。
        reference_image_url: 可选参考图 URL（A-04：与真实渠道签名对齐；
            mock 不消费参考图内容，仅在 meta 中留痕供溯源断言）。

    Returns:
        Optional[Dict]: 成功返回
            {"url": "/storage/generated/placeholder_keyframe_{seed}_{ts}.svg",
             "meta": {"mock": True, "format": "svg", "placeholder": True,
                      "reference_image_url": 参考图（传入时才有）}}；
            写盘失败等异常场景返回 None（由调用方按平台错误处理）。

    Raises:
        无：内部捕获全部异常并返回 None，不向上抛出。
    """
    try:
        ts = int(time.time())
        # D3 打标：文件名统一带 placeholder_ 前缀，画面显著标注「演示占位」
        filename = f"placeholder_keyframe_{seed}_{ts}.svg"
        content = render_placeholder_svg(prompt, seed, label="演示占位")
        _write_svg(out_dir, filename, content)
        meta: Dict = {"mock": True, "format": "svg", "placeholder": True}
        # A-04：收到参考图时在 meta 留痕（不影响 placeholder 打标与计费豁免语义）
        if reference_image_url:
            meta["reference_image_url"] = reference_image_url
        return {
            "url": f"{_URL_PREFIX}/{filename}",
            "meta": meta,
        }
    except Exception:
        return None
