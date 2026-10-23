<script setup lang="ts">
import MfSidebar, { type MfMenuGroup, type MfMenuItem } from './MfSidebar.vue'
import MfTopbar from './MfTopbar.vue'

interface Props {
  menu: MfMenuGroup[]
  activeKey?: string
  title?: string
  collapsed?: boolean
}

withDefaults(defineProps<Props>(), { activeKey: '', title: '漫序 MangaFlow', collapsed: false })

const emit = defineEmits<{ select: [key: string, item: MfMenuItem] }>()
</script>

<template>
  <div class="mf-shell">
    <MfSidebar
      :menu="menu"
      :active-key="activeKey"
      :title="title"
      :collapsed="collapsed"
      @select="(k, i) => emit('select', k, i)"
    >
      <template #brand><slot name="brand" /></template>
      <template #footer><slot name="sidebar-footer" /></template>
    </MfSidebar>

    <div class="mf-shell__main">
      <MfTopbar>
        <template #left><slot name="topbar-left" /></template>
        <slot name="topbar" />
        <template #right><slot name="topbar-right" /></template>
      </MfTopbar>
      <main class="mf-shell__content">
        <slot />
      </main>
    </div>
  </div>
</template>

<style scoped>
.mf-shell {
  display: flex;
  height: 100vh;
  overflow: hidden;
  background: var(--mf-bg);
  color: var(--mf-text);
}
.mf-shell__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.mf-shell__content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
</style>
