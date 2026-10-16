"""剧本创作阶段状态机测试模块（文件级说明，P-1 范式级重构 Task 2）。

覆盖范围：
- 阶段守卫 require_phase：快速模式（script_phase=NULL）放行、未达所需阶段
  409（detail 含当前阶段）、等于/晚于所需阶段放行、非法阶段名 400；
- 阶段推进 advance_phase：跳步（idea→body）拒绝且不修改原值、按序推进
  （idea→characters）合法、快速模式项目首次推进仅允许进入 idea；
- 里程碑快照 write_stage_snapshot：字段（project_id/stage/summary）正确、
  flush 后可持久化、非法 stage 抛 ValueError；
- 模型层：直接 ORM 建带 P-1 新字段的 Character / Episode 读写往返
  （新列可空、无默认值；带值往返不丢），projects.script_phase 的
  CHECK 约束在测试库真实生效（非法值 IntegrityError）。

迁移验证不在本文件（用 alembic 命令在独立库 mangaflow_mig_test 执行，
见任务报告）。
"""

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Character, Episode, Project, User, VersionSnapshot
from app.services.script_pipeline_service import (
    advance_phase,
    require_phase,
    write_stage_snapshot,
)
from tests.conftest import unique_suffix


async def _make_project(db, script_phase=None, title="阶段测试项目"):
    """辅助：直插一个用户与其项目（可指定 script_phase），返回项目对象。

    Args:
        db: 测试库异步会话。
        script_phase: 项目初始剧本阶段（None=快速模式）。
        title: 项目标题。

    Returns:
        Project: 已 flush 的项目 ORM 对象（含 id）。

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    user = User(
        username=f"phase_{unique_suffix()}",
        password_hash="test-hash-not-for-login",
        role="user",
        status=1,
    )
    db.add(user)
    await db.flush()
    project = Project(user_id=user.id, title=title, status="draft", script_phase=script_phase)
    db.add(project)
    await db.flush()
    return project


# ---------------------------------------------------------------------------
# 1. 阶段守卫 require_phase
# ---------------------------------------------------------------------------
async def test_require_phase_fast_mode_pass(db):
    """快速模式（script_phase=NULL）：任何阶段要求一律放行。"""
    project = await _make_project(db, script_phase=None)
    # 快速模式不拦截：最低阶段与最高阶段都不抛异常
    require_phase(project, "idea")
    require_phase(project, "body")


async def test_require_phase_not_reached_409(db):
    """characters 阶段访问 outline 阶段资源：409，detail 带当前阶段。"""
    project = await _make_project(db, script_phase="characters")
    with pytest.raises(HTTPException) as exc_info:
        require_phase(project, "outline")
    assert exc_info.value.status_code == 409
    # detail 包含当前阶段与前置动作中文提示
    assert "characters" in exc_info.value.detail
    assert "角色" in exc_info.value.detail


async def test_require_phase_equal_and_later_pass(db):
    """等于当前阶段 / 已越过（晚于）所需阶段均放行。"""
    project = await _make_project(db, script_phase="characters")
    require_phase(project, "characters")  # 等于当前阶段
    require_phase(project, "idea")  # 晚于所需阶段（idea 已过）


async def test_require_phase_invalid_phase_400(db):
    """非法阶段名（即使快速模式也先校验合法性）：400。"""
    project = await _make_project(db, script_phase=None)
    with pytest.raises(HTTPException) as exc_info:
        require_phase(project, "storyboard")
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# 2. 阶段推进 advance_phase
# ---------------------------------------------------------------------------
async def test_advance_phase_skip_rejected(db):
    """跳步推进（idea→body）拒绝：409，且原阶段不被修改。"""
    project = await _make_project(db, script_phase="idea")
    with pytest.raises(HTTPException) as exc_info:
        await advance_phase(db, project, "body")
    assert exc_info.value.status_code == 409
    # 跳步被拒后项目阶段保持不变
    assert project.script_phase == "idea"


async def test_advance_phase_step_by_step_valid(db):
    """按序推进（idea→characters）合法：返回值与就地赋值一致。"""
    project = await _make_project(db, script_phase="idea")
    result = await advance_phase(db, project, "characters")
    assert result == "characters"
    assert project.script_phase == "characters"


async def test_advance_phase_from_fast_mode_first_idea_only(db):
    """快速模式项目首次推进仅允许进入 idea（NULL 视作 -1 序）。"""
    project = await _make_project(db, script_phase=None)
    with pytest.raises(HTTPException) as exc_info:
        await advance_phase(db, project, "characters")
    assert exc_info.value.status_code == 409
    result = await advance_phase(db, project, "idea")
    assert result == "idea"
    assert project.script_phase == "idea"


# ---------------------------------------------------------------------------
# 3. 里程碑快照 write_stage_snapshot
# ---------------------------------------------------------------------------
async def test_write_stage_snapshot_fields(db):
    """快照字段正确：flush 后含自增 id，commit 后可重新查询。"""
    project = await _make_project(db, script_phase="outline")
    summary = {"episode_count": 3, "confirmed_characters": 4}
    snapshot = await write_stage_snapshot(db, project, "outline_confirmed", summary)
    # flush 已填充主键与外键，但未提交
    assert snapshot.id is not None
    assert snapshot.project_id == project.id
    assert snapshot.stage == "outline_confirmed"
    assert snapshot.summary == summary
    # 提交后重新查询验证持久化
    await db.commit()
    row = (
        await db.execute(select(VersionSnapshot).where(VersionSnapshot.id == snapshot.id))
    ).scalar_one()
    assert row.stage == "outline_confirmed"
    assert row.summary == summary


async def test_write_stage_snapshot_invalid_stage(db):
    """非法快照阶段：ValueError（服务内部传参错误）。"""
    project = await _make_project(db)
    with pytest.raises(ValueError):
        await write_stage_snapshot(db, project, "idea")


# ---------------------------------------------------------------------------
# 4. 模型层：P-1 新字段读写往返
# ---------------------------------------------------------------------------
async def test_character_new_fields_roundtrip(db):
    """Character 新字段带值往返：commit + 重新查询后字段不丢。"""
    project = await _make_project(db)
    confirmed = datetime.now(timezone.utc)
    relations = [{"target": "陈默", "relation": "漫画搭档"}]
    db.add(Character(
        project_id=project.id,
        name="林小满",
        visual_prompt="统一风格：日漫；主角少女立绘",
        role_type="lead",
        age="约17岁",
        personality="倔强善良，遇挫不折",
        motivation="和挚友一起成为漫画家",
        arc="从畏缩退让到坚定追梦",
        relations=relations,
        confirmed_at=confirmed,
    ))
    await db.commit()
    row = (
        await db.execute(select(Character).where(Character.name == "林小满"))
    ).scalar_one()
    assert row.role_type == "lead"
    assert row.age == "约17岁"
    assert row.personality == "倔强善良，遇挫不折"
    assert row.motivation == "和挚友一起成为漫画家"
    assert row.arc == "从畏缩退让到坚定追梦"
    assert row.relations == relations
    # TIMESTAMPTZ 往返保持时区语义
    assert row.confirmed_at is not None
    assert row.confirmed_at == confirmed


async def test_character_new_fields_nullable_default_none(db):
    """Character 新字段全部可空：不填也能插入，读取为 None。"""
    project = await _make_project(db)
    db.add(Character(
        project_id=project.id,
        name="背景路人",
        visual_prompt="统一风格：日漫；路人角色立绘",
    ))
    await db.commit()
    row = (
        await db.execute(select(Character).where(Character.name == "背景路人"))
    ).scalar_one()
    for field in ("role_type", "age", "personality", "motivation", "arc",
                  "relations", "confirmed_at"):
        assert getattr(row, field) is None, f"新字段 {field} 默认应为 None"


async def test_episode_new_fields_roundtrip(db):
    """Episode 新字段带值往返：logline/outline/confirmed_at 持久化。"""
    project = await _make_project(db)
    confirmed = datetime.now(timezone.utc)
    outline = {
        "core_event": "天台立约，共同报名新人奖",
        "characters": ["林小满", "陈默"],
        "emotion_curve": "平静 → 意外相遇 → 热血立誓",
        "duration_sec": 90,
    }
    db.add(Episode(
        project_id=project.id,
        title="第一集：天台之约",
        sequence=1,
        logline="两个少女在天台相遇并约定一起成为漫画家",
        outline=outline,
        confirmed_at=confirmed,
    ))
    await db.commit()
    row = (
        await db.execute(select(Episode).where(Episode.project_id == project.id))
    ).scalar_one()
    assert row.logline == "两个少女在天台相遇并约定一起成为漫画家"
    assert row.outline == outline
    assert row.confirmed_at == confirmed


async def test_project_script_phase_check_constraint(db):
    """projects.script_phase CHECK 约束真实生效：非法值在 flush 时触发 IntegrityError。"""
    user = User(
        username=f"phase_{unique_suffix()}",
        password_hash="test-hash-not-for-login",
        role="user",
        status=1,
    )
    db.add(user)
    await db.flush()
    bad = Project(user_id=user.id, title="非法阶段项目", status="draft", script_phase="nonsense")
    db.add(bad)
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()  # 释放失败事务，避免影响夹具收尾
