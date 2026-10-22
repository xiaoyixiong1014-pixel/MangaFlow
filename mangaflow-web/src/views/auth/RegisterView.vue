<!--
  注册页（文件级注释）。
  用户名/邮箱/密码表单 → auth store register（成功即登录）→ 跳转 /agent。
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

/** 表单实例 */
const formRef = ref<FormInstance>()
/** 提交中标记 */
const loading = ref(false)
/** 注册表单数据 */
const form = reactive({
  username: '',
  email: '',
  password: '',
  confirmPassword: '',
})

/** 表单校验规则 */
const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  email: [{ type: 'email', message: '邮箱格式不正确', trigger: 'blur' }],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
  confirmPassword: [
    {
      validator: (_rule, value: string, callback) => {
        if (value !== form.password) callback(new Error('两次输入的密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

/**
 * 提交注册：成功即建立会话并进入工作台（或回跳 redirect 地址）。
 */
async function submit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await auth.register({
      username: form.username,
      email: form.email || undefined,
      password: form.password,
    })
    ElMessage.success('注册成功，已赠送初始积分')
    // 支持回跳 redirect 参数（缺省进工作台）
    const redirect = route.query.redirect as string | undefined
    router.push(redirect || '/agent')
  } catch {
    // 业务错误已由拦截器提示（409 用户名/邮箱被占用等）
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <h1 class="auth-title">注册</h1>
      <p class="auth-subtitle">新用户注册即赠 5000 积分</p>

      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" size="large" @keyup.enter="submit">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="用户名" autocomplete="username" />
        </el-form-item>
        <el-form-item label="邮箱（选填）" prop="email">
          <el-input v-model="form.email" placeholder="邮箱" autocomplete="email" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" placeholder="至少 6 位" show-password autocomplete="new-password" />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirmPassword">
          <el-input v-model="form.confirmPassword" type="password" placeholder="再次输入密码" show-password autocomplete="new-password" />
        </el-form-item>
        <el-button type="primary" class="submit-btn" :loading="loading" @click="submit">
          注 册
        </el-button>
      </el-form>

      <div class="auth-links">
        <router-link to="/login">已有账号？去登录</router-link>
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
  margin-top: var(--mf-sp-4);
  font-size: var(--mf-fs-sm);
  text-align: center;
}
.auth-links a {
  color: var(--mf-brand-strong);
  text-decoration: none;
}
.auth-links a:hover {
  text-decoration: underline;
}
</style>
