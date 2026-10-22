# -*- coding: utf-8 -*-
"""素材库聚合数据源现状排查脚本（开发期验证用，不属于交付物）。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402


async def main() -> None:
    """主流程：scripts.content 结构 / characters 试音 / film_outputs 数量 / 关键帧数量。"""
    async with SessionLocal() as db:
        row = (await db.execute(text(
            "SELECT project_id, episode_id, content, raw_text IS NOT NULL AS has_raw "
            "FROM scripts ORDER BY id DESC LIMIT 1"
        ))).first()
        if row:
            pid, eid, content, has_raw = row
            print(f"scripts 样例 project={pid} episode={eid} has_raw={has_raw}")
            print("content keys:", list(content.keys()) if isinstance(content, dict) else type(content))
            if isinstance(content, dict):
                for k, v in content.items():
                    print(f"  {k}: {str(v)[:60]}")
        n_chars = (await db.execute(text(
            "SELECT COUNT(*) FROM characters WHERE voice_sample_url IS NOT NULL"
        ))).scalar()
        print("有试音的角色数:", n_chars)
        sample = (await db.execute(text(
            "SELECT id, name, voice_sample_url FROM characters WHERE voice_sample_url IS NOT NULL LIMIT 2"
        ))).all()
        for r in sample:
            print(" ", r)
        n_films = (await db.execute(text(
            "SELECT COUNT(*) FROM film_outputs"
        ))).scalar()
        print("成片版本数:", n_films)
        n_kf = (await db.execute(text(
            "SELECT COUNT(*) FROM storyboards sb JOIN projects p ON p.id=sb.project_id "
            "WHERE sb.keyframe_url IS NOT NULL AND sb.keyframe_url NOT LIKE '%%.svg' "
            "AND p.deleted_at IS NULL"
        ))).scalar()
        print("有效关键帧数:", n_kf)
        n_ma = (await db.execute(text(
            "SELECT media_type, COUNT(*) FROM media_assets WHERE deleted_at IS NULL GROUP BY media_type"
        ))).all()
        print("media_assets 现状:", n_ma)


if __name__ == "__main__":
    asyncio.run(main())
