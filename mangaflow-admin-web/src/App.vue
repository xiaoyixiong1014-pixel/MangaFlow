<script setup lang="ts">
/**
 * 文件级注释：应用根组件。
 * 管理端固定浅色主题（长期读表格与报表场景，见 UI 原型 V2.1 第 271 行），
 * 由路由出口渲染 AdminLayout / LoginView。
 */
</script>

<template>
  <router-view />
</template>

<style>
/* 全局兜底：占满视口高度，保证布局滚动区域正确 */
html,
body,
#app {
  height: 100%;
  margin: 0;
}
</style>
