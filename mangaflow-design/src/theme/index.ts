import { readonly, ref } from 'vue'

export type MfTheme = 'light' | 'dark'

const theme = ref<MfTheme>('light')

/** 应用主题到 <html data-theme="...">（tokens.css 依此切换变量组） */
export function applyTheme(t: MfTheme): void {
  theme.value = t
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.theme = t
  }
}

/** 应用启动时初始化主题；未传则读 localStorage / 系统偏好 */
export function initTheme(defaultTheme?: MfTheme): MfTheme {
  if (typeof localStorage !== 'undefined') {
    const saved = localStorage.getItem('mf-theme') as MfTheme | null
    if (saved === 'light' || saved === 'dark') {
      applyTheme(saved)
      return saved
    }
  }
  if (defaultTheme) {
    applyTheme(defaultTheme)
    return defaultTheme
  }
  const prefersDark =
    typeof window !== 'undefined' &&
    window.matchMedia?.('(prefers-color-scheme: dark)').matches
  applyTheme(prefersDark ? 'dark' : 'light')
  return theme.value
}

/** 切换主题并持久化 */
export function toggleTheme(): MfTheme {
  const next: MfTheme = theme.value === 'dark' ? 'light' : 'dark'
  applyTheme(next)
  try {
    localStorage.setItem('mf-theme', next)
  } catch {
    /* 隐私模式下忽略 */
  }
  return next
}

/** 主题组合式 API */
export function useTheme() {
  return {
    theme: readonly(theme),
    applyTheme,
    initTheme,
    toggleTheme,
  }
}
