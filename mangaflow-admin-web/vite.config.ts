/**
 * 文件级注释：Vite 构建配置。
 * 管理端独立端口 5174（RD-58），开发环境下将 /api 代理到本地 FastAPI 后端（8300 端口，
 * 因本机 8000 被其他项目占用，MangaFlow 后端统一使用 8300）。
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
    /** 管理端独立端口，与用户端隔离 */
    port: 5174,
    proxy: {
      /** 后端管理 API 统一前缀 /api，代理到 FastAPI 服务 */
      '/api': {
        target: 'http://127.0.0.1:8300',
        changeOrigin: true,
      },
    },
  },
})
