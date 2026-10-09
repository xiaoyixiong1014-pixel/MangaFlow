"""剧本域业务服务模块（文件级说明）。

承载剧本工作流的业务逻辑：
- 项目归属校验（统一 404，不泄露他人项目存在性）
- 剧本行获取 / 输入保存
- LLM 流式生成提示词构造、剧本 JSON 解析容错、离线降级占位剧本
- RD-03 字段级来源标记包装（每个一级字段 {"value":..., "source":"ai"|"user"}）
- 规则体检（RD-02）
- 实体抽取（规则 + LLM 双路）、别名冲突检测（RD-05 / RD-06）
- 抽取确认入池（建 characters / scenes / props 资产行）

路由层（app/api/script.py）只做参数校验与编排，事务细节统一收敛在本模块。
"""

import json
import re
from collections import Counter
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Character, Project, Prop, Scene, Script, VersionSnapshot
from app.providers.llm import chat

# visual_prompt 缺省中文模板（抽取确认 / 手工建角色共用）
DEFAULT_VISUAL_TEMPLATE = "统一风格插画，{name}，{description}，全身立绘，纯色背景，高细节"

# 低置信度阈值：抽取条目 confidence 低于该值时标记 low_confidence（前端标黄）
LOW_CONFIDENCE_THRESHOLD = 0.7

# 规则路：对白行"角色名：台词"的角色名提取（行首 1~10 位中文/字母/数字，后接冒号）
_DIALOGUE_NAME_RE = re.compile(r"^\s*([\u4e00-\u9fa5A-Za-z0-9·]{1,10})\s*[：:]\s*\S+", re.M)

# 规则路 2-3 字中文高频词候选的停用词（虚词/常见代词等，避免噪声实体）
_STOPWORDS = frozenset({
    "我们", "你们", "他们", "她们", "什么", "这个", "那个", "自己", "没有", "就是",
    "还是", "已经", "所以", "因为", "但是", "如果", "现在", "时候", "知道", "一个",
    "一下", "起来", "过来", "出来", "怎么", "这样", "那样", "可以", "不是", "说道",
    "的话", "你的", "我的", "他的", "她的", "他们", "这里", "那里", "出现", "最后",
})

# LLM 剧本生成系统提示词（要求仅输出严格 JSON）
_GENERATE_SYSTEM_PROMPT = (
    "你是专业的漫剧（动态漫画短剧）编剧。请根据用户输入构思剧本。\n"
    "必须仅输出一个严格合法的 JSON 对象（禁止 markdown 代码块围栏、禁止任何解释文字），"
    "结构如下：\n"
    '{"title":"剧本标题","logline":"一句话故事梗概",'
    '"outline":[{"act":"第一幕","summary":"本幕概要"}],'
    '"scenes":[{"scene_no":1,"title":"场景标题","plot":"场景剧情描述",'
    '"dialogue":[{"character":"角色名","line":"台词"}],"duration_sec":30}]}\n'
    "要求：三幕结构（outline 恰好 3 项）；scenes 为 3~6 个场景；"
    "每个场景含 1~4 条对白；duration_sec 为该场景时长（秒，正整数）；全部使用中文。"
)

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
    """获取项目最新（唯一）剧本行，不存在时抛 404。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。

    Returns:
        Script: 剧本 ORM 对象。

    Raises:
        HTTPException: 404 —— 项目尚未创建剧本。
    """
    script = (
        await db.execute(select(Script).where(Script.project_id == project.id))
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


def parse_script_json(text: str) -> Dict[str, Any]:
    """解析 LLM 输出的剧本 JSON（容错）。

    Args:
        text: LLM 输出的完整文本。

    Returns:
        Dict[str, Any]: 解析成功的 JSON dict；失败时返回 {"raw": 全文} 兜底。
    """
    try:
        data = json.loads(strip_json_fences(text))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    # 解析失败兜底：原文放入 raw 字段，保证内容不丢失
    return {"raw": text}


def build_generate_messages(project: Project, raw_text: str) -> List[Dict[str, str]]:
    """构造剧本生成的 LLM 消息（中文提示词，注入项目元信息）。

    Args:
        project: 项目对象（genre/style/fidelity/target_duration/aspect_ratio）。
        raw_text: 用户原始输入文本。

    Returns:
        List[Dict[str, str]]: OpenAI 格式 messages。
    """
    user_content = (
        f"项目体裁：{project.genre or '未指定'}；视觉风格：{project.style or '未指定'}；"
        f"忠实度：{project.fidelity or 'free'}；目标时长：{project.target_duration or 60} 秒；"
        f"画幅比例：{project.aspect_ratio}。\n"
        f"用户输入：\n{raw_text[:4000]}"
    )
    return [
        {"role": "system", "content": _GENERATE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def build_fallback_script(raw_text: str) -> Dict[str, Any]:
    """离线降级：LLM 不可用时基于 raw_text 拆句构造确定性 3 幕占位剧本。

    Args:
        raw_text: 用户原始输入文本。

    Returns:
        Dict[str, Any]: 与正常生成同构的剧本 dict（title/logline/outline/scenes）。
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
    per_duration = 10  # 离线降级每场景固定 10s
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
    """RD-03 字段级来源标记：将剧本内容每个一级字段包装为 {"value":..., "source":...}。

    Args:
        content: 剧本内容 dict（一级字段为 title/logline/outline/scenes 等）。
        source: 来源标记（"ai" 由模型生成 / "user" 由用户编辑）。

    Returns:
        Dict[str, Any]: 包装后的 content dict。
    """
    return {k: {"value": v, "source": source} for k, v in content.items()}


def unwrap_scenes(content: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从剧本 content 中取出场景数组（兼容包装/未包装两种结构）。

    Args:
        content: 剧本 content dict。

    Returns:
        List[Dict[str, Any]]: 场景列表；结构不符或缺失时返回空列表。
    """
    if not isinstance(content, dict):
        return []
    raw = content.get("scenes")
    if isinstance(raw, dict) and "value" in raw:
        raw = raw["value"]
    return raw if isinstance(raw, list) else []


def run_health_check(project: Project, script: Script) -> Dict[str, Any]:
    """剧本规则体检（RD-02）。

    体检规则（中文注释）：
    1. 剧本无任何场景数据 → error「剧本中没有任何场景数据」；
    2. 全剧未检测到任何对白 → warning「全剧未检测到任何对白」；
    3. 对白条目缺少角色名 → warning「对白缺少角色名」（定位到场景）；
    4. 各场景 duration_sec 合计与项目 target_duration 偏差 > 20%
       → error「时长偏差超标」（未设置目标时长时跳过）。

    计分：起始 100 分，每个 error 扣 25、每个 warning 扣 10，下限 0。

    Args:
        project: 项目对象（读取 target_duration）。
        script: 剧本对象（读取 content）。

    Returns:
        Dict[str, Any]: {"score": 0~100, "issues": [{"level","message","location"}]}。
    """
    scenes = unwrap_scenes(script.content)
    issues: List[Dict[str, Any]] = []
    has_dialogue = False

    if not scenes:
        issues.append({"level": "error", "message": "剧本中没有任何场景数据", "location": "content.scenes"})
    for sc in scenes:
        location = f"场景{sc.get('scene_no', sc.get('title', '?'))}"
        dialogue = sc.get("dialogue") or []
        if isinstance(dialogue, str):
            # 兼容字符串对白：按行拆为 "角色名：台词"
            dialogue = [
                {"character": parts[0] if len(parts := line.split("：", 1)) > 1 else "",
                 "line": parts[-1]}
                for line in dialogue.splitlines() if line.strip()
            ]
        for d in dialogue:
            if not isinstance(d, dict):
                continue
            if not str(d.get("character") or "").strip():
                issues.append({"level": "warning", "message": "对白缺少角色名", "location": location})
            else:
                has_dialogue = True
    if scenes and not has_dialogue:
        issues.append({"level": "warning", "message": "全剧未检测到任何对白", "location": "全局"})

    total = 0
    for sc in scenes:
        try:
            total += int(sc.get("duration_sec") or 0)
        except (TypeError, ValueError):
            continue
    target = project.target_duration
    if target and scenes:
        deviation = abs(total - target) / target
        if deviation > 0.2:
            issues.append({
                "level": "error",
                "message": f"场景时长合计 {total}s 与目标时长 {target}s 偏差 {deviation:.0%}，超过 20%",
                "location": "全局",
            })

    score = 100
    for it in issues:
        score -= 25 if it["level"] == "error" else 10
    return {"score": max(0, min(100, score)), "issues": issues}


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
        text = await chat(
            [
                {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": (raw_text or "")[:4000]},
            ],
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

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        script: 剧本对象。

    Returns:
        Dict[str, Any]: {"characters","scenes","props","conflicts"}，
            每条实体含 confidence 与 low_confidence 标记。

    Raises:
        HTTPException: 400 —— 剧本无原始输入文本，无法抽取。
    """
    raw_text = script.raw_text or ""
    if not raw_text.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="剧本无原始输入文本，请先保存剧本输入")

    # 双路抽取：LLM 优先，失败降级规则结果
    result = await llm_extract(raw_text)
    if result is None:
        result = rule_extract(raw_text)

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

    # 项目状态推进 + 版本快照（V-01 / RD-08）
    project.status = "asset"
    db.add(VersionSnapshot(
        project_id=project.id,
        stage="extract_confirmed",
        summary={"characters": len(req.characters), "scenes": len(req.scenes), "props": len(req.props)},
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
