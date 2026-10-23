// 将可选桥接 CSS 复制进 dist（vite lib 模式会把已 import 的样式打进主 css，
// bridge / compat 作为按需引入的独立文件，直接从源码复制）
import { copyFileSync, mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const dist = join(root, 'dist')
mkdirSync(dist, { recursive: true })

copyFileSync(join(root, 'src/styles/element-plus-bridge.css'), join(dist, 'element-plus-bridge.css'))
copyFileSync(join(root, 'src/styles/compat.css'), join(dist, 'compat.css'))

console.log('[mangaflow-design] bridge & compat css copied to dist/')
