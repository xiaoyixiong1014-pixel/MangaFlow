<!--
  剧本创作页 · 版本历史抽屉（文件级注释，redesign-script-workspace Task 1）。

  自 StepIdea 工具行整体迁出（V-01 版本快照回退），挂载于 ScriptView 顶部通栏：
  - 抽屉打开时拉取项目版本快照列表（前 50 条，created_at 倒序，后端已排序）；
  - 支持回退到指定快照：二次确认后调回退接口，服务端事务内自动创建
    「回退前存档」快照（可再次回退撤销）并写审计日志；
  - 显隐由父组件（ScriptView）v-model 控制。
-->
<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { rollbackSnapshot, snapshots as snapshotsApi } from '@/api/projects'
import type { VersionSnapshot } from '@/types'

/** 组件入参 */
const props = defineProps<{
  /** 当前项目 ID（快照接口路径参数） */
  projectId: number
}>()

/** 抽屉显隐（父组件 v-model） */
const visible = defineModel<boolean>({ default: false })

/** 快照列表加载中 */
const snapshotsLoading = ref(false)
/** 项目版本快照列表（created_at 倒序，后端已排序） */
const snapshotRows = ref<VersionSnapshot[]>([])
/** 正在回退的快照 ID（按钮 loading），null 表示无进行中的回退 */
const rollingBackId = ref<number | null>(null)

/** 快照阶段中文标签映射（抽屉展示用；未覆盖的阶段原样展示标识） */
const STAGE_LABELS: Record<string, string> = {
  extract_confirmed: '资产抽取确认',
  asset_locked: '角色定妆锁定',
  pre_rollback: '回退前存档',
  manual: '手动快照',
}

/**
 * 快照阶段的中文标签。
 * @param stage 阶段标识
 * @returns 中文标签（未知阶段返回原标识）
 */
function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage
}

/**
 * 格式化快照创建时间为本地可读格式。
 * @param iso ISO8601 时间串（可空）
 * @returns 形如「2026-10-10 20:30」的串；无效输入原样返回或 '-'
 */
function formatSnapshotTime(iso?: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/**
 * 生成快照摘要文案（summary 为各类实体计数时拼接展示，否则占位）。
 * @param snap 快照行
 * @returns 摘要文案
 */
function summarizeSnapshot(snap: VersionSnapshot): string {
  const s = snap.summary as Record<string, unknown> | null | undefined
  if (s && typeof s === 'object') {
    const parts: string[] = []
    if (typeof s.characters === 'number') parts.push(`角色 ${s.characters}`)
    if (typeof s.scenes === 'number') parts.push(`场景 ${s.scenes}`)
    if (typeof s.props === 'number') parts.push(`道具 ${s.props}`)
    if (parts.length) return parts.join(' · ')
  }
  return '（无摘要）'
}

/**
 * 拉取项目版本快照列表（首页前 50 条，created_at 倒序）。
 * @returns 无
 * @throws 接口错误已由 client 拦截器统一弹窗提示
 */
async function fetchSnapshotRows(): Promise<void> {
  snapshotsLoading.value = true
  try {
    const page = await snapshotsApi(props.projectId, { page: 1, page_size: 50 })
    snapshotRows.value = page.items
  } catch {
    // 业务错误已由 client 拦截器统一弹窗提示
  } finally {
    snapshotsLoading.value = false
  }
}

/**
 * 回退到指定快照：elMessageBox 二次确认 → 调回退 API → 成功提示并刷新列表。
 * 服务端会自动创建「回退前存档」快照（可再次回退撤销），并写审计日志。
 * @param snap 目标快照
 * @returns 无
 * @throws 接口错误已由 client 拦截器统一弹窗提示
 */
async function handleRollback(snap: VersionSnapshot): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '将把项目资产（角色/场景/道具）恢复到该快照记录的状态；回退前会自动保存当前状态为「回退前存档」快照，可再次回退撤销。是否继续？',
      '回退此版本',
      { type: 'warning', confirmButtonText: '确认回退', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消确认
  }
  rollingBackId.value = snap.id
  try {
    const res = await rollbackSnapshot(props.projectId, snap.id)
    ElMessage.success(`已回退；回退前状态已存为快照 #${res.new_snapshot_id}`)
    await fetchSnapshotRows()
  } catch {
    // 业务错误已由 client 拦截器统一弹窗提示
  } finally {
    rollingBackId.value = null
  }
}

// 抽屉每次打开时刷新快照列表（保证展示最新版本点）
watch(visible, (v) => {
  if (v) void fetchSnapshotRows()
})
</script>

<template>
  <el-drawer v-model="visible" title="版本历史" size="480px" append-to-body>
    <div v-loading="snapshotsLoading">
      <el-empty
        v-if="!snapshotsLoading && snapshotRows.length === 0"
        description="暂无版本快照（在关键阶段会自动创建）"
      />
      <div v-for="snap in snapshotRows" :key="snap.id" class="snapshot-item">
        <div class="snapshot-head">
          <el-tag size="small" :type="snap.stage === 'pre_rollback' ? 'info' : 'primary'">
            {{ stageLabel(snap.stage) }}
          </el-tag>
          <span class="snapshot-time">{{ formatSnapshotTime(snap.created_at) }}</span>
        </div>
        <p class="snapshot-summary">{{ summarizeSnapshot(snap) }}</p>
        <div class="snapshot-actions">
          <el-button
            size="small"
            type="warning"
            plain
            :loading="rollingBackId === snap.id"
            @click="handleRollback(snap)"
          >
            回退此版本
          </el-button>
        </div>
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
/* 版本历史抽屉内的快照卡片（样式自 StepIdea 原样迁入） */
.snapshot-item {
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  margin-bottom: 12px;
}
.snapshot-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.snapshot-time {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.snapshot-summary {
  font-size: 13px;
  color: var(--el-text-color-regular);
  margin: 8px 0;
}
.snapshot-actions {
  text-align: right;
}
</style>
