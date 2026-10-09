<!--
  404 页面不存在（文件级注释）。
  居中大状态码 + 文案 + 返回按钮。
-->
<script setup lang="ts">
import { useRouter } from 'vue-router'

const router = useRouter()

/**
 * 返回上一页：无历史时回首页。
 */
function goBack(): void {
  if (window.history.length > 1) router.back()
  else router.push('/')
}
</script>

<template>
  <div class="error-page">
    <div class="error-card">
      <div class="error-code" aria-hidden="true">404</div>
      <div class="error-icon" aria-hidden="true">🧭</div>
      <h1 class="error-title">页面不存在</h1>
      <p class="error-desc">
        抱歉，你访问的页面不存在或已被移除。<br />
        请检查地址是否正确，或从首页重新出发。
      </p>
      <div class="error-actions">
        <el-button type="primary" size="large" @click="router.push('/')">返回首页</el-button>
        <el-button size="large" @click="goBack">返回上一页</el-button>
      </div>
      <p class="error-foot">漫序 MangaFlow · 错误码 404</p>
    </div>
  </div>
</template>

<style scoped>
.error-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--mf-sp-8);
  background:
    radial-gradient(1200px 600px at 20% -10%, var(--mf-brand-soft), transparent 60%),
    radial-gradient(900px 500px at 110% 110%, var(--mf-brand2-soft), transparent 60%),
    var(--mf-bg);
  color: var(--mf-text);
}
.error-card {
  width: min(560px, 94vw);
  text-align: center;
  padding: var(--mf-sp-10) var(--mf-sp-8);
  border-radius: var(--mf-r-card);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  box-shadow: var(--mf-shadow-pop);
  position: relative;
  overflow: hidden;
}
.error-code {
  position: absolute;
  top: -28px;
  left: 50%;
  transform: translateX(-50%);
  font-size: 150px;
  font-weight: var(--mf-fw-bold);
  letter-spacing: 8px;
  color: var(--mf-info);
  opacity: 0.08;
  user-select: none;
  pointer-events: none;
}
.error-icon {
  font-size: 52px;
  line-height: 1;
}
.error-title {
  margin: var(--mf-sp-4) 0 var(--mf-sp-2);
  font-size: var(--mf-fs-2xl);
  font-weight: var(--mf-fw-bold);
}
.error-desc {
  margin: 0 0 var(--mf-sp-6);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-md);
  line-height: 1.8;
}
.error-actions {
  display: flex;
  justify-content: center;
  gap: var(--mf-sp-3);
}
.error-foot {
  margin: var(--mf-sp-7) 0 0;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
</style>
