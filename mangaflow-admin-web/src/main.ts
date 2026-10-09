/**
 * 文件级注释：管理端应用入口。
 * 装配 Pinia / Vue Router / Element Plus，并引入 @mangaflow/design
 * 的设计 token 与 Element Plus 桥接样式（浅色主题，RD-58 设计单一事实源）。
 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'

// 设计系统：token + 基础样式 + Element Plus 主题桥接（管理端固定浅色）
import '@mangaflow/design/styles.css'
import '@mangaflow/design/element-plus.css'

import App from './App.vue'
import router from './router'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus)

app.mount('#app')
