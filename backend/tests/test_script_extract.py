"""实体抽取规则路与归一化测试模块（文件级说明，P0-2）。

覆盖链路：
- rule_extract 场景规则：内景/外景标记行、场景后缀词表兜底；
- _normalize_entities 键名漂移容错：locations/items 别名归并、entities 嵌套层；
- extract_entities 统计反馈：stats 字段（计数 + 抽取来源）；
- 多集剧本聚合取源：集级剧本行场次结构参与抽取（P0-2 补强）。
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Script
from app.services.script_service import (
    _normalize_entities,
    extract_entities,
    rule_extract,
)
from tests.conftest import create_project, unique_suffix


def test_guize_chouqu_neijing_waijing_biaoji():
    """规则路场景抽取：「内景/外景」标记行命中为场景实体。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    text = (
        "内景 咖啡馆 - 日\n"
        "林小满：你来了。\n"
        "咖啡馆里很安静。\n"
        "外景 天台 - 夜\n"
        "陈默：走吧。\n"
        "天台的风很大。\n"
    )
    result = rule_extract(text)
    names = [s["name"] for s in result["scenes"]]
    assert "咖啡馆" in names
    assert "天台" in names
    # 标记行场景置信度高于低置信阈值 0.4
    assert all(s["confidence"] >= 0.5 for s in result["scenes"])


def test_guize_chouqu_houzuicibiao_doudi():
    """规则路场景抽取：无标记行时场景后缀词表兜底（命中 >=2 次入选）。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    text = (
        "他们走进教室，教室里空无一人。\n"
        "后来又回到教室拿书包。\n"
        "办公室的门开着，办公室没有人。\n"
        "办公室的灯还亮着。\n"
    )
    result = rule_extract(text)
    names = [s["name"] for s in result["scenes"]]
    assert "教室" in names
    assert "办公室" in names
    # 后缀词表兜底 confidence=0.4（自动带 low_confidence 标记）
    assert all(s["confidence"] == 0.4 for s in result["scenes"])


def test_guize_chouqu_changjing_yu_jiaose_huchi():
    """规则路互斥：场景名与角色名重叠时丢弃，防误报。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    text = (
        "林咖啡馆：你怎么来了。\n"
        "林咖啡馆：走吧。\n"
        "内景 咖啡馆 - 日\n"
        "内景 林咖啡馆 - 夜\n"
    )
    result = rule_extract(text)
    names = [s["name"] for s in result["scenes"]]
    # 「林咖啡馆」是对白冒号前角色名，场景抽取须与其互斥
    assert "林咖啡馆" not in names
    assert "咖啡馆" in names


def test_normalize_jianming_bieming_guibing():
    """_normalize_entities：LLM 键名漂移（locations/items）归并为标准键。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    data = {
        "roles": [{"name": "张三", "confidence": 0.9}],
        "locations": [{"name": "医院", "confidence": 0.8}],
        "items": [{"name": "怀表", "confidence": 0.7}],
    }
    normalized = _normalize_entities(data)
    assert [c["name"] for c in normalized["characters"]] == ["张三"]
    assert [s["name"] for s in normalized["scenes"]] == ["医院"]
    assert [p["name"] for p in normalized["props"]] == ["怀表"]


def test_normalize_entities_qiantao_ceng():
    """_normalize_entities：{"entities": {...}} 包裹结构自动解包。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    data = {
        "entities": {
            "characters": [{"name": "李四"}],
            "scenes": [{"name": "学校"}],
            "props": [],
        }
    }
    normalized = _normalize_entities(data)
    assert [c["name"] for c in normalized["characters"]] == ["李四"]
    assert [s["name"] for s in normalized["scenes"]] == ["学校"]


async def test_chouqu_tongji_fankui_stats(client, auth, db: AsyncSession):
    """extract_entities：返回 stats 统计（计数 + 抽取来源 + 源文本长度）。

    LLM 可达时走 llm 路，不可达降级规则路，两种来源均为合法。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from app.services.script_service import unwrap_scenes

    project = await create_project(client, auth["headers"], title=f"抽取项目_{unique_suffix()}")
    content = {
        "scenes": [
            {"title": "咖啡馆", "plot": "两人重逢",
             "dialogue": [{"character": "林小满", "line": "你来了。"}]},
            {"title": "天台", "plot": "告白",
             "dialogue": [{"character": "陈默", "line": "走吧。"}]},
        ]
    }
    script = Script(project_id=project["id"], content=content,
                    raw_text="内景 咖啡馆 - 日\n林小满：你来了。")
    db.add(script)
    await db.commit()

    # 重新取项目对象（extract_entities 需要 Project ORM）
    from app.models import Project
    from app.services.script_service import get_owned_project
    project_orm = await get_owned_project(db, project["id"], auth["user_id"])
    result = await extract_entities(db, project_orm, script)

    assert "stats" in result
    stats = result["stats"]
    # 抽取来源：测试环境 LLM 可达时为 llm，不可达降级 rule（两种均为合法路径）
    assert stats["source"] in ("llm", "rule")
    assert stats["characters"] == len(result["characters"])
    assert stats["scenes"] == len(result["scenes"])
    assert stats["props"] == len(result["props"])
    assert stats["source_text_chars"] > 0
    # 规则路兜底保证场景至少命中一个（raw_text 含内景标记；LLM 路成功时同样应有场景）
    assert stats["scenes"] >= 1
    # 结果已落库 scripts.fences
    assert result["characters"] is not None
    assert unwrap_scenes(content)  # 前置确认源结构合法


async def test_duoji_jvben_jvheyuanqu(client, auth, db: AsyncSession):
    """多集剧本聚合抽取：集级剧本行（含场次结构）参与抽取源（P0-2 补强）。

    模拟流水线项目形态：默认行（仅 idea 输入，无场次）+ 集级剧本行（完整
    content.scenes）——修复前抽取只读默认行抽不出场景，聚合后场景应被抽出。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from app.models import Episode, Project
    from app.services.script_service import get_owned_project

    project = await create_project(client, auth["headers"], title=f"聚合项目_{unique_suffix()}")
    pid = project["id"]

    # 默认剧本行：仅 idea 输入（无场次结构）
    db.add(Script(project_id=pid, content={"input_mode": "idea"},
                  raw_text="林小满：我们要做出最棒的动画。"))
    # 集级剧本行：完整场次结构（整体包装形态，与 AI 生成落库同构）
    ep = Episode(project_id=pid, title="第1集", sequence=1)
    db.add(ep)
    await db.flush()
    episode_content = {
        "value": {
            "title": "第一集",
            "logline": "咖啡馆的相遇",
            "scenes": [
                {"scene_no": 1, "title": "咖啡馆", "plot": "两人重逢",
                 "dialogue": [{"character": "林小满", "line": "你来了。"}]},
                {"scene_no": 2, "title": "天台", "plot": "看星星",
                 "dialogue": [{"character": "陈默", "line": "走吧。"}]},
            ],
        },
        "source": "ai",
    }
    db.add(Script(project_id=pid, episode_id=ep.id, content=episode_content, raw_text=""))
    await db.commit()

    project_orm = await get_owned_project(db, pid, auth["user_id"])
    default_script = (
        await db.execute(
            select(Script).where(Script.project_id == pid, Script.episode_id.is_(None))
        )
    ).scalar_one()
    result = await extract_entities(db, project_orm, default_script)

    names = [s["name"] for s in result["scenes"]]
    # 集级剧本行的场景（咖啡馆/天台）必须被抽出（修复前只读默认行永远为 0）
    assert "咖啡馆" in names
    assert "天台" in names
    # 统计口径一致
    assert result["stats"]["scenes"] == len(result["scenes"])
    assert result["stats"]["source_text_chars"] >= 20  # 聚合源包含集级正文


async def test_quanbu_jvben_kong_baosi_bai(client, auth, db: AsyncSession):
    """聚合取源兜底：项目全部剧本行均无内容时 extract_entities 抛 400。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 未抛出 400 或错误文案不符时中断。
    """
    import pytest as _pytest
    from fastapi import HTTPException

    from app.models import Project
    from app.services.script_service import get_owned_project

    project = await create_project(client, auth["headers"], title=f"空剧本项目_{unique_suffix()}")
    db.add(Script(project_id=project["id"], content={}, raw_text=None))
    await db.commit()

    project_orm = await get_owned_project(db, project["id"], auth["user_id"])
    script = (
        await db.execute(
            select(Script).where(Script.project_id == project["id"])
        )
    ).scalar_one()
    with _pytest.raises(HTTPException) as exc_info:
        await extract_entities(db, project_orm, script)
    assert exc_info.value.status_code == 400
    assert "尚无内容" in exc_info.value.detail


async def test_anji_chouqu_juese_baimingdan_guolv(client, auth, db: AsyncSession):
    """按集抽取角色白名单过滤：抽取结果必须与该集大纲出场角色清单一致。

    按集工作流硬约束：大纲出场角色 = ['王小满', '摸鱼系统']，正文对白出现
    清单外角色「赵卷王」与清单内简称「小满」——抽取后清单外角色必须被程序级
    过滤丢弃，简称归一化为清单规范名「王小满」（LLM/规则两路统一生效）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from app.models import Episode, Project
    from app.services.script_service import get_owned_project

    project = await create_project(client, auth["headers"], title=f"白名单项目_{unique_suffix()}")
    pid = project["id"]

    ep = Episode(project_id=pid, title="第1集", sequence=1,
                 outline={"characters": ["王小满", "摸鱼系统"], "core_event": "绑定摸鱼系统"})
    db.add(ep)
    await db.flush()
    episode_content = {
        "value": {
            "title": "第一集",
            "scenes": [
                {"scene_no": 1, "title": "格子间", "location": "深夜写字楼格子间",
                 "time_of_day": "夜", "int_ext": "内景", "plot": "濒死绑定",
                 "dialogue": [
                     {"character": "小满", "line": "系统救我。"},
                     {"character": "赵卷王", "line": "还在加班？"},
                 ]},
            ],
        },
        "source": "ai",
    }
    db.add(Script(project_id=pid, episode_id=ep.id, content=episode_content, raw_text=""))
    await db.commit()

    project_orm = await get_owned_project(db, pid, auth["user_id"])
    ep_script = (
        await db.execute(
            select(Script).where(Script.project_id == pid, Script.episode_id == ep.id)
        )
    ).scalar_one()
    result = await extract_entities(db, project_orm, ep_script, scope_episode_id=ep.id)

    char_names = {c["name"] for c in result["characters"]}
    # 清单外角色（赵卷王）被程序级过滤丢弃——LLM 提示词约束失效时兜底生效
    assert "赵卷王" not in char_names
    # 简称「小满」归一化为清单规范名「王小满」
    assert "小满" not in char_names
    assert "王小满" in char_names
    # 清单兜底：大纲声明出场但正文未命中的角色（摸鱼系统）仍须出现
    assert "摸鱼系统" in char_names
    assert char_names == {"王小满", "摸鱼系统"}
    # 场景/道具不受角色白名单影响（LLM 可能抽镜头标题「格子间」或地点全名，均为正文场景命中）
    assert len(result["scenes"]) >= 1


async def test_confirm_huifu_ruanshan_zichan(client, auth, db: AsyncSession):
    """confirm_extract：同名已软删资产确认入池时复活（deleted_at 置空）。

    修复回归：场景删除后再确认入池永远"回不来"（exist 查询命中已删行仅更新
    不恢复 deleted_at）；角色分支原过滤 deleted_at 后新建会撞全量唯一索引
    uk_characters_name。三类资产统一走"复活"路径。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from datetime import datetime, timezone as tz

    from app.models import Character, Prop, Project, Scene
    from app.schemas.script import (
        ConfirmCharacterItem,
        ConfirmPropItem,
        ConfirmSceneItem,
        ExtractConfirmRequest,
    )
    from app.services.script_service import confirm_extract, get_owned_project

    project = await create_project(client, auth["headers"], title=f"复活项目_{unique_suffix()}")
    pid = project["id"]
    now = datetime.now(tz.utc)
    db.add(Character(project_id=pid, name="旧角色", visual_prompt="旧提示词",
                     enabled_at=None, deleted_at=now))
    db.add(Scene(project_id=pid, name="咖啡馆", visual_prompt="旧提示词", deleted_at=now))
    db.add(Prop(project_id=pid, name="怀表", visual_prompt="旧提示词", deleted_at=now))
    await db.commit()

    project_orm = await get_owned_project(db, pid, auth["user_id"])
    out = await confirm_extract(db, project_orm, ExtractConfirmRequest(
        characters=[ConfirmCharacterItem(name="旧角色", description="新描述")],
        scenes=[ConfirmSceneItem(name="咖啡馆")],
        props=[ConfirmPropItem(name="怀表")],
    ))

    # 不撞唯一约束且三类资产全部复活
    char = (await db.execute(
        select(Character).where(Character.project_id == pid, Character.name == "旧角色")
    )).scalar_one()
    scene = (await db.execute(
        select(Scene).where(Scene.project_id == pid, Scene.name == "咖啡馆")
    )).scalar_one()
    prop = (await db.execute(
        select(Prop).where(Prop.project_id == pid, Prop.name == "怀表")
    )).scalar_one()
    assert char.deleted_at is None and char.enabled_at is not None
    assert scene.deleted_at is None
    assert prop.deleted_at is None
    assert out["characters"] == 1 and out["scenes"] == 1 and out["props"] == 1


async def test_confirm_anji_qiyong_mingdan_luoku(client, auth, db: AsyncSession):
    """confirm_extract 传 episode_id：确认入池的资产名 union 落该集启用名单。

    按集视图语义：资产管理页按集视图仅显示该集 enabled_assets 名单内资产，
    未启用过的集显示空态。同名资产跨集重复确认时名单 union 去重，资产行
    幂等复用（visual_prompt 保留首集视觉设定，保证跨集一致性）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from app.models import Episode
    from app.schemas.script import (
        ConfirmCharacterItem,
        ConfirmSceneItem,
        ExtractConfirmRequest,
    )
    from app.services.script_service import confirm_extract, get_owned_project

    project = await create_project(client, auth["headers"], title=f"启用名单项目_{unique_suffix()}")
    pid = project["id"]
    ep = Episode(project_id=pid, title="第1集", sequence=1)
    db.add(ep)
    await db.commit()

    project_orm = await get_owned_project(db, pid, auth["user_id"])
    req = ExtractConfirmRequest(
        characters=[ConfirmCharacterItem(name="王小满", visual_prompt="统一风格插画，王小满，v1")],
        scenes=[ConfirmSceneItem(name="咖啡馆")],
        props=[],
        episode_id=ep.id,
    )
    await confirm_extract(db, project_orm, req)
    # 重复确认：同名角色带不同 visual_prompt，资产行复用、视觉设定保留（跨集一致性）
    req2 = ExtractConfirmRequest(
        characters=[ConfirmCharacterItem(name="王小满", visual_prompt="统一风格插画，王小满，漂移的v2")],
        scenes=[ConfirmSceneItem(name="咖啡馆")],
        episode_id=ep.id,
    )
    await confirm_extract(db, project_orm, req2)

    await db.refresh(ep)
    enabled = ep.enabled_assets or {}
    # 名单 union 去重：角色/场景各一个，风格自动创建计入
    assert enabled.get("characters") == ["王小满"]
    assert enabled.get("scenes") == ["咖啡馆"]
    assert len(enabled.get("styles") or []) >= 1
    # 跨集一致性：visual_prompt 保留首次确认的设定，未被第二次模板覆盖
    from app.models import Character
    char = (await db.execute(
        select(Character).where(Character.project_id == pid, Character.name == "王小满")
    )).scalar_one()
    assert "v1" in (char.visual_prompt or "")
    # GET /assets?episode_id= 响应携带 episode_enabled 名单
    resp = await client.get(
        f"/api/projects/{pid}/assets",
        params={"episode_id": ep.id},
        headers=auth["headers"],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["episode_enabled"]["characters"] == ["王小满"]
    # 未启用过的集：名单规范化四键（各为空数组）→ 前端按集视图显示空态
    ep2 = Episode(project_id=pid, title="第2集", sequence=2)
    db.add(ep2)
    await db.commit()
    resp2 = await client.get(
        f"/api/projects/{pid}/assets",
        params={"episode_id": ep2.id},
        headers=auth["headers"],
    )
    assert resp2.status_code == 200
    assert resp2.json()["episode_enabled"] == {
        "characters": [], "scenes": [], "props": [], "styles": [],
    }
