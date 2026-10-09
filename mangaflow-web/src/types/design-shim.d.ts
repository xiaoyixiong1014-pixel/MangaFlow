/**
 * @mangaflow/design 类型补充声明（文件级注释）。
 *
 * 设计包构建产物缺失 components 桶文件的 d.ts（dist/components/index.d.ts），
 * 导致根入口 `export * from './components'` 在类型层面没有组件导出
 * （运行时 JS 导出正常）。此文件对根入口做模块扩充，按源码 API 补齐
 * 六个组件与菜单类型，保证业务侧具名导入与模板类型检查可用。
 * 后续设计包重新构建补齐桶文件后，本文件可整体移除。
 */
declare module '@mangaflow/design' {
  import type { DefineComponent } from 'vue'

  // ============================================================
  // theme（对应设计包 src/theme/index.ts）
  // ============================================================

  /** 主题枚举 */
  export type MfTheme = 'light' | 'dark'

  /** 应用主题到 <html data-theme="...">（tokens.css 依此切换变量组） */
  export function applyTheme(t: MfTheme): void

  /** 应用启动时初始化主题；未传则读 localStorage('mf-theme') / 系统偏好 */
  export function initTheme(defaultTheme?: MfTheme): MfTheme

  /** 切换主题并持久化（localStorage 'mf-theme'），返回切换后的主题 */
  export function toggleTheme(): MfTheme

  /** 主题组合式 API */
  export function useTheme(): {
    theme: Readonly<import('vue').Ref<MfTheme>>
    applyTheme: typeof applyTheme
    initTheme: typeof initTheme
    toggleTheme: typeof toggleTheme
  }

  // ============================================================
  // tokens（对应设计包 src/tokens/index.ts）
  // ============================================================

  /** 主题调色板结构 */
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

  /** 浅色主题调色板 */
  export const lightPalette: ThemePalette
  /** 深色主题调色板 */
  export const darkPalette: ThemePalette
  /** 双主题调色板集合 */
  export const palettes: Record<MfTheme, ThemePalette>
  /** 圆角 token（两主题一致） */
  export const radius: { card: string; ctl: string; pill: string }
  /** 布局尺寸 token（两主题一致） */
  export const layout: { sidebarWidth: string; topbarHeight: string }
  /** 运行时读取当前主题下的 CSS 变量值（name 不含 --mf- 前缀） */
  export function getCssToken(name: string, fallback?: string): string

  // ============================================================
  // components
  // ============================================================

  /** 菜单项（结构与设计包 MfSidebar 源码对齐） */
  export interface MfMenuItem {
    /** 菜单项唯一 key */
    key: string
    /** 文案 */
    label: string
    /** 文本图标 / emoji */
    icon?: string
    /** 右侧胶囊徽标 */
    badge?: string | number
    /** 是否禁用 */
    disabled?: boolean
  }

  /** 菜单分组 */
  export interface MfMenuGroup {
    /** 分组标题 */
    title?: string
    /** 分组内菜单项 */
    items: MfMenuItem[]
  }

  /** 通用组件类型（props 宽松声明，运行时以设计包实现为准） */
  type MfComponent<P> = DefineComponent<P & Record<string, unknown>, Record<string, never>, unknown>

  /** 按钮（variant/size/pill/disabled/loading/block，click 事件） */
  export const MfButton: MfComponent<{
    variant?: 'primary' | 'soft' | 'outline' | 'ghost' | 'danger'
    size?: 'sm' | 'md' | 'lg'
    pill?: boolean
    disabled?: boolean
    loading?: boolean
    block?: boolean
  }>

  /** 卡片（padding/hoverable/flat，header/extra/footer 插槽） */
  export const MfCard: MfComponent<{
    padding?: string
    hoverable?: boolean
    flat?: boolean
  }>

  /** 侧栏（menu 必填/activeKey/title/collapsed/width，select 事件） */
  export const MfSidebar: MfComponent<{
    menu: MfMenuGroup[]
    activeKey?: string
    title?: string
    collapsed?: boolean
    width?: string
  }>

  /** 顶栏（height，left/right 插槽） */
  export const MfTopbar: MfComponent<{
    height?: string
  }>

  /** 布局壳（sidebar + topbar + content 一体） */
  export const MfShell: MfComponent<{
    menu: MfMenuGroup[]
    activeKey?: string
    title?: string
    collapsed?: boolean
  }>

  /** 语义标签（variant/size） */
  export const MfTag: MfComponent<{
    variant?: 'brand' | 'brand2' | 'ok' | 'warn' | 'danger' | 'info' | 'neutral'
    size?: 'sm' | 'md'
  }>
}
