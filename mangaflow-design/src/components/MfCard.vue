<script setup lang="ts">
import { computed } from 'vue'

interface Props {
  /** 内边距（任意 CSS 值，默认 20px） */
  padding?: string
  /** 悬浮上浮 + 阴影 */
  hoverable?: boolean
  /** 无边框（用于叠色场景） */
  flat?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  padding: 'var(--mf-sp-5)',
  hoverable: false,
  flat: false,
})

const style = computed(() => ({ padding: props.padding }))
const cls = computed(() => [
  'mf-card',
  { 'mf-card--hoverable': props.hoverable, 'mf-card--flat': props.flat },
])
</script>

<template>
  <section :class="cls" :style="style">
    <header v-if="$slots.header || $slots.extra" class="mf-card__header">
      <div class="mf-card__title"><slot name="header" /></div>
      <div class="mf-card__extra"><slot name="extra" /></div>
    </header>
    <div class="mf-card__body"><slot /></div>
    <footer v-if="$slots.footer" class="mf-card__footer"><slot name="footer" /></footer>
  </section>
</template>

<style scoped>
.mf-card {
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-card);
  transition: box-shadow var(--mf-dur) var(--mf-ease),
    transform var(--mf-dur) var(--mf-ease), border-color var(--mf-dur) var(--mf-ease);
}
.mf-card--flat { border-color: transparent; }
.mf-card--hoverable:hover {
  box-shadow: var(--mf-shadow-pop);
  transform: translateY(-2px);
  border-color: var(--mf-border-strong);
}

.mf-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-3);
  margin-bottom: var(--mf-sp-3);
}
.mf-card__title {
  font-size: var(--mf-fs-lg);
  font-weight: var(--mf-fw-semibold);
  color: var(--mf-text);
}
.mf-card__extra {
  font-size: var(--mf-fs-sm);
  color: var(--mf-text-3);
}
.mf-card__footer {
  margin-top: var(--mf-sp-4);
  padding-top: var(--mf-sp-4);
  border-top: 1px solid var(--mf-border);
}
</style>
