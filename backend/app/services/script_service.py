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
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm_adapter import llm
from app.models import Character, Project, Prop, Scene, Script, VersionSnapshot
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
    """从 characters.description 人设摘要中提取「外观：」段（外貌锁定用）。

    characters.description 由 build_profile_description 以「性格：…；外观：…；
    动机：…；弧光：…」格式拼接，本函数按全角分号分段取「外观：」后的内容，
    供分镜锁定层/角色圣经在不新增列的前提下拿到纯视觉描述。

    Args:
        description: 角色人设摘要（可空；抽取/手工建角色的旧数据可能无该段）。

    Returns:
        Optional[str]: 外观描述文本（无「外观：」段或为空时返回 None）。

    Raises:
        无：纯字符串处理。
    """
    if not description:
        return None
    for seg in str(description).split("；"):
        seg = seg.strip()
        if seg.startswith("外观："):
            return seg[len("外观："):].strip() or None
    return None

# 低置信度阈值：抽取条目 confidence 低于该值时标记 low_confidence（前端标黄）
LOW_CONFIDENCE_THRESHOLD = 0.7

# 默认目标时长（秒，P-1 Task 8 时长口径统一）：项目未显式设置 target_duration
# 时的缺省口径，与竖屏漫剧短剧体量一致（原硬编码 60 秒与「3~6 场 × 30s」不自洽，
# 统一提升为 180 秒；不改 PRD 允许范围 1~3600s，仅改缺省取值）。
DEFAULT_TARGET_DURATION = 180

# 规则路：对白行"角色名：台词"的角色名提取（行首 1~10 位中文/字母/数字，后接冒号）
_DIALOGUE_NAME_RE = re.compile(r"^\s*([\u4e00-\u9fa5A-Za-z0-9·]{1,10})\s*[：:]\s*\S+", re.M)

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
    # 场景：每场标题 + 剧情描述 + 对白行（"角色名：台词" 逐行格式）
    for sc in (plain.get("scenes") or []):
        if not isinstance(sc, dict):
            continue
        scene_no = sc.get("scene_no", "")
        lines.append(f"场景{scene_no}《{sc.get('title', '')}》")
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

    Args:
        scene: 场次 dict。
        index: 场次在 scenes 数组中的下标（0 起，scene_no 缺失时兜底）。

    Returns:
        str: 形如「场景3」的定位标签。

    Raises:
        无：字段缺失按下标兜底。
    """
    return f"场景{scene.get('scene_no', index + 1)}"


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

    Args:
        data: LLM 输出解析后的 dict。

    Returns:
        Dict[str, List[Dict[str, Any]]]: {"characters": [...], "scenes": [...], "props": [...]}。
    """
    normalized: Dict[str, List[Dict[str, Any]]] = {}
    for group in ("characters", "scenes", "props"):
        items: List[Dict[str, Any]] = []
        raw_items = data.get(group)
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
    2. 出现频次 >= 2 的 2-3 字中文词作为道具候选（过滤停用词与角色名重叠项）。

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

    # 规则 2：2-3 字中文片段频次统计
    seg_counter: Counter = Counter()
    for i in range(max(0, len(raw_text) - 1)):
        for n in (2, 3):
            seg = raw_text[i:i + n]
            if len(seg) == n and all("\u4e00" <= ch <= "\u9fff" for ch in seg):
                seg_counter[seg] += 1

    props: List[Dict[str, Any]] = []
    for seg, cnt in seg_counter.most_common(80):
        if cnt < 2 or len(props) >= 10:
            break
        # 过滤：停用词、与角色名互相包含（避免把角色名拆词当道具）
        if seg in _STOPWORDS:
            continue
        if any(seg in nm or nm in seg for nm in char_names):
            continue
        props.append({
            "name": seg,
            "aliases": [],
            "description": f"（规则候选：文本中出现 {cnt} 次）",
            "confidence": 0.4,
        })
    return {"characters": characters, "scenes": [], "props": props}


async def llm_extract(raw_text: str) -> Optional[Dict[str, Any]]:
    """LLM 路实体抽取（失败返回 None，由调用方降级到规则结果）。

    Args:
        raw_text: 剧本原始文本。

    Returns:
        Optional[Dict[str, Any]]: 成功返回规整后的实体 dict；LLM 异常或解析失败返回 None。

    Raises:
        无：内部捕获一切异常（含网络/鉴权/限流），保证降级路径可达。
    """
    try:
        text = await llm.chat(
            None,
            [
                {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": (raw_text or "")[:12000]},
            ],
            agent_name="script_extract",
            json_mode=True,
            temperature=0.2,
        )
        data = json.loads(strip_json_fences(text))
        if not isinstance(data, dict):
            return None
        return _normalize_entities(data)
    except Exception:  # noqa: BLE001 —— LLM 抽取失败必须走规则降级
        return None


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


async def extract_entities(db: AsyncSession, project: Project, script: Script) -> Dict[str, Any]:
    """实体抽取主流程：规则 + LLM 双路，结果落 scripts.fences 并返回。

    P0 断链修复：抽取源由 script.raw_text（用户原始输入，一句话模式下仅一句
    创意）改判为 build_extract_source_text(script)——优先使用生成的完整剧本
    content（scenes 对白），仅在无结构化 payload 时兜底 raw_text。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        script: 剧本对象。

    Returns:
        Dict[str, Any]: {"characters","scenes","props","conflicts"}，
            每条实体含 confidence 与 low_confidence 标记。

    Raises:
        HTTPException: 400 —— 剧本尚无内容（既无结构化剧本也无原始输入）。
    """
    # 抽取源文本：novel 模式且已解析章节时用「章节摘要 + 正文节选」拼接
    # 上下文（P-7 长文截断消除）；否则沿用 build_extract_source_text 现有取源
    #（优先生成后的完整剧本 content，无结构化 payload 时兜底 raw_text）。
    chapters = (project.config or {}).get("novel_chapters")
    input_mode = str(content_plain(script.content).get("input_mode") or "")
    if input_mode == "novel" and isinstance(chapters, list) and chapters:
        source_text = build_novel_context(project, fallback_text=script.raw_text)
    else:
        source_text = build_extract_source_text(script)

    # 双路抽取：LLM 优先，失败降级规则结果
    result = await llm_extract(source_text)
    if result is None:
        result = rule_extract(source_text)

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
    - 同名资产已存在时更新描述/别名/提示词（幂等确认，不重复建行）；
    - project.status 置 'asset'，写 version_snapshots(stage='extract_confirmed')。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        req: ExtractConfirmRequest 请求对象。

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
        exist = (
            await db.execute(
                select(Character).where(
                    Character.project_id == project.id,
                    Character.name == item.name,
                    Character.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if exist is not None:
            # 幂等确认：已存在则更新可变字段，保留既有 visual_prompt（若未显式提供）
            exist.description = item.description or exist.description
            exist.aliases = item.aliases or exist.aliases
            exist.visual_prompt = item.visual_prompt or exist.visual_prompt
        else:
            db.add(Character(
                project_id=project.id,
                name=item.name,
                aliases=list(item.aliases or []),
                description=item.description or None,
                visual_prompt=visual_prompt,
            ))

    # ---------- 场景 ----------
    scene_seen: set = set()
    for item in req.scenes:
        if item.name in scene_seen:
            continue
        scene_seen.add(item.name)
        visual_prompt = item.visual_prompt or DEFAULT_VISUAL_TEMPLATE.format(
            name=item.name, description=item.description or item.name)
        exist = (
            await db.execute(
                select(Scene).where(Scene.project_id == project.id, Scene.name == item.name)
            )
        ).scalar_one_or_none()
        if exist is not None:
            exist.description = item.description or exist.description
            exist.visual_prompt = item.visual_prompt or exist.visual_prompt
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
        exist = (
            await db.execute(
                select(Prop).where(
                    Prop.project_id == project.id,
                    Prop.name == item.name,
                    Prop.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if exist is not None:
            exist.description = item.description or exist.description
            exist.visual_prompt = item.visual_prompt or exist.visual_prompt
        else:
            db.add(Prop(
                project_id=project.id,
                name=item.name,
                description=item.description or None,
                visual_prompt=visual_prompt,
            ))

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
