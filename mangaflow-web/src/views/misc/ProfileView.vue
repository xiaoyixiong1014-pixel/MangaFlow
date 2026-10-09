<!--
  个人中心（文件级注释，P-06）。
  四 Tab：账号与安全（头像/邮箱/修改密码）· 用量统计（项目数/任务状态分布/本月消耗）·
  导入导出（演示占位）· 版本与日志（操作日志表格 + 快照说明）。
  说明：用量统计与操作日志走 /api/users/me/usage、/api/users/me/logs，
  现有 api 层未封装且不允许改动，故经 client 直连。
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import client from '@/api/client'
import * as authApi from '@/api/auth'
import { useAuthStore } from '@/stores/auth'
import type { PageResult } from '@/types'

const auth = useAuthStore()

// ---------- 账号与安全 ----------
/** 头像 URL 输入 */
const avatarInput = ref('')
/** 邮箱输入 */
const emailInput = ref('')
/** 资料保存中 */
const savingProfile = ref(false)
/** 修改密码表单 */
const pwdForm = reactive({ oldPassword: '', newPassword: '', confirm: '' })
/** 修改密码中 */
const changingPwd = ref(false)

/**
 * 保存资料（头像 URL / 邮箱），成功后刷新 store 用户信息。
 */
async function saveProfile(): Promise<void> {
  savingProfile.value = true
  try {
    await authApi.updateMe({
      avatar: avatarInput.value.trim() || undefined,
      email: emailInput.value.trim() || undefined,
    })
    await auth.fetchMe()
    ElMessage.success('资料已保存')
  } catch {
    // 409 邮箱被占用等错误已由拦截器提示
  } finally {
    savingProfile.value = false
  }
}

/**
 * 提交修改密码（前端先校验一致性，旧密码错误由后端 400 提示）。
 */
async function changePassword(): Promise<void> {
  if (!pwdForm.oldPassword || !pwdForm.newPassword) {
    ElMessage.warning('请填写旧密码与新密码')
    return
  }
  if (pwdForm.newPassword.length < 6) {
    ElMessage.warning('新密码至少 6 位')
    return
  }
  if (pwdForm.newPassword !== pwdForm.confirm) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }
  changingPwd.value = true
  try {
    await authApi.updatePassword(pwdForm.oldPassword, pwdForm.newPassword)
    ElMessage.success('密码修改成功')
    pwdForm.oldPassword = ''
    pwdForm.newPassword = ''
    pwdForm.confirm = ''
  } catch {
    // 400 旧密码不正确已由拦截器提示
  } finally {
    changingPwd.value = false
  }
}

// ---------- 用量统计 ----------
interface UsageData {
  /** 项目数 */
  project_count: number
  /** 任务状态计数 */
  task_count_by_status: Record<string, number>
  /** 本月消耗 */
  points_used_this_month: number
  /** 持有积分总额 */
  points_total: number
}
/** 用量数据 */
const usage = ref<UsageData | null>(null)
/** 用量加载状态 */
const usageLoading = ref(true)

/** 任务状态展示定义（中文 + 颜色） */
const statusDefs = [
  { key: 'queued', label: '排队中', color: 'var(--mf-info)' },
  { key: 'running', label: '进行中', color: 'var(--mf-warn)' },
  { key: 'done', label: '已完成', color: 'var(--mf-ok)' },
  { key: 'failed', label: '失败', color: 'var(--mf-danger)' },
] as const

/**
 * 任务状态分布进度条数据（按总任务数归一化百分比）。
 */
const statusBars = computed(() => {
  const counts = usage.value?.task_count_by_status ?? {}
  const total = Object.values(counts).reduce((s, n) => s + Number(n || 0), 0)
  return statusDefs.map((d) => ({
    ...d,
    count: Number(counts[d.key] ?? 0),
    percent: total > 0 ? Math.round((Number(counts[d.key] ?? 0) / total) * 100) : 0,
  }))
})

// ---------- 版本与日志 ----------
/** 操作日志行类型（宽松结构，与后端 OperationLogOut 对齐） */
interface LogRow {
  id: number
  action: string
  detail?: unknown
  ip?: string | null
  risk_level?: string
  created_at?: string | null
}
/** 操作日志 */
const logs = ref<LogRow[]>([])
/** 日志加载状态 */
const logsLoading = ref(true)
/** 日志总数 */
const logsTotal = ref(0)
/** 日志页码 */
const logPage = ref(1)

/**
 * 拉取操作日志（经 client 直连 /users/me/logs）。
 */
async function fetchLogs(): Promise<void> {
  logsLoading.value = true
  try {
    const resp = await client.get<PageResult<LogRow>>('/users/me/logs', {
      params: { page: logPage.value, page_size: 10 },
    })
    logs.value = resp.data.items
    logsTotal.value = resp.data.total
  } catch {
    // 错误已由拦截器提示
  } finally {
    logsLoading.value = false
  }
}

/**
 * 导出项目数据（演示级提示）。
 */
function exportData(): void {
  ElMessage.info('项目数据导出为异步任务，演示环境暂未开通，敬请期待')
}

/**
 * 导入项目 ZIP（占位提示）。
 */
function importZip(): void {
  ElMessage.info('项目 ZIP 导入即将开放，敬请期待')
}

onMounted(async () => {
  // 预填当前用户资料
  avatarInput.value = auth.user?.avatar ?? ''
  emailInput.value = auth.user?.email ?? ''
  // 并行拉取用量与日志
  try {
    const resp = await client.get<UsageData>('/users/me/usage')
    usage.value = resp.data
  } catch {
    // 错误已由拦截器提示
  } finally {
    usageLoading.value = false
  }
  fetchLogs()
})
</script>

<template>
  <div class="page-container">
    <h2 class="page-title">个人中心</h2>

    <el-tabs class="profile-tabs">
      <!-- 账号与安全 -->
      <el-tab-pane label="账号与安全">
        <div class="pane-narrow">
          <!-- 头像与用户名 -->
          <div class="profile-head">
            <img v-if="auth.user?.avatar" :src="auth.user.avatar" class="avatar-lg" alt="头像" />
            <span v-else class="avatar-lg avatar-fallback">{{ (auth.user?.username ?? '游')[0] }}</span>
            <div>
              <div class="username">{{ auth.user?.username }}</div>
              <div class="role-tip">用户名即昵称，暂不支持修改 · 角色：{{ auth.user?.role === 'operator' ? '运营' : '用户' }}</div>
            </div>
          </div>

          <el-form label-width="90px" label-position="left" class="form">
            <el-form-item label="头像 URL">
              <el-input v-model="avatarInput" placeholder="https://... 图片地址" />
            </el-form-item>
            <el-form-item label="用户名">
              <el-input :model-value="auth.user?.username" disabled />
            </el-form-item>
            <el-form-item label="邮箱">
              <el-input v-model="emailInput" placeholder="you@example.com" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="savingProfile" @click="saveProfile">保存资料</el-button>
            </el-form-item>
          </el-form>

          <el-divider />
          <h3 class="sub-title">修改密码</h3>
          <el-form label-width="90px" label-position="left" class="form">
            <el-form-item label="旧密码">
              <el-input v-model="pwdForm.oldPassword" type="password" show-password />
            </el-form-item>
            <el-form-item label="新密码">
              <el-input v-model="pwdForm.newPassword" type="password" show-password placeholder="至少 6 位" />
            </el-form-item>
            <el-form-item label="确认新密码">
              <el-input v-model="pwdForm.confirm" type="password" show-password />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="changingPwd" @click="changePassword">修改密码</el-button>
            </el-form-item>
          </el-form>
        </div>
      </el-tab-pane>

      <!-- 用量统计 -->
      <el-tab-pane label="用量统计">
        <div v-loading="usageLoading" class="usage-pane">
          <div class="usage-cards">
            <div class="usage-card">
              <div class="usage-num">{{ usage?.project_count ?? '—' }}</div>
              <div class="usage-label">项目数</div>
            </div>
            <div class="usage-card">
              <div class="usage-num">{{ usage?.points_used_this_month ?? '—' }}</div>
              <div class="usage-label">本月积分消耗</div>
            </div>
            <div class="usage-card">
              <div class="usage-num">{{ usage?.points_total ?? '—' }}</div>
              <div class="usage-label">持有积分（可用+冻结）</div>
            </div>
          </div>

          <h3 class="sub-title">任务状态分布</h3>
          <div class="status-bars">
            <div v-for="s in statusBars" :key="s.key" class="status-row">
              <span class="status-label">{{ s.label }}</span>
              <el-progress
                :percentage="s.percent"
                :color="s.color"
                :stroke-width="12"
                class="status-bar"
              />
              <span class="status-count">{{ s.count }} 个</span>
            </div>
          </div>
        </div>
      </el-tab-pane>

      <!-- 导入导出 -->
      <el-tab-pane label="导入导出">
        <div class="pane-narrow">
          <p class="io-desc">
            支持将项目数据（剧本 / 资产 / 分镜配置）打包导出为 ZIP，或从 ZIP 恢复到新项目。
            导出为异步任务，完成后可在消息通知中下载。
          </p>
          <div class="io-actions">
            <el-button type="primary" @click="exportData">导出项目数据</el-button>
            <el-button @click="importZip">项目 ZIP 导入</el-button>
          </div>
        </div>
      </el-tab-pane>

      <!-- 版本与日志 -->
      <el-tab-pane label="版本与日志">
        <h3 class="sub-title">操作日志</h3>
        <el-table :data="logs" v-loading="logsLoading" class="table">
          <el-table-column prop="action" label="操作" width="160" />
          <el-table-column label="详情" min-width="240">
            <template #default="{ row }">
              <span class="log-detail">{{ typeof row.detail === 'string' ? row.detail : JSON.stringify(row.detail ?? '') }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="ip" label="IP" width="140">
            <template #default="{ row }">{{ row.ip || '—' }}</template>
          </el-table-column>
          <el-table-column label="风险" width="90">
            <template #default="{ row }">
              <el-tag :type="row.risk_level === 'high' ? 'danger' : 'info'" size="small">
                {{ row.risk_level === 'high' ? '高' : '正常' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="时间" width="180">
            <template #default="{ row }">{{ (row.created_at ?? '').slice(0, 19).replace('T', ' ') }}</template>
          </el-table-column>
        </el-table>
        <div class="pager">
          <el-pagination
            v-model:current-page="logPage"
            :total="logsTotal"
            :page-size="10"
            layout="prev, pager, next"
            @current-change="fetchLogs"
          />
        </div>

        <el-divider />
        <h3 class="sub-title">版本快照</h3>
        <p class="io-desc">
          项目在关键阶段（剧本生成 / 资产定妆 / 成片发布等）会自动创建版本快照，可在创作工作区内
          回溯查看；回退版本前会展示影响面（受影响的资产与分镜数量）并要求二次确认。
        </p>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.profile-tabs :deep(.el-tabs__header) {
  margin-bottom: var(--mf-sp-5);
}
.pane-narrow {
  max-width: 560px;
}
.profile-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-4);
  margin-bottom: var(--mf-sp-6);
}
.avatar-lg {
  width: 64px;
  height: 64px;
  border-radius: var(--mf-r-pill);
  object-fit: cover;
}
.avatar-fallback {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, var(--mf-brand), var(--mf-brand2));
  color: var(--mf-brand-contrast);
  font-size: var(--mf-fs-2xl);
  font-weight: var(--mf-fw-semibold);
}
.username {
  font-size: var(--mf-fs-xl);
  font-weight: var(--mf-fw-semibold);
}
.role-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
  margin-top: 2px;
}
.sub-title {
  margin: 0 0 var(--mf-sp-3);
  font-size: var(--mf-fs-md);
  font-weight: var(--mf-fw-semibold);
}
.usage-pane {
  min-height: 200px;
}
.usage-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--mf-sp-4);
  margin-bottom: var(--mf-sp-6);
}
.usage-card {
  padding: var(--mf-sp-5);
  border-radius: var(--mf-r-card);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  text-align: center;
}
.usage-num {
  font-size: var(--mf-fs-2xl);
  font-weight: var(--mf-fw-bold);
  color: var(--mf-brand);
  font-family: var(--mf-mono);
}
.usage-label {
  margin-top: 4px;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.status-bars {
  max-width: 560px;
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
}
.status-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.status-label {
  width: 60px;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  flex-shrink: 0;
}
.status-bar {
  flex: 1;
}
.status-count {
  width: 64px;
  text-align: right;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
  flex-shrink: 0;
}
.io-desc {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.8;
  margin: 0 0 var(--mf-sp-4);
}
.io-actions {
  display: flex;
  gap: var(--mf-sp-3);
}
.table {
  width: 100%;
  border-radius: var(--mf-r-ctl);
  overflow: hidden;
}
.log-detail {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-xs);
  word-break: break-all;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--mf-sp-4);
}
</style>
