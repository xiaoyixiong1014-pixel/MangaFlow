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
        aes_key: 模型渠道 API Key 的 AES 加密密钥（须为 16/24/32 字节字符串，本默认值为 32 字符）。
        storage_dir: 本地文件存储根目录；相对路径时按项目根目录拼接为绝对路径。
        cors_origins: 允许跨域的来源列表，"*" 表示不限制。
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
    deepseek_api_key: str = "sk-461126bcb5944b62a6a6a15137df42eb"
    deepseek_model: str = "deepseek-flash"

    # ---------- 模型渠道 Key 加密 ----------
    aes_key: str = "mangaflow-aes-key-1234567890abcd"

    # ---------- 本地存储 ----------
    storage_dir: str = "backend/storage"

    # ---------- 跨域 ----------
    cors_origins: List[str] = ["*"]

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
