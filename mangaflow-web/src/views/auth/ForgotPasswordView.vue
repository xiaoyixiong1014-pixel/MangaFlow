<!--
  找回密码页（文件级注释，独立页版 L-03，与 AuthDialog 找回面板同逻辑）。
  两步流程：①验证账号（用户名 + 邮箱，演示环境仅非空校验）
  → ②设置新密码（演示环境未接入验证码，需输入原密码通过后端校验后写入新密码）。
  成功后回登录页。
-->
<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { updatePassword } from '@/api/auth'

const router = useRouter()

/** 当前步骤（1 验证账号 → 2 设置新密码） */
const step = ref<1 | 2>(1)
/** 提交中标记 */
const loading = ref(false)
/** 第二步表单实例 */
const formRef = ref<FormInstance>()

/** 两步共享的表单数据（返回上一步不丢失输入） */
const form = reactive({
  /** 第一步：用户名 */
  username: '',
  /** 第一步：注册邮箱 */
  email: '',
  /** 第二步：原密码（演示环境后端仍校验旧密码） */
  oldPassword: '',
  /** 第二步：新密码 */
  newPassword: '',
  /** 第二步：确认新密码 */
  confirmPassword: '',
})

/** 第二步表单校验规则 */
const rules: FormRules = {
  oldPassword: [{ required: true, message: '请输入原密码完成身份校验', trigger: 'blur' }],
  newPassword: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
  confirmPassword: [
    {
      validator: (_rule, value: string, callback) => {
        if (value !== form.newPassword) callback(new Error('两次输入的密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

/**
 * 第一步：验证账号（演示环境仅做非空校验，不真正校验账号存在性）。
 */
function nextStep(): void {
  if (!form.username.trim()) {
    ElMessage.warning('请输入用户名')
    return
  }
  if (!form.email.trim()) {
    ElMessage.warning('请输入注册邮箱')
    return
  }
  step.value = 2
}

/**
 * 第二步提交：演示环境直接调 updatePassword 写入新密码
 * （后端校验旧密码，故需输入原密码）。成功后跳转登录页。
 */
async function submitReset(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await updatePassword(form.oldPassword, form.newPassword)
    ElMessage.success('密码重置成功，请使用新密码登录')
    router.push('/login')
  } catch {
    // 400 旧密码不正确等已由拦截器提示
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <h1 class="auth-title">找回密码</h1>
      <p class="auth-subtitle">两步完成：验证账号 → 设置新密码</p>

      <!-- 步骤指示 -->
      <el-steps :active="step - 1" align-center finish-status="success" class="steps">
        <el-step title="验证账号" />
        <el-step title="设置新密码" />
      </el-steps>

      <!-- 第一步：验证账号（本地非空校验，账号有效性由第二步原密码校验兜底） -->
      <el-form v-if="step === 1" label-position="top" size="large" @submit.prevent>
        <el-form-item label="用户名">
          <el-input v-model="form.username" placeholder="注册时的用户名" autocomplete="username" />
        </el-form-item>
        <el-form-item label="注册邮箱">
          <el-input v-model="form.email" placeholder="注册时绑定的邮箱" autocomplete="email" />
        </el-form-item>
        <el-button type="primary" class="submit-btn" size="large" @click="nextStep">
          下一步：设置新密码
        </el-button>
      </el-form>

      <!-- 第二步：设置新密码（需原密码完成身份校验，与后端 updatePassword 能力一致） -->
      <el-form v-else ref="formRef" :model="form" :rules="rules" label-position="top" size="large" @keyup.enter="submitReset">
        <el-form-item label="原密码" prop="oldPassword">
          <el-input v-model="form.oldPassword" type="password" placeholder="当前使用的密码" show-password />
        </el-form-item>
        <el-form-item label="新密码" prop="newPassword">
          <el-input v-model="form.newPassword" type="password" placeholder="至少 6 位" show-password autocomplete="new-password" />
        </el-form-item>
        <el-form-item label="确认新密码" prop="confirmPassword">
          <el-input v-model="form.confirmPassword" type="password" placeholder="再次输入新密码" show-password autocomplete="new-password" />
        </el-form-item>
        <el-button type="primary" class="submit-btn" size="large" :loading="loading" @click="submitReset">
          重置密码
        </el-button>
      </el-form>

      <div class="auth-links">
        <router-link to="/login">返回登录</router-link>
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
.steps {
  margin-bottom: var(--mf-sp-6);
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
