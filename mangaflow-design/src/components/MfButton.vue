<script setup lang="ts">
import { computed } from 'vue'

interface Props {
  /** 视觉变体 */
  variant?: 'primary' | 'soft' | 'outline' | 'ghost' | 'danger'
  size?: 'sm' | 'md' | 'lg'
  /** 胶囊形（默认为 r-ctl 12px 圆角） */
  pill?: boolean
  disabled?: boolean
  loading?: boolean
  block?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  variant: 'primary',
  size: 'md',
  pill: false,
  disabled: false,
  loading: false,
  block: false,
})

const emit = defineEmits<{ click: [e: MouseEvent] }>()

const cls = computed(() => [
  'mf-btn',
  `mf-btn--${props.variant}`,
  `mf-btn--${props.size}`,
  { 'mf-btn--pill': props.pill, 'mf-btn--block': props.block },
])

function onClick(e: MouseEvent) {
  if (props.disabled || props.loading) return
  emit('click', e)
}
</script>

<template>
  <button type="button" :class="cls" :disabled="disabled || loading" @click="onClick">
    <span v-if="loading" class="mf-btn__spin" aria-hidden="true" />
    <slot />
  </button>
</template>

<style scoped>
.mf-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--mf-sp-2);
  border: 1px solid transparent;
  border-radius: var(--mf-r-ctl);
  font-family: var(--mf-font);
  font-weight: var(--mf-fw-medium);
  line-height: 1;
  cursor: pointer;
  white-space: nowrap;
  transition: background var(--mf-dur) var(--mf-ease),
    color var(--mf-dur) var(--mf-ease), border-color var(--mf-dur) var(--mf-ease),
    box-shadow var(--mf-dur) var(--mf-ease), transform var(--mf-dur) var(--mf-ease);
}
.mf-btn:active:not(:disabled) {
  transform: translateY(1px);
}
.mf-btn:disabled {
  opacity: .5;
  cursor: not-allowed;
}
.mf-btn--pill { border-radius: var(--mf-r-pill); }
.mf-btn--block { display: flex; width: 100%; }

/* 尺寸 */
.mf-btn--sm { height: 28px; padding: 0 var(--mf-sp-3); font-size: var(--mf-fs-sm); }
.mf-btn--md { height: 34px; padding: 0 var(--mf-sp-4); font-size: var(--mf-fs-md); }
.mf-btn--lg { height: 42px; padding: 0 var(--mf-sp-6); font-size: var(--mf-fs-base); }

/* 变体 */
.mf-btn--primary {
  background: var(--mf-brand);
  color: var(--mf-brand-contrast);
}
.mf-btn--primary:hover:not(:disabled) {
  background: color-mix(in srgb, var(--mf-brand) 85%, var(--mf-brand-contrast));
}
.mf-btn--soft {
  background: var(--mf-brand-soft);
  color: var(--mf-brand-strong);
}
.mf-btn--soft:hover:not(:disabled) {
  background: color-mix(in srgb, var(--mf-brand-soft) 100%, transparent 0%);
  filter: brightness(1.05);
}
.mf-btn--outline {
  background: transparent;
  border-color: var(--mf-border-strong);
  color: var(--mf-text);
}
.mf-btn--outline:hover:not(:disabled) {
  border-color: var(--mf-brand);
  color: var(--mf-brand);
}
.mf-btn--ghost {
  background: transparent;
  color: var(--mf-text-2);
}
.mf-btn--ghost:hover:not(:disabled) {
  background: var(--mf-surface-2);
  color: var(--mf-text);
}
.mf-btn--danger {
  background: var(--mf-danger);
  color: #fff;
}
.mf-btn--danger:hover:not(:disabled) {
  background: color-mix(in srgb, var(--mf-danger) 85%, white);
}

/* 加载指示 */
.mf-btn__spin {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  border: 2px solid currentColor;
  border-top-color: transparent;
  animation: mf-spin .7s linear infinite;
}
@keyframes mf-spin {
  to { transform: rotate(360deg); }
}
</style>
