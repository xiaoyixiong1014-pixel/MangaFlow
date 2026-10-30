"""应用配置模块（文件级说明）。

基于 pydantic-settings 的集中式配置：所有字段均提供开发环境默认值，
生产部署时可通过环境变量或 backend/.env 文件覆盖（环境变量名不区分大小写）。
"""

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend 目录（本文件位于 backend/app/core/config.py，向上三级）
BASE_DIR = Path(__file__).resolve().parent.parent.parent
# 项目根目录（backend 的上一级），相对路径 storage_dir 以此为基准拼接
PROJECT_ROOT = BASE_DIR.parent


class Settings(BaseSettings):
    """全局配置类。

    Attributes:
        database_url: 业务库连接串（PostgreSQL 异步驱动 asyncpg）。
        admin_database_url: 管理库连接串（连接 postgres 系统库，仅用于自动创建 mangaflow 业务库）。
        jwt_secret_key: JWT 签名密钥，生产环境务必更换。
        jwt_algorithm: JWT 签名算法。
        access_token_expire_minutes: 访问令牌 access token 有效期（分钟）。
        refresh_token_expire_days: 刷新令牌 refresh token 有效期（天）。
        deepseek_base_url: DeepSeek 兼容 OpenAI 协议的 API 地址。
        deepseek_api_key: DeepSeek API Key。
        deepseek_model: 默认文本大模型名称。
        agnes_llm_base_url: Agnes 文本 LLM（兜底渠道）兼容 OpenAI 协议的 API 地址。
        agnes_llm_api_key: Agnes 文本 LLM API Key。
        agnes_llm_model: Agnes 文本 LLM 默认模型名。
        agnes_image_base_url: Agnes 图像生成兼容 OpenAI 协议的 API 地址。
        agnes_image_api_key: Agnes 图像生成 API Key。
        agnes_image_model: Agnes 图像生成默认模型名。
        agnes_video_base_url: Agnes 视频生成兼容 OpenAI 协议的 API 地址。
        agnes_video_api_key: Agnes 视频生成 API Key。
        agnes_video_model: Agnes 视频生成默认模型名。
        agnes_video_mode: Agnes 视频生成模式（官方枚举 text/keyframe/reference，
            text 即文生视频；首帧图生视频由 provider 按传入的 first_frame_url
            自动切换 keyframe 模式，2026-10-09 依官方文档与实测确定）。
        agnes_vision_model: 视觉机审模型名（图像/视频产物真实视觉机审，OpenAI
            兼容图文混合协议；探测详见 .run/probe_result.md）。
        bailian_tts_base_url: 阿里云百炼 TTS 兼容 OpenAI 协议的 API 地址。
        bailian_tts_api_key: 阿里云百炼 TTS API Key。
        bailian_tts_model: 阿里云百炼 TTS 默认模型名。
        video_poll_interval_seconds: 异步视频生成任务轮询间隔（秒）。
        video_poll_timeout_seconds: 异步视频生成任务轮询总超时（秒）。
        aes_key: 模型渠道 API Key 的 AES 加密密钥（须为 16/24/32 字节字符串，本默认值为 32 字符）。
        storage_dir: 本地文件存储根目录；相对路径时按项目根目录拼接为绝对路径。
        cors_origins: 允许跨域的来源列表，"*" 表示不限制。
        alembic_on_startup: 启动时是否先执行 Alembic 迁移（alembic upgrade head）再走
            create_all 兜底初始化。默认 False 保持现状（仅 create_all + 幂等补列）；
            生产部署建议置 True 或改由部署流水线在启动前显式执行迁移。
            迁移失败不阻断启动（记 error 日志后降级为 create_all 兜底）。
        langsmith_tracing: 是否启用 LangSmith 链路追踪（MA-13）。环境变量
            LANGSMITH_TRACING 可覆盖（"true"/"1" 等真值）；默认 False 时
            setup_tracing() 零开销直接跳过。
        langsmith_api_key: LangSmith API Key（环境变量 LANGSMITH_API_KEY 可覆盖）；
            未配置时仅注入追踪开关，上报会被 LangSmith SDK 拒绝（无副作用）。
        langsmith_project: LangSmith 项目名（环境变量 LANGSMITH_PROJECT 可覆盖），
            trace 上报的目标项目，默认 "mangaflow"。
        plugin_allowed_hosts: 画布插件 endpoint_url 信任域白名单（CV-22 轻量加固）。
            非空时插件 HTTP 主机必须命中白名单（精确或子域名匹配，大小写不敏感）；
            空列表 = 不限公网域名，仅强制内网保留地址黑名单（SSRF 防护）；
            "builtin:" 前缀进程内插件不受限。
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---------- 数据库 ----------
    database_url: str = "postgresql+asyncpg://postgres:123456@localhost:5432/mangaflow"
    admin_database_url: str = "postgresql+asyncpg://postgres:123456@localhost:5432/postgres"

    # ---------- 认证 / JWT ----------
    jwt_secret_key: str = "mangaflow-dev-jwt-secret-change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 10080  # 7 天
    refresh_token_expire_days: int = 30

    # ---------- LLM（DeepSeek） ----------
    deepseek_base_url: str = "https://api.deepseek.com"
    # 安全基线（D1）：密钥一律不落代码，默认空串，从环境变量/DEEPSEEK_API_KEY 注入
    deepseek_api_key: str = ""
    # 模型名实测（2026-10-10）：deepseek-flash 网关接受但返回空 content（51 tokens 无输出），
    # deepseek-chat 正常回复——现役模型名为 deepseek-chat
    deepseek_model: str = "deepseek-chat"

    # ---------- Agnes 文本 LLM（兜底） ----------
    agnes_llm_base_url: str = "https://apihub.agnes-ai.com/v1"
    # 安全基线（D1）：密钥一律不落代码，默认空串，从环境变量/AGNES_LLM_API_KEY 注入
    agnes_llm_api_key: str = ""
    agnes_llm_model: str = "agnes-3.0-flash"

    # ---------- Agnes 图像 ----------
    agnes_image_base_url: str = "https://apihub.agnes-ai.com/v1"
    # 安全基线（D1）：密钥一律不落代码，默认空串，从环境变量/AGNES_IMAGE_API_KEY 注入
    agnes_image_api_key: str = ""
    agnes_image_model: str = "agnes-image-2.5-flash"

    # ---------- Agnes 视频 ----------
    agnes_video_base_url: str = "https://apihub.agnes-ai.com/v1"
    # 安全基线（D1）：密钥一律不落代码，默认空串，从环境变量/AGNES_VIDEO_API_KEY 注入
    agnes_video_api_key: str = ""
    agnes_video_model: str = "agnes-video-2.5-flash"
    # 视频生成模式：官方枚举为 text / keyframe / reference（经探测与官方文档确定，
    # 文生视频传 text；带首帧图时 provider 自动改用 keyframe）
    agnes_video_mode: str = "text"

    # ---------- Agnes 视觉机审（图像/视频产物真实视觉机审，Task 7 / R-08、C-01） ----------
    # 视觉理解模型名（OpenAI 兼容 chat/completions 图文混合协议）。
    # 经探测（2026-10-10，详见 .run/probe_result.md「视觉机审模型探测」一节）：
    # 网关 /models 共 11 个模型均无显式视觉命名（无 vision/vl 关键词），但对文本
    # 旗舰 agnes-2.5-pro-alpha / agnes-3.0-flash-max / agnes-2.5-pro 实测图文小样
    # 调用均 HTTP 200 并返回准确图片描述（usage.prompt_tokens_details.image_tokens
    # =943 证明图片真实入模），即 Agnes 旗舰文本模型即多模态视觉模型。默认取
    # 3.0 代旗舰；视觉调用失败时 runner 自动降级 llm-text → keyword 文本机审。
    # 2026-10-20：agnes-3.0-flash-max 已从平台下线（default 分组 503
    # model_not_found），实测换代为 agnes-3.0-flash（图文探测 200 且正确
    # 识别测试图颜色）。
    agnes_vision_model: str = "agnes-3.0-flash"

    # ---------- P0-4 面部视觉一致性（Agnes 面部档案 + 余弦比对闸门） ----------
    # 一致性阈值：新图面部档案与主形象档案余弦低于该值判定偏差过大。
    # 初始 0.70 为保守起步（跨图档案描述比对语义噪声大于同源自比对），
    # 影子模式积累 task.meta.face_score 样本后经回测校准再收紧。
    face_consistency_threshold: float = 0.70
    # 强拦截开关：False=影子模式（仅记录 score 不拦截，默认）；
    # True=低于阈值自动重抽一次，仍低则任务 failed（error_code=consistency_failed）。
    # 建议累计 50+ 真实回测样本后再开启。
    face_consistency_enforce: bool = False

    # ---------- 阿里云百炼 TTS ----------
    bailian_tts_base_url: str = "https://ws-lhelc7dy7rch25ia.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    # 安全基线（D1）：密钥一律不落代码，默认空串，从环境变量/BAILIAN_TTS_API_KEY 注入
    bailian_tts_api_key: str = ""
    # 默认模型名：qwen-audio-3.1-tts-flash 仅提供 WebSocket 端点
    # （dashscope.audio.tts_v2.SpeechSynthesizer），不出现在 OpenAI 兼容
    # GET /models 列表中（2026-10-12 实测合成成功，接入详见 tts_bailian.py）
    bailian_tts_model: str = "qwen-audio-3.1-tts-flash"

    # ---------- 阿里云百炼视频（wan2.7-i2v 图生视频，DashScope 原生异步协议） ----------
    # 根域名：视频任务创建/轮询走原生路径 /api/v1/...（非 OpenAI 兼容 /compatible-mode）；
    # API Key 与 TTS 渠道共用（bailian_tts_api_key）
    bailian_wan_base_url: str = "https://ws-lhelc7dy7rch25ia.cn-beijing.maas.aliyuncs.com"
    bailian_wan_model: str = "wan2.7-i2v"

    # ---------- 视频轮询配置 ----------
    video_poll_interval_seconds: int = 5
    video_poll_timeout_seconds: int = 300

    # ---------- 站点公网地址（MiniMax v2 图生视频依赖，2026-10-30） ----------
    # 站点对外访问地址（如 http://82.156.216.134:8006）：MiniMax v2 契约的
    # image_url 仅接受公网 URL（data URI 触发 2013 Mismatch type of schema），
    # 本地首帧写盘后经 {public_base_url}/storage/generated/xxx 暴露为公网直链；
    # 留空时 v2 图生视频自动退化为文生视频（不影响 v1 data URI 路径）
    public_base_url: str = ""

    # ---------- 口型同步（Wav2Lip 本地推理，2026-10-20） ----------
    # 配音生成后对分镜视频做口型对齐：True=该镜有配音时自动口型同步
    # （失败自动降级为不做口型同步，不阻断任务）；模型权重位于
    # storage/models/lipsync/（wav2lip_gan.pth + s3fd.pth）。
    # 许可提示：Wav2Lip 为 CC BY-NC 4.0（非商业），商业化需替换为可商用方案
    lipsync_enabled: bool = True

    # ---------- 模型渠道 Key 加密 ----------
    aes_key: str = "mangaflow-aes-key-1234567890abcd"

    # ---------- 本地存储 ----------
    storage_dir: str = "backend/storage"

    # ---------- 跨域 ----------
    cors_origins: List[str] = ["*"]

    # ---------- 画布插件（CV-22 轻量加固，2026-10-10） ----------
    # 插件 endpoint_url 信任域白名单（运营方可配）：非空时插件 HTTP 主机必须
    # 命中白名单（精确或子域名匹配，大小写不敏感）；空列表 = 不限公网域名，
    # 仅强制内网保留地址黑名单（localhost / 127.0.0.0/8 / 10/8 / 172.16-31 /
    # 192.168/16 / 169.254/16 / ::1 等，SSRF 防护）。"builtin:" 前缀进程内
    # 插件不受限；安装（POST）与 invoke 前均校验，违规 400。
    plugin_allowed_hosts: List[str] = []

    # ---------- 业务开关 ----------
    # 违规分级处罚准入校验（C-04：banned 403 拒绝 / restricted 当日 10 次限流 429）。
    # 产品上线前默认关闭（False）：处罚数据照常累计与通知，但生成接口不拦截；
    # 产品成功上线后于 backend/.env 或环境变量设 PUNISHMENT_ENABLED=true 即可重新启用。
    punishment_enabled: bool = False

    # 生成渠道全失败时的本地 mock 占位兜底（演示模式）。
    # False（默认）：真实渠道全部失败时任务落 failed（平台退款，可在任务面板重试），
    # 不再产出「演示占位」图污染分镜；仅当降级链完全为空（未配置任何渠道）时
    # 仍追加 mock 哨兵，保证全新部署开箱可用。
    # True：恢复旧行为——链尾恒追加 mock 哨兵（开发联调/纯演示部署使用，
    # 测试套件经 conftest autouse fixture 强制开启）。
    mock_fallback_enabled: bool = False

    # ---------- 口型同步（LatentSync，预留集成位） ----------
    # 用户自部署的 LatentSync 口型对齐推理服务（视频 + 音频 → 口型同步视频）。
    # 服务就绪后配置 LIP_SYNC_BASE_URL（如 http://127.0.0.1:8000）并置
    # LIP_SYNC_ENABLED=true；调用契约（端点/请求体/轮询方式）待服务部署后
    # 按实际 API 对齐，当前仅占位配置，不参与生成流程。
    lip_sync_enabled: bool = False
    lip_sync_base_url: str = ""

    # 启动钩子开关：lifespan 在 create_all 之前可选执行 Alembic 迁移（alembic upgrade
    # head，程序化 API）。默认 False 保持现状（仅 create_all + 幂等补列），生产部署
    # 可设 ALEMBIC_ON_STARTUP=true 或由部署流水线在启动前显式执行迁移
    alembic_on_startup: bool = False

    # ---------- LangSmith 追踪（MA-13） ----------
    # 是否启用 LangSmith 链路追踪：默认 False（不配置零开销）；生产环境在
    # backend/.env 或环境变量设 LANGSMITH_TRACING=true 并配 LANGSMITH_API_KEY 即启用
    langsmith_tracing: bool = False
    # LangSmith API Key（lsv2_ 前缀），启用追踪时上报鉴权用；当前无真实 Key，
    # 链路已就绪待密钥启用
    langsmith_api_key: str = ""
    # trace 上报的目标 LangSmith 项目名
    langsmith_project: str = "mangaflow"

    def storage_path(self) -> Path:
        """获取存储根目录的绝对路径。

        Returns:
            Path: storage_dir 对应的绝对路径；相对路径基于项目根目录拼接。
        """
        path = Path(self.storage_dir)
        return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例。

    Returns:
        Settings: 进程内唯一的配置对象（lru_cache 缓存）。
    """
    return Settings()


# 模块级配置实例，供各模块直接导入使用
settings = get_settings()
