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
    CanvasPlugin,
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
    2. 模型渠道：id=1 平台 DeepSeek 渠道；id=2~5 真实渠道（Agnes 文本/图像/视频、
       百炼 TTS）；id=6~8 本地 mock 渠道（protocol=mock）。全部 API Key 经
       crypto.encrypt_text 加密为 Fernet 密文落库（mock 渠道无 Key，置 NULL）
    3. 逻辑模型：llm 沿用 deepseek-chat（降级链 [1,2]）；image/video/tts 新增
       Agnes/百炼真实模型并设为各能力默认（降级链含真实渠道 + mock 兜底），
       既有 mock 模型降为备选
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

        # ---------- 2.1 平台渠道扩展（id=2~5 真实渠道 + id=6~8 本地 mock 渠道） ----------
        # models 目录沿用渠道 1 的 {"能力键": [模型名]} 结构；真实渠道初始健康
        # 状态为 unknown（等待探测），mock 渠道恒为 healthy；mock 渠道无 Key，
        # api_key_encrypted 按 nullable 列约束置 NULL
        seed_channels = [
            (2, "openai", settings.agnes_llm_base_url, settings.agnes_llm_api_key,
             {"chat": ["agnes-3.0-flash"]}, "unknown"),
            (3, "openai", settings.agnes_image_base_url, settings.agnes_image_api_key,
             {"image": ["agnes-image-2.5-flash"]}, "unknown"),
            (4, "openai", settings.agnes_video_base_url, settings.agnes_video_api_key,
             {"video": ["agnes-video-2.5-flash"]}, "unknown"),
            (5, "openai", settings.bailian_tts_base_url, settings.bailian_tts_api_key,
             {"tts": [settings.bailian_tts_model]}, "unknown"),
            # id=9：百炼视频渠道（wan2.7-i2v 图生视频，DashScope 原生异步协议，
            # base_url 为根域名 /api/v1/... 原生路径；Key 与 TTS 渠道共用）
            (9, "dashscope", settings.bailian_wan_base_url, settings.bailian_tts_api_key,
             {"video": [settings.bailian_wan_model]}, "unknown"),
            (6, "mock", "local://mock", None, {"image": ["mock-image"]}, "healthy"),
            (7, "mock", "local://mock", None, {"video": ["mock-video"]}, "healthy"),
            (8, "mock", "local://mock", None, {"tts": ["mock-tts"]}, "healthy"),
        ]
        for cid, protocol, base_url, api_key, models, health in seed_channels:
            if await session.get(ModelChannel, cid):
                logger.info("模型渠道 id=%d 已存在，跳过", cid)
                continue
            session.add(ModelChannel(
                id=cid,
                protocol=protocol,
                base_url=base_url,
                # 真实渠道 Key 加密落库；mock 渠道无 Key 置 NULL
                api_key_encrypted=encrypt_text(api_key) if api_key else None,
                owner_user_id=None,  # NULL = 平台渠道
                health_status=health,
                enabled=True,
                last_check_at=datetime.now(timezone.utc) if health == "healthy" else None,
                models=models,
            ))
            logger.info("种子模型渠道 id=%d（%s %s）已创建", cid, protocol, base_url)

        # ---------- 3. 逻辑模型（基础四条；image/video/tts 的 mock 备选链指向 mock 渠道 6~8） ----------
        seed_models = [
            ("llm", "deepseek-chat", True,
             {"input_per_1k": 1, "output_per_1k": 2},
             "通用文本创作，适用于剧本生成、拆解与对话",
             "https://api-docs.deepseek.com",
             [1]),
            ("image", "mock-image", False,
             {"per_image": 10},
             "本地占位图像生成模型（Mock），用于开发联调与兜底",
             None,
             [6]),
            ("video", "mock-video", False,
             {"per_second": 20},
             "本地占位视频生成模型（Mock），用于开发联调与兜底",
             None,
             [7]),
            ("tts", "mock-tts", False,
             {"per_100_chars": 5},
             "本地占位配音模型（Mock），用于开发联调与兜底",
             None,
             [8]),
        ]
        for capability, name, is_default, price, scene_desc, doc_url, priority in seed_models:
            exists = await session.scalar(
                select(LogicalModel).where(LogicalModel.capability == capability, LogicalModel.name == name)
            )
            if exists:
                logger.info("逻辑模型 %s/%s 已存在，跳过", capability, name)
                continue
            session.add(LogicalModel(
                capability=capability,
                name=name,
                channel_priority=priority,  # 通过渠道 id 引用，表达故障降级顺序
                is_default=is_default,
                price_per_unit=price,
                scene_desc=scene_desc,
                doc_url=doc_url,
            ))
            logger.info("种子逻辑模型 %s/%s 已创建", capability, name)

        # ---------- 3.1 逻辑模型扩展（llm 降级链补 Agnes 兜底；image/video/tts 增设真实默认模型） ----------
        # llm 主模型 deepseek-chat 降级链：DeepSeek(1) → Agnes 文本(2)；
        # 兼容旧种子名 deepseek-flash（2026-10-10 实测该模型名网关空回复，现役为 deepseek-chat）
        lm = await session.scalar(
            select(LogicalModel).where(LogicalModel.capability == "llm", LogicalModel.name == "deepseek-chat")
        )
        if lm is None:
            lm = await session.scalar(
                select(LogicalModel).where(LogicalModel.capability == "llm", LogicalModel.name == "deepseek-flash")
            )
            if lm is not None:
                lm.name = "deepseek-chat"
                logger.info("逻辑模型 llm 显示名 deepseek-flash -> deepseek-chat（模型名实测修正）")
        if lm is not None and lm.channel_priority != [1, 2]:
            lm.channel_priority = [1, 2]
            logger.info("逻辑模型 llm/deepseek-flash 降级链已更新为 [1, 2]")

        # image/video/tts 新增真实模型为各能力默认（部分唯一索引 idx_lm_default
        # 要求同能力仅一行默认，故先降级既有 mock 默认为备选再插入）
        seed_real_models = [
            ("image", "agnes-image-2.5-flash", 6,
             {"per_image": 10},
             "Agnes 竖版图像生成，适用于角色定妆与分镜关键帧",
             [3, 6], "https://apihub.agnes-ai.com"),
            # video 默认：百炼 wan2.7-i2v 图生视频（首帧驱动，本地关键帧以
            # base64 传入）；Agnes 渠道免费队列高峰常满载（video_queue_full），
            # 保留为链中备选，mock 兜底
            ("video", "wan2.7-i2v", 7,
             {"per_second": 20},
             "阿里云百炼通义万相图生视频，以分镜关键帧为首帧驱动动态化",
             [9, 4, 7], None),
            ("tts", "qwen-audio-3.1-tts-flash", 8,
             {"per_100_chars": 5},
             "阿里云百炼语音合成，适用于台词配音",
             [5, 8], None),
        ]
        for capability, name, mock_channel_id, price, scene_desc, priority, doc_url in seed_real_models:
            # 同能力互斥默认：既有 mock 默认模型降为备选（仅指向本地 mock 渠道）
            old_default = await session.scalar(
                select(LogicalModel).where(
                    LogicalModel.capability == capability, LogicalModel.is_default.is_(True)
                )
            )
            if old_default is not None and old_default.name != name:
                old_default.is_default = False
                old_default.channel_priority = [mock_channel_id]
                logger.info("逻辑模型 %s/%s 已降为备选（priority=[%d]）",
                            capability, old_default.name, mock_channel_id)
            exists = await session.scalar(
                select(LogicalModel).where(LogicalModel.capability == capability, LogicalModel.name == name)
            )
            if exists:
                logger.info("逻辑模型 %s/%s 已存在，跳过", capability, name)
                continue
            session.add(LogicalModel(
                capability=capability,
                name=name,
                channel_priority=priority,  # 真实渠道在前，mock 渠道兜底
                is_default=True,
                price_per_unit=price,
                scene_desc=scene_desc,
                doc_url=doc_url,
            ))
            logger.info("种子逻辑模型 %s/%s 已创建（能力默认）", capability, name)

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

        # ---------- 6.1 内置插件（Task 9 CV-20/21：builtin:text-enhance，已审核已启用） ----------
        # 挂在 admin（operator）用户名下；admin 不存在（如测试库）时跳过。
        # 幂等键：name + plugin_url 组合查询（不依赖用户 id）。
        builtin_name, builtin_url = "文本增强", "builtin:text-enhance"
        builtin_exists = await session.scalar(
            select(CanvasPlugin).where(
                CanvasPlugin.name == builtin_name, CanvasPlugin.plugin_url == builtin_url)
        )
        if builtin_exists:
            logger.info("内置插件 %s 已存在，跳过", builtin_url)
        else:
            admin_user = await session.scalar(select(User).where(User.username == "admin"))
            if admin_user is None:
                logger.warning("种子内置插件跳过：admin 用户不存在")
            else:
                session.add(CanvasPlugin(
                    user_id=admin_user.id,
                    plugin_url=builtin_url,
                    name=builtin_name,
                    version="1.0",
                    permissions={
                        "scopes": ["text:enhance"],
                        "inputs": [{"name": "text", "type": "text"}],
                        "outputs": [{"name": "text", "type": "text"}],
                        "description": "内置文本规则增强：去重复标点、折叠重复词、补句号、清理首尾空白。",
                    },
                    granted=True,
                    enabled=True,
                    reviewed=True,  # 内置插件出厂即通过审核（approved）
                ))
                logger.info("种子内置插件 %s（%s）已创建", builtin_name, builtin_url)

        await session.commit()
        logger.info("种子数据写入完成")

        # ---------- 7. BGM 曲库目录（P-14，文件系统种子） ----------
        # 曲库为纯文件目录（不入库）：运营方经 POST /api/bgm 上传音频文件至此，
        # GET /api/bgm 列出目录内容；此处仅保证目录存在并写入说明文件（幂等）。
        bgm_dir = settings.storage_path() / "bgm"
        bgm_dir.mkdir(parents=True, exist_ok=True)
        readme = bgm_dir / "README.txt"
        if not readme.exists():
            readme.write_text(
                "BGM 曲库目录（storage/bgm）\n"
                "==========================\n"
                "本目录存放平台曲库背景音乐文件（mp3/wav/m4a/aac/flac/ogg），由运营方经\n"
                "POST /api/bgm 上传、DELETE /api/bgm/{name} 下架、GET /api/bgm 列表查询。\n"
                "成片合成时通过 compose 请求的 bgm.url（/storage/bgm/<文件名>）引用。\n"
                "用户自定义 BGM 可经 /api/media/upload 上传后以 /storage/uploads/... 地址直传。\n",
                encoding="utf-8",
            )
        logger.info("BGM 曲库目录已就绪：%s", bgm_dir)


async def init_database() -> None:
    """数据库初始化总入口：建库 → 建表 → 补列 → 种子数据。

    Raises:
        Exception: 建库/建表/补列/种子任一环节失败时向上抛出。
    """
    from app.core.database import engine

    await ensure_database()
    # create_all 为幂等操作（checkfirst=True 默认，存在即跳过）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _add_missing_columns()
    await seed_data()


# 存量库补列清单（单一事实来源：主库初始化与测试库建表共用）。
# 使用 PostgreSQL 原生幂等 DDL（ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS）。
# (目标表, 补列/建索引 DDL)
MISSING_COLUMN_DDL_STATEMENTS: list = [
    ("agent_threads",
     "ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS rewind_checkpoint VARCHAR(80)"),
    # Task 2.1：任务优先级（存量行填充默认值 5）
    ("tasks", "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 5"),
    # Task 2.1：指数退避下次重试时间（可空）
    ("tasks", "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ"),
    # Task 2.1：死信标记（存量行填充默认值 false）
    ("tasks", "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS dead_letter BOOLEAN NOT NULL DEFAULT FALSE"),
    # Task 2.1：补列对应的调度/死信查询索引（幂等建索引）
    ("tasks", "CREATE INDEX IF NOT EXISTS ix_tasks_priority ON tasks (priority)"),
    ("tasks", "CREATE INDEX IF NOT EXISTS ix_tasks_dead_letter ON tasks (dead_letter)"),
    # Task 4：审核记录机审方式（keyword/llm-text/gate，可空）
    ("review_records", "ALTER TABLE review_records ADD COLUMN IF NOT EXISTS method VARCHAR(20)"),
    # Task 4（C-04）：分级处罚——账号状态与违规累计次数（存量行填充默认值）
    ("users", "ALTER TABLE users ADD COLUMN IF NOT EXISTS account_status VARCHAR(20) NOT NULL DEFAULT 'active'"),
    ("users", "ALTER TABLE users ADD COLUMN IF NOT EXISTS violation_count INTEGER NOT NULL DEFAULT 0"),
    # ---------- 列注释对齐（Alembic 基线引入，Task 3） ----------
    # 裸 ALTER TABLE 补列不会带 COMMENT ON COLUMN，而 ORM 模型声明了 comment，
    # autogenerate 的注释比较（alembic 1.20 起内置）会将其识别为 modify_comment
    # 噪音 diff。此处按 ORM 注释原文逐列补齐（COMMENT ON 幂等，重复执行仅覆盖），
    # 使存量库与「ORM / create_all / 初始迁移」三方的列注释完全一致。
    ("agent_threads",
     "COMMENT ON COLUMN agent_threads.rewind_checkpoint IS "
     "'回溯目标检查点 ID（一次性，分叉续聊后清空）'"),
    ("prompts",
     "COMMENT ON COLUMN prompts.status IS "
     "'上下架状态：published 已上架 / offline 已下架'"),
    ("review_records",
     "COMMENT ON COLUMN review_records.method IS "
     "'机审方式：keyword 词表 / llm-text 大模型 / gate 闸门聚合（Task 4.1）'"),
    ("tasks", "COMMENT ON COLUMN tasks.priority IS '调度优先级（0-9，越大越先执行，默认 5）'"),
    ("tasks",
     "COMMENT ON COLUMN tasks.next_retry_at IS "
     "'下次允许重试时间（指数退避；NULL 表示立即可领取）'"),
    ("tasks",
     "COMMENT ON COLUMN tasks.dead_letter IS "
     "'死信标记（重试耗尽仍失败，进入管理端死信队列）'"),
    ("tasks",
     "COMMENT ON COLUMN tasks.meta IS "
     "'任务附加信息：目标资产/提示词/时长等'"),
    ("users",
     "COMMENT ON COLUMN users.account_status IS "
     "'账号状态（C-04 分级处罚）：active/restricted/banned'"),
    ("users",
     "COMMENT ON COLUMN users.violation_count IS "
     "'内容违规累计次数（C-04：1-2 次受限、≥3 次禁用）'"),
    # Task 9（CV-21/19）：节点类型白名单升级——canvas_nodes 增加 plugin/mcp_tool。
    # 存量库旧 CHECK 约束仅含 10 类，需删除后按 12 类重建（NOT VALID 免全表校验，
    # 对新插入/更新行仍生效）。兼容两种约束名：naming_convention 渲染的
    # ck_canvas_nodes_ck_cn_node_type（与 ORM/初始迁移一致，Task 3 起统一用该名重建）
    # 与历史上直接命名的 ck_cn_node_type；DO 块幂等，兼容"create_all 已按新模型
    # 建约束"的库。
    ("canvas_nodes", "ALTER TABLE canvas_nodes DROP CONSTRAINT IF EXISTS ck_canvas_nodes_ck_cn_node_type"),
    ("canvas_nodes", "ALTER TABLE canvas_nodes DROP CONSTRAINT IF EXISTS ck_cn_node_type"),
    ("canvas_nodes", """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'canvas_nodes'::regclass
                  AND conname IN ('ck_cn_node_type', 'ck_canvas_nodes_ck_cn_node_type')
            ) THEN
                ALTER TABLE canvas_nodes ADD CONSTRAINT ck_canvas_nodes_ck_cn_node_type
                CHECK (node_type IN ('text', 'image', 'video', 'audio', 'generate',
                       'asset_ref', 'shot', 'compose', 'logic', 'group', 'plugin', 'mcp_tool'))
                NOT VALID;
            END IF;
        END $$;
    """),
    # Task 10（V-01）：操作日志风险等级放开 medium——版本回退等可撤销但
    # 影响面较大的操作使用 medium（RD-56）。存量库旧 CHECK 仅含 normal/high，
    # 需删除后按三值重建。兼容两种约束名：naming_convention 渲染的
    # ck_operation_logs_ck_oplog_risk_level（与 ORM/初始迁移一致）与历史上
    # 直接命名的 ck_oplog_risk_level；DO 块幂等，兼容「create_all 已按新模型
    # 建约束」的库。
    ("operation_logs",
     "ALTER TABLE operation_logs DROP CONSTRAINT IF EXISTS ck_operation_logs_ck_oplog_risk_level"),
    ("operation_logs",
     "ALTER TABLE operation_logs DROP CONSTRAINT IF EXISTS ck_oplog_risk_level"),
    ("operation_logs", """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'operation_logs'::regclass
                  AND conname IN ('ck_oplog_risk_level', 'ck_operation_logs_ck_oplog_risk_level')
            ) THEN
                ALTER TABLE operation_logs ADD CONSTRAINT ck_operation_logs_ck_oplog_risk_level
                CHECK (risk_level IN ('normal', 'medium', 'high'));
            END IF;
        END $$;
    """),
]


async def _add_missing_columns() -> None:
    """对存量库补加「模型新增列」（create_all 只建表不加列，幂等补列）。

    使用 ALTER TABLE ... ADD COLUMN IF NOT EXISTS（PostgreSQL 原生幂等），
    可重复执行。当前补列清单：
    - agent_threads.rewind_checkpoint：MA-06 Time Travel 回溯检查点标记。
    - tasks.priority：任务调度优先级（Task 2.1，越大越先执行）。
    - tasks.next_retry_at：下次重试时间（Task 2.1 指数退避调度）。
    - tasks.dead_letter：死信标记（Task 2.1 重试耗尽进入管理端死信队列）。
    - review_records.method：机审方式 keyword/llm-text/gate（Task 4.1）。
    - users.account_status：分级处罚账号状态 active/restricted/banned（Task 4.3，C-04）。
    - users.violation_count：内容违规累计次数（Task 4.3，C-04）。

    Returns:
        None。

    Raises:
        Exception: 数据库不可达/权限不足时向上抛出。
    """
    from app.core.database import engine

    async with engine.begin() as conn:
        for table, ddl in MISSING_COLUMN_DDL_STATEMENTS:
            await conn.execute(text(ddl))
            logger.info("存量表补列完成检查：%s", table)


if __name__ == "__main__":
    asyncio.run(init_database())
