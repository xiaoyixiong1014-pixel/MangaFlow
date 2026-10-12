"""pgvector 双模向量检索支撑模块（文件级说明）。

Task 6（架构偏离 #2 收敛）：为资产语义检索与用户记忆检索提供 pgvector
「可用 / 降级」双模支撑，职责四块：

- 探测：probe_pgvector() 尝试 CREATE EXTENSION IF NOT EXISTS vector 并确认
  pg_extension 注册；结果进程内缓存（模块级变量，避免每请求重复探测）。
- 影子列：ensure_pgvector_schema() 在 pgvector 可用时幂等创建
  asset_embeddings.embedding_vec 与 langgraph_store.embedding_vec
  （类型 vector(dim)），并建 ivfflat（vector_cosine_ops，lists=100）索引；
  JSONB 主存储不变（向量双写，迁移期安全）。
- 双写：upsert 时经 SAVEPOINT（begin_nested）同步影子列，影子列失败仅告警，
  绝不影响 JSONB 主存储事务。
- 分流：检索侧 can_use_pgvector(len(qvec)) 判定 → SQL 余弦 TopK
  （1 - cosine_distance，注意 ivfflat 的 <=> 为距离语义）或回退原有
  Python 全量余弦路径，两种模式返回结构完全一致。

维度决策：系统向量维度不固定——本地 hash-bag 降级恒 256 维，远程
text-embedding-v4 由模型决定；因此影子列维度取「ensure 时探测到的现有
JSONB 向量最长维度与 256 的较大者」。维度不匹配的向量不写影子列（置
NULL，主存储完好）；查询向量维度与影子列不一致时检索自动走 Python 路径。
向量维度整体切换（如远程模型变更）后 pgvector 路径自动失效，重启 ensure
时按新维度重建影子列（DROP → ADD）并从 JSONB 回填。

迁移策略：影子列与索引用运行时 ensure 而非 Alembic——扩展可能不存在，
Alembic 迁移无法表达条件 DDL；ensure 幂等，重复执行无副作用。

单库假设：探测/维度缓存为进程级单值（运行期请求与 lifespan 使用同一业务库），
与现有 SessionLocal 全局重定向的测试约定一致。
"""

import logging
import re
from typing import Any, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

logger = logging.getLogger("mangaflow.services.vector_store")

# ---------------------------------------------------------------------------
# 进程内探测/维度缓存（模块级变量；None = 未探测）
# ---------------------------------------------------------------------------
# pgvector 是否可用（True=可用；False=不可用或运行期自愈降级）
_AVAILABLE: Optional[bool] = None
# 影子列维度（ensure 成功后确定；与 _AVAILABLE 同时生效）
_DIM: Optional[int] = None

# 影子列维度兜底下限：本地 hash-bag 降级向量的固定维度
_BASE_DIM = 256
# ivfflat 索引参数：聚簇清单（与任务约定 lists=100 一致）
_IVFFLAT_LISTS = 100

# 列类型名解析（format_type 输出形如 "vector(256)" / "vector"）
_VECTOR_TYPE_RE = re.compile(r"^vector\((\d+)\)$")

# asset_embeddings.embedding JSONB 中向量元素个数的最大值（兼容 list 旧形态
# 与 {"vector": [...]} dict 形态；非数组载荷一律忽略）
_ASSET_DIM_SQL = text(
    """
    SELECT COALESCE(MAX(CASE
        WHEN jsonb_typeof(embedding) = 'object'
             AND jsonb_typeof(embedding -> 'vector') = 'array'
            THEN jsonb_array_length(embedding -> 'vector')
        WHEN jsonb_typeof(embedding) = 'array'
            THEN jsonb_array_length(embedding)
    END), 0)
    FROM asset_embeddings
    """
)

# langgraph_store 记忆行（namespace LIKE 'user_memory:%'）向量元素个数最大值：
# 向量可能写在 embedding 列（{"vector": [...]}）或 value.embedding（数组），
# 两处取较大者；站点设置 / MCP KV 等非记忆行不参与维度判定
_MEMORY_DIM_SQL = text(
    """
    SELECT COALESCE(MAX(GREATEST(
        CASE WHEN jsonb_typeof(embedding) = 'object'
                  AND jsonb_typeof(embedding -> 'vector') = 'array'
            THEN jsonb_array_length(embedding -> 'vector') END,
        CASE WHEN jsonb_typeof(value) = 'object'
                  AND jsonb_typeof(value -> 'embedding') = 'array'
            THEN jsonb_array_length(value -> 'embedding') END
    )), 0)
    FROM langgraph_store
    WHERE namespace LIKE 'user_memory:%'
    """
)


def pgvector_available() -> bool:
    """查询 pgvector 可用性缓存（不触发探测，检索热路径只读缓存）。

    Returns:
        bool: True 表示探测可用且影子列初始化成功；False/未探测均视为不可用。

    Raises:
        无。
    """
    return _AVAILABLE is True


def pgvector_dim() -> Optional[int]:
    """查询影子列维度缓存。

    Returns:
        Optional[int]: 影子列维度；未 ensure 或 pgvector 不可用时为 None。

    Raises:
        无。
    """
    return _DIM


def can_use_pgvector(vector_len: int) -> bool:
    """判定本次检索/写入能否走 pgvector 路径（双模分流唯一入口）。

    条件：探测可用 + 影子列维度已确定 + 向量维度与影子列完全一致
    （pgvector 的 <=> 运算要求两侧维度一致，维度不一致会直接报错）。

    Args:
        vector_len: 参与运算的向量维度（查询向量或写入向量长度）。

    Returns:
        bool: True 表示可走 pgvector SQL 路径；False 表示走 JSONB+Python 降级路径。

    Raises:
        无。
    """
    return _AVAILABLE is True and _DIM is not None and vector_len == _DIM


def vector_literal(vector: Sequence[float]) -> str:
    """把向量序列化为 pgvector 文本字面量 '[0.1,0.2,...]'。

    经 CAST(:v AS vector)（或 :v::vector）传入 SQL；asyncpg 对 vector 类型
    未注册 codec 时按 text 编码传输，由服务端 vector_in 解析。

    Args:
        vector: 向量分量序列。

    Returns:
        str: pgvector 字面量字符串（float 最短 round-trip 表示）。

    Raises:
        无。
    """
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def mark_pgvector_unavailable(reason: Exception) -> None:
    """运行期自愈降级：pgvector SQL 路径失败后关闭本进程的 pgvector 开关。

    典型场景：影子列被人工删除 / 列缺失导致 UndefinedColumn。置 False 后
    后续检索不再尝试 SQL 路径（避免每请求重复失败），直至进程重启重新 ensure。

    Args:
        reason: 触发降级的原始异常（仅记录日志，不向上抛出）。

    Returns:
        None。

    Raises:
        无。
    """
    global _AVAILABLE
    if _AVAILABLE:
        logger.warning(
            "pgvector 检索路径失败（%s），进程内降级为 JSONB+Python 余弦路径（重启后重新探测）",
            reason,
        )
    _AVAILABLE = False


def reset_pgvector_cache() -> None:
    """重置探测/维度缓存（仅供测试隔离使用；生产代码不应调用）。

    Returns:
        None。

    Raises:
        无。
    """
    global _AVAILABLE, _DIM
    _AVAILABLE = None
    _DIM = None


async def probe_pgvector(engine: AsyncEngine) -> bool:
    """探测 pgvector 扩展可用性：尝试 CREATE EXTENSION IF NOT EXISTS vector。

    首次调用真实探测并缓存；后续调用直接返回缓存结果。探测失败（扩展不存在、
    无超级用户权限、数据库不可达等）一律吞异常返回 False，调用方无需 try。

    Args:
        engine: 目标数据库异步引擎（探测其所在库是否可安装/已安装 vector 扩展）。

    Returns:
        bool: True —— 扩展可用（本次安装成功或此前已存在）；False —— 不可用。

    Raises:
        无：所有异常在函数内捕获并记录告警。
    """
    global _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE
    try:
        async with engine.begin() as conn:
            # CREATE EXTENSION 允许在事务内执行；IF NOT EXISTS 保证幂等
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            name = await conn.scalar(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            )
        _AVAILABLE = (name == "vector")
    except Exception as exc:  # noqa: BLE001 —— 探测失败即降级，绝不向上传播
        _AVAILABLE = False
        logger.warning("pgvector 扩展探测失败，向量检索将走 JSONB+Python 余弦降级路径：%s", exc)
    if _AVAILABLE:
        logger.info("pgvector 扩展探测成功，将启用向量影子列与 ivfflat 索引")
    return _AVAILABLE


async def ensure_pgvector_schema(engine: AsyncEngine) -> None:
    """pgvector 可用时幂等初始化影子向量列、回填存量向量并建 ivfflat 索引。

    执行顺序（单事务）：
    1. probe_pgvector —— 不可用时直接返回（跳过建列，降级路径零副作用）；
    2. 维度探测：dim = max(256, asset_embeddings JSONB 向量最长维度,
       langgraph_store 记忆行 JSONB 向量最长维度)；
    3. 两表影子列 ensure：缺失则 ADD COLUMN vector(dim)；维度不符则
       DROP 后重建（JSONB 主存储不受影响，随后回填）；
    4. 从 JSONB 回填影子列为 NULL 且维度匹配的行（幂等，数据量小；
       大数据量场景应由离线脚本回填，此处仅兜底自愈）；
    5. ivfflat 索引（vector_cosine_ops，lists=100，IF NOT EXISTS 幂等）。

    全程异常捕获：失败时置 pgvector 不可用并告警，由调用方（lifespan）
    二次兜底，绝不阻断应用启动。

    Args:
        engine: 业务库异步引擎。

    Returns:
        None。

    Raises:
        无：所有异常在函数内捕获记录，保证降级语义。
    """
    global _AVAILABLE, _DIM
    if not await probe_pgvector(engine):
        logger.info("pgvector 不可用，跳过影子列/索引初始化（检索走 JSONB+Python 余弦降级路径）")
        return
    try:
        async with engine.begin() as conn:
            # ---------- 1. 维度探测 ----------
            asset_max = int(await conn.scalar(_ASSET_DIM_SQL) or 0)
            memory_max = int(await conn.scalar(_MEMORY_DIM_SQL) or 0)
            dim = max(_BASE_DIM, asset_max, memory_max)
            logger.info(
                "pgvector 影子列维度探测完成：asset 最长 %d 维 / 记忆最长 %d 维 → 取 %d 维",
                asset_max, memory_max, dim,
            )

            # ---------- 2. 影子列 ensure（asset_embeddings / langgraph_store） ----------
            asset_action = await _ensure_vector_column(conn, "asset_embeddings", "embedding_vec", dim)
            memory_action = await _ensure_vector_column(conn, "langgraph_store", "embedding_vec", dim)

            # ---------- 3. 存量 JSONB 向量回填（幂等：仅回填影子列为 NULL 的行） ----------
            n_asset = await _backfill_asset_vectors(conn, dim)
            n_memory = await _backfill_memory_vectors(conn, dim)

            # ---------- 4. ivfflat 索引（IF NOT EXISTS 幂等） ----------
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_ae_embedding_vec ON asset_embeddings "
                f"USING ivfflat (embedding_vec vector_cosine_ops) WITH (lists = {_IVFFLAT_LISTS})"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_lgs_embedding_vec ON langgraph_store "
                f"USING ivfflat (embedding_vec vector_cosine_ops) WITH (lists = {_IVFFLAT_LISTS})"
            ))

        _DIM = dim
        _AVAILABLE = True
        logger.info(
            "pgvector 影子列初始化完成：asset_embeddings（%s，回填 %d 行）/ "
            "langgraph_store（%s，回填 %d 行），ivfflat(lists=%d) 索引就绪",
            asset_action, n_asset, memory_action, n_memory, _IVFFLAT_LISTS,
        )
    except Exception as exc:  # noqa: BLE001 —— ensure 失败降级，不阻断启动
        _AVAILABLE = False
        logger.error("pgvector 影子列/索引初始化失败，向量检索降级为 JSONB+Python 余弦路径：%s", exc)


async def _ensure_vector_column(
    conn: Any, table: str, column: str, dim: int
) -> str:
    """幂等 ensure 单表影子向量列（内部函数，须在活动事务内调用）。

    - 列不存在：ADD COLUMN vector(dim)；
    - 列存在且维度一致：跳过；
    - 列存在但维度不一致：DROP COLUMN 后按新维度重建（ivfflat 索引随列级联
      删除，随后统一重建；JSONB 主存储不受影响）。

    Args:
        conn: 活动事务中的数据库连接（AsyncConnection）。
        table: 目标表名（内部常量，非用户输入，允许拼接）。
        column: 影子列名。
        dim: 目标向量维度。

    Returns:
        str: 动作标记 "created"（新建）/ "exists"（已存在且维度一致）/
            "recreated"（维度不符重建）。

    Raises:
        Exception: DDL 执行失败向上抛出，由 ensure_pgvector_schema 统一降级。
    """
    typ = await conn.scalar(
        text(
            "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "WHERE c.relname = :t AND a.attname = :col "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"t": table, "col": column},
    )
    if typ is None:
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} vector({dim})"))
        logger.info("影子向量列已创建：%s.%s vector(%d)", table, column, dim)
        return "created"
    m = _VECTOR_TYPE_RE.match(typ or "")
    if m and int(m.group(1)) == dim:
        return "exists"
    # 维度不符：DROP 重建（JSONB 主存储完好，影子列随后回填恢复）
    logger.warning(
        "影子向量列维度不符（%s.%s 现为 %s，目标 vector(%d)），将重建并回填",
        table, column, typ, dim,
    )
    await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} vector({dim})"))
    return "recreated"


async def _backfill_asset_vectors(conn: Any, dim: int) -> int:
    """从 asset_embeddings.embedding JSONB 回填影子列（内部函数，幂等）。

    仅处理 embedding_vec IS NULL 且向量维度与影子列一致的行；JSONB 载荷
    兼容 list（旧约定）与 {"vector": [...]}（当前约定）两种形态。

    Args:
        conn: 活动事务中的数据库连接。
        dim: 影子列维度。

    Returns:
        int: 本次回填行数。

    Raises:
        Exception: 查询/更新失败向上抛出，由 ensure_pgvector_schema 统一降级。
    """
    rows = (
        await conn.execute(text(
            "SELECT id, embedding FROM asset_embeddings "
            "WHERE embedding_vec IS NULL AND embedding IS NOT NULL"
        ))
    ).all()
    params: List[dict] = []
    for r in rows:
        vec = _extract_vector(r.embedding)
        if vec is not None and len(vec) == dim:
            params.append({"id": r.id, "v": vector_literal(vec)})
    if params:
        await conn.execute(
            text("UPDATE asset_embeddings SET embedding_vec = CAST(:v AS vector) WHERE id = :id"),
            params,
        )
    return len(params)


async def _backfill_memory_vectors(conn: Any, dim: int) -> int:
    """从 langgraph_store 记忆行 JSONB 回填影子列（内部函数，幂等）。

    仅处理 namespace LIKE 'user_memory:%' 的记忆行；向量优先取 embedding 列
    （{"vector": [...]}），缺失时回退 value.embedding（数组），其余 KV 行
    （站点设置 / MCP 配置等）不受影响。

    Args:
        conn: 活动事务中的数据库连接。
        dim: 影子列维度。

    Returns:
        int: 本次回填行数。

    Raises:
        Exception: 查询/更新失败向上抛出，由 ensure_pgvector_schema 统一降级。
    """
    rows = (
        await conn.execute(text(
            "SELECT id, embedding, value FROM langgraph_store "
            "WHERE namespace LIKE 'user_memory:%' AND embedding_vec IS NULL"
        ))
    ).all()
    params: List[dict] = []
    for r in rows:
        vec = _extract_vector(r.embedding)
        if vec is None:
            vec = _extract_vector(r.value.get("embedding") if isinstance(r.value, dict) else None)
        if vec is not None and len(vec) == dim:
            params.append({"id": r.id, "v": vector_literal(vec)})
    if params:
        await conn.execute(
            text("UPDATE langgraph_store SET embedding_vec = CAST(:v AS vector) WHERE id = :id"),
            params,
        )
    return len(params)


def _extract_vector(payload: Any) -> Optional[List[float]]:
    """从 JSONB 载荷提取向量列表（内部函数）。

    兼容 list[float]（旧约定）与 {"vector": [...]}（dict 形态）。

    Args:
        payload: JSONB 值（list / dict / None）。

    Returns:
        Optional[List[float]]: 向量列表；结构不合法返回 None。

    Raises:
        无。
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        vec = payload.get("vector")
        if isinstance(vec, list):
            return vec
    return None


async def sync_asset_embedding_vec(
    db: AsyncSession, asset_type: str, asset_id: int, vector: Sequence[float]
) -> None:
    """资产影子向量列双写（SAVEPOINT 隔离，失败不影响 JSONB 主事务）。

    维度与影子列一致时写值；不一致时置 NULL（影子列与 JSONB 严格对齐，
    避免过期维度值干扰检索）。任何失败仅告警，主事务（JSONB 写入）照常提交。

    Args:
        db: 与 JSONB 写入同一事务的异步会话。
        asset_type: 资产类型 character / scene / prop / style。
        asset_id: 资产 ID。
        vector: 本次写入的向量。

    Returns:
        None。

    Raises:
        无：所有异常在函数内捕获并记录告警。
    """
    dim = pgvector_dim()
    if not pgvector_available() or dim is None:
        return
    try:
        async with db.begin_nested():  # SAVEPOINT：影子列失败只回滚到保存点
            if len(vector) == dim:
                await db.execute(
                    text(
                        "UPDATE asset_embeddings SET embedding_vec = CAST(:v AS vector) "
                        "WHERE asset_type = :t AND asset_id = :a"
                    ),
                    {"v": vector_literal(vector), "t": asset_type, "a": asset_id},
                )
            else:
                await db.execute(
                    text(
                        "UPDATE asset_embeddings SET embedding_vec = NULL "
                        "WHERE asset_type = :t AND asset_id = :a"
                    ),
                    {"t": asset_type, "a": asset_id},
                )
    except Exception as exc:  # noqa: BLE001 —— 影子列失败不影响 JSONB 主流程
        logger.warning("资产影子向量列同步失败（%s/%s）：%s", asset_type, asset_id, exc)


async def sync_memory_embedding_vec(
    db: AsyncSession, namespace: str, key: str, vector: Sequence[float]
) -> None:
    """记忆影子向量列双写（SAVEPOINT 隔离，失败不影响 JSONB 主事务）。

    维度语义与 sync_asset_embedding_vec 一致；定位键为完整 namespace
    （含 user_memory: 前缀）+ key，与 langgraph_store 唯一索引对齐。

    Args:
        db: 与 JSONB 写入同一事务的异步会话。
        namespace: 完整命名空间（形如 "user_memory:3:chat"）。
        key: 记忆键。
        vector: 本次写入的向量。

    Returns:
        None。

    Raises:
        无：所有异常在函数内捕获并记录告警。
    """
    dim = pgvector_dim()
    if not pgvector_available() or dim is None:
        return
    try:
        async with db.begin_nested():  # SAVEPOINT：影子列失败只回滚到保存点
            if len(vector) == dim:
                await db.execute(
                    text(
                        "UPDATE langgraph_store SET embedding_vec = CAST(:v AS vector) "
                        "WHERE namespace = :ns AND key = :k"
                    ),
                    {"v": vector_literal(vector), "ns": namespace, "k": key},
                )
            else:
                await db.execute(
                    text(
                        "UPDATE langgraph_store SET embedding_vec = NULL "
                        "WHERE namespace = :ns AND key = :k"
                    ),
                    {"ns": namespace, "k": key},
                )
    except Exception as exc:  # noqa: BLE001 —— 影子列失败不影响 JSONB 主流程
        logger.warning("记忆影子向量列同步失败（%s/%s）：%s", namespace, key, exc)
