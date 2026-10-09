<script setup lang="ts">
/**
 * 文件级注释：A-08 系统设置与存储备份。
 * 六个页签：站点资料（表单占位·演示）、基础设置（site_name / registration_enabled，
 * PUT /admin/settings 真实保存）、存储配置（表单占位·演示）、数据备份
 * （立即备份 + 文件列表）、监控告警与注销申请（说明性占位）。
 */
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  createSettingsBackup,
  getSettings,
  getSettingsBackups,
  saveSettings,
} from '@/api/admin'
import type { BackupFile, SettingsDict } from '@/types'

/** 当前页签 */
const activeTab = ref('basic')

/* ============================ 基础设置 ============================ */

/** 设置加载状态 */
const loading = ref(false)
/** 保存中 */
const saving = ref(false)

/** 基础设置表单 */
const basicForm = reactive({
  site_name: '',
  registration_enabled: true,
})

/**
 * 加载系统设置并填充基础设置表单。
 * @returns 无
 */
async function fetchSettings() {
  loading.value = true
  try {
    const dict: SettingsDict = await getSettings()
    basicForm.site_name = String(dict.site_name ?? '漫序 MangaFlow')
    basicForm.registration_enabled = dict.registration_enabled !== false
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    loading.value = false
  }
}

/**
 * 保存基础设置（PUT /admin/settings）。
 * @returns 无
 */
async function saveBasic() {
  saving.value = true
  try {
    await saveSettings({
      site_name: basicForm.site_name,
      registration_enabled: basicForm.registration_enabled,
    })
    ElMessage.success('基础设置已保存')
  } catch {
    /* 错误已由拦截器提示（保存失败保留原值供重试） */
  } finally {
    saving.value = false
  }
}

/* ============================ 数据备份 ============================ */

/** 备份列表加载状态 */
const bkLoading = ref(false)
/** 备份文件列表 */
const backups = ref<BackupFile[]>([])
/** 备份执行中 */
const backingUp = ref(false)

/**
 * 加载备份文件列表。
 * @returns 无
 */
async function fetchBackups() {
  bkLoading.value = true
  try {
    backups.value = await getSettingsBackups()
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    bkLoading.value = false
  }
}

/**
 * 立即创建备份（异步任务）并刷新列表。
 * @returns 无
 */
async function handleBackup() {
  backingUp.value = true
  try {
    await createSettingsBackup()
    ElMessage.success('备份任务已提交，稍后可在列表查看')
    await fetchBackups()
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    backingUp.value = false
  }
}

/**
 * 文件大小格式化（字节 → KB/MB）。
 * @param size 字节数
 * @returns 可读字符串
 */
function fmtSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(2)} MB`
}

onMounted(() => {
  void fetchSettings()
  void fetchBackups()
})
</script>

<template>
  <div class="settings" v-loading="loading">
    <div class="settings__toolbar">
      <h2 class="page-title">系统设置与存储备份</h2>
    </div>

    <el-tabs v-model="activeTab">
      <!-- ==================== 站点资料（演示占位） ==================== -->
      <el-tab-pane label="站点资料" name="site">
        <el-alert
          title="演示表单"
          description="站点资料保存接口待后端开放，当前为界面占位，不会提交。"
          type="info"
          :closable="false"
          show-icon
          class="settings__notice"
        />
        <el-form label-width="120px" class="settings__form">
          <el-form-item label="站点名称">
            <el-input model-value="漫序 MangaFlow" style="max-width: 360px" />
          </el-form-item>
          <el-form-item label="SEO 标题">
            <el-input placeholder="漫序 MangaFlow — AI 漫剧创作平台" style="max-width: 480px" />
          </el-form-item>
          <el-form-item label="SEO 描述">
            <el-input type="textarea" :rows="3" placeholder="站点 SEO 描述" style="max-width: 480px" />
          </el-form-item>
          <el-form-item>
            <el-button disabled>保存（演示）</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <!-- ==================== 基础设置（真实保存） ==================== -->
      <el-tab-pane label="基础设置" name="basic">
        <el-form label-width="140px" class="settings__form">
          <el-form-item label="站点名称">
            <el-input
              v-model="basicForm.site_name"
              style="max-width: 360px"
              data-test="site-name"
            />
          </el-form-item>
          <el-form-item label="开放注册">
            <el-switch
              v-model="basicForm.registration_enabled"
              active-text="开放"
              inactive-text="关闭"
              data-test="registration-switch"
            />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="saving" @click="saveBasic">保存设置</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <!-- ==================== 存储配置（演示占位） ==================== -->
      <el-tab-pane label="存储配置" name="storage">
        <el-alert
          title="演示表单"
          description="存储切换前建议先测试连通性；切换存储不会删除已有文件。当前为界面占位，不会提交。"
          type="info"
          :closable="false"
          show-icon
          class="settings__notice"
        />
        <el-form label-width="120px" class="settings__form">
          <el-form-item label="存储类型">
            <el-radio-group model-value="local">
              <el-radio value="local">本地存储</el-radio>
              <el-radio value="s3" disabled>S3 兼容对象存储</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="Endpoint">
            <el-input placeholder="https://s3.example.com" style="max-width: 420px" disabled />
          </el-form-item>
          <el-form-item label="Bucket">
            <el-input placeholder="mangaflow-media" style="max-width: 420px" disabled />
          </el-form-item>
          <el-form-item>
            <el-button disabled>测试连通性（演示）</el-button>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <!-- ==================== 数据备份（真实接口） ==================== -->
      <el-tab-pane label="数据备份" name="backup">
        <div class="settings__backup-bar">
          <el-button type="primary" :loading="backingUp" data-test="backup-now" @click="handleBackup">
            立即备份
          </el-button>
          <span class="settings__hint">备份为异步任务，完成后出现在下方列表</span>
        </div>
        <el-table :data="backups" v-loading="bkLoading" stripe>
          <el-table-column prop="file" label="文件名" min-width="260" show-overflow-tooltip />
          <el-table-column label="大小" width="120">
            <template #default="{ row }">{{ fmtSize(row.size) }}</template>
          </el-table-column>
          <el-table-column prop="created_at" label="创建时间" width="200" />
        </el-table>
        <el-empty v-if="backups.length === 0 && !bkLoading" description="暂无备份文件" :image-size="80" />
      </el-tab-pane>

      <!-- ==================== 监控告警（演示占位） ==================== -->
      <el-tab-pane label="监控告警" name="monitor">
        <el-alert
          title="说明"
          description="监控阈值（P95 延迟 / 失败率 / 队列积压）可配置，告警在「经营看板 → 资损与告警」中集中呈现；阈值配置接口待后端开放。"
          type="info"
          :closable="false"
          show-icon
        />
        <el-empty description="监控阈值配置待后端开放（演示占位）" :image-size="100" />
      </el-tab-pane>

      <!-- ==================== 注销申请（演示占位） ==================== -->
      <el-tab-pane label="注销申请" name="deletion">
        <el-alert
          title="说明"
          description="用户注销申请将在此集中审核：核验剩余积分与未结订单后执行数据清除；接口待后端开放。"
          type="info"
          :closable="false"
          show-icon
        />
        <el-empty description="暂无待处理的注销申请（演示占位）" :image-size="100" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.settings {
  max-width: 1440px;
  margin: 0 auto;
}

.settings__toolbar {
  margin-bottom: var(--mf-sp-3);
}

.page-title {
  margin: 0;
  font-size: var(--mf-fs-xl);
  font-weight: var(--mf-fw-semibold);
}

.settings__notice {
  margin-bottom: var(--mf-sp-4);
}

.settings__form {
  max-width: 720px;
  padding-top: var(--mf-sp-2);
}

.settings__backup-bar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  margin-bottom: var(--mf-sp-4);
}

.settings__hint {
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}
</style>
