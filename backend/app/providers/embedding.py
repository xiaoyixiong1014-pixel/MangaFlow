"""文本向量化模块（文件级说明）。

提供 embed_text()：优先调用 Agnes 渠道 OpenAI 兼容 /embeddings 接口获取
真实向量；接口不可用时静默降级为本地 hash_bag_vector()（文本 2-gram 逐个
MD5 哈希取模落桶 + L2 归一化），保证任何情况下都能产出可比对向量。

另提供 cosine_similarity() 向量相似度计算（维度不一致时截断对齐）。

存储方案说明：向量当前以 JSONB 列（字段名 embedding）承载（本机未装
pgvector），相似度在 Python 侧计算；升级 pgvector 时仅需替换本模块的
存储与计算层，调用方无需改动。
"""

import hashlib
import logging
import math
from typing import List, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger("mangaflow.providers.embedding")

# 远程 embedding 请求超时（秒）
_EMBED_TIMEOUT = 15.0


async def _remote_embed(text: str) -> Optional[List[float]]:
    """调用远程 /embeddings 接口获取向量（内部函数）。

    Args:
        text: 待向量化文本。

    Returns:
        Optional[List[float]]: 归一化前的原始向量；任何失败返回 None（静默降级）。

    Raises:
        无：内部捕获全部异常。
    """
    try:
        async with httpx.AsyncClient(timeout=_EMBED_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.agnes_llm_base_url.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {settings.agnes_llm_api_key}"},
                json={"model": "text-embedding-v4", "input": text},
            )
        if resp.status_code != 200:
            logger.warning("远程 embedding 失败（HTTP %s），降级本地哈希向量", resp.status_code)
            return None
        data = (resp.json() or {}).get("data") or []
        if not data or not data[0].get("embedding"):
            return None
        embedding = data[0]["embedding"]
        return [float(x) for x in embedding]
    except Exception as exc:  # noqa: BLE001 —— 静默降级，不影响业务
        logger.warning("远程 embedding 异常：%s，降级本地哈希向量", exc)
        return None


def hash_bag_vector(text: str, dim: int = 256) -> List[float]:
    """构造文本的本地哈希袋向量（hash-bag-of-grams，L2 归一化）。

    对文本的全部相邻 2-gram（文本过短时回退单字）逐个计算 MD5 摘要，
    以摘要整数取模落入 dim 维桶中累加权重，最后做 L2 归一化。
    结果为确定性的稠密向量：相同文本向量相同，相似文本部分碰撞。

    Args:
        text: 待向量化文本。
        dim: 向量维度（默认 256，与落库 JSONB 存储的规模约定一致）。

    Returns:
        List[float]: L2 归一化后的 dim 维向量。

    Raises:
        无。
    """
    vec = [0.0] * dim
    cleaned = (text or "").strip()
    grams = [cleaned[i:i + 2] for i in range(len(cleaned) - 1)] or ([cleaned] if cleaned else [])
    for gram in grams:
        digest = hashlib.md5(gram.encode("utf-8")).hexdigest()
        idx = int(digest, 16) % dim
        vec[idx] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


async def embed_text(text: str, dim: int = 256) -> List[float]:
    """获取文本向量：优先远程 embedding，失败静默降级本地哈希向量。

    Args:
        text: 待向量化文本。
        dim: 降级向量的维度（远程向量维度由模型决定，不做截断）。

    Returns:
        List[float]: 文本向量（远程为模型维度，本地为 dim 维）。

    Raises:
        无：任何失败均降级为本地哈希向量，不向上抛出。
    """
    remote = await _remote_embed(text)
    if remote:
        return remote
    return hash_bag_vector(text, dim)


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """计算两个向量的余弦相似度。

    维度不一致时按较短长度截断对齐（兼容远程向量与本地降级向量混存）。

    Args:
        a: 向量 A。
        b: 向量 B。

    Returns:
        float: 余弦相似度，取值 [-1, 1]；任一向量模长为 0 时返回 0.0。

    Raises:
        无。
    """
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))  # 维度对齐：截断到较短长度
    dot = sum(a[i] * b[i] for i in range(n))
    norm_a = math.sqrt(sum(x * x for x in a[:n]))
    norm_b = math.sqrt(sum(x * x for x in b[:n]))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
