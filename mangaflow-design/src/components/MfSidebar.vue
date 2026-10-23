<script setup lang="ts">
import { computed, type Component } from 'vue'
import MfLogo from './MfLogo.vue'

/** 菜单项（通用结构，不耦合业务——内容由应用传入） */
export interface MfMenuItem {
  key: string
  label: string
  /** 图标：字符串（文本/emoji 兼容旧用法）或图标组件（推荐，渲染一致） */
  icon?: string | Component
  /** 右侧胶囊徽标（数字或文案） */
  badge?: string | number
  /** 禁用（如 V-03/04 协作本期不做） */
  disabled?: boolean
}

/** 菜单分组 */
export interface MfMenuGroup {
  title?: string
  items: MfMenuItem[]
}

interface Props {
  menu: MfMenuGroup[]
  activeKey?: string
  /** 顶部品牌区文案（可用 #brand 插槽替换） */
  title?: string
  collapsed?: boolean
  width?: string
}

const props = withDefaults(defineProps<Props>(), {
  activeKey: '',
  title: '漫序 MangaFlow',
  collapsed: false,
  width: 'var(--mf-sidebar-w)',
})

const emit = defineEmits<{ select: [key: string, item: MfMenuItem] }>()

const style = computed(() => ({ width: props.collapsed ? '68px' : props.width }))
</script>

<template>
  <aside class="mf-sidebar" :style="style" :class="{ 'mf-sidebar--collapsed': collapsed }">
    <div class="mf-sidebar__brand">
      <slot name="brand">
        <MfLogo :size="22" label="" />
        <span v-if="!collapsed" class="mf-sidebar__title">{{ title }}</span>
      </slot>
    </div>

    <nav class="mf-sidebar__nav" aria-label="主导航">
      <div v-for="(g, gi) in menu" :key="gi" class="mf-sidebar__group">
        <div v-if="g.title && !collapsed" class="mf-sidebar__group-title">{{ g.title }}</div>
        <button
          v-for="item in g.items"
          :key="item.key"
          type="button"
          class="mf-sidebar__item"
          :class="{
            'mf-sidebar__item--active': item.key === activeKey,
            'mf-sidebar__item--disabled': item.disabled,
          }"
          :disabled="item.disabled"
          :title="collapsed ? item.label : undefined"
          :aria-current="item.key === activeKey ? 'page' : undefined"
          @click="!item.disabled && emit('select', item.key, item)"
        >
          <span v-if="item.icon" class="mf-sidebar__icon" aria-hidden="true">
            <component :is="item.icon" v-if="typeof item.icon !== 'string'" class="mf-sidebar__svg" />
            <template v-else>{{ item.icon }}</template>
          </span>
          <span v-if="!collapsed" class="mf-sidebar__label">{{ item.label }}</span>
          <span
            v-if="item.badge !== undefined && !collapsed"
            class="mf-sidebar__badge"
          >{{ item.badge }}</span>
        </button>
      </div>
    </nav>

    <div v-if="$slots.footer" class="mf-sidebar__footer">
      <slot name="footer" />
    </div>
  </aside>
</template>

<style scoped>
.mf-sidebar {
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  height: 100vh;
  background: var(--mf-surface-3);
  border-right: 1px solid var(--mf-border);
  overflow: hidden;
  transition: width var(--mf-dur) var(--mf-ease);
}
.mf-sidebar--collapsed { width: 68px; }

.mf-sidebar__brand {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  height: var(--mf-topbar-h);
  padding: 0 var(--mf-sp-5);
  border-bottom: 1px solid var(--mf-border);
  flex-shrink: 0;
}
/* MfLogo 矢量标：不随容器收缩 */
.mf-sidebar__brand > :first-child {
  flex-shrink: 0;
}
.mf-sidebar__title {
  font-size: var(--mf-fs-md);
  font-weight: var(--mf-fw-bold);
  color: var(--mf-text);
  white-space: nowrap;
}

.mf-sidebar__nav {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--mf-sp-3) var(--mf-sp-3) var(--mf-sp-4);
}
.mf-sidebar__group + .mf-sidebar__group { margin-top: var(--mf-sp-4); }
.mf-sidebar__group-title {
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
  padding: 0 var(--mf-sp-2) var(--mf-sp-2);
  letter-spacing: .05em;
}

.mf-sidebar__item {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  width: 100%;
  height: 38px;
  padding: 0 var(--mf-sp-3);
  border: none;
  border-radius: var(--mf-r-ctl);
  background: transparent;
  color: var(--mf-text-2);
  font-family: var(--mf-font);
  font-size: var(--mf-fs-md);
  cursor: pointer;
  text-align: left;
  transition: background var(--mf-dur) var(--mf-ease), color var(--mf-dur) var(--mf-ease);
}
.mf-sidebar__item:hover:not(:disabled):not(.mf-sidebar__item--active) {
  background: var(--mf-surface-2);
  color: var(--mf-text);
}
.mf-sidebar__item--active {
  background: var(--mf-brand-soft);
  color: var(--mf-brand-strong);
  font-weight: var(--mf-fw-semibold);
}
.mf-sidebar__item--disabled {
  opacity: .45;
  cursor: not-allowed;
}

.mf-sidebar__icon {
  width: 20px;
  text-align: center;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
/* 图标组件统一 16px 网格（继承 currentColor，随选中/hover 态变色） */
.mf-sidebar__icon :deep(svg),
.mf-sidebar__svg {
  width: 16px;
  height: 16px;
}
.mf-sidebar__label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mf-sidebar__badge {
  flex-shrink: 0;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: var(--mf-r-pill);
  background: var(--mf-danger-soft);
  color: var(--mf-danger);
  font-size: var(--mf-fs-xs);
  font-weight: var(--mf-fw-semibold);
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.mf-sidebar__footer {
  flex-shrink: 0;
  padding: var(--mf-sp-3);
  border-top: 1px solid var(--mf-border);
}
</style>
