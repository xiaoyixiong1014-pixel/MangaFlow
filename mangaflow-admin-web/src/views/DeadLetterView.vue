<script setup lang="ts">
/**
 * 文件级注释：死信队列管理页（Task 10.5，路由 /dead-letters）。
 * 表格展示重试耗尽的死信任务（任务/类型/错误/重试次数/时间）；
 * 操作「重新入队」：elMessageBox.prompt 必填 reason（写入审计留痕），
 * 成功后刷新列表。归属信息（任务/项目/用户 ID）辅助定位问题。
 */
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getDeadLetters, requeueDeadLetter } from '@/api/admin'
import type { DeadLetterItem } from '@/types'

/** 列表加载中 */
const loading = ref(false)
/** 死信任务列表 */
const items = ref<DeadLetterItem[]>([])
/** 总条数 */
const total = ref(0)
/** 当前页码 */
const page = ref(1)
/** 每页条数 */
const pageSize = ref(10)
/** 重新入队进行中的任务 ID */
const requeueBusyId = ref<number | null>(null)

/**
 * 拉取死信任务分页列表。
 * @returns 无
 * @throws 无（接口错误由拦截器统一提示）
 */
async function fetchList() {
  loading.value = true
  try {
    const resp = await getDeadLetters({ page: page.value, page_size: pageSize.value })
    items.value = resp.items
    total.value = resp.total
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    loading.value = false
  }
}

/**
 * 重新入队：prompt 必填 reason（空/仅空白则阻止提交），成功后刷新列表。
 * @param row 目标死信任务
 * @returns 无
 */
async function handleRequeue(row: DeadLetterItem) {
  let reason: string
  try {
    const { value } = await ElMessageBox.prompt(
      `将任务 #${row.id} 重新入队：会为归属用户重新冻结 ${row.cost_points} 积分，并清空错误与重试计数。请填写操作原因（必填，写入审计日志）：`,
      '重新入队',
      {
        confirmButtonText: '重新入队',
        cancelButtonText: '取消',
        inputPlaceholder: '例如：上游渠道故障已恢复，重跑该任务',
        inputValidator: (v: string) => (v && v.trim().length > 0 ? true : '操作原因不能为空'),
      },
    )
    reason = value.trim()
  } catch {
    return // 用户取消
  }
  requeueBusyId.value = row.id
  try {
    const resp = await requeueDeadLetter(row.id, { reason })
    ElMessage.success(resp.message || `任务 #${row.id} 已重新入队`)
    await fetchList()
  } catch {
    /* 错误已由拦截器提示（400 余额不足等） */
  } finally {
    requeueBusyId.value = null
  }
}

/** 任务类型 → 中文 */
function typeLabel(t: string): string {
  return ({ image: '关键帧', video: '视频', tts: '配音', compose: '合成', llm: '文本', extract: '抽取' })[t] ?? t
}

onMounted(fetchList)
</script>

<template>
  <div class="dead-letters">
    <div class="dead-letters__toolbar">
      <h2 class="page-title">死信队列</h2>
      <el-button data-test="refresh" @click="fetchList">刷新</el-button>
    </div>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="dead-letters__tip"
      title="死信任务为平台故障（platform_error）重试耗尽后的终态失败任务；重新入队会重新冻结归属用户积分并按优先级重新执行。"
    />

    <el-table :data="items" v-loading="loading" stripe>
      <el-table-column prop="id" label="任务 ID" width="90" />
      <el-table-column label="类型" width="90">
        <template #default="{ row }">
          <el-tag size="small" effect="plain">{{ typeLabel(row.type) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="error_code" label="错误码" width="150">
        <template #default="{ row }">{{ row.error_code || '—' }}</template>
      </el-table-column>
      <el-table-column prop="error" label="错误信息" min-width="240" show-overflow-tooltip>
        <template #default="{ row }">{{ row.error || '—' }}</template>
      </el-table-column>
      <el-table-column prop="retry_count" label="重试次数" width="90" align="center" />
      <el-table-column prop="cost_points" label="冻结积分" width="90" align="right" />
      <el-table-column label="归属" width="140">
        <template #default="{ row }">
          <span class="dead-letters__owner">
            项目 {{ row.project_id ?? '—' }} / 用户 {{ row.user_id ?? '—' }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ row.created_at?.slice(0, 19).replace('T', ' ') }}</template>
      </el-table-column>
      <el-table-column label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button
            link
            type="warning"
            size="small"
            :loading="requeueBusyId === row.id"
            data-test="requeue"
            @click="handleRequeue(row)"
          >
            重新入队
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="dead-letters__pagination">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[10, 20, 50]"
        layout="total, sizes, prev, pager, next"
        @current-change="fetchList"
        @size-change="page = 1; fetchList()"
      />
    </div>
  </div>
</template>

<style scoped>
.dead-letters {
  max-width: 1440px;
  margin: 0 auto;
}

.dead-letters__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--mf-sp-3);
}

.page-title {
  margin: 0;
  font-size: var(--mf-fs-xl);
  font-weight: var(--mf-fw-semibold);
}

.dead-letters__tip {
  margin-bottom: var(--mf-sp-3);
  border-radius: var(--mf-r-ctl);
}

.dead-letters__owner {
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-2);
}

.dead-letters__pagination {
  display: flex;
  justify-content: center;
  padding: var(--mf-sp-3) 0;
}
</style>
