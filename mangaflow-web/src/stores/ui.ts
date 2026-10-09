/**
 * 全局 UI 状态 store（文件级注释）。
 *
 * - 主题：'dark' | 'light'，创作端默认深色；应用机制沿用设计包
 *   （applyTheme 将 <html data-theme="..."> 切换，tokens.css 依此换变量组），
 *   持久化键 'mf_theme'
 * - 侧栏折叠：≤1360px 视口自动折叠，持久化键 'mf_sidebar'
 * - 移动端：≤1024px 视口侧栏抽屉化（默认收起，由顶栏按钮打开）
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'
import { applyTheme } from '@mangaflow/design'

/** 主题类型（与设计包 MfTheme 对齐） */
export type UiTheme = 'dark' | 'light'

/** 视口宽度阈值：小于等于该值时自动折叠侧栏 */
const COLLAPSE_BREAKPOINT = 1360

/** 移动端断点：小于等于该值时侧栏抽屉化（常驻侧栏不渲染） */
const MOBILE_BREAKPOINT = 1024

/**
 * UI 状态 store：主题、侧栏折叠与移动端抽屉。
 * @returns state/actions 集合
 */
export const useUiStore = defineStore('ui', () => {
  // ---------- state ----------
  /** 当前主题（默认深色，创作端规范） */
  const theme = ref<UiTheme>((localStorage.getItem('mf_theme') as UiTheme) || 'dark')
  /** 侧栏是否折叠（localStorage 'mf_sidebar' === '1'） */
  const sidebarCollapsed = ref(localStorage.getItem('mf_sidebar') === '1')
  /** 是否移动端视口（≤1024px，侧栏抽屉化） */
  const isMobile = ref(typeof window !== 'undefined' && window.innerWidth <= MOBILE_BREAKPOINT)
  /** 移动端抽屉侧栏是否打开（默认收起，仅移动端生效） */
  const mobileSidebarOpen = ref(false)

  /**
   * 应用启动时初始化主题（把持久化的主题套到 <html data-theme>）。
   * 未持久化过时按默认 'dark' 生效。
   */
  function initTheme(): void {
    applyTheme(theme.value)
  }

  /**
   * 设置主题：经设计包 applyTheme 切换 data-theme 属性并持久化。
   * @param value 目标主题
   */
  function setTheme(value: UiTheme): void {
    theme.value = value
    applyTheme(value) // 设计包主题机制：<html data-theme="dark|light">
    localStorage.setItem('mf_theme', value)
  }

  /** 切换深浅主题（深↔浅） */
  function toggleTheme(): void {
    setTheme(theme.value === 'dark' ? 'light' : 'dark')
  }

  /**
   * 设置侧栏折叠态并持久化。
   * @param value 是否折叠
   */
  function setSidebarCollapsed(value: boolean): void {
    sidebarCollapsed.value = value
    localStorage.setItem('mf_sidebar', value ? '1' : '0')
  }

  /** 切换移动端抽屉侧栏开合（由顶栏按钮触发） */
  function toggleMobileSidebar(): void {
    mobileSidebarOpen.value = !mobileSidebarOpen.value
  }

  /** 关闭移动端抽屉侧栏（路由切换 / 宽屏恢复时自动调用） */
  function closeMobileSidebar(): void {
    mobileSidebarOpen.value = false
  }

  // 浏览器环境监听视口变化：≤1360px 自动折叠；≤1024px 抽屉化；宽屏强制收起抽屉
  if (typeof window !== 'undefined') {
    window.addEventListener('resize', () => {
      const collapsed = window.innerWidth <= COLLAPSE_BREAKPOINT
      if (collapsed !== sidebarCollapsed.value) setSidebarCollapsed(collapsed)
      const mobile = window.innerWidth <= MOBILE_BREAKPOINT
      if (mobile !== isMobile.value) isMobile.value = mobile
      // 非 mobile 时抽屉态强制关闭，避免残留开合状态
      if (!mobile && mobileSidebarOpen.value) closeMobileSidebar()
    })
  }

  return {
    theme,
    sidebarCollapsed,
    isMobile,
    mobileSidebarOpen,
    initTheme,
    setTheme,
    toggleTheme,
    setSidebarCollapsed,
    toggleMobileSidebar,
    closeMobileSidebar,
  }
})
