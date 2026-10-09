"""数据库初始化模块（文件级说明）。

职责：
- ensure_database(): 连接 postgres 系统库，若 mangaflow 业务库不存在则创建
- init_database(): 建库 → 建表（create_all）→ 写入幂等种子数据
- seed_data(): 种子数据（用户/钱包/渠道/逻辑模型/提示词/公告/站点设置 KV）

幂等策略：所有种子均按业务键"存在即跳过"，可重复执行。
可直接运行：`python -m app.core.init_db`（cwd=backend）。
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.crypto import encrypt_text
from app.core.database import Base, SessionLocal
from app.core.security import hash_password
from app.models import (  # noqa: F401 —— 导入全部模型以注册到 Base.metadata
    Announcement,
    LogicalModel,
    LanggraphStore,
    ModelChannel,
    Prompt,
    User,
    Wallet,
)

logger = logging.getLogger("mangaflow.init_db")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


async def ensure_database() -> None:
    """确保业务数据库存在，不存在则自动创建。

    使用 admin_database_url 连接 postgres 系统库，以 AUTOCOMMIT 模式执行
    CREATE DATABASE（该语句不能在事务块内运行）。

    Raises:
        Exception: PostgreSQL 连接失败或建库失败时向上抛出。
    """
    # 从业务库连接串中解析目标库名（不硬编码）
    target_db = make_url(settings.database_url).database
    admin_engine = create_async_engine(
        settings.admin_database_url,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    try:
        async with admin_engine.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target_db}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{target_db}" ENCODING \'UTF8\''))
                logger.info("数据库 %s 已创建", target_db)
            else:
                logger.info("数据库 %s 已存在，跳过创建", target_db)
    finally:
        await admin_engine.dispose()


async def seed_data() -> None:
    """写入幂等种子数据（存在即跳过）。

    种子内容：
    1. 用户：admin/operator（1000000 积分）、demo/user（50000 积分），各带钱包
    2. 模型渠道：id=1 平台 DeepSeek 渠道（API Key 经 crypto.encrypt_text
       加密为 Fernet 密文落库）
    3. 逻辑模型：llm/image/video/tts 四种能力各一条；mock 模型也指向渠道 1，
       属本地占位实现（未接入真实图像/视频/配音渠道）
    4. 公共提示词：5 条（capability：角色定妆/分镜画面/运镜/配音）
    5. 公告：2 条已发布（一条置顶）
    6. 站点设置 KV：写入 langgraph_store（namespace='site_settings'），
       复用长期记忆表承载站点开关，避免偏离 31 表架构新增表

    Raises:
        Exception: 数据库写入异常向上抛出。
    """
    async with SessionLocal() as session:
        # ---------- 1. 用户与钱包 ----------
        seed_users = [
            ("admin", "admin123", "operator", 1_000_000),
            ("demo", "demo123456", "user", 50_000),
        ]
        for username, password, role, points in seed_users:
            exists = await session.scalar(select(User).where(User.username == username))
            if exists:
                logger.info("用户 %s 已存在，跳过", username)
                continue
            user = User(
                username=username,
                password_hash=hash_password(password),
                role=role,
                status=1,
            )
            session.add(user)
            await session.flush()  # 取 user.id 供钱包外键使用
            session.add(Wallet(user_id=user.id, points_balance=points))
            logger.info("种子用户 %s（%s）与钱包已创建，初始积分 %d", username, role, points)

        # ---------- 2. 平台 DeepSeek 渠道（固定 id=1） ----------
        channel = await session.get(ModelChannel, 1)
        if channel:
            logger.info("模型渠道 id=1 已存在，跳过")
        else:
            session.add(ModelChannel(
                id=1,
                protocol="openai",
                base_url=settings.deepseek_base_url,
                # Fernet 密文落库，与加密工具 crypto.encrypt_text 对齐
                api_key_encrypted=encrypt_text(settings.deepseek_api_key),
                owner_user_id=None,  # NULL = 平台渠道
                health_status="healthy",
                enabled=True,
                last_check_at=datetime.now(timezone.utc),
                models={"chat": [settings.deepseek_model]},
            ))
            logger.info("种子模型渠道 id=1（DeepSeek）已创建")

        # ---------- 3. 逻辑模型（4 种能力各一条） ----------
        # mock 模型的 channel_priority 也指向渠道 1：本地占位实现，未接入真实渲染渠道
        seed_models = [
            ("llm", "deepseek-flash", True,
             {"input_per_1k": 1, "output_per_1k": 2},
             "通用文本创作，适用于剧本生成、拆解与对话",
             "https://api-docs.deepseek.com"),
            ("image", "mock-image", True,
             {"per_image": 10},
             "本地占位图像生成模型（Mock），用于开发联调",
             None),
            ("video", "mock-video", True,
             {"per_second": 20},
             "本地占位视频生成模型（Mock），用于开发联调",
             None),
            ("tts", "mock-tts", True,
             {"per_100_chars": 5},
             "本地占位配音模型（Mock），用于开发联调",
             None),
        ]
        for capability, name, is_default, price, scene_desc, doc_url in seed_models:
            exists = await session.scalar(
                select(LogicalModel).where(LogicalModel.capability == capability, LogicalModel.name == name)
            )
            if exists:
                logger.info("逻辑模型 %s/%s 已存在，跳过", capability, name)
                continue
            session.add(LogicalModel(
                capability=capability,
                name=name,
                channel_priority=[1],  # 通过渠道 id 引用，指向 DeepSeek 渠道（mock 同）
                is_default=is_default,
                price_per_unit=price,
                scene_desc=scene_desc,
                doc_url=doc_url,
            ))
            logger.info("种子逻辑模型 %s/%s 已创建", capability, name)

        # ---------- 4. 公共提示词（5 条，scope=public） ----------
        seed_prompts = [
            ("角色定妆 · 全身立绘", "角色定妆", "character",
             ["定妆", "立绘"],
             "为角色「{name}」生成全身定妆立绘。外观设定：{description}。"
             "要求：单一角色居中构图、纯色背景、全身可见、面部清晰、光线均匀柔和；"
             "服装与发型严格符合设定；统一动漫风格，线条干净，色块分明；禁止多余文字与水印。"),
            ("角色定妆 · 表情差分", "角色定妆", "character",
             ["表情", "差分"],
             "基于角色「{name}」的定妆形象生成表情差分（喜怒哀惊各一张）。"
             "要求：面部特征与定妆照完全一致，仅改变表情与眼神；头部特写构图；"
             "保持统一光源方向；背景使用与定妆照相同的纯色。"),
            ("分镜画面 · 关键帧构图", "分镜画面", "storyboard",
             ["分镜", "关键帧"],
             "画面主体：{subject}。场景：{scene}。镜头景别：{shot_type}。"
             "要求：忠实呈现分镜描述「{visual_description}」；角色面部特征与定妆照保持一致；"
             "应用全局风格锚点「{style}」；构图注意视线引导与负空间；电影感光影；画面中禁止出现文字。"),
            ("运镜 · 镜头语言设计", "运镜", "camera",
             ["运镜", "镜头语言"],
             "为镜头设计运镜方案。当前景别：{shot_type}，情绪基调：{emotion}。"
             "可选运镜：推（强调主体）、拉（揭示环境）、摇（跟随扫视）、移（建立空间）、"
             "跟（角色跟随）、升格（情绪强调）。"
             '输出 JSON：{"camera_movement": "...", "duration": 秒数, "reason": "一句话理由"}。'),
            ("配音 · 台词情绪朗读", "配音", "tts",
             ["配音", "朗读"],
             "将台词转换为自然口语朗读指令。台词：「{dialogue}」。角色情绪：{emotion}。"
             "要求：标注停顿位置与重音词；句尾语气与情绪匹配；数字与英文按中文习惯读出；"
             "输出分句朗读标记。"),
        ]
        for title, capability, category, tags, content in seed_prompts:
            exists = await session.scalar(select(Prompt).where(Prompt.title == title))
            if exists:
                logger.info("提示词 %s 已存在，跳过", title)
                continue
            session.add(Prompt(
                user_id=None,  # NULL = 公共提示词
                scope="public",
                capability=capability,
                title=title,
                content=content,
                category=category,
                tags=tags,
            ))
            logger.info("种子公共提示词 %s 已创建", title)

        # ---------- 5. 公告（2 条已发布） ----------
        seed_announcements = [
            ("漫序 MangaFlow 平台正式上线", True,
             "漫序 MangaFlow —— AI 漫剧一站式生产平台正式上线！"
             "支持剧本创作、角色定妆、分镜生成、画布编排与成片发布的完整工作流，"
             "欢迎体验并在个人中心反馈建议。"),
            ("新用户注册即送 50,000 创作积分", False,
             "即日起，新注册用户将自动获得 50,000 创作积分，可用于角色定妆、"
             "分镜图生成与视频合成。积分可用于全部 AI 生成能力，祝创作愉快！"),
        ]
        for title, is_pinned, content in seed_announcements:
            exists = await session.scalar(select(Announcement).where(Announcement.title == title))
            if exists:
                logger.info("公告 %s 已存在，跳过", title)
                continue
            session.add(Announcement(
                title=title,
                content=content,
                is_pinned=is_pinned,
                status="published",
                published_at=datetime.now(timezone.utc),
            ))
            logger.info("种子公告 %s 已创建", title)

        # ---------- 6. 站点设置 KV（复用 langgraph_store 表承载） ----------
        site_key = await session.scalar(
            select(LanggraphStore).where(
                LanggraphStore.namespace == "site_settings", LanggraphStore.key == "site")
        )
        if site_key:
            logger.info("站点设置 KV 已存在，跳过")
        else:
            session.add(LanggraphStore(
                namespace="site_settings",
                key="site",
                value={"registration_enabled": True, "site_name": "漫序 MangaFlow"},
            ))
            logger.info("种子站点设置 KV 已创建")

        await session.commit()
        logger.info("种子数据写入完成")


async def init_database() -> None:
    """数据库初始化总入口：建库 → 建表 → 种子数据。

    Raises:
        Exception: 建库/建表/种子任一环节失败时向上抛出。
    """
    from app.core.database import engine

    await ensure_database()
    # create_all 为幂等操作（checkfirst=True 默认，存在即跳过）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_data()


if __name__ == "__main__":
    asyncio.run(init_database())
