"""剧本域业务服务模块（文件级说明）。

承载剧本工作流的业务逻辑：
- 项目归属校验（统一 404，不泄露他人项目存在性）
- 剧本行获取 / 输入保存
- LLM 流式生成提示词构造、剧本 JSON 解析容错、离线降级占位剧本
- RD-03 字段级来源标记包装（顶层每字段 {"value":..., "source":...}，且 scenes
  元素级追加 source 键——P-1 Task 8 场次级 source 双层包装）
- 剧本 content 解包工具（content_plain / content_has_payload，供本模块与路由层共用）
- 场次级 source 合并（merge_scene_sources，P-1 Task 5 正文保存：内容未变的
  场次保留旧 source，改动/新增标 user，首次保存全 ai）
- AI 优化 diff 计算与 path 安全工具（P-6 Task 7：split_content_path /
  get_value_by_path / set_value_by_path / build_field_diffs /
  apply_optimize_changes——仅产出片段化 changes、禁止整篇覆盖）
- 抽取源文本构造（build_extract_source_text：优先取生成后的完整剧本 content，
  无结构化 payload 时兜底 raw_text，P0 断链修复）
- 规则体检（RD-02）
- 实体抽取（规则 + LLM 双路）、别名冲突检测（RD-05 / RD-06）
- 抽取确认入池（建 characters / scenes / props 资产行）
- 剧本生成进程内互斥锁（单进程语义，防同项目并发生成）

路由层（app/api/script.py）只做参数校验与编排，事务细节统一收敛在本模块。
"""

import copy
import difflib
import hashlib
import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm_adapter import llm
from app.models import Character, Project, Prop, Scene, Script, Style, VersionSnapshot
from app.schemas.script import validate_script_content

# visual_prompt 缺省中文模板（抽取确认 / 手工建角色共用）
DEFAULT_VISUAL_TEMPLATE = "统一风格插画，{name}，{description}，全身立绘，纯色背景，高细节"


def build_character_bible_prompt(
    name: str, age: Optional[str], appearance: Optional[str]
) -> str:
    """构建「角色圣经」固定前缀：纯视觉锁定描述，供生图提示词复用。

    参照 AI 漫剧行业惯例（每条人物提示词以角色圣经固定前缀开头，只改动作/
    场景），前缀只收**视觉要素**（姓名 + 年龄感 + 外貌设定），性格/动机/弧光
    等非视觉信息不进入提示词（避免画面干扰）；appearance 缺失时回退姓名。

    Args:
        name: 角色名。
        age: 年龄设定（弹性表述，如「约30岁」；可空）。
        appearance: 外貌设定（发型/五官/服装等视觉描述；可空）。

    Returns:
        str: 圣经前缀文本（如「林越，约30岁，黑发束起，黑色风衣，眼神冷峻」）。

    Raises:
        无：纯字符串拼装，字段缺失按空值跳过。
    """
    parts = [str(name or "").strip()]
    for extra in (age, appearance):
        text = str(extra or "").strip()
        if text:
            parts.append(text)
    return "，".join(p for p in parts if p)


def extract_appearance_from_description(description: Optional[str]) -> Optional[str]:
    """从 characters.description 人设摘要中提取视觉三段（外貌锁定用）。

    characters.description 由 build_profile_description 以「性格：…；外观：…；
    服装配饰：…；风格画质：…；动机：…；弧光：…」格式拼接，本函数按全角分号
    分段，拼回「外观 + 服装配饰 + 风格画质」三个视觉段（旧数据无后两段时
    自动降级为仅外观），供分镜锁定层/角色圣经拿到完整视觉锁定描述。

    Args:
        description: 角色人设摘要（可空；抽取/手工建角色的旧数据可能无该段）。

    Returns:
        Optional[str]: 视觉三段拼接文本（以「；」连接；无任何视觉段返回 None）。

    Raises:
        无：纯字符串处理。
    """
    if not description:
        return None
    visual_keys = ("外观：", "服装配饰：", "风格画质：")
    parts: List[str] = []
    for seg in str(description).split("；"):
        seg = seg.strip()
        for key in visual_keys:
            if seg.startswith(key):
                text = seg[len(key):].strip()
                if text:
                    parts.append(text)
                break
    return "；".join(parts) or None

# 低置信度阈值：抽取条目 confidence 低于该值时标记 low_confidence（前端标黄）
LOW_CONFIDENCE_THRESHOLD = 0.7

# 默认目标时长（秒，P-1 Task 8 时长口径统一）：项目未显式设置 target_duration
# 时的缺省口径，与竖屏漫剧短剧体量一致（原硬编码 60 秒与「3~6 场 × 30s」不自洽，
# 统一提升为 180 秒；不改 PRD 允许范围 1~3600s，仅改缺省取值）。
DEFAULT_TARGET_DURATION = 180

# 规则路：对白行"角色名：台词"的角色名提取（行首 1~10 位中文/字母/数字，后接冒号）
_DIALOGUE_NAME_RE = re.compile(r"^\s*([\u4e00-\u9fa5A-Za-z0-9·]{1,10})\s*[：:]\s*\S+", re.M)

# 规则路：场景抽取标记行（P0-2：「内景/外景XXX」「场景：XXX」「第X场 XXX」三种格式）
_SCENE_MARK_RE = re.compile(
    r"(?:^|\n)\s*(?:(?:内景|外景)[\s\-—·]*([\u4e00-\u9fa5A-Za-z0-9]{2,12}?)"
    r"(?=\s|$|[，。：:,，])"
    r"|(?:场景|地点|第[一二三四五六七八九十\d]+场)\s*[：:]?\s*"
    r"([\u4e00-\u9fa5A-Za-z0-9]{2,12})(?=\s|$|[，。：:,，]))"
)

# 规则路：场景后缀词表兜底（全文统计命中，弥补无标记行格式的剧本）
_SCENE_SUFFIXES = (
    "咖啡馆", "咖啡店", "教室", "办公室", "会议室", "医院", "诊所", "学校", "操场",
    "公园", "酒吧", "餐厅", "食堂", "厨房", "卧室", "客厅", "书房", "浴室", "房间",
    "走廊", "楼道", "天台", "阳台", "地下室", "车库", "电梯", "地铁站", "车站",
    "码头", "机场", "街上", "街头", "小巷", "广场", "商场", "超市", "便利店",
    "图书馆", "体育馆", "实验室", "宿舍", "旅馆", "酒店", "森林", "山顶", "海边",
    "河边", "湖边", "桥上", "陵园", "寺庙", "祠堂", "家门口", "院子里", "屋顶",
)

# LLM 实体抽取键名别名归并表（P0-2：LLM 键名漂移容错，如 locations→scenes）
_ENTITY_GROUP_ALIASES: Dict[str, Tuple[str, ...]] = {
    "characters": ("characters", "roles", "persons", "人物", "角色"),
    "scenes": ("scenes", "locations", "places", "settings", "场景", "地点"),
    "props": ("props", "items", "objects", "道具", "物品"),
}

# 本模块日志器（P0-2：抽取降级/全空不再静默）
logger = logging.getLogger("mangaflow.script_service")

# 规则路 2-3 字中文高频词候选的停用词（虚词/常见代词等，避免噪声实体）
_STOPWORDS = frozenset({
    "我们", "你们", "他们", "她们", "什么", "这个", "那个", "自己", "没有", "就是",
    "还是", "已经", "所以", "因为", "但是", "如果", "现在", "时候", "知道", "一个",
    "一下", "起来", "过来", "出来", "怎么", "这样", "那样", "可以", "不是", "说道",
    "的话", "你的", "我的", "他的", "她的", "他们", "这里", "那里", "出现", "最后",
})

# LLM 剧本生成系统提示词（要求仅输出严格 JSON）。
# 说明（P-1 Task 8 时长口径统一）：本字符串为 str.format 模板，其中 {target}
# 为目标时长占位符，JSON 示例里的花括号已按 format 规则转义为 {{ }}；场次时长
# 要求不再硬编码「示例 30 秒」，改为按项目目标时长自洽分配（合计≈目标时长±10%）。
_GENERATE_SYSTEM_PROMPT = (
    "你是专业的漫剧（动态漫画短剧）编剧。请根据用户输入构思剧本。\n"
    "必须仅输出一个严格合法的 JSON 对象（禁止 markdown 代码块围栏、禁止任何解释文字），"
    "结构如下：\n"
    '{{"title":"剧本标题","logline":"一句话故事梗概",'
    '"outline":[{{"act":"第一幕","summary":"本幕概要"}}],'
    '"scenes":[{{"scene_no":1,"title":"场景标题","plot":"场景剧情描述",'
    '"dialogue":[{{"character":"角色名","line":"台词"}}],"duration_sec":60}}]}}\n'
    "要求：三幕结构（outline 恰好 3 项）；scenes 为 3~6 个场景；"
    "各场景 duration_sec 按目标时长 {target} 秒合理分配（合计约等于目标时长 ±10%）；"
    "每个场景含 1~4 条对白；全部使用中文。"
)

# ---------------------------------------------------------------------------
# 剧本生成进程内互斥锁（单进程语义：本项目无 Redis，多进程部署需另行分布式锁）
# ---------------------------------------------------------------------------

# 正在执行 LLM 生成的项目 ID 集合（进程内共享，防同项目并发重复生成）
_generating_projects: set = set()


def acquire_generate_lock(project_id: int) -> bool:
    """尝试获取指定项目的生成锁（非阻塞，原子判断并占用）。

    Args:
        project_id: 项目 ID。

    Returns:
        bool: True —— 获取成功（已占用）；False —— 该项目正在生成中。

    Raises:
        无：纯集合操作。
    """
    if project_id in _generating_projects:
        return False
    _generating_projects.add(project_id)
    return True


def release_generate_lock(project_id: int) -> None:
    """释放指定项目的生成锁（幂等：未持锁时调用无副作用）。

    Args:
        project_id: 项目 ID。

    Returns:
        None。

    Raises:
        无：纯集合操作。
    """
    _generating_projects.discard(project_id)


# LLM 实体抽取系统提示词（要求仅输出严格 JSON）
_EXTRACT_SYSTEM_PROMPT = (
    "你是动漫剧项目的实体抽取助手。请从用户给出的剧本文本中抽取实体：\n"
    "1. characters：角色（含别名 aliases 数组、外貌与性格 description、置信度 confidence 0~1）\n"
    "2. scenes：场景地点（含 description、confidence）\n"
    "3. props：关键道具（含 description、confidence）\n"
    "仅输出严格 JSON，禁止任何其他文字，格式：\n"
    '{"characters":[{"name":"...","aliases":["..."],"description":"...","confidence":0.9}],'
    '"scenes":[{"name":"...","description":"...","confidence":0.9}],'
    '"props":[{"name":"...","description":"...","confidence":0.9}]}'
)


async def get_owned_project(db: AsyncSession, project_id: int, user_id: int) -> Project:
    """校验并返回归属于当前用户的未删除项目。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。
        user_id: 当前用户 ID。

    Returns:
        Project: 项目 ORM 对象。

    Raises:
        HTTPException: 404 —— 项目不存在、已软删除或不属于当前用户
            （统一 404，避免泄露他人项目存在性）。
    """
    project = (
        await db.execute(
            select(Project).where(
                Project.id == project_id,
                Project.user_id == user_id,
                Project.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return project


async def get_script_or_404(db: AsyncSession, project: Project) -> Script:
    """获取项目剧本行（无 episode 参数接口的默认定位），不存在时抛 404。

    用法定位「默认剧本行」：范式流水线（P-1）下同一项目可能同时存在未绑定
    剧集的默认剧本行（episode_id IS NULL，idea/novel 快速模式或手动保存）
    与多集级剧本行（episode_id 非空，逐集正文）——按 (project_id, episode_id)
    建行。无 episode 参数的接口（如 /script/extract）统一取默认剧本行；
    项目仅有集级行（无默认行）时按 id 升序取首行兜底；单行项目行为不变。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。

    Returns:
        Script: 剧本 ORM 对象。

    Raises:
        HTTPException: 404 —— 项目尚未创建任何剧本行。
    """
    rows = (
        await db.execute(
            select(Script).where(Script.project_id == project.id).order_by(Script.id)
        )
    ).scalars().all()
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧本不存在，请先保存剧本输入")
    # 优先默认剧本行（episode_id IS NULL）；无默认行时取 id 最小的首行
    for row in rows:
        if row.episode_id is None:
            return row
    return rows[0]


async def validate_episode(
    db: AsyncSession, project: Project, episode_id: int
) -> None:
    """校验剧集存在且属于指定项目（S-06 剧集链路适配共用）。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        episode_id: 剧集 ID。

    Returns:
        None

    Raises:
        HTTPException: 404 —— 剧集不存在或不属于该项目（统一 404，不泄露存在性）。
    """
    from app.models import Episode

    episode = await db.get(Episode, episode_id)
    if episode is None or episode.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧集不存在")


async def get_script_scoped_or_404(
    db: AsyncSession, project: Project, episode_id: Optional[int]
) -> Script:
    """按剧集范围获取剧本行，不存在时抛 404（S-06）。

    范围语义（与存量数据完全兼容——存量剧本行 episode_id 均为 NULL）：
    - episode_id 为 None：定位「未绑定剧集」的默认剧本行（episode_id IS NULL），
      即既有单剧本行项目的唯一行，行为与 get_script_or_404 一致；
    - episode_id 传值：定位该剧集绑定的剧本行（uk_scripts_project 保证
      (project_id, episode_id) 唯一）。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        episode_id: 剧集 ID（None 表示默认/未绑定剧集剧本行）。

    Returns:
        Script: 剧本 ORM 对象。

    Raises:
        HTTPException: 404 —— 对应范围的剧本行不存在。
    """
    conds = [Script.project_id == project.id]
    if episode_id is None:
        conds.append(Script.episode_id.is_(None))
    else:
        conds.append(Script.episode_id == episode_id)
    script = (
        await db.execute(select(Script).where(*conds))
    ).scalar_one_or_none()
    if script is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧本不存在，请先保存剧本输入")
    return script


def strip_json_fences(text: str) -> str:
    """剥离 LLM 输出中的 markdown 围栏并截取首尾大括号之间的内容。

    Args:
        text: LLM 原始输出文本。

    Returns:
        str: 清洗后的候选 JSON 字符串（不保证可解析）。
    """
    t = (text or "").strip()
    # 剥 ```json ... ``` 围栏
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    # 截取第一个 { 到最后一个 } 之间，容错模型输出的前后杂文本
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        t = t[start:end + 1]
    return t


def parse_script_json(text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """解析并校验 LLM 输出的剧本 JSON（容错解析 + Pydantic 结构校验，PRD S-03）。

    双重把关：先剥围栏做 JSON 解析，再经 ScriptContent 结构校验；
    任一环节失败均返回错误摘要——彻底取代旧版「{"raw": 全文} 静默兜底落库」。

    Args:
        text: LLM 输出的完整文本。

    Returns:
        Tuple[Optional[Dict[str, Any]], Optional[str]]:
            (规范化剧本 dict, None) —— 解析与结构校验双通过；
            (None, 错误摘要) —— JSON 非法或结构不满足校验规则。

    Raises:
        无：解析/校验失败均以错误摘要返回，不抛出。
    """
    try:
        data = json.loads(strip_json_fences(text))
    except (json.JSONDecodeError, ValueError, TypeError):
        return None, "输出不是合法的 JSON"
    if not isinstance(data, dict):
        return None, "输出不是 JSON 对象"
    # 结构校验（schemas.script.ScriptContent）：双通过才视为有效剧本
    return validate_script_content(data)


def build_generate_messages(project: Project, raw_text: str,
                            max_chars: int = 4000) -> List[Dict[str, str]]:
    """构造剧本生成的 LLM 消息（中文提示词，注入项目元信息）。

    时长口径统一（P-1 Task 8）：目标时长取 project.target_duration，未设置时
    兜底 DEFAULT_TARGET_DURATION（180 秒）；system 提示词中的 {target} 占位符
    同步注入该目标时长，使场次数量与时长要求自洽。

    Args:
        project: 项目对象（genre/style/fidelity/target_duration/aspect_ratio）。
        raw_text: 用户原始输入文本。
        max_chars: 用户输入的截断上限（默认 4000，保持既有行为；P-7 小说
            改编链路在 novel 模式下传入更大的上限以容纳章节上下文）。

    Returns:
        List[Dict[str, str]]: OpenAI 格式 messages。

    Raises:
        无：纯字符串拼装，属性缺失按「未指定」兜底。
    """
    target = project.target_duration or DEFAULT_TARGET_DURATION
    user_content = (
        f"项目体裁：{project.genre or '未指定'}；视觉风格：{project.style or '未指定'}；"
        f"忠实度：{project.fidelity or 'free'}；目标时长：{target} 秒；"
        f"画幅比例：{project.aspect_ratio}。\n"
        f"用户输入：\n{(raw_text or '')[:max_chars]}"
    )
    return [
        {"role": "system", "content": _GENERATE_SYSTEM_PROMPT.format(target=target)},
        {"role": "user", "content": user_content},
    ]


def build_fallback_script(
    raw_text: str, target_duration: Optional[int] = None
) -> Dict[str, Any]:
    """离线降级：LLM 不可用时基于 raw_text 拆句构造确定性 3 幕占位剧本。

    时长口径统一（P-1 Task 8）：每场景时长按目标时长在 3 个场景上均分
    （非固定 10s），保证 fallback 结构可稳定通过体检的时长校验（±10%）；
    target_duration 缺省/非法时回退 DEFAULT_TARGET_DURATION。

    Args:
        raw_text: 用户原始输入文本。
        target_duration: 项目目标时长（秒，可空；缺省走 DEFAULT_TARGET_DURATION）。

    Returns:
        Dict[str, Any]: 与正常生成同构的剧本 dict（title/logline/outline/scenes）。

    Raises:
        无：纯结构构造，数值异常按缺省时长兜底。
    """
    # 按中英文句读拆句，构造确定性三幕
    sentences = [s.strip() for s in re.split(r"[。！？!?\n；;]+", raw_text) if s.strip()]
    if not sentences:
        sentences = ["主角登场", "矛盾爆发", "故事收尾"]
    n = len(sentences)
    third = max(1, n // 3)
    acts = [
        ("第一幕·开端", sentences[:third] or [sentences[0]]),
        ("第二幕·冲突", sentences[third:2 * third] or [sentences[-1]]),
        ("第三幕·结局", sentences[2 * third:] or [sentences[-1]]),
    ]
    outline = [{"act": name, "summary": ("。".join(sents[:3]) + "。")[:80]} for name, sents in acts]
    # 目标时长兜底（含非法值）：3 个场景均分，保证合计≈目标时长
    try:
        target = int(target_duration) if target_duration else DEFAULT_TARGET_DURATION
    except (TypeError, ValueError):
        target = DEFAULT_TARGET_DURATION
    per_duration = max(1, round(target / len(acts)))
    scenes: List[Dict[str, Any]] = []
    for idx, (name, sents) in enumerate(acts, start=1):
        dialogue = []
        for s in sents[:3]:
            # 句子形如 "角色名：台词" 时提取角色名，否则归为旁白
            m = re.match(r"^([\u4e00-\u9fa5A-Za-z0-9·]{1,10})[：:]\s*(.+)$", s)
            if m:
                dialogue.append({"character": m.group(1), "line": m.group(2)})
            else:
                dialogue.append({"character": "旁白", "line": s})
        scenes.append({
            "scene_no": idx,
            "title": name,
            "plot": ("。".join(sents[:3]) + "。")[:200],
            "dialogue": dialogue,
            "duration_sec": per_duration,
        })
    first = sentences[0]
    title = (first[:12] + "…") if len(first) > 12 else first
    logline = ("。".join(sentences[:2]) + "。")[:60]
    return {
        "title": title or "离线占位剧本",
        "logline": logline or "离线降级生成的占位剧本",
        "outline": outline,
        "scenes": scenes,
    }


def wrap_content_fields(content: Dict[str, Any], source: str) -> Dict[str, Any]:
    """RD-03 字段级来源标记：一级字段包装 + scenes 元素级 source（双层包装）。

    包装形态（P-1 Task 8 场次级 source 补齐）：
    - 顶层：每个一级字段包装为 {"value": ..., "source": source}（保持原行为）；
    - 场次层：若某一级字段为 scenes 列表，则其 value 列表中的每个 scene dict
      追加 `source` 键（同一 source），使场次可获得独立的来源标记（供
      merge_scene_sources 逐场比对与锁定）。

    兼容性说明：顶层仍为 {value, source} 形态，content_plain 可正常解包
    （其识别 {"value":…, "source":…} 结构并取 value）；scenes.value 仍为
    数组，unwrap_scenes 直接取用不受元素级 source 影响；merge_scene_sources
    读取旧场次的 source 键做逐场合并（旧数据无该键时视为 "ai"）。

    Args:
        content: 剧本内容 dict（一级字段为 title/logline/outline/scenes 等）。
        source: 来源标记（"ai" 由模型生成 / "user" 由用户编辑 /
            "ai_optimized" 由 AI 优化）。

    Returns:
        Dict[str, Any]: 包装后的 content dict（scenes 元素带 source 键）。

    Raises:
        无：结构异常按缺数据处理，不抛出。
    """
    wrapped: Dict[str, Any] = {}
    for key, value in content.items():
        if key == "scenes" and isinstance(value, list):
            # 场次级：逐场追加 source 键（非 dict 元素原样保留）
            scenes: List[Any] = []
            for scene in value:
                if isinstance(scene, dict):
                    scene = {**scene, "source": source}
                scenes.append(scene)
            wrapped[key] = {"value": scenes, "source": source}
        else:
            wrapped[key] = {"value": value, "source": source}
    return wrapped


def unwrap_scenes(content: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从剧本 content 中取出场景数组（兼容三种落库形态）。

    兼容形态（与 content_plain 口径一致，2026-10-15 下游接收修复补齐）：
    - 整体包装 {"value": {...}, "source": ...}（手动保存 PUT body 落库形态）；
    - 字段级包装 {"scenes": {"value": [...], "source": ...}}（AI 生成，RD-03）；
    - 未包装 {"scenes": [...]}（旧数据 / 直写形态）。

    Args:
        content: 剧本 content dict。

    Returns:
        List[Dict[str, Any]]: 场景列表；结构不符或缺失时返回空列表。
    """
    if not isinstance(content, dict):
        return []
    raw = content
    # 整体包装：解到内层纯结构再取 scenes
    if isinstance(raw.get("value"), dict) and "source" in raw:
        raw = raw["value"]
    scenes = raw.get("scenes")
    if isinstance(scenes, dict) and "value" in scenes:
        scenes = scenes["value"]
    return scenes if isinstance(scenes, list) else []


def content_plain(content: Any) -> Dict[str, Any]:
    """把剧本 content JSONB 解包为纯结构 dict（自路由层迁入，公开工具函数）。

    兼容三种落库形态（与前端 normalizeScript 口径一致）：
    - 整体包装 {"value": {...}, "source": "user"}（手动保存）；
    - 字段级包装 {"title": {"value":…, "source":…}, ...}（AI 生成，RD-03）；
    - 纯文本 {"raw": 全文} 或其他自由结构（原样返回）。

    Args:
        content: 剧本 content JSONB 值（可能为 None）。

    Returns:
        Dict[str, Any]: 解包后的纯结构；content 非法时返回空 dict。

    Raises:
        无：结构异常一律按空结构处理，不抛出。
    """
    if not isinstance(content, dict):
        return {}
    if isinstance(content.get("value"), dict) and "source" in content:
        return dict(content["value"])
    plain: Dict[str, Any] = {}
    for key, val in content.items():
        if isinstance(val, dict) and "value" in val and "source" in val:
            plain[key] = val["value"]
        else:
            plain[key] = val
    return plain


def content_has_payload(plain: Dict[str, Any]) -> bool:
    """判断解包后的剧本结构是否含实质内容（自路由层迁入，公开工具函数）。

    Args:
        plain: content_plain 的解包结果。

    Returns:
        bool: raw/title/logline/outline/scenes 任一非空即为 True。

    Raises:
        无：纯字典取值判断。
    """
    if plain.get("raw") or plain.get("title") or plain.get("logline"):
        return True
    if plain.get("outline") or plain.get("scenes"):
        return True
    return False


def _scene_fingerprint(scene: Dict[str, Any]) -> str:
    """构造场次内容指纹（plot/action/dialogue 全文稳定序列化，内部函数）。

    Args:
        scene: 场次 dict（缺字段按空值处理）。

    Returns:
        str: 稳定序列化指纹（用于场次内容全等比较）；序列化异常按空串兜底。

    Raises:
        无：json.dumps 失败（如含不可序列化对象）时返回空串，不抛出。
    """
    try:
        return json.dumps({
            "plot": scene.get("plot") or "",
            "action": scene.get("action") or "",
            "dialogue": scene.get("dialogue") or [],
        }, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return ""


def merge_scene_sources(old_content: Any, new_content: Any) -> Dict[str, Any]:
    """场次级 source 合并（P-1 Task 5 正文保存，RD-03 场次粒度扩展，纯函数）。

    对 new_content 的 scenes 逐场与旧 content（content_plain 解包）的
    scenes 按 scene_no+title 匹配：
    - 旧 scenes 为空（首次保存/旧行无结构化场次）→ 新场次全部标 "ai"
      （AI 生成后未改动直接保存的场景）；
    - 匹配到旧场次且内容（plot/action/dialogue 全文比较）一致 → 保留旧
      场次的 source（旧数据场景无 source 时视为 "ai"）；
    - 内容不一致（用户改过）或新增场次 → 标 "user"。

    Args:
        old_content: DB 旧 content JSONB（支持整体包装/字段级包装/纯结构，
            可为 None）。
        new_content: 用户提交的新结构化 content（不被就地修改）。

    Returns:
        Dict[str, Any]: 合并 source 后的新 content 深拷贝（scenes 数组元素
            带 source 字段）；new_content 非 dict 或 scenes 非列表时按原
            结构深拷贝返回。

    Raises:
        无：结构异常一律按缺数据处理，不抛出。
    """
    old_plain = content_plain(old_content) if isinstance(old_content, dict) else {}
    old_scenes = old_plain.get("scenes")
    old_scenes = old_scenes if isinstance(old_scenes, list) else []
    # 旧场次索引：按 (scene_no, title) 匹配（同键取首个，scene_no/title 归一为字符串）
    old_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for sc in old_scenes:
        if isinstance(sc, dict):
            key = (str(sc.get("scene_no") or ""), str(sc.get("title") or "").strip())
            old_index.setdefault(key, sc)
    # 深拷贝隔离：不修改调用方传入的 new_content
    merged = copy.deepcopy(new_content) if isinstance(new_content, dict) else {}
    scenes = merged.get("scenes")
    if not isinstance(scenes, list):
        return merged
    first_save = not old_scenes
    for sc in scenes:
        if not isinstance(sc, dict):
            continue
        if first_save:
            sc["source"] = "ai"
            continue
        key = (str(sc.get("scene_no") or ""), str(sc.get("title") or "").strip())
        old = old_index.get(key)
        if old is not None and _scene_fingerprint(old) == _scene_fingerprint(sc):
            # 内容未变：保留旧场次 source（旧数据无 source 视为 ai）
            sc["source"] = str(old.get("source") or "ai")
        else:
            # 内容改动或新增场次：标记为用户修改
            sc["source"] = "user"
    return merged


# ---------------------------------------------------------------------------
# P-6 AI 优化（Task 7）：path 安全工具 + 片段化 diff（禁止整篇覆盖）
# ---------------------------------------------------------------------------

# scenes 场次级可优化字段白名单（path 形如 scenes[i].<field>）
OPTIMIZABLE_SCENE_FIELDS = frozenset({
    "plot", "action", "title", "emotion", "location", "time_of_day",
    "int_ext", "duration_sec",
})

# 对白级可优化字段白名单（path 形如 scenes[i].dialogue[j].<field>）
OPTIMIZABLE_DIALOGUE_FIELDS = frozenset({"line", "emotion"})

# path 解析缺失哨兵：get_value_by_path 的缺省返回值，用于区分
# 「路径存在但值为 None」与「路径不存在」
PATH_MISSING = object()

# path 片段正则：标识符 + 可选 [数字] 下标（如 scenes[2] / dialogue[0] / line）
_PATH_SEG_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?")
# path 末段（字段名）正则：纯标识符，不允许下标
_PATH_LEAF_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def split_content_path(path: str) -> Tuple[List[Any], str]:
    """解析结构化 content 的字段路径为（段序列, 字段名）（纯函数）。

    支持形如 "scenes[2].dialogue[0].line" 的路径：下标片段解析为 int，
    普通片段解析为 str；末段固定为字段名（不含下标）。路径非法时返回
    ([], "")，由调用方判定非法。

    Args:
        path: 字段路径字符串（如 "scenes[2].plot" / "scenes[0].dialogue[1].line"）。

    Returns:
        Tuple[List[Any], str]: (段序列, 末段字段名)。如
            "scenes[2].dialogue[0].line" → (["scenes", 2, "dialogue", 0], "line")；
            非法路径返回 ([], "")。

    Raises:
        无：非法输入一律返回空结果，不抛出。
    """
    if not isinstance(path, str) or not path.strip():
        return [], ""
    parts = [p.strip() for p in path.strip().split(".")]
    if any(not p for p in parts):
        return [], ""
    segs: List[Any] = []
    for part in parts[:-1]:
        m = _PATH_SEG_RE.fullmatch(part)
        if m is None:
            return [], ""  # 中间片段非法（含括号不匹配等）
        segs.append(m.group(1))
        if m.group(2) is not None:
            segs.append(int(m.group(2)))  # 下标片段转为 int，便于索引
    leaf = parts[-1]
    if _PATH_LEAF_RE.fullmatch(leaf) is None:
        return [], ""  # 末段必须是纯字段名
    return segs, leaf


def _resolve_content_path(plain: Any, path: str) -> Tuple[bool, Any]:
    """按路径在 plain 结构中取值并返回是否存在（内部函数）。

    Args:
        plain: 解包后的纯结构（dict/list 嵌套）。
        path: 字段路径（split_content_path 可解析）。

    Returns:
        Tuple[bool, Any]: (是否存在, 值)；路径非法或中途缺失返回 (False, None)。

    Raises:
        无：越界/类型不符一律按不存在处理，不抛出。
    """
    segs, leaf = split_content_path(path)
    if not leaf:
        return (False, None)
    node = plain
    for key in segs:
        if isinstance(key, int):
            if not isinstance(node, list) or key < 0 or key >= len(node):
                return (False, None)  # 数组下标越界
            node = node[key]
        else:
            if not isinstance(node, dict) or key not in node:
                return (False, None)  # 对象缺键
            node = node[key]
    if isinstance(node, dict) and leaf in node:
        return (True, node[leaf])
    return (False, None)


def get_value_by_path(plain: Any, path: str, default: Any = PATH_MISSING) -> Any:
    """按字段路径读取值（纯函数，路径不存在时返回 default）。

    Args:
        plain: 解包后的纯结构（dict/list 嵌套）。
        path: 字段路径（如 "scenes[2].plot"）。
        default: 路径不存在时的返回值（缺省为 PATH_MISSING 哨兵，便于
            调用方区分「值为 None」与「路径不存在」）。

    Returns:
        Any: 路径存在时返回值（可能为 None）；否则返回 default。

    Raises:
        无：结构异常按路径不存在处理，不抛出。
    """
    exists, value = _resolve_content_path(plain, path)
    return value if exists else default


def _set_value_at(node: Any, segs: List[Any], leaf: str, value: Any) -> Any:
    """沿段序列不可变式写值（内部递归函数）。

    Args:
        node: 当前节点（dict/list）。
        segs: 剩余段序列（str 键 / int 下标）。
        leaf: 目标字段名。
        value: 待写入值。

    Returns:
        Any: 新节点（沿途容器为新建副本，未命中路径时原样返回 node）。

    Raises:
        无：路径不可达时返回原节点，不抛出。
    """
    if not segs:
        if isinstance(node, dict):
            new_node = dict(node)
            new_node[leaf] = value
            return new_node
        return node
    key = segs[0]
    if isinstance(key, int):
        if not isinstance(node, list) or key < 0 or key >= len(node):
            return node
        new_list = list(node)
        new_list[key] = _set_value_at(new_list[key], segs[1:], leaf, value)
        return new_list
    if not isinstance(node, dict) or key not in node:
        return node
    new_dict = dict(node)
    new_dict[key] = _set_value_at(node[key], segs[1:], leaf, value)
    return new_dict


def set_value_by_path(plain: Any, path: str, value: Any) -> Any:
    """按字段路径不可变式写值（纯函数，返回新结构）。

    沿途 dict/list 均新建副本（未改动分支共享引用但只读），避免就地修改
    调用方结构导致 SQLAlchemy 变更检测失效（Task 3 deepcopy 教训）。

    Args:
        plain: 解包后的纯结构（dict/list 嵌套）。
        path: 字段路径（如 "scenes[2].dialogue[0].line"）。
        value: 待写入值。

    Returns:
        Any: 写入后的新结构；路径非法或不可达时原样返回 plain。

    Raises:
        无：异常路径一律返回原结构，不抛出。
    """
    segs, leaf = split_content_path(path)
    if not leaf:
        return plain
    return _set_value_at(plain, segs, leaf, value)


def is_optimizable_path(path: str) -> bool:
    """判断路径是否为 P-6 AI 优化允许的字段范围（纯函数）。

    仅允许两类路径：
    - scenes[i].<field>，field ∈ OPTIMIZABLE_SCENE_FIELDS；
    - scenes[i].dialogue[j].<field>，field ∈ OPTIMIZABLE_DIALOGUE_FIELDS。

    Args:
        path: 字段路径字符串。

    Returns:
        bool: True —— 匹配上述白名单；否则 False。

    Raises:
        无：非法输入返回 False，不抛出。
    """
    segs, leaf = split_content_path(path)
    if not segs or not leaf:
        return False
    if (len(segs) == 2 and segs[0] == "scenes" and isinstance(segs[1], int)
            and leaf in OPTIMIZABLE_SCENE_FIELDS):
        return True
    if (len(segs) == 4 and segs[0] == "scenes" and isinstance(segs[1], int)
            and segs[2] == "dialogue" and isinstance(segs[3], int)
            and leaf in OPTIMIZABLE_DIALOGUE_FIELDS):
        return True
    return False


def _split_diff_units(text: str) -> List[str]:
    """把文本按换行 + 中文句读切分为「行」单元（内部函数，供 diff 对比）。

    切分规则：先按换行拆行，再按中文句读（。！？；及英文 !?;）切句，
    句读符保留在前一单元末尾；无任何分隔符时整串作为 1 个单元。

    Args:
        text: 待切分文本（可为 None/空串）。

    Returns:
        List[str]: 单元列表（可能为空）。

    Raises:
        无：纯字符串切分。
    """
    if not text:
        return []
    units: List[str] = []
    for line in str(text).split("\n"):
        if not line:
            continue
        parts = re.findall(r"[^。！？；!?;]+[。！？；!?;]?", line)
        units.extend([p for p in parts if p])
    return units


def _change_id(path: str, original: Any, proposed: Any) -> str:
    """构造 change 的稳定哈希 ID（内部函数，optimize/apply 双侧一致）。

    Args:
        path: 字段路径。
        original: 变更前原文。
        proposed: 变更后建议值。

    Returns:
        str: sha1 前 12 位十六进制串（同 path/original/proposed 幂等一致）。

    Raises:
        无：纯哈希计算。
    """
    raw = f"{path}\u0000{'' if original is None else original}\u0000{'' if proposed is None else proposed}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def build_field_diffs(plain_content: Dict[str, Any], changes: List[dict]) -> List[dict]:
    """把优化 changes 转换为片段化 diff（P-6 Task 7，纯函数）。

    对每条 change 产出 diff 片段：{id, path, kind:"field", original,
    proposed, reason, diff_lines}。diff_lines 由 difflib.SequenceMatcher
    对「行/句单元」做对比（中文按句读 + 换行切分），opcode replace 拆为
    delete + insert 两段。**禁止整篇覆盖**：即使 grain=global 也只返回
    片段化 changes，绝不返回整篇新文本字段。

    Args:
        plain_content: 解包后的当前纯结构（仅作上下文占位，diff 只按
            change 自身的 original/proposed 计算）。
        changes: 优化 Agent 规整后的 changes 列表，元素结构
            {path, original, proposed, reason}。

    Returns:
        List[dict]: diff 片段列表（id 为稳定哈希；diff_lines 元素形如
            {"op": "equal"|"delete"|"insert", "text": 单元文本}）。

    Raises:
        无：结构异常条目跳过，不抛出。
    """
    diffs: List[dict] = []
    for item in changes or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        original = item.get("original")
        proposed = item.get("proposed")
        a_units = _split_diff_units("" if original is None else str(original))
        b_units = _split_diff_units("" if proposed is None else str(proposed))
        diff_lines: List[dict] = []
        matcher = difflib.SequenceMatcher(a=a_units, b=b_units, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                diff_lines.append({"op": "equal", "text": "".join(a_units[i1:i2])})
            elif tag == "delete":
                diff_lines.append({"op": "delete", "text": "".join(a_units[i1:i2])})
            elif tag == "insert":
                diff_lines.append({"op": "insert", "text": "".join(b_units[j1:j2])})
            else:  # replace：拆为 delete + insert，便于前端逐段渲染
                if i2 > i1:
                    diff_lines.append({"op": "delete", "text": "".join(a_units[i1:i2])})
                if j2 > j1:
                    diff_lines.append({"op": "insert", "text": "".join(b_units[j1:j2])})
        diffs.append({
            "id": _change_id(path, original, proposed),
            "path": path,
            "kind": "field",
            "original": original,
            "proposed": proposed,
            "reason": str(item.get("reason") or ""),
            "diff_lines": diff_lines,
        })
    return diffs


def apply_optimize_changes(
    old_content: Any, changes: List[dict]
) -> Tuple[Dict[str, Any], int, int]:
    """应用选中的优化片段并重写 source 标记（P-6 Task 7，纯函数）。

    逐条校验：path 合法性（白名单）→ 可解析（下标不越界）→ original 与
    当前库值一致（不一致视为并发改过，跳过防覆盖）→ set_value_by_path
    写入 proposed。应用后重写 content 包装（保持 wrap_content_fields
    兼容形态：「一级字段 {value, source}」+ scenes 元素级 source）：
    - 被改动场次（任一改动落在该 scenes[i]）→ 元素 source 标 "ai_optimized"；
    - 未改动场次 → 保留旧元素 source（旧数据无 source 视为 "ai"）；
    - 一级字段 source：字段级包装保留各字段旧 source，整体包装沿用整体 source。

    Args:
        old_content: DB 旧 content JSONB（支持整体包装/字段级包装/纯结构）。
        changes: 选中并待应用的片段列表，元素结构
            {id, path, original, proposed}。

    Returns:
        Tuple[Dict[str, Any], int, int]: (重包装后的新 content, applied 数,
            skipped 数)。applied=0 时新 content 返回原 content（无实质变更）。

    Raises:
        无：结构异常条目计入 skipped，不抛出。
    """
    plain = content_plain(old_content)
    old_scenes = plain.get("scenes")
    old_scenes = old_scenes if isinstance(old_scenes, list) else []
    total = len(changes or [])
    if not old_scenes:
        # 无结构化场次：无可应用路径
        return (old_content if isinstance(old_content, dict) else {}, 0, total)

    working = copy.deepcopy(plain)  # 深拷贝隔离，避免引用共享
    edited: set = set()
    applied = 0
    skipped = 0
    for item in changes or []:
        if not isinstance(item, dict):
            skipped += 1
            continue
        path = str(item.get("path") or "")
        if not is_optimizable_path(path):
            skipped += 1  # path 不在白名单
            continue
        exists, current = _resolve_content_path(working, path)
        if not exists:
            skipped += 1  # 路径越界/缺失
            continue
        original = item.get("original")
        want = "" if original is None else str(original)
        have = "" if current is None else str(current)
        if want != have:
            skipped += 1  # 原文与当前库值不符（并发改动），跳过防覆盖
            continue
        working = set_value_by_path(working, path, item.get("proposed"))
        segs, _leaf = split_content_path(path)
        if len(segs) >= 2 and segs[0] == "scenes" and isinstance(segs[1], int):
            edited.add(segs[1])
        applied += 1

    if applied == 0:
        return (old_content if isinstance(old_content, dict) else {}, 0, skipped)

    # ---- 重写 scenes 元素级 source ----
    new_scenes = working.get("scenes")
    if isinstance(new_scenes, list):
        for idx, scene in enumerate(new_scenes):
            if not isinstance(scene, dict):
                continue
            if idx in edited:
                scene["source"] = "ai_optimized"
            else:
                old_scene = old_scenes[idx] if (
                    idx < len(old_scenes) and isinstance(old_scenes[idx], dict)) else None
                scene["source"] = str(old_scene.get("source")) if (
                    old_scene and old_scene.get("source")) else "ai"

    # ---- 一级字段 source：字段级包装保留各字段 source，整体包装用整体 source ----
    field_sources: Dict[str, str] = {}
    overall_source = "ai"
    if (isinstance(old_content, dict) and isinstance(old_content.get("value"), dict)
            and "source" in old_content):
        overall_source = str(old_content.get("source") or "user")
    elif isinstance(old_content, dict):
        for key, val in old_content.items():
            if isinstance(val, dict) and "value" in val and "source" in val:
                field_sources[key] = str(val.get("source") or "ai")

    new_content: Dict[str, Any] = {}
    for key, val in working.items():
        new_content[key] = {"value": val, "source": field_sources.get(key, overall_source)}
    return (new_content, applied, skipped)


def build_novel_context(project: Project, max_chars: int = 12000,
                        fallback_text: Optional[str] = None) -> str:
    """构造小说改编的长文本上下文（P-7 长文截断消除，供生成/抽取复用）。

    输入源取 project.config["novel_chapters"]（由 /script/input/novel/parse
    落库的章节元信息 + 摘要 + 正文前 2000 字预览）：

    1. 先按「第N章 标题：摘要」逐章拼接摘要；
    2. 长度仍不足 max_chars 时，再逐章追加「正文节选」（content_preview），
       直到逼近 max_chars 上限或章节耗尽；
    3. 最终裁剪到 max_chars。

    无章节数据（非 novel 模式/尚未解析）时回退 fallback_text 的 max_chars
    截断（调用方传 script.raw_text）；两者皆空返回空串。

    Args:
        project: 项目对象（读取 config.novel_chapters）。
        max_chars: 上下文长度上限（默认 12000，替代旧 raw_text[:4000] 硬截断）。
        fallback_text: 无章节数据时的兜底文本（通常为 script.raw_text）。

    Returns:
        str: 拼接后的上下文文本（可能为空串）。

    Raises:
        无：结构异常按空章节处理。
    """
    chapters = (project.config or {}).get("novel_chapters")
    if not isinstance(chapters, list) or not chapters:
        return (fallback_text or "")[:max_chars]

    valid = [c for c in chapters if isinstance(c, dict)]
    # 第一段：逐章摘要
    head_lines = [
        f"第{c.get('index')}章 {c.get('title')}：{c.get('summary') or ''}"
        for c in valid
    ]
    text = "\n".join(head_lines)
    # 第二段：长度不足时追加正文节选补足（避开首章正文节选以外的丢失）
    if len(text) < max_chars:
        extra_lines: List[str] = []
        extra_len = 0
        for chapter in valid:
            preview = str(chapter.get("content_preview") or "").strip()
            if not preview:
                continue
            line = f"【第{chapter.get('index')}章正文节选】{preview}"
            if len(text) + extra_len + len(line) + 1 > max_chars:
                break
            extra_lines.append(line)
            extra_len += len(line) + 1
        if extra_lines:
            text = "\n".join(head_lines + extra_lines)
    return text[:max_chars]


def build_extract_source_text(script: Script) -> str:
    """构造实体抽取的源文本（P0 断链修复：抽取源从 raw_text 改判为生成的完整剧本）。

    取源优先级：
    1. content 解包出结构化剧本（title/logline/outline 各幕 summary/scenes
       每场 title+plot+对白行）后拼接为纯文本——对白按「角色名：台词」逐行
       格式输出，规则路 _DIALOGUE_NAME_RE 依赖该格式才能抽到角色名；
    2. 结构化内容无实质 payload（如仅保存过 idea/novel 输入）且 raw_text
       非空 → 返回 raw_text（novel 兜底）；
    3. 两者皆空 → 抛 400。

    Args:
        script: 剧本 ORM 对象（读取 content 与 raw_text）。

    Returns:
        str: 拼接后的抽取用纯文本（非空）。

    Raises:
        HTTPException: 400 —— 剧本尚无任何内容（既无结构化剧本也无原始输入）。
    """
    plain = content_plain(script.content)
    lines: List[str] = []
    if plain.get("title"):
        lines.append(f"标题：{plain['title']}")
    if plain.get("logline"):
        lines.append(f"梗概：{plain['logline']}")
    # 三幕大纲：每幕 "幕名：概要" 一行
    for act in (plain.get("outline") or []):
        if isinstance(act, dict):
            text_line = f"{act.get('act', '')}：{act.get('summary', '')}".strip("：")
            if text_line:
                lines.append(text_line)
    # 场景：每场标题 + 地点信息 + 剧情描述 + 对白行（"角色名：台词" 逐行格式）。
    # 地点行显式输出（P0-2 对应关系补强）：镜头绑定的 location/time_of_day/
    # int_ext 即资产管理「场景」资产应对应的地点，显式注入抽取源使 LLM/规则
    # 路都能稳定抽出这些地点为场景实体，保证"镜头 → 场景资产"可对应。
    for sc in (plain.get("scenes") or []):
        if not isinstance(sc, dict):
            continue
        scene_no = sc.get("scene_no", "")
        heading = f"镜头{scene_no}《{sc.get('title', '')}》"
        loc_parts = [
            str(sc.get(key) or "").strip()
            for key in ("location", "time_of_day", "int_ext")
        ]
        loc_parts = [p for p in loc_parts if p]
        if loc_parts:
            heading += f"（地点：{'，'.join(loc_parts)}）"
        lines.append(heading)
        if str(sc.get("plot") or "").strip():
            lines.append(str(sc["plot"]))
        for dlg in (sc.get("dialogue") or []):
            if not isinstance(dlg, dict):
                continue
            char = str(dlg.get("character") or "").strip()
            line_text = str(dlg.get("line") or "").strip()
            if not line_text:
                continue
            # 有角色名按 "角色名：台词" 输出（规则路依赖该格式）；无角色名仅输出台词
            lines.append(f"{char}：{line_text}" if char else line_text)

    structured_text = "\n".join(lines).strip()
    if structured_text:
        return structured_text

    # 兜底：无结构化 payload（novel/idea 模式）时使用用户原始输入
    raw_text = (script.raw_text or "").strip()
    if raw_text:
        return raw_text
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail="剧本尚无内容，请先生成或保存剧本")


# ---------------------------------------------------------------------------
# RD-02 结构化体检（P-1 Task 8：规则对齐决议四条）
# ---------------------------------------------------------------------------
# 单条对白字数上限（超过建议拆分）
_HEALTH_DIALOGUE_MAX_CHARS = 120

# 场景时长合计与目标时长的偏差阈值（PRD 收紧到 ±10%）
_HEALTH_DURATION_DEVIATION = 0.10

# 对白语速（字/秒）：估算对白时长 = 对白总字数 / 该值
_HEALTH_DIALOGUE_SPEED = 4

# 对白估算时长与场次时长合计的交叉校验偏差阈值
_HEALTH_DIALOGUE_CROSS_DEVIATION = 0.30

# 高潮点情绪峰值词（末段场次 emotion 命中其一即视为存在高潮）
_PEAK_EMOTION_WORDS = ("爆发", "高潮", "激烈", "突破", "决绝", "释然")

# 旁白角色名（不计入角色出场统计）
_NARRATOR_NAME = "旁白"

# 体检计分扣分表（error -25 / warning -10 / info -3，下限 0）
_HEALTH_DEDUCTIONS = {"error": 25, "warning": 10, "info": 3}


def _normalize_dialogue(raw: Any) -> List[Dict[str, Any]]:
    """把场次 dialogue 归一为对白 dict 列表（兼容字符串对白，内部函数）。

    Args:
        raw: dialogue 原始值（列表 / 字符串 / 其它）。

    Returns:
        List[Dict[str, Any]]: 归一后的对白条目列表（字符串对白按行拆为
            {"character","line"}，无冒号时 character 为空串）。

    Raises:
        无：结构异常按缺数据处理，不抛出。
    """
    if isinstance(raw, str):
        result: List[Dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            parts = line.split("：", 1)
            if len(parts) > 1:
                result.append({"character": parts[0].strip(), "line": parts[1]})
            else:
                result.append({"character": "", "line": line})
        return result
    if isinstance(raw, list):
        return [d for d in raw if isinstance(d, dict)]
    return []


def _scene_label(scene: Dict[str, Any], index: int) -> str:
    """构造场次定位标签（内部函数，供 location/message 使用）。

    术语约定：剧本正文中的场次段落粒度为「镜头」（为分镜准备），标签以
    「镜头N」呈现，与资产管理中指地点的「场景」区分。

    Args:
        scene: 场次 dict。
        index: 场次在 scenes 数组中的下标（0 起，scene_no 缺失时兜底）。

    Returns:
        str: 形如「镜头3」的定位标签。

    Raises:
        无：字段缺失按下标兜底。
    """
    return f"镜头{scene.get('scene_no', index + 1)}"


def _appearance_indices(scenes: List[Dict[str, Any]], name: str) -> List[int]:
    """返回角色在各场次中出场的下标列表（内部函数，去重保序）。

    Args:
        scenes: 场次列表。
        name: 目标角色名（精确匹配 dialogue.character）。

    Returns:
        List[int]: 出场场次下标列表（升序，无重复）。

    Raises:
        无：结构异常按未出场处理。
    """
    indices: List[int] = []
    for i, sc in enumerate(scenes):
        if not isinstance(sc, dict):
            continue
        for d in _normalize_dialogue(sc.get("dialogue")):
            if str(d.get("character") or "").strip() == name:
                indices.append(i)
                break
    return indices


def _build_report(issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按扣分表把 issues 汇总为体检报告（内部函数）。

    Args:
        issues: 体检问题列表（元素含 level/message/location）。

    Returns:
        Dict[str, Any]: {"score": 0~100, "issues": issues}（起始 100 分，
            error -25 / warning -10 / info -3，下限 0）。

    Raises:
        无：纯数值计算。
    """
    score = 100
    for it in issues:
        score -= _HEALTH_DEDUCTIONS.get(str(it.get("level")), 0)
    return {"score": max(0, min(100, score)), "issues": issues}


def run_health_check(
    project: Project,
    script: Script,
    confirmed_characters: Optional[List[str]] = None,
    location_prefix: str = "",
) -> Dict[str, Any]:
    """剧本规则体检（RD-02，P-1 Task 8 决议规则重写）。

    体检规则（对齐 RD-02 决议四条；旧「无场景/无对白/对白缺角色名」已移除，
    无场景与无对白降级为生成闸门防御，不再出现在体检报告中）：
    1. 角色出场断层（warning，location=集/全局）：传入已确认角色清单时，检查
       各场次 dialogue 的 character（过滤旁白）引用——某角色在全部内容中零出场
       → warning「角色「X」未在任何场次出场」；某角色出场场次序号不连续且最后
       出场场次 < 总场次 60% → warning「角色「X」出场断层」；
    2. 对白过长（warning，location=场次）：单条 dialogue.line 字数 > 120
       → warning「场景N 第M条对白 K 字，建议拆分」；
    3. 缺动作描写（warning，location=场次）：场次 action 缺失或空字符串，
       且该剧本属于新结构（至少一个场次含 action 键）→ warning；旧 Schema
       （全部场次无 action 键，如快速模式旧数据）自动跳过该规则避免误报；
    4. 缺高潮点（info，location=集/全局）：末 20% 场次的 emotion 均不含峰值词
       （爆发/高潮/激烈/突破/决绝/释然）且 plot 不含「!」/「！」→ info；
    另：时长偏差（error，阈值 ±10%）与对白语速交叉校验（warning，偏差 >30%）。

    计分：起始 100 分，每个 error 扣 25、warning 扣 10、info 扣 3，下限 0。

    Args:
        project: 项目对象（读取 target_duration）。
        script: 剧本对象（读取 content）。
        confirmed_characters: 已确认角色名清单（可空；用于角色出场断层规则，
            空/None 时跳过该规则）。
        location_prefix: 定位前缀（按集体检时传入「第N集 · 」，单剧本为空串）。

    Returns:
        Dict[str, Any]: {"score": 0~100, "issues": [{"level","message","location"}]}。

    Raises:
        无：结构异常按缺数据处理，不抛出。
    """
    scenes = unwrap_scenes(script.content)
    issues: List[Dict[str, Any]] = []

    # ---- 规则 1：角色出场断层（需已确认角色清单与场次数据） ----
    if scenes and confirmed_characters:
        total_scenes = len(scenes)
        for name in confirmed_characters:
            cname = str(name or "").strip()
            if not cname or cname == _NARRATOR_NAME:
                continue
            idxs = _appearance_indices(scenes, cname)
            if not idxs:
                issues.append({
                    "level": "warning",
                    "message": f"角色「{cname}」未在任何场次出场",
                    "location": f"{location_prefix}全局",
                })
                continue
            last = idxs[-1]
            contiguous = idxs == list(range(idxs[0], idxs[-1] + 1))
            # 断层判定：出场场次序号不连续，且最后出场场次 < 总场次 60%
            if not contiguous and last < total_scenes * 0.6:
                issues.append({
                    "level": "warning",
                    "message": f"角色「{cname}」出场断层（后段场次未见出场）",
                    "location": f"{location_prefix}全局",
                })

    # ---- 规则 2：对白过长（定位到场次与第 M 条） ----
    for i, sc in enumerate(scenes):
        if not isinstance(sc, dict):
            continue
        label = _scene_label(sc, i)
        for j, d in enumerate(_normalize_dialogue(sc.get("dialogue")), start=1):
            line = str(d.get("line") or "")
            if len(line) > _HEALTH_DIALOGUE_MAX_CHARS:
                issues.append({
                    "level": "warning",
                    "message": f"{label} 第{j}条对白 {len(line)} 字，建议拆分",
                    "location": f"{location_prefix}{label}",
                })

    # ---- 规则 3：缺动作描写（仅新 Schema：至少一场含 action 键时启用） ----
    has_action_schema = any(
        isinstance(sc, dict) and "action" in sc for sc in scenes)
    if has_action_schema:
        for i, sc in enumerate(scenes):
            if not isinstance(sc, dict):
                continue
            if not str(sc.get("action") or "").strip():
                label = _scene_label(sc, i)
                issues.append({
                    "level": "warning",
                    "message": f"{label} 缺少动作描写（action 为空）",
                    "location": f"{location_prefix}{label}",
                })

    # ---- 规则 4：缺高潮点（末 20% 场次无情绪峰值词且 plot 无感叹号） ----
    if scenes:
        tail_count = max(1, round(len(scenes) * 0.2))
        has_peak = False
        for sc in scenes[-tail_count:]:
            if not isinstance(sc, dict):
                continue
            emotion = str(sc.get("emotion") or "")
            plot = str(sc.get("plot") or "")
            if any(word in emotion for word in _PEAK_EMOTION_WORDS) or "!" in plot or "！" in plot:
                has_peak = True
                break
        if not has_peak:
            issues.append({
                "level": "info",
                "message": "末段（后 20% 场次）缺少情绪高潮点，建议强化冲突或情绪峰值",
                "location": f"{location_prefix}全局",
            })

    # ---- 时长校验（合计 vs 目标时长，阈值 ±10%） ----
    total_duration = 0
    total_chars = 0
    for sc in scenes:
        if not isinstance(sc, dict):
            continue
        try:
            total_duration += int(sc.get("duration_sec") or 0)
        except (TypeError, ValueError):
            continue
        for d in _normalize_dialogue(sc.get("dialogue")):
            total_chars += len(str(d.get("line") or ""))
    target = project.target_duration
    if target and scenes:
        deviation = abs(total_duration - target) / target
        if deviation > _HEALTH_DURATION_DEVIATION:
            issues.append({
                "level": "error",
                "message": (
                    f"场景时长合计 {total_duration}s 与目标时长 {target}s "
                    f"偏差 {deviation:.0%}，超过 10%"),
                "location": f"{location_prefix}全局",
            })

    # ---- 对白语速交叉校验：估算对白时长（字/秒）vs 场次时长合计 ----
    if scenes and total_duration > 0:
        estimated = total_chars / _HEALTH_DIALOGUE_SPEED
        cross_deviation = abs(estimated - total_duration) / total_duration
        if cross_deviation > _HEALTH_DIALOGUE_CROSS_DEVIATION:
            issues.append({
                "level": "warning",
                "message": (
                    f"对白总字数 {total_chars} 字估算约 {estimated:.0f} 秒，"
                    f"与场次时长合计 {total_duration} 秒偏差过大，配音时长可能不符"),
                "location": f"{location_prefix}全局",
            })

    return _build_report(issues)


async def list_confirmed_character_names(db: AsyncSession, project_id: int) -> List[str]:
    """查询项目已确认（未删）角色名清单（P-1 Task 8 按集体检共用）。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。

    Returns:
        List[str]: 角色名列表（去重保序；无角色时为空列表）。

    Raises:
        无：纯查询。
    """
    rows = await db.execute(
        select(Character.name)
        .where(Character.project_id == project_id, Character.deleted_at.is_(None))
        .order_by(Character.id)
    )
    names: List[str] = []
    for name in rows.scalars().all():
        text = str(name or "").strip()
        if text and text not in names:
            names.append(text)
    return names


async def run_project_health_check(
    db: AsyncSession, project: Project, episode_id: Optional[int] = None
) -> Dict[str, Any]:
    """按剧集范围执行剧本体检（P-1 Task 8 按集体检）。

    语义：
    - episode_id 传值：体检该集绑定剧本行（不存在抛 404），角色清单取项目
      已确认角色，location 不带集号前缀；
    - episode_id 为 None：聚合体检项目下全部「已绑定剧集的剧本行」——各集分别
      体检并汇总 issues（location 带「第N集 · 」前缀）；项目无任何集级剧本行
      时（快速模式无 episodes）按既有单剧本逻辑体检默认剧本行（episode_id
      为 NULL），保持存量兼容。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        episode_id: 可选剧集 ID（None 表示聚合体检/快速模式单剧本体检）。

    Returns:
        Dict[str, Any]: {"score": 0~100, "issues": [...]}。

    Raises:
        HTTPException: 404 —— 指定剧集（或不可用时的默认剧本行）不存在。
    """
    from app.models import Episode

    confirmed = await list_confirmed_character_names(db, project.id)

    if episode_id is not None:
        script = await get_script_scoped_or_404(db, project, episode_id)
        return run_health_check(project, script, confirmed)

    # 聚合模式：查询该项目下全部集级剧本行（联表取集号用于定位前缀）
    rows = await db.execute(
        select(Script, Episode)
        .join(Episode, Script.episode_id == Episode.id)
        .where(Script.project_id == project.id)
        .order_by(Episode.sequence.asc(), Episode.id.asc())
    )
    pairs = rows.all()
    if pairs:
        issues: List[Dict[str, Any]] = []
        for script, episode in pairs:
            report = run_health_check(
                project, script, confirmed, location_prefix=f"第{episode.sequence}集 · ")
            issues.extend(report["issues"])
        return _build_report(issues)

    # 快速模式兜底：无集级剧本行时按既有单剧本逻辑体检默认剧本行
    script = await get_script_scoped_or_404(db, project, None)
    return run_health_check(project, script, confirmed)


def _normalize_entities(data: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """把 LLM 抽取结果规整为统一实体结构（字段容错，内部函数）。

    P0-2 键名漂移容错：LLM 可能输出 locations/items/场景 等别名键，或把三组
    包裹在 entities 嵌套层内；此处先探测嵌套层，再按别名表归并取列表，
    避免漂移键被静默清空导致场景/道具恒为 0。

    Args:
        data: LLM 输出解析后的 dict。

    Returns:
        Dict[str, List[Dict[str, Any]]]: {"characters": [...], "scenes": [...], "props": [...]}。
    """
    # 嵌套层探测：LLM 偶发输出 {"entities": {...三组...}} 包裹结构
    for wrapper in ("entities", "result", "data"):
        inner = data.get(wrapper)
        if isinstance(inner, dict) and any(
                k in inner for aliases in _ENTITY_GROUP_ALIASES.values() for k in aliases):
            data = inner
            break

    normalized: Dict[str, List[Dict[str, Any]]] = {}
    for group in ("characters", "scenes", "props"):
        items: List[Dict[str, Any]] = []
        # 别名归并：按别名表顺序取第一个命中的列表键（精确键优先）
        raw_items: Any = None
        for key in _ENTITY_GROUP_ALIASES[group]:
            candidate = data.get(key)
            if isinstance(candidate, list):
                raw_items = candidate
                break
        if isinstance(raw_items, list):
            for it in raw_items:
                if not isinstance(it, dict) or not str(it.get("name") or "").strip():
                    continue  # 无名条目直接丢弃
                try:
                    confidence = float(it.get("confidence", 0.8))
                except (TypeError, ValueError):
                    confidence = 0.8
                items.append({
                    "name": str(it["name"]).strip()[:50],
                    "aliases": [str(a) for a in (it.get("aliases") or []) if str(a).strip()],
                    "description": str(it.get("description") or "")[:500],
                    "confidence": round(min(max(confidence, 0.0), 1.0), 2),
                })
        normalized[group] = items
    return normalized


def rule_extract(raw_text: str) -> Dict[str, Any]:
    """规则路实体抽取（LLM 不可用时的兜底 / 候选补充）。

    规则（中文注释）：
    1. 对白冒号前角色名收集：匹配行首 "角色名：台词" 模式，收集角色名；
    2. 出现频次 >= 2 的 2-3 字中文词作为道具候选（过滤停用词与角色名重叠项）；
    3. 场景抽取（P0-2，原规则路硬编码 scenes=[] 导致降级后场景恒为 0）：
       3a. 标记行正则匹配「内景/外景XXX」「场景：XXX」「第X场 XXX」；
       3b. 场景后缀词表兜底（命中 >= 2 次取 Top8，confidence=0.4 自动带
           low_confidence 标记），与角色名/已抽道具重叠的丢弃。

    Args:
        raw_text: 剧本原始文本。

    Returns:
        Dict[str, Any]: {"characters": [...], "scenes": [...], "props": [...]}。
    """
    # 规则 1：对白冒号前角色名
    name_counter = Counter(_DIALOGUE_NAME_RE.findall(raw_text or ""))
    characters = [
        {
            "name": name,
            "aliases": [],
            "description": "（规则抽取：对白中出现，无描述）",
            "confidence": 0.5,
        }
        for name, _cnt in name_counter.most_common(20)
    ]
    char_names = {c["name"] for c in characters}

    # 规则 3：场景抽取（先标记行，后缀词表兜底）
    scenes: List[Dict[str, Any]] = []
    seen_scenes: set = set()
    text = raw_text or ""

    # 3a. 标记行匹配（confidence=0.6，格式显式可信度较高）
    for match in _SCENE_MARK_RE.finditer(text):
        name = (match.group(1) or match.group(2) or "").strip()
        if not name or name in seen_scenes or name in char_names or len(scenes) >= 12:
            continue
        seen_scenes.add(name)
        scenes.append({
            "name": name[:50],
            "aliases": [],
            "description": "（规则抽取：场次标记行命中）",
            "confidence": 0.6,
        })

    # 3b. 后缀词表兜底（无标记行格式时生效；标记行已抽足 12 个则跳过）
    if len(scenes) < 12:
        suffix_counter: Counter = Counter()
        for suffix in _SCENE_SUFFIXES:
            hits = text.count(suffix)
            if hits >= 2:
                suffix_counter[suffix] = hits
        for suffix, cnt in suffix_counter.most_common(8):
            if len(scenes) >= 12:
                break
            # 后缀命中名即场景名（如「咖啡馆」），与角色名互斥防误报
            if suffix in seen_scenes or any(suffix in nm or nm in suffix for nm in char_names):
                continue
            seen_scenes.add(suffix)
            scenes.append({
                "name": suffix,
                "aliases": [],
                "description": f"（规则抽取：场景后缀词命中 {cnt} 次）",
                "confidence": 0.4,
            })

    # 规则 2：2-3 字中文片段频次统计
    seg_counter: Counter = Counter()
    for i in range(max(0, len(text) - 1)):
        for n in (2, 3):
            seg = text[i:i + n]
            if len(seg) == n and all("\u4e00" <= ch <= "\u9fff" for ch in seg):
                seg_counter[seg] += 1

    props: List[Dict[str, Any]] = []
    scene_names = {s["name"] for s in scenes}
    for seg, cnt in seg_counter.most_common(80):
        if cnt < 2 or len(props) >= 10:
            break
        # 过滤：停用词、与角色名/场景名互相包含（避免把角色名/场景名拆词当道具）
        if seg in _STOPWORDS:
            continue
        if any(seg in nm or nm in seg for nm in char_names):
            continue
        if any(seg in nm or nm in seg for nm in scene_names):
            continue
        props.append({
            "name": seg,
            "aliases": [],
            "description": f"（规则候选：文本中出现 {cnt} 次）",
            "confidence": 0.4,
        })
    return {"characters": characters, "scenes": scenes, "props": props}


async def llm_extract(
    raw_text: str, cast_names: Optional[List[str]] = None
) -> Optional[Dict[str, Any]]:
    """LLM 路实体抽取（失败返回 None，由调用方降级到规则结果）。

    按集抽取工作流：cast_names 传值（该集分集大纲出场角色清单）时，在用户
    消息头部注入硬性白名单约束——characters 组仅允许输出清单内角色，清单外
    角色即使正文出现也禁止输出（程序级过滤见 _filter_characters_by_cast，
    双保险防 LLM 不遵从约束）。约束注入走 user 消息文本，不改动 llm.chat
    签名，测试替身与既有调用侧不受影响。

    Args:
        raw_text: 剧本原始文本。
        cast_names: 出场角色白名单（None/空=不启用白名单约束，全项目抽取路径）。

    Returns:
        Optional[Dict[str, Any]]: 成功返回规整后的实体 dict；LLM 异常或解析失败返回 None。

    Raises:
        无：内部捕获一切异常（含网络/鉴权/限流），保证降级路径可达。
    """
    try:
        user_content = raw_text or ""
        if cast_names:
            constraint = (
                "【硬性约束】characters 只允许输出以下出场角色清单中的角色，"
                "清单之外的角色（即使正文出现）一律不得输出；"
                "正文中的称呼若为清单角色的简称/别名，请归一化为清单中的名字：\n"
                f"出场角色清单：{'、'.join(cast_names)}\n\n"
            )
            user_content = constraint + user_content[:12000]
        else:
            user_content = user_content[:12000]
        text = await llm.chat(
            None,
            [
                {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            agent_name="script_extract",
            json_mode=True,
            temperature=0.2,
        )
        data = json.loads(strip_json_fences(text))
        if not isinstance(data, dict):
            logger.warning("LLM 实体抽取输出非 dict（type=%s），降级规则路", type(data).__name__)
            return None
        result = _normalize_entities(data)
        # P0-2：LLM 成功但三组全空时告警（区分"剧本确实无实体"与"键名漂移被清空"）
        if not any(result.values()):
            logger.warning("LLM 实体抽取三组全空（keys=%s），请检查输出键名是否漂移",
                           list(data.keys()))
        return result
    except Exception:  # noqa: BLE001 —— LLM 抽取失败必须走规则降级
        logger.warning("LLM 实体抽取异常降级规则路", exc_info=True)
        return None


def _match_cast_name(name: str, cast_names: List[str]) -> Optional[str]:
    """将抽取出的角色名匹配到出场角色白名单（别名/简称归一化）。

    匹配顺序：精确相等优先，其次双向子串包含（抽取名「小满」↔ 清单名
    「王小满」视为同一角色）。仅用于按集抽取的白名单过滤，不做拼音/模糊匹配，
    避免误归并。

    Args:
        name: 抽取出的角色名（已 strip）。
        cast_names: 分集大纲出场角色清单（已 strip 非空）。

    Returns:
        Optional[str]: 匹配到的清单规范名；无匹配返回 None。

    Raises:
        无：纯字符串处理。
    """
    if name in cast_names:
        return name
    for cn in cast_names:
        if name in cn or cn in name:
            return cn
    return None


def _filter_characters_by_cast(
    result: Dict[str, Any], cast_names: List[str]
) -> Dict[str, Any]:
    """按集抽取的角色白名单过滤（原位修改 result["characters"]）。

    按集工作流硬约束：角色抽取结果必须与该集分集大纲出场角色清单一致——
    1. 白名单过滤：清单外角色一律丢弃（LLM 提示词约束失效时的程序级兜底）；
    2. 别名归一化：抽取名匹配清单名（双向子串）后改写为清单规范名，同名
       条目合并（description 取更长、aliases 并集）；
    3. 清单兜底：清单中未被正文抽取命中的角色仍须出现（大纲声明了本集
       出场即视为有效候选），追加低置信度条目供用户在抽取页确认或剔除。

    Args:
        result: 抽取结果 dict（含 characters 键）。
        cast_names: 出场角色白名单（非空，调用方保证）。

    Returns:
        Dict[str, Any]: 原 result 对象（characters 已被过滤/归一化/补齐）。

    Raises:
        无：纯列表处理。
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for item in result.get("characters", []):
        cast_name = _match_cast_name(str(item.get("name") or ""), cast_names)
        if cast_name is None:
            continue  # 白名单外角色丢弃
        if cast_name in merged:
            old = merged[cast_name]
            if len(str(item.get("description") or "")) > len(str(old.get("description") or "")):
                old["description"] = item.get("description")
            old["aliases"] = list(dict.fromkeys(
                [*(old.get("aliases") or []), *(item.get("aliases") or [])]))
        else:
            merged[cast_name] = {
                "name": cast_name,
                "aliases": list(item.get("aliases") or []),
                "description": str(item.get("description") or ""),
                "confidence": item.get("confidence", 0.8),
            }
    # 清单兜底：大纲声明出场但正文未命中的角色，低置信度条目补齐
    for cn in cast_names:
        if cn not in merged:
            merged[cn] = {
                "name": cn,
                "aliases": [],
                "description": "分集大纲出场角色（正文抽取未命中，请核对设定）",
                "confidence": 0.6,
            }
    # 按大纲清单顺序输出
    result["characters"] = [merged[cn] for cn in cast_names if cn in merged]
    return result


def detect_alias_conflicts(names: List[str]) -> List[Dict[str, Any]]:
    """别名冲突检测（RD-06）：角色名互相包含（如"小明/明明"）判定为疑似别名冲突。

    Args:
        names: 角色名列表。

    Returns:
        List[Dict[str, Any]]: [{"type": "alias", "candidates": [a, b], "suggestion": 规范名}]，
            suggestion 取更长的名字作为建议保留的规范名。
    """
    conflicts: List[Dict[str, Any]] = []
    unique_names = list(dict.fromkeys(names))  # 去重保序
    for i, a in enumerate(unique_names):
        for b in unique_names[i + 1:]:
            if a != b and (a in b or b in a):
                conflicts.append({
                    "type": "alias",
                    "candidates": [a, b],
                    # 建议保留更完整的名字（更长者）
                    "suggestion": a if len(a) >= len(b) else b,
                })
    return conflicts


async def build_project_extract_source(
    db: AsyncSession,
    project: Project,
    default_script: Script,
    scope_episode_id: Optional[int] = None,
) -> str:
    """聚合项目剧本行作为实体抽取源文本（P0-2 补强 + 按集抽取范围）。

    背景：/script/extract 原只读「默认剧本行」（episode_id NULL），而多集
    流水线项目真正的场次结构在集级剧本行（每集一行的 content.scenes）里，
    导致此类项目实体抽取永远抽不出场景/道具。聚合规则：
    - scope_episode_id 传值（按集抽取）：只取该集剧本行的源文本（逐集生成
      逐集入池的按集工作流）；
    - 默认行 novel 模式且已解析章节：章节上下文（章节摘要 + 正文节选）即
      全文，直接返回不再聚合；
    - 其余情况：逐行取 build_extract_source_text（结构化剧本优先、raw_text
      兜底，完全空白的行跳过），行间以空行拼接。场景标记行与「角色名：台词」
      对白格式原样保留，规则路抽取依赖该格式。

    Args:
        db: 请求级异步数据库会话。
        project: 已校验归属的项目。
        default_script: 默认剧本行（get_script_or_404 的定位结果，novel
            分支判定依据）。
        scope_episode_id: 抽取范围限定剧集 ID（None=全项目聚合，按集工作流传值）。

    Returns:
        str: 聚合后的抽取源文本（非空）。

    Raises:
        HTTPException: 400 —— 范围内剧本行均无任何内容。
    """
    from app.models import Episode  # 局部导入避免与既有模块级导入顺序耦合

    chapters = (project.config or {}).get("novel_chapters")
    input_mode = str(content_plain(default_script.content).get("input_mode") or "")
    if scope_episode_id is None and input_mode == "novel" and isinstance(chapters, list) and chapters:
        # novel 章节上下文即全文（含各章摘要与正文节选），无需再聚合其他行
        return build_novel_context(project, fallback_text=default_script.raw_text)

    parts: List[str] = []
    conds = [Script.project_id == project.id]
    if scope_episode_id is not None:
        conds.append(Script.episode_id == scope_episode_id)
        # 按集抽取源头部：该集分集大纲（出场角色 + 核心事件）——角色候选优先
        # 来自大纲出场角色清单，正文段落提供场景/道具/对白线索
        ep = await db.get(Episode, scope_episode_id)
        if ep is not None:
            outline = ep.outline if isinstance(ep.outline, dict) else {}
            head_lines: List[str] = []
            if str(ep.title or "").strip():
                head_lines.append(f"第{ep.sequence}集《{str(ep.title).strip()}》")
            core_event = str(outline.get("core_event") or "").strip()
            if core_event:
                head_lines.append(f"核心事件：{core_event}")
            raw_cast = outline.get("characters")
            cast_names = (
                [str(c).strip() for c in raw_cast if str(c or "").strip()]
                if isinstance(raw_cast, list) else []
            )
            if cast_names:
                head_lines.append("出场角色：" + "、".join(cast_names))
            if head_lines:
                parts.append("\n".join(head_lines))
    rows = (
        await db.execute(select(Script).where(*conds).order_by(Script.id.asc()))
    ).scalars().all()
    for row in rows:
        try:
            text = build_extract_source_text(row)
        except HTTPException:
            # 该行完全无内容（既无结构化剧本也无 raw_text），跳过
            continue
        if text.strip():
            parts.append(text.strip())
    merged = "\n\n".join(parts).strip()
    if not merged:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="剧本尚无内容，请先生成或保存剧本")
    return merged


async def extract_entities(
    db: AsyncSession, project: Project, script: Script,
    scope_episode_id: Optional[int] = None,
) -> Dict[str, Any]:
    """实体抽取主流程：规则 + LLM 双路，结果落 scripts.fences 并返回。

    P0 断链修复：抽取源由 script.raw_text（用户原始输入，一句话模式下仅一句
    创意）改判为聚合项目全部剧本行（build_project_extract_source）——单行项目
    等价于 build_extract_source_text(script)，多集流水线项目集级剧本行
    （含场次结构）一并参与抽取。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        script: 剧本对象。

    Returns:
        Dict[str, Any]: {"characters","scenes","props","conflicts"}，
            每条实体含 confidence 与 low_confidence 标记。

    Raises:
        HTTPException: 400 —— 项目全部剧本行均无内容。
    """
    # 抽取源文本：聚合项目剧本行（P0-2 补强：/script/extract 原只读默认
    # 剧本行，多集流水线项目的集级剧本行（含场次结构）不参与抽取，导致场景/
    # 道具抽不出）。scope_episode_id 传值时限定为该集（按集工作流）。
    # novel 模式且已解析章节时章节上下文即全文，直接使用。
    source_text = await build_project_extract_source(
        db, project, script, scope_episode_id=scope_episode_id)

    # 按集工作流：读取该集分集大纲出场角色清单作为角色白名单（None=全项目
    # 抽取不过滤）。大纲声明了本集出场角色，正文里出现的清单外角色（如其他
    # 集的主角客串提及）不属于本集资产，程序级强制过滤保证按集语义。
    cast_names: List[str] = []
    if scope_episode_id is not None:
        from app.models import Episode  # 局部导入，与 build_project_extract_source 一致
        ep = await db.get(Episode, scope_episode_id)
        raw_cast = ep.outline.get("characters") if ep is not None and isinstance(ep.outline, dict) else None
        if isinstance(raw_cast, list):
            cast_names = [str(c).strip() for c in raw_cast if str(c or "").strip()]

    # 双路抽取：LLM 优先，失败降级规则结果（P0-2：记录实际使用路径供前端统计展示）。
    # cast_names 非空时 LLM 提示词注入白名单硬约束 + 结果程序级过滤双保险。
    # 注意：cast_names 仅在按集路径条件传参，默认路径保持旧签名（测试替身兼容）
    if cast_names:
        result = await llm_extract(source_text, cast_names=cast_names)
    else:
        result = await llm_extract(source_text)
    source = "llm"
    if result is None:
        result = rule_extract(source_text)
        source = "rule"
    if cast_names:
        result = _filter_characters_by_cast(result, cast_names)

    # P0-2 统计反馈：抽取结果计数与来源（前端展示"解析出角色X/场景Y/道具Z"，
    # 消除场景/道具为 0 时的静默失败）
    result["stats"] = {
        "characters": len(result.get("characters", [])),
        "scenes": len(result.get("scenes", [])),
        "props": len(result.get("props", [])),
        "source": source,
        "source_text_chars": len(source_text),
    }

    # 低置信度标记（RD-05）：confidence < 0.7 → low_confidence=True
    for group in ("characters", "scenes", "props"):
        for item in result.get(group, []):
            item["low_confidence"] = float(item.get("confidence", 1.0)) < LOW_CONFIDENCE_THRESHOLD

    # 别名冲突检测（RD-06）
    conflicts = detect_alias_conflicts([c["name"] for c in result.get("characters", [])])
    result["conflicts"] = conflicts

    # 落库 scripts.fences（RD-05/RD-06 记录位）
    script.fences = result
    await db.commit()
    return result


async def serialize_project_assets(db: AsyncSession, project_id: int) -> Dict[str, Any]:
    """序列化项目当前有效资产状态（V-01 版本回退的最小状态载体）。

    仅序列化业务可变字段（name/aliases/description/visual_prompt/category），
    不含生成态字段（feature_vector/asset_url/face_locked 等）——回退语义是
    「恢复抽取确认时刻的实体名单与描述」，不回滚生成产物。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。

    Returns:
        Dict[str, Any]: 形如 {"entity": "assets", "characters": [...],
        "scenes": [...], "props": [...]} 的可 JSONB 序列化状态字典，
        entity 字段标识恢复目标域（回退 API 据此分派恢复逻辑）。

    Raises:
        Exception: 数据库查询异常向上抛出。
    """
    chars = (
        await db.execute(
            select(Character).where(
                Character.project_id == project_id, Character.deleted_at.is_(None)
            ).order_by(Character.id)
        )
    ).scalars().all()
    scenes = (
        await db.execute(
            select(Scene).where(
                Scene.project_id == project_id, Scene.deleted_at.is_(None)
            ).order_by(Scene.id)
        )
    ).scalars().all()
    props = (
        await db.execute(
            select(Prop).where(
                Prop.project_id == project_id, Prop.deleted_at.is_(None)
            ).order_by(Prop.id)
        )
    ).scalars().all()
    return {
        "entity": "assets",
        "characters": [
            {"name": c.name, "aliases": list(c.aliases or []), "description": c.description,
             "visual_prompt": c.visual_prompt}
            for c in chars
        ],
        "scenes": [
            {"name": s.name, "description": s.description, "visual_prompt": s.visual_prompt}
            for s in scenes
        ],
        "props": [
            {"name": p.name, "category": p.category, "description": p.description,
             "visual_prompt": p.visual_prompt}
            for p in props
        ],
    }


async def confirm_extract(db: AsyncSession, project: Project, req: Any) -> Dict[str, int]:
    """抽取确认入池：按确认清单建/更 characters / scenes / props 资产行。

    - visual_prompt 缺省使用中文模板「统一风格插画，{name}，{description}，全身立绘，纯色背景，高细节」；
    - 同名资产已存在时更新描述/别名（幂等确认，不重复建行）；visual_prompt
      已存在非空时保留（跨集一致性：同一资产全项目仅一份视觉设定）；
    - 命中已软删的同名资产时复活（deleted_at 置空）；
    - req.episode_id 传值时把确认的资产名 union 追加到该集 enabled_assets
      启用名单（资产管理页按集视图仅显示名单内资产）；
    - project.status 置 'asset'，写 version_snapshots(stage='extract_confirmed')。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        req: ExtractConfirmRequest 请求对象（episode_id 可空=不记按集名单）。

    Returns:
        Dict[str, int]: 项目内各类未删资产现有总数 {"characters","scenes","props"}。
    """
    # ---------- 角色 ----------
    seen: set = set()
    for item in req.characters:
        if item.name in seen:
            continue  # 请求内同名去重，避免撞 (project_id, name) 唯一索引
        seen.add(item.name)
        visual_prompt = item.visual_prompt or DEFAULT_VISUAL_TEMPLATE.format(
            name=item.name, description=item.description or item.name)
        # exist 查询不过滤 deleted_at：uk_characters_name 为全量唯一索引，
        # 已删同名角色必须走"复活"路径（恢复 deleted_at + 更新字段），否则
        # 新建会撞唯一约束；确认入池语义 = 该实体应在池中
        exist = (
            await db.execute(
                select(Character).where(
                    Character.project_id == project.id,
                    Character.name == item.name,
                )
            )
        ).scalar_one_or_none()
        if exist is not None:
            # 幂等确认：已存在则更新可变字段，保留既有 visual_prompt（若未显式提供）；
            # 启用资产语义：无论来源（角色设定 script_planner / 抽取），确认入池即启用
            exist.description = item.description or exist.description
            exist.aliases = item.aliases or exist.aliases
            # 跨集一致性：同名资产全项目仅一行一视觉，visual_prompt 已存在非空时
            # 不被新集抽取模板覆盖（防止每集重复确认导致定妆照视觉漂移；
            # 改视觉应走资产页编辑/重新生成），仅缺省时才填充
            if not (exist.visual_prompt or "").strip():
                exist.visual_prompt = item.visual_prompt
            exist.enabled_at = datetime.now(timezone.utc)
            if exist.deleted_at is not None:
                exist.deleted_at = None  # 已删同名角色复活
        else:
            db.add(Character(
                project_id=project.id,
                name=item.name,
                aliases=list(item.aliases or []),
                description=item.description or None,
                visual_prompt=visual_prompt,
                enabled_at=datetime.now(timezone.utc),
            ))

    # ---------- 场景 ----------
    scene_seen: set = set()
    for item in req.scenes:
        if item.name in scene_seen:
            continue
        scene_seen.add(item.name)
        visual_prompt = item.visual_prompt or DEFAULT_VISUAL_TEMPLATE.format(
            name=item.name, description=item.description or item.name)
        # exist 查询不过滤 deleted_at（uk_scenes_name 全量唯一索引）：命中已删行
        # 必须恢复 deleted_at，否则用户删除场景后再确认入池永远"回不来"
        exist = (
            await db.execute(
                select(Scene).where(Scene.project_id == project.id, Scene.name == item.name)
            )
        ).scalar_one_or_none()
        if exist is not None:
            exist.description = item.description or exist.description
            # 跨集一致性：visual_prompt 已存在非空时不被新集模板覆盖（同角色）
            if not (exist.visual_prompt or "").strip():
                exist.visual_prompt = item.visual_prompt
            if exist.deleted_at is not None:
                exist.deleted_at = None  # 已删同名场景复活
        else:
            db.add(Scene(
                project_id=project.id,
                name=item.name,
                description=item.description or None,
                visual_prompt=visual_prompt,
            ))

    # ---------- 道具 ----------
    prop_seen: set = set()
    for item in req.props:
        if item.name in prop_seen:
            continue
        prop_seen.add(item.name)
        visual_prompt = item.visual_prompt or DEFAULT_VISUAL_TEMPLATE.format(
            name=item.name, description=item.description or item.name)
        # exist 查询不过滤 deleted_at（与角色/场景一致）：命中已删行恢复 deleted_at，
        # 保证"删除后重新确认入池"行为三类资产一致
        exist = (
            await db.execute(
                select(Prop).where(
                    Prop.project_id == project.id,
                    Prop.name == item.name,
                )
            )
        ).scalar_one_or_none()
        if exist is not None:
            exist.description = item.description or exist.description
            # 跨集一致性：visual_prompt 已存在非空时不被新集模板覆盖（同角色）
            if not (exist.visual_prompt or "").strip():
                exist.visual_prompt = item.visual_prompt
            if exist.deleted_at is not None:
                exist.deleted_at = None  # 已删同名道具复活
        else:
            db.add(Prop(
                project_id=project.id,
                name=item.name,
                description=item.description or None,
                visual_prompt=visual_prompt,
            ))

    # ---------- 默认风格自动落池（风格不从剧本抽取，取项目创建时选定的画风） ----------
    has_style = (
        await db.execute(
            select(func.count()).select_from(Style).where(
                Style.project_id == project.id, Style.deleted_at.is_(None))
        )
    ).scalar_one_or_none() or 0
    if not has_style:
        style_name = (project.style or "").strip() or "默认画风"
        db.add(Style(
            project_id=project.id,
            name=style_name,
            # 风格资产提示词模板：全局画风锚点（生成时作为风格层注入）
            visual_prompt=f"统一{style_name}画风，高质量插画，色彩协调，细节丰富，画面干净",
            is_default=True,  # 项目内无其他风格行，直接置默认（部分唯一索引不冲突）
        ))

    # ---------- 按集启用名单（按集工作流）：确认入池的资产名 union 进该集 enabled_assets ----------
    # 资产管理页按集视图语义：仅显示该集名单内的资产，未启用过的集四类显示为空。
    # 同名资产跨集重复确认时名单 union 去重（资产行本身幂等复用，保证一致性）。
    if req.episode_id is not None:
        from app.models import Episode  # 局部导入，与抽取源构造一致
        ep = await db.get(Episode, req.episode_id)
        if ep is not None and ep.project_id == project.id:
            style_rows = (
                await db.execute(
                    select(Style).where(
                        Style.project_id == project.id, Style.deleted_at.is_(None))
                )
            ).scalars().all()
            # 风格不抽取：确认后项目必有默认风格（上方自动创建兜底），全部计入名单
            enabled = dict(ep.enabled_assets or {})
            for key, values in {
                "characters": [i.name for i in req.characters],
                "scenes": [i.name for i in req.scenes],
                "props": [i.name for i in req.props],
                "styles": [s.name for s in style_rows],
            }.items():
                enabled[key] = list(dict.fromkeys([*(enabled.get(key) or []), *values]))
            ep.enabled_assets = enabled

    # 先 flush 使本轮新增/更新的资产行在当前事务内可见，再序列化实体状态
    await db.flush()
    assets_state = await serialize_project_assets(db, project.id)

    # 项目状态推进 + 版本快照（V-01 / RD-08；impact_scope 记录实体完整状态，供版本回退恢复）
    project.status = "asset"
    db.add(VersionSnapshot(
        project_id=project.id,
        stage="extract_confirmed",
        summary={"characters": len(req.characters), "scenes": len(req.scenes), "props": len(req.props)},
        impact_scope=assets_state,
    ))
    await db.commit()

    # 返回项目内各类未删资产现有总数
    n_characters = await db.scalar(
        select(func.count()).select_from(Character).where(
            Character.project_id == project.id, Character.deleted_at.is_(None)))
    n_scenes = await db.scalar(
        select(func.count()).select_from(Scene).where(Scene.project_id == project.id))
    n_props = await db.scalar(
        select(func.count()).select_from(Prop).where(
            Prop.project_id == project.id, Prop.deleted_at.is_(None)))
    return {"characters": n_characters or 0, "scenes": n_scenes or 0, "props": n_props or 0}
