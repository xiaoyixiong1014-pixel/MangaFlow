/**
 * 路由表与全局守卫（文件级注释）。
 *
 * 结构：
 * - 公开路由（meta.public）：官网首页 / 登录 / 注册 / 找回密码 / 分享页 / 帮助
 * - DefaultLayout 子路由（requiresAuth）：工作台、项目、画布、资产、账户等
 * - StudioLayout 子路由（requiresAuth）：创作工作区五阶段
 * - 错误页：403 / 404
 *
 * 守卫：requiresAuth 且无 token → 重定向 /login 并携带 redirect 回跳参数；
 * meta.title 同步 document.title。
 */
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import DefaultLayout from '@/layouts/DefaultLayout.vue'
import BlankLayout from '@/layouts/BlankLayout.vue'
import StudioLayout from '@/layouts/StudioLayout.vue'

const routes: RouteRecordRaw[] = [
  // ---------- 公开：官网首页（无布局壳，浅色 hero） ----------
  {
    path: '/',
    name: 'home',
    component: () => import('@/views/home/HomePage.vue'),
    meta: { public: true, title: '首页' },
  },

  // ---------- 公开：认证 / 分享 / 帮助（空白布局） ----------
  {
    path: '/',
    component: BlankLayout,
    children: [
      {
        path: 'login',
        name: 'login',
        component: () => import('@/views/auth/LoginView.vue'),
        meta: { public: true, title: '登录' },
      },
      {
        path: 'register',
        name: 'register',
        component: () => import('@/views/auth/RegisterView.vue'),
        meta: { public: true, title: '注册' },
      },
      {
        path: 'forgot-password',
        name: 'forgot-password',
        component: () => import('@/views/auth/ForgotPasswordView.vue'),
        meta: { public: true, title: '找回密码' },
      },
      {
        path: 'share/:token',
        name: 'share',
        component: () => import('@/views/share/ShareView.vue'),
        meta: { public: true, title: '作品分享' },
      },
      {
        path: 'help',
        name: 'help',
        component: () => import('@/views/misc/HelpView.vue'),
        meta: { public: true, title: '帮助中心' },
      },
    ],
  },

  // ---------- 主应用：DefaultLayout 子路由（需登录） ----------
  {
    path: '/',
    component: DefaultLayout,
    children: [
      {
        path: 'agent',
        name: 'agent',
        component: () => import('@/views/agent/AgentWorkspace.vue'),
        meta: { requiresAuth: true, title: 'AI 工作台' },
      },
      {
        path: 'projects',
        name: 'projects',
        component: () => import('@/views/projects/ProjectListView.vue'),
        meta: { requiresAuth: true, title: '漫剧项目' },
      },
      {
        path: 'collab',
        name: 'collab',
        component: () => import('@/views/misc/CollabDisabledView.vue'),
        meta: { requiresAuth: true, title: '协作', disabled: true },
      },
      {
        path: 'characters',
        name: 'characters',
        component: () => import('@/views/misc/CharactersView.vue'),
        meta: { requiresAuth: true, title: '角色与场景' },
      },
      {
        path: 'media',
        name: 'media',
        component: () => import('@/views/misc/MediaLibraryView.vue'),
        meta: { requiresAuth: true, title: '素材库' },
      },
      {
        path: 'prompts',
        name: 'prompts',
        component: () => import('@/views/misc/PromptLibraryView.vue'),
        meta: { requiresAuth: true, title: '提示词库' },
      },
      {
        path: 'models',
        name: 'models',
        component: () => import('@/views/misc/ModelConfigView.vue'),
        meta: { requiresAuth: true, title: '模型配置' },
      },
      {
        path: 'models/compare',
        name: 'models-compare',
        component: () => import('@/views/misc/ModelCompareView.vue'),
        meta: { requiresAuth: true, title: '模型效果对比' },
      },
      {
        path: 'billing',
        name: 'billing',
        component: () => import('@/views/misc/BillingView.vue'),
        meta: { requiresAuth: true, title: '计费与配额' },
      },
      {
        path: 'profile',
        name: 'profile',
        component: () => import('@/views/misc/ProfileView.vue'),
        meta: { requiresAuth: true, title: '个人中心' },
      },
      {
        path: 'canvas',
        name: 'canvas',
        component: () => import('@/views/canvas/CanvasListView.vue'),
        meta: { requiresAuth: true, title: 'AI 画布' },
      },
      {
        path: 'canvas/:id',
        name: 'canvas-editor',
        component: () => import('@/views/canvas/CanvasEditorView.vue'),
        meta: { requiresAuth: true, title: '画布编辑', fullscreen: true },
      },
    ],
  },

  // ---------- 创作工作区：StudioLayout 五阶段（需登录） ----------
  {
    path: '/projects/:id',
    component: StudioLayout,
    meta: { requiresAuth: true },
    children: [
      { path: '', redirect: { name: 'studio-script' } },
      {
        path: 'script',
        name: 'studio-script',
        component: () => import('@/views/studio/ScriptView.vue'),
        meta: { requiresAuth: true, title: '剧本创作' },
      },
      {
        path: 'script/extract',
        name: 'studio-extract',
        component: () => import('@/views/studio/EntityExtractView.vue'),
        meta: { requiresAuth: true, title: '实体抽取' },
      },
      {
        path: 'assets',
        name: 'studio-assets',
        component: () => import('@/views/studio/AssetsView.vue'),
        meta: { requiresAuth: true, title: '资产管理' },
      },
      {
        path: 'storyboard',
        name: 'studio-storyboard',
        component: () => import('@/views/studio/StoryboardView.vue'),
        meta: { requiresAuth: true, title: '分镜' },
      },
      {
        path: 'production',
        name: 'studio-production',
        component: () => import('@/views/studio/ProductionView.vue'),
        meta: { requiresAuth: true, title: '生成' },
      },
      {
        path: 'publish',
        name: 'studio-publish',
        component: () => import('@/views/studio/PublishView.vue'),
        meta: { requiresAuth: true, title: '发布' },
      },
    ],
  },

  // ---------- 错误页 ----------
  {
    path: '/403',
    name: 'forbidden',
    component: () => import('@/views/error/ForbiddenView.vue'),
    meta: { title: '无权访问' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/error/NotFoundView.vue'),
    meta: { title: '页面不存在' },
  },
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})

// 全局前置守卫：登录校验 + 页面标题
router.beforeEach((to) => {
  // 登录校验：直接读 localStorage（与 api/client.ts 键名一致，规避 store 时序问题）；
  // 本地无 token 视为未登录，重定向登录页并记录回跳地址
  const token = localStorage.getItem('mf_token')
  if (to.meta.requiresAuth && !token) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  // 同步页面标题
  if (to.meta.title) {
    document.title = `${to.meta.title} · 漫序 MangaFlow`
  }
})

export default router
