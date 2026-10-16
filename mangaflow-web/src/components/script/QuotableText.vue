<!--
  剧本工作台 · 可引用文本容器（QuotableText，文件级注释）。

  「选中文本 → 添加对话中」链路的通用包装（参考 Trae CN 的选区交互）：
  - 包一层相对定位容器（slot 内放 el-input / textarea / 单行 input），
    容器级监听 select/mouseup/keyup/scroll（capture 冒泡自 slot 内任意
    input/textarea，无需侵入子组件）——读到非空选区（≥2 字符）时，把
    「添加对话中」按钮浮出在**选区末尾光标的正下方**（镜像层测量技术：
    复制 textarea 字体/内边距/宽度到隐藏 div，填入选区前文本并在末尾放
    标记 span，读标记坐标换算按钮位置）；
  - 点击按钮：经引用桥（inject，容器 ScriptView provide）把 {来源, 作用域,
    目标集, 选中文本} 入队到右侧 AI 助手面板的引用区，随后折叠选区并隐藏
    按钮（@mousedown.prevent 先阻止 textarea 失焦，保证按钮可点）；
  - blur（点击其他区域）自动隐藏按钮；按钮展示态带淡入动画。
-->
<script setup lang="ts">
import { inject, ref } from 'vue'
import { EMPTY_QUOTE_BRIDGE, SCRIPT_QUOTE_KEY } from './quoteBridge'
import type { QuoteScope } from '@/api/scriptPipeline'

/** 组件入参 */
const props = defineProps<{
  /** 来源展示标签（如「故事设定 · 创意内容」「剧本正文 · 第 1 集 · 文章体」） */
  source: string
  /** 引用作用域（quote-rewrite 接口语境标注用） */
  scope: QuoteScope
  /** 目标剧集 ID（大纲/正文作用域下携带，可选） */
  targetEpisodeId?: number | null
}>()

/** 引用桥（容器 provide；缺省空桥静默） */
const bridge = inject(SCRIPT_QUOTE_KEY, EMPTY_QUOTE_BRIDGE)

/** 容器引用（按钮定位的坐标系基准） */
const wrapRef = ref<HTMLElement | null>(null)

/** 浮动按钮可见性（容器内 input/textarea 存在非空选区时为 true） */
const quoteVisible = ref(false)
/** 当前选中的文本（strip 后；点击添加时入队） */
const selText = ref('')
/** 浮动按钮位置（px，相对容器；跟随选区末尾光标下方） */
const btnPos = ref({ left: 0, top: 0 })

/** 镜像层需要从 textarea 复制的样式清单（影响换行/行距/内边距的关键项） */
const MIRROR_STYLE_PROPS = [
  'fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'letterSpacing',
  'lineHeight', 'textIndent', 'textTransform', 'tabSize',
  'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
  'boxSizing', 'borderTopWidth', 'borderRightWidth', 'borderBottomWidth',
  'borderLeftWidth',
] as const

/**
 * 用镜像层测量选区末尾在 textarea 中的像素坐标（内部函数）。
 *
 * textarea 原生不提供选区坐标：创建一个隐藏 div，复制 textarea 的字体/
 * 行高/内边距/盒模型与内容宽度，填入 value 的 0~selectionEnd 部分并在末尾
 * 追加零宽标记 span——标记的 offsetLeft/offsetTop 即选区末尾坐标（减去
 * scrollTop 得到可视区坐标）。
 * @param el 目标输入元素（textarea / 单行 input）
 * @returns {left, top} 选区末尾相对 textarea 边框盒左上角的坐标（px）
 */
function measureSelectionEnd(
  el: HTMLTextAreaElement | HTMLInputElement,
): { left: number; top: number } {
  const end = el.selectionEnd ?? 0
  const mirror = document.createElement('div')
  const cs = window.getComputedStyle(el)
  for (const prop of MIRROR_STYLE_PROPS) {
    // camelCase 转 kebab-case 后经 CSSOM API 复制（避免索引签名类型问题）
    const kebab = prop.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)
    mirror.style.setProperty(kebab, cs.getPropertyValue(kebab))
  }
  // 换行口径与 textarea 一致（覆盖white-space/word-wrap 默认值）
  mirror.style.whiteSpace = 'pre-wrap'
  mirror.style.wordWrap = 'break-word'
  mirror.style.overflowWrap = 'break-word'
  // 与 textarea 内容盒同宽（clientWidth=content+padding，border 由上面复制）
  mirror.style.width = `${el.clientWidth}px`
  mirror.style.position = 'absolute'
  mirror.style.visibility = 'hidden'
  mirror.style.top = '0'
  mirror.style.left = '-9999px'
  // 选区前文本（末尾换行时补零宽字符确保标记落到新行）+ 末尾标记 span
  const before = el.value.slice(0, end)
  mirror.textContent = before.endsWith('\n') ? `${before}\u200b` : before
  const marker = document.createElement('span')
  marker.textContent = '\u200b'
  mirror.appendChild(marker)
  document.body.appendChild(mirror)
  try {
    // 单行 input 换行不生效，标记恒在首行——left 需按 marker 实际偏移
    return { left: marker.offsetLeft, top: marker.offsetTop + marker.offsetHeight }
  } finally {
    mirror.remove()
  }
}

/**
 * 容器级选区检测（select/mouseup/keyup/scroll capture 共用）：目标为
 * input/textarea 且选区 strip 后 ≥2 字符时，测量选区末尾坐标并把按钮
 * 定位到其正下方（相对容器，限制在容器可视范围内）。
 * 目标不是输入元素时（如点击浮动按钮本身触发的 mouseup）直接跳过——
 * 保留已捕获的选区文本，随后的 click 才能正常入队。
 * @param e DOM 事件（target 应为输入元素）
 * @returns 无
 */
function checkSelection(e: Event): void {
  const el = e.target as HTMLTextAreaElement | HTMLInputElement | null
  if (!el || !el.isConnected || (el.tagName !== 'TEXTAREA' && el.tagName !== 'INPUT')) {
    return
  }
  const text = el.value.slice(el.selectionStart ?? 0, el.selectionEnd ?? 0).trim()
  if (text.length < 2) {
    selText.value = ''
    quoteVisible.value = false
    return
  }
  selText.value = text
  // 换算按钮位置：textarea 边框盒相对容器 + 标记坐标 - 滚动偏移
  const wrapRect = wrapRef.value?.getBoundingClientRect()
  const elRect = el.getBoundingClientRect()
  if (!wrapRect) return
  const marker = measureSelectionEnd(el)
  const BUTTON_WIDTH = 96
  const BUTTON_HEIGHT = 26
  let left = elRect.left - wrapRect.left + marker.left - el.scrollLeft
  let top = elRect.top - wrapRect.top + marker.top - el.scrollTop + 4
  // 钳制在容器可视范围内（长文滚动到边缘时按钮不越界）
  const maxLeft = Math.max(0, wrapRect.width - BUTTON_WIDTH - 8)
  const maxTop = Math.max(0, wrapRect.height - BUTTON_HEIGHT - 4)
  left = Math.min(Math.max(8, left), maxLeft)
  top = Math.min(Math.max(0, top), maxTop)
  btnPos.value = { left, top }
  quoteVisible.value = true
}

/**
 * 隐藏按钮（blur 时调用；按钮自身 @mousedown.prevent 不会触发 textarea blur）。
 * @returns 无
 */
function hideButton(): void {
  quoteVisible.value = false
}

/**
 * 「添加对话中」：把选中文本经引用桥入队，随后折叠原选区（setSelectionRange
 * 折叠到结尾，避免按钮残留）并隐藏按钮。
 * @returns 无
 */
function addQuote(): void {
  if (!selText.value) return
  bridge.add(props.source, props.scope, selText.value, props.targetEpisodeId ?? null)
  selText.value = ''
  quoteVisible.value = false
  // 折叠 activeElement 的选区（点击按钮时 textarea 未失焦）
  const active = document.activeElement as HTMLInputElement | HTMLTextAreaElement | null
  if (active && (active.tagName === 'TEXTAREA' || active.tagName === 'INPUT')) {
    const end = active.value.length
    try { active.setSelectionRange(end, end) } catch { /* 非文本元素忽略 */ }
  }
}
</script>

<template>
  <div
    ref="wrapRef"
    class="quote-wrap"
    @select.capture="checkSelection"
    @mouseup.capture="checkSelection"
    @keyup.capture="checkSelection"
    @scroll.capture="checkSelection"
    @blur.capture="hideButton"
  >
    <slot />
    <transition name="quote-fade">
      <button
        v-if="quoteVisible"
        type="button"
        class="quote-btn"
        :style="{ left: `${btnPos.left}px`, top: `${btnPos.top}px` }"
        title="把选中的内容添加到右侧 AI 助手对话中"
        @mousedown.prevent
        @click="addQuote"
      >
        添加对话中
      </button>
    </transition>
  </div>
</template>

<style scoped>
/* 可引用文本容器：相对定位供浮动按钮锚定 */
.quote-wrap {
  position: relative;
  min-width: 0;
}
/* 「添加对话中」浮动按钮：跟随选区末尾光标下方（left/top 由测量结果内联） */
.quote-btn {
  position: absolute;
  z-index: 5;
  padding: 3px 12px;
  border: 1px solid var(--mf-brand, #36cfc9);
  border-radius: 999px;
  background: var(--mf-brand-soft, rgba(54, 207, 201, 0.12));
  color: var(--mf-brand, #36cfc9);
  font-size: 12px;
  line-height: 1.5;
  cursor: pointer;
  white-space: nowrap;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.25);
  transition: background var(--mf-dur, 0.15s) ease, transform var(--mf-dur, 0.15s) ease;
}
.quote-btn:hover {
  background: var(--mf-brand, #36cfc9);
  color: #fff;
  transform: translateY(-1px);
}
/* 按钮淡入/淡出 */
.quote-fade-enter-active,
.quote-fade-leave-active {
  transition: opacity 0.15s ease, transform 0.15s ease;
}
.quote-fade-enter-from,
.quote-fade-leave-to {
  opacity: 0;
  transform: translateY(4px);
}
</style>
