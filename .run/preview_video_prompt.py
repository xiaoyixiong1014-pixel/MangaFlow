# -*- coding: utf-8 -*-
"""视频提示词组装预览脚本（开发期验证用，不属于交付物）。

用真实分镜数据走 create_video_task 同款组装逻辑（不入库、不扣费），
打印动作驱动型 VIDEO_DIRECTIVE 生效后的完整提示词。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.models import Project, Storyboard  # noqa: E402
from app.services.generation_service import (  # noqa: E402
    VIDEO_DIRECTIVE,
    build_prompt,
    split_prompt_negative,
)


async def main() -> None:
    """主流程：取分镜 615 组装视频提示词并打印。"""
    async with SessionLocal() as db:
        sb = await db.get(Storyboard, 615)
        proj = await db.get(Project, sb.project_id)
        locked_positive, _ = split_prompt_negative(sb.prompt_locked)
        locked_positive = locked_positive.replace("/固定", "").replace("运镜：固定", "运镜")
        sb_audio = sb.audio or {}
        video_parts = []
        if (sb.visual_description or "").strip():
            video_parts.append(f"分镜画面描述：{sb.visual_description.strip()}")
        if sb.camera_angle:
            video_parts.append(f"机位角度：{sb.camera_angle}")
        if (sb.lighting or "").strip():
            video_parts.append(f"环境光影：{sb.lighting}")
        emotion = str(sb_audio.get("emotion") or "").strip()
        if emotion:
            video_parts.append(f"镜头情绪：{emotion}")
        if (sb.dialogue or "").strip():
            video_parts.append(
                f"角色对白（说话角色口部自然开合，并配合台词情绪做出表情与肢体反应）：{sb.dialogue.strip()}"
            )
        prompt = build_prompt(
            *video_parts, locked_positive, sb.prompt_editable, proj.style, VIDEO_DIRECTIVE,
        )
    print("=" * 60)
    print(prompt)
    print("=" * 60)
    print("长度:", len(prompt))


if __name__ == "__main__":
    asyncio.run(main())
