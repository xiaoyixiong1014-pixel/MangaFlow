/**
 * 文件级注释：管理端路由表与导航守卫。
 * /login 为公开页面；其余页面均为 AdminLayout 子路由且 requiresAuth。
 * 守卫规则：无 token → /login；已登录但角色非 operator → 提示并回到 /login。
 */
import { createRouter, createWebHistory } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { title: '登录', public: true },
    },
    {
      path: '/',
      component: () => import('@/layouts/AdminLayout.vue'),
      redirect: '/dashboard',
      meta: { requiresAuth: true },
      children: [
        {
          path: 'dashboard',
          name: 'dashboard',
          component: () => import('@/views/DashboardView.vue'),
          meta: { title: '经营看板' },
        },
        {
          path: 'users',
          name: 'users',
          component: () => import('@/views/UsersView.vue'),
          meta: { title: '用户管理' },
        },
        {
          path: 'review',
          name: 'review',
          component: () => import('@/views/ReviewView.vue'),
          meta: { title: '内容审核' },
        },
        {
          path: 'models',
          name: 'models',
          component: () => import('@/views/ModelsView.vue'),
          meta: { title: '模型配置' },
        },
        {
          path: 'orders',
          name: 'orders',
          component: () => import('@/views/OrdersView.vue'),
          meta: { title: '订单计费' },
        },
        {
          path: 'content',
          name: 'content',
          component: () => import('@/views/ContentView.vue'),
          meta: { title: '内容运营' },
        },
        {
          path: 'settings',
          name: 'settings',
          component: () => import('@/views/SettingsView.vue'),
          meta: { title: '系统设置' },
        },
      ],
    },
    /** 兜底：未匹配路径回看板 */
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})

/**
 * 全局前置守卫：登录态与角色校验。
 * @param to 目标路由
 * @returns 放行或重定向
 */
router.beforeEach((to) => {
  const auth = useAuthStore()
  const isPublic = to.meta.public === true

  // 公开页面：已登录且具备权限时直接进看板
  if (isPublic) {
    if (auth.token && auth.isOperator) {
      return { path: '/dashboard' }
    }
    return true
  }

  // 未登录 → 登录页
  if (!auth.token) {
    return { path: '/login', query: to.fullPath !== '/' ? { redirect: to.fullPath } : undefined }
  }

  // 角色非 operator → 提示并登出回登录页
  if (!auth.isOperator) {
    auth.clearSession()
    ElMessage.error('该账号无管理权限')
    return { path: '/login' }
  }

  return true
})

/**
 * 后置守卫：同步页面标题。
 * @param to 目标路由
 */
router.afterEach((to) => {
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} · 漫序管理后台` : '漫序管理后台'
})

export default router
