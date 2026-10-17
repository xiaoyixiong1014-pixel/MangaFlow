# -*- coding: utf-8 -*-
"""临时排查脚本：检查项目 25 按集抽取链路的实际数据。

输出：
1. episodes 表各集的 outline.characters（出场角色）
2. 集级剧本行 content.scenes 的 location/time_of_day/int_ext 是否有值
3. scripts.fences 最近一次抽取的 stats 与抽取出的角色/场景名
4. characters/scenes 资产现状（enabled_at）
"""
import asyncio
import json
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from sqlalchemy import select
from app.core.database import SessionLocal
from app.models import Episode, Script, Character, Scene, Project


async def main() -> None:
    """主流程：逐项输出排查数据。"""
    async with SessionLocal() as db:
        proj = (await db.execute(
            select(Project).where(Project.title == "摸鱼之神", Project.deleted_at.is_(None))
            .order_by(Project.id.desc())
        )).scalars().first()
        if proj is None:
            print("project 摸鱼之神 not found")
            return
        print(f"== project id={proj.id} title={proj.title} input_mode_config_keys={list((proj.config or {}).keys())}")

        eps = (await db.execute(select(Episode).where(Episode.project_id == proj.id).order_by(Episode.sequence))).scalars().all()
        for ep in eps:
            outline = ep.outline if isinstance(ep.outline, dict) else {}
            print(f"\n-- episode id={ep.id} seq={ep.sequence} title={ep.title}")
            print(f"   outline.characters = {outline.get('characters')}")
            print(f"   outline.core_event = {str(outline.get('core_event') or '')[:60]}")

        scripts = (await db.execute(select(Script).where(Script.project_id == proj.id).order_by(Script.id))).scalars().all()
        for sc in scripts:
            content = sc.content if isinstance(sc.content, dict) else {}
            plain = content.get("content") if isinstance(content.get("content"), dict) else content
            scenes = plain.get("scenes") if isinstance(plain, dict) else None
            print(f"\n-- script id={sc.id} episode_id={sc.episode_id} raw_text_len={len(sc.raw_text or '')}")
            if isinstance(scenes, list):
                print(f"   scenes count={len(scenes)}")
                for s in scenes[:3]:
                    if isinstance(s, dict):
                        print(f"   scene_no={s.get('scene_no')} title={s.get('title')} location={s.get('location')!r} time={s.get('time_of_day')!r} int_ext={s.get('int_ext')!r}")
            else:
                print(f"   content keys={list(content.keys()) if isinstance(content, dict) else type(content)}")
            fences = sc.fences if isinstance(sc.fences, dict) else None
            if fences:
                stats = fences.get("stats")
                chars = [c.get("name") for c in fences.get("characters", [])]
                scn = [s.get("name") for s in fences.get("scenes", [])]
                prps = [p.get("name") for p in fences.get("props", [])]
                print(f"   fences.stats={stats}")
                print(f"   fences.characters={chars}")
                print(f"   fences.scenes={scn}")
                print(f"   fences.props={prps}")

        chars = (await db.execute(select(Character).where(Character.project_id == proj.id))).scalars().all()
        print(f"\n== characters total={len(chars)}")
        for c in chars:
            print(f"   id={c.id} name={c.name} enabled_at={c.enabled_at} deleted_at={c.deleted_at}")
        scns = (await db.execute(select(Scene).where(Scene.project_id == proj.id))).scalars().all()
        print(f"== scenes total={len(scns)}")
        for s in scns:
            print(f"   id={s.id} name={s.name} deleted_at={s.deleted_at}")


asyncio.run(main())
