"""对称加密工具模块（文件级说明）。

基于 cryptography 库的 Fernet 对称加密（AES-128-CBC + HMAC-SHA256），用于
模型渠道 API Key 等敏感凭据的落库加密：

- 密钥派生：对配置项 settings.aes_key 做 SHA-256 摘要后进行
  base64.urlsafe_b64encode，得到 Fernet 要求的 32 字节 url-safe 密钥。
- encrypt_text()：明文 → 密文字符串（可直接存入 api_key_encrypted 字段）。
- decrypt_text()：密文 → 明文；解密失败（密文损坏 / Key 轮换 / 内容本为明文）
  统一抛 ValueError，由调用方决定降级策略（如按明文处理）。
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

# 由配置密钥派生的 Fernet 实例（进程内单例，模块导入时构建一次）
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.aes_key.encode("utf-8")).digest()))


def encrypt_text(plain: str) -> str:
    """加密明文字符串。

    Args:
        plain: 待加密的明文（如模型渠道 API Key）。

    Returns:
        str: Fernet 密文字符串（url-safe base64，可安全落库/传输）。

    Raises:
        TypeError: 传入非字符串时抛出。
    """
    if not isinstance(plain, str):
        raise TypeError("encrypt_text 仅接受字符串输入")
    return _fernet.encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_text(cipher: str) -> str:
    """解密密文字符串。

    Args:
        cipher: encrypt_text 产生的密文字符串。

    Returns:
        str: 解密后的明文。

    Raises:
        ValueError: 密文格式非法、校验失败或与当前 aes_key 不匹配时抛出，
            调用方应捕获并按明文降级处理（兼容历史明文存储的数据）。
        TypeError: 传入非字符串时抛出。
    """
    if not isinstance(cipher, str):
        raise TypeError("decrypt_text 仅接受字符串输入")
    try:
        return _fernet.decrypt(cipher.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        # InvalidToken：token 损坏/密钥不匹配/非 Fernet 格式；统一转 ValueError
        raise ValueError("解密失败：密文非法或与当前密钥不匹配") from exc
