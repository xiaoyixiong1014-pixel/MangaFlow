/**
 * @mangaflow/design · 程序化 token
 *
 * 用途：画布（C-02）节点绘制、ECharts 等需要 JS 侧取真实色值的场景。
 * 数值与 styles/tokens.css 严格同源（UI 文档 §0.7 + 全站原型实测）。
 * 修改任一侧时必须同步另一侧（CI 断言见 README）。
 */

export interface ThemePalette {
  brand: string
  brandSoft: string
  brand2: string
  brand2Soft: string
  ok: string
  warn: string
  danger: string
  info: string
  bg: string
  surface: string
  surface2: string
  surface3: string
  text: string
  text2: string
  text3: string
  border: string
}

export const lightPalette: ThemePalette = {
  brand: '#12B3A6',
  brandSoft: 'rgba(18,179,166,.10)',
  brand2: '#7C6BF0',
  brand2Soft: 'rgba(124,107,240,.12)',
  ok: '#1E9E6A',
  warn: '#B87100',
  danger: '#CE4343',
  info: '#3B73C4',
  bg: '#F5F7F6',
  surface: '#FFFFFF',
  surface2: '#FAFAF8',
  surface3: '#EFF2F1',
  text: '#14181A',
  text2: '#5F6B68',
  text3: '#8E9A97',
  border: 'rgba(0,0,0,.09)',
}

export const darkPalette: ThemePalette = {
  brand: '#4FD8CA',
  brandSoft: 'rgba(18,179,166,.18)',
  brand2: '#9A8CF5',
  brand2Soft: 'rgba(124,107,240,.20)',
  ok: '#4CC38A',
  warn: '#E0A33A',
  danger: '#F0736F',
  info: '#5E9BE8',
  bg: '#0E1112',
  surface: '#15191A',
  surface2: '#1C2223',
  surface3: '#111516',
  text: '#E8EDEA',
  text2: '#9BA5A2',
  text3: '#6B7573',
  border: 'rgba(255,255,255,.09)',
}

export const palettes: Record<'light' | 'dark', ThemePalette> = {
  light: lightPalette,
  dark: darkPalette,
}

/** 圆角 token（两主题一致） */
export const radius = { card: '20px', ctl: '12px', pill: '999px' } as const

/** 布局尺寸（两主题一致） */
export const layout = { sidebarWidth: '252px', topbarHeight: '62px' } as const

/**
 * 运行时读取当前主题下的 CSS 变量值（SSR 环境返回 fallback）。
 * @param name 不含 --mf- 前缀的 token 名，如 'brand'、'r-card'
 */
export function getCssToken(name: string, fallback = ''): string {
  if (typeof window === 'undefined' || typeof document === 'undefined') return fallback
  const v = getComputedStyle(document.documentElement).getPropertyValue(`--mf-${name}`).trim()
  return v || fallback
}
