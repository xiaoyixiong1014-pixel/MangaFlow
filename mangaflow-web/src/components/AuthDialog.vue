<!--
  登录 / 注册 / 找回密码弹窗（文件级注释，对应原型 L-02 / L-03）。
  全局复用：官网首页（L-01）主转化链在未登录时弹出本组件；
  面板结构 = 登录（验证码 Tab 占位 + 密码）/ 注册 / 找回密码（两步）。
  面板与 Tab 切换不丢失已输入账号（表单数据集中于同一 reactive 对象）。
  登录 / 注册成功后 emit('success')，由宿主页决定意图承接与跳转。
-->
<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { MfLogo } from '@mangaflow/design'
import { useAuthStore } from '@/stores/auth'
import { useAgentStore } from '@/stores/agent'
import { updatePassword } from '@/api/auth'

/** 弹窗可见性（v-model） */
const modelValue = defineModel<boolean>({ default: false })

/** 组件事件：success = 登录/注册成功 */
const emit = defineEmits<{ success: [] }>()

/** 组件属性：打开时的初始面板（登录 / 注册） */
const props = defineProps<{
  /** 初始面板模式：login 登录（默认）/ register 注册 */
  initialMode?: 'login' | 'register'
}>()

const auth = useAuthStore()
const agentStore = useAgentStore()

/** 面板模式：login 登录 / register 注册 / forgot 找回密码 */
const mode = ref<'login' | 'register' | 'forgot'>(props.initialMode ?? 'login')
/** 登录方式子 Tab：captcha 验证码（演示占位）/ password 密码 */
const loginType = ref<'captcha' | 'password'>('password')
/** 找回密码当前步骤（1 验证账号 → 2 设置新密码） */
const forgotStep = ref<1 | 2>(1)
/** 提交 loading（登录/注册/重置共用） */
const loading = ref(false)

/** 表单实例（登录 / 注册各自独立引用） */
const loginFormRef = ref<FormInstance>()
const registerFormRef = ref<FormInstance>()
const forgotFormRef = ref<FormInstance>()

/**
 * 全部认证表单数据（集中一个对象：
 * 登录/注册/找回面板与 Tab 之间切换时输入不丢失）。
 */
const form = reactive({
  /** 登录：用户名或邮箱 */
  username: '',
  /** 登录：密码 */
  password: '',
  /** 验证码（演示环境不可发送，仅占位） */
  captcha: '',
  /** 注册：邮箱（选填） */
  email: '',
  /** 注册/找回：密码 */
  password2: '',
  /** 注册/找回：确认密码 */
  confirmPassword: '',
  /** 注册：用户协议勾选 */
  agree: false,
  /** 找回第一步：用户名 */
  forgotUsername: '',
  /** 找回第一步：邮箱 */
  forgotEmail: '',
  /** 找回第二步：原密码（演示环境后端仍需校验旧密码） */
  oldPassword: '',
})

/** 登录表单校验规则 */
const loginRules: FormRules = {
  username: [{ required: true, message: '请输入用户名或邮箱', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

/** 注册表单校验规则 */
const registerRules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  email: [{ type: 'email', message: '邮箱格式不正确', trigger: 'blur' }],
  password2: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
  confirmPassword: [
    {
      validator: (_rule, value: string, callback) => {
        if (value !== form.password2) callback(new Error('两次输入的密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

/** 找回密码（第二步）校验规则 */
const forgotRules: FormRules = {
  oldPassword: [{ required: true, message: '演示环境需输入原密码完成校验', trigger: 'blur' }],
  password2: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
  confirmPassword: [
    {
      validator: (_rule, value: string, callback) => {
        if (value !== form.password2) callback(new Error('两次输入的密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

/** 弹窗标题（随面板切换） */
const dialogTitle = computed(() =>
  mode.value === 'login' ? '登录后开始你的创作' : mode.value === 'register' ? '注册账号' : '找回密码',
)

/** 头部副文案（随面板切换） */
const headSub = computed(() =>
  mode.value === 'login' ? 'AI 漫剧一站式生产平台' : mode.value === 'register' ? '注册即赠初始积分，免费开始第一部漫剧' : '两步重置你的账号密码',
)

/** 官网承接的创作意图（只读 chip 展示，登录后由工作台承接） */
const pendingIntent = computed(() => agentStore.pendingIntent)

// 弹窗每次打开：按 initialMode 切到对应面板（保留已输入内容）
watch(modelValue, (visible) => {
  if (visible) {
    mode.value = props.initialMode ?? 'login'
    loginType.value = 'password'
  }
})

/**
 * 提交登录（仅密码登录可用；验证码 Tab 为演示占位）。
 * 成功后关闭弹窗并 emit('success')。
 */
async function submitLogin(): Promise<void> {
  if (loginType.value === 'captcha') {
    ElMessage.info('演示环境未接入短信服务，请使用密码登录')
    return
  }
  const valid = await loginFormRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    ElMessage.success('登录成功')
    modelValue.value = false
    emit('success')
  } catch {
    // 业务错误（401/403）已由 axios 拦截器统一提示 detail
  } finally {
    loading.value = false
  }
}

/**
 * 提交注册：协议勾选门控（未勾选点注册给出提示）；
 * 成功即建立会话，关闭弹窗并 emit('success')。
 */
async function submitRegister(): Promise<void> {
  if (!form.agree) {
    ElMessage.warning('请先阅读并同意《用户协议》与《隐私政策》')
    return
  }
  const valid = await registerFormRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await auth.register({
      username: form.username,
      email: form.email || undefined,
      password: form.password2,
    })
    ElMessage.success('注册成功，已赠送初始积分')
    modelValue.value = false
    emit('success')
  } catch {
    // 业务错误（409 用户名/邮箱被占用）已由拦截器提示
  } finally {
    loading.value = false
  }
}

/**
 * 找回第一步（演示）：仅做非空校验，不真正校验账号（未接入短信/邮箱服务）。
 */
function nextForgotStep(): void {
  if (!form.forgotUsername.trim()) {
    ElMessage.warning('请输入用户名')
    return
  }
  if (!form.forgotEmail.trim()) {
    ElMessage.warning('请输入注册邮箱')
    return
  }
  forgotStep.value = 2
}

/**
 * 找回第二步：演示环境直接调 updatePassword 写入新密码
 * （后端仍校验旧密码，故需输入原密码，界面已标注演示说明）。
 * 成功后回到登录面板并回填用户名。
 */
async function submitForgot(): Promise<void> {
  const valid = await forgotFormRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await updatePassword(form.oldPassword, form.password2)
    ElMessage.success('密码重置成功，请使用新密码登录')
    form.username = form.forgotUsername
    form.password = ''
    backToLogin()
  } catch {
    // 400 旧密码不正确等已由拦截器提示
  } finally {
    loading.value = false
  }
}

/** 从找回面板返回登录面板（保留账号输入） */
function backToLogin(): void {
  mode.value = 'login'
  forgotStep.value = 1
}
</script>

<template>
  <el-dialog
    v-model="modelValue"
    width="480px"
    align-center
    destroy-on-close
    class="auth-dialog"
    :show-close="true"
  >
    <!-- 自定义头部：品牌标 + 动态标题 + 副文案（放映厅深色头带，与官网呼应） -->
    <template #header>
      <div class="auth-head">
        <MfLogo :size="40" label="漫序 MangaFlow" class="auth-head__logo" />
        <h2 class="auth-head__title">{{ dialogTitle }}</h2>
        <p class="auth-head__sub">{{ headSub }}</p>
      </div>
    </template>

    <!-- 意图承接区：官网带入的创作意图（原型 L-02） -->
    <el-alert
      v-if="pendingIntent"
      type="success"
      :closable="false"
      show-icon
      class="intent-chip"
      title="登录后将保留你刚才的创作意图"
      :description="pendingIntent"
    />

    <!-- ==================== 登录面板 ==================== -->
    <template v-if="mode === 'login'">
      <el-tabs v-model="loginType" stretch>
        <!-- 验证码登录：演示环境占位，发送按钮禁用 -->
        <el-tab-pane label="验证码登录" name="captcha">
          <el-form label-position="top" size="large" @submit.prevent>
            <el-form-item label="手机号 / 邮箱">
              <el-input v-model="form.username" placeholder="手机号或邮箱" />
            </el-form-item>
            <el-form-item label="验证码">
              <div class="captcha-row">
                <el-input v-model="form.captcha" placeholder="验证码" />
                <el-button disabled>获取验证码</el-button>
              </div>
            </el-form-item>
            <div class="demo-tip">演示环境未接入短信服务，请使用密码登录</div>
            <el-button type="primary" class="submit-btn" size="large" @click="submitLogin">
              登 录
            </el-button>
          </el-form>
        </el-tab-pane>

        <!-- 密码登录：本项目实际可用方式 -->
        <el-tab-pane label="密码登录" name="password">
          <el-form
            ref="loginFormRef"
            :model="form"
            :rules="loginRules"
            label-position="top"
            size="large"
            @keyup.enter="submitLogin"
          >
            <el-form-item label="用户名 / 邮箱" prop="username">
              <el-input v-model="form.username" placeholder="用户名或邮箱" autocomplete="username" />
            </el-form-item>
            <el-form-item label="密码" prop="password">
              <el-input
                v-model="form.password"
                type="password"
                placeholder="密码"
                show-password
                autocomplete="current-password"
              />
            </el-form-item>
            <el-button type="primary" class="submit-btn" size="large" :loading="loading" @click="submitLogin">
              登 录
            </el-button>
          </el-form>
        </el-tab-pane>
      </el-tabs>

      <div class="panel-links">
        <el-button link type="primary" @click="mode = 'forgot'">忘记密码？</el-button>
        <span class="link-text">还没有账号？</span>
        <el-button link type="primary" @click="mode = 'register'">立即注册</el-button>
      </div>
    </template>

    <!-- ==================== 注册面板 ==================== -->
    <template v-else-if="mode === 'register'">
      <el-form
        ref="registerFormRef"
        :model="form"
        :rules="registerRules"
        label-position="top"
        size="large"
        @keyup.enter="submitRegister"
      >
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="用户名" autocomplete="username" />
        </el-form-item>
        <el-form-item label="邮箱（选填）" prop="email">
          <el-input v-model="form.email" placeholder="邮箱" autocomplete="email" />
        </el-form-item>
        <el-form-item label="密码" prop="password2">
          <el-input v-model="form.password2" type="password" placeholder="至少 6 位" show-password autocomplete="new-password" />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirmPassword">
          <el-input v-model="form.confirmPassword" type="password" placeholder="再次输入密码" show-password autocomplete="new-password" />
        </el-form-item>
        <el-checkbox v-model="form.agree">
          我已阅读并同意
          <a class="doc-link" href="#" @click.stop>《用户协议》</a>与
          <a class="doc-link" href="#" @click.stop>《隐私政策》</a>
        </el-checkbox>
        <el-button type="primary" class="submit-btn" size="large" :loading="loading" @click="submitRegister">
          注 册
        </el-button>
      </el-form>
      <div class="panel-links">
        <span class="link-text">已有账号？</span>
        <el-button link type="primary" @click="mode = 'login'">去登录</el-button>
      </div>
    </template>

    <!-- ==================== 找回密码面板（L-03，两步） ==================== -->
    <template v-else>
      <!-- 第一步：验证账号（演示环境仅非空校验） -->
      <el-form v-if="forgotStep === 1" label-position="top" size="large" @submit.prevent>
        <div class="demo-tip demo-tip--top">演示环境未接入短信 / 邮箱验证，账号信息仅作记录</div>
        <el-form-item label="用户名">
          <el-input v-model="form.forgotUsername" placeholder="注册时的用户名" />
        </el-form-item>
        <el-form-item label="注册邮箱">
          <el-input v-model="form.forgotEmail" placeholder="注册时绑定的邮箱" />
        </el-form-item>
        <el-button type="primary" class="submit-btn" size="large" @click="nextForgotStep">
          下一步：设置新密码
        </el-button>
      </el-form>

      <!-- 第二步：设置新密码（演示环境需输入原密码通过后端校验） -->
      <el-form v-else ref="forgotFormRef" :model="form" :rules="forgotRules" label-position="top" size="large" @keyup.enter="submitForgot">
        <div class="demo-tip demo-tip--top">演示环境说明：未接入验证码，需输入原密码完成校验后设置新密码</div>
        <el-form-item label="原密码（演示校验用）" prop="oldPassword">
          <el-input v-model="form.oldPassword" type="password" placeholder="当前使用的密码" show-password />
        </el-form-item>
        <el-form-item label="新密码" prop="password2">
          <el-input v-model="form.password2" type="password" placeholder="至少 6 位" show-password />
        </el-form-item>
        <el-form-item label="确认新密码" prop="confirmPassword">
          <el-input v-model="form.confirmPassword" type="password" placeholder="再次输入新密码" show-password />
        </el-form-item>
        <el-button type="primary" class="submit-btn" size="large" :loading="loading" @click="submitForgot">
          重置密码
        </el-button>
      </el-form>

      <div class="panel-links">
        <el-button link type="primary" @click="backToLogin">返回登录</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
/* ============================================================
   品牌头部带：放映厅深色渐变，与官网 Hero 同源；
   el-dialog header 本体置空（padding 清零），头部满铺至弹窗边缘
   ============================================================ */
.auth-dialog :deep(.el-dialog) {
  border-radius: var(--mf-r-card);
  overflow: hidden;
  padding: 0;
}
.auth-dialog :deep(.el-dialog__header) {
  padding: 0;
  margin-right: 0;
}
.auth-dialog :deep(.el-dialog__headerbtn) {
  top: 14px;
  right: 14px;
  z-index: 2;
}
.auth-dialog :deep(.el-dialog__headerbtn .el-dialog__close) {
  color: rgba(232, 237, 234, 0.8);
}
.auth-dialog :deep(.el-dialog__headerbtn:hover .el-dialog__close) {
  color: #fff;
}
.auth-dialog :deep(.el-dialog__body) {
  padding: var(--mf-sp-6) var(--mf-sp-6) var(--mf-sp-5);
}
.auth-head {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: var(--mf-sp-6) var(--mf-sp-5) var(--mf-sp-5);
  text-align: center;
  background:
    radial-gradient(120% 140% at 80% 0%, rgba(79, 216, 202, 0.22), transparent 55%),
    linear-gradient(160deg, #12241F, #0C1A18);
}
.auth-head__logo {
  margin-bottom: 2px;
}
.auth-head__title {
  margin: 0;
  font-size: var(--mf-fs-xl);
  font-weight: var(--mf-fw-bold);
  color: #FFFFFF;
}
.auth-head__sub {
  margin: 0;
  font-size: var(--mf-fs-xs);
  color: rgba(232, 237, 234, 0.62);
}

/* 意图承接提示（弹窗顶部） */
.intent-chip {
  margin-bottom: var(--mf-sp-4);
}

/* 验证码输入行 */
.captcha-row {
  display: flex;
  gap: var(--mf-sp-2);
  width: 100%;
}

/* 演示环境提示文案 */
.demo-tip {
  font-size: var(--mf-fs-xs);
  color: var(--mf-warn);
  margin-bottom: var(--mf-sp-3);
}
.demo-tip--top {
  margin-bottom: var(--mf-sp-4);
}

/* 主提交按钮 */
.submit-btn {
  width: 100%;
  margin-top: var(--mf-sp-2);
  height: 44px;
  font-size: var(--mf-fs-base);
}

/* 面板底部链接区 */
.panel-links {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--mf-sp-1);
  margin-top: var(--mf-sp-4);
  font-size: var(--mf-fs-sm);
}
.link-text {
  color: var(--mf-text-2);
}

/* 协议文档链接 */
.doc-link {
  color: var(--mf-brand);
  text-decoration: none;
}
</style>
