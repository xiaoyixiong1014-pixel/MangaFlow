"""临时运维脚本：终结测试库上空闲超过阈值的 idle in transaction 会话。

用于多代理并行 pytest 共享同一测试库时清理泄漏的长事务持锁会话，
只处理 idle 超过 LEAK_THRESHOLD 秒的会话，避免误杀在途请求。
"""
import asyncio

import asyncpg

# 空闲事务超过该秒数视为泄漏会话
LEAK_THRESHOLD_SECONDS = 120


async def main() -> None:
    """连接测试库并终结泄漏会话（内部函数）。"""
    conn = await asyncpg.connect("postgresql://postgres:123456@localhost:5432/mangaflow_test")
    rows = await conn.fetch(
        """
        SELECT pid, state, xact_start,
               extract(epoch FROM now() - state_change)::int AS idle_secs,
               left(query, 60) AS q
        FROM pg_stat_activity
        WHERE datname = 'mangaflow_test'
          AND state = 'idle in transaction'
          AND now() - state_change > make_interval(secs => $1)
        """,
        LEAK_THRESHOLD_SECONDS,
    )
    for r in rows:
        print("terminating leak:", r["pid"], "idle", r["idle_secs"], "s |", r["q"])
        await conn.execute("SELECT pg_terminate_backend($1::int)", r["pid"])
    if not rows:
        print("no leaks")
    await conn.close()


asyncio.run(main())
