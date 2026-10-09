<!--
  默认布局（文件级注释）。
  结构：el-container 包裹 —— 左侧 MfSidebar（四组 11 项菜单 + 底部帮助），
  右侧纵向：MfTopbar（折叠按钮/标题/积分余额/通知/主题切换/头像菜单）+ main 路由出口。
  折叠态由 ui store 控制；meta.fullscreen 的页面隐藏侧栏以最大化画布区。
  响应式：≤1024px 时常驻侧栏不渲染，改为覆盖式抽屉（默认收起，顶栏按钮打开，
  路由切换后自动关闭），抽屉背景使用设计包变量以适配深浅主题。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { MfSidebar, MfTopbar, type MfMenuGroup } from '@mangaflow/design'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import * as announcementsApi from '@/api/announcements'
import type { Announcement } from '@/types'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const ui = useUiStore()

/** 侧栏菜单：四组 11 项（创作/项目/资产/账户）+ 底部帮助 */
const menu: MfMenuGroup[] = [
  {
    title: '创作',
    items: [
      { key: '/agent', label: 'AI 工作台', icon: '✨' },
    ],
  },
  {
    title: '项目',
    items: [
      { key: '/projects', label: '漫剧项目', icon: '🎬' },
      { key: '/canvas', label: 'AI 画布', icon: '🧩' },
      { key: '/collab', label: '协作', icon: '👥', disabled: true },
    ],
  },
  {
    title: '资产',
    items: [
      { key: '/characters', label: '角色与场景', icon: '🧑‍🎨' },
      { key: '/media', label: '素材库', icon: '🖼️' },
      { key: '/prompts', label: '提示词库', icon: '📝' },
    ],
  },
  {
    title: '账户',
    items: [
      { key: '/models', label: '模型配置', icon: '⚙️' },
      { key: '/billing', label: '计费与配额', icon: '💰' },
      { key: '/profile', label: '个人中心', icon: '👤' },
    ],
  },
]

/**
 * 依据当前路由推导侧栏高亮 key：
 * 创作工作区（/projects/:id/*）归入"漫剧项目"，其余取首段路径。
 */
const activeKey = computed(() => {
  const path = route.path
  if (/^\/projects\/\d+/.test(path)) return '/projects'
  return `/${path.split('/')[1] ?? ''}`
})

/** 顶栏标题（取路由 meta.title） */
const pageTitle = computed(() => (route.meta.title as string | undefined) ?? '')

/** 当前页面是否全屏（隐藏侧栏） */
const isFullscreen = computed(() => route.meta.fullscreen === true)

/** 通知公告（顶栏 Popover 展示前 3 条） */
const notices = ref<Announcement[]>([])

/**
 * 菜单点击：跳转对应路由。
 * @param key 菜单项 key（即路由路径）
 */
function onSelect(key: string): void {
  router.push(key)
}

/** 退出登录：清空会话并回到登录页 */
function handleLogout(): void {
  auth.logout()
  ElMessage.success('已退出登录')
  router.push('/login')
}

// 路由切换后自动关闭移动端抽屉侧栏（仅移动端有开合状态，非 mobile 调用为幂等空操作）
watch(
  () => route.path,
  () => ui.closeMobileSidebar(),
)

// 首次进入：拉取用户信息（余额）与前 3 条公告
onMounted(async () => {
  if (auth.isLoggedIn) {
    auth.fetchMe().catch(() => {/* 401 已由拦截器统一处理 */})
  }
  try {
    notices.value = (await announcementsApi.list()).slice(0, 3)
  } catch {
    // 公告为公开接口，失败不影响主流程
  }
})
</script>

<template>
  <el-container class="default-layout">
    <!-- 侧栏：全屏页隐藏；移动端（≤1024px）不渲染常驻侧栏，改用下方抽屉 -->
    <MfSidebar
      v-if="!isFullscreen && !ui.isMobile"
      :menu="menu"
      :active-key="activeKey"
      :collapsed="ui.sidebarCollapsed"
      @select="onSelect"
    >
      <template #footer>
        <button type="button" class="help-link" @click="router.push('/help')">
          <span aria-hidden="true">❓</span>
          <span v-if="!ui.sidebarCollapsed">帮助</span>
        </button>
      </template>
    </MfSidebar>

    <!-- 移动端抽屉侧栏：同一菜单配置，展开态呈现；路由切换自动关闭 -->
    <el-drawer
      v-if="ui.isMobile"
      direction="ltr"
      size="240px"
      :with-header="false"
      :model-value="ui.mobileSidebarOpen"
      class="mobile-sidebar-drawer"
      @update:model-value="(v: boolean) => { if (!v) ui.closeMobileSidebar() }"
    >
      <MfSidebar :menu="menu" :active-key="activeKey" :collapsed="false" @select="onSelect">
        <template #footer>
          <button type="button" class="help-link" @click="router.push('/help')">
            <span aria-hidden="true">❓</span>
            <span>帮助</span>
          </button>
        </template>
      </MfSidebar>
    </el-drawer>

    <el-container direction="vertical" class="default-layout__right">
      <MfTopbar>
        <template #left>
          <!-- 顶栏控制按钮：移动端打开/关闭抽屉，桌面端折叠/展开侧栏 -->
          <button
            v-if="!isFullscreen"
            type="button"
            class="collapse-btn"
            :title="ui.isMobile ? '打开侧栏' : (ui.sidebarCollapsed ? '展开侧栏' : '折叠侧栏')"
            @click="ui.isMobile ? ui.toggleMobileSidebar() : ui.setSidebarCollapsed(!ui.sidebarCollapsed)"
          >
            {{ ui.isMobile ? '☰' : (ui.sidebarCollapsed ? '»' : '«') }}
          </button>
          <span class="page-title">{{ pageTitle }}</span>
        </template>

        <template #right>
          <!-- 积分余额：点击跳计费页 -->
          <button type="button" class="balance-btn" title="积分余额，点击查看计费与配额" @click="router.push('/billing')">
            <span aria-hidden="true">💰</span>
            <span>{{ auth.balance }}</span>
          </button>

          <!-- 通知：前 3 条公告 -->
          <el-popover placement="bottom-end" :width="300" trigger="click">
            <template #reference>
              <button type="button" class="icon-btn" title="通知">
                <span aria-hidden="true">🔔</span>
              </button>
            </template>
            <div v-if="notices.length === 0" class="notice-empty">暂无公告</div>
            <ul v-else class="notice-list">
              <li v-for="n in notices" :key="n.id" class="notice-item">
                <div class="notice-title">{{ n.title }}</div>
                <div class="notice-content">{{ n.content }}</div>
              </li>
            </ul>
          </el-popover>

          <!-- 主题切换 -->
          <button
            type="button"
            class="icon-btn"
            :title="ui.theme === 'dark' ? '切换浅色' : '切换深色'"
            @click="ui.toggleTheme()"
          >
            {{ ui.theme === 'dark' ? '🌙' : '☀️' }}
          </button>

          <!-- 头像菜单 -->
          <el-dropdown trigger="click" @command="(cmd: string) => (cmd === 'profile' ? router.push('/profile') : handleLogout())">
            <button type="button" class="avatar-btn">
              <span class="avatar-circle">{{ (auth.user?.username ?? '游')[0] }}</span>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="profile">个人中心</el-dropdown-item>
                <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </MfTopbar>

      <main class="default-layout__main">
        <router-view />
      </main>
    </el-container>
  </el-container>
</template>

<style scoped>
.default-layout {
  height: 100vh;
  background: var(--mf-bg);
  color: var(--mf-text);
}
.default-layout__right {
  min-width: 0;
}
.default-layout__main {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

/* 折叠按钮 / 图标按钮 */
.collapse-btn,
.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: none;
  border-radius: var(--mf-r-ctl);
  background: transparent;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-lg);
  cursor: pointer;
  transition: background var(--mf-dur) var(--mf-ease), color var(--mf-dur) var(--mf-ease);
}
.collapse-btn:hover,
.icon-btn:hover {
  background: var(--mf-surface-2);
  color: var(--mf-text);
}

/* 页面标题 */
.page-title {
  font-size: var(--mf-fs-lg);
  font-weight: var(--mf-fw-semibold);
  color: var(--mf-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 积分余额按钮 */
.balance-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--mf-sp-1);
  height: 32px;
  padding: 0 var(--mf-sp-3);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-pill);
  background: var(--mf-brand-soft);
  color: var(--mf-brand);
  font-family: var(--mf-font);
  font-size: var(--mf-fs-sm);
  font-weight: var(--mf-fw-semibold);
  cursor: pointer;
  transition: filter var(--mf-dur) var(--mf-ease);
}
.balance-btn:hover {
  filter: brightness(1.05);
}

/* 通知列表 */
.notice-empty {
  padding: var(--mf-sp-5);
  text-align: center;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.notice-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.notice-item + .notice-item {
  margin-top: var(--mf-sp-3);
  padding-top: var(--mf-sp-3);
  border-top: 1px solid var(--mf-border);
}
.notice-title {
  font-weight: var(--mf-fw-semibold);
  color: var(--mf-text);
  font-size: var(--mf-fs-sm);
}
.notice-content {
  margin-top: 2px;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-xs);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* 头像 */
.avatar-btn {
  border: none;
  background: transparent;
  padding: 0;
  cursor: pointer;
  display: inline-flex;
}
.avatar-circle {
  width: 32px;
  height: 32px;
  border-radius: var(--mf-r-pill);
  background: linear-gradient(135deg, var(--mf-brand), var(--mf-brand2));
  color: var(--mf-brand-contrast);
  font-size: var(--mf-fs-md);
  font-weight: var(--mf-fw-semibold);
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

/* 侧栏底部帮助 */
.help-link {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  width: 100%;
  height: 38px;
  padding: 0 var(--mf-sp-3);
  border: none;
  border-radius: var(--mf-r-ctl);
  background: transparent;
  color: var(--mf-text-2);
  font-family: var(--mf-font);
  font-size: var(--mf-fs-md);
  cursor: pointer;
  transition: background var(--mf-dur) var(--mf-ease), color var(--mf-dur) var(--mf-ease);
}
.help-link:hover {
  background: var(--mf-surface-2);
  color: var(--mf-text);
}
</style>

<style>
/* ============================================================
   移动端抽屉侧栏（全局样式，el-drawer 挂载于 body 下）
   背景使用设计包变量 --mf-surface-2（次级面板/悬浮底），避免深色主题白块
   ============================================================ */
.mobile-sidebar-drawer .el-drawer,
.el-drawer.mobile-sidebar-drawer {
  background: var(--mf-surface-2);
  color: var(--mf-text);
}
.mobile-sidebar-drawer .el-drawer__body {
  padding: 0;
}
</style>
