/**
 * 应用入口（文件级注释）。
 *
 * 初始化顺序：Pinia → Element Plus（zh-cn 语言包）→ 设计包样式
 * （styles.css token + element-plus.css 主题桥接）→ 全局样式 → 主题初始化
 * （创作端默认深色，<html data-theme>）→ Router → 挂载。
 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'

import 'element-plus/dist/index.css'
import '@mangaflow/design/styles.css'
import '@mangaflow/design/element-plus.css'
import './styles/app.css'

import App from './App.vue'
import router from './router'
import { useUiStore } from './stores/ui'

const app = createApp(App)

// Pinia 先于 Router 安装（路由守卫与组件内均依赖 store）
app.use(createPinia())

// 主题初始化：读取持久化主题（默认 dark）并应用到 <html data-theme>
useUiStore().initTheme()

// Element Plus：中文语言包全量注册
app.use(ElementPlus, { locale: zhCn })

app.use(router)

app.mount('#app')
