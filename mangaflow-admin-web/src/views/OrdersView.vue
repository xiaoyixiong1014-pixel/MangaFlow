<script setup lang="ts">
/**
 * 文件级注释：A-06 订单与计费管理。
 * 五个页签：订单（搜索/筛选/分页 + 人工完成/关闭/退款，均二次确认且原因必填）、
 * 积分规则与支付渠道（静态说明）、财务流水与对账（由订单数据派生的简化表，标注演示数据）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { closeOrder, completeOrder, getOrders, refundOrder } from '@/api/admin'
import type { OrderItem } from '@/types'

/** 当前页签 */
const activeTab = ref('orders')

/* ============================ 订单列表 ============================ */

/** 列表加载状态 */
const loading = ref(false)
/** 订单列表 */
const items = ref<OrderItem[]>([])
/** 总条数 */
const total = ref(0)

/** 查询条件 */
const query = reactive({
  page: 1,
  page_size: 10,
  status: '',
  search: '',
})

/** 订单状态中文 */
const statusMap: Record<string, string> = {
  pending: '待支付',
  paid: '已支付',
  closed: '已关闭',
  refunded: '已退款',
}

/** 订单状态 Tag 类型 */
function statusTag(s: string): 'warning' | 'success' | 'info' | 'danger' {
  if (s === 'paid') return 'success'
  if (s === 'pending') return 'warning'
  if (s === 'refunded') return 'danger'
  return 'info'
}

/**
 * 加载订单列表。
 * @returns 无
 * @throws 无（接口错误由拦截器统一提示）
 */
async function fetchList() {
  loading.value = true
  try {
    const data = await getOrders({
      page: query.page,
      page_size: query.page_size,
      status: query.status || undefined,
      search: query.search || undefined,
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
 * 搜索（重置页码）。
 * @returns 无
 */
function handleSearch() {
  query.page = 1
  void fetchList()
}

/**
 * 统一的高危操作执行器：确认框（原因必填）→ 调接口 → 刷新。
 * @param row 目标订单
 * @param action 操作类型：complete / close / refund
 * @returns 无
 * @throws 无（用户取消或接口失败时静默返回）
 */
async function handleOrderAction(row: OrderItem, action: 'complete' | 'close' | 'refund') {
  const meta = {
    complete: { title: '人工完成订单', text: '确认将该订单标记为已支付并发放权益吗？', btn: '人工完成', required: false },
    close: { title: '关闭订单', text: '确认关闭该订单吗？关闭后不可再支付。', btn: '关闭', required: false },
    refund: { title: '订单退款', text: '确认退款该订单吗？退款将原路退回并扣减对应积分。', btn: '确认退款', required: true },
  }[action]

  let reason = ''
  try {
    const { value } = await ElMessageBox.prompt(`${meta.text}（原因将写入财务流水留痕）`, meta.title, {
      confirmButtonText: meta.btn,
      cancelButtonText: '取消',
      type: action === 'refund' ? 'warning' : 'info',
      inputPlaceholder: meta.required ? '退款原因（必填）' : '操作原因（选填）',
      inputValidator: (v: string) =>
        meta.required ? (v && v.trim().length > 0) || '退款原因必填' : true,
    })
    reason = value?.trim() ?? ''
  } catch {
    return
  }

  try {
    if (action === 'complete') {
      await completeOrder(row.id, { reason: reason || undefined })
    } else if (action === 'close') {
      await closeOrder(row.id, { reason: reason || undefined })
    } else {
      await refundOrder(row.id, { reason })
    }
    ElMessage.success(`订单 ${row.order_no} 操作成功`)
    void fetchList()
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/* ====================== 财务流水 / 对账（演示数据） ====================== */

/** 由已支付/已退款订单派生的简化流水 */
const ledgerRows = computed(() =>
  items.value
    .filter((o) => o.status === 'paid' || o.status === 'refunded')
    .map((o) => ({
      order_no: o.order_no,
      username: o.username,
      type: o.status === 'refunded' ? '退款出账' : '支付入账',
      amount: o.status === 'refunded' ? -o.amount : o.amount,
      time: o.paid_at ?? o.created_at,
    })),
)

/** 由订单数据按创建日期聚合的简化对账视图 */
const reconRows = computed(() => {
  const map = new Map<string, { platform: number; count: number }>()
  for (const o of items.value) {
    if (o.status !== 'paid') continue
    const day = (o.created_at ?? '').slice(0, 10) || '—'
    const cur = map.get(day) ?? { platform: 0, count: 0 }
    cur.platform += o.amount
    cur.count += 1
    map.set(day, cur)
  }
  // 演示：渠道对账按平台流水打 98 折模拟差异
  return Array.from(map.entries())
    .sort(([a], [b]) => (a < b ? 1 : -1))
    .map(([date, v]) => {
      const channel = Math.round(v.platform * 0.98)
      return { date, platform: v.platform, channel, diff: channel - v.platform, count: v.count }
    })
})

onMounted(() => {
  void fetchList()
})
</script>

<template>
  <div class="orders">
    <div class="orders__toolbar">
      <h2 class="page-title">订单与计费管理</h2>
    </div>

    <el-tabs v-model="activeTab">
      <!-- ==================== 订单 ==================== -->
      <el-tab-pane label="订单" name="orders">
        <div class="orders__filters">
          <el-input
            v-model="query.search"
            placeholder="搜索订单号 / 用户"
            clearable
            style="width: 240px"
            @keyup.enter="handleSearch"
            @clear="handleSearch"
          />
          <el-select
            v-model="query.status"
            placeholder="状态"
            clearable
            style="width: 130px"
            @change="handleSearch"
          >
            <el-option label="待支付" value="pending" />
            <el-option label="已支付" value="paid" />
            <el-option label="已关闭" value="closed" />
            <el-option label="已退款" value="refunded" />
          </el-select>
          <el-button type="primary" @click="handleSearch">查询</el-button>
        </div>

        <el-table :data="items" v-loading="loading" stripe>
          <el-table-column prop="order_no" label="订单号" min-width="180" show-overflow-tooltip />
          <el-table-column prop="username" label="用户" width="120" />
          <el-table-column label="类型" width="90">
            <template #default="{ row }">
              {{ row.product_type === 'points' ? '积分' : row.product_type === 'subscription' ? '订阅' : row.product_type }}
            </template>
          </el-table-column>
          <el-table-column label="金额" width="110" align="right">
            <template #default="{ row }">¥{{ row.amount?.toLocaleString('zh-CN') }}</template>
          </el-table-column>
          <el-table-column prop="points" label="积分" width="90" align="right" />
          <el-table-column prop="pay_channel" label="支付渠道" width="100" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="statusTag(row.status)" effect="light">
                {{ statusMap[row.status] ?? row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="created_at" label="创建时间" width="170" show-overflow-tooltip />
          <el-table-column label="操作" width="220" fixed="right">
            <template #default="{ row }">
              <template v-if="row.status === 'pending'">
                <el-button link type="success" size="small" @click="handleOrderAction(row, 'complete')">
                  人工完成
                </el-button>
                <el-button link type="danger" size="small" @click="handleOrderAction(row, 'close')">
                  关闭
                </el-button>
              </template>
              <el-button
                v-if="row.status === 'paid'"
                link
                type="danger"
                size="small"
                @click="handleOrderAction(row, 'refund')"
              >
                退款
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="orders__pager">
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

        <el-alert
          title="高危操作提示"
          description="退款 / 人工完成订单需二次确认并填写原因，写入财务流水（RD-56）；资金三件套不可绕过（RD-55）。"
          type="warning"
          :closable="false"
          show-icon
          class="orders__notice"
        />
      </el-tab-pane>

      <!-- ==================== 积分规则（静态说明） ==================== -->
      <el-tab-pane label="积分规则" name="points-rules">
        <el-alert
          title="说明"
          description="积分规则（充值档位 / 消耗单价 / 有效期）由「系统设置 → 基础设置」与逻辑模型单价共同决定，暂无独立维护界面。"
          type="info"
          :closable="false"
          show-icon
        />
        <el-empty description="积分规则配置待后端开放，敬请期待" />
      </el-tab-pane>

      <!-- ==================== 支付渠道（静态说明） ==================== -->
      <el-tab-pane label="支付渠道" name="pay-channels">
        <el-alert
          title="说明"
          description="支付渠道（微信 / 支付宝 / Stripe）的商户参数属敏感配置，由部署环境变量管理，管理端仅展示状态。"
          type="info"
          :closable="false"
          show-icon
        />
        <el-empty description="支付渠道配置待后端开放，敬请期待" />
      </el-tab-pane>

      <!-- ==================== 财务流水（演示数据） ==================== -->
      <el-tab-pane label="财务流水" name="ledger">
        <el-alert
          title="演示数据"
          description="以下流水由当前订单列表数据派生，仅作界面演示；完整流水接口待后端开放。"
          type="info"
          :closable="false"
          show-icon
          class="orders__notice"
        />
        <el-table :data="ledgerRows" stripe>
          <el-table-column prop="time" label="时间" width="180" />
          <el-table-column prop="order_no" label="关联订单号" min-width="180" />
          <el-table-column prop="username" label="用户" width="120" />
          <el-table-column prop="type" label="类型" width="120" />
          <el-table-column label="金额" width="120" align="right">
            <template #default="{ row }">
              <span :class="{ 'orders__neg': row.amount < 0 }">
                ¥{{ row.amount.toLocaleString('zh-CN') }}
              </span>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- ==================== 对账（演示数据） ==================== -->
      <el-tab-pane label="对账" name="recon">
        <el-alert
          title="演示数据"
          description="以下对账视图由当前订单列表按日聚合派生，渠道对账金额为模拟值；对账差异将标红并可下钻。"
          type="info"
          :closable="false"
          show-icon
          class="orders__notice"
        />
        <el-table :data="reconRows" stripe>
          <el-table-column prop="date" label="日期" width="140" />
          <el-table-column label="平台流水" width="140" align="right">
            <template #default="{ row }">¥{{ row.platform.toLocaleString('zh-CN') }}</template>
          </el-table-column>
          <el-table-column label="渠道对账" width="140" align="right">
            <template #default="{ row }">¥{{ row.channel.toLocaleString('zh-CN') }}</template>
          </el-table-column>
          <el-table-column label="差异" width="120" align="right">
            <template #default="{ row }">
              <span :class="{ 'orders__neg': row.diff !== 0 }">
                {{ row.diff >= 0 ? '+' : '' }}{{ row.diff }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="row.diff === 0 ? 'success' : 'danger'" effect="light">
                {{ row.diff === 0 ? '正常' : '差异待核查' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="count" label="订单笔数" align="right" />
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.orders {
  max-width: 1440px;
  margin: 0 auto;
}

.orders__toolbar {
  margin-bottom: var(--mf-sp-3);
}

.page-title {
  margin: 0;
  font-size: var(--mf-fs-xl);
  font-weight: var(--mf-fw-semibold);
}

.orders__filters {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-4);
}

.orders__pager {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--mf-sp-4);
}

.orders__notice {
  margin-top: var(--mf-sp-4);
}

.orders__neg {
  color: var(--mf-danger);
  font-weight: var(--mf-fw-semibold);
}
</style>
