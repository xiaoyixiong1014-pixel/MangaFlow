# -*- coding: utf-8 -*-
"""随机关键帧封面查询实测脚本（开发期验证用，不属于交付物）。"""
import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.models import Project, Storyboard  # noqa: E402


async def main() -> None:
    """主流程：取全部项目，按新方案（分组随机）生成封面并两次对比。"""
    async with SessionLocal() as db:
        rows = await db.execute(
            select(Project.id, Project.title).where(Project.deleted_at.is_(None))
        )
        proj_rows = rows.all()
        id_list = [p.id for p in proj_rows]
        titles = {p.id: p.title for p in proj_rows}
        kf_rows = await db.execute(
            select(Storyboard.project_id, Storyboard.keyframe_url)
            .where(Storyboard.project_id.in_(id_list),
                   Storyboard.keyframe_url.isnot(None))
        )
        grouped: dict = {}
        for pid, url in kf_rows.all():
            if url:
                grouped.setdefault(pid, []).append(url)
        print(f"候选项目: {len(proj_rows)}，有关键帧的项目: {len(grouped)}")
        for attempt in (1, 2):
            covers = {pid: urls[random.randrange(len(urls))]
                      for pid, urls in grouped.items()}
            print(f"第 {attempt} 次：{len(covers)} 个项目有封面")
            for pid in list(covers)[:4]:
                print(f"  [{pid}] {titles.get(pid)} -> {covers[pid]}")
            for pid, title in proj_rows:
                if pid not in covers:
                    print(f"  [{pid}] {title} -> 渐变占位")


if __name__ == "__main__":
    asyncio.run(main())
