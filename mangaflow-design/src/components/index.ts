import type { App } from 'vue'
import MfButton from './MfButton.vue'
import MfCard from './MfCard.vue'
import MfLogo from './MfLogo.vue'
import MfShell from './MfShell.vue'
import MfSidebar, { type MfMenuGroup, type MfMenuItem } from './MfSidebar.vue'
import MfTag from './MfTag.vue'
import MfTopbar from './MfTopbar.vue'

export { MfButton, MfCard, MfLogo, MfShell, MfSidebar, MfTag, MfTopbar }
export type { MfMenuGroup, MfMenuItem }

export const components = { MfButton, MfCard, MfLogo, MfShell, MfSidebar, MfTag, MfTopbar }

/** 全量注册插件：app.use(MangaflowDesign) */
export default {
  install(app: App) {
    for (const [name, comp] of Object.entries(components)) {
      app.component(name, comp)
    }
  },
}
