# =============================================================================
# 漫序 MangaFlow 运营管理端（mangaflow-admin-web）镜像（文件级说明）
#
# 用途   : Vue3 管理端构建 + nginx 托管静态产物（容器内端口 80）
# 基础   : node:22-alpine（构建）+ nginx:alpine（托管）
# 上下文 : 仓库根（docker build -f deploy/admin.Dockerfile .）
#
# 关键点：
#   1. 管理端同样依赖本地共享包 "@mangaflow/design": "file:../mangaflow-design"，
#      构建上下文为仓库根，镜像内先构建设计系统再构建管理端；
#   2. 管理端以网关子路径 /admin/ 对外服务（RD-58 方案 B：同机独立路径），
#      构建时通过 vite CLI 参数 --base=/admin/ 指定资源前缀——产物中
#      <script>/<link> 引用与 vue-router 的 createWebHistory(import.meta.env.BASE_URL)
#      均自动跟随 /admin/，无需修改任何应用代码。
#
# 与部署文档 V1.0 §5.6 的对应关系：文档提供"独立域名（方案 A）/独立端口（方案 B）"
# 两种形态，本实现取方案 B 的变体——统一由网关 80 端口按 /admin/ 路径分流，
# 省去独立公网端口；如需方案 A，可复用本镜像将网关 server_name 换成独立域名。
# =============================================================================

# ---- 阶段一：管理端构建（安装依赖 + vite build --base=/admin/）----
FROM node:22-alpine AS build

# ARG: npm 国内镜像加速开关（默认官方源；国内构建可传
#   --build-arg NPM_REGISTRY=https://registry.npmmirror.com）
ARG NPM_REGISTRY=https://registry.npmjs.org
# RUN: 将镜像源写入 npm 全局配置（对后续 npm ci / npx 拉包生效）
RUN npm config set registry "${NPM_REGISTRY}"

# 工作目录：/src，镜像内目录布局与仓库一致（保证 file:../mangaflow-design 相对路径语义）
WORKDIR /src

# 第 1 步：安装共享设计系统依赖
COPY mangaflow-design/package.json mangaflow-design/package-lock.json ./mangaflow-design/
# RUN: 优先 npm ci；lock 与清单不同步时回退 npm install（不修改仓库文件）
RUN cd mangaflow-design && (npm ci || npm install)

# 第 2 步：构建设计系统 dist（管理端与创作端共用同一构建产物来源）
COPY mangaflow-design/ ./mangaflow-design/
# RUN: 产出 dist/，供管理端构建时 file: 链接解析
RUN cd mangaflow-design && npm run build

# 第 3 步：安装管理端依赖（@mangaflow/design symlink 至 ../mangaflow-design）
COPY mangaflow-admin-web/package.json mangaflow-admin-web/package-lock.json ./mangaflow-admin-web/
# RUN: 同样采用 ci 失败回退 install 的策略
RUN cd mangaflow-admin-web && (npm ci || npm install)

# 第 4 步：拷贝管理端源码并构建（--base=/admin/ 使产物资源路径与路由 base 一致）
COPY mangaflow-admin-web/ ./mangaflow-admin-web/
# RUN: 产出 /src/mangaflow-admin-web/dist（资源引用前缀 /admin/）
RUN cd mangaflow-admin-web && npx vite build --base=/admin/

# ---- 阶段二：nginx 托管静态产物 ----
FROM nginx:alpine

# 拷贝管理端 nginx 站点配置（剥 /admin/ 前缀 + SPA history 路由 fallback）
COPY deploy/admin-nginx.conf /etc/nginx/conf.d/default.conf
# 拷贝构建产物到镜像内站点目录（注意：产物本身不含 admin/ 目录层级，
# 前缀由站点配置 alias 剥离映射）
COPY --from=build /src/mangaflow-admin-web/dist /usr/share/nginx/html

# EXPOSE: 容器内托管端口
EXPOSE 80

# CMD: 前台运行 nginx（容器主进程）
CMD ["nginx", "-g", "daemon off;"]
