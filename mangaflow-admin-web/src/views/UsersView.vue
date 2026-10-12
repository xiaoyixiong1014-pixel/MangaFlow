<script setup lang="ts">
/**
 * 文件级注释：A-03 用户管理。
 * 搜索 + 状态筛选 + 分页表格；操作列提供：
 * - 详情抽屉（钱包 / 近期日志 / 近期项目）
 * - 积分调整（输入调整量 → 填写原因，二次确认后提交）
 * - 禁用 / 启用（填写原因确认）
 * 高危操作均需原因并写入审计日志（RD-56）。
 */
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { adjustUserPoints, getUserDetail, getUsers, setUserStatus } from '@/api/admin'
import type { AdminUserDetail, AdminUserItem } from '@/types'

/** 列表加载状态 */
const loading = ref(false)
/** 用户列表 */
const items = ref<AdminUserItem[]>([])
/** 总条数 */
const total = ref(0)

/** 查询条件 */
const query = reactive({
  page: 1,
  page_size: 10,
  search: '',
  status: '' as number | '',
})

/** 详情抽屉可见性 */
const drawerVisible = ref(false)
/** 详情加载状态 */
const detailLoading = ref(false)
/** 当前查看的用户详情 */
const detail = ref<AdminUserDetail | null>(null)

/**
 * 加载用户列表。
 * @returns 无
 * @throws 无（接口错误由拦截器统一提示）
 */
async function fetchList() {
  loading.value = true
  try {
    const data = await getUsers({
      page: query.page,
      page_size: query.page_size,
      search: query.search || undefined,
      status: query.status === '' ? undefined : query.status,
    })
    items.value = data.items
    total.value = data.total
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    loading.value = false
  }
}

/**
 * 搜索（重置到第一页）。
 * @returns 无
 */
function handleSearch() {
  query.page = 1
  void fetchList()
}

/**
 * 打开详情抽屉并加载用户详情。
 * @param row 目标用户行
 * @returns 无
 */
async function openDetail(row: AdminUserItem) {
  drawerVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await getUserDetail(row.id)
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    detailLoading.value = false
  }
}

/**
 * 调整用户积分：第一步输入调整量（正加负减），第二步填写原因并确认（即二次确认）。
 * @param row 目标用户行
 * @returns 无
 * @throws 无（用户取消或校验失败时静默返回）
 */
async function handleAdjustPoints(row: AdminUserItem) {
  // 第一步：输入调整量
  let amount: number
  try {
    const { value } = await ElMessageBox.prompt(
      `为用户「${row.username}」调整积分，正数增加、负数扣减`,
      '积分调整 · 第 1 步',
      {
        confirmButtonText: '下一步',
        cancelButtonText: '取消',
        inputPattern: /^-?\d+$/,
        inputErrorMessage: '请输入整数积分（可为负数）',
      },
    )
    amount = Number(value)
  } catch {
    return
  }

  // 第二步：填写原因（高危操作二次确认）
  let reason: string
  try {
    const { value } = await ElMessageBox.prompt(
      `确认对「${row.username}」${amount > 0 ? '增加' : '扣减'} ${Math.abs(amount)} 积分？该操作将写入审计日志，请填写原因`,
      '积分调整 · 确认',
      {
        confirmButtonText: '确认调整',
        cancelButtonText: '取消',
        inputPlaceholder: '如：客诉补偿 / 风控回收',
        inputValidator: (v: string) => (v && v.trim().length > 0) || '原因必填',
      },
    )
    reason = value.trim()
  } catch {
    return
  }

  try {
    await adjustUserPoints(row.id, { amount, reason })
    ElMessage.success('积分调整成功')
    void fetchList()
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/**
 * 禁用 / 启用用户（需填写原因确认）。
 * @param row 目标用户行
 * @param target 目标状态：0 禁用 / 1 启用
 * @returns 无
 * @throws 无（用户取消或校验失败时静默返回）
 */
async function handleToggleStatus(row: AdminUserItem, target: 0 | 1) {
  const actionText = target === 0 ? '禁用' : '启用'
  let reason: string
  try {
    const { value } = await ElMessageBox.prompt(
      `确定要${actionText}用户「${row.username}」吗？${actionText}后立即生效`,
      `${actionText}账号`,
      {
        confirmButtonText: `确认${actionText}`,
        cancelButtonText: '取消',
        type: target === 0 ? 'warning' : 'info',
        inputPlaceholder: '请填写操作原因（必填，写入审计日志）',
        inputValidator: (v: string) => (v && v.trim().length > 0) || '原因必填',
      },
    )
    reason = value.trim()
  } catch {
    return
  }

  try {
    await setUserStatus(row.id, { status: target, reason })
    ElMessage.success(`已${actionText}用户「${row.username}」`)
    void fetchList()
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/** 状态文案 */
function statusText(s: number): string {
  return s === 1 ? '正常' : '禁用'
}

/** 状态 Tag 类型 */
function statusTag(s: number): 'success' | 'danger' {
  return s === 1 ? 'success' : 'danger'
}

onMounted(() => {
  void fetchList()
})
</script>

<template>
  <div class="users">
    <!-- 工具栏：搜索 + 状态筛选 -->
    <div class="users__toolbar">
      <h2 class="page-title">用户管理</h2>
      <div class="users__filters">
        <el-input
          v-model="query.search"
          placeholder="搜索用户名 / 邮箱"
          clearable
          style="width: 240px"
          @keyup.enter="handleSearch"
          @clear="handleSearch"
        />
        <el-select
          v-model="query.status"
          placeholder="状态"
          style="width: 120px"
          data-test="status-filter"
          @change="handleSearch"
        >
          <el-option label="全部" :value="''" />
          <el-option label="正常" :value="1" />
          <el-option label="禁用" :value="0" />
        </el-select>
        <el-button type="primary" @click="handleSearch">查询</el-button>
      </div>
    </div>

    <!-- 用户表格 -->
    <el-table :data="items" v-loading="loading" stripe class="users__table">
      <el-table-column label="用户" min-width="140">
        <template #default="{ row }">
          <span class="users__name">{{ row.username }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="email" label="邮箱" min-width="200" show-overflow-tooltip />
      <el-table-column label="套餐" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="row.vip_level === 'free' ? 'info' : 'warning'" effect="plain">
            {{ row.vip_level }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="statusTag(row.status)" effect="light">
            {{ statusText(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="project_count" label="项目" width="80" align="right" />
      <el-table-column label="积分" width="120" align="right">
        <template #default="{ row }">
          <!-- 列表接口 AdminUserItem 为扁平结构，points_balance 直接在行上；
               wallet 嵌套对象仅存在于详情接口，误绑会导致积分列恒 0（A-BUG-1） -->
          <span :class="{ 'users__points-neg': row.points_balance < 0 }">
            {{ (row.points_balance ?? 0).toLocaleString('zh-CN') }}
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="注册时间" width="170" show-overflow-tooltip />
      <el-table-column label="操作" width="240" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openDetail(row)">详情</el-button>
          <el-button link type="warning" size="small" @click="handleAdjustPoints(row)">积分调整</el-button>
          <el-button
            v-if="row.status === 1"
            link
            type="danger"
            size="small"
            @click="handleToggleStatus(row, 0)"
          >
            禁用
          </el-button>
          <el-button v-else link type="success" size="small" @click="handleToggleStatus(row, 1)">
            启用
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 分页 -->
    <div class="users__pager">
      <el-pagination
        v-model:current-page="query.page"
        v-model:page-size="query.page_size"
        :total="total"
        :page-sizes="[10, 20, 50]"
        layout="total, sizes, prev, pager, next"
        background
        @current-change="fetchList"
        @size-change="handleSearch"
      />
    </div>

    <!-- 用户详情抽屉 -->
    <el-drawer v-model="drawerVisible" title="用户详情" size="480px">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="ID">{{ detail.id }}</el-descriptions-item>
            <el-descriptions-item label="用户名">{{ detail.username }}</el-descriptions-item>
            <el-descriptions-item label="邮箱" :span="2">{{ detail.email }}</el-descriptions-item>
            <el-descriptions-item label="状态">{{ statusText(detail.status) }}</el-descriptions-item>
            <el-descriptions-item label="套餐">{{ detail.vip_level }}</el-descriptions-item>
            <el-descriptions-item label="可用积分">
              {{ detail.wallet?.points_balance ?? '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="冻结积分">
              {{ detail.wallet?.frozen_points ?? '—' }}
            </el-descriptions-item>
          </el-descriptions>

          <h4 class="users__section">近期项目</h4>
          <el-empty
            v-if="!detail.recent_projects?.length"
            description="暂无项目"
            :image-size="60"
          />
          <el-table v-else :data="detail.recent_projects" size="small">
            <el-table-column prop="title" label="标题" min-width="140" show-overflow-tooltip />
            <el-table-column prop="status" label="状态" width="90" />
            <el-table-column prop="updated_at" label="更新时间" width="150" show-overflow-tooltip />
          </el-table>

          <h4 class="users__section">审计日志</h4>
          <el-empty v-if="!detail.recent_logs?.length" description="暂无日志" :image-size="60" />
          <el-timeline v-else>
            <el-timeline-item
              v-for="log in detail.recent_logs"
              :key="log.id"
              :timestamp="log.created_at"
            >
              <b>{{ log.action }}</b> — {{ log.detail }}
            </el-timeline-item>
          </el-timeline>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.users {
  max-width: 1440px;
  margin: 0 auto;
}

.users__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--mf-sp-4);
  flex-wrap: wrap;
  gap: var(--mf-sp-3);
}

.page-title {
  margin: 0;
  font-size: var(--mf-fs-xl);
  font-weight: var(--mf-fw-semibold);
}

.users__filters {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}

.users__table {
  background: var(--mf-surface);
  border-radius: var(--mf-r-card);
}

.users__name {
  font-weight: var(--mf-fw-medium);
}

.users__points-neg {
  color: var(--mf-danger);
  font-weight: var(--mf-fw-semibold);
}

.users__pager {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--mf-sp-4);
}

.users__section {
  margin: var(--mf-sp-5) 0 var(--mf-sp-2);
  font-size: var(--mf-fs-md);
}
</style>
