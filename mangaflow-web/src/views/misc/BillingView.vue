<!--
  计费中心（文件级注释，P-05）。
  概览卡（可用/冻结/本月消耗 + 低额提醒）+ 三 Tab（积分充值 / 订阅套餐 / 我的订单）+
  积分流水表格（类型中文映射 / 带符号金额 / 分页）。
  支付为模拟流程：创建订单 → 确认 → pay 幂等发放 → 全量刷新。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as billingApi from '@/api/billing'
import { useAuthStore } from '@/stores/auth'
import type { MonthConsumed, Order, Pkg, Wallet, WalletTransactionItem } from '@/types'

const auth = useAuthStore()

// ---------- 概览 ----------
/** 钱包概要 */
const wallet = ref<Wallet | null>(null)
/** 月度消耗列表（取最近月） */
const summary = ref<MonthConsumed[]>([])
/** 加载状态 */
const loading = ref(true)

/** 是否低余额（可用 < 1000） */
const lowBalance = computed(() => (wallet.value?.points_balance ?? 0) < 1000)

/**
 * 最近一月消耗。
 */
const monthConsumed = computed(() => {
  if (summary.value.length === 0) return 0
  return summary.value[summary.value.length - 1]?.consumed ?? 0
})

// ---------- 套餐 / 订单 / 流水 ----------
/** 全部套餐 */
const packages = ref<Pkg[]>([])
/** 积分套餐（非订阅） */
const pointPackages = computed(() => packages.value.filter((p) => !p.subscription))
/** 订阅套餐 */
const subPackages = computed(() => packages.value.filter((p) => p.subscription))

/** 订单列表 */
const orders = ref<Order[]>([])
/** 订单分页 */
const orderPage = ref(1)
const orderTotal = ref(0)
/** 流水列表 */
const transactions = ref<WalletTransactionItem[]>([])
/** 流水分页 */
const txPage = ref(1)
const txTotal = ref(0)
/** 购买/支付中的套餐或订单号（防重复提交） */
const paying = ref('')

/** 订单状态中文与颜色映射 */
const orderStatusMap: Record<string, { label: string; type: 'warning' | 'success' | 'info' | 'danger' }> = {
  pending: { label: '待支付', type: 'warning' },
  paid: { label: '已支付', type: 'success' },
  closed: { label: '已关闭', type: 'info' },
  refunded: { label: '已退款', type: 'danger' },
}

/** 流水类型中文映射 */
const txTypeMap: Record<string, string> = {
  freeze: '冻结',
  consume: '消耗',
  refund: '退回',
  recharge: '充值',
  grant: '赠送',
}

/**
 * 拉取钱包 / 月度汇总 / 套餐。
 */
async function fetchOverview(): Promise<void> {
  loading.value = true
  try {
    const [w, s, pkgs] = await Promise.all([billingApi.wallet(), billingApi.summary(), billingApi.packages()])
    wallet.value = w
    summary.value = s
    packages.value = pkgs
  } catch {
    // 错误已由拦截器提示
  } finally {
    loading.value = false
  }
}

/**
 * 拉取订单列表。
 */
async function fetchOrders(): Promise<void> {
  try {
    const resp = await billingApi.orders({ page: orderPage.value, page_size: 10 })
    orders.value = resp.items
    orderTotal.value = resp.total
  } catch {
    // 错误已由拦截器提示
  }
}

/**
 * 拉取积分流水。
 */
async function fetchTransactions(): Promise<void> {
  try {
    const resp = await billingApi.transactions({ page: txPage.value, page_size: 10 })
    transactions.value = resp.items
    txTotal.value = resp.total
  } catch {
    // 错误已由拦截器提示
  }
}

/**
 * 购买套餐（充值/订阅共用）：创建订单 → 模拟支付确认 → 到账刷新。
 * @param pkg 套餐
 */
async function buyPackage(pkg: Pkg): Promise<void> {
  try {
    const order = await billingApi.recharge(pkg.id)
    try {
      await ElMessageBox.confirm(
        `模拟支付 ¥${pkg.price}（订单号 ${order.order_no}）？`,
        '确认支付',
        { confirmButtonText: '立即支付', cancelButtonText: '取消', type: 'info' },
      )
    } catch {
      // 用户取消：订单留在"待支付"，可稍后到"我的订单"继续支付
      await fetchOrders()
      return
    }
    paying.value = order.order_no
    try {
      const resp = await billingApi.pay(order.order_no)
      ElMessage.success(`支付成功，已到账 ${resp.order.points} 积分`)
      // 刷新钱包 / 顶栏余额 / 订单 / 流水
      await Promise.all([fetchOverview(), fetchOrders(), fetchTransactions(), auth.fetchWallet().catch(() => undefined)])
    } finally {
      paying.value = ''
    }
  } catch {
    // 错误已由拦截器提示
  }
}

/**
 * 支付待支付订单。
 * @param order 订单
 */
async function payOrder(order: Order): Promise<void> {
  paying.value = order.order_no
  try {
    const resp = await billingApi.pay(order.order_no)
    ElMessage.success(`支付成功，已到账 ${resp.order.points} 积分`)
    await Promise.all([fetchOverview(), fetchOrders(), fetchTransactions(), auth.fetchWallet().catch(() => undefined)])
  } catch {
    // 错误已由拦截器提示
  } finally {
    paying.value = ''
  }
}

onMounted(() => {
  fetchOverview()
  fetchOrders()
  fetchTransactions()
})
</script>

<template>
  <div class="page-container">
    <h2 class="page-title">计费与配额</h2>

    <!-- 概览卡 -->
    <div v-loading="loading" class="overview-card">
      <div class="stat main-stat">
        <span class="stat-label">可用积分</span>
        <span class="stat-value">{{ wallet?.points_balance ?? '—' }}</span>
      </div>
      <div class="stat">
        <span class="stat-label">冻结积分</span>
        <span class="stat-value small">{{ wallet?.frozen_points ?? '—' }}</span>
      </div>
      <div class="stat">
        <span class="stat-label">本月消耗</span>
        <span class="stat-value small">{{ monthConsumed }}</span>
      </div>
    </div>
    <el-alert
      v-if="lowBalance && !loading"
      title="积分不足，请及时充值"
      type="warning"
      show-icon
      :closable="false"
      class="low-alert"
    />

    <!-- 三个 Tab -->
    <el-tabs class="bill-tabs">
      <!-- 积分充值 -->
      <el-tab-pane label="积分充值">
        <div class="pkg-grid">
          <div v-for="p in pointPackages" :key="p.id" class="pkg-card">
            <div class="pkg-points">{{ p.points.toLocaleString() }} 积分</div>
            <div class="pkg-price">¥ {{ p.price }}</div>
            <el-button
              type="primary"
              round
              :loading="paying !== '' && paying.startsWith('p')"
              @click="buyPackage(p)"
            >立即购买</el-button>
          </div>
          <el-empty v-if="pointPackages.length === 0" description="暂无积分套餐" :image-size="80" />
        </div>
      </el-tab-pane>

      <!-- 订阅套餐 -->
      <el-tab-pane label="订阅套餐">
        <div class="pkg-grid">
          <div v-for="p in subPackages" :key="p.id" class="pkg-card sub">
            <div class="pkg-name">{{ p.name }}</div>
            <div class="pkg-points">{{ p.points.toLocaleString() }} 积分 / 月</div>
            <div class="pkg-price">¥ {{ p.price }} / 月</div>
            <el-button type="primary" round :disabled="paying !== ''" @click="buyPackage(p)">订阅</el-button>
          </div>
          <el-empty v-if="subPackages.length === 0" description="暂无订阅套餐" :image-size="80" />
        </div>
        <p class="soft-tip">配额耗尽时已提交的任务会继续完成，仅新任务受限。</p>
      </el-tab-pane>

      <!-- 我的订单 -->
      <el-tab-pane label="我的订单">
        <el-table :data="orders" class="table">
          <el-table-column prop="order_no" label="订单号" min-width="200" show-overflow-tooltip />
          <el-table-column label="类型" width="100">
            <template #default="{ row }">{{ row.product_type === 'subscription' ? '订阅' : '积分' }}</template>
          </el-table-column>
          <el-table-column label="金额" width="100">
            <template #default="{ row }">¥{{ row.amount }}</template>
          </el-table-column>
          <el-table-column prop="points" label="积分" width="100" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="orderStatusMap[row.status]?.type ?? 'info'" size="small">
                {{ orderStatusMap[row.status]?.label ?? row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="时间" width="170">
            <template #default="{ row }">{{ (row.created_at ?? '').slice(0, 19).replace('T', ' ') }}</template>
          </el-table-column>
          <el-table-column label="操作" width="110" fixed="right">
            <template #default="{ row }">
              <el-button
                v-if="row.status === 'pending'"
                link
                type="primary"
                size="small"
                :disabled="paying !== ''"
                @click="payOrder(row)"
              >去支付</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="pager">
          <el-pagination
            v-model:current-page="orderPage"
            :total="orderTotal"
            :page-size="10"
            layout="prev, pager, next"
            @current-change="fetchOrders"
          />
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- 积分流水 -->
    <section class="tx-section">
      <h3 class="section-title">积分流水</h3>
      <el-table :data="transactions" class="table">
        <el-table-column label="类型" width="100">
          <template #default="{ row }">{{ txTypeMap[row.type] ?? row.type }}</template>
        </el-table-column>
        <el-table-column label="变动" width="120">
          <template #default="{ row }">
            <span :class="row.amount >= 0 ? 'amount-plus' : 'amount-minus'">
              {{ row.amount >= 0 ? '+' : '' }}{{ row.amount }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="remark" label="备注" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ row.remark || '—' }}</template>
        </el-table-column>
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ (row.created_at ?? '').slice(0, 19).replace('T', ' ') }}</template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          v-model:current-page="txPage"
          :total="txTotal"
          :page-size="10"
          layout="prev, pager, next"
          @current-change="fetchTransactions"
        />
      </div>
    </section>
  </div>
</template>

<style scoped>
.overview-card {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-10);
  padding: var(--mf-sp-6) var(--mf-sp-8);
  border-radius: var(--mf-r-card);
  background:
    linear-gradient(135deg, var(--mf-brand-soft), transparent 55%),
    var(--mf-surface);
  border: 1px solid var(--mf-border);
  min-height: 110px;
}
.stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.stat-label {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.stat-value {
  font-size: 40px;
  font-weight: var(--mf-fw-bold);
  color: var(--mf-brand);
  font-family: var(--mf-mono);
  line-height: 1.1;
}
.stat-value.small {
  font-size: 26px;
  color: var(--mf-text);
}
.low-alert {
  margin-top: var(--mf-sp-3);
  border-radius: var(--mf-r-ctl);
}
.bill-tabs {
  margin-top: var(--mf-sp-5);
}
.pkg-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: var(--mf-sp-4);
  padding-top: var(--mf-sp-2);
}
.pkg-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--mf-sp-2);
  padding: var(--mf-sp-6) var(--mf-sp-4);
  border-radius: var(--mf-r-card);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  transition: transform var(--mf-dur) var(--mf-ease), box-shadow var(--mf-dur) var(--mf-ease);
}
.pkg-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--mf-shadow-pop);
}
.pkg-card.sub {
  background: linear-gradient(160deg, var(--mf-brand2-soft), var(--mf-surface) 60%);
}
.pkg-name {
  font-weight: var(--mf-fw-semibold);
}
.pkg-points {
  font-size: var(--mf-fs-xl);
  font-weight: var(--mf-fw-bold);
  color: var(--mf-brand);
}
.pkg-price {
  font-size: var(--mf-fs-lg);
  color: var(--mf-text);
}
.soft-tip {
  margin-top: var(--mf-sp-4);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.table {
  width: 100%;
  border-radius: var(--mf-r-ctl);
  overflow: hidden;
}
.tx-section {
  margin-top: var(--mf-sp-7);
}
.section-title {
  margin: 0 0 var(--mf-sp-3);
  font-size: var(--mf-fs-lg);
  font-weight: var(--mf-fw-semibold);
}
.amount-plus {
  color: var(--mf-ok);
  font-family: var(--mf-mono);
}
.amount-minus {
  color: var(--mf-danger);
  font-family: var(--mf-mono);
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--mf-sp-4);
}
</style>
