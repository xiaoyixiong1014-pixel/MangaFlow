<!--
  登录页（文件级注释）。
  用户名/密码表单 → auth store login → 跳转 redirect 参数或 /agent；
  错误经 api 拦截器统一 ElMessage 提示 detail。
-->
<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

/** 表单实例（用于触发校验） */
const formRef = ref<FormInstance>()
/** 提交中标记（防重复点击） */
const loading = ref(false)
/** 登录表单数据 */
const form = reactive({
  username: '',
  password: '',
})

/** 表单校验规则 */
const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名或邮箱', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

/**
 * 提交登录：校验通过后调用 store，成功后跳转回跳地址或工作台。
 * 官网带入的创作意图（pendingIntent）保持暂存，由 W-01 意图承接条展示。
 */
async function submit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    ElMessage.success('登录成功')
    // 优先回跳 redirect 参数（如从路由守卫带入），缺省进工作台
    const redirect = route.query.redirect as string | undefined
    router.push(redirect || '/agent')
  } catch {
    // 业务错误已由拦截器 ElMessage 提示 detail
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <div class="brand">
        <span class="brand-logo" aria-hidden="true" />
        <span class="brand-name">漫序 MangaFlow</span>
      </div>
      <h1 class="auth-title">登录</h1>
      <p class="auth-subtitle">AI 漫剧一站式生产平台</p>

      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" size="large" @keyup.enter="submit">
        <el-form-item label="用户名 / 邮箱" prop="username">
          <el-input v-model="form.username" placeholder="用户名或邮箱" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" placeholder="密码" show-password autocomplete="current-password" />
        </el-form-item>
        <el-button type="primary" class="submit-btn" :loading="loading" @click="submit">
          登 录
        </el-button>
      </el-form>

      <div class="auth-links">
        <router-link to="/forgot-password">忘记密码？</router-link>
        <router-link to="/register">没有账号？立即注册</router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.auth-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: var(--mf-bg);
  color: var(--mf-text);
}
.auth-card {
  width: 400px;
  padding: var(--mf-sp-9);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-card);
  box-shadow: var(--mf-shadow);
}
.brand {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-6);
}
.brand-logo {
  width: 22px;
  height: 22px;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--mf-brand), var(--mf-brand2));
}
.brand-name {
  font-weight: var(--mf-fw-bold);
  color: var(--mf-text);
}
.auth-title {
  margin: 0 0 var(--mf-sp-1);
  font-size: var(--mf-fs-2xl);
  font-weight: var(--mf-fw-bold);
}
.auth-subtitle {
  margin: 0 0 var(--mf-sp-6);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-md);
}
.submit-btn {
  width: 100%;
  margin-top: var(--mf-sp-2);
}
.auth-links {
  display: flex;
  justify-content: space-between;
  margin-top: var(--mf-sp-4);
  font-size: var(--mf-fs-sm);
}
.auth-links a {
  color: var(--mf-brand);
  text-decoration: none;
}
.auth-links a:hover {
  text-decoration: underline;
}
</style>
