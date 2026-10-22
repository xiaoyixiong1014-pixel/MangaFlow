# -*- coding: utf-8 -*-
"""查询最近合成任务的 BGM 配置与该集分镜音轨状态（开发期排查用）。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402


async def main() -> None:
    """主流程：compose 任务 meta + 相关分镜 audio 状态。"""
    async with SessionLocal() as db:
        tasks = (await db.execute(text(
            "SELECT id, status, meta FROM tasks WHERE type='compose' ORDER BY id DESC LIMIT 2"
        ))).all()
        for tid, status, meta in tasks:
            m = meta or {}
            print(f"compose#{tid} status={status} bgm={m.get('bgm')} subs={m.get('subtitles')} "
                  f"intro={m.get('intro_card')} episode={m.get('episode_id')}")
        sbs = (await db.execute(text(
            "SELECT id, sequence, video_url IS NOT NULL AS has_video, "
            "audio_url IS NOT NULL AS has_audio, audio->>'video_dubbed' AS dubbed, "
            "audio->>'burned_subtitles' AS burned "
            "FROM storyboards WHERE project_id=34 AND episode_id=33 ORDER BY sequence"
        ))).all()
        for r in sbs:
            print(r)


if __name__ == "__main__":
    asyncio.run(main())
