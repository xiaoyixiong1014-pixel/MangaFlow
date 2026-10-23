<script lang="ts">
/**
 * 顶栏 Props（显式导出，保证包出口 d.ts 中类型可命名，
 * 避免 TS4023 阻断 components/index.d.ts 生成）。
 */
export interface Props {
  /** 顶栏高度（默认 62px，与原型一致） */
  height?: string
}
</script>

<script setup lang="ts">
withDefaults(defineProps<Props>(), { height: 'var(--mf-topbar-h)' })
</script>

<template>
  <header class="mf-topbar" :style="{ height }">
    <div class="mf-topbar__left"><slot name="left" /></div>
    <div class="mf-topbar__center"><slot /></div>
    <div class="mf-topbar__right"><slot name="right" /></div>
  </header>
</template>

<style scoped>
.mf-topbar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-4);
  flex-shrink: 0;
  padding: 0 var(--mf-sp-5);
  /* 玻璃底：环境光斑透出模糊可见，与内容区视觉连续 */
  background: var(--mf-topbar-bg, var(--mf-surface));
  backdrop-filter: blur(14px) saturate(140%);
  -webkit-backdrop-filter: blur(14px) saturate(140%);
  border-bottom: 1px solid var(--mf-border);
  z-index: var(--mf-z-sticky);
}
.mf-topbar__left,
.mf-topbar__right {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  flex-shrink: 0;
}
.mf-topbar__center {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--mf-sp-2);
}
</style>
