<script setup lang="ts">
/**
 * 文件级注释：管理端主布局。
 * 顶部横向导航：左侧 logo「漫序 MangaFlow · 运营管理」，中间 7 个模块 Tab
 * （el-menu horizontal），右侧管理员名 + 退出登录；下方为 router-view 内容区。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

/** 顶部导航 7 个模块 */
const menus = [
  { path: '/dashboard', label: '经营看板' },
  { path: '/users', label: '用户管理' },
  { path: '/review', label: '内容审核' },
  { path: '/models', label: '模型配置' },
  { path: '/orders', label: '订单计费' },
  { path: '/content', label: '内容运营' },
  { path: '/settings', label: '系统设置' },
]

/** 当前激活菜单（跟随路由） */
const activeMenu = computed(() => `/${route.path.split('/')[1] ?? ''}`)

/** 管理员显示名 */
const adminName = computed(() => auth.user?.username ?? '管理员')

/**
 * 退出登录：二次确认后清空会话并跳转登录页。
 * @returns 无
 * @throws 无（取消确认时静默返回）
 */
async function handleLogout() {
  try {
    await ElMessageBox.confirm('确定退出管理后台吗？', '退出登录', {
      confirmButtonText: '退出',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  auth.clearSession()
  router.push('/login')
}
</script>

<template>
  <div class="admin-shell">
    <!-- 顶部横向导航栏 -->
    <header class="admin-topbar">
      <div class="admin-topbar__brand" @click="router.push('/dashboard')">
        <span class="admin-logo" aria-hidden="true" />
        <span class="admin-topbar__title">漫序 MangaFlow · 运营管理</span>
      </div>

      <el-menu
        mode="horizontal"
        :default-active="activeMenu"
        :ellipsis="false"
        router
        class="admin-topbar__nav"
      >
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          {{ m.label }}
        </el-menu-item>
      </el-menu>

      <div class="admin-topbar__right">
        <el-dropdown>
          <span class="admin-user">
            <el-avatar :size="28" class="admin-user__avatar">
              {{ adminName.slice(0, 1).toUpperCase() }}
            </el-avatar>
            <span class="admin-user__name">{{ adminName }}</span>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item data-test="logout" @click="handleLogout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>

    <!-- 页面内容区 -->
    <main class="admin-main">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.admin-shell {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--mf-bg);
  color: var(--mf-text);
}

.admin-topbar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-6);
  height: var(--mf-topbar-h);
  padding: 0 var(--mf-sp-5);
  background: var(--mf-surface);
  border-bottom: 1px solid var(--mf-border);
  flex-shrink: 0;
}

.admin-topbar__brand {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  cursor: pointer;
  flex-shrink: 0;
}

.admin-logo {
  width: 22px;
  height: 22px;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--mf-brand), var(--mf-brand2));
}

.admin-topbar__title {
  font-size: var(--mf-fs-md);
  font-weight: var(--mf-fw-bold);
  white-space: nowrap;
}

.admin-topbar__nav {
  flex: 1;
  min-width: 0;
  border-bottom: none;
  background: transparent;
}

.admin-topbar__right {
  display: flex;
  align-items: center;
  flex-shrink: 0;
}

.admin-user {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  cursor: pointer;
  outline: none;
}

.admin-user__avatar {
  background: linear-gradient(135deg, var(--mf-brand), var(--mf-brand2));
  color: #fff;
  font-size: var(--mf-fs-sm);
  font-weight: var(--mf-fw-semibold);
}

.admin-user__name {
  font-size: var(--mf-fs-sm);
  color: var(--mf-text-2);
}

.admin-main {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--mf-sp-5);
}
</style>
