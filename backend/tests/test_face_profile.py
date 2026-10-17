"""面部视觉一致性服务测试模块（文件级说明，P0-4）。

覆盖链路：
- profile_to_text 固定键序规整（纯函数）；
- build_face_profile 建档（mock Agnes 档案 → characters.face_profile 落库
  + asset_embeddings kind='face' 向量行）；
- compare_face_profile：无主档案返回 None；有主档案返回余弦分数；
- runner 闸门影子模式（默认）：记录 face_score 不拦截。
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AssetEmbedding, Character
from app.services.face_profile import (
    build_face_profile,
    compare_face_profile,
    profile_to_text,
)
from tests.conftest import create_project, unique_suffix


_PROFILE_A = {
    "gender": "女性", "age_look": "少女", "face_shape": "鹅蛋脸",
    "hair_style": "长直发", "hair_color": "银白", "eye_color": "红瞳",
    "eyebrow": "柳叶眉", "nose": "小巧", "lips": "樱唇", "skin_tone": "白皙",
    "distinctive_marks": ["泪痣"], "accessories": [],
}


def test_mianbu_dangan_guding_jianxu_guizheng():
    """profile_to_text：固定键序拼接，同档案产出同文本；空档案返回空串。

    Args:
        无。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    text1 = profile_to_text(_PROFILE_A)
    text2 = profile_to_text(dict(reversed(list(_PROFILE_A.items()))))
    assert text1 == text2  # 键序无关
    assert "hair_color:银白" in text1
    assert "distinctive_marks:泪痣" in text1
    assert profile_to_text({}) == ""
    assert profile_to_text(None) == ""


async def test_jiandang_luku_liangxing(client, auth, db: AsyncSession, monkeypatch):
    """build_face_profile：建档写 face_profile + kind='face' 向量行。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        monkeypatch: pytest 打桩。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from app.providers import vision_review

    async def _fake_profile(image_url=None, image_b64=None):
        return {"profile": dict(_PROFILE_A), "model": "mock-vision"}

    monkeypatch.setattr(vision_review, "review_face_profile", _fake_profile)

    project = await create_project(client, auth["headers"], title=f"建档项目_{unique_suffix()}")
    ch = Character(project_id=project["id"], name="林小满",
                   visual_prompt="统一风格插画，林小满")
    db.add(ch)
    await db.commit()
    await db.refresh(ch)

    profile = await build_face_profile(db, ch, "/storage/generated/x.png")
    assert profile is not None
    await db.refresh(ch)
    assert ch.face_profile["hair_color"] == "银白"

    row = (
        await db.execute(
            select(AssetEmbedding).where(
                AssetEmbedding.asset_type == "character",
                AssetEmbedding.asset_id == ch.id,
                AssetEmbedding.kind == "face",
            )
        )
    ).scalar_one_or_none()
    assert row is not None
    assert isinstance(row.embedding, dict) and row.embedding.get("vector")


async def test_bidui_wu_zhudangan_fanhui_none(client, auth, db: AsyncSession):
    """compare_face_profile：库中无主档案（未建档）返回 None。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    project = await create_project(client, auth["headers"], title=f"比对项目_{unique_suffix()}")
    ch = Character(project_id=project["id"], name="陈默",
                   visual_prompt="统一风格插画，陈默")
    db.add(ch)
    await db.commit()
    await db.refresh(ch)

    score = await compare_face_profile(db, ch, "/storage/generated/y.png")
    assert score is None


async def test_bidui_you_zhudangan_fanhui_fenshu(client, auth, db: AsyncSession, monkeypatch):
    """compare_face_profile：有主档案时返回 0~1 余弦分数（mock 视觉模型）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        monkeypatch: pytest 打桩。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from app.providers import vision_review

    async def _fake_profile(image_url=None, image_b64=None):
        return {"profile": dict(_PROFILE_A), "model": "mock-vision"}

    monkeypatch.setattr(vision_review, "review_face_profile", _fake_profile)

    project = await create_project(client, auth["headers"], title=f"比对分项目_{unique_suffix()}")
    ch = Character(project_id=project["id"], name="林小满",
                   visual_prompt="统一风格插画，林小满")
    db.add(ch)
    await db.commit()
    await db.refresh(ch)

    # 先建档
    assert await build_face_profile(db, ch, "/storage/generated/a.png") is not None
    # 再比对（同档案 → 高相似）
    score = await compare_face_profile(db, ch, "/storage/generated/b.png")
    assert score is not None
    assert 0.0 <= score <= 1.0
    assert score >= 0.99  # 同档案文本完全一致，余弦应为 1（或浮点近似）


async def test_yingzi_moshi_jilu_bulanjie(client, auth, db: AsyncSession, monkeypatch):
    """runner 闸门影子模式（默认 enforce=False）：比对低分仅记录 face_score，任务放行 done。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库会话。
        monkeypatch: pytest 打桩。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    from unittest.mock import patch

    from app.providers import vision_review
    from app.services.face_profile import _PROFILE_KEYS  # noqa: F401 确认模块可达
    from tests.conftest import wait_task_terminal

    # 与主档案差异极大的档案（低相似）
    profile_b = {
        "gender": "男性", "age_look": "中年", "face_shape": "方脸",
        "hair_style": "光头", "hair_color": "黑色", "eye_color": "黑瞳",
        "eyebrow": "粗眉", "nose": "高挺", "lips": "厚唇", "skin_tone": "小麦色",
        "distinctive_marks": ["刀疤"], "accessories": ["墨镜"],
    }

    async def _fake_profile(image_url=None, image_b64=None):
        # 首次调用（建档）返回 A 档案，其后（比对）返回 B 档案
        return {"profile": dict(_PROFILE_A if not _fake_profile.called else profile_b),
                "model": "mock-vision"}

    _fake_profile.called = False

    async def _spy(image_url=None, image_b64=None):
        result = await _fake_profile(image_url=image_url, image_b64=image_b64)
        _fake_profile.called = True
        return result

    monkeypatch.setattr(vision_review, "review_face_profile", _spy)

    project = await create_project(client, auth["headers"], title=f"影子项目_{unique_suffix()}")
    resp = await client.post(
        f"/api/projects/{project['id']}/assets/characters",
        headers=auth["headers"], json={"name": "林小满", "description": "元气少女"},
    )
    cid = resp.json()["id"]

    # 第一次生成：建档 + 放行（影子模式不拦截）
    first = await client.post(
        f"/api/projects/{project['id']}/assets/characters/{cid}/generate",
        headers=auth["headers"],
    )
    assert first.status_code == 200, first.text
    task1 = await wait_task_terminal(client, auth["headers"], first.json()["id"])
    assert task1["status"] == "done", task1

    # 第二次生成（替换）：比对低分 → 影子模式记录 face_score 且仍 done
    second = await client.post(
        f"/api/projects/{project['id']}/assets/characters/{cid}/generate",
        headers=auth["headers"],
    )
    assert second.status_code == 200, second.text
    task2 = await wait_task_terminal(client, auth["headers"], second.json()["id"])
    assert task2["status"] == "done", task2

    # TaskOut 不透出 meta，直接查库断言 face_score 已记录（影子模式证据）
    from app.models import Task
    row = await db.get(Task, second.json()["id"])
    await db.refresh(row)
    assert row.meta is not None
    assert "face_score" in row.meta
    assert 0.0 <= row.meta["face_score"] <= 1.0
