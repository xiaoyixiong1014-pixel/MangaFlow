/**
 * Vite 构建配置（文件级注释）。
 *
 * - dev server 端口 5173、host true（容器/局域网可访问）；一键启动脚本
 *   mangaflow.ps1 会在端口被外部进程占用时通过 --port 指定其他空闲端口
 * - 代理：/api → FastAPI 后端；/storage → 后端静态素材目录。
 *   后端端口通过环境变量 MANGAFLOW_API_PORT 注入（mangaflow.ps1 在 8300
 *   被占用导致后端端口漂移时自动设置），未设置时回退 8300
 *   （端口说明：本机 8000 被其他项目占用，MangaFlow 后端默认使用 8300）
 */
import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'

// vite.config 运行于 Node 环境; 项目未装 @types/node, 此处做最小类型声明
declare const process: { env: Record<string, string | undefined> }

// 后端 API 端口: mangaflow.ps1 启动后端后通过环境变量传入实际端口, 缺省 8300
const apiPort = process.env.MANGAFLOW_API_PORT || '8300'
// 后端 API 服务地址 (供 /api 与 /storage 代理使用)
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
    port: 5173, // 开发服务器默认端口 (可被 CLI --port 覆盖)
    host: true, // 监听 0.0.0.0，便于局域网/容器访问
    proxy: {
      // 后端 API 网关前缀（本机 8000 被其他应用占用，MangaFlow 后端默认使用 8300）
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
      // 生成/上传素材的静态资源
      '/storage': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
})
