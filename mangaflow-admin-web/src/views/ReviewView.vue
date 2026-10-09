<script setup lang="ts">
/**
 * 文件级注释：A-04 项目与内容审核。
 * el-tabs 三页签（待审 / 已通过 / 已驳回）；左列表右详情双栏（40% / 60%）；
 * 详情展示审核对象类型、闸门、项目标题、分镜镜头；处置支持通过 /
 * 驳回（驳回必须填写具体建议，空则不允许提交，RD-36）。
 */
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getReviews, handleReview } from '@/api/admin'
import type { ReviewItem } from '@/types'

/** 页签定义 */
const tabs = [
  { name: 'pending', label: '待审' },
  { name: 'passed', label: '已通过' },
  { name: 'rejected', label: '已驳回' },
]

/** 当前页签 */
const activeTab = ref('pending')
/** 列表加载状态 */
const loading = ref(false)
/** 审核列表 */
const items = ref<ReviewItem[]>([])
/** 总条数 */
const total = ref(0)

/** 查询条件 */
const query = reactive({
  page: 1,
  page_size: 20,
  gate: '',
})

/** 当前选中的审核记录 */
const current = ref<ReviewItem | null>(null)

/** 闸门中文映射 */
const gateMap: Record<string, string> = {
  generation: '生成级',
  composition: '合成级',
  export: '导出级',
}

/**
 * 加载审核列表。
 * @returns 无
 * @throws 无（接口错误由拦截器统一提示）
 */
async function fetchList() {
  loading.value = true
  current.value = null
  try {
    const data = await getReviews({
      page: query.page,
      page_size: query.page_size,
      tab: activeTab.value,
      gate: query.gate || undefined,
    })
    items.value = data.items
    total.value = data.total
    // 默认选中第一条便于快速处置
    current.value = data.items[0] ?? null
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    loading.value = false
  }
}

/**
 * 切换页签（重置分页）。
 * @returns 无
 */
function handleTabChange() {
  query.page = 1
  void fetchList()
}

/**
 * 处置：通过。
 * @param item 目标审核记录
 * @returns 无
 * @throws 无（用户取消或接口失败时静默返回）
 */
async function handleApprove(item: ReviewItem) {
  try {
    await ElMessageBox.confirm(
      `确认通过「${item.project_title}」的该条审核记录吗？通过后立即同步用户端`,
      '通过确认',
      { confirmButtonText: '通过', cancelButtonText: '取消', type: 'success' },
    )
  } catch {
    return
  }
  try {
    await handleReview(item.id, { action: 'approve' })
    ElMessage.success('已通过')
    void fetchList()
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/**
 * 处置：驳回（建议必填，空则不让提交，RD-36）。
 * @param item 目标审核记录
 * @returns 无
 * @throws 无（用户取消或接口失败时静默返回）
 */
async function handleReject(item: ReviewItem) {
  let suggestion: string
  try {
    const { value } = await ElMessageBox.prompt(
      '驳回建议将展示给用户，须具体到镜头与修改方向',
      `驳回 · ${item.project_title}`,
      {
        confirmButtonText: '提交驳回',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: `例如：第 ${item.storyboard_seq ?? '—'} 镜画面疑似暴力，建议调整提示词或替换该关键帧`,
        inputValidator: (v: string) =>
          v && v.trim().length > 0 ? true : '驳回建议必填，且须具体（RD-36）',
      },
    )
    suggestion = value.trim()
  } catch {
    return
  }
  try {
    await handleReview(item.id, { action: 'reject', suggestion })
    ElMessage.success('已驳回并同步建议至用户端')
    void fetchList()
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/** 处置状态中文 */
function statusText(s: string): string {
  return s === 'pending' ? '待审' : s === 'passed' ? '已通过' : '已驳回'
}

/** 处置状态 Tag 类型 */
function statusTag(s: string): 'warning' | 'success' | 'danger' {
  return s === 'pending' ? 'warning' : s === 'passed' ? 'success' : 'danger'
}

onMounted(() => {
  void fetchList()
})
</script>

<template>
  <div class="review">
    <div class="review__toolbar">
      <h2 class="page-title">项目与内容审核</h2>
      <el-select
        v-model="query.gate"
        placeholder="闸门：全部"
        clearable
        style="width: 140px"
        @change="query.page = 1; fetchList()"
      >
        <el-option label="生成级" value="generation" />
        <el-option label="合成级" value="composition" />
        <el-option label="导出级" value="export" />
      </el-select>
    </div>

    <el-tabs v-model="activeTab" class="review__tabs" @tab-change="handleTabChange">
      <el-tab-pane
        v-for="t in tabs"
        :key="t.name"
        :name="t.name"
        :label="`${t.label}（${t.name === activeTab ? total : '…'}）`"
      />
    </el-tabs>

    <!-- 左列表右详情双栏 -->
    <div class="review__split">
      <!-- 左：队列列表 -->
      <aside class="review__list" v-loading="loading">
        <el-empty v-if="items.length === 0" :description="activeTab === 'pending' ? '待审队列为空' : '暂无记录'" />
        <div
          v-for="item in items"
          :key="item.id"
          class="review-card"
          :class="{ 'review-card--active': current?.id === item.id }"
          @click="current = item"
        >
          <div class="review-card__top">
            <span class="review-card__title">{{ item.project_title }}</span>
            <el-tag size="small" :type="statusTag(item.manual_status)" effect="light">
              {{ statusText(item.manual_status) }}
            </el-tag>
          </div>
          <div class="review-card__meta">
            {{ gateMap[item.gate] ?? item.gate }} · {{ item.target_type }}
            <template v-if="item.storyboard_seq !== null"> · #{{ item.storyboard_seq }} 镜</template>
          </div>
          <div class="review-card__reason">命中原因：{{ item.violation_type || item.result || '—' }}</div>
        </div>
        <el-pagination
          v-if="total > query.page_size"
          v-model:current-page="query.page"
          :page-size="query.page_size"
          :total="total"
          layout="prev, pager, next"
          small
          background
          class="review__pager"
          @current-change="fetchList"
        />
      </aside>

      <!-- 右：详情 -->
      <section class="review__detail">
        <el-empty v-if="!current" description="从左侧选择一条审核记录" />
        <template v-else>
          <div class="detail-head">
            <h3 class="detail-head__title">项目：{{ current.project_title }}</h3>
            <el-tag :type="statusTag(current.manual_status)" effect="light">
              {{ statusText(current.manual_status) }}
            </el-tag>
          </div>

          <el-descriptions :column="2" border size="small" class="detail-desc">
            <el-descriptions-item label="闸门">
              {{ gateMap[current.gate] ?? current.gate }}
            </el-descriptions-item>
            <el-descriptions-item label="审核对象类型">{{ current.target_type }}</el-descriptions-item>
            <el-descriptions-item label="命中原因" :span="2">
              {{ current.violation_type || '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="机审结果" :span="2">
              {{ current.result || '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="定位镜头">
              {{ current.storyboard_seq !== null ? `第 ${current.storyboard_seq} 镜` : '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="提交时间">{{ current.created_at }}</el-descriptions-item>
            <el-descriptions-item label="所属项目 ID" :span="2">
              #{{ current.project_id }}
            </el-descriptions-item>
          </el-descriptions>

          <div v-if="current.suggestion" class="detail-suggestion">
            <b>处置建议：</b>{{ current.suggestion }}
          </div>

          <div v-if="current.manual_status === 'pending'" class="detail-actions">
            <el-button type="success" @click="handleApprove(current)">通过</el-button>
            <el-button type="danger" @click="handleReject(current)">驳回</el-button>
          </div>
          <el-alert
            v-else
            title="该记录已处置"
            :description="'疑似内容仅锁定「导出」，用户仍可继续编辑其他部分（RD-35）'"
            type="info"
            :closable="false"
            show-icon
          />
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.review {
  max-width: 1440px;
  margin: 0 auto;
}

.review__toolbar {
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

.review__split {
  display: grid;
  grid-template-columns: 40% 1fr;
  gap: var(--mf-sp-4);
  align-items: start;
}

@media (max-width: 1000px) {
  .review__split {
    grid-template-columns: 1fr;
  }
}

.review__list {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
  min-height: 320px;
}

.review-card {
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
  padding: var(--mf-sp-3);
  cursor: pointer;
  transition: border-color var(--mf-dur) var(--mf-ease), box-shadow var(--mf-dur) var(--mf-ease);
}

.review-card:hover {
  border-color: var(--mf-border-strong);
}

.review-card--active {
  border-color: var(--mf-brand);
  box-shadow: var(--mf-ring);
}

.review-card__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-2);
}

.review-card__title {
  font-weight: var(--mf-fw-medium);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.review-card__meta {
  margin-top: var(--mf-sp-1);
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}

.review-card__reason {
  margin-top: var(--mf-sp-1);
  font-size: var(--mf-fs-sm);
  color: var(--mf-warn);
}

.review__pager {
  justify-content: center;
  margin-top: var(--mf-sp-2);
}

.review__detail {
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-card);
  padding: var(--mf-sp-5);
  min-height: 320px;
}

.detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-3);
}

.detail-head__title {
  margin: 0 0 var(--mf-sp-3);
  font-size: var(--mf-fs-lg);
}

.detail-desc {
  margin-bottom: var(--mf-sp-4);
}

.detail-suggestion {
  padding: var(--mf-sp-3);
  background: var(--mf-surface-2);
  border-radius: var(--mf-r-ctl);
  font-size: var(--mf-fs-sm);
  color: var(--mf-text-2);
  margin-bottom: var(--mf-sp-4);
}

.detail-actions {
  display: flex;
  gap: var(--mf-sp-3);
}
</style>
