# -*- coding: utf-8 -*-
"""MangaFlow 剧本创作范式流水线端到端冒烟 + 性能基线脚本（文件级说明）。

面向「剧本创作模块范式级重构」（bend: rebuild-script-pipeline）Task 12 第一部分：
1. 端到端冒烟：用 httpx.ASGITransport 进程内直连 FastAPI 应用（不监听端口、不
   触发 lifespan），配合 monkeypatch 模块级 LLM 挂点（app.agents.script_pipeline
   的 _llm_chat / _llm_chat_stream 返回固定角色/大纲/正文/优化 JSON），保证
   **完全确定性**地串起四阶段范式流水线：
   idea → characters → outline → body，再覆盖体检 / 抽取 / 影响面 / 导出 /
   子图重连状态等收口接口；
2. 性能基线：对核心只读/轻写接口做本机进程内计时（每接口 3 次取中位数），
   记录本地 dev 口径的响应时间（非生产压测结论）。

运行方式（须用 backend venv 的 python，脚本会自动 chdir 到 backend）：
    d:\\Code_Work\\MangaFlow\\backend\\.venv\\Scripts\\python.exe .run\\smoke_script_pipeline.py
退出码：0 全部通过；非 0 表示存在失败步骤。

工作台改造（redesign-script-workspace）冒烟覆盖（在原四阶段主链上插入）：
1. 步骤 2.5 故事设定域：POST setting/generate → GET setting → PUT setting
   （source=user）→ POST assist 预览（不落库断言）→ assist/apply 采纳
   （AI 协作文本落库）→ POST setting/regenerate；
2. 步骤 4.5 单集大纲 AI 重生成（title「（重生成）」标记 + body_exists=false
   + sequence 不变）与阶段锁定负向断言（正文阶段 setting/generate 409
   「正文已开始生成，故事设定已锁定」）；
3. 步骤 5.5 assist 正文作用域（复用 optimize 预览/落库通道，version 递增）
   与步骤 5.6 漫剧格式渲染数据契约断言（scenes 的 action/location/
   time_of_day/int_ext、dialogue 的 type/emotion/character、locations 非空）；
4. LLM 桩新增 setting_planner / script_assist 分支；episode_planner 同时
   服务整卷分集与单集重生成，按重生成 system 提示词固定文案「只重写第」
   判别路由；
5. 性能基线新增 GET /script/setting 与 POST /script/assist（预览）两项。

数据隔离：全程使用独立测试库 mangaflow_test（幂等建库 + create_all），
**绝不触碰主库 mangaflow 的业务数据**；子图检查点用 InMemorySaver，
不读写任何数据库检查点表。
"""

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 路径准备：脚本位于 <root>/.run/，须把 backend 加入 import 路径并 chdir 到
# backend（保证 pydantic-settings 读到 backend/.env、相对 storage 路径正确）。
# 注意：chdir 必须在 import 项目模块之前完成。
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _ROOT / "backend"
sys.path.insert(0, str(_BACKEND))

import os  # noqa: E402

os.chdir(_BACKEND)

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings  # noqa: E402

# 导入全部 ORM 模型，确保 create_all 发现全部业务表
import app.models  # noqa: F401,E402
from app.core.database import Base, get_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402

# 被测/被替换模块
import app.agents.script_pipeline as sp_mod  # noqa: E402
import app.api.script_pipeline as sp_api_mod  # noqa: E402
import app.services.script_service as ss_mod  # noqa: E402
from app.agents.script_pipeline_graph import build_script_pipeline_subgraph  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 测试库名（与主库 mangaflow 隔离；由主库连接串派生）
TEST_DB_NAME = "mangaflow_test"
TEST_DB_URL = make_url(settings.database_url).set(database=TEST_DB_NAME).render_as_string(
    hide_password=False
)
ADMIN_DB_URL = settings.admin_database_url

# 冒烟固定输入（idea 模式一句话创意）
_IDEA_TEXT = "一句话创意：两个少女约定一起成为漫画家，在天台立下共同追梦的约定。"

# 每接口性能测量次数（取中位数）
_PERF_ROUNDS = 3

# 失败步骤累积（结尾汇总，决定退出码）
_FAILS: List[str] = []
# 性能基线结果（结尾打印，供文档回填）
_PERF: List[Tuple[str, float, List[float], str]] = []


# ---------------------------------------------------------------------------
# 日志与断言工具
# ---------------------------------------------------------------------------
def log(message: str) -> None:
    """打印带时间戳的日志行。

    Args:
        message: 日志文本。

    Returns:
        None。

    Raises:
        无。
    """
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    """记录单步断言结果（打印 [OK]/[FAIL] 并累积失败项）。

    Args:
        name: 步骤说明。
        cond: 断言是否通过。
        detail: 附加信息（失败时更有价值）。

    Returns:
        None。

    Raises:
        无。
    """
    tag = "OK  " if cond else "FAIL"
    log(f"[{tag}] {name}{(' | ' + detail) if detail else ''}")
    if not cond:
        _FAILS.append(f"{name}{(' | ' + detail) if detail else ''}")


def parse_sse(raw_text: str) -> List[Tuple[str, Optional[Dict[str, Any]]]]:
    """把 SSE 完整响应文本解析为 (event, data) 列表。

    Args:
        raw_text: SSE 响应全文（形如 "event: x\\ndata: {...}\\n\\n"）。

    Returns:
        List[Tuple[str, Optional[dict]]]: 事件名与 JSON 负载（解析失败为 None）。

    Raises:
        无：data 解析失败时负载置 None，不抛异常。
    """
    events: List[Tuple[str, Optional[Dict[str, Any]]]] = []
    for block in raw_text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_name: Optional[str] = None
        data_lines: List[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if event_name is None:
            continue
        payload: Optional[Dict[str, Any]] = None
        if data_lines:
            try:
                parsed = json.loads("\n".join(data_lines))
                payload = parsed if isinstance(parsed, dict) else None
            except (json.JSONDecodeError, ValueError):
                payload = None
        events.append((event_name, payload))
    return events


# ---------------------------------------------------------------------------
# 测试库准备（复刻 tests/conftest.py 口径，绝不触碰主库业务数据）
# ---------------------------------------------------------------------------
async def ensure_test_database() -> AsyncEngine:
    """幂等创建测试库 mangaflow_test 与全部业务表，返回绑定测试库的异步引擎。

    步骤：连 postgres 系统库（AUTOCOMMIT）按需 CREATE DATABASE；
    对测试库 create_all（幂等）+ 执行存量表幂等补列清单。

    Returns:
        AsyncEngine: 绑定测试库的异步引擎。

    Raises:
        Exception: PostgreSQL 不可达或建库/建表失败时向上抛出。
    """
    admin_engine = create_async_engine(
        ADMIN_DB_URL, isolation_level="AUTOCOMMIT", pool_pre_ping=True
    )
    try:
        async with admin_engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DB_NAME},
            )
            if not exists:
                await conn.execute(
                    text(f'CREATE DATABASE "{TEST_DB_NAME}" ENCODING \'UTF8\'')
                )
    finally:
        await admin_engine.dispose()

    engine = create_async_engine(TEST_DB_URL, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        from app.core.init_db import MISSING_COLUMN_DDL_STATEMENTS

        for _table, ddl in MISSING_COLUMN_DDL_STATEMENTS:
            await conn.execute(text(ddl))
    return engine


# ---------------------------------------------------------------------------
# LLM 桩：按 agent_name 路由返回固定 JSON（确定性）
# ---------------------------------------------------------------------------
def _characters_payload() -> str:
    """构造角色班底 LLM 输出 JSON（2 角色：lead + support）。

    Returns:
        str: {"characters": [...]} 的 JSON 文本。

    Raises:
        无。
    """
    return json.dumps({
        "characters": [
            {"name": "林小满", "role_type": "lead", "age": "约17岁",
             "personality": "倔强善良，遇挫不折", "appearance": "短发少女，戴红色发卡",
             "motivation": "和挚友一起成为漫画家", "arc": "从畏缩退让到坚定追梦",
             "relations": [{"target": "陈默", "relation": "漫画搭档"}]},
            {"name": "陈默", "role_type": "support", "age": "约18岁",
             "personality": "沉稳寡言，内心炽热", "appearance": "高瘦少年，黑框眼镜",
             "motivation": "守护小满的梦想", "arc": "从旁观者到并肩者",
             "relations": [{"target": "林小满", "relation": "漫画搭档"}]},
        ]
    }, ensure_ascii=False)


def _episodes_payload() -> str:
    """构造单集分集大纲 LLM 输出 JSON。

    Returns:
        str: {"episodes": [1 集]} 的 JSON 文本（时长合计对齐项目 180s）。

    Raises:
        无。
    """
    return json.dumps({"episodes": [{
        "title": "第一集：天台之约",
        "logline": "两个少女在天台相遇并约定一起成为漫画家",
        "core_event": "天台立约，共同报名新人奖",
        "characters": ["林小满", "陈默"],
        "emotion_curve": "热血",
        "duration_sec": 180,
    }]}, ensure_ascii=False)


def _scenes_payload() -> str:
    """构造结构化场次 LLM 输出 JSON（2 场，合计 180s，含对白与地点）。

    Returns:
        str: {"locations": [...], "scenes": [...]} 的 JSON 文本。

    Raises:
        无。
    """
    return json.dumps({
        "locations": [
            {"name": "学校天台", "description": "黄昏的学校天台", "time_of_day": "dusk"},
        ],
        "scenes": [
            {"scene_no": 1, "title": "天台立约", "action": "两人站上天台边缘远眺城市",
             "plot": "立约", "location": "学校天台", "time_of_day": "dusk",
             "int_ext": "ext", "sfx": ["风声"], "emotion": "热血",
             "dialogue": [{"character": "林小满", "type": "对白",
                           "emotion": "坚定", "line": "我们一起拿金奖吧。"}],
             "duration_sec": 90},
            {"scene_no": 2, "title": "画室赶稿", "action": "深夜画室两人挑灯赶稿",
             "plot": "赶稿", "location": "画室", "time_of_day": "night",
             "int_ext": "int", "sfx": ["笔尖沙沙"], "emotion": "温情",
             "dialogue": [{"character": "陈默", "type": "旁白",
                           "emotion": "平静", "line": "分镜我来帮你。"}],
             "duration_sec": 90},
        ],
    }, ensure_ascii=False)


def _optimize_payload() -> str:
    """构造优化 LLM 输出 JSON（1 条片段：scenes[0].action）。

    Returns:
        str: {"changes": [1 条]} 的 JSON 文本。

    Raises:
        无。
    """
    return json.dumps({"changes": [{
        "path": "scenes[0].action",
        "original": "LLM 乱写的原文（将被当前库值覆盖）",
        "proposed": "夜风呼啸，两人并肩站上天台边缘远眺城市灯火。",
        "reason": "增强画面感与情绪张力",
    }]}, ensure_ascii=False)


def _setting_payload() -> str:
    """构造故事设定 LLM 输出 JSON（设定三段 + 2 个与角色域同构的人物候选）。

    Returns:
        str: {"logline", "core_conflict", "worldview", "characters": [...]}
            的 JSON 文本（characters 结构与 _characters_payload 同构）。

    Raises:
        无。
    """
    return json.dumps({
        "logline": "两个少女约定成为漫画家",
        "core_conflict": "天赋差距与友情考验",
        "worldview": "现代都市漫画行业",
        "characters": [
            {"name": "林小满", "role_type": "lead", "age": "约17岁",
             "personality": "倔强善良，遇挫不折", "appearance": "短发少女，戴红色发卡",
             "motivation": "和挚友一起成为漫画家", "arc": "从畏缩退让到坚定追梦",
             "relations": [{"target": "陈默", "relation": "漫画搭档"}]},
            {"name": "陈默", "role_type": "support", "age": "约18岁",
             "personality": "沉稳寡言，内心炽热", "appearance": "高瘦少年，黑框眼镜",
             "motivation": "守护小满的梦想", "arc": "从旁观者到并肩者",
             "relations": [{"target": "林小满", "relation": "漫画搭档"}]},
        ],
    }, ensure_ascii=False)


def _outline_regen_payload() -> str:
    """构造单集大纲重生成 LLM 输出 JSON（复用 episodes 数组形态，恰好 1 条）。

    plan_episode_outline_regen 与 plan_episodes 共用 agent_name=
    "episode_planner"，且复用 _normalize_episode_candidates 解析后取第一条，
    故返回结构与 _episodes_payload 一致；title 带「（重生成）」标记供冒烟
    断言重生成确实生效，角色名取自已确认名单（名单外名字会被规整剔除）。

    Returns:
        str: {"episodes": [1 集]} 的 JSON 文本（时长对齐项目 180s）。

    Raises:
        无。
    """
    return json.dumps({"episodes": [{
        "title": "第一集：天台之约（重生成）",
        "logline": "重生成：两个少女在天台重逢并重启新人奖之约",
        "core_event": "天台重启立约并重新报名新人奖（AI 重生成）",
        "characters": ["林小满", "陈默"],
        # emotion_curve 必须与整卷桩（_episodes_payload）保持单段取值："→" 分隔
        # 会被 episode_writer 判为多批续写，delta/raw_text 翻倍破坏步骤 5 断言
        "emotion_curve": "热血",
        "duration_sec": 180,
    }]}, ensure_ascii=False)


def _assist_payload(messages) -> str:
    """构造 script_assist 对话式改写 LLM 输出 JSON（按作用域判别关键词分支）。

    判别依据（build_assist_messages 的 user 消息固定文案，比裸词 logline/
    大纲 更可靠——两个作用域的 user 消息都含 "logline"）：setting 作用域
    首行为「改写作用域：故事设定改写：…」；episode_outline 作用域首行为
    「改写作用域：单集大纲改写：…」。original 占位即可——服务端
    _normalize_assist_changes 会以当前库值覆盖 original，保证 apply 的
    original 一致性校验必然通过。

    Args:
        messages: OpenAI 格式消息列表（取 user 消息文本判别作用域）。

    Returns:
        str: {"changes": [{path, original, proposed, reason}], "summary"}
            的 JSON 文本；setting 作用域返回 logline/core_conflict 两条
            片段（采纳后 core_conflict 断言 AI 协作文本），episode_outline
            作用域返回 core_event 一条片段。

    Raises:
        无：未识别的作用域按 setting 分支兜底。
    """
    user_text = ""
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            user_text = str(msg.get("content") or "")
            break
    if "单集大纲改写" in user_text:
        return json.dumps({"changes": [{
            "path": "core_event",
            "original": "占位原文（服务端以当前库值覆盖）",
            "proposed": "（AI 协作）天台立约并共同报名新人奖",
            "reason": "按用户指令强化核心事件",
        }], "summary": "改写单集大纲核心事件"}, ensure_ascii=False)
    return json.dumps({"changes": [
        {"path": "logline",
         "original": "占位原文（服务端以当前库值覆盖）",
         "proposed": "（AI 协作）两个少女约定成为漫画家",
         "reason": "按用户指令强化一句话故事"},
        {"path": "core_conflict",
         "original": "占位原文（服务端以当前库值覆盖）",
         "proposed": "（AI 协作）天赋差距与友情考验的正面碰撞",
         "reason": "按用户指令增强核心冲突"},
    ], "summary": "改写故事设定"}, ensure_ascii=False)


# 正文流分块（单批固定 2 块，供 SSE 断言 delta 数量）
_STREAM_CHUNKS = ["天台的风吹过，", "林小满握紧了拳头。"]


async def _fake_llm_chat(messages, *, temperature=0.8, agent_name="character_planner", **kwargs) -> str:
    """替换 app.agents.script_pipeline._llm_chat：按 agent_name 路由返回固定 JSON。

    Args:
        messages: OpenAI 格式消息列表（忽略）。
        temperature: 采样温度（忽略）。
        agent_name: 调用方 Agent 标识（路由依据）。
        **kwargs: 其余关键字参数（忽略）。

    Returns:
        str: 对应 Agent 的固定 JSON 文本。

    Raises:
        ValueError: agent_name 未覆盖分支时抛出（便于及时发现挂点变化）。
    """
    if agent_name == "character_planner":
        return _characters_payload()
    if agent_name == "setting_planner":
        return _setting_payload()
    if agent_name == "episode_planner":
        # 单集大纲重生成（plan_episode_outline_regen）与整卷分集（plan_episodes）
        # 共用 agent_name="episode_planner"：以重生成 system 提示词固定文案
        # 「只重写第」（_EPISODE_OUTLINE_REGEN_SYSTEM_PROMPT）判别路由。
        system_text = ""
        if messages and isinstance(messages[0], dict):
            system_text = str(messages[0].get("content") or "")
        if "只重写第" in system_text:
            return _outline_regen_payload()
        return _episodes_payload()
    if agent_name == "episode_writer":
        return _scenes_payload()
    if agent_name == "script_optimize":
        return _optimize_payload()
    if agent_name == "script_assist":
        return _assist_payload(messages)
    raise ValueError(f"未覆盖的 agent_name：{agent_name}")


async def _fake_llm_chat_stream(messages, *, agent_name="episode_writer", **kwargs):
    """替换 app.agents.script_pipeline._llm_chat_stream：按序产出固定正文增量。

    Args:
        messages: OpenAI 格式消息列表（忽略）。
        agent_name: 调用方 Agent 标识（忽略）。
        **kwargs: 其余关键字参数（忽略）。

    Yields:
        str: 预置的正文增量块（顺序产出）。

    Raises:
        无。
    """
    for chunk in _STREAM_CHUNKS:
        yield chunk


async def _fake_get_compiled_pipeline():
    """替换 app.api.script_pipeline.get_compiled_script_pipeline：返回 InMemory 子图。

    以 InMemorySaver 编译三阶段子图，使 GET /script/pipeline/status 等端点
    完全不触碰数据库检查点表（主库零读写）。

    Returns:
        Any: 带 InMemorySaver 检查点的编译子图。

    Raises:
        无。
    """
    return build_script_pipeline_subgraph(checkpointer=InMemorySaver())


async def _fake_llm_extract(_raw_text: str) -> None:
    """替换 app.services.script_service.llm_extract：恒返回 None 强制规则抽取。

    使 POST /script/extract 走确定性的规则路（从结构化 content 的
    「角色名：台词」格式抽角色），避免真实 LLM 渠道引入不确定输出。

    Args:
        _raw_text: 抽取源文本（忽略）。

    Returns:
        None（触发调用方降级到 rule_extract）。
    """
    return None


# ---------------------------------------------------------------------------
# HTTP 请求工具
# ---------------------------------------------------------------------------
async def req(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    headers: Optional[Dict[str, str]] = None,
    **kwargs: Any,
) -> httpx.Response:
    """发起带可选 Bearer 认证的请求（自动补 testserver base_url）。

    Args:
        client: ASGITransport 异步客户端。
        method: HTTP 方法。
        path: 以 /api 开头的路径。
        headers: 附加请求头（如 Authorization）。
        **kwargs: 透传 httpx 参数（json 等）。

    Returns:
        httpx.Response: 响应对象。

    Raises:
        无：网络/ASGI 异常由 httpx 抛出，由调用方处理。
    """
    return await client.request(method, path, headers=headers or {}, **kwargs)


# ---------------------------------------------------------------------------
# 性能测量
# ---------------------------------------------------------------------------
async def _measure(
    client: httpx.AsyncClient,
    name: str,
    method: str,
    path: str,
    headers: Dict[str, str],
    json_body: Optional[Any] = None,
    note: str = "",
) -> None:
    """对单个接口测 3 次响应时间取中位数并记录到性能基线表。

    Args:
        client: 进程内异步客户端。
        name: 接口名称（写入基线表）。
        method: HTTP 方法。
        path: 请求路径（含 query）。
        headers: 认证请求头。
        json_body: 请求体（可空）。
        note: 数据量备注。

    Returns:
        None。

    Raises:
        无：失败仅记录（不影响退出码的通过判定另由 check 控制）。
    """
    samples: List[float] = []
    for _ in range(_PERF_ROUNDS):
        start = time.perf_counter()
        resp = await client.request(method, path, headers=headers, json=json_body)
        samples.append((time.perf_counter() - start) * 1000.0)
        if resp.status_code != 200:
            check(f"性能测量 {method} {path} 返回 200", False,
                  f"http={resp.status_code} body={resp.text[:120]}")
            return
    median = statistics.median(samples)
    _PERF.append((f"{method} {path}", median, samples, note))
    log(f"[PERF] {name}: median={median:.1f}ms samples={[round(s, 1) for s in samples]}")


async def _measure_sse_first_byte(
    client: httpx.AsyncClient,
    name: str,
    path: str,
    headers: Dict[str, str],
    note: str = "",
) -> None:
    """测量 SSE 接口首事件耗时（首字节口径，mock LLM 即时完成）。

    完整消费响应体（保证生成锁释放），仅记录首个非空行的到达耗时。

    Args:
        client: 进程内异步客户端。
        name: 接口名称。
        path: 请求路径。
        headers: 认证请求头。
        note: 备注。

    Returns:
        None。

    Raises:
        无：异常仅记录。
    """
    samples: List[float] = []
    for _ in range(_PERF_ROUNDS):
        start = time.perf_counter()
        first_byte: Optional[float] = None
        try:
            async with client.stream("POST", path, headers=headers) as resp:
                if resp.status_code != 200:
                    check(f"性能测量 SSE {path} 返回 200", False, f"http={resp.status_code}")
                    return
                async for line in resp.aiter_lines():
                    if first_byte is None and line.strip():
                        first_byte = (time.perf_counter() - start) * 1000.0
        except Exception as exc:  # noqa: BLE001
            check(f"性能测量 SSE {path}", False, f"异常：{exc}")
            return
        if first_byte is not None:
            samples.append(first_byte)
    if not samples:
        log(f"[PERF] {name}: 未测到首字节")
        return
    median = statistics.median(samples)
    _PERF.append((f"POST {path}", median, samples, note))
    log(f"[PERF] {name}: 首字节 median={median:.1f}ms samples={[round(s, 1) for s in samples]}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def run() -> int:
    """执行端到端冒烟主流程与性能测量。

    Returns:
        int: 0 全部通过；1 存在失败（退出码）。

    Raises:
        无：异常在内部捕获并记录为失败项。
    """
    engine = await ensure_test_database()

    # ---------- 依赖覆写：get_db 指向测试库 ----------
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    async def _override_get_db():
        """依赖覆写：为每个请求提供测试库会话（内部函数）。

        Yields:
            AsyncSession: 测试库异步会话。

        Raises:
            无：请求结束在 finally 关闭会话。
        """
        session = factory()
        try:
            yield session
        finally:
            await session.close()

    fastapi_app.dependency_overrides[get_db] = _override_get_db

    # ---------- LLM 挂点与子图挂点替换（进程内，确定性） ----------
    sp_mod._llm_chat = _fake_llm_chat  # type: ignore[assignment]
    sp_mod._llm_chat_stream = _fake_llm_chat_stream  # type: ignore[assignment]
    ss_mod.llm_extract = _fake_llm_extract  # type: ignore[assignment]
    sp_api_mod.get_compiled_script_pipeline = _fake_get_compiled_pipeline  # type: ignore[assignment]

    username = f"smoke_pipeline_{int(time.time())}"
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
        timeout=httpx.Timeout(120.0),
    ) as client:
        try:
            await _scenario(client, username)
        except Exception as exc:  # noqa: BLE001 —— 场景级异常计为失败
            check("冒烟主流程无未捕获异常", False, f"{type(exc).__name__}: {exc}")
        finally:
            fastapi_app.dependency_overrides.pop(get_db, None)

    await engine.dispose()

    # ---------- 汇总 ----------
    log("=" * 68)
    log("性能基线（本机进程内 ASGITransport 口径，3 次取中位数）")
    for api, median, samples, note in _PERF:
        log(f"  {api}  median={median:.1f}ms  samples={[round(s, 1) for s in samples]}  {note}")
    log("=" * 68)
    if _FAILS:
        log(f"冒烟失败：共 {len(_FAILS)} 项")
        for item in _FAILS:
            log(f"  - {item}")
        return 1
    log("冒烟全部通过")
    return 0


async def _scenario(client: httpx.AsyncClient, username: str) -> None:
    """执行完整范式流水线冒烟场景（注册 → 建项目 → 四阶段 → 收口接口）。

    Args:
        client: 进程内异步客户端。
        username: 本次随机用户名（保证多次运行互不冲突）。

    Returns:
        None。

    Raises:
        无：各步骤以 check 记录结果，不中断后续（关键依赖 null 时后续步骤自然失败）。
    """
    # ---------- 步骤 1：注册用户 → 建项目（target_duration=180） ----------
    resp = await req(client, "POST", "/api/auth/register", json={
        "username": username,
        "email": f"{username}@test.local",
        "password": "Smoke@12345",
    })
    check("步骤1 注册用户", resp.status_code == 200, f"http={resp.status_code} body={resp.text[:160]}")
    token = resp.json().get("access_token") if resp.status_code == 200 else None
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    resp = await req(client, "POST", "/api/projects", headers,
                     json={"title": "范式流水线冒烟项目", "target_duration": 180})
    check("步骤1 建项目", resp.status_code in (200, 201), f"http={resp.status_code} body={resp.text[:160]}")
    pid = resp.json().get("id") if resp.status_code in (200, 201) else None
    check("步骤1 项目 id 有效", bool(pid), f"pid={pid}")

    # ---------- 步骤 2：PUT /script/input（idea 模式一句话创意） ----------
    resp = await req(client, "PUT", f"/api/projects/{pid}/script/input", headers,
                     json={"mode": "idea", "raw_text": _IDEA_TEXT})
    check("步骤2 保存 idea 输入", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:160]}")

    # ---------- 步骤 2.5：故事设定域 generate → get → edit → assist → regenerate ----------
    resp = await req(client, "POST", f"/api/projects/{pid}/script/setting/generate", headers)
    check("步骤2.5 设定 generate", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    gen_setting = resp.json() if resp.status_code == 200 else {}
    st = gen_setting.get("setting") or {}
    check("步骤2.5 设定三段非空",
          bool(st.get("logline")) and bool(st.get("core_conflict"))
          and bool(st.get("worldview")),
          f"logline={st.get('logline')} conflict={st.get('core_conflict')}")
    check("步骤2.5 generate 后 phase=characters", gen_setting.get("phase") == "characters",
          f"phase={gen_setting.get('phase')}")
    check("步骤2.5 人物候选 ≥1", len(gen_setting.get("characters") or []) >= 1,
          f"candidates={len(gen_setting.get('characters') or [])}")

    resp = await req(client, "GET", f"/api/projects/{pid}/script/setting", headers)
    check("步骤2.5 GET 设定", resp.status_code == 200, f"http={resp.status_code}")
    setting_detail = resp.json() if resp.status_code == 200 else {}
    check("步骤2.5 setting 非 null 且 planned 候选在",
          setting_detail.get("setting") is not None
          and len(setting_detail.get("planned") or []) >= 1,
          f"planned={len(setting_detail.get('planned') or [])}")

    resp = await req(client, "PUT", f"/api/projects/{pid}/script/setting", headers,
                     json={"logline": "（用户编辑）两个少女约定一起成为漫画家"})
    check("步骤2.5 编辑设定", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:160]}")
    check("步骤2.5 编辑后 source=user",
          resp.status_code == 200
          and (resp.json().get("setting") or {}).get("source") == "user",
          f"source={(resp.json().get('setting') or {}).get('source') if resp.status_code == 200 else None}")

    # assist 预览（不落库）：随后 GET 验证 core_conflict 未被改动
    resp = await req(client, "POST", f"/api/projects/{pid}/script/assist", headers,
                     json={"scope": "setting", "instruction": "把核心冲突再强一些"})
    check("步骤2.5 assist 预览", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    assist_prev = resp.json() if resp.status_code == 200 else {}
    check("步骤2.5 changes ≥1",
          (assist_prev.get("count") or 0) >= 1 and bool(assist_prev.get("changes")),
          f"count={assist_prev.get('count')}")

    resp = await req(client, "GET", f"/api/projects/{pid}/script/setting", headers)
    before_conflict = (((resp.json() or {}).get("setting") or {}).get("core_conflict")
                       if resp.status_code == 200 else None)
    check("步骤2.5 预览不落库（core_conflict 未变）",
          before_conflict == "天赋差距与友情考验", f"core_conflict={before_conflict}")

    resp = await req(client, "POST", f"/api/projects/{pid}/script/assist/apply", headers,
                     json={"scope": "setting",
                           "accepts": [{"id": c.get("id"), "path": c.get("path"),
                                        "original": c.get("original"),
                                        "proposed": c.get("proposed")}
                                       for c in (assist_prev.get("changes") or [])]})
    check("步骤2.5 assist 采纳", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    applied_assist = resp.json() if resp.status_code == 200 else {}
    check("步骤2.5 applied ≥1",
          (applied_assist.get("applied") or 0) >= 1,
          f"applied={applied_assist.get('applied')} skipped={applied_assist.get('skipped')}")

    resp = await req(client, "GET", f"/api/projects/{pid}/script/setting", headers)
    ai_conflict = (((resp.json() or {}).get("setting") or {}).get("core_conflict")
                   if resp.status_code == 200 else None)
    check("步骤2.5 采纳后 core_conflict=AI 协作文本",
          bool(ai_conflict) and ai_conflict.startswith("（AI 协作）"),
          f"core_conflict={ai_conflict}")

    resp = await req(client, "POST", f"/api/projects/{pid}/script/setting/regenerate", headers,
                     json={"instruction": "世界观更紧凑"})
    check("步骤2.5 设定重生成", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    regen_setting = (resp.json().get("setting") or {}) if resp.status_code == 200 else {}
    check("步骤2.5 重生成后三段非空",
          bool(regen_setting.get("logline")) and bool(regen_setting.get("core_conflict"))
          and bool(regen_setting.get("worldview")),
          f"logline={regen_setting.get('logline')} worldview={regen_setting.get('worldview')}")

    # ---------- 步骤 3：角色域 generate → get → edit → confirm ----------
    resp = await req(client, "POST", f"/api/projects/{pid}/script/characters/generate",
                     headers, json={})
    check("步骤3 角色 generate", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    gen_body = resp.json() if resp.status_code == 200 else {}
    candidates = gen_body.get("characters", [])
    check("步骤3 生成 2 个候选角色", len(candidates) == 2,
          f"candidates={[c.get('name') for c in candidates]}")
    check("步骤3 generate 后 phase=characters", gen_body.get("phase") == "characters",
          f"phase={gen_body.get('phase')}")

    resp = await req(client, "GET", f"/api/projects/{pid}/script/characters", headers)
    check("步骤3 角色列表", resp.status_code == 200, f"http={resp.status_code}")
    lst = resp.json() if resp.status_code == 200 else {}
    planned = lst.get("planned", [])
    first_candidate_id = planned[0].get("candidate_id") if planned else None

    # 改 1 个候选：按 candidate_id 更新 personality
    resp = await req(client, "PUT", f"/api/projects/{pid}/script/characters", headers, json={
        "upsert": [{"id": first_candidate_id, "name": "林小满",
                    "personality": "倔强善良，越挫越勇（用户已编辑）"}],
        "remove_ids": [],
        "remove_names": [],
    })
    check("步骤3 批量编辑 1 个候选", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:160]}")
    edited = [c for c in (resp.json().get("characters", []) if resp.status_code == 200 else [])
              if c.get("name") == "林小满"]
    check("步骤3 编辑生效（personality 更新）",
          bool(edited) and "用户已编辑" in (edited[0].get("personality") or ""),
          f"personality={edited[0].get('personality') if edited else None}")

    resp = await req(client, "POST", f"/api/projects/{pid}/script/characters/confirm",
                     headers, json={"characters": [{"name": "林小满"}, {"name": "陈默"}]})
    check("步骤3 角色 confirm", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:160]}")
    check("步骤3 confirm 后 phase=outline",
          resp.status_code == 200 and resp.json().get("phase") == "outline",
          f"phase={resp.json().get('phase') if resp.status_code == 200 else None}")

    resp = await req(client, "GET", f"/api/projects/{pid}/script/characters", headers)
    confirmed = resp.json().get("confirmed", []) if resp.status_code == 200 else []
    check("步骤3 已入池角色 2 个", len(confirmed) == 2,
          f"confirmed={[c.get('name') for c in confirmed]}")
    character_id = confirmed[0].get("id") if confirmed else None

    # ---------- 步骤 4：大纲域 plan → edit outline → confirm ----------
    resp = await req(client, "POST", f"/api/projects/{pid}/script/episodes/plan",
                     headers, json={"target_episodes": 1})
    check("步骤4 生成分集大纲", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    episodes = resp.json().get("episodes", []) if resp.status_code == 200 else []
    check("步骤4 生成 1 集", len(episodes) == 1, f"episodes={len(episodes)}")
    eid = episodes[0].get("episode_id") if episodes else None

    resp = await req(client, "PUT", f"/api/projects/{pid}/script/episodes/{eid}/outline",
                     headers, json={"core_event": "天台立约并共同报名新人奖（用户已编辑）"})
    check("步骤4 编辑单集大纲", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:160]}")
    check("步骤4 core_event 已更新",
          resp.status_code == 200 and "用户已编辑" in (resp.json().get("core_event") or ""),
          f"core_event={resp.json().get('core_event') if resp.status_code == 200 else None}")

    resp = await req(client, "POST", f"/api/projects/{pid}/script/episodes/plan/confirm", headers)
    check("步骤4 确认大纲", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:160]}")
    check("步骤4 confirm 后 phase=body",
          resp.status_code == 200 and resp.json().get("phase") == "body",
          f"phase={resp.json().get('phase') if resp.status_code == 200 else None}")

    # ---------- 步骤 4.5：单集大纲 AI 重生成（redesign-script-workspace Task 4） ----------
    resp = await req(client, "POST",
                     f"/api/projects/{pid}/script/episodes/{eid}/outline/regenerate", headers,
                     json={"instruction": "钩子前置"})
    check("步骤4.5 单集大纲重生成", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    regen_ep = resp.json() if resp.status_code == 200 else {}
    check("步骤4.5 title 带（重生成）标记",
          "（重生成）" in (regen_ep.get("title") or ""), f"title={regen_ep.get('title')}")
    check("步骤4.5 body_exists=false（正文未生成）",
          regen_ep.get("body_exists") is False, f"body_exists={regen_ep.get('body_exists')}")
    check("步骤4.5 sequence 不变（仅重写本集，其余集不受影响）",
          regen_ep.get("sequence") == 1, f"sequence={regen_ep.get('sequence')}")

    # ---------- 阶段锁定负向断言：正文阶段故事设定锁定（409） ----------
    resp = await req(client, "POST", f"/api/projects/{pid}/script/setting/generate", headers)
    lock_detail = ""
    if resp.status_code == 409:
        try:
            lock_detail = str((resp.json() or {}).get("detail") or "")
        except ValueError:  # noqa: BLE001 —— 非 JSON 响应体兜底
            lock_detail = resp.text[:120]
    check("步骤4.5 正文阶段 setting/generate 被锁 409", resp.status_code == 409,
          f"http={resp.status_code} detail={lock_detail[:80]}")
    check("步骤4.5 409 文案含「锁定」", "锁定" in lock_detail, f"detail={lock_detail[:80]}")

    # ---------- 步骤 5：正文域 SSE 生成 → PUT body（场次级 source 合并） ----------
    resp = await req(client, "POST",
                     f"/api/projects/{pid}/script/episodes/{eid}/body/generate/stream", headers)
    check("步骤5 正文 SSE 生成 HTTP 200", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    events = parse_sse(resp.text) if resp.status_code == 200 else []
    names = [n for n, _ in events]
    delta_count = names.count("delta")
    done = next((d for n, d in events if n == "done" and d), None)
    check("步骤5 SSE 事件含 delta 与 done（delta/status/done）",
          delta_count == len(_STREAM_CHUNKS) and done is not None, f"events={names}")
    check("步骤5 done.version=1",
          bool(done) and done.get("version") == 1,
          f"version={done.get('version') if done else None}")
    check("步骤5 done.raw_text 拼接正确",
          bool(done) and done.get("raw_text") == "".join(_STREAM_CHUNKS),
          f"raw_text={done.get('raw_text') if done else None}")

    # 解包 done.content 为纯结构，改第 1 场 action（第 2 场不动）
    plain: Dict[str, Any] = {
        "title": done["content"]["title"]["value"],
        "logline": done["content"]["logline"]["value"],
        "locations": done["content"]["locations"]["value"],
        "scenes": done["content"]["scenes"]["value"],
    }
    plain["scenes"][0]["action"] = "用户改过的动作：小满攥紧栏杆俯瞰操场。"

    resp = await req(client, "PUT", f"/api/projects/{pid}/script/episodes/{eid}/body",
                     headers, json={"raw_text": "用户修改后的文章体正文。", "content": plain,
                                    "base_version": 1})
    check("步骤5 保存单集正文", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    saved = resp.json() if resp.status_code == 200 else {}
    check("步骤5 保存后 version=2", saved.get("version") == 2, f"version={saved.get('version')}")
    saved_scenes = (saved.get("content") or {}).get("value", {}).get("scenes", [])
    check("步骤5 场次级 source 合并（改场 user / 未改场 ai）",
          len(saved_scenes) == 2
          and saved_scenes[0].get("source") == "user"
          and saved_scenes[1].get("source") == "ai",
          f"sources={[s.get('source') for s in saved_scenes]}")

    # ---------- 步骤 5.5：assist 正文作用域（复用 optimize 预览/落库通道） ----------
    resp = await req(client, "POST", f"/api/projects/{pid}/script/assist", headers,
                     json={"scope": "episode_body", "target": eid,
                           "instruction": "加强画面感"})
    check("步骤5.5 assist 正文预览", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    body_assist = resp.json() if resp.status_code == 200 else {}
    check("步骤5.5 正文预览 changes ≥1",
          (body_assist.get("count") or 0) >= 1 and bool(body_assist.get("changes")),
          f"count={body_assist.get('count')}")
    body_frag = (body_assist.get("changes") or [{}])[0]

    resp = await req(client, "POST", f"/api/projects/{pid}/script/assist/apply", headers,
                     json={"scope": "episode_body", "target": eid,
                           "accepts": [{"id": body_frag.get("id"),
                                        "path": body_frag.get("path"),
                                        "original": body_frag.get("original"),
                                        "proposed": body_frag.get("proposed")}],
                           "base_version": saved.get("version")})
    check("步骤5.5 assist 正文采纳", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    body_applied = resp.json() if resp.status_code == 200 else {}
    check("步骤5.5 applied ≥1 且 version/content 回传",
          (body_applied.get("applied") or 0) >= 1
          and body_applied.get("version") == (saved.get("version") or 0) + 1
          and isinstance(body_applied.get("content"), dict),
          f"applied={body_applied.get('applied')} version={body_applied.get('version')}")
    # 后续步骤 6 的乐观锁基准：5.5 采纳已把 version 从 2 推进一格（动态对齐）
    body_version = body_applied.get("version") or ((saved.get("version") or 0) + 1)

    # ---------- 步骤 5.6：漫剧格式渲染数据契约断言（前端 manga-format 渲染器输入） ----------
    resp = await req(client, "GET", f"/api/projects/{pid}/script?episode_id={eid}", headers)
    check("步骤5.6 GET 单集正文", resp.status_code == 200, f"http={resp.status_code}")
    body_out = resp.json() if resp.status_code == 200 else {}
    wrap = body_out.get("content") if isinstance(body_out.get("content"), dict) else {}
    # 兼容两种包装形态：字段级 {"scenes": {"value": [...]}}（apply 重包装后，
    # 取法对齐步骤 5 done.content 的逐字段 ["value"] 解包）/ 整体包装
    # {"value": {...}}（PUT body 后未再 apply 的兜底形态）
    if isinstance(wrap.get("scenes"), dict):
        scenes_doc = wrap["scenes"].get("value") or []
        locations_doc = (wrap.get("locations") or {}).get("value") or []
    elif isinstance(wrap.get("value"), dict):
        scenes_doc = wrap["value"].get("scenes") or []
        locations_doc = wrap["value"].get("locations") or []
    else:
        scenes_doc, locations_doc = [], []
    first_scene = scenes_doc[0] if scenes_doc and isinstance(scenes_doc[0], dict) else {}
    first_dialogue = first_scene.get("dialogue") or [{}]
    first_dialogue = first_dialogue[0] if isinstance(first_dialogue[0], dict) else {}
    check("步骤5.6 场次字段齐备（action/location/time_of_day/int_ext）",
          bool(scenes_doc) and all(first_scene.get(k) for k in
                                   ("action", "location", "time_of_day", "int_ext")),
          f"scene_keys={sorted(first_scene.keys())}")
    check("步骤5.6 对白字段齐备（type/emotion/character）",
          bool(first_dialogue) and all(first_dialogue.get(k) for k in
                                       ("type", "emotion", "character")),
          f"dialogue={first_dialogue}")
    check("步骤5.6 locations 非空数组",
          isinstance(locations_doc, list) and len(locations_doc) >= 1,
          f"locations={len(locations_doc) if isinstance(locations_doc, list) else locations_doc}")

    # ---------- 步骤 6：优化 optimize（segment）→ apply ----------
    resp = await req(client, "POST",
                     f"/api/projects/{pid}/script/episodes/{eid}/optimize", headers,
                     json={"grain": "segment", "target": 1})
    check("步骤6 优化预览 optimize", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    opt = resp.json() if resp.status_code == 200 else {}
    check("步骤6 返回 1 条片段（含 diff_lines）",
          opt.get("count") == 1 and bool(opt.get("changes"))
          and bool(opt["changes"][0].get("diff_lines")),
          f"count={opt.get('count')}")
    frag = opt["changes"][0] if opt.get("changes") else {}

    resp = await req(client, "POST",
                     f"/api/projects/{pid}/script/episodes/{eid}/optimize/apply", headers,
                     json={"accept_ids": [frag.get("id")],
                           "changes": [{"id": frag.get("id"), "path": frag.get("path"),
                                        "original": frag.get("original"),
                                        "proposed": frag.get("proposed")}],
                           "base_version": body_version})
    check("步骤6 应用优化片段", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    applied_body = resp.json() if resp.status_code == 200 else {}
    # 注：步骤 5.5 的 assist 正文采纳已把 version 从 2 推进到 body_version，
    # 本步乐观锁基准与递增断言随之动态对齐（不再硬编码 2/3）。
    check("步骤6 applied=1 且 version 递增",
          applied_body.get("applied") == 1
          and applied_body.get("version") == body_version + 1,
          f"applied={applied_body.get('applied')} version={applied_body.get('version')}")

    # ---------- 步骤 7：POST /script/health（按集体检） ----------
    resp = await req(client, "POST", f"/api/projects/{pid}/script/health?episode_id={eid}", headers)
    check("步骤7 剧本体检", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    report = resp.json() if resp.status_code == 200 else {}
    issues = report.get("issues")
    check("步骤7 score 为 0~100 整数",
          isinstance(report.get("score"), int) and 0 <= report["score"] <= 100,
          f"score={report.get('score')}")
    check("步骤7 issues 结构（level/message/location）",
          isinstance(issues, list) and all(
              isinstance(i, dict) and {"level", "message", "location"} <= set(i) for i in issues),
          f"issues={len(issues) if isinstance(issues, list) else issues}")

    # ---------- 步骤 8：POST /script/extract（改判源：从 content 抽角色） ----------
    # 让默认剧本行具备结构化内容（extract 取源为默认行 episode_id IS NULL）
    resp = await req(client, "PUT", f"/api/projects/{pid}/script", headers,
                     json={"content": plain})
    check("步骤8 默认行保存结构化剧本", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:160]}")

    resp = await req(client, "POST", f"/api/projects/{pid}/script/extract", headers)
    check("步骤8 实体抽取", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    extracted = resp.json() if resp.status_code == 200 else {}
    ex_names = [c.get("name") for c in extracted.get("characters", [])]
    check("步骤8 从 content 抽到角色（含 林小满）",
          "林小满" in ex_names, f"characters={ex_names}")

    # ---------- 步骤 9：影响面 impact → 导出 fountain ----------
    resp = await req(
        client, "GET",
        f"/api/projects/{pid}/script/entities/impact"
        f"?entity_type=character&entity_id={character_id}", headers)
    check("步骤9 影响面预览", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    impact = resp.json() if resp.status_code == 200 else {}
    check("步骤9 script_refs>0 且结构完整",
          impact.get("script_refs", 0) > 0
          and {"storyboard_refs", "script_locations", "storyboard_ids", "summary"} <= set(impact),
          f"script_refs={impact.get('script_refs')} summary={impact.get('summary')}")

    resp = await req(client, "GET", f"/api/projects/{pid}/script/export?format=fountain", headers)
    check("步骤9 导出 fountain", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:160]}")
    fountain_text = resp.text
    check("步骤9 fountain 含场景标题前缀（INT./EXT.）",
          "INT." in fountain_text or "EXT." in fountain_text,
          f"head={fountain_text[:80]!r}")

    # ---------- 步骤 10：GET /script/pipeline/status（断线重连状态） ----------
    resp = await req(client, "GET", f"/api/projects/{pid}/script/pipeline/status", headers)
    check("步骤10 子图状态", resp.status_code == 200,
          f"http={resp.status_code} body={resp.text[:200]}")
    status = resp.json() if resp.status_code == 200 else {}
    check("步骤10 status 结构（script_phase/has_pending_confirm/summary）",
          {"script_phase", "has_pending_confirm", "summary", "next_nodes"} <= set(status),
          f"keys={sorted(status.keys())}")

    # ---------- 性能基线测量（数据齐备后；SSE 放最后，覆盖正文不影响后续） ----------
    log("开始性能基线测量（3 次取中位数，进程内 ASGITransport 口径）...")
    await _measure(client, "项目列表", "GET", "/api/projects", headers,
                   note="本人 1 个项目")
    await _measure(client, "分集大纲列表", "GET",
                   f"/api/projects/{pid}/script/episodes/plan", headers,
                   note="1 集大纲")
    await _measure(client, "剧本体检（按集）", "POST",
                   f"/api/projects/{pid}/script/health?episode_id={eid}", headers,
                   note="1 集 2 场次")
    await _measure(client, "剧本导出 TXT", "GET",
                   f"/api/projects/{pid}/script/export?format=txt", headers,
                   note="1 集 2 场次")
    await _measure(client, "实体影响面", "GET",
                   f"/api/projects/{pid}/script/entities/impact"
                   f"?entity_type=character&entity_id={character_id}", headers,
                   note="1 角色 0 分镜")
    await _measure(client, "故事设定详情", "GET",
                   f"/api/projects/{pid}/script/setting", headers,
                   note="1 设定 2 候选")
    # setting 作用域在正文阶段（phase=body）锁定 409，此处用同一接口同一
    # Agent 通道的 episode_outline 作用域测量 assist 预览（不落库，幂等）
    await _measure(client, "AI 协作预览", "POST",
                   f"/api/projects/{pid}/script/assist", headers,
                   json_body={"scope": "episode_outline", "target": eid,
                              "instruction": "把大纲节奏再收紧一些"},
                   note="episode_outline 预览（setting 作用域 body 阶段锁定）")
    await _measure_sse_first_byte(
        client, "正文生成 SSE", f"/api/projects/{pid}/script/episodes/{eid}/body/generate/stream",
        headers, note="mock LLM，首事件耗时")


def main() -> int:
    """脚本入口：设置 UTF-8 输出并运行异步主流程。

    Returns:
        int: 进程退出码（0 通过 / 1 失败）。

    Raises:
        无：异常在 run 内捕获。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 —— 非 TTY 环境忽略
        pass
    log("== MangaFlow 剧本范式流水线冒烟 + 性能基线开始 ==")
    code = asyncio.run(run())
    log(f"== 结束（exit={code}）==")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
