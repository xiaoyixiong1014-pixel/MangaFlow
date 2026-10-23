import { createApp } from 'vue'
import App from './App.vue'
import { initTheme } from '../src'

// playground 默认按创作端主题（深色）初始化；支持 ?theme=light 预览浅色
const urlTheme = new URLSearchParams(location.search).get('theme')
initTheme(urlTheme === 'light' ? 'light' : 'dark')

createApp(App).mount('#app')
