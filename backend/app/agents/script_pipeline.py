"""剧本创作流水线 Agent 集（文件级说明，P-1 范式级重构 Task 3/4/5）。

本模块承载剧本四阶段流水线（idea → characters → outline → body）中
各阶段的结构化生成 Agent。已实现：
- character_planner：P-2 角色设定阶段 Agent（Task 3）；
- episode_planner：P-3 分集大纲阶段 Agent（Task 4）；
- episode_writer：P-4 逐集正文阶段 Agent（Task 5，两段式）；
- script_optimize：P-6 AI 优化四粒度 Agent（Task 7，global/episode/
  segment/field），仅输出片段化 changes，禁止整篇覆盖。
- script_assist：P4 对话式改写通道 Agent（redesign-script-workspace Task 5，
  scope=setting/episode_outline），按用户自然语言指令对当前库值提出片段化
  改写建议（episode_body 作用域复用 script_optimize 通道，不重造）。
- setting_planner：P2 故事设定阶段 Agent（redesign-script-workspace Task 2），
  生成「一句话故事 / 核心冲突 / 世界观」三段设定与人物候选（人物部分复用
  character_planner 的归一化口径）。

RD-01 忠实度差异化（P-1 Task 8）：
- build_fidelity_directive(project, confirmed_characters)：按 free/outline/
  faithful 档位返回差异化约束段，四类 Agent 信息构造统一调用；
- faithful 一致性校验：plan_episodes / write_episode_body 在 faithful 档位下
  校验生成结果的名单外角色（新造角色），超过阈值（2 个）记录 warning 并额外
  重试 1 次，不阻断主流程。

character_planner 职责：
- build_character_planner_messages()：构造角色班底设计的 LLM 提示词
  （system 要求仅输出严格 JSON + user 注入项目体裁/风格/忠实度档位/
  画幅/用户创意/可选补充指令；RD-01 忠实度约束句走 build_fidelity_directive）；
- plan_characters()：调用 LLM 生成角色班底并做解析容错与结构规整
  （name 必填否则丢弃、role_type 白名单外归 support、relations 列表
  容错、字符串截长），失败自动重试 2 次（温度递增 0.8 → 1.0），
  仍失败抛 502。

episode_planner 职责：
- build_episode_planner_messages()：构造分集大纲设计的 LLM 提示词
  （system 要求仅输出严格 JSON + user 注入项目体裁/风格/忠实度档位/
  已确认角色清单/用户创意/可选章节摘要；target_episodes=1 时附加
  「单集完整起承转合」要求）；
- plan_episodes()：调用 LLM 生成分集大纲并做解析容错与结构规整
  （title 必填否则丢弃、duration_sec 夹取 30~3600、characters 过滤到
  已确认角色名单内、emotion_curve 截长），失败自动重试 2 次
  （温度递增 0.8 → 1.0），仍失败抛 502。
- plan_episode_outline_regen()：单集大纲重生成（P3，redesign-script-workspace
  Task 4，作用域收窄为 1 集）：复用 episode_planner 提示词骨架与
  _normalize_episode_candidates 校验，构造「仅重写第 N 集」提示词（携带该集
  现有大纲 + 前后集钩子上下文 + 可选 instruction + 已确认角色强约束 +
  RD-01 忠实度挂点），失败重试 2 次（温度递增），仍失败抛 502。

episode_writer 职责（两段式：先文章体正文、再结构化场次，可靠且可流式）：
- build_episode_writer_messages()：文章体正文提示词（system 要求「可读的
  文章体剧本正文」+ 对白行格式 + 篇幅按 duration_sec 换算约 4 字/秒 +
  角色姓名与设定不得漂移；user 注入本集大纲/已确认角色清单/项目元信息）；
- stream_episode_body()：长文分段流式生成（按大纲 emotion_curve 段数
  分 1~3 批，每批带前文结尾 200 字衔接上下文，单批超 2500 字截断续写，
  yield ("delta", 文本) 块供接口层透传 SSE）；
- write_episode_body()：非流式便捷入口（复用分段流式逻辑拼接为整串）；
- build_episode_scenes_messages()：第二段结构化提示词（要求仅输出严格
  JSON：locations 去重地点清单 + scenes 3~8 场含 action/dialogue 等）；
- plan_episode_scenes()：非流式 json_mode 调用（agent_name=episode_writer），
  重试 2 次（温度递增），规整（scene_no 重排 1..n、dialogue.character_id
  按名单回填——LLM 给错的以名单为准、locations 按 name 去重、duration
  夹取），返回 {"locations": [...], "scenes": [...]}。
"""

import json
import logging
import re
from types import SimpleNamespace
from typing import AsyncGenerator, List, Optional, Tuple

from fastapi import HTTPException, status

from app.agents.llm_adapter import llm
from app.models import Episode, Project
from app.services.script_service import (
    DEFAULT_TARGET_DURATION,
    PATH_MISSING,
    get_value_by_path,
    is_optimizable_path,
    strip_json_fences,
)

# 模块日志器（忠实度一致性校验的 warning 记录位）
logger = logging.getLogger(__name__)

# faithful 档位下允许新增的名单外角色数上限（超过则触发生成后重试 1 次）
_FAITHFUL_EXTRA_CHARACTER_LIMIT = 2

# 文章体正文对白行角色名正则（行首「角色名（情绪）：」/「角色名：」，多行匹配）
_BODY_DIALOGUE_NAME_RE = re.compile(
    r"^\s*([\u4e00-\u9fa5A-Za-z0-9·]{1,10})\s*(?:[（(][^）)\n]*[）)])?\s*[：:]", re.M)

# character_planner 系统提示词（要求仅输出严格 JSON 的角色班底）
_CHARACTER_PLANNER_SYSTEM_PROMPT = (
    "你是专业的漫剧（动态漫画短剧）角色设定师。请根据项目信息与用户创意，"
    "设计本剧的角色班底。\n"
    "必须仅输出一个严格合法的 JSON 对象（禁止 markdown 代码块围栏、禁止任何解释文字），"
    "结构如下：\n"
    '{"characters":[{"name":"角色名","role_type":"lead","age":"约30岁",'
    '"personality":"性格设定","appearance":"外观设定","motivation":"行为动机",'
    '"arc":"人物弧光","relations":[{"target":"对方角色名","relation":"关系描述"}]}]}\n'
    "要求：4~8 个角色；主角（role_type 取 lead）至少 1 名；"
    "role_type 只能取 lead/support/villain/extra；"
    "relations 的 target 必须指向班底中的其他角色名；全部使用中文。"
)

# 角色定位合法取值白名单（与 characters.role_type 列 CHECK 约束一致）
_ROLE_TYPES = frozenset({"lead", "support", "villain", "extra"})

# 重试温度序列：首次 0.8，重试 2 次（共 3 次尝试）温度递增至 1.0
_TEMPERATURES = (0.8, 1.0, 1.0)


async def _llm_chat(
    messages: List[dict],
    *,
    temperature: float,
    agent_name: str = "character_planner",
    json_mode: bool = True,
) -> str:
    """模块级 LLM 调用包装：统一流水线各 Agent 的出参入口。

    独立成模块级函数便于测试用 monkeypatch 替换（无需伪造 llm 单例）；
    agent_name 可参数化以区分调用方 Agent（character_planner/episode_planner）。

    Args:
        messages: OpenAI 格式消息列表（role/content）。
        temperature: 采样温度。
        agent_name: 调用方 Agent 标识（用于渠道侧按 Agent 路由/记账，
            缺省为 character_planner 兼容 Task 3 既有调用）。
        json_mode: 是否强制 JSON 输出（结构化 Agent 为 True；纯文本通道
            如选中文本优化 quote_rewrite 为 False）。

    Returns:
        str: 模型返回的完整文本内容。

    Raises:
        Exception: 底层渠道全部失败时向上抛出（由 plan_characters/
            plan_episodes 统一重试）。
    """
    return await llm.chat(
        None,
        messages,
        agent_name=agent_name,
        json_mode=json_mode,
        temperature=temperature,
    )


def build_fidelity_directive(
    project: Project, confirmed_characters: Optional[list] = None
) -> str:
    """构造 RD-01 忠实度档位的差异化约束指令（P-1 Task 8 策略函数）。

    按 project.fidelity 返回可直接注入提示词 user 消息的约束段：
    - faithful：严格遵循原著情节与人物设定，不得新增主要角色、不得改变关键
      事件结局（传入已确认角色清单时附「出场角色必须限定在名单内」约束）；
    - outline：遵循给定大纲脉络，可在细节处适度创作；
    - free（及未知档位）：自由发挥创意，鼓励新颖表达（温度可更高）。

    各 Agent（character_planner / episode_planner / episode_writer /
    script_optimize）的信息构造统一调用本函数，替换原 if/elif 拼接。

    Args:
        project: 项目对象（读取 fidelity 档位）。
        confirmed_characters: 已确认角色清单（可空；元素为 ORM 对象（含 name
            属性）或字符串，faithful 档位下用于明确「不得新增角色」名单）。

    Returns:
        str: 约束指令文本（单行/多行，供 append 到 user 消息；free 档位亦有
            正向创作提示，非空）。

    Raises:
        无：纯字符串拼装，属性缺失按缺省档位兜底。
    """
    fidelity = project.fidelity or "free"
    if fidelity == "faithful":
        directive = (
            "约束：本项目为忠实改编，严格遵循原著情节与人物设定，"
            "不得新增主要角色、不得改变关键事件结局。")
        names: List[str] = []
        for ch in (confirmed_characters or []):
            name = str(getattr(ch, "name", ch) or "").strip()
            if name and name not in names:
                names.append(name)
        if names:
            directive += f"出场角色必须限定在已确认名单内：{'、'.join(names)}。"
        return directive
    if fidelity == "outline":
        return "约束：本项目按给定大纲改编，遵循给定大纲脉络，可在细节处适度创作。"
    return "创作提示：自由发挥创意，鼓励新颖表达。"


def _collect_episode_character_names(data: dict) -> set:
    """汇总分集大纲 LLM 原始输出中出现的角色名集合（内部函数）。

    仅统计未经理整的原始 episodes[*].characters（用于 faithful 档位下识别
    LLM 新造的名单外角色；规整阶段 _normalize_episode_candidates 会把名单外
    名字过滤掉，故需在规整前取原始集合）。

    Args:
        data: LLM 输出 JSON 解析后的 dict（期望含 episodes 数组）。

    Returns:
        set: 角色名集合（strip 后非空；结构异常返回空集合）。

    Raises:
        无：结构异常按缺数据处理。
    """
    names: set = set()
    for ep in (data.get("episodes") if isinstance(data, dict) else None) or []:
        if not isinstance(ep, dict):
            continue
        for raw in (ep.get("characters") or []):
            name = str(raw or "").strip()
            if name:
                names.add(name)
    return names


def _extract_body_character_names(text: str) -> set:
    """从文章体正文中提取对白行角色名集合（内部函数）。

    识别形如「角色名（情绪）：台词」或「角色名：台词」的行首角色名（1~10 位
    中文/字母/数字/间隔号，可选圆括号情绪标注），用于 faithful 档位下比对
    正文是否出现名单外角色。

    Args:
        text: 文章体正文全文。

    Returns:
        set: 角色名集合（strip 后非空；无可识别对白行时为空集合）。

    Raises:
        无：纯正则匹配。
    """
    names: set = set()
    for match in _BODY_DIALOGUE_NAME_RE.findall(text or ""):
        name = str(match or "").strip()
        if name:
            names.add(name)
    return names


def build_character_planner_messages(
    project: Project,
    raw_text: str,
    instruction: Optional[str] = None,
) -> List[dict]:
    """构造 character_planner 的 LLM 消息（中文提示词，注入项目元信息）。

    user 内容依次拼接：项目体裁/风格/忠实度档位/画幅比例 → 忠实度约束句
    （RD-01 差异化策略：build_fidelity_directive 统一构造 faithful/outline/
    free 约束）→ 用户创意（截前 6000 字）→ 可选补充指令（单角色重生成时传）。

    Args:
        project: 项目对象（genre/style/fidelity/aspect_ratio）。
        raw_text: 用户原始输入文本（创意/大纲/剧本等）。
        instruction: 可选补充指令（班底要求或单角色重生成方向）。

    Returns:
        List[dict]: OpenAI 格式 messages（system + user 两条）。

    Raises:
        无：纯字符串拼装，属性缺失按「未指定」兜底。
    """
    fidelity = project.fidelity or "free"
    lines: List[str] = [
        f"项目体裁：{project.genre or '未指定'}；视觉风格：{project.style or '未指定'}；"
        f"忠实度档位：{fidelity}；画幅比例：{project.aspect_ratio}。",
    ]
    # RD-01 忠实度约束句（差异化策略函数统一构造）
    lines.append(build_fidelity_directive(project))
    # 用户创意（截 6000 字，防止超长输入）
    lines.append(f"用户创意：\n{(raw_text or '')[:6000]}")
    if instruction:
        lines.append(f"补充指令：{instruction}")
    return [
        {"role": "system", "content": _CHARACTER_PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _normalize_character_candidates(data: dict) -> List[dict]:
    """把 LLM 输出解析结果规整为候选角色结构列表（字段容错，内部函数）。

    规整规则：
    - name 必填（strip 后非空），缺失的条目直接丢弃；
    - role_type 白名单（lead/support/villain/extra）外的取值归 support；
    - relations 非列表或条目缺 target/relation 时容错跳过，字符串截长；
    - 其余字符串字段截长防异常长输出（name 50 / age 20 / 其余 500）。

    Args:
        data: LLM 输出 JSON 解析后的 dict（期望含 characters 数组）。

    Returns:
        List[dict]: 规整后的候选列表，元素结构固定为
            {name, role_type, age, personality, appearance, motivation,
            arc, relations}；无任何合法条目时返回空列表。

    Raises:
        无：结构异常一律按缺数据处理，不抛出。
    """
    raw_items = data.get("characters")
    if not isinstance(raw_items, list):
        return []
    result: List[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue  # name 必填否则丢弃
        role_type = str(item.get("role_type") or "").strip()
        if role_type not in _ROLE_TYPES:
            role_type = "support"  # 白名单外归 support
        # relations 列表容错：仅保留 target/relation 均非空的 dict 条目
        relations: List[dict] = []
        raw_relations = item.get("relations")
        if isinstance(raw_relations, list):
            for rel in raw_relations:
                if not isinstance(rel, dict):
                    continue
                target = str(rel.get("target") or "").strip()
                relation = str(rel.get("relation") or "").strip()
                if target and relation:
                    relations.append({"target": target[:50], "relation": relation[:100]})

        def _clean_str(value, limit: int) -> Optional[str]:
            """字符串字段清洗：转 str、strip、截长，空串归 None（内部函数）。

            Args:
                value: 原始值（任意类型）。
                limit: 最大长度。

            Returns:
                Optional[str]: 清洗后的字符串；空串返回 None。

            Raises:
                无。
            """
            text = str(value or "").strip()[:limit]
            return text or None

        result.append({
            "name": name[:50],
            "role_type": role_type,
            "age": _clean_str(item.get("age"), 20),
            "personality": _clean_str(item.get("personality"), 500),
            "appearance": _clean_str(item.get("appearance"), 500),
            "motivation": _clean_str(item.get("motivation"), 500),
            "arc": _clean_str(item.get("arc"), 500),
            "relations": relations,
        })
    return result


async def plan_characters(
    project: Project,
    raw_text: str,
    instruction: Optional[str] = None,
) -> List[dict]:
    """生成角色班底候选：调 LLM → 剥围栏解析 → 结构规整，失败重试 2 次。

    重试策略：共最多 3 次尝试，温度依次 0.8 → 1.0 → 1.0；解析结果规整后
    为空（无任何合法角色条目）视为本轮失败进入下一轮；全部失败抛 502。

    Args:
        project: 项目对象（供提示词注入体裁/风格/忠实度等元信息）。
        raw_text: 用户原始输入文本（创意/大纲等，截 6000 字）。
        instruction: 可选补充指令（单角色重生成时传该角色的重设计要求）。

    Returns:
        List[dict]: 规整后的候选角色列表（结构见 _normalize_character_candidates）。

    Raises:
        HTTPException: 502 —— 3 次尝试（LLM 异常或解析后无合法角色）均失败。
    """
    messages = build_character_planner_messages(project, raw_text, instruction)
    for temperature in _TEMPERATURES:
        try:
            text = await _llm_chat(messages, temperature=temperature)
            data = json.loads(strip_json_fences(text))
            if isinstance(data, dict):
                candidates = _normalize_character_candidates(data)
                if candidates:
                    return candidates
        except (json.JSONDecodeError, ValueError, TypeError):
            # JSON 解析失败：进入下一轮重试
            continue
        except Exception:  # noqa: BLE001 —— LLM 网络/渠道异常同样走重试
            continue
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="角色生成失败，请重试",
    )


# ---------------------------------------------------------------------------
# episode_planner：P-3 分集大纲阶段 Agent（Task 4）
# ---------------------------------------------------------------------------
# episode_planner 系统提示词（要求仅输出严格 JSON 的分集大纲；
# JSON 示例中的花括号按 str.format 规则转义为 {{ }}，仅 {target} 为占位符）
_EPISODE_PLANNER_SYSTEM_PROMPT = (
    "你是专业的漫剧（动态漫画短剧）分集大纲策划师。请根据项目信息、"
    "已确认角色与用户创意，把故事切分为多集的大纲。\n"
    "必须仅输出一个严格合法的 JSON 对象（禁止 markdown 代码块围栏、禁止任何解释文字），"
    "结构如下：\n"
    '{{"episodes":[{{"title":"单集标题","logline":"一句话梗概（不超过60字）",'
    '"core_event":"本集核心事件","characters":["出场角色名"],'
    '"emotion_curve":"依该集剧情推导的情绪演变（用「→」串联2~4个阶段）",'
    '"duration_sec":90}}]}}\n'
    "要求：恰好输出 {target} 集；集与集之间要有连续剧钩子（每集结尾留下悬念，"
    "吸引观众观看下一集）；emotion_curve 必须逐集依据该集的梗概与核心事件"
    "独立分析推导（如「期待→紧张→反转→意犹未尽」「温馨→甜蜜→误会」），"
    "各集走向随剧情差异化，严禁所有集套用同一固定模板；"
    "characters 中的角色名必须取自已确认角色名单，"
    "不得新造主要角色；duration_sec 取 60~300；全部使用中文。"
)

# duration_sec 合法夹取范围（秒）：超出范围的 LLM 输出统一夹回边界值
_DURATION_MIN, _DURATION_MAX = 30, 3600


def build_episode_planner_messages(
    project: Project,
    raw_text: str,
    confirmed_characters: list,
    target_episodes: int,
    from_chapters: bool = False,
    chapter_summaries: Optional[list] = None,
) -> List[dict]:
    """构造 episode_planner 的 LLM 消息（中文提示词，注入项目与角色元信息）。

    system 要求仅输出严格 JSON 的 episodes 数组（恰好 target_episodes 集、
    集间连续剧钩子、角色名取自已确认名单）；user 内容依次拼接：项目体裁/
    风格/忠实度档位 → 忠实度约束句（build_fidelity_directive 统一构造，
    传入已确认角色清单强化 faithful 名单约束）→ 已确认角色清单（每行
    「name（role_type）：personality 摘要」）→ 用户创意（截前 6000 字）→
    可选章节摘要（from_chapters 时注入，Task 9 小说改编链路将使用）；
    target_episodes=1 时附加「单集完整起承转合」要求。

    Args:
        project: 项目对象（genre/style/fidelity）。
        raw_text: 用户原始输入文本（创意/大纲等，截 6000 字）。
        confirmed_characters: 已确认角色列表（characters 表 ORM 对象，
            须含 name/role_type/personality 属性）。
        target_episodes: 目标集数（1~100）。
        from_chapters: 是否按小说章节切分（Task 9 预留；True 时注入章节摘要）。
        chapter_summaries: 可选章节摘要列表（from_chapters 时使用；元素为
            字符串或含摘要的 dict，逐条截长注入）。

    Returns:
        List[dict]: OpenAI 格式 messages（system + user 两条）。

    Raises:
        无：纯字符串拼装，属性缺失按「未指定」兜底。
    """
    fidelity = project.fidelity or "free"
    lines: List[str] = [
        f"项目体裁：{project.genre or '未指定'}；视觉风格：{project.style or '未指定'}；"
        f"忠实度档位：{fidelity}。",
    ]
    # RD-01 忠实度约束句（差异化策略函数统一构造，faithful 附名单约束）
    lines.append(build_fidelity_directive(project, confirmed_characters))
    # 已确认角色清单（每行：name（role_type）：personality 摘要）
    lines.append("已确认角色名单（出场角色必须从中选取）：")
    for ch in confirmed_characters:
        personality = (getattr(ch, "personality", None) or "").strip()[:100]
        lines.append(
            f"- {getattr(ch, 'name', '未命名')}（{getattr(ch, 'role_type', None) or 'support'}）"
            f"：{personality or '暂无性格设定'}")
    # 单集特例：target_episodes=1 时要求完整起承转合
    if target_episodes <= 1:
        lines.append("单集要求：仅 1 集，须在本集内完成完整起承转合。")
    # 用户创意（截 6000 字，防止超长输入）
    lines.append(f"用户创意：\n{(raw_text or '')[:6000]}")
    # 可选章节摘要（from_chapters：小说改编切集依据，Task 9 使用）
    if from_chapters and chapter_summaries:
        lines.append("章节摘要：")
        for idx, summary in enumerate(chapter_summaries, start=1):
            text = summary if isinstance(summary, str) else str(summary or "")
            lines.append(f"{idx}. {text.strip()[:500]}")
    return [
        {"role": "system", "content": _EPISODE_PLANNER_SYSTEM_PROMPT.format(
            target=target_episodes)},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _normalize_episode_candidates(data: dict, known_names: Optional[set] = None) -> List[dict]:
    """把 LLM 输出解析结果规整为分集大纲条目列表（字段容错，内部函数）。

    规整规则：
    - title 必填（strip 后非空），缺失的条目直接丢弃；
    - duration_sec 非法（非数值）置 None，数值则夹取 30~3600；
    - characters 非列表按空处理；条目逐个转 str 容错，known_names 非空时
      过滤到已确认角色名单内（剔除 LLM 新造的名字），保序去重；
    - 字符串字段 strip 截长（title 100 / logline 200 / core_event 2000 /
      emotion_curve 100，与落库列宽对齐）。

    Args:
        data: LLM 输出 JSON 解析后的 dict（期望含 episodes 数组）。
        known_names: 已确认角色名集合（strip 后比较）；None/空集合表示
            不做名单过滤（防御分支，接口层已保证名单非空）。

    Returns:
        List[dict]: 规整后的分集列表，元素结构固定为
            {title, logline, core_event, characters, emotion_curve,
            duration_sec}；无任何合法条目时返回空列表。

    Raises:
        无：结构异常一律按缺数据处理，不抛出。
    """
    raw_items = data.get("episodes")
    if not isinstance(raw_items, list):
        return []
    known = {str(n or "").strip() for n in (known_names or set())} or None
    result: List[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue  # title 必填否则丢弃
        # characters 容错：逐条转 str、过滤到已确认名单内、保序去重
        characters: List[str] = []
        raw_names = item.get("characters")
        if isinstance(raw_names, list):
            for raw in raw_names:
                name = str(raw or "").strip()
                if not name:
                    continue
                if known is not None and name not in known:
                    continue  # 名单外（LLM 新造）的角色名剔除
                if name not in characters:
                    characters.append(name)
        # duration_sec 夹取：非法置 None，数值夹回 [30, 3600]
        try:
            duration = int(item.get("duration_sec"))
        except (TypeError, ValueError):
            duration = None
        else:
            duration = max(_DURATION_MIN, min(_DURATION_MAX, duration))
        result.append({
            "title": title[:100],
            "logline": str(item.get("logline") or "").strip()[:200],
            "core_event": str(item.get("core_event") or "").strip()[:2000],
            "characters": characters,
            "emotion_curve": str(item.get("emotion_curve") or "").strip()[:100],
            "duration_sec": duration,
        })
    return result


async def plan_episodes(
    project: Project,
    raw_text: str,
    confirmed_characters: list,
    target_episodes: int,
    from_chapters: bool = False,
    chapter_summaries: Optional[list] = None,
    faithful_report: Optional[dict] = None,
) -> List[dict]:
    """生成分集大纲候选：调 LLM → 剥围栏解析 → 结构规整，失败重试 2 次。

    重试策略同 plan_characters：共最多 3 次尝试，温度依次 0.8 → 1.0 → 1.0；
    解析结果规整后为空（无任何合法分集条目）视为本轮失败进入下一轮；
    全部失败抛 502。

    faithful 一致性校验（RD-01，不阻断主流程）：faithful 档位下统计 LLM 原始
    输出中名单外角色（新造角色）——超过阈值（2 个）时记录 warning 日志并额外
    重试 1 次；重试后仍超阈值则照常返回（规整阶段已把名单外名字过滤，仅取
    名单内角色）。校验结果写入 faithful_report["extra_characters"]（可选回填）。

    Args:
        project: 项目对象（供提示词注入体裁/风格/忠实度等元信息）。
        raw_text: 用户原始输入文本（创意/大纲等，截 6000 字）。
        confirmed_characters: 已确认角色列表（characters 表 ORM 对象）。
        target_episodes: 目标集数（1~100）。
        from_chapters: 是否按小说章节切分（Task 9 预留）。
        chapter_summaries: 可选章节摘要列表（from_chapters 时注入提示词）。
        faithful_report: 可选回填容器（faithful 校验结果落
            {"extra_characters": [...]}；None 时仅日志）。

    Returns:
        List[dict]: 规整后的分集大纲列表（结构见 _normalize_episode_candidates）。

    Raises:
        HTTPException: 502 —— 3 次尝试（LLM 异常或解析后无合法分集）均失败。
    """
    messages = build_episode_planner_messages(
        project, raw_text, confirmed_characters, target_episodes,
        from_chapters=from_chapters, chapter_summaries=chapter_summaries)
    # 已确认角色名集合：供规整阶段过滤 LLM 新造的角色名
    known = {getattr(ch, "name", "") for ch in confirmed_characters}
    faithful = (project.fidelity or "free") == "faithful"
    extra_names: set = set()
    for attempt, temperature in enumerate(_TEMPERATURES):
        try:
            text = await _llm_chat(
                messages, temperature=temperature, agent_name="episode_planner")
            data = json.loads(strip_json_fences(text))
            if isinstance(data, dict):
                items = _normalize_episode_candidates(data, known)
                if items:
                    if faithful and known:
                        # faithful 校验：原始输出中的名单外角色（新造角色）
                        extra_names = _collect_episode_character_names(data) - known
                        if len(extra_names) > _FAITHFUL_EXTRA_CHARACTER_LIMIT:
                            logger.warning(
                                "faithful 忠实度校验：分集大纲出现名单外角色 %s"
                                "（超过阈值 %d），触发重试",
                                sorted(extra_names), _FAITHFUL_EXTRA_CHARACTER_LIMIT)
                            if attempt == 0:
                                continue  # 额外重试 1 次（不阻断主流程）
                            logger.warning(
                                "faithful 忠实度校验：重试后仍存在名单外角色 %s，"
                                "已按名单过滤后返回", sorted(extra_names))
                    if faithful_report is not None:
                        faithful_report["extra_characters"] = sorted(extra_names)
                    return items
        except (json.JSONDecodeError, ValueError, TypeError):
            # JSON 解析失败：进入下一轮重试
            continue
        except Exception:  # noqa: BLE001 —— LLM 网络/渠道异常同样走重试
            continue
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="分集大纲生成失败，请重试",
    )


# ---------------------------------------------------------------------------
# episode_planner 扩展：单集大纲重生成（P3，redesign-script-workspace Task 4）
# ---------------------------------------------------------------------------
# 单集大纲重生成系统提示词（复用 episode_planner 的提示词骨架，作用域收窄为
# 单集：要求仅输出 1 条第 {sequence} 集的大纲，JSON 示例花括号按 str.format
# 规则转义为 {{ }}，仅 {sequence} 为占位符）
_EPISODE_OUTLINE_REGEN_SYSTEM_PROMPT = (
    "你是专业的漫剧（动态漫画短剧）分集大纲策划师。请基于该集现有大纲与"
    "前后集剧情衔接，只重写第 {sequence} 集的大纲。\n"
    "必须仅输出一个严格合法的 JSON 对象（禁止 markdown 代码块围栏、禁止任何解释文字），"
    "结构如下：\n"
    '{{"episodes":[{{"title":"单集标题","logline":"一句话梗概（不超过60字）",'
    '"core_event":"本集核心事件","characters":["出场角色名"],'
    '"emotion_curve":"依该集剧情推导的情绪演变（用「→」串联2~4个阶段）",'
    '"duration_sec":90}}]}}\n'
    "要求：episodes 数组恰好 1 条且只对应第 {sequence} 集（禁止输出其他集）；"
    "开头承接前一集结尾钩子、结尾为后一集留下悬念；emotion_curve 必须依据"
    "该集梗概与核心事件独立分析推导（如「期待→紧张→反转」「决裂→悔恨→和解」），"
    "严禁套用固定模板；characters 中的角色名必须"
    "取自已确认角色名单，不得新造主要角色；duration_sec 取 60~300；全部使用中文。"
)

# 单集大纲 AI 一键填充系统提示词（与 regen 的差异：只补全用户未填写的字段，
# 已填写字段必须原样保留——填充结果回表单由用户确认后再保存，不直接落库；
# title 明确要求直接输出剧情标题名，禁止「第X集」序号形式——集卡上已单独
# 展示「第N集」标签，标题再带序号即冗余）
_EPISODE_OUTLINE_AUTOFILL_SYSTEM_PROMPT = (
    "你是专业的漫剧（动态漫画短剧）分集大纲策划师。第 {sequence} 集的大纲中"
    "部分字段尚未填写，请依据故事设定、角色设定与本集已填写的内容，"
    "把缺失的字段补全。\n"
    "必须仅输出一个严格合法的 JSON 对象（禁止 markdown 代码块围栏、禁止任何解释文字），"
    "结构如下：\n"
    '{{"episodes":[{{"title":"单集剧情标题（直接输出标题名，如「初入宗门」，'
    '禁止「第X集」「第X集：」等序号形式）","logline":"一句话梗概（不超过60字）",'
    '"core_event":"本集核心事件","characters":["出场角色名"],'
    '"emotion_curve":"依该集剧情推导的情绪演变（用「→」串联2~4个阶段）",'
    '"duration_sec":90}}]}}\n'
    "要求：episodes 数组恰好 1 条且只对应第 {sequence} 集；"
    "title 必须是依据剧情拟定的真实标题名，禁止「第X集」或「第X集：」等序号形式；"
    "用户已填写的字段必须原样输出、一字不改；只补全缺失字段，补全内容须与"
    "故事设定（一句话故事/核心冲突/世界观）和角色设定保持一致，并与前后集"
    "剧情自然衔接；emotion_curve 必须依据该集梗概与核心事件独立分析推导"
    "（如「期待→紧张→反转」「决裂→悔恨→和解」），严禁套用固定模板；"
    "characters 中的角色名必须取自已确认角色名单；"
    "duration_sec 取 60~300；全部使用中文。"
)


def is_placeholder_episode_title(title: Optional[str]) -> bool:
    """判断单集标题是否为创建空集时的占位名（如「第 2 集」「第 12 集」）。

    占位标题在「AI 一键填充」链路视为未填写——AI 应生成真实标题而非原样
    保留占位名（新增集默认标题「第 X 集」由剧集创建接口写入）。

    Args:
        title: 单集标题（可为 None/空串）。

    Returns:
        bool: 占位标题为 True（应视为未填写）。
    """
    if not title or not title.strip():
        return True
    return bool(re.fullmatch(r"第\s*\d+\s*集", title.strip()))


# 标题开头的序号前缀（「第3集：」「第 3 集·」「第12集-」等）——autofill 输出清洗用
_EPISODE_TITLE_PREFIX_RE = re.compile(r"^第\s*\d+\s*集\s*[:：·\-—\s]*")


def _polish_autofill_title(item: dict) -> dict:
    """清洗 AI 一键填充建议的标题：剥掉「第X集」序号前缀，确保为真实标题名（内部函数）。

    LLM 受提示词上下文「第 N 集」序列描述影响，可能把标题拟成「第 3 集」或
    「第3集：初入宗门」——剥前缀后若为空/仍为占位，依次用 logline、core_event
    前 20 字兜底（集卡上已单独展示「第N集」标签，标题不应重复序号）。

    Args:
        item: 规整后的单集大纲建议条目（含 title/logline/core_event）。

    Returns:
        dict: 原条目（title 就地修正；结构不变）。

    Raises:
        无：纯字符串处理，字段缺失按空值兜底。
    """
    title = str(item.get("title") or "").strip()
    stripped = _EPISODE_TITLE_PREFIX_RE.sub("", title).strip()
    if not is_placeholder_episode_title(stripped):
        item["title"] = stripped
        return item
    # 剥前缀后为空或仍为占位（LLM 直接输出了「第 X 集」）：依次取
    # logline / core_event 前 20 字作为标题兜底
    for key in ("logline", "core_event"):
        text = str(item.get(key) or "").strip()
        if text:
            item["title"] = text[:20]
            return item
    item["title"] = stripped or title
    return item


def build_episode_outline_autofill_messages(
    project: Project,
    episode: Episode,
    confirmed_characters: list,
    story_setting: Optional[dict] = None,
    prev_episode: Optional[Episode] = None,
    next_episode: Optional[Episode] = None,
) -> List[dict]:
    """构造单集大纲 AI 一键填充的 LLM 消息（只补缺失字段，已填字段原样保留）。

    system 为 autofill 约束（已填字段原样输出、缺失字段依据上下文补全）；
    user 依次拼接：项目体裁/风格/忠实度档位 → 故事设定三段（一句话故事/
    核心冲突/世界观，缺失段跳过）→ 已确认角色名单（每行「name（role_type）：
    personality 摘要」）→ 该集当前大纲（逐字段标注「已填写/未填写」）→
    前后集钩子上下文（保证补全内容与邻集衔接）。

    Args:
        project: 项目对象（genre/style/fidelity/story_setting）。
        episode: 目标剧集 ORM 对象（sequence/title/logline/outline）。
        confirmed_characters: 已确认角色列表（characters 表 ORM 对象）。
        story_setting: 故事设定 JSONB（logline/core_conflict/worldview，
            可为 None 或缺段）。
        prev_episode: 前一集 ORM 对象（第 1 集时为 None）。
        next_episode: 后一集 ORM 对象（最后一集时为 None）。

    Returns:
        List[dict]: OpenAI 格式 messages（system + user 两条）。

    Raises:
        无：纯字符串拼装，outline 结构异常按缺字段兜底。
    """
    outline = episode.outline if isinstance(episode.outline, dict) else {}
    setting = story_setting if isinstance(story_setting, dict) else {}
    lines: List[str] = [
        f"项目体裁：{project.genre or '未指定'}；视觉风格：{project.style or '未指定'}；"
        f"忠实度档位：{project.fidelity or 'free'}。",
    ]
    # 故事设定三段（autofill 的主要依据：补全内容不得与之冲突）
    setting_parts = [
        ("一句话故事", setting.get("logline")),
        ("核心冲突", setting.get("core_conflict")),
        ("世界观", setting.get("worldview")),
    ]
    if any(text for _, text in setting_parts):
        lines.append("故事设定（补全内容必须与之保持一致）：")
        for label, text in setting_parts:
            if text and str(text).strip():
                lines.append(f"- {label}：{str(text).strip()[:300]}")
    # 已确认角色清单（出场角色必须从中选取）
    lines.append("已确认角色名单（出场角色必须从中选取）：")
    for ch in confirmed_characters:
        personality = (getattr(ch, "personality", None) or "").strip()[:100]
        lines.append(
            f"- {getattr(ch, 'name', '未命名')}（{getattr(ch, 'role_type', None) or 'support'}）"
            f"：{personality or '暂无性格设定'}")
    # 该集当前大纲：逐字段标注已填写/未填写（LLM 据此决定保留还是补全；
    # 占位标题「第 X 集」视为未填写，AI 应生成真实标题）
    title_filled = not is_placeholder_episode_title(episode.title)
    lines.append(f"第 {episode.sequence} 集当前大纲（已填写字段必须原样保留）：")
    lines.append(f"- 标题：{'（已填写）' + episode.title if title_filled else '（未填写，请依据剧情拟定真实标题）'}")
    lines.append(
        f"- 一句话梗概：{'（已填写）' + episode.logline if episode.logline else '（未填写）'}")
    lines.append(
        f"- 核心事件：{'（已填写）' + str(outline.get('core_event')) if outline.get('core_event') else '（未填写）'}")
    lines.append(
        f"- 出场角色：{'（已填写）' + '、'.join(str(c) for c in outline['characters']) if outline.get('characters') else '（未填写）'}")
    # 情绪走向：已填写原样标注；未填写时引导 LLM 依据本集梗概与核心事件推导
    curve_text = str(outline.get("emotion_curve") or "").strip()
    if curve_text:
        lines.append(f"- 情绪走向：（已填写）{curve_text}")
    else:
        lines.append(
            "- 情绪走向：（未填写，请依据本集梗概与核心事件推导情绪演变，"
            "2~4 个阶段用「→」连接）")
    lines.append(
        f"- 目标时长：{'（已填写）' + str(outline.get('duration_sec')) + ' 秒' if outline.get('duration_sec') else '（未填写）'}")
    # 前后集钩子上下文（保证补全内容与邻集衔接）
    if prev_episode is not None:
        lines.append(
            f"前一集（第 {prev_episode.sequence} 集）《{prev_episode.title}》"
            f"结尾：{prev_episode.logline or prev_episode.title}（本集开头需承接该钩子）")
    if next_episode is not None:
        lines.append(
            f"后一集（第 {next_episode.sequence} 集）《{next_episode.title}》"
            f"开头：{next_episode.logline or next_episode.title}（本集结尾需为其留下悬念）")
    return [
        {"role": "system", "content": _EPISODE_OUTLINE_AUTOFILL_SYSTEM_PROMPT.format(
            sequence=episode.sequence)},
        {"role": "user", "content": "\n".join(lines)},
    ]


async def plan_episode_outline_autofill(
    project: Project,
    episode: Episode,
    confirmed_characters: list,
    story_setting: Optional[dict] = None,
    prev_episode: Optional[Episode] = None,
    next_episode: Optional[Episode] = None,
) -> dict:
    """单集大纲 AI 一键填充：依据故事设定/角色设定/本集已填内容补全缺失字段。

    复用 plan_episode_outline_regen 的重试骨架（3 次尝试，温度依次
    0.8 → 1.0 → 1.0）与 _normalize_episode_candidates 规整校验（含
    known_names 名单外角色过滤）。注意：本函数只返回建议结果不落库——
    填充值由前端回填表单，用户确认后走既有 PUT outline 保存（留痕在保存侧）。

    Args:
        project: 项目对象（genre/style/fidelity/story_setting）。
        episode: 目标剧集 ORM 对象（sequence/title/logline/outline）。
        confirmed_characters: 已确认角色列表（characters 表 ORM 对象）。
        story_setting: 故事设定 JSONB（可为 None）。
        prev_episode: 前一集 ORM 对象（第 1 集时为 None）。
        next_episode: 后一集 ORM 对象（最后一集时为 None）。

    Returns:
        dict: 规整后的单集大纲条目（结构同 plan_episode_outline_regen）。

    Raises:
        HTTPException: 502 —— 3 次尝试（LLM 异常或解析后无合法分集条目）均失败。
    """
    messages = build_episode_outline_autofill_messages(
        project, episode, confirmed_characters, story_setting,
        prev_episode=prev_episode, next_episode=next_episode)
    # 已确认角色名集合：供规整阶段过滤 LLM 新造的角色名
    known = {getattr(ch, "name", "") for ch in confirmed_characters}
    for temperature in _TEMPERATURES:
        try:
            text = await _llm_chat(
                messages, temperature=temperature, agent_name="episode_planner")
            data = json.loads(strip_json_fences(text))
            if isinstance(data, dict):
                items = _normalize_episode_candidates(data, known)
                if items:
                    # 标题清洗：剥「第X集」序号前缀/占位兜底，确保返回真实标题名
                    return _polish_autofill_title(items[0])
        except (json.JSONDecodeError, ValueError, TypeError):
            # JSON 解析失败：进入下一轮重试
            continue
        except Exception:  # noqa: BLE001 —— LLM 网络/渠道异常同样走重试
            continue
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="AI 一键填充失败，请重试",
    )


def build_episode_outline_regen_messages(
    project: Project,
    episode: Episode,
    prev_episode: Optional[Episode],
    next_episode: Optional[Episode],
    confirmed_characters: list,
    instruction: Optional[str] = None,
) -> List[dict]:
    """构造单集大纲重生成的 LLM 消息（中文提示词，携带现有大纲与前后集钩子）。

    system 要求仅输出严格 JSON 的 episodes 数组（恰好 1 条、只对应目标集）；
    user 内容依次拼接：项目体裁/风格/忠实度档位 → 忠实度约束句（RD-01
    build_fidelity_directive 统一构造，faithful 档位附名单约束）→ 已确认
    角色名单（每行「name（role_type）：personality 摘要」）→ 该集现有大纲
    （标题/梗概/核心事件/出场角色/情绪走向/时长，供 LLM 在原基础上重写）→
    前一集结尾钩子与后一集开头（保证集间衔接）→ 可选用户调整方向
    （instruction，如「冲突提前到开场」）。

    Args:
        project: 项目对象（genre/style/fidelity）。
        episode: 目标剧集 ORM 对象（sequence/title/logline/outline）。
        prev_episode: 前一集 ORM 对象（第 1 集时为 None）。
        next_episode: 后一集 ORM 对象（最后一集时为 None）。
        confirmed_characters: 已确认角色列表（characters 表 ORM 对象）。
        instruction: 可选用户调整方向描述。

    Returns:
        List[dict]: OpenAI 格式 messages（system + user 两条）。

    Raises:
        无：纯字符串拼装，outline 结构异常按缺字段兜底。
    """
    outline = episode.outline if isinstance(episode.outline, dict) else {}
    fidelity = project.fidelity or "free"
    lines: List[str] = [
        f"项目体裁：{project.genre or '未指定'}；视觉风格：{project.style or '未指定'}；"
        f"忠实度档位：{fidelity}。",
    ]
    # RD-01 忠实度约束句（差异化策略函数统一构造，faithful 附名单约束）
    lines.append(build_fidelity_directive(project, confirmed_characters))
    # 已确认角色清单（与 plan_episodes 同款：出场角色必须从中选取）
    lines.append("已确认角色名单（出场角色必须从中选取）：")
    for ch in confirmed_characters:
        personality = (getattr(ch, "personality", None) or "").strip()[:100]
        lines.append(
            f"- {getattr(ch, 'name', '未命名')}（{getattr(ch, 'role_type', None) or 'support'}）"
            f"：{personality or '暂无性格设定'}")
    # 该集现有大纲（重写参考）
    lines.append(
        f"第 {episode.sequence} 集现有大纲（在其基础上重写）：")
    lines.append(f"- 标题：{episode.title or '未命名'}")
    if episode.logline:
        lines.append(f"- 一句话梗概：{episode.logline}")
    if outline.get("core_event"):
        lines.append(f"- 核心事件：{outline['core_event']}")
    if outline.get("characters"):
        lines.append(f"- 出场角色：{'、'.join(str(c) for c in outline['characters'])}")
    if outline.get("emotion_curve"):
        lines.append(f"- 情绪走向：{outline['emotion_curve']}")
    if outline.get("duration_sec"):
        lines.append(f"- 目标时长：{outline['duration_sec']} 秒")
    # 前后集钩子上下文（保证集间衔接）
    if prev_episode is not None:
        lines.append(
            f"前一集（第 {prev_episode.sequence} 集）《{prev_episode.title}》"
            f"结尾：{prev_episode.logline or prev_episode.title}（本集开头需承接该钩子）")
    if next_episode is not None:
        lines.append(
            f"后一集（第 {next_episode.sequence} 集）《{next_episode.title}》"
            f"开头：{next_episode.logline or next_episode.title}（本集结尾需为其留下悬念）")
    # 可选用户调整方向
    if instruction and instruction.strip():
        lines.append(f"用户调整要求：{instruction.strip()}")
    return [
        {"role": "system", "content": _EPISODE_OUTLINE_REGEN_SYSTEM_PROMPT.format(
            sequence=episode.sequence)},
        {"role": "user", "content": "\n".join(lines)},
    ]


async def plan_episode_outline_regen(
    project: Project,
    episode: Episode,
    prev_episode: Optional[Episode],
    next_episode: Optional[Episode],
    confirmed_characters: list,
    instruction: Optional[str] = None,
) -> dict:
    """重生成单集大纲：调 LLM → 剥围栏解析 → 复用 _normalize_episode_candidates
    规整并取第一条，失败重试 2 次。

    重试策略同 plan_episodes：共最多 3 次尝试，温度依次 0.8 → 1.0 → 1.0；
    解析规整后为空（无合法分集条目）视为本轮失败进入下一轮；全部失败抛 502。
    角色名单校验复用 _normalize_episode_candidates 的 known_names 过滤
    （LLM 新造的名单外角色名剔除），与整卷生成口径一致。

    Args:
        project: 项目对象（供提示词注入体裁/风格/忠实度等元信息）。
        episode: 目标剧集 ORM 对象（提供 sequence 与现有大纲上下文）。
        prev_episode: 前一集 ORM 对象（第 1 集时为 None）。
        next_episode: 后一集 ORM 对象（最后一集时为 None）。
        confirmed_characters: 已确认角色列表（characters 表 ORM 对象）。
        instruction: 可选用户调整方向描述。

    Returns:
        dict: 规整后的单集大纲条目（结构见 _normalize_episode_candidates：
            {title, logline, core_event, characters, emotion_curve,
            duration_sec}）。

    Raises:
        HTTPException: 502 —— 3 次尝试（LLM 异常或解析后无合法分集条目）均失败。
    """
    messages = build_episode_outline_regen_messages(
        project, episode, prev_episode, next_episode, confirmed_characters,
        instruction=instruction)
    # 已确认角色名集合：供规整阶段过滤 LLM 新造的角色名
    known = {getattr(ch, "name", "") for ch in confirmed_characters}
    for temperature in _TEMPERATURES:
        try:
            text = await _llm_chat(
                messages, temperature=temperature, agent_name="episode_planner")
            data = json.loads(strip_json_fences(text))
            if isinstance(data, dict):
                items = _normalize_episode_candidates(data, known)
                if items:
                    return items[0]
        except (json.JSONDecodeError, ValueError, TypeError):
            # JSON 解析失败：进入下一轮重试
            continue
        except Exception:  # noqa: BLE001 —— LLM 网络/渠道异常同样走重试
            continue
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="单集大纲重生成失败，请重试",
    )


# ---------------------------------------------------------------------------
# episode_writer：P-4 逐集正文阶段 Agent（Task 5，两段式生成）
# ---------------------------------------------------------------------------
# episode_writer 文章体正文系统提示词（两段式第一段：可读的文章体正文）
_EPISODE_WRITER_SYSTEM_PROMPT = (
    "你是专业的漫剧（动态漫画短剧）编剧。请根据给定的本集大纲与角色名单，"
    "写出本集「可读的文章体剧本正文」：\n"
    "- 以叙述推进画面与动作，穿插角色对白；对白行固定格式：「角色名（情绪）：台词」；\n"
    "- 篇幅按本集目标时长换算（约 4 字/秒，如 180 秒的集约 700~900 字），"
    "按指定篇幅写满即止，不要超写或烂尾；\n"
    "- 角色姓名与设定必须与给定名单完全一致，不得新造角色、不得漂移人设；\n"
    "- 只输出正文本身（纯文章体），禁止输出 JSON、markdown 围栏、章节号或任何解释文字。"
)

# episode_scenes 结构化系统提示词（两段式第二段：正文 → 场次 JSON）
_EPISODE_SCENES_SYSTEM_PROMPT = (
    "你是专业的漫剧剧本结构化编辑。请把给定的文章体正文整理为结构化场次 JSON。\n"
    "必须仅输出一个严格合法的 JSON 对象（禁止 markdown 代码块围栏、禁止任何解释文字），"
    "结构如下：\n"
    '{"locations":[{"name":"地点名","description":"地点描述","time_of_day":"day"}],'
    '"scenes":[{"scene_no":1,"title":"场次标题","action":"动作/画面描述（必填）",'
    '"plot":"剧情概要","location":"地点名（引用 locations.name）",'
    '"time_of_day":"day","int_ext":"int","sfx":["音效"],"emotion":"场次主导情绪",'
    '"dialogue":[{"character":"角色名","character_id":1,"type":"对白",'
    '"emotion":"情绪","line":"台词"}],"duration_sec":30}]}\n'
    "要求：场次 3~8 个；location 必须引用 locations 中已有的 name；"
    "dialogue 的 character 必须取自给定角色名单，character_id 使用名单中对应的数字 ID；"
    "type 只能取 对白/旁白/独白/画外音；time_of_day 只能取 day/night/dawn/dusk；"
    "int_ext 只能取 int/ext；每场 duration_sec 为正整数，且各场合计约等于本集目标时长"
    "（±10%）；全部使用中文。"
)

# 正文单批流式输出长度上限（字符）：超长截断后交由下一批衔接续写
_BODY_BATCH_MAX_CHARS = 2500

# 正文分段批次上限：情绪曲线段数多于 3 时截为 3 批
_BODY_BATCH_MAX = 3

# 衔接上下文长度（前文结尾注入下一批 user 消息的字符数）
_BODY_BATCH_TAIL_CHARS = 200

# dialogue 台词类型白名单（与 DialogueItem.type Literal 一致）
_DIALOGUE_TYPES = frozenset({"对白", "旁白", "独白", "画外音"})

# 时段白名单（与 SceneItem/LocationItem.time_of_day Literal 一致）
_TIME_OF_DAY = frozenset({"day", "night", "dawn", "dusk"})

# 内外景白名单（与 SceneItem.int_ext Literal 一致）
_INT_EXT = frozenset({"int", "ext"})


async def _llm_chat_stream(
    messages: List[dict],
    *,
    agent_name: str = "episode_writer",
) -> AsyncGenerator[str, None]:
    """模块级流式 LLM 调用包装：episode_writer 的增量出参入口。

    独立成模块级函数便于测试用 monkeypatch 替换（与 _llm_chat 挂点同风格）。

    Args:
        messages: OpenAI 格式消息列表（role/content）。
        agent_name: 调用方 Agent 标识（写入用量记录，缺省 episode_writer）。

    Yields:
        str: 模型文本增量片段（拼接后为完整输出）。

    Raises:
        Exception: 底层渠道全部失败时向上抛出（由调用方决定降级/报错方式）。
    """
    async for delta in llm.chat_stream(None, messages, agent_name=agent_name):
        yield delta


def build_episode_writer_messages(
    project: Project,
    episode: Episode,
    confirmed_characters: list,
    fidelity_ctx: Optional[str] = None,
) -> Tuple[str, str]:
    """构造 episode_writer 文章体正文生成的提示词（两段式第一段）。

    system 为文章体正文要求（叙述+对白、对白行格式「角色名（情绪）：台词」、
    篇幅按时长换算、角色姓名不得漂移）；user 依次拼接：项目体裁/风格/忠实度
    档位/画幅 → 忠实度约束句（RD-01 差异化策略：build_fidelity_directive 统一
    构造 faithful/outline/free 约束，faithful 附名单约束）→ 本集大纲
    （title/logline/core_event/characters/emotion_curve/duration_sec + 篇幅
    换算）→ 已确认角色清单（每行「name（role_type）：personality 摘要」）。

    Args:
        project: 项目对象（genre/style/fidelity/aspect_ratio/target_duration）。
        episode: 剧集对象（title/logline 列 + outline JSONB 大纲字段）。
        confirmed_characters: 已确认角色列表（characters 表 ORM 对象，
            须含 name/role_type/personality 属性）。
        fidelity_ctx: 忠实度档位覆盖（None 时取 project.fidelity or "free"）。

    Returns:
        Tuple[str, str]: (system, user) 提示词二元组。

    Raises:
        无：纯字符串拼装，属性缺失按「未指定」兜底。
    """
    fidelity = fidelity_ctx or project.fidelity or "free"
    outline = episode.outline if isinstance(episode.outline, dict) else {}
    # 篇幅换算：大纲 duration_sec 优先，缺省退项目目标时长，再缺省 DEFAULT_TARGET_DURATION
    try:
        duration = int(
            outline.get("duration_sec") or project.target_duration or DEFAULT_TARGET_DURATION)
    except (TypeError, ValueError):
        duration = DEFAULT_TARGET_DURATION
    lines: List[str] = [
        f"项目体裁：{project.genre or '未指定'}；视觉风格：{project.style or '未指定'}；"
        f"忠实度档位：{fidelity}；画幅比例：{project.aspect_ratio}。",
    ]
    # RD-01 忠实度约束句（差异化策略函数统一构造；fidelity_ctx 覆盖时同步）
    directive_project = project if fidelity == (project.fidelity or "free") else SimpleNamespace(
        fidelity=fidelity)
    lines.append(build_fidelity_directive(directive_project, confirmed_characters))
    lines.append("本集大纲：")
    lines.append(f"- 标题：{episode.title}")
    if episode.logline:
        lines.append(f"- 梗概：{episode.logline}")
    if outline.get("core_event"):
        lines.append(f"- 核心事件：{outline['core_event']}")
    curve_chars = outline.get("characters") or []
    if curve_chars:
        lines.append(f"- 出场角色：{'、'.join(str(c) for c in curve_chars)}")
    if outline.get("emotion_curve"):
        lines.append(f"- 情绪曲线：{outline['emotion_curve']}")
    lines.append(
        f"- 目标时长：{duration} 秒（正文篇幅约 {duration * 4} 字，按约 4 字/秒换算）")
    lines.append("已确认角色名单（出场角色姓名与设定必须与之完全一致，不得漂移）：")
    for ch in confirmed_characters:
        personality = (getattr(ch, "personality", None) or "").strip()[:100]
        lines.append(
            f"- {getattr(ch, 'name', '未命名')}"
            f"（{getattr(ch, 'role_type', None) or 'support'}）：{personality or '暂无性格设定'}")
    return _EPISODE_WRITER_SYSTEM_PROMPT, "\n".join(lines)


def _split_emotion_segments(curve: str) -> List[str]:
    """把情绪曲线字符串拆为分段批次段落（内部函数）。

    规则：按常见分隔符（→/-/—/–/>/、等）切分；多于 _BODY_BATCH_MAX 段时
    截断为前 3 段；拆不出多段（单段或空）时返回空列表表示不分批。

    Args:
        curve: 情绪曲线字符串（如「压抑→爆发→释然」）。

    Returns:
        List[str]: 情绪段落文本列表（strip 后非空，0~3 个；单段/空 → 空列表）。

    Raises:
        无：纯字符串切分。
    """
    parts = [p.strip() for p in re.split(r"[→\-—–>、]+", curve or "") if p.strip()]
    if len(parts) <= 1:
        return []
    return parts[:_BODY_BATCH_MAX]


async def stream_episode_body(
    project: Project,
    episode: Episode,
    confirmed_characters: list,
    fidelity_ctx: Optional[str] = None,
) -> AsyncGenerator[Tuple[str, str], None]:
    """分段流式生成单集文章体正文（P-4 长文分段策略，接口层透传 SSE delta）。

    分段规则：按本集大纲 emotion_curve 的段数分批（2~3 段 → 2~3 批，上限
    3 批；单段/缺失 → 单批不分段）；除首批外每批 user 消息携带前文结尾
    200 字作为衔接上下文，末批提示完成本集收尾；单批输出累计超过 2500 字
    即截断进入下一批。delta 持续透传，全部批次拼接为完整正文。

    Args:
        project: 项目对象（供提示词注入体裁/风格/忠实度等元信息）。
        episode: 剧集对象（title/logline 列 + outline JSONB 大纲字段）。
        confirmed_characters: 已确认角色列表（characters 表 ORM 对象）。
        fidelity_ctx: 忠实度档位覆盖（None 时取 project.fidelity or "free"）。

    Yields:
        Tuple[str, str]: ("delta", 文本增量) 二元组（接口层原样透传为 SSE delta）。

    Raises:
        Exception: 底层 LLM 渠道异常向上抛出（由接口层统一捕获转 error 事件）。
    """
    system, user_base = build_episode_writer_messages(
        project, episode, confirmed_characters, fidelity_ctx)
    outline = episode.outline if isinstance(episode.outline, dict) else {}
    segments = _split_emotion_segments(str(outline.get("emotion_curve") or ""))
    # 单段/缺失 → 单批（segment=None 表示该批无情绪段落要求）
    batches: List[Optional[str]] = list(segments) if segments else [None]
    tail = ""
    for idx, segment in enumerate(batches):
        lines: List[str] = []
        if segment is not None:
            last = idx == len(batches) - 1
            lines.append(
                f"本批情绪段落：{segment}（本批为最后一段，请完成本集结尾与收束）"
                if last else f"本批情绪段落：{segment}（只写本批内容，不要越段）")
        if tail:
            lines.append(f"前文结尾（衔接上下文，自然续写、不要重复）：\n…{tail[-_BODY_BATCH_TAIL_CHARS:]}")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_base + ("\n" + "\n".join(lines) if lines else "")},
        ]
        batch_parts: List[str] = []
        batch_len = 0
        async for delta in _llm_chat_stream(messages, agent_name="episode_writer"):
            batch_parts.append(delta)
            batch_len += len(delta)
            yield ("delta", delta)
            if batch_len >= _BODY_BATCH_MAX_CHARS:
                break  # 单批超长截断，剩余内容交给下一批衔接续写
        tail = "".join(batch_parts)


async def write_episode_body(
    project: Project,
    episode: Episode,
    confirmed_characters: list,
    fidelity_ctx: Optional[str] = None,
    faithful_report: Optional[dict] = None,
) -> str:
    """生成单集文章体正文并收集为完整文本（非流式便捷入口）。

    内部复用 stream_episode_body 的分段流式逻辑，仅把增量拼接为整串返回
    （供不需要 SSE 透传的调用方/测试使用）。

    faithful 一致性校验（RD-01，不阻断主流程）：faithful 档位下从正文中提取
    对白行角色名（「角色名（情绪）：台词」/「角色名：台词」格式），与已确认
    名单比对——名单外角色数超过阈值（2 个）时记录 warning 日志并额外重试
    1 次；重试仍有则照常返回。校验结果写入 faithful_report["extra_characters"]。

    Args:
        project: 项目对象。
        episode: 剧集对象。
        confirmed_characters: 已确认角色列表（characters 表 ORM 对象）。
        fidelity_ctx: 忠实度档位覆盖（None 时取 project.fidelity or "free"）。
        faithful_report: 可选回填容器（faithful 校验结果落
            {"extra_characters": [...]}；None 时仅日志）。

    Returns:
        str: 拼接后的完整文章体正文。

    Raises:
        Exception: 底层 LLM 渠道异常向上抛出。
    """
    fidelity = fidelity_ctx or project.fidelity or "free"
    allowed = {
        str(getattr(ch, "name", "") or "").strip()
        for ch in confirmed_characters
        if str(getattr(ch, "name", "") or "").strip()
    }
    faithful = fidelity == "faithful" and bool(allowed)
    extra_names: set = set()
    text = ""
    for attempt in range(2):  # 首次 + 最多 1 次 faithful 重试
        parts: List[str] = []
        async for kind, delta in stream_episode_body(
                project, episode, confirmed_characters, fidelity_ctx):
            if kind == "delta":
                parts.append(delta)
        text = "".join(parts)
        if not faithful:
            break
        extra_names = _extract_body_character_names(text) - allowed
        if len(extra_names) <= _FAITHFUL_EXTRA_CHARACTER_LIMIT:
            break
        logger.warning(
            "faithful 忠实度校验：正文出现名单外角色 %s（超过阈值 %d）",
            sorted(extra_names), _FAITHFUL_EXTRA_CHARACTER_LIMIT)
        if attempt == 0:
            continue  # 额外重试 1 次（不阻断主流程）
        logger.warning(
            "faithful 忠实度校验：重试后仍存在名单外角色 %s，已返回", sorted(extra_names))
    if faithful_report is not None:
        faithful_report["extra_characters"] = sorted(extra_names)
    return text


def build_episode_scenes_messages(
    project: Project,
    episode: Episode,
    confirmed_characters: list,
    body_text: str,
) -> List[dict]:
    """构造 episode_scenes 结构化的 LLM 消息（两段式第二段：正文 → 场次 JSON）。

    system 要求仅输出严格 JSON（locations 去重地点清单 + scenes 3~8 场，
    每场含 scene_no/title/action/plot/location/time_of_day/int_ext/sfx/
    emotion/dialogue/duration_sec，dialogue 带 character/character_id/type/
    emotion/line，各场合计时长≈本集时长±10%）；user 注入本集大纲 + 角色
    名单（name+id）+ 已生成正文（截前 8000 字）。

    Args:
        project: 项目对象（预留参数位：结构化提示词暂不消费项目元信息，
            与既有 plan_* 签名风格保持一致）。
        episode: 剧集对象（读取 outline JSONB 大纲字段）。
        confirmed_characters: 已确认角色列表（提供 name+id 名单）。
        body_text: 已生成的文章体正文全文（截 8000 字）。

    Returns:
        List[dict]: OpenAI 格式 messages（system + user 两条）。

    Raises:
        无：纯字符串拼装，属性缺失按「未指定」兜底。
    """
    outline = episode.outline if isinstance(episode.outline, dict) else {}
    lines: List[str] = ["本集大纲："]
    lines.append(f"- 标题：{episode.title}")
    if episode.logline:
        lines.append(f"- 梗概：{episode.logline}")
    if outline.get("core_event"):
        lines.append(f"- 核心事件：{outline['core_event']}")
    if outline.get("emotion_curve"):
        lines.append(f"- 情绪曲线：{outline['emotion_curve']}")
    try:
        duration = int(outline.get("duration_sec") or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration > 0:
        lines.append(f"- 目标时长：{duration} 秒（各场次 duration_sec 合计须≈{duration}±10%）")
    lines.append("角色名单（dialogue.character 必须取自名单，character_id 用对应数字 ID）：")
    for ch in confirmed_characters:
        lines.append(f"- {getattr(ch, 'name', '未命名')}（id={getattr(ch, 'id', '')}）")
    lines.append("已生成正文：\n" + (body_text or "")[:8000])
    return [
        {"role": "system", "content": _EPISODE_SCENES_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _normalize_episode_scenes(data: dict, name_to_id: dict) -> dict:
    """把 LLM 结构化输出规整为 {locations, scenes}（字段容错，内部函数）。

    规整规则：
    - locations：name 必填否则丢弃，按 name 保序去重，字段截长、白名单外
      time_of_day 置 None；
    - scenes：scene_no 重排 1..n（忽略 LLM 编号）；title 缺失按「第 N 场」
      兜底、plot 缺失按 action 兜底；time_of_day/int_ext 白名单外置 None；
      duration_sec 非法置 1 后夹取 1~3600；sfx 逐条 strip 截长；
    - dialogue：line 为空的条目丢弃；type 白名单外归「对白」；character_id
      一律以名单为准按 character 名回填（LLM 给错的以名单为准）。

    Args:
        data: LLM 输出 JSON 解析后的 dict（期望含 locations/scenes 数组）。
        name_to_id: 角色名 → 角色 ID 映射（来自已确认角色名单）。

    Returns:
        dict: {"locations": [...], "scenes": [...]}（scenes 可能为空列表，
            由调用方判定失败进入重试）。

    Raises:
        无：结构异常一律按缺数据处理，不抛出。
    """
    # ---------- locations：按 name 保序去重的地点清单 ----------
    locations: List[dict] = []
    seen_locs: set = set()
    raw_locs = data.get("locations")
    if isinstance(raw_locs, list):
        for item in raw_locs:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in seen_locs:
                continue  # name 必填且不重复
            seen_locs.add(name)
            loc_tod = str(item.get("time_of_day") or "").strip()
            locations.append({
                "name": name[:100],
                "description": str(item.get("description") or "").strip()[:500] or None,
                "time_of_day": loc_tod if loc_tod in _TIME_OF_DAY else None,
            })

    # ---------- scenes：结构化场次（scene_no 重排 1..n） ----------
    raw_scenes = data.get("scenes")
    raw_scenes = raw_scenes if isinstance(raw_scenes, list) else []
    scenes: List[dict] = []
    seq = 0
    for item in raw_scenes:
        if not isinstance(item, dict):
            continue
        seq += 1
        title = str(item.get("title") or "").strip()[:100] or f"第{seq}场"
        action = str(item.get("action") or "").strip()[:2000] or None
        plot = str(item.get("plot") or "").strip()[:2000] or (action or "")
        location = str(item.get("location") or "").strip()[:100] or None
        tod = str(item.get("time_of_day") or "").strip()
        int_ext = str(item.get("int_ext") or "").strip().lower()
        try:
            duration = int(item.get("duration_sec"))
        except (TypeError, ValueError):
            duration = 1
        duration = max(1, min(3600, duration))
        sfx = [str(s).strip()[:50] for s in (item.get("sfx") or []) if str(s or "").strip()]
        # dialogue：逐条容错 + character_id 按名单回填（以名单为准）
        dialogue: List[dict] = []
        raw_dialogue = item.get("dialogue")
        if isinstance(raw_dialogue, list):
            for d in raw_dialogue:
                if not isinstance(d, dict):
                    continue
                line = str(d.get("line") or "").strip()
                if not line:
                    continue  # 台词为空的条目丢弃
                character = str(d.get("character") or "").strip()[:50]
                dtype = str(d.get("type") or "").strip()
                dialogue.append({
                    "character": character,
                    "character_id": name_to_id.get(character) if character else None,
                    "type": dtype if dtype in _DIALOGUE_TYPES else "对白",
                    "emotion": str(d.get("emotion") or "").strip()[:50] or None,
                    "line": line[:2000],
                })
        scenes.append({
            "scene_no": seq,
            "title": title,
            "action": action,
            "plot": plot,
            "location": location,
            "time_of_day": tod if tod in _TIME_OF_DAY else None,
            "int_ext": int_ext if int_ext in _INT_EXT else None,
            "sfx": sfx,
            "emotion": str(item.get("emotion") or "").strip()[:50] or None,
            "dialogue": dialogue,
            "duration_sec": duration,
        })
    return {"locations": locations, "scenes": scenes}


async def plan_episode_scenes(
    project: Project,
    episode: Episode,
    confirmed_characters: list,
    body_text: str,
) -> dict:
    """把文章体正文结构化为 {locations, scenes}：调 LLM → 剥围栏解析 → 规整。

    重试策略同 plan_characters/plan_episodes：最多 3 次尝试，温度依次
    0.8 → 1.0 → 1.0；解析后 scenes 为空视为本轮失败进入下一轮；全部失败
    抛 502。

    Args:
        project: 项目对象（保留参数位，与既有 plan_* 签名风格一致）。
        episode: 剧集对象（大纲 JSONB 提供时长等上下文）。
        confirmed_characters: 已确认角色列表（character_id 回填依据）。
        body_text: 已生成的文章体正文全文（截 8000 字注入提示词）。

    Returns:
        dict: {"locations": [...], "scenes": [...]}（规整后结构：scene_no
            已重排 1..n、character_id 已按名单回填、locations 已按 name 去重）。

    Raises:
        HTTPException: 502 —— 3 次尝试（LLM 异常或解析后无合法场次）均失败。
    """
    messages = build_episode_scenes_messages(project, episode, confirmed_characters, body_text)
    name_to_id = {
        str(getattr(ch, "name", "") or "").strip(): getattr(ch, "id", None)
        for ch in confirmed_characters
    }
    for temperature in _TEMPERATURES:
        try:
            text = await _llm_chat(
                messages, temperature=temperature, agent_name="episode_writer")
            data = json.loads(strip_json_fences(text))
            if isinstance(data, dict):
                result = _normalize_episode_scenes(data, name_to_id)
                if result.get("scenes"):
                    return result
        except (json.JSONDecodeError, ValueError, TypeError):
            # JSON 解析失败：进入下一轮重试
            continue
        except Exception:  # noqa: BLE001 —— LLM 网络/渠道异常同样走重试
            continue
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="正文结构化失败，请重试",
    )


# ---------------------------------------------------------------------------
# script_optimize：P-6 AI 优化四粒度（Task 7）
# ---------------------------------------------------------------------------
# 优化系统提示词（仅输出严格 JSON 的片段化 changes，禁止整篇覆盖）
_OPTIMIZE_SYSTEM_PROMPT = (
    "你是专业的漫剧（动态漫画短剧）剧本优化助手。请在给定的优化范围内，"
    "找出可改进之处并给出优化建议。\n"
    "必须仅输出一个严格合法的 JSON 对象（禁止 markdown 代码块围栏、禁止任何解释文字），"
    "结构如下：\n"
    '{"changes":[{"path":"scenes[0].plot","original":"原文","proposed":"优化后",'
    '"reason":"理由"}]}\n'
    "硬性要求：\n"
    "- 只优化指定范围：未指定范围的字段一律不得出现在 changes 中；\n"
    "- path 必须精确指向被优化字段，格式为 scenes[i].<字段> 或 "
    "scenes[i].dialogue[j].line/emotion（i/j 为从 0 起的数组下标）；\n"
    "- original 必须逐字复制该字段当前值；proposed 为优化后的完整新值（不是差异片段）；\n"
    "- 保持角色姓名与人物设定不变，不得新增、删除或改名角色；\n"
    "- 若范围内已足够优秀、无需优化，返回 {\"changes\":[]}。"
)

# 四粒度优化范围说明（拼接到 system 提示词，约束 changes 覆盖范围）
_OPTIMIZE_SCOPE_DESC = {
    "global": (
        "本次为「全篇优化」：可对任意场次的场次字段（action/plot/title/emotion/"
        "location/time_of_day/int_ext/duration_sec）与对白（line/emotion）提出优化，"
        "请覆盖全篇主要可优化点；path 使用 scenes[i] 下标。"),
    "episode": (
        "本次为「整集优化」：语义同全篇优化，可对任意场次提出优化，"
        "请覆盖全篇主要可优化点；path 使用 scenes[i] 下标。"),
    "segment": (
        "本次为「单场次优化」：仅可优化用户指定的那一个场次，"
        "其他场次的内容一律不得出现在 changes 中。"),
    "field": (
        "本次为「单字段优化」：仅可优化用户指定的一个字段，"
        "其他字段一律不得出现在 changes 中。"),
}

# 优化专属采样温度（覆盖式建议，偏低温度保证输出稳定）
_OPTIMIZE_TEMPERATURE = 0.5

# 优化尝试次数（首次 + 重试 2 次）
_OPTIMIZE_ATTEMPTS = 3


def _collect_character_names(plain_content: dict) -> List[str]:
    """汇总本集出现的角色名（对白 character + 去重保序，内部函数）。

    Args:
        plain_content: 解包后的纯结构（含 scenes 数组）。

    Returns:
        List[str]: 角色名列表（首次出现顺序，去重）；无对白时为空列表。

    Raises:
        无：结构异常按缺数据处理。
    """
    names: List[str] = []
    scenes = plain_content.get("scenes") if isinstance(plain_content, dict) else None
    for scene in (scenes if isinstance(scenes, list) else []):
        if not isinstance(scene, dict):
            continue
        for dlg in (scene.get("dialogue") or []):
            if not isinstance(dlg, dict):
                continue
            name = str(dlg.get("character") or "").strip()
            if name and name not in names:
                names.append(name)
    return names


def _scene_brief(scene: dict, index: int) -> str:
    """场次摘要单行文本（内部函数，供全篇优化提示词使用）。

    Args:
        scene: 场次 dict。
        index: 该场在 scenes 数组中的下标（0 起，与 path 对齐）。

    Returns:
        str: 形如 "[索引 0] 场次1《天台立约》" 的摘要行。

    Raises:
        无：字段缺失按空兜底。
    """
    return f"[索引 {index}] 场次{scene.get('scene_no', index + 1)}《{scene.get('title') or ''}》"


def _scene_full_text(scene: dict) -> str:
    """场次全文文本（内部函数，供优化提示词注入待优化内容）。

    Args:
        scene: 场次 dict（含 action/plot/对白等字段）。

    Returns:
        str: 多行文本（场次各字段 + 逐条对白）。

    Raises:
        无：字段缺失按空兜底。
    """
    lines: List[str] = [
        f"场景序号：{scene.get('scene_no')}",
        f"标题：{scene.get('title') or ''}",
        f"地点：{scene.get('location') or ''}",
        f"时段：{scene.get('time_of_day') or ''}；内外景：{scene.get('int_ext') or ''}",
        f"情绪：{scene.get('emotion') or ''}；时长(秒)：{scene.get('duration_sec')}",
        f"动作/画面：{scene.get('action') or ''}",
        f"剧情概要：{scene.get('plot') or ''}",
    ]
    sfx = scene.get("sfx") or []
    if sfx:
        lines.append(f"音效：{'、'.join(str(s) for s in sfx)}")
    dialogue = scene.get("dialogue") or []
    if dialogue:
        lines.append("对白：")
        for d in dialogue:
            if isinstance(d, dict):
                lines.append(
                    f"- {d.get('character') or '旁白'}（{d.get('emotion') or ''}/"
                    f"{d.get('type') or '对白'}）：{d.get('line') or ''}")
    return "\n".join(lines)


def _find_scene_by_no(plain_content: dict, scene_no) -> Optional[Tuple[int, dict]]:
    """按 scene_no 定位场次并返回（下标, 场次）（内部函数）。

    Args:
        plain_content: 解包后的纯结构。
        scene_no: 目标场次序号。

    Returns:
        Optional[Tuple[int, dict]]: (scenes 数组下标, 场次 dict)；未命中返回 None。

    Raises:
        无：结构异常按未命中处理。
    """
    scenes = plain_content.get("scenes") if isinstance(plain_content, dict) else None
    for index, scene in enumerate(scenes if isinstance(scenes, list) else []):
        if isinstance(scene, dict) and scene.get("scene_no") == scene_no:
            return index, scene
    return None


def build_optimize_messages(
    project: Project,
    episode: Episode,
    plain_content: dict,
    grain: str,
    target=None,
    instruction: Optional[str] = None,
) -> List[dict]:
    """构造 script_optimize 的 LLM 消息（四粒度提示词，中文）。

    system 由 _OPTIMIZE_SYSTEM_PROMPT + 粒度范围说明（grain/target 决定）
    组成，强制仅输出严格 JSON 的片段化 changes 且仅覆盖指定范围；user 注入
    项目元信息 + 忠实度约束句（build_fidelity_directive 统一构造）+
    本集大纲 + 角色名单 + 待优化范围内容：
    - grain=segment：仅给 target（scene_no）对应场次的全文；
    - grain=field：仅给 target（字段路径）的当前值；
    - grain=global/episode：给 scenes 摘要 + 全量场次文本（changes 需覆盖全篇）。

    Args:
        project: 项目对象（genre/style/fidelity 等元信息）。
        episode: 剧集对象（title/logline 列 + outline JSONB 大纲）。
        plain_content: 解包后的本集纯结构（title/logline/scenes 等）。
        grain: 优化粒度 global/episode/segment/field。
        target: 粒度目标：segment 为场次序号（scene_no），field 为字段路径
            字符串；global/episode 忽略。
        instruction: 可选补充指令（用户对优化方向的额外要求）。

    Returns:
        List[dict]: OpenAI 格式 messages（system + user 两条）。

    Raises:
        无：纯字符串拼装，属性缺失按「未指定」兜底。
    """
    grain_key = grain if grain in _OPTIMIZE_SCOPE_DESC else "global"
    outline = episode.outline if isinstance(episode.outline, dict) else {}
    fidelity = project.fidelity or "free"
    lines: List[str] = [
        f"项目体裁：{project.genre or '未指定'}；视觉风格：{project.style or '未指定'}；"
        f"忠实度档位：{fidelity}。",
    ]
    # RD-01 忠实度约束句（差异化策略函数统一构造）
    lines.append(build_fidelity_directive(project))
    lines.append("本集大纲：")
    lines.append(f"- 标题：{episode.title}")
    if episode.logline:
        lines.append(f"- 梗概：{episode.logline}")
    if outline.get("core_event"):
        lines.append(f"- 核心事件：{outline['core_event']}")
    if outline.get("emotion_curve"):
        lines.append(f"- 情绪曲线：{outline['emotion_curve']}")
    # 角色名单：大纲角色 + 对白角色，去重保序
    names: List[str] = []
    for name in (outline.get("characters") or []):
        text = str(name or "").strip()
        if text and text not in names:
            names.append(text)
    for name in _collect_character_names(plain_content):
        if name not in names:
            names.append(name)
    lines.append(
        "角色名单（优化时保持角色姓名与设定不变）：" + ("、".join(names) if names else "（无）"))

    # ---------- 待优化范围内容 ----------
    if grain_key == "field":
        current = get_value_by_path(plain_content, target, PATH_MISSING)
        lines.append(f"待优化字段（仅此字段可出现在 changes 中）：{target}")
        lines.append(f"当前值：{'' if current is PATH_MISSING or current is None else current}")
    elif grain_key == "segment":
        found = _find_scene_by_no(plain_content, target)
        if found is not None:
            index, scene = found
            lines.append(f"待优化场次（仅该场次可出现在 changes 中）：{_scene_brief(scene, index)}")
            lines.append(_scene_full_text(scene))
        else:
            lines.append(f"待优化场次：scene_no={target}（未找到）")
    else:
        scenes = plain_content.get("scenes") if isinstance(plain_content, dict) else None
        scenes = scenes if isinstance(scenes, list) else []
        lines.append("全篇场次摘要：")
        for index, scene in enumerate(scenes):
            if isinstance(scene, dict):
                lines.append(_scene_brief(scene, index))
        lines.append("全篇场次文本：")
        for index, scene in enumerate(scenes):
            if isinstance(scene, dict):
                lines.append(f"[scenes[{index}]]\n{_scene_full_text(scene)}")
    if instruction:
        lines.append(f"补充指令：{instruction}")

    system = _OPTIMIZE_SYSTEM_PROMPT + "\n优化范围：" + _OPTIMIZE_SCOPE_DESC[grain_key]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _normalize_optimize_changes(
    data: dict, plain_content: dict
) -> Tuple[List[dict], int]:
    """把 LLM 优化输出规整为 changes 列表（path 安全校验，内部函数）。

    逐条校验：
    - path 必须匹配白名单（is_optimizable_path：scenes[i].<场次字段> 或
      scenes[i].dialogue[j].line/emotion），否则丢弃；
    - path 必须能解析到 plain_content 中的真实路径（下标不越界），否则丢弃；
    - proposed 为空（strip 后）丢弃；
    - original 一律以当前库值覆盖（LLM 输出的原文字段不可信，避免误改）。

    Args:
        data: LLM 输出 JSON 解析后的 dict（期望含 changes 数组）。
        plain_content: 解包后的本集纯结构（path 校验与 original 覆盖依据）。

    Returns:
        Tuple[List[dict], int]: (合法 changes 列表, 丢弃计数)。合法元素结构
            为 {path, original, proposed, reason}。

    Raises:
        无：结构异常一律按丢弃处理，不抛出。
    """
    raw = data.get("changes")
    if not isinstance(raw, list):
        return [], 0
    result: List[dict] = []
    dropped = 0
    for item in raw:
        if not isinstance(item, dict):
            dropped += 1
            continue
        path = str(item.get("path") or "").strip()
        if not is_optimizable_path(path):
            dropped += 1  # path 不在白名单
            continue
        current = get_value_by_path(plain_content, path, PATH_MISSING)
        if current is PATH_MISSING:
            dropped += 1  # 路径越界/不存在于当前正文
            continue
        proposed = str(item.get("proposed") or "").strip()
        if not proposed:
            dropped += 1  # 空建议无意义
            continue
        result.append({
            "path": path,
            "original": "" if current is None else str(current),  # 以当前库值为准
            "proposed": proposed,
            "reason": str(item.get("reason") or "").strip()[:500],
        })
    return result, dropped


async def optimize_episode_content(
    project: Project,
    episode: Episode,
    plain_content: dict,
    grain: str,
    target=None,
    instruction: Optional[str] = None,
) -> List[dict]:
    """生成指定粒度的优化建议 changes：调 LLM → 剥围栏解析 → 安全规整。

    调 _llm_chat（agent_name=script_optimize, json_mode=True,
    temperature=0.5），最多 3 次尝试（重试 2 次）；解析出的 changes 经
    _normalize_optimize_changes 做 path 白名单 + 真实性校验，非法路径丢弃
    （因此返回条数可能少于 LLM 输出条数）。3 次均无合法 JSON 时抛 502。

    Args:
        project: 项目对象。
        episode: 剧集对象。
        plain_content: 解包后的本集纯结构。
        grain: 优化粒度 global/episode/segment/field。
        target: segment 为场次序号，field 为字段路径。
        instruction: 可选补充指令。

    Returns:
        List[dict]: 规整后的 changes 列表（元素结构 {path, original,
            proposed, reason}；无优化点时为 []）。

    Raises:
        HTTPException: 502 —— 3 次尝试（LLM 异常或 JSON 解析失败）均未取得
            合法 JSON 输出。
    """
    messages = build_optimize_messages(
        project, episode, plain_content, grain, target, instruction)
    for _attempt in range(_OPTIMIZE_ATTEMPTS):
        try:
            text = await _llm_chat(
                messages, temperature=_OPTIMIZE_TEMPERATURE, agent_name="script_optimize")
            data = json.loads(strip_json_fences(text))
            if isinstance(data, dict) and isinstance(data.get("changes"), list):
                changes, _dropped = _normalize_optimize_changes(data, plain_content)
                return changes
        except (json.JSONDecodeError, ValueError, TypeError):
            # JSON 解析失败：进入下一轮重试
            continue
        except Exception:  # noqa: BLE001 —— LLM 网络/渠道异常同样走重试
            continue
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="剧本优化失败，请重试",
    )


# ---------------------------------------------------------------------------
# setting_planner：P2 故事设定阶段 Agent（redesign-script-workspace Task 2）
# ---------------------------------------------------------------------------
# setting_planner 系统提示词（要求仅输出严格 JSON 的故事设定四段；
# 人物部分提示词骨架与 character_planner 同口径：name/role_type/age/
# personality/appearance/motivation/arc/relations）
_SETTING_PLANNER_SYSTEM_PROMPT = (
    "你是专业的漫剧（动态漫画短剧）故事设定师。请根据项目信息与用户创意，"
    "为本剧构建故事设定：一句话故事（logline）、核心冲突（core_conflict）、"
    "世界观（worldview）与主要人物班底。\n"
    "必须仅输出一个严格合法的 JSON 对象（禁止 markdown 代码块围栏、禁止任何解释文字），"
    "结构如下：\n"
    '{"logline":"一句话故事（不超过80字，写清主角与核心事件）",'
    '"core_conflict":"核心冲突描述（主角面临的根本矛盾与对抗，不超过300字）",'
    '"worldview":"世界观设定（故事发生的时代/地点/规则/氛围，不超过600字）",'
    '"characters":[{"name":"角色名","role_type":"lead","age":"约30岁",'
    '"personality":"性格设定","appearance":"外观设定","motivation":"行为动机",'
    '"arc":"人物弧光","relations":[{"target":"对方角色名","relation":"关系描述"}]}]}\n'
    "要求：4~8 个角色；主角（role_type 取 lead）至少 1 名；"
    "role_type 只能取 lead/support/villain/extra；"
    "relations 的 target 必须指向班底中的其他角色名；全部使用中文。"
)

# 设定三段截长上限（story_setting 为 JSONB 无列宽限制，截长防异常长输出）
_SETTING_LOGLINE_MAX = 200
_SETTING_CONFLICT_MAX = 1000
_SETTING_WORLDVIEW_MAX = 2000


def build_setting_planner_messages(
    project: Project,
    raw_text: str,
    instruction: Optional[str] = None,
) -> List[dict]:
    """构造 setting_planner 的 LLM 消息（中文提示词，注入项目元信息）。

    user 内容依次拼接：项目体裁/风格/忠实度档位/画幅比例 → 忠实度约束句
    （RD-01 差异化策略：build_fidelity_directive 统一构造 faithful/outline/
    free 约束）→ 用户创意（截前 6000 字，idea 一句话创意或 novel 章节上下文
    由调用方取源决定）→ 可选补充指令（重生成时传当前设定摘要与调整方向）。

    Args:
        project: 项目对象（genre/style/fidelity/aspect_ratio）。
        raw_text: 用户原始输入文本（创意/小说摘要上下文等）。
        instruction: 可选补充指令（重生成方向或对设定的额外要求）。

    Returns:
        List[dict]: OpenAI 格式 messages（system + user 两条）。

    Raises:
        无：纯字符串拼装，属性缺失按「未指定」兜底。
    """
    fidelity = project.fidelity or "free"
    lines: List[str] = [
        f"项目体裁：{project.genre or '未指定'}；视觉风格：{project.style or '未指定'}；"
        f"忠实度档位：{fidelity}；画幅比例：{project.aspect_ratio}。",
    ]
    # RD-01 忠实度约束句（差异化策略函数统一构造）
    lines.append(build_fidelity_directive(project))
    # 用户创意（截 6000 字，防止超长输入）
    lines.append(f"用户创意：\n{(raw_text or '')[:6000]}")
    if instruction:
        lines.append(f"补充指令：{instruction}")
    return [
        {"role": "system", "content": _SETTING_PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _normalize_story_setting(data: dict) -> dict:
    """把 LLM 输出解析结果规整为故事设定结构（字段容错，内部函数）。

    规整规则：
    - 设定三段（logline/core_conflict/worldview）逐段转 str、strip、截长
      （200/1000/2000），异常类型按空串兜底；
    - 人物部分复用 _normalize_character_candidates（name 必填否则丢弃、
      role_type 白名单外归 support、relations 列表容错、字符串截长）。

    Args:
        data: LLM 输出 JSON 解析后的 dict（期望含 logline/core_conflict/
            worldview/characters）。

    Returns:
        dict: {"logline": str, "core_conflict": str, "worldview": str,
            "characters": List[dict]}（结构见各归一化函数）。

    Raises:
        无：结构异常一律按缺数据处理，不抛出。
    """
    return {
        "logline": str(data.get("logline") or "").strip()[:_SETTING_LOGLINE_MAX],
        "core_conflict": str(data.get("core_conflict") or "").strip()[:_SETTING_CONFLICT_MAX],
        "worldview": str(data.get("worldview") or "").strip()[:_SETTING_WORLDVIEW_MAX],
        "characters": _normalize_character_candidates(data),
    }


async def plan_story_setting(
    project: Project,
    raw_text: str,
    instruction: Optional[str] = None,
) -> dict:
    """生成故事设定：调 LLM → 剥围栏解析 → 结构规整，失败重试 2 次。

    重试策略同 plan_characters：共最多 3 次尝试，温度依次 0.8 → 1.0 → 1.0；
    解析规整后设定三段（logline/core_conflict/worldview）任一为空视为本轮
    失败进入下一轮（三段为故事设定的核心产物，缺一不可）；全部失败抛 502。
    人物候选可能为空（不作为失败条件，由调用方按业务判定）。

    Args:
        project: 项目对象（供提示词注入体裁/风格/忠实度等元信息）。
        raw_text: 用户原始输入文本（创意/小说摘要上下文等，截 6000 字）。
        instruction: 可选补充指令（重生成时传当前设定摘要与调整方向）。

    Returns:
        dict: 规整后的故事设定 {"logline", "core_conflict", "worldview",
            "characters"}（characters 结构见 _normalize_character_candidates）。

    Raises:
        HTTPException: 502 —— 3 次尝试（LLM 异常、JSON 解析失败或设定三段
            不全）均失败。
    """
    messages = build_setting_planner_messages(project, raw_text, instruction)
    for temperature in _TEMPERATURES:
        try:
            text = await _llm_chat(
                messages, temperature=temperature, agent_name="setting_planner")
            data = json.loads(strip_json_fences(text))
            if isinstance(data, dict):
                result = _normalize_story_setting(data)
                # 设定三段为故事设定核心产物：任一为空即本轮失败重试
                if result["logline"] and result["core_conflict"] and result["worldview"]:
                    return result
        except (json.JSONDecodeError, ValueError, TypeError):
            # JSON 解析失败：进入下一轮重试
            continue
        except Exception:  # noqa: BLE001 —— LLM 网络/渠道异常同样走重试
            continue
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="故事设定生成失败，请重试",
    )


# ---------------------------------------------------------------------------
# script_assist：P4 对话式改写通道（redesign-script-workspace Task 5）
# ---------------------------------------------------------------------------
# 改写系统提示词（复用 build_optimize_messages 的「仅变更片段 JSON」约束口径：
# 仅输出片段化 changes，禁止整篇覆盖；额外携带 summary 供前端回执展示）
_ASSIST_SYSTEM_PROMPT = (
    "你是专业的漫剧（动态漫画短剧）剧本共创助手。请根据用户的自然语言指令，"
    "对给定的当前内容提出可执行的改写建议（片段化，禁止整篇覆盖）。\n"
    "必须仅输出一个严格合法的 JSON 对象（禁止 markdown 代码块围栏、禁止任何解释文字），"
    "结构如下：\n"
    '{"changes":[{"path":"logline","original":"该字段当前原文",'
    '"proposed":"改写后的完整新值","reason":"改写理由"}],'
    '"summary":"一句话概括本轮改动"}\n'
    "硬性要求：\n"
    "- path 只能取「允许修改的字段清单」中的字段名，清单外的字段一律不得出现在 changes 中；\n"
    "- original 必须逐字复制该字段当前值；proposed 为改写后的完整新值（不是差异片段）；\n"
    "- characters 字段（若在清单中）的 proposed 为以「、」分隔的角色名清单，"
    "角色名必须取自当前名单或已确认角色；\n"
    "- 仅提出确需修改的字段，保持与现有内容风格连贯；若无需修改，返回"
    " {\"changes\":[],\"summary\":\"\"}；全部使用中文。"
)

# 各作用域的改写范围说明（拼接到 user 消息，约束 changes 覆盖范围）
_ASSIST_SCOPE_DESC = {
    "setting": (
        "故事设定改写：仅允许修改一句话故事（logline）/ 核心冲突（core_conflict）/ "
        "世界观（worldview）三个字段。"),
    "episode_outline": (
        "单集大纲改写：仅允许修改标题（title）/ 一句话梗概（logline）/ "
        "核心事件（core_event）/ 出场角色（characters，顿号分隔名单）/ "
        "情绪走向（emotion_curve）五个字段。"),
}

# 各作用域允许改写的字段白名单（顺序即提示词展示顺序；公开常量，API 层的
# apply 落库 path 校验与本处提示词约束共用同一事实来源，避免双处维护漂移）
ASSIST_PATH_WHITELIST = {
    "setting": ("logline", "core_conflict", "worldview"),
    "episode_outline": ("title", "logline", "core_event", "characters", "emotion_curve"),
}

# 改写专属采样温度（对齐 optimize：覆盖式建议用偏低温度保证输出稳定）
_ASSIST_TEMPERATURE = 0.5

# 改写尝试次数（首次 + 重试 2 次，与 _OPTIMIZE_ATTEMPTS 同口径）
_ASSIST_ATTEMPTS = 3

# summary 概括截长上限
_ASSIST_SUMMARY_MAX = 200


def build_assist_messages(
    scope: str,
    context_payload: dict,
    instruction: str,
) -> List[dict]:
    """构造 script_assist 的 LLM 消息（中文提示词，注入当前库值与用户指令）。

    system 要求仅输出严格 JSON 的片段化 changes（path 限定白名单字段名、
    original 逐字复制当前值、禁止整篇覆盖，口径与 build_optimize_messages
    一致）；user 依次拼接：改写作用域说明 → 允许修改的字段清单 → 当前内容
    （逐字段列出，characters 名单以顿号连接）→ 用户改写指令。

    Args:
        scope: 改写作用域（仅支持 setting/episode_outline；episode_body
            作用域由调用方直接走 optimize_episode_content 通道）。
        context_payload: 当前库值构成的扁平 dict（键为字段名，如
            {"logline": ..., "core_conflict": ..., "worldview": ...}）。
        instruction: 用户自然语言改写指令（必填，接口层已校验 1~500 字）。

    Returns:
        List[dict]: OpenAI 格式 messages（system + user 两条）。

    Raises:
        KeyError: scope 不在 ASSIST_PATH_WHITELIST 中（调用方传参错误）。
    """
    allowed = ASSIST_PATH_WHITELIST[scope]
    lines: List[str] = [
        f"改写作用域：{_ASSIST_SCOPE_DESC[scope]}",
        "允许修改的字段清单（path 只能取其中的字段名）：" + "、".join(allowed),
        "当前内容：",
    ]
    for key, value in (context_payload or {}).items():
        if isinstance(value, list):
            text = "、".join(str(v) for v in value)
        else:
            text = "" if value is None else str(value)
        lines.append(f"- {key}：{text}")
    lines.append(f"用户指令：{(instruction or '').strip()}")
    return [
        {"role": "system", "content": _ASSIST_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _normalize_assist_changes(
    data: dict, context_payload: dict, allowed_paths: tuple
) -> Tuple[List[dict], int]:
    """把 LLM 改写输出规整为 changes 列表（path 白名单 + 当前值覆盖，内部函数）。

    逐条校验：
    - path 必须在 allowed_paths 白名单内，否则丢弃；
    - path 必须能解析到 context_payload 中的真实字段（get_value_by_path），
      否则丢弃；
    - proposed 为空（strip 后）丢弃；
    - original 一律以当前库值覆盖（LLM 输出的原文字段不可信，避免误改；
      列表值如 characters 名单以顿号连接为展示文本）。

    Args:
        data: LLM 输出 JSON 解析后的 dict（期望含 changes 数组）。
        context_payload: 当前库值构成的扁平 dict（path 校验与 original
            覆盖依据）。
        allowed_paths: 允许的字段名元组（ASSIST_PATH_WHITELIST[scope]）。

    Returns:
        Tuple[List[dict], int]: (合法 changes 列表, 丢弃计数)。合法元素结构
            为 {path, original, proposed, reason}。

    Raises:
        无：结构异常一律按丢弃处理，不抛出。
    """
    raw = data.get("changes")
    if not isinstance(raw, list):
        return [], 0
    result: List[dict] = []
    dropped = 0
    for item in raw:
        if not isinstance(item, dict):
            dropped += 1
            continue
        path = str(item.get("path") or "").strip()
        if path not in allowed_paths:
            dropped += 1  # path 不在作用域白名单
            continue
        current = get_value_by_path(context_payload, path, PATH_MISSING)
        if current is PATH_MISSING:
            dropped += 1  # 字段不存在于当前内容
            continue
        if isinstance(current, list):  # 名单类字段：顿号连接为展示文本
            original_text = "、".join(str(v) for v in current)
        else:
            original_text = "" if current is None else str(current)
        proposed = str(item.get("proposed") or "").strip()
        if not proposed:
            dropped += 1  # 空建议无意义
            continue
        result.append({
            "path": path,
            "original": original_text,  # 以当前库值为准
            "proposed": proposed,
            "reason": str(item.get("reason") or "").strip()[:500],
        })
    return result, dropped


async def assist_rewrite(
    scope: str,
    context_payload: dict,
    instruction: str,
) -> Tuple[List[dict], str]:
    """按用户指令对当前内容提出片段化改写建议：调 LLM → 剥围栏解析 → 安全规整。

    调 _llm_chat（agent_name=script_assist, json_mode=True,
    temperature=0.5），最多 3 次尝试（重试 2 次）；解析出的 changes 经
    _normalize_assist_changes 做 path 白名单 + 真实性校验，非法路径丢弃
    （返回条数可能少于 LLM 输出条数）。3 次均无合法 JSON 时抛 502。
    **不落库**：仅返回规整后的片段与概括，落库由调用方在 apply 侧完成。

    Args:
        scope: 改写作用域（仅支持 setting/episode_outline）。
        context_payload: 当前库值构成的扁平 dict。
        instruction: 用户自然语言改写指令（必填）。

    Returns:
        Tuple[List[dict], str]: (规整后的 changes 列表, LLM 改动概括文本；
            changes 元素结构 {path, original, proposed, reason}，无建议时
            为空列表)。

    Raises:
        HTTPException: 502 —— 3 次尝试（LLM 异常或 JSON 解析失败）均未取得
            合法 JSON 输出；KeyError —— scope 不在 ASSIST_PATH_WHITELIST 中。
    """
    messages = build_assist_messages(scope, context_payload, instruction)
    allowed = ASSIST_PATH_WHITELIST[scope]
    for _attempt in range(_ASSIST_ATTEMPTS):
        try:
            text = await _llm_chat(
                messages, temperature=_ASSIST_TEMPERATURE, agent_name="script_assist")
            data = json.loads(strip_json_fences(text))
            if isinstance(data, dict) and isinstance(data.get("changes"), list):
                changes, _dropped = _normalize_assist_changes(
                    data, context_payload, allowed)
                summary = str(data.get("summary") or "").strip()[:_ASSIST_SUMMARY_MAX]
                return changes, summary
        except (json.JSONDecodeError, ValueError, TypeError):
            # JSON 解析失败：进入下一轮重试
            continue
        except Exception:  # noqa: BLE001 —— LLM 网络/渠道异常同样走重试
            continue
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="对话改写失败，请重试",
    )


# ---------------------------------------------------------------------------
# quote_rewrite：选中文本优化通道（工作台选中文本「添加对话中」→ 引用 + 优化
# 方向 → 输出可复制的优化文本；纯文本不落库，粘贴由用户手动完成）
# ---------------------------------------------------------------------------
_QUOTE_REWRITE_SYSTEM_PROMPT = (
    "你是专业的漫剧（动态漫画短剧）剧本共创助手。用户在剧本工作台（故事设定/"
    "角色设定/分集大纲/剧本正文）中选中了一段文本并给出了优化方向，"
    "请按优化方向对选中的原文进行优化。\n"
    "必须仅输出优化后的文本本身（纯文本）：禁止 markdown 代码块围栏、禁止任何"
    "解释性文字、禁止「优化后：」之类的前缀或后缀。\n"
    "要求：保持与原文语言风格和叙述口吻一致；优化方向未涉及的字句尽量原样保留；"
    "篇幅与原文相当（优化方向明确要求增删时除外）；与项目体裁/风格匹配；"
    "全部使用中文。"
)

# 引用优化作用域的语境标签（拼接到 user 消息，帮助 LLM 理解原文所处层次）
_QUOTE_SCOPE_LABELS = {
    "setting": "故事设定",
    "characters": "角色设定",
    "episode_outline": "分集大纲",
    "episode_body": "剧本正文",
}

# 引用优化采样温度（保守起见首轮 0.5，重试升到 0.7 增加多样性）
_QUOTE_TEMPERATURES = (0.5, 0.7, 0.7)


def build_quote_rewrite_messages(
    project: Project,
    scope: str,
    quote_text: str,
    instruction: str,
    target_episode_id: Optional[int] = None,
) -> List[dict]:
    """构造选中文本优化的 LLM 消息（引用原文 + 层次语境 + 项目元信息 + 方向）。

    system 要求仅输出优化后的纯文本；user 依次拼接：项目体裁/风格/忠实度 →
    选中文本来源（作用域标签 + 可选第 N 集标注）→ 引用原文（截 8000 字）→
    用户优化方向。

    Args:
        project: 项目对象（genre/style/fidelity）。
        scope: 选中文本的作用域（setting/characters/episode_outline/
            episode_body，未知值按「剧本内容」兜底）。
        quote_text: 用户选中的原文（接口层已校验非空）。
        instruction: 用户优化方向描述（接口层已校验非空）。
        target_episode_id: 目标剧集 ID（大纲/正文作用域下用于「第 N 集」标注，
            仅展示用；未知 ID 不标注）。

    Returns:
        List[dict]: OpenAI 格式 messages（system + user 两条）。

    Raises:
        无：纯字符串拼装，属性缺失按「未指定」兜底。
    """
    scope_label = _QUOTE_SCOPE_LABELS.get(scope, "剧本内容")
    episode_suffix = ""
    if target_episode_id is not None:
        episode_suffix = f"（第 {target_episode_id} 集）"
    lines: List[str] = [
        f"项目体裁：{project.genre or '未指定'}；视觉风格：{project.style or '未指定'}；"
        f"忠实度档位：{project.fidelity or 'free'}。",
        f"选中文本来源：{scope_label}{episode_suffix}",
        "选中的原文：",
        (quote_text or "").strip()[:8000],
        f"优化方向：{(instruction or '').strip()}",
    ]
    return [
        {"role": "system", "content": _QUOTE_REWRITE_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _normalize_quote_output(text: str) -> str:
    """把 LLM 优化输出规整为纯文本（内部函数）。

    处理顺序：剥代码块围栏（strip_json_fences 兼容纯文本）→ strip；若剥完
    恰好是 JSON 对象（模型不听话输出了 JSON），尝试取 text/optimized/content
    字段，取不到则按原样返回。

    Args:
        text: LLM 原始输出。

    Returns:
        str: 规整后的优化文本（strip 后非空由调用方校验）。

    Raises:
        无：结构异常按原样返回。
    """
    cleaned = (strip_json_fences(text or "") or "").strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                for key in ("text", "optimized", "optimized_text", "content"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        except json.JSONDecodeError:
            pass
    return cleaned


async def rewrite_quote(
    project: Project,
    scope: str,
    quote_text: str,
    instruction: str,
    target_episode_id: Optional[int] = None,
) -> str:
    """选中文本优化：依据引用原文与优化方向生成可复制的优化文本。

    调 _llm_chat（agent_name=script_assist，json_mode=False 纯文本通道），
    温度序列 0.5 → 0.7 → 0.7 最多 3 次尝试；输出经 _normalize_quote_output
    规整（剥围栏 + JSON 误输出兜底），3 次均空时抛 502。**不落库**：优化
    文本由前端展示并复制，用户自行粘贴回对应位置。

    Args:
        project: 项目对象（genre/style/fidelity）。
        scope: 选中文本的作用域（setting/characters/episode_outline/episode_body）。
        quote_text: 用户选中的原文。
        instruction: 用户优化方向描述。
        target_episode_id: 目标剧集 ID（仅用于语境标注，可选）。

    Returns:
        str: 优化后的文本（strip 后非空）。

    Raises:
        HTTPException: 502 —— 3 次尝试（LLM 异常或输出为空）均失败。
    """
    messages = build_quote_rewrite_messages(
        project, scope, quote_text, instruction, target_episode_id)
    for temperature in _QUOTE_TEMPERATURES:
        try:
            text = await _llm_chat(
                messages, temperature=temperature,
                agent_name="script_assist", json_mode=False)
            optimized = _normalize_quote_output(text)
            if optimized:
                return optimized
        except Exception:  # noqa: BLE001 —— LLM 网络/渠道异常同样走重试
            continue
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="优化生成失败，请重试",
    )
