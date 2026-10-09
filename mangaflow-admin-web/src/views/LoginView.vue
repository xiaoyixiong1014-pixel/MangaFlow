<script setup lang="ts">
/**
 * 文件级注释：A-01 管理端登录页。
 * 账号 + 密码 + 登录按钮；登录成功后校验角色，非 operator 提示
 * 「该账号无管理权限」并登出；接口错误展示后端 detail 文案。
 */
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { login } from '@/api/admin'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

/** 登录表单数据 */
const form = reactive({
  username: '',
  password: '',
})

/** 表单校验规则 */
const rules: FormRules = {
  username: [{ required: true, message: '请输入管理员账号', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

/** 提交 loading 状态 */
const loading = ref(false)
/** 表单引用 */
const formRef = ref<FormInstance>()

/**
 * 提交登录：校验通过后调接口；非 operator 角色提示并清空会话。
 * @returns 无
 * @throws 无（接口错误由拦截器统一提示）
 */
async function handleLogin() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    const result = await login({ username: form.username, password: form.password })
    if (result.user?.role !== 'operator') {
      ElMessage.error('该账号无管理权限')
      return
    }
    auth.setSession(result.access_token, result.user)
    ElMessage.success('登录成功')
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/dashboard'
    router.push(redirect)
  } catch {
    /* 错误 detail 已由 client 拦截器 ElMessage.error 展示 */
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-brand">
        <span class="login-logo" aria-hidden="true" />
        <h1 class="login-title">漫序 MangaFlow</h1>
        <p class="login-subtitle">运营管理后台</p>
      </div>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        size="large"
        @keyup.enter="handleLogin"
      >
        <el-form-item label="管理员账号" prop="username">
          <el-input v-model="form.username" placeholder="请输入账号" autofocus />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            show-password
          />
        </el-form-item>
        <el-button
          type="primary"
          class="login-submit"
          :loading="loading"
          data-test="login-submit"
          @click="handleLogin"
        >
          登 录
        </el-button>
      </el-form>

      <p class="login-tip">与用户端入口隔离，仅运营管理员（operator）可登录</p>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  background:
    radial-gradient(1200px 600px at 20% -10%, var(--mf-brand-soft), transparent 60%),
    radial-gradient(1000px 500px at 90% 110%, var(--mf-brand2-soft), transparent 60%),
    var(--mf-bg);
}

.login-card {
  width: 400px;
  padding: var(--mf-sp-8);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-card);
  box-shadow: var(--mf-shadow-pop);
}

.login-brand {
  text-align: center;
  margin-bottom: var(--mf-sp-6);
}

.login-logo {
  display: inline-block;
  width: 40px;
  height: 40px;
  border-radius: 12px;
  background: linear-gradient(135deg, var(--mf-brand), var(--mf-brand2));
  margin-bottom: var(--mf-sp-3);
}

.login-title {
  margin: 0;
  font-size: var(--mf-fs-xl);
  font-weight: var(--mf-fw-bold);
  color: var(--mf-text);
}

.login-subtitle {
  margin: var(--mf-sp-1) 0 0;
  font-size: var(--mf-fs-sm);
  color: var(--mf-text-3);
}

.login-submit {
  width: 100%;
  margin-top: var(--mf-sp-2);
}

.login-tip {
  margin: var(--mf-sp-4) 0 0;
  text-align: center;
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}
</style>
