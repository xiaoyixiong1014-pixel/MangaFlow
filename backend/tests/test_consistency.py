"""语义检索与一致性真实化测试（Task 6，文件级说明）。

覆盖链路：
- hash-bag 向量：相同文本相似度≈1 且高于不同文本（远程 embedding 失败时
  embed_text 自动降级 hash-bag，测试环境无网也能跑）
- 资产向量写入（Task 6.1）：创建角色后 asset_embeddings 落行
- 一致性校验真实化（Task 6.2）：details 分项、综合评分公式一致、
  无伪公式（锁定→1.0 通过；未锁定 self-check→0.8 不通过）、变体继承语义
- 资产语义搜索（Task 6.3）：/api/assets/search 命中创建的角色、type 过滤、
  用户数据隔离
- 记忆语义检索（Task 6.4 / MA-07）：upsert_memory 写入后 /api/memories/search
  命中，且其他用户不可见

测试环境无远程 embedding 服务时全部走本地 hash-bag 向量，结果确定性可断言。
"""

import pytest
from sqlalchemy import select

from app.models import AssetEmbedding
from app.providers.embedding import cosine_similarity, embed_text, hash_bag_vector
from app.services.memory_service import upsert_memory
from tests.conftest import create_project, register_user, unique_suffix


async def _make_project_with_character(client, auth, tag: str, name: str = "林小满",
                                       description: str = "元气少女漫画家"):
    """辅助：创建项目与角色资产，返回 (project_id, character_id)（内部函数）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        tag: 标题防冲突后缀。
        name: 角色名。
        description: 角色描述。

    Returns:
        Tuple[int, int]: (项目 ID, 角色 ID)。

    Raises:
        AssertionError: 接口返回非 200 时中断。
    """
    project = await create_project(client, auth["headers"], title=f"一致性项目_{tag}")
    resp = await client.post(
        f"/api/projects/{project['id']}/assets/characters",
        headers=auth["headers"],
        json={"name": name, "description": description},
    )
    assert resp.status_code == 200, resp.text
    return project["id"], resp.json()["id"]


async def test_hashbag_xiangtong_wenben_similar():
    """hash-bag 向量：相同文本余弦相似度≈1，且严格高于不同文本。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    text_a = "元气少女漫画家，扎着双马尾，笑容明亮"
    text_b = "阴沉的中年侦探，独来独往，常年穿风衣"
    same = cosine_similarity(hash_bag_vector(text_a), hash_bag_vector(text_a))
    diff = cosine_similarity(hash_bag_vector(text_a), hash_bag_vector(text_b))
    assert same == pytest.approx(1.0, abs=1e-9)
    assert same > diff
    # embed_text 异步链路可用（远程失败自动降级 hash-bag，产出可比对向量）
    vec = await embed_text(text_a)
    assert len(vec) > 0
    assert cosine_similarity(vec, hash_bag_vector(text_a)) > diff  # 自身 ≈1 高于跨文本


async def test_zichan_xiangliang_xieru(client, auth, db):
    """资产向量写入接线：创建角色后 asset_embeddings 出现对应行。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库异步会话（直查 asset_embeddings 断言）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, cid = await _make_project_with_character(client, auth, unique_suffix())
    row = (
        await db.execute(
            select(AssetEmbedding).where(
                AssetEmbedding.asset_type == "character",
                AssetEmbedding.asset_id == cid,
            )
        )
    ).scalar_one_or_none()
    assert row is not None, "创建角色后 asset_embeddings 应落行"
    payload = row.embedding
    assert isinstance(payload, dict) and isinstance(payload.get("vector"), list)
    assert payload.get("text_preview")  # 文本预览前 200 字已存储
    assert row.model in ("remote", "hash-bag")


async def test_consistency_check_details_wuweigongshi(client, auth):
    """一致性校验真实化：details 分项、评分公式一致、无伪公式行为。

    未锁定 self-check：visual=1.0、desc=1.0、locked=0 → score=0.8 → 不通过；
    锁定后：score=1.0 → 通过（旧伪公式恒 [0.88, 0.99] 的行为被移除）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, cid = await _make_project_with_character(client, auth, unique_suffix())
    url = f"/api/projects/{pid}/assets/consistency-check"

    # ---------- 未锁定 self-check ----------
    resp = await client.post(url, headers=auth["headers"], json={"character_id": cid})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["algorithm"] == "embedding-cosine-v1"
    assert body["threshold"] == 0.85
    assert body["details"]["visual_prompt_sim"] == pytest.approx(1.0, abs=1e-3)
    assert body["details"]["description_sim"] == pytest.approx(1.0, abs=1e-3)
    assert body["details"]["locked_bonus"] == 0.0
    # 综合公式一致性：score == round(0.5*visual + 0.3*desc + 0.2*locked, 4)
    d = body["details"]
    assert body["score"] == pytest.approx(
        round(0.5 * d["visual_prompt_sim"] + 0.3 * d["description_sim"]
              + 0.2 * d["locked_bonus"], 4), abs=1e-3)
    assert body["score"] == body["similarity"]
    assert body["passed"] == (body["score"] >= body["threshold"])
    assert "一致性" in body["report"]
    # 无伪公式：未锁定 self-check 必为 0.8（伪公式区间恒 [0.88, 0.99]）
    assert body["score"] == pytest.approx(0.8, abs=1e-3)
    assert body["passed"] is False

    # ---------- 锁定后 self-check ----------
    lock = await client.post(
        f"/api/projects/{pid}/assets/characters/{cid}/lock", headers=auth["headers"])
    assert lock.status_code == 200, lock.text
    resp2 = await client.post(url, headers=auth["headers"], json={"character_id": cid})
    body2 = resp2.json()
    assert body2["details"]["locked_bonus"] == 1.0
    assert body2["score"] == pytest.approx(1.0, abs=1e-3)
    assert body2["passed"] is True


async def test_consistency_check_variant_jicheng(client, auth):
    """一致性校验：变体未填 visual_prompt 时按继承主形象语义计（visual sim=1.0）。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    pid, cid = await _make_project_with_character(client, auth, unique_suffix())
    variant = await client.post(
        f"/api/projects/{pid}/assets/characters/{cid}/variants",
        headers=auth["headers"],
        json={"variant_type": "outfit", "name": "战斗装",
              "inherit_mask": {"face": True, "outfit": False}},
    )
    assert variant.status_code == 200, variant.text
    vid = variant.json()["id"]
    resp = await client.post(
        f"/api/projects/{pid}/assets/consistency-check",
        headers=auth["headers"],
        json={"character_id": cid, "variant_id": vid},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["algorithm"] == "embedding-cosine-v1"
    assert body["details"]["target"] == f"变体 {vid}"
    # 继承语义：变体未填 visual_prompt → visual 分项与主形象完全一致
    assert body["details"]["visual_prompt_sim"] == pytest.approx(1.0, abs=1e-3)
    assert body["passed"] == (body["score"] >= body["threshold"])


async def test_assets_search_jiansuo(client, auth):
    """资产语义搜索：命中创建的角色、type 过滤生效、用户数据隔离。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    tag = unique_suffix()
    pid, cid = await _make_project_with_character(
        client, auth, tag, name=f"林小满{tag}", description="元气少女漫画家，赛博朋克都市")

    # ---------- 基本命中 ----------
    resp = await client.get(
        "/api/assets/search", headers=auth["headers"],
        params={"q": "赛博朋克都市 少女漫画家"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1
    hit = next((i for i in body["items"]
                if i["asset_type"] == "character" and i["asset_id"] == cid), None)
    assert hit is not None, f"语义搜索应命中刚创建的角色：{body}"
    assert hit["name"] == f"林小满{tag}"
    assert hit["score"] > 0
    assert hit["preview"]

    # ---------- type 过滤：scene 类型下无命中 ----------
    resp_scene = await client.get(
        "/api/assets/search", headers=auth["headers"],
        params={"q": "赛博朋克", "type": "scene"})
    assert resp_scene.status_code == 200
    assert resp_scene.json()["total"] == 0

    # ---------- 非法 type 返回 400 ----------
    resp_bad = await client.get(
        "/api/assets/search", headers=auth["headers"],
        params={"q": "任意", "type": "invalid"})
    assert resp_bad.status_code == 400

    # ---------- 用户隔离：其他用户搜索不到该角色 ----------
    other = await register_user(client, prefix="other")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    resp_other = await client.get(
        "/api/assets/search", headers=other_headers,
        params={"q": "赛博朋克都市"})
    assert resp_other.status_code == 200
    items = resp_other.json()["items"]
    assert all(i["asset_id"] != cid for i in items)

    # ---------- 未认证 401 ----------
    resp_anon = await client.get("/api/assets/search", params={"q": "测试"})
    assert resp_anon.status_code == 401


async def test_memories_search_jiansuo(client, auth, db):
    """记忆语义检索（MA-07）：upsert_memory 后检索命中，且对其他用户不可见。

    Args:
        client: 内存 HTTP 客户端。
        auth: 随机注册用户上下文。
        db: 测试库异步会话（直调 upsert_memory 造数）。

    Returns:
        None。

    Raises:
        AssertionError: 任一断言失败时中断。
    """
    await upsert_memory(db, auth["user_id"], "chat", "thread-1",
                        "用户偏好：赛博朋克风格的霓虹都市画面", commit=True)
    await upsert_memory(db, auth["user_id"], "chat", "thread-2",
                        "用户不喜欢低饱和度的水彩画风", commit=True)

    resp = await client.get(
        "/api/memories/search", headers=auth["headers"], params={"q": "赛博朋克霓虹"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1
    top = body["items"][0]
    assert top["key"] in ("thread-1", "thread-2")
    assert top["namespace"] == "chat"
    assert top["score"] > 0
    assert "赛博朋克" in body["items"][0]["text"] or \
        any("赛博朋克" in i["text"] for i in body["items"])

    # 用户隔离：其他用户检索不到该记忆
    other = await register_user(client, prefix="other")
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    resp_other = await client.get(
        "/api/memories/search", headers=other_headers, params={"q": "赛博朋克"})
    assert resp_other.status_code == 200
    assert resp_other.json()["total"] == 0
