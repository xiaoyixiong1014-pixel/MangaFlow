"""面部视觉一致性服务模块（文件级说明，P0-4）。

基于 Agnes 视觉模型的「面部档案」路线实现跨图角色一致性比对：
- build_face_profile()：定妆照生成后提取结构化面部档案 JSON，写
  characters.face_profile 并把档案文本向量化入库（asset_embeddings.kind='face'）；
- compare_face_profile()：对新图提档案 → 档案文本向量化 → 与库中主形象
  档案向量算余弦相似度（无主档案返回 None，首次生成直接建档放行）；
- profile_to_text()：档案 JSON 按固定键序拼接为比对文本（同特征产出同文本，
  减少向量化抖动）。

技术路线取舍（相对 InsightFace 等专业人脸识别）：零新增外部依赖、动漫图
适配更稳、档案人可读（可解释"差异在发色"）；比对的语义噪声通过影子模式
回测校准阈值（config.face_consistency_threshold，默认 0.70）。

容错约定：与 upsert_asset_embedding 一致，全程吞异常仅告警——视觉调用/
向量化的任何失败都不阻塞出图主链路，最坏退化为"无一致性校验"。
"""

import logging
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AssetEmbedding, Character
from app.providers.embedding import cosine_similarity, embed_text
from app.providers import vision_review
from app.services.asset_service import upsert_asset_embedding

logger = logging.getLogger("mangaflow.face_profile")

# 档案固定键序（profile_to_text 拼接顺序；与 vision_review 提示词键一致）
_PROFILE_KEYS = (
    "gender", "age_look", "face_shape", "hair_style", "hair_color",
    "eye_color", "eyebrow", "nose", "lips", "skin_tone",
)
# 数组型档案键（逗号拼接参与文本）
_PROFILE_LIST_KEYS = ("distinctive_marks", "accessories")


def profile_to_text(profile: Optional[Dict[str, Any]]) -> str:
    """把面部档案 JSON 规整为比对用描述文本（固定键序拼接）。

    固定键序保证"同特征 → 同文本 → 同向量"，避免键序抖动造成的余弦噪声；
    数组键按字典序排序后逗号拼接；空值键跳过。

    Args:
        profile: 面部档案 JSON（固定键，可空）。

    Returns:
        str: 规整后的描述文本（全空返回空串）。

    Raises:
        无：纯字符串处理。
    """
    if not isinstance(profile, dict):
        return ""
    parts = []
    for key in _PROFILE_KEYS:
        val = str(profile.get(key) or "").strip()
        if val:
            parts.append(f"{key}:{val}")
    for key in _PROFILE_LIST_KEYS:
        items = profile.get(key)
        if isinstance(items, list) and items:
            joined = "、".join(sorted(str(i).strip() for i in items if str(i).strip()))
            if joined:
                parts.append(f"{key}:{joined}")
    return " ".join(parts)


async def build_face_profile(db: AsyncSession, character: Character, image_url: str) -> Optional[dict]:
    """提取并落库角色面部档案（P0-4：定妆照首次生成后调用）。

    调 Agnes 视觉模型提档案 → 写 characters.face_profile + 档案文本向量化
    入库（asset_embeddings.kind='face'）。全程吞异常仅告警（失败不阻塞出图
    主链路，与 upsert_asset_embedding 同策略）。

    Args:
        db: 异步数据库会话（调用方主数据回写已提交后调用）。
        character: 角色 ORM 对象。
        image_url: 定妆照 URL（本地存储 URL / 公网 URL）。

    Returns:
        Optional[dict]: 成功返回档案 JSON；失败返回 None（已内部告警）。

    Raises:
        无：任何异常在函数内捕获并记录告警。
    """
    try:
        res = await vision_review.review_face_profile(image_url=image_url)
        profile = res.get("profile")
        if not isinstance(profile, dict) or not profile:
            logger.warning("面部档案提取结果为空（character_id=%s）", character.id)
            return None
        character.face_profile = profile
        await db.commit()
        text = profile_to_text(profile)
        if text:
            await upsert_asset_embedding(db, "character", character.id, text, kind="face")
        return profile
    except Exception as exc:  # noqa: BLE001 —— 档案提取失败不阻塞出图主链路
        logger.warning(
            "面部档案提取失败（character_id=%s）：%s", character.id, exc
        )
        await db.rollback()
        return None


async def compare_face_profile(
    db: AsyncSession, character: Character, image_url: str
) -> Optional[float]:
    """比对新图面部档案与主形象档案（P0-4 一致性闸门）。

    新图提档案 → 档案文本向量化 → 与库中 kind='face' 主档案向量余弦。
    库中无主档案（首次生成尚未建档）返回 None，调用方应先建档并放行本次结果。

    Args:
        db: 异步数据库会话。
        character: 角色 ORM 对象。
        image_url: 新生成图像 URL。

    Returns:
        Optional[float]: 余弦相似度（0~1，越大越像）；主档案缺失或比对失败
            返回 None（调用方按"跳过校验"处理）。

    Raises:
        无：任何异常在函数内捕获并记录告警。
    """
    try:
        master = (
            await db.execute(
                select(AssetEmbedding).where(
                    AssetEmbedding.asset_type == "character",
                    AssetEmbedding.asset_id == character.id,
                    AssetEmbedding.kind == "face",
                )
            )
        ).scalar_one_or_none()
        if master is None or not isinstance(master.embedding, dict):
            return None
        master_vec = (master.embedding or {}).get("vector")
        if not master_vec:
            return None
        res = await vision_review.review_face_profile(image_url=image_url)
        text = profile_to_text(res.get("profile"))
        if not text:
            return None
        vec = await embed_text(text)
        return round(max(0.0, min(1.0, cosine_similarity(master_vec, vec))), 4)
    except Exception as exc:  # noqa: BLE001 —— 比对失败按跳过校验处理
        logger.warning(
            "面部一致性比对失败（character_id=%s）：%s", character.id, exc
        )
        return None
