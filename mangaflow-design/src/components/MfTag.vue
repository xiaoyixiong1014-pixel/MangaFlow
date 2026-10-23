<script setup lang="ts">
import { computed } from 'vue'

interface Props {
  /** 语义变体（soft 底 + 语义色文字，胶囊形） */
  variant?: 'brand' | 'brand2' | 'ok' | 'warn' | 'danger' | 'info' | 'neutral'
  size?: 'sm' | 'md'
}

const props = withDefaults(defineProps<Props>(), { variant: 'neutral', size: 'md' })

const cls = computed(() => ['mf-tag', `mf-tag--${props.variant}`, `mf-tag--${props.size}`])
</script>

<template>
  <span :class="cls"><slot /></span>
</template>

<style scoped>
.mf-tag {
  display: inline-flex;
  align-items: center;
  gap: var(--mf-sp-1);
  border-radius: var(--mf-r-pill);
  font-weight: var(--mf-fw-medium);
  line-height: 1;
  white-space: nowrap;
}
.mf-tag--sm { height: 20px; padding: 0 8px; font-size: var(--mf-fs-xs); }
.mf-tag--md { height: 24px; padding: 0 10px; font-size: var(--mf-fs-sm); }

.mf-tag--brand   { background: var(--mf-brand-soft);   color: var(--mf-brand-strong); }
.mf-tag--brand2  { background: var(--mf-brand2-soft);  color: var(--mf-brand2); }
.mf-tag--ok      { background: var(--mf-ok-soft);      color: var(--mf-ok); }
.mf-tag--warn    { background: var(--mf-warn-soft);    color: var(--mf-warn); }
.mf-tag--danger  { background: var(--mf-danger-soft);  color: var(--mf-danger); }
.mf-tag--info    { background: var(--mf-info-soft);    color: var(--mf-info); }
.mf-tag--neutral { background: var(--mf-surface-2);    color: var(--mf-text-2); }
[data-theme='dark'] .mf-tag--neutral {
  background: var(--mf-surface-2);
  color: var(--mf-text-2);
}
</style>
