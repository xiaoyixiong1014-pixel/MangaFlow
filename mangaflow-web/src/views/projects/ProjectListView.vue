<!--
  漫剧项目列表 W-02（文件级注释）。
  统计卡（今日/本月创建，前端统计）+ 置顶公告条 + 工具行
  （防抖搜索 / 状态筛选 / 排序 / 视图切换）+ 卡片 / 列表双视图
  + 悬浮操作（打开/复制/重命名/导出/删除）+ 分页 + 空态引导。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { MfButton, MfTag } from '@mangaflow/design'
import * as projectsApi from '@/api/projects'
import * as announcementsApi from '@/api/announcements'
import type { Announcement, Project } from '@/types'
import NewProjectDialog from '@/components/NewProjectDialog.vue'

const router = useRouter()

// ---------- 列表状态 ----------
/** 当前页码（从 1 开始） */
const page = ref(1)
/** 每页条数 */
const pageSize = ref(12)
/** 总条数（分页器） */
const total = ref(0)
/** 当前页项目数据 */
const items = ref<Project[]>([])
/** 列表加载中 */
const loading = ref(false)
/** 搜索关键字（防抖输入源） */
const searchInput = ref('')
/** 实际生效的搜索关键字（防抖后更新） */
const search = ref('')
/** 状态筛选（空串 = 全部） */
const statusFilter = ref('')
/** 排序字段 */
const sortBy = ref<'updated_at' | 'created_at'>('updated_at')
/** 视图模式：card 卡片 / list 列表 */
const viewMode = ref<'card' | 'list'>('card')
/** 请求序号守卫（防抖/快速切换时丢弃过期响应） */
let requestSeq = 0
/** 搜索防抖定时器 */
let searchTimer: ReturnType<typeof setTimeout> | null = null

/** 项目状态筛选选项（value 对齐后端枚举） */
const STATUS_OPTIONS = [
  { value: 'draft', label: '草稿' },
  { value: 'scripting', label: '剧本中' },
  { value: 'asset', label: '资产中' },
  { value: 'storyboard', label: '分镜中' },
  { value: 'producing', label: '生成中' },
  { value: 'review', label: '审核中' },
  { value: 'published', label: '已发布' },
]

/** 状态 → 中文文案映射 */
const STATUS_TEXT: Record<string, string> = {
  draft: '草稿',
  scripting: '剧本中',
  asset: '资产中',
  storyboard: '分镜中',
  producing: '生成中',
  review: '审核中',
  published: '已发布',
}

/** 状态 → MfTag 语义色映射 */
const STATUS_VARIANT: Record<string, 'neutral' | 'info' | 'brand' | 'warn' | 'ok'> = {
  draft: 'neutral',
  scripting: 'info',
  asset: 'brand',
  storyboard: 'warn',
  producing: 'info',
  review: 'warn',
  published: 'ok',
}

/** 卡片封面占位渐变（按 id 取色，墨青系避免与他域撞色） */
const COVER_GRADS = [
  'linear-gradient(135deg, #0F766E, #12B3A6)',
  'linear-gradient(135deg, #0E7490, #22D3EE)',
  'linear-gradient(135deg, #1E3A8A, #38BDF8)',
  'linear-gradient(135deg, #065F46, #34D399)',
  'linear-gradient(135deg, #155E75, #67E8F9)',
  'linear-gradient(135deg, #1F2937, #4FD8CA)',
]

// ---------- 统计（今日 / 本月创建，前端统计） ----------
/** 今日新建数 */
const todayCount = ref(0)
/** 本月创建数 */
const monthCount = ref(0)
/** 总项目数 */
const totalProjects = ref(0)

// ---------- 公告 ----------
/** 置顶公告（取第一条） */
const pinnedAnnouncement = ref<Announcement | null>(null)
/** 公告条可见性（可关闭） */
const announcementVisible = ref(true)

/**
 * 格式化时间：ISO → YYYY-MM-DD HH:mm（截断到分钟即可）。
 * @param iso ISO 时间字符串
 * @returns 展示文本（空入参返回 '—'）
 */
function formatTime(iso?: string | null): string {
  if (!iso) return '—'
  return iso.slice(0, 16).replace('T', ' ')
}

/**
 * 封面占位渐变：按项目 id 稳定取色。
 * @param p 项目
 * @returns CSS background 值
 */
function coverOf(p: Project): string {
  return COVER_GRADS[p.id % COVER_GRADS.length] ?? COVER_GRADS[0] ?? ''
}

/**
 * 卡片悬浮「更多」下拉命令分发。
 * @param cmd 命令名
 * @param p 项目
 */
function onCardCommand(cmd: string, p: Project): void {
  if (cmd === 'duplicate') duplicateProject(p)
  else if (cmd === 'rename') renameProject(p)
  else if (cmd === 'export') exportProject()
  else if (cmd === 'delete') deleteProject(p)
}

/**
 * 拉取项目列表（分页 + 搜索 + 筛选 + 排序）。
 * 同时用一次大页请求统计今日 / 本月创建数。
 */
async function fetchList(): Promise<void> {
  loading.value = true
  const seq = ++requestSeq
  try {
    const result = await projectsApi.list({
      page: page.value,
      page_size: pageSize.value,
      search: search.value || undefined,
      status: statusFilter.value || undefined,
      sort: sortBy.value,
      order: 'desc',
    })
    if (seq !== requestSeq) return // 过期响应丢弃
    items.value = result.items
    total.value = result.total
  } catch {
    // 错误已由拦截器提示 detail
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

/**
 * 统计今日 / 本月创建：取前 100 条按 created_at 前端汇总（简单实现）。
 */
async function fetchStats(): Promise<void> {
  try {
    const result = await projectsApi.list({ page: 1, page_size: 100 })
    const now = new Date()
    totalProjects.value = result.total
    todayCount.value = 0
    monthCount.value = 0
    for (const p of result.items) {
      if (!p.created_at) continue
      const d = new Date(p.created_at)
      const sameMonth = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()
      if (sameMonth) {
        monthCount.value++
        if (d.getDate() === now.getDate()) todayCount.value++
      }
    }
  } catch {
    // 统计失败局部降级（显示 0），不阻塞列表
  }
}

/**
 * 拉取置顶公告（公开接口，取第一条置顶项）。
 */
async function fetchAnnouncement(): Promise<void> {
  try {
    const list = await announcementsApi.list()
    pinnedAnnouncement.value = list.find((a) => a.is_pinned) ?? list[0] ?? null
  } catch {
    // 公告失败不影响主流程
  }
}

/**
 * 搜索框输入：300ms 防抖后更新关键字并回到第 1 页查询。
 */
function onSearchInput(): void {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    search.value = searchInput.value.trim()
    page.value = 1
    fetchList()
  }, 300)
}

/**
 * 状态筛选 / 排序变更：回到第 1 页重新查询。
 */
function onFilterChange(): void {
  page.value = 1
  fetchList()
}

/**
 * 分页变更：重新查询当前页。
 */
function onPaginationChange(): void {
  fetchList()
}

/**
 * 点击卡片 / 打开按钮：进入剧本创作页。
 * @param p 项目
 */
function openProject(p: Project): void {
  router.push(`/projects/${p.id}/script`)
}

/**
 * 复制项目（后端复制项目行，标题追加"(副本)"）后刷新。
 * @param p 项目
 */
async function duplicateProject(p: Project): Promise<void> {
  try {
    await projectsApi.duplicate(p.id)
    ElMessage.success('复制成功')
    fetchList()
    fetchStats()
  } catch {
    // 错误已由拦截器提示
  }
}

/**
 * 重命名项目：MessageBox 输入新标题 → update 后刷新。
 * @param p 项目
 */
async function renameProject(p: Project): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt('请输入新的项目标题', '重命名项目', {
      inputValue: p.title,
      inputValidator: (v: string) => (v.trim() ? true : '标题不能为空'),
      confirmButtonText: '确定',
      cancelButtonText: '取消',
    })
    await projectsApi.update(p.id, { title: value.trim() })
    ElMessage.success('重命名成功')
    fetchList()
  } catch (e) {
    if ((e as { action?: string })?.action === 'cancel') return // 用户取消
    // 接口错误已由拦截器提示
  }
}

/** 导出（暂未开放占位提示） */
function exportProject(): void {
  ElMessage.info('导出功能即将开放')
}

/**
 * 删除项目：confirm 后软删除并刷新。
 * @param p 项目
 */
async function deleteProject(p: Project): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除项目「${p.title}」吗？删除后可在回收站恢复。`, '删除项目', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return // 用户取消
  }
  try {
    await projectsApi.remove(p.id)
    ElMessage.success('已删除')
    fetchList()
    fetchStats()
  } catch {
    // 错误已由拦截器提示
  }
}

// ---------- 新建项目 ----------
/** 新建项目弹窗可见性 */
const newProjectVisible = ref(false)

/** 创建成功回调：刷新列表与统计 */
function onCreated(): void {
  fetchList()
  fetchStats()
}

/** 空态是否展示（非加载中且当前条件无数据） */
const isEmpty = computed(() => !loading.value && items.value.length === 0)

// 页面进入：公告 / 统计 / 列表并发拉取
onMounted(() => {
  fetchAnnouncement()
  fetchStats()
  fetchList()
})
// 离开页面：清理防抖定时器
onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
})
</script>

<template>
  <div class="page-container projects-page">
    <!-- ==================== 统计卡 ==================== -->
    <div class="stat-grid">
      <div class="stat-card">
        <span class="stat-label">今日新建</span>
        <span class="stat-value">{{ todayCount }}</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">本月创建</span>
        <span class="stat-value">{{ monthCount }}</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">项目总数</span>
        <span class="stat-value">{{ totalProjects }}</span>
      </div>
    </div>

    <!-- ==================== 系统公告条 ==================== -->
    <el-alert
      v-if="pinnedAnnouncement && announcementVisible"
      type="warning"
      show-icon
      class="announcement"
      :title="`系统公告：${pinnedAnnouncement.title}`"
      :description="pinnedAnnouncement.content"
      closable
      @close="announcementVisible = false"
    />

    <!-- ==================== 工具行 ==================== -->
    <div class="toolbar">
      <div class="toolbar__left">
        <el-input
          v-model="searchInput"
          class="search-input"
          placeholder="搜索项目标题…"
          clearable
          @input="onSearchInput"
          @clear="search = ''; page = 1; fetchList()"
        />
        <el-select v-model="statusFilter" class="status-select" placeholder="全部状态" @change="onFilterChange">
          <el-option label="全部状态" value="" />
          <el-option v-for="s in STATUS_OPTIONS" :key="s.value" :label="s.label" :value="s.value" />
        </el-select>
        <el-select v-model="sortBy" class="sort-select" @change="onFilterChange">
          <el-option label="最近更新" value="updated_at" />
          <el-option label="最近创建" value="created_at" />
        </el-select>
      </div>
      <div class="toolbar__right">
        <el-radio-group v-model="viewMode" size="small">
          <el-radio-button value="card">卡片</el-radio-button>
          <el-radio-button value="list">列表</el-radio-button>
        </el-radio-group>
        <MfButton @click="newProjectVisible = true">＋ 新建项目</MfButton>
      </div>
    </div>

    <!-- ==================== 列表加载骨架 ==================== -->
    <div v-if="loading" class="card-grid">
      <div v-for="i in 6" :key="i" class="project-card project-card--skeleton">
        <el-skeleton :rows="3" animated />
      </div>
    </div>

    <!-- ==================== 卡片视图 ==================== -->
    <template v-else-if="viewMode === 'card' && !isEmpty">
      <el-row :gutter="20" class="card-row">
        <el-col v-for="p in items" :key="p.id" :xs="24" :sm="12" :md="8" :lg="6">
          <div class="project-card" @click="openProject(p)">
            <div class="card-cover" :style="{ background: p.cover_url ? `url(${p.cover_url}) center/cover` : coverOf(p) }">
              <MfTag class="card-status" :variant="STATUS_VARIANT[p.status] ?? 'neutral'" size="sm">
                {{ STATUS_TEXT[p.status] ?? p.status }}
              </MfTag>
              <!-- 悬浮操作按钮 -->
              <div class="card-actions" @click.stop>
                <el-button size="small" type="primary" @click="openProject(p)">打开</el-button>
                <el-dropdown trigger="click" @command="(cmd: string) => onCardCommand(cmd, p)">
                  <el-button size="small">更多</el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="duplicate">复制项目</el-dropdown-item>
                      <el-dropdown-item command="rename">重命名</el-dropdown-item>
                      <el-dropdown-item command="export">导出（即将开放）</el-dropdown-item>
                      <el-dropdown-item command="delete" divided>删除项目</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </div>
            </div>
            <div class="card-body">
              <div class="card-title" :title="p.title">{{ p.title }}</div>
              <div class="card-meta">
                <span>{{ p.genre || '未设体裁' }}</span>
                <span>{{ p.style || '未设风格' }}</span>
                <span>{{ p.aspect_ratio }}</span>
              </div>
              <div class="card-time">更新于 {{ formatTime(p.updated_at) }}</div>
            </div>
          </div>
        </el-col>
      </el-row>
    </template>

    <!-- ==================== 列表视图 ==================== -->
    <template v-else-if="viewMode === 'list' && !isEmpty">
      <el-table :data="items" class="list-table" @row-click="openProject">
        <el-table-column label="标题" min-width="200">
          <template #default="{ row }">
            <div class="table-title">
              <span class="table-cover" :style="{ background: coverOf(row) }" />
              {{ row.title }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <MfTag :variant="STATUS_VARIANT[row.status] ?? 'neutral'" size="sm">
              {{ STATUS_TEXT[row.status] ?? row.status }}
            </MfTag>
          </template>
        </el-table-column>
        <el-table-column prop="genre" label="体裁" width="100">
          <template #default="{ row }">{{ row.genre || '—' }}</template>
        </el-table-column>
        <el-table-column prop="style" label="风格" width="110">
          <template #default="{ row }">{{ row.style || '—' }}</template>
        </el-table-column>
        <el-table-column prop="aspect_ratio" label="比例" width="80" />
        <el-table-column label="更新时间" width="160">
          <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="240" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click.stop="openProject(row)">打开</el-button>
            <el-button link size="small" @click.stop="duplicateProject(row)">复制</el-button>
            <el-button link size="small" @click.stop="renameProject(row)">重命名</el-button>
            <el-button link size="small" @click.stop="exportProject()">导出</el-button>
            <el-button link type="danger" size="small" @click.stop="deleteProject(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <!-- ==================== 空态 ==================== -->
    <el-empty v-else description="还没有项目，从一句话开始你的第一部漫剧">
      <MfButton size="lg" @click="newProjectVisible = true">创建第一个项目</MfButton>
    </el-empty>

    <!-- ==================== 分页 ==================== -->
    <div v-if="total > pageSize" class="pagination-row">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[12, 24, 48]"
        layout="total, sizes, prev, pager, next"
        @current-change="onPaginationChange"
        @size-change="onPaginationChange"
      />
    </div>

    <!-- 新建项目弹窗（S-01） -->
    <NewProjectDialog v-model="newProjectVisible" @created="onCreated" />
  </div>
</template>

<style scoped>
.projects-page {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-5);
}

/* ---------- 统计卡 ---------- */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 240px));
  gap: var(--mf-sp-4);
}
.stat-card {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-1);
  padding: var(--mf-sp-4) var(--mf-sp-5);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-card);
}
.stat-label {
  font-size: var(--mf-fs-sm);
  color: var(--mf-text-2);
}
.stat-value {
  font-size: var(--mf-fs-2xl);
  font-weight: var(--mf-fw-bold);
  color: var(--mf-brand);
}

/* ---------- 公告条 ---------- */
.announcement {
  border-radius: var(--mf-r-ctl);
}

/* ---------- 工具行 ---------- */
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--mf-sp-3);
}
.toolbar__left,
.toolbar__right {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.search-input {
  width: 220px;
}
.status-select {
  width: 130px;
}
.sort-select {
  width: 120px;
}

/* ---------- 卡片视图 ---------- */
.card-row {
  row-gap: var(--mf-sp-4);
}
.project-card {
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-card);
  overflow: hidden;
  cursor: pointer;
  transition: box-shadow var(--mf-dur) var(--mf-ease), transform var(--mf-dur) var(--mf-ease),
    border-color var(--mf-dur) var(--mf-ease);
}
.project-card:hover {
  box-shadow: var(--mf-shadow-pop);
  transform: translateY(-2px);
  border-color: var(--mf-border-strong);
}
.project-card--skeleton {
  padding: var(--mf-sp-4);
  cursor: default;
}
.card-cover {
  position: relative;
  aspect-ratio: 16 / 9;
}
.card-status {
  position: absolute;
  top: 10px;
  left: 10px;
}
.card-actions {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--mf-sp-2);
  background: rgba(0, 0, 0, 0.45);
  opacity: 0;
  transition: opacity var(--mf-dur) var(--mf-ease);
}
.project-card:hover .card-actions {
  opacity: 1;
}
.card-body {
  padding: var(--mf-sp-3) var(--mf-sp-4) var(--mf-sp-4);
}
.card-title {
  font-size: var(--mf-fs-md);
  font-weight: var(--mf-fw-semibold);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-meta {
  display: flex;
  gap: var(--mf-sp-3);
  margin-top: var(--mf-sp-2);
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-2);
}
.card-time {
  margin-top: var(--mf-sp-1);
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}

/* ---------- 列表视图 ---------- */
.list-table {
  border-radius: var(--mf-r-card);
}
.table-title {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  font-weight: var(--mf-fw-medium);
}
.table-cover {
  width: 28px;
  height: 28px;
  flex: none;
  border-radius: 6px;
}
:deep(.el-table__row) {
  cursor: pointer;
}

/* ---------- 分页 ---------- */
.pagination-row {
  display: flex;
  justify-content: center;
  padding: var(--mf-sp-2) 0;
}
</style>
