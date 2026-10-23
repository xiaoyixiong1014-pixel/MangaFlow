/**
 * @mangaflow/design — 漫序 MangaFlow 共享设计系统
 *
 * 面向 mangaflow-web（官网 + 创作端）与 mangaflow-admin-web（运营管理端），
 * 输出：设计 token（CSS 变量 + JS 调色板）、主题切换、基础组件。
 * 包内不耦合业务（RD-58 / 架构文档 §3.1.4）。
 *
 * 样式引入（应用入口 main.ts）：
 *   import '@mangaflow/design/styles.css'            // 必引：token + base
 *   import '@mangaflow/design/element-plus.css'      // 可选：EP 主题桥接
 *   import '@mangaflow/design/compat.css'            // 可选：原型旧变量名兼容
 */

// 样式（sideEffects 声明于 package.json，打包时保留）
import './styles/index.css'

// token（JS 侧程序化取色）
export * from './tokens'

// 主题
export * from './theme'

// 画布插件共享类型（CV-20/21/22，前后端字段对齐）
// 显式 export type 命名 re-export：dts 产物保留 type 修饰，避免生成的
// 值形态 import 在 TS6 下把纯类型模块绑定解析为 error（表现为成员不可见）
export type {
  PluginPortType,
  PluginPortDef,
  PluginManifest,
  PluginInvokeResult,
} from './plugins'

// 基础组件（含 MfMenuGroup/MfMenuItem 类型与 components 集合）+ install 插件（default）
export { default } from './components'
export * from './components'
