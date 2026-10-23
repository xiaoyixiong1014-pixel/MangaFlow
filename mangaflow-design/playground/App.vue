<script setup lang="ts">
import { ref } from 'vue'
import {
  MfButton,
  MfCard,
  MfShell,
  MfTag,
  palettes,
  useTheme,
  type MfMenuGroup,
} from '../src'

const { theme, toggleTheme } = useTheme()

/** 示例菜单 = UI 文档 §0.6.1 菜单定稿表（仅演示数据，包本身不耦合业务） */
const menu: MfMenuGroup[] = [
  { title: '创作', items: [{ key: 'agent', label: 'Agent', icon: '✦' }] },
  {
    title: '项目',
    items: [
      { key: 'projects', label: '漫剧项目', icon: '🎬' },
      { key: 'canvas', label: 'AI 画布', icon: '🎨' },
      { key: 'collab', label: '协作', icon: '👥', disabled: true },
    ],
  },
  {
    title: '资产',
    items: [
      { key: 'characters', label: '角色与场景', icon: '🧑‍🎤' },
      { key: 'media', label: '素材库', icon: '🖼' },
      { key: 'prompts', label: '提示词库', icon: '✍' },
    ],
  },
  {
    title: '账户',
    items: [
      { key: 'models', label: '模型配置', icon: '⚙' },
      { key: 'billing', label: '计费与配额', icon: '💰' },
      { key: 'profile', label: '个人中心', icon: '👤' },
    ],
  },
  { items: [{ key: 'help', label: '帮助', icon: '❓' }] },
]
const activeKey = ref('agent')

const swatches = palettes[theme.value]
</script>

<template>
  <MfShell :menu="menu" :active-key="activeKey" @select="(k) => (activeKey = k)">
    <template #topbar-left>
      <strong style="font-size: var(--mf-fs-md)">设计系统 Playground</strong>
    </template>
    <template #topbar-right>
      <MfButton variant="soft" size="sm" pill @click="toggleTheme()">
        {{ theme === 'dark' ? '☀ 浅色' : '☾ 深色' }}
      </MfButton>
    </template>

    <div style="max-width: 880px; margin: 0 auto; padding: var(--mf-sp-6); display: grid; gap: var(--mf-sp-6)">
      <MfCard header="色板（当前主题取自 palettes）">
        <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 10px">
          <div v-for="(v, k) in swatches" :key="k">
            <div :style="{ height: '44px', borderRadius: '10px', background: v, border: '1px solid var(--mf-border)' }" />
            <div style="font-size: var(--mf-fs-xs); color: var(--mf-text-2); margin-top: 4px">{{ k }} {{ v }}</div>
          </div>
        </div>
      </MfCard>

      <MfCard header="按钮 MfButton">
        <div style="display: flex; flex-wrap: wrap; gap: 10px; align-items: center">
          <MfButton>主要</MfButton>
          <MfButton variant="soft">柔和</MfButton>
          <MfButton variant="outline">描边</MfButton>
          <MfButton variant="ghost">幽灵</MfButton>
          <MfButton variant="danger">危险</MfButton>
          <MfButton pill>胶囊</MfButton>
          <MfButton loading>生成中</MfButton>
          <MfButton disabled>禁用</MfButton>
          <MfButton size="sm">小</MfButton>
          <MfButton size="lg">大</MfButton>
        </div>
      </MfCard>

      <MfCard header="标签 MfTag">
        <div style="display: flex; flex-wrap: wrap; gap: 8px">
          <MfTag variant="brand">墨青 · Studio</MfTag>
          <MfTag variant="brand2">藤紫 · Canvas</MfTag>
          <MfTag variant="ok">通过</MfTag>
          <MfTag variant="warn">疑似</MfTag>
          <MfTag variant="danger">违规</MfTag>
          <MfTag variant="info">提示</MfTag>
          <MfTag>中性</MfTag>
        </div>
      </MfCard>

      <MfCard header="圆角 token">
        <div style="display: flex; gap: 12px; flex-wrap: wrap">
          <div style="width: 96px; height: 64px; background: var(--mf-brand-soft); border: 1px solid var(--mf-brand); border-radius: var(--mf-r-card); display: flex; align-items: center; justify-content: center; font-size: var(--mf-fs-sm)">r-card 20</div>
          <div style="width: 96px; height: 64px; background: var(--mf-brand2-soft); border: 1px solid var(--mf-brand2); border-radius: var(--mf-r-ctl); display: flex; align-items: center; justify-content: center; font-size: var(--mf-fs-sm)">r-ctl 12</div>
          <div style="width: 96px; height: 64px; background: var(--mf-ok-soft); border: 1px solid var(--mf-ok); border-radius: var(--mf-r-pill); display: flex; align-items: center; justify-content: center; font-size: var(--mf-fs-sm)">r-pill</div>
        </div>
      </MfCard>
    </div>
  </MfShell>
</template>
