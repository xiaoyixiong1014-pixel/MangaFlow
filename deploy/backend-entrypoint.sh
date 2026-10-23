#!/bin/sh
# =============================================================================
# 漫序 MangaFlow 后端容器入口脚本（文件级说明）
#
# 用途   : backend 容器启动前置处理——修复命名卷属主后，从 root 降权到
#          非特权用户 appuser 再 exec 主进程（uvicorn）。
# 背景   : mf_prod_storage 命名卷首次挂载时，/app/backend/storage 会被卷内容
#          覆盖为 root:root 属主，非 root 应用进程无法写入；本脚本在启动时
#          检测并修复属主（仅在属主不匹配时执行，避免每次全量 chown 开销）。
# 安全   : 容器以 root 启动仅为本脚本完成属主修复，修复后立即通过 gosu
#          切换到 appuser 运行业务进程，uvicorn / ffmpeg 均以非特权身份执行。
# =============================================================================

set -e

# 仅在以 root 身份启动时执行属主修复与降权（healthcheck 等以 appuser 直接 exec）
if [ "$(id -u)" = "0" ]; then
    # 存储目录属主与应用用户不一致（典型为首次挂载空命名卷）时修复
    # stat -c %u：GNU coreutils（python:3.12-slim 自带）输出属主 uid
    if [ "$(stat -c %u /app/backend/storage)" != "$(id -u appuser)" ]; then
        chown -R appuser:appuser /app/backend/storage
    fi
    # 降权并以业务命令替换本 shell（gosu 不引入中间进程，信号可直达 uvicorn）
    exec gosu appuser "$@"
fi

# 非 root 身份进入（如 compose exec 调试）时直接执行命令
exec "$@"
