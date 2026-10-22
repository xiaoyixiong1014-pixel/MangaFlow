# -*- coding: utf-8 -*-
"""素材库聚合接口实测脚本（开发期验证用，不属于交付物）。"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.api.media import list_media  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402


async def main() -> None:
    """主流程：全类型聚合 + 各类型单独聚合 + source=project 过滤。"""
    from sqlalchemy import func, select
    from app.models import Character, FilmOutput, Project, Storyboard

    # 自动探测拥有关键帧最多的用户（避免硬编码 user_id 造成假空）
    async with SessionLocal() as db0:
        rows = await db0.execute(
            select(Project.user_id, func.count(Storyboard.id))
            .join(Storyboard, Storyboard.project_id == Project.id)
            .where(Storyboard.keyframe_url.isnot(None), Project.deleted_at.is_(None))
            .group_by(Project.user_id)
            .order_by(func.count(Storyboard.id).desc())
            .limit(1)
        )
        row = rows.first()
        uid = row[0] if row else 1
        print(f"关键帧归属用户探测: user_id={uid} (count={row[1] if row else 0})")
        # 顺带核对该用户的成片 / 试音存量
        for label, stmt in (
            ("成片", select(func.count(FilmOutput.id)).join(Project, Project.id == FilmOutput.project_id).where(Project.user_id == uid)),
            ("试音", select(func.count(Character.id)).join(Project, Project.id == Character.project_id).where(Project.user_id == uid, Character.voice_sample_url.isnot(None))),
        ):
            r = await db0.execute(stmt)
            print(f"  该用户 {label} 数量: {r.scalar()}")

    user = SimpleNamespace(id=uid)
    async with SessionLocal() as db:
        resp = await list_media(
            media_type=None, source=None, project_id=None, search=None,
            page=1, page_size=500, user=user, db=db,
        )
    by_key: dict = {}
    for m in resp.items:
        key = (m.source, m.media_type)
        by_key[key] = by_key.get(key, 0) + 1
    print(f"total={resp.total}")
    for (src, mt), n in sorted(by_key.items()):
        print(f"  {src}/{mt}: {n}")
    # 抽样：各类型第一条聚合产物
    for mt in ("image", "video", "audio", "text"):
        first = next((m for m in resp.items if m.source == "project" and m.media_type == mt), None)
        if first:
            meta = first.meta or {}
            print(f"[{mt}] id={first.id} url={first.url[:50]} meta={ {k: str(v)[:30] for k, v in list(meta.items())[:4]} }")
        else:
            print(f"[{mt}] 无聚合条目")
    # source=project 过滤
    async with SessionLocal() as db2:
        resp2 = await list_media(
            media_type=None, source="project", project_id=None, search=None,
            page=1, page_size=10, user=user, db=db2,
        )
    print(f"source=project 过滤 total={resp2.total}")
    # 删除防护
    from app.api.media import delete_media
    async with SessionLocal() as db3:
        try:
            await delete_media(-1, user=user, db=db3)
        except Exception as e:  # noqa: BLE001
            print(f"删除聚合条目拦截: {getattr(e, 'status_code', '?')} {getattr(e, 'detail', e)}")


if __name__ == "__main__":
    asyncio.run(main())
