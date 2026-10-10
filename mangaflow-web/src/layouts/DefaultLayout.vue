<!--
  默认布局（文件级注释）。
  结构：el-container 包裹 —— 左侧 MfSidebar（四组 11 项菜单 + 底部帮助），
  右侧纵向：MfTopbar（折叠按钮/标题/积分余额/通知铃铛/主题切换/头像菜单）
  + 配额预警条 + main 路由出口。
  折叠态由 ui store 控制；meta.fullscreen 的页面隐藏侧栏以最大化画布区。
  响应式：≤1024px 时常驻侧栏不渲染，改为覆盖式抽屉（默认收起，顶栏按钮打开，
  路由切换后自动关闭），抽屉背景使用设计包变量以适配深浅主题。
  通知（Task 10.2）：铃铛 el-badge 未读数 + Popover 列表（类型 icon/标题/时间，
  点击标记已读 / 全部已读），挂载时拉 unread-count 并订阅 /notifications/stream
  实时追加（断线静默 + 5s 延迟重连）；积分余额低于免费额度 20% 时展示配额预警条。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { MfSidebar, MfTopbar, type MfMenuGroup } from '@mangaflow/design'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import * as notificationsApi from '@/api/notifications'
import type { NotificationItem } from '@/types'

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
      { key: '/models/compare', label: '模型效果对比', icon: '📊' },
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

// ============================================================
// 站内通知（Task 10.2 D-05 配套：通知铃铛）
// ============================================================

/** 通知列表（Popover 内展示最近 20 条） */
const notifications = ref<NotificationItem[]>([])
/** 未读通知数（铃铛角标） */
const unreadCount = ref(0)
/** SSE 订阅中止控制器 */
let notifyAbort: AbortController | null = null
/** 组件是否已卸载（卸载后不再重连通知流） */
let notifyDisposed = false
/** 重连定时器 */
let notifyRetryTimer: ReturnType<typeof setTimeout> | null = null

/** 通知类型 → 展示 icon 映射 */
const NOTIFY_ICONS: Record<string, string> = {
  task_done: '✅',
  task_failed: '❌',
  review_result: '🛡️',
  quota_warning: '⚠️',
  system: '📢',
}

/**
 * 拉取通知列表与未读数（挂载时一次）。
 * @returns 无
 */
async function fetchNotifications(): Promise<void> {
  try {
    const [listResp, countResp] = await Promise.all([
      notificationsApi.list({ page: 1, page_size: 20 }),
      notificationsApi.unreadCount(),
    ])
    notifications.value = listResp.items
    unreadCount.value = countResp.unread_count
  } catch {
    // 通知为辅助功能，失败静默（拦截器已提示 4xx detail）
  }
}

/**
 * 订阅通知 SSE 实时流：message 事件头部追加通知并角标 +1。
 * 流结束（服务端 600s 超时）或断开后 5s 自动重连，直到页面卸载。
 * @returns 无
 */
function subscribeNotifications(): void {
  notifyAbort = new AbortController()
  void notificationsApi
    .stream((event, data) => {
      if (event !== 'message') return // heartbeat/done 忽略
      const item = data as NotificationItem
      if (!item?.id) return
      // 去重：SSE 重连补推时避免同一条重复展示
      if (notifications.value.some((n) => n.id === item.id)) return
      notifications.value.unshift(item)
      if (notifications.value.length > 30) notifications.value.pop()
      if (!item.read_at) unreadCount.value += 1
    }, notifyAbort.signal)
    .catch(() => {
      // 断线容错：静默后延迟重连（页面隐藏/网络抖动等场景）
    })
    .finally(() => {
      if (notifyDisposed) return
      notifyRetryTimer = setTimeout(() => subscribeNotifications(), 5000)
    })
}

/**
 * 点击单条通知：未读则标记已读（本地同步更新，角标 -1）。
 * @param n 目标通知
 * @returns 无
 */
async function onNotificationClick(n: NotificationItem): Promise<void> {
  if (n.read_at) return
  n.read_at = new Date().toISOString()
  unreadCount.value = Math.max(0, unreadCount.value - 1)
  try {
    await notificationsApi.markRead({ ids: [n.id] })
  } catch {
    // 已读失败静默：下次打开列表会以服务端状态为准
  }
}

/**
 * 全部已读：本地全部置已读并调批量接口。
 * @returns 无
 */
async function markAllRead(): Promise<void> {
  if (unreadCount.value === 0) return
  notifications.value.forEach((n) => {
    if (!n.read_at) n.read_at = new Date().toISOString()
  })
  unreadCount.value = 0
  try {
    await notificationsApi.markRead({ all: true })
  } catch {
    // 失败静默
  }
}

// ============================================================
// 配额预警（PL-24）：余额低于免费额度（5000）的 20% 时提示
// ============================================================

/** 注册赠送的免费积分额度（与后端 auth._REGISTER_GRANT_POINTS 一致） */
const FREE_QUOTA_POINTS = 5000
/** 预警阈值：免费额度 20% */
const QUOTA_WARN_THRESHOLD = FREE_QUOTA_POINTS * 0.2
/** 余额是否已拉取完成（避免初始 0 误报） */
const balanceLoaded = ref(false)
/** 是否展示配额预警条 */
const showQuotaAlert = computed(
  () => balanceLoaded.value && auth.isLoggedIn && auth.balance < QUOTA_WARN_THRESHOLD,
)

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

// 首次进入：拉取用户信息（余额）；登录态下拉取通知并启动实时流
onMounted(async () => {
  if (auth.isLoggedIn) {
    try {
      await auth.fetchMe()
    } catch {
      /* 401 已由拦截器统一处理 */
    }
    balanceLoaded.value = true
    void fetchNotifications()
    subscribeNotifications()
  }
})

// 页面卸载：断开通知流并取消重连定时器
onBeforeUnmount(() => {
  notifyDisposed = true
  if (notifyRetryTimer) clearTimeout(notifyRetryTimer)
  notifyAbort?.abort()
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

          <!-- 通知铃铛：未读角标 + 通知列表（Task 10.2） -->
          <el-popover placement="bottom-end" :width="340" trigger="click" @show="fetchNotifications">
            <template #reference>
              <el-badge :value="unreadCount" :hidden="unreadCount === 0" :max="99" class="notify-badge">
                <button type="button" class="icon-btn" title="通知">
                  <span aria-hidden="true">🔔</span>
                </button>
              </el-badge>
            </template>
            <div class="notify-head">
              <span class="notify-head__title">通知中心</span>
              <el-button link type="primary" size="small" :disabled="unreadCount === 0" @click="markAllRead">
                全部已读
              </el-button>
            </div>
            <div v-if="notifications.length === 0" class="notify-empty">暂无通知</div>
            <ul v-else class="notify-list">
              <li
                v-for="n in notifications"
                :key="n.id"
                class="notify-item"
                :class="{ 'notify-item--unread': !n.read_at }"
                @click="onNotificationClick(n)"
              >
                <span class="notify-icon" aria-hidden="true">{{ NOTIFY_ICONS[n.type] ?? '📨' }}</span>
                <div class="notify-body">
                  <div class="notify-title">
                    {{ n.title }}
                    <span v-if="!n.read_at" class="notify-dot" />
                  </div>
                  <div class="notify-content">{{ n.content }}</div>
                  <div class="notify-time">{{ n.created_at?.slice(0, 16).replace('T', ' ') ?? '' }}</div>
                </div>
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

      <!-- 配额预警条（PL-24）：余额低于免费额度 20% 时展示 -->
      <el-alert
        v-if="showQuotaAlert"
        type="warning"
        show-icon
        :closable="true"
        class="quota-alert"
        title="积分余额不足"
        description="当前积分余额低于免费额度的 20%，AI 生成任务可能因余额不足失败，建议前往计费页充值。"
        @click="router.push('/billing')"
      />

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

/* 通知铃铛与列表（Task 10.2） */
.notify-badge {
  display: inline-flex;
}
.notify-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: var(--mf-sp-2);
  border-bottom: 1px solid var(--mf-border);
}
.notify-head__title {
  font-weight: var(--mf-fw-semibold);
  font-size: var(--mf-fs-sm);
}
.notify-empty {
  padding: var(--mf-sp-5);
  text-align: center;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.notify-list {
  margin: 0;
  padding: 0;
  list-style: none;
  max-height: 360px;
  overflow-y: auto;
}
.notify-item {
  display: flex;
  gap: var(--mf-sp-2);
  padding: var(--mf-sp-2) var(--mf-sp-1);
  border-radius: var(--mf-r-ctl);
  cursor: pointer;
  transition: background var(--mf-dur) var(--mf-ease);
}
.notify-item:hover {
  background: var(--mf-surface-2);
}
.notify-item + .notify-item {
  margin-top: 2px;
  padding-top: var(--mf-sp-2);
  border-top: 1px solid var(--mf-border);
}
.notify-item--unread .notify-title {
  font-weight: var(--mf-fw-semibold);
}
.notify-icon {
  flex: none;
  font-size: var(--mf-fs-md);
}
.notify-body {
  flex: 1;
  min-width: 0;
}
.notify-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: var(--mf-fw-medium);
  color: var(--mf-text);
  font-size: var(--mf-fs-sm);
}
.notify-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--mf-danger, #e5484d);
  flex: none;
}
.notify-content {
  margin-top: 2px;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-xs);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.notify-time {
  margin-top: 2px;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}

/* 配额预警条（PL-24） */
.quota-alert {
  margin: 0 var(--mf-sp-5) var(--mf-sp-2);
  border-radius: var(--mf-r-ctl);
  cursor: pointer;
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
