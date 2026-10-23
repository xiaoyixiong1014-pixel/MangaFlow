<!--
  MfLogo（文件级注释）：漫序 MangaFlow 品牌标志。
  图形：∞ 胶片环一笔绘成，墨青 → 藤紫双色渐变 stroke，
  左右两枚引擎圆点（Studio 墨青 / Canvas 藤紫），
  语义为双引擎汇入「漫无止境」。
  矢量自绘（viewBox 24×24），在 16~64px 均保持清晰，
  自带渐变与色值，不随主题切换（品牌标固定色）。
-->
<script setup lang="ts">
import { useId } from 'vue'

interface Props {
  /** 渲染尺寸（px，宽高一致） */
  size?: number
  /** 可访问名称（纯装饰场景传空串并以 aria-hidden 渲染） */
  label?: string
}

const props = withDefaults(defineProps<Props>(), {
  size: 22,
  label: '漫序 MangaFlow',
})

/** 渐变 id：useId 保证同页多实例不冲突 */
const gradientId = `mf-logo-g-${useId()}`
</script>

<template>
  <svg
    :width="props.size"
    :height="props.size"
    viewBox="0 0 24 24"
    :role="props.label ? 'img' : undefined"
    :aria-label="props.label || undefined"
    :aria-hidden="props.label ? undefined : 'true'"
  >
    <defs>
      <linearGradient :id="gradientId" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0" stop-color="#0B7C73" />
        <stop offset="0.55" stop-color="#12B3A6" />
        <stop offset="1" stop-color="#7C6BF0" />
      </linearGradient>
    </defs>
    <!-- ∞ 环：双环相切一笔绘成 -->
    <path
      d="M12 12 C10 8.8 8 7.2 5.8 7.2 C3.4 7.2 2 9.2 2 12 C2 14.8 3.4 16.8 5.8 16.8 C8 16.8 10 15.2 12 12 C14 8.8 16 7.2 18.2 7.2 C20.6 7.2 22 9.2 22 12 C22 14.8 20.6 16.8 18.2 16.8 C16 16.8 14 15.2 12 12 Z"
      fill="none"
      :stroke="`url(#${gradientId})`"
      stroke-width="2.6"
      stroke-linecap="round"
    />
    <!-- 双引擎圆点 -->
    <circle cx="5.8" cy="12" r="1.7" fill="#12B3A6" />
    <circle cx="18.2" cy="12" r="1.7" fill="#7C6BF0" />
  </svg>
</template>
