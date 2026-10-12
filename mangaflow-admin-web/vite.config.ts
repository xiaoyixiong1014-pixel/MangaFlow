/**
 * 文件级注释：Vite 构建配置。
 * 管理端独立默认端口 5174（RD-58，可被一键启动脚本的 CLI --port 覆盖），开发环境下
 * 将 /api 代理到本地 FastAPI 后端（默认 8300 端口；一键启动脚本 mangaflow.ps1 在
 * 8300 被占用导致后端端口漂移时，会通过环境变量 MANGAFLOW_API_PORT 注入实际端口）。
 */
import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'

// vite.config 运行于 Node 环境; 项目未装 @types/node, 此处做最小类型声明
declare const process: { env: Record<string, string | undefined> }

// 后端 API 端口: mangaflow.ps1 启动后端后通过环境变量传入实际端口, 缺省 8300
const apiPort = process.env.MANGAFLOW_API_PORT || '8300'
// 后端 API 服务地址 (供 /api 代理使用)
const apiTarget = `http://127.0.0.1:${apiPort}`

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    vueDevTools(),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    /** 管理端独立默认端口，与用户端隔离 (可被 CLI --port 覆盖) */
    port: 5174,
    // ENV-01（2026-10-10）：显式绑定 IPv4 0.0.0.0——Node ≥17 默认按 DNS 解析
    // localhost 命中 ::1（仅 IPv6），导致管理端 dev server 仅监听 [::1]:5174，
    // IPv4 (127.0.0.1) 不可达；与创作端 vite.config.ts 的 host 配置口径对齐
    host: "0.0.0.0",
    proxy: {
      /** 后端管理 API 统一前缀 /api，代理到 FastAPI 服务 */
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
})
