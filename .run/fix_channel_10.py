# -*- coding: utf-8 -*-
"""一次性修复脚本：将用户自有图像渠道（id=10）切换为 DashScope 原生协议。

原因：qwen-image-3.0 为图像模型，百炼 OpenAI 兼容模式（compatible-mode）
不提供 /images/generations 端点——专属实例直接 403 Workspace endpoint
access denied，被连通测试判为鉴权失败（down）。
修正：protocol 改 dashscope（多模态生成端点）、base_url 改公共云根域名
（专属实例根域名未开通多模态生成端点权限，同为 403），健康重置 unknown。
"""
import asyncio
import os

import asyncpg

DSN = "postgresql://postgres:123456@localhost:5432/mangaflow"


async def main() -> None:
    """主流程：更新渠道配置并用真实 Key 验证连通。"""
    conn = await asyncpg.connect(DSN)
    row = await conn.fetchrow(
        "UPDATE model_channels "
        "SET protocol = 'dashscope', base_url = 'https://dashscope.aliyuncs.com', "
        "    health_status = 'unknown', last_check_at = NULL, updated_at = now() "
        "WHERE id = 10 RETURNING id, protocol, base_url")
    key_row = await conn.fetchrow(
        "SELECT api_key_encrypted FROM model_channels WHERE id = 10")
    await conn.close()
    print("更新结果:", dict(row) if row else "未找到渠道")

    # 用真实 Key 跑一次连通探测（无效模型提交，不计费）
    os.sys.path.insert(0, r"d:\Code_Work\MangaFlow\backend")
    from app.core.crypto import decrypt_text

    api_key = decrypt_text(key_row["api_key_encrypted"])
    import httpx

    url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    payload = {
        "model": "__probe_invalid_model__",
        "input": {"messages": [{"role": "user", "content": [{"text": "ping"}]}]},
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=payload)
    ok = resp.status_code not in (401, 403) and resp.status_code < 500
    print(f"连通探测: HTTP {resp.status_code} -> {'连通正常' if ok else '失败: ' + resp.text[:120]}")
    if ok:
        conn = await asyncpg.connect(DSN)
        await conn.execute(
            "UPDATE model_channels SET health_status = 'healthy', "
            "last_check_at = now() WHERE id = 10")
        await conn.close()
        print("渠道健康已置为 healthy")


if __name__ == "__main__":
    asyncio.run(main())
