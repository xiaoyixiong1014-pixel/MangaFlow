# =============================================================================
# 漫序 MangaFlow 创作端（mangaflow-web）镜像（文件级说明）
#
# 用途   : Vue3 创作端构建 + nginx 托管静态产物（容器内端口 80）
# 基础   : node:22-alpine（构建）+ nginx:alpine（托管）
# 上下文 : 仓库根（docker build -f deploy/web.Dockerfile .）
#
# 关键点：mangaflow-web 依赖本地共享包 "@mangaflow/design": "file:../mangaflow-design"，
#         因此构建上下文必须为仓库根（同时包含 mangaflow-design/），本文件先
#         构建设计系统产出 dist，再安装并构建创作端，两步均在镜像内完成，
#         不依赖宿主机预构建产物。
#
# 与部署文档 V1.0 §5.3 的对应关系：
#   - 文档使用 node:22-alpine（§5.3 写作 node:22-alpine，容器清单处笔误 node:20）；
#     因两个前端 package.json 的 engines 要求 "^22.18.0 || >=24.12.0"，
#     node:20 不满足约束，故采用 node:22-alpine
#   - 构建命令使用 vite build（等价于 npm run build 的 build-only 部分）：
#     容器内跳过 vue-tsc 全量类型检查（类型校验由开发/CI 流程负责），不修改应用代码
# =============================================================================

# ---- 阶段一：前端构建（安装依赖 + vite build）----
FROM node:22-alpine AS build

# ARG: npm 国内镜像加速开关（默认官方源；国内构建可传
#   --build-arg NPM_REGISTRY=https://registry.npmmirror.com）
ARG NPM_REGISTRY=https://registry.npmjs.org
# RUN: 将镜像源写入 npm 全局配置（对后续 npm ci / npx 拉包生效）
RUN npm config set registry "${NPM_REGISTRY}"

# ENV: 限制 V8 老生代堆上限 1536MB——2GB 内存服务器上构建时防止 vite/vue-tsc
# 默认堆策略吃满内存触发 OOM（配合部署文档的 swap 配置使用）
ENV NODE_OPTIONS=--max-old-space-size=1536

# 工作目录：/src，镜像内目录布局与仓库一致（mangaflow-design 与 mangaflow-web 平级，
# 保证 package.json 中 "file:../mangaflow-design" 的相对路径语义不变）
WORKDIR /src

# 第 1 步：安装共享设计系统依赖（先拷贝清单利用层缓存）
COPY mangaflow-design/package.json mangaflow-design/package-lock.json ./mangaflow-design/
# RUN: 优先 npm ci 按锁文件精确安装；若仓库 lock 与 package.json 不同步则回退
# npm install（按清单重新解析，不修改仓库文件）
RUN cd mangaflow-design && (npm ci || npm install)

# 第 2 步：拷贝设计系统源码并构建出 dist（vite lib 模式 + dts 声明 + 样式桥接）
COPY mangaflow-design/ ./mangaflow-design/
# RUN: 产出 dist/mangaflow-design.js|css 等文件，供第 3 步 file: 依赖解析引用
RUN cd mangaflow-design && npm run build

# 第 3 步：安装创作端依赖（npm 自动将 @mangaflow/design 以 symlink 链到 ../mangaflow-design）
COPY mangaflow-web/package.json mangaflow-web/package-lock.json ./mangaflow-web/
# RUN: 同样采用 ci 失败回退 install 的策略
RUN cd mangaflow-web && (npm ci || npm install)

# 第 4 步：拷贝创作端源码并构建产物（vite build；base 默认 "/"，经网关 "/" 反代）
COPY mangaflow-web/ ./mangaflow-web/
# RUN: npx vite build 产出 /src/mangaflow-web/dist
RUN cd mangaflow-web && npx vite build

# ---- 阶段二：nginx 托管静态产物 ----
FROM nginx:alpine

# 拷贝创作端 nginx 站点配置（SPA history 路由 fallback）
COPY deploy/web-nginx.conf /etc/nginx/conf.d/default.conf
# 拷贝构建产物到 nginx 默认站点根
COPY --from=build /src/mangaflow-web/dist /usr/share/nginx/html

# EXPOSE: 容器内托管端口
EXPOSE 80

# CMD: 前台运行 nginx（容器主进程）
CMD ["nginx", "-g", "daemon off;"]
