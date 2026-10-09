/**
 * Vite 构建配置（文件级注释）。
 *
 * - dev server 端口 5173、host true（容器/局域网可访问）
 * - 代理：/api → FastAPI 后端（127.0.0.1:8300）；/storage → 后端静态素材目录
 *   （端口说明：本机 8000 被其他项目占用，MangaFlow 后端统一使用 8300）
 */
import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'

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
    port: 5173, // 开发服务器端口
    host: true, // 监听 0.0.0.0，便于局域网/容器访问
    proxy: {
      // 后端 API 网关前缀（本机 8000 被其他应用占用，MangaFlow 后端固定使用 8300）
      '/api': {
        target: 'http://127.0.0.1:8300',
        changeOrigin: true,
      },
      // 生成/上传素材的静态资源
      '/storage': {
        target: 'http://127.0.0.1:8300',
        changeOrigin: true,
      },
    },
  },
})
