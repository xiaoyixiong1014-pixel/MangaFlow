"""小说改编链路解析器模块（文件级说明，P-1 范式级重构 Task 9 / P-7）。

实现「小说（.txt/.epub）→ 章节切分 → 章节摘要」的核心纯函数，供
`/script/input/novel/*` 接口（章节解析入库）与分集映射（章节 → 分集）复用：

- split_txt_chapters：txt 文本按章节标题正则切分；无标题标记时按空行段落
  启发式切分为若干「章节」；
- parse_epub：用 zipfile + xml.etree + html.parser 纯标准库解析 epub
  （container.xml → OPF manifest + spine 顺序逐篇 XHTML → 剥 HTML 标签）；
- parse_novel：按扩展名分派（.txt/.epub），单章正文超长时按段落边界切分子段；
- summarize_chapters：生成章节摘要（首 N 字）供 episode_planner 使用。

依赖选择（依据评估文档 §3.1 不足 4 / §3.6.9「小说改编合并」）：
epub 本质是 zip 容器 + XML 清单（mimetype / META-INF/container.xml /
OPF manifest+spine），其结构用标准库即可稳定解析，因此**不引入 ebooklib
等第三方重依赖**，避免污染生产镜像与额外的 XML 容错成本。

模块内所有函数均为纯函数（无 DB / 无 IO 副作用），便于单元测试。
"""

import io
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 章节标题行最大字数（超过该长度的行判定为正文，避免误切）
_CHAPTER_TITLE_MAX_LEN = 40

# 单章正文超长阈值（字）：超过按段落边界切分为子段（避免下游提示词超长）
_MAX_CHARS_PER_CHAPTER = 8000

# 无标题标记文本的启发式切分目标：每章约 3000 字
_HEURISTIC_CHARS_PER_CHAPTER = 3000

# 章节标题正则（行首匹配即可，无需整行命中）：
# ① 中文章回体「第X章/节/回/卷/篇/首」（X 支持中文数字与阿拉伯数字）；
# ② 英文「Chapter N」；③ 「1.」「1、」「1．」等序号式标题。
_CHAPTER_TITLE_PATTERNS = (
    re.compile(r"^第\s*[0-9一二三四五六七八九十百千零两]+\s*[章节回卷篇首]"),
    re.compile(r"^Chapter\s*\d+", re.IGNORECASE),
    re.compile(r"^\d+\s*[、.．]\s*\S"),
)


# ---------------------------------------------------------------------------
# 纯文本（txt）章节切分
# ---------------------------------------------------------------------------
def split_txt_chapters(text: str) -> List[dict]:
    """按章节标题正则把纯文本切分为章节列表（无标记时走段落启发式）。

    标题行判定（依据评估文档 §3.1 建议的常见中文章节格式）：行首命中
    `_CHAPTER_TITLE_PATTERNS` 之一且去空白后行长 < 40。任务描述中的
    「前后有空行」作为强特征仅辅助，不强制——纯文本电子书标题行上下
    未必留空行，强制会漏切，故采用宽松策略。命中标题行后，其下至下一个
    标题行之间为该章正文。

    无任何标题行时退化为「空行段落启发式」：按空行切段，按总字数估算
    目标章数（每章约 3000 字），均分后每 N 段一章。

    Args:
        text: 小说纯文本（可为空/None）。

    Returns:
        List[dict]: 章节列表，元素结构固定为
            {"index": 1..n, "title": str, "content": str}；空输入返回 []。

    Raises:
        无：纯字符串处理，异常结构按空文本兜底。
    """
    if not text or not text.strip():
        return []
    lines = text.splitlines()
    # 先定位全部标题行下标（行首命中且长度受控）
    title_indices: List[int] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) >= _CHAPTER_TITLE_MAX_LEN:
            continue
        if _is_chapter_title(stripped):
            title_indices.append(idx)

    if title_indices:
        chapters: List[dict] = []
        for pos, start in enumerate(title_indices):
            end = title_indices[pos + 1] if pos + 1 < len(title_indices) else len(lines)
            title = lines[start].strip()
            content = "\n".join(lines[start + 1:end]).strip()
            chapters.append({"index": pos + 1, "title": title, "content": content})
        return chapters

    # 无标题标记 → 空行段落启发式切分
    return _heuristic_split(text)


def _is_chapter_title(line: str) -> bool:
    """判断一行文本是否为章节标题（内部函数，行首正则命中即视为标题）。

    Args:
        line: 已去首尾空白的单行文本。

    Returns:
        bool: True 表示命中任一章节标题模式。

    Raises:
        无：纯正则匹配。
    """
    return any(pattern.match(line) for pattern in _CHAPTER_TITLE_PATTERNS)


def _heuristic_split(text: str) -> List[dict]:
    """无标题标记时的段落启发式切分（内部函数）。

    按空行切段，按总字数估算目标章数（每章约 3000 字），再均分为
    「每 N 段一章」；无空行段落时整体作为单章。生成的标题统一为「第N章」。

    Args:
        text: 小说纯文本（非空）。

    Returns:
        List[dict]: 启发式切分出的章节列表（{"index","title","content"}）。

    Raises:
        无：纯字符串处理。
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]
    target_count = max(1, -(-len(text) // _HEURISTIC_CHARS_PER_CHAPTER))
    paras_per_chapter = max(1, -(-len(paragraphs) // target_count))
    chapters: List[dict] = []
    for start in range(0, len(paragraphs), paras_per_chapter):
        group = paragraphs[start:start + paras_per_chapter]
        chapters.append({
            "index": len(chapters) + 1,
            "title": f"第{len(chapters) + 1}章",
            "content": "\n\n".join(group),
        })
    return chapters


# ---------------------------------------------------------------------------
# EPUB 解析（纯标准库）
# ---------------------------------------------------------------------------
def parse_epub(file_bytes: bytes) -> List[dict]:
    """解析 epub 字节流为章节列表（zipfile + XML + HTML 剥标签）。

    解析路径：zipfile 打开 → 读 `META-INF/container.xml` 定位 OPF 清单 →
    解析 OPF 的 manifest（id → href）与 spine（idref 顺序）→ 按 spine 顺序
    逐篇读 XHTML → 剥 HTML 标签提取纯文本 → 每篇作为一个章节（title 取
    `<h1>~<h3>`，缺失时回退 `<title>`，再缺失取文件名）。

    Args:
        file_bytes: epub 文件二进制内容。

    Returns:
        List[dict]: 章节列表（{"index","title","content"}，正文已剥标签）。

    Raises:
        ValueError: 非合法 zip / 缺少 OPF 清单 / 未解析到任何有效章节。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            opf_path = _locate_opf(archive)
            manifest, spine_ids = _parse_opf(archive, opf_path)
            base_dir = posixpath.dirname(opf_path)
            chapters: List[dict] = []
            for item_id in spine_ids:
                href = manifest.get(item_id)
                if not href:
                    continue
                member = posixpath.normpath(
                    posixpath.join(base_dir, href)) if base_dir else href
                try:
                    raw = archive.read(member)
                except KeyError:
                    continue  # spine 引用的文件缺失：跳过，保持稳健
                text, doc_title = _html_to_text(raw)
                if not text.strip():
                    continue
                title = doc_title or posixpath.basename(href)
                chapters.append({
                    "index": len(chapters) + 1,
                    "title": title,
                    "content": text.strip(),
                })
            if not chapters:
                raise ValueError("EPUB 未解析到有效章节")
            return chapters
    except zipfile.BadZipFile as exc:
        raise ValueError("EPUB 文件损坏或格式不合法") from exc


def _locate_opf(archive: zipfile.ZipFile) -> str:
    """从 container.xml 定位 OPF 清单路径（内部函数）。

    Args:
        archive: 已打开的 epub zip 对象。

    Returns:
        str: OPF 清单在 zip 内的相对路径（full-path 属性值）。

    Raises:
        ValueError: 缺少 container.xml 或无 rootfile 条目。
    """
    try:
        raw = archive.read("META-INF/container.xml")
    except KeyError as exc:
        raise ValueError("EPUB 缺少 META-INF/container.xml") from exc
    root = ET.fromstring(raw)
    for element in root.iter():
        if _local_name(element.tag) == "rootfile":
            full_path = element.get("full-path")
            if full_path:
                return full_path
    raise ValueError("EPUB container.xml 中未找到 rootfile")


def _parse_opf(archive: zipfile.ZipFile, opf_path: str) -> Tuple[Dict[str, str], List[str]]:
    """解析 OPF 清单，返回 manifest 映射与 spine 顺序（内部函数）。

    Args:
        archive: 已打开的 epub zip 对象。
        opf_path: OPF 清单相对路径。

    Returns:
        Tuple[Dict[str, str], List[str]]: (manifest 的 id→href 映射,
            spine 的 idref 顺序列表)。

    Raises:
        ValueError: OPF 缺失或 XML 解析失败。
    """
    try:
        raw = archive.read(opf_path)
    except KeyError as exc:
        raise ValueError(f"EPUB 缺少 OPF 清单：{opf_path}") from exc
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("EPUB OPF 清单 XML 解析失败") from exc
    manifest: Dict[str, str] = {}
    spine_ids: List[str] = []
    for element in root.iter():
        local = _local_name(element.tag)
        if local == "item":
            item_id, href = element.get("id"), element.get("href")
            if item_id and href:
                manifest[item_id] = href
        elif local == "itemref":
            idref = element.get("idref")
            if idref:
                spine_ids.append(idref)
    return manifest, spine_ids


def _local_name(tag: str) -> str:
    """去掉 XML 命名空间前缀取本地标签名（内部函数）。

    Args:
        tag: ElementTree 元素标签（形如 `{ns}itemref` 或 `itemref`）。

    Returns:
        str: 本地标签名。

    Raises:
        无：无命名空间时原样返回。
    """
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


class _HtmlTextExtractor(HTMLParser):
    """HTML 文本提取器（内部类，剥标签并捕获标题）。

    忽略 script/style 内容；收集全部可见文本（逐段以换行连接）；同时捕获
    首个 `<h1>~<h3>` 标题（优先）与 `<title>` 文本（回退）。

    Attributes:
        parts: 收集到的文本片段列表。
        heading: 首个 h1~h3 标题文本（无则 None）。
        title: `<title>` 文本（无则 None）。
    """

    def __init__(self) -> None:
        """初始化提取器（清空状态）。"""
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._capture: Optional[str] = None
        self.parts: List[str] = []
        self.heading: Optional[str] = None
        self.title: Optional[str] = None

    def handle_starttag(self, tag: str, attrs) -> None:
        """处理开始标签：进入 script/style 跳过区或标记标题捕获（内部方法）。

        Args:
            tag: 标签名。
            attrs: 标签属性列表（未使用）。

        Returns:
            None。

        Raises:
            无。
        """
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag == "title":
            self._capture = "title"
        elif tag in ("h1", "h2", "h3"):
            self._capture = tag

    def handle_endtag(self, tag: str) -> None:
        """处理结束标签：退出跳过区或清除标题捕获（内部方法）。

        Args:
            tag: 标签名。

        Returns:
            None。

        Raises:
            无。
        """
        if tag in ("script", "style"):
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in ("title", "h1", "h2", "h3"):
            self._capture = None

    def handle_data(self, data: str) -> None:
        """处理文本节点：沉淀正文并捕获标题（内部方法）。

        Args:
            data: 文本节点内容。

        Returns:
            None。

        Raises:
            无。
        """
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        self.parts.append(text)
        if self._capture == "title" and self.title is None:
            self.title = text
        elif self._capture in ("h1", "h2", "h3") and self.heading is None:
            self.heading = text


def _html_to_text(raw: bytes) -> Tuple[str, Optional[str]]:
    """把 XHTML 字节流剥标签为纯文本并提取标题（内部函数）。

    Args:
        raw: XHTML 文件二进制内容。

    Returns:
        Tuple[str, Optional[str]]: (纯文本正文, 标题)。标题优先取 h1~h3，
            回退 `<title>`；均无则 None。

    Raises:
        无：解码失败按 utf-8 ignore 兜底。
    """
    extractor = _HtmlTextExtractor()
    try:
        extractor.feed(raw.decode("utf-8", errors="ignore"))
        extractor.close()
    except Exception:  # noqa: BLE001 —— HTML 解析容错，异常时返回已收集内容
        pass
    text = "\n".join(extractor.parts)
    return text, extractor.heading or extractor.title


# ---------------------------------------------------------------------------
# 统一入口与后处理
# ---------------------------------------------------------------------------
def parse_novel(filename: str, data: bytes) -> List[dict]:
    """按扩展名分派解析小说文件（统一入口）。

    支持 .txt（自动探测 utf-8/utf-8-sig/gbk/gb18030 编码）与 .epub；
    其他扩展名抛 ValueError。解析后对单章正文超长（>8000 字）的章节
    按段落边界切分为子段，标题追加「（i/n）」标注，避免下游提示词超长。

    Args:
        filename: 原始文件名（用于判定扩展名与 epub 标题兜底）。
        data: 文件二进制内容。

    Returns:
        List[dict]: 章节列表（{"index","title","content"}）。

    Raises:
        ValueError: 扩展名不受支持、文本解码失败或内容为空。
    """
    name = (filename or "").strip().lower()
    if name.endswith(".txt"):
        text = _decode_text(data)
        chapters = split_txt_chapters(text)
    elif name.endswith(".epub"):
        chapters = parse_epub(data)
    else:
        raise ValueError("仅支持 .txt/.epub 格式")
    if not chapters:
        raise ValueError("未能从文件中解析出章节内容")
    return _split_oversize_chapters(chapters)


def _decode_text(data: bytes) -> str:
    """把文本字节流解码为字符串（内部函数，多编码依次尝试）。

    Args:
        data: 文本文件二进制内容。

    Returns:
        str: 解码后的文本。

    Raises:
        ValueError: 所有候选编码均解码失败。
    """
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("文本文件编码无法识别（支持 utf-8/gbk）")


def _split_oversize_chapters(chapters: List[dict],
                             max_chars: int = _MAX_CHARS_PER_CHAPTER) -> List[dict]:
    """把超长章节按段落边界切分为子段并重排序号（内部函数）。

    单章正文长度 ≤ max_chars 原样保留；超过时按换行段落累积切分（单段超长
    再硬切），子段标题追加「（i/n）」标注，index 全量重排。

    Args:
        chapters: 原始章节列表。
        max_chars: 单章正文上限（字）。

    Returns:
        List[dict]: 切分后的章节列表（index 连续重排）。

    Raises:
        无：纯字符串处理。
    """
    result: List[dict] = []
    for chapter in chapters:
        content = str(chapter.get("content") or "")
        title = str(chapter.get("title") or "").strip()
        if len(content) <= max_chars:
            result.append({"index": len(result) + 1, "title": title, "content": content})
            continue
        segments = _split_by_paragraph(content, max_chars)
        total = len(segments)
        for seq, segment in enumerate(segments, start=1):
            result.append({
                "index": len(result) + 1,
                "title": f"{title}（{seq}/{total}）",
                "content": segment,
            })
    return result


def _split_by_paragraph(content: str, max_chars: int) -> List[str]:
    """按换行段落边界把长文本切分为不超过 max_chars 的片段（内部函数）。

    Args:
        content: 章节正文。
        max_chars: 单段上限（字）。

    Returns:
        List[str]: 切分后的片段列表（至少 1 段）。

    Raises:
        无：纯字符串处理（超长单段按字符硬切）。
    """
    segments: List[str] = []
    buffer: List[str] = []
    buffer_len = 0
    for line in content.split("\n"):
        line_len = len(line)
        if line_len > max_chars:
            # 单段本身超长：先冲刷已有缓冲，再按字符硬切
            if buffer:
                segments.append("\n".join(buffer))
                buffer, buffer_len = [], 0
            for start in range(0, line_len, max_chars):
                segments.append(line[start:start + max_chars])
            continue
        if buffer_len + line_len + 1 > max_chars and buffer:
            segments.append("\n".join(buffer))
            buffer, buffer_len = [], 0
        buffer.append(line)
        buffer_len += line_len + 1
    if buffer:
        segments.append("\n".join(buffer))
    return segments or [content]


def summarize_chapters(chapters: List[dict], max_chars: int = 200) -> List[dict]:
    """生成章节摘要列表（首 N 字）供 episode_planner 使用（纯函数）。

    摘要取章节正文前 max_chars 字（去除首尾空白）；同时保留章节标题与正文
    字数，便于接口层落 config 与前端展示。

    Args:
        chapters: 章节列表（{"index","title","content"}）。
        max_chars: 摘要截取字数上限（默认 200）。

    Returns:
        List[dict]: 摘要列表，元素结构为
            {"index": int, "title": str, "summary": str, "char_count": int}。

    Raises:
        无：结构异常按空值兜底。
    """
    result: List[dict] = []
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        content = str(chapter.get("content") or "").strip()
        result.append({
            "index": int(chapter.get("index") or len(result) + 1),
            "title": str(chapter.get("title") or "").strip(),
            "summary": content[:max_chars],
            "char_count": len(content),
        })
    return result
