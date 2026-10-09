<script setup lang="ts">
/**
 * 文件级注释：A-02 经营分析看板。
 * 顶部时间范围 Segmented（7d/30d/90d）；6 张指标卡；ECharts 双图
 * （收入趋势折线 + 生成健康度柱状三系列）；底部资损告警列表（high 级标红）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { getDashboardAlerts, getDashboardOverview } from '@/api/admin'
import type { AlertItem, DashboardOverview } from '@/types'

/** 可选时间范围 */
const ranges = [
  { label: '近 7 天', value: '7d' },
  { label: '近 30 天', value: '30d' },
  { label: '近 90 天', value: '90d' },
]

/** 当前选中范围 */
const range = ref('7d')
/** 首次加载状态 */
const loading = ref(false)
/** 看板指标 */
const overview = ref<DashboardOverview | null>(null)
/** 资损告警列表 */
const alerts = ref<AlertItem[]>([])

/** 6 张指标卡（由 overview 派生） */
const metrics = computed(() => {
  const d = overview.value
  return [
    { label: '用户总数', value: d ? fmt(d.users_total) : '—', hint: '全平台注册用户' },
    { label: '新增用户', value: d ? fmt(d.users_new) : '—', hint: '所选时间范围内' },
    { label: '总收入', value: d ? `¥${fmt(d.revenue_total)}` : '—', hint: '已支付订单金额' },
    { label: '已支付订单', value: d ? fmt(d.orders_paid) : '—', hint: '支付成功笔数' },
    { label: '积分负债', value: d ? fmt(d.points_liability) : '—', hint: '已发放未消耗积分' },
    { label: '生成任务', value: d ? `${fmt(d.tasks_total)} / 失败 ${fmt(d.tasks_failed)}` : '—', hint: '含图片 / 视频 / TTS' },
  ]
})

/** 收入趋势图实例 */
let revenueChart: echarts.ECharts | null = null
/** 生成健康度图实例 */
let genChart: echarts.ECharts | null = null
/** 图表容器 DOM 引用 */
const revenueRef = ref<HTMLDivElement>()
const genRef = ref<HTMLDivElement>()

/** 失败率（用于健康度副标题展示） */
const failRate = ref('—')

/**
 * 千分位格式化。
 * @param n 数值
 * @returns 千分位字符串
 */
function fmt(n: number): string {
  return n.toLocaleString('zh-CN')
}

/**
 * 拉取看板数据并刷新图表。
 * @returns 无
 * @throws 无（接口错误由拦截器统一提示，页面保留旧数据）
 */
async function fetchAll() {
  loading.value = true
  try {
    const [data, alertList] = await Promise.all([
      getDashboardOverview(range.value),
      getDashboardAlerts(),
    ])
    overview.value = data
    alerts.value = alertList ?? []

    const total = data.tasks_total
    failRate.value = total > 0 ? `${((data.tasks_failed / total) * 100).toFixed(1)}%` : '0%'

    renderCharts(data)
  } catch {
    /* 局部降级：保留旧数据 */
  } finally {
    loading.value = false
  }
}

/**
 * 渲染（或更新）两张 ECharts 图表。
 * @param data 看板聚合数据
 * @returns 无
 */
function renderCharts(data: DashboardOverview) {
  const axis = data.revenue_by_day.map((i) => i.date)
  const revenueSeries = data.revenue_by_day.map((i) => i.amount)
  const dates = data.generation_by_day.map((i) => i.date)

  // 收入趋势折线
  if (revenueRef.value) {
    if (!revenueChart) {
      revenueChart = echarts.init(revenueRef.value)
    }
    revenueChart.setOption({
      tooltip: { trigger: 'axis' },
      grid: { left: 48, right: 16, top: 32, bottom: 28 },
      xAxis: { type: 'category', data: axis, boundaryGap: false },
      yAxis: { type: 'value', name: '元' },
      series: [
        {
          name: '收入',
          type: 'line',
          smooth: true,
          data: revenueSeries,
          areaStyle: { opacity: 0.12 },
          itemStyle: { color: '#12B3A6' },
        },
      ],
    })
  }

  // 生成健康度柱状（图片 / 视频 / TTS 三系列）
  if (genRef.value) {
    if (!genChart) {
      genChart = echarts.init(genRef.value)
    }
    genChart.setOption({
      tooltip: { trigger: 'axis' },
      legend: { top: 0 },
      grid: { left: 48, right: 16, top: 36, bottom: 28 },
      xAxis: { type: 'category', data: dates },
      yAxis: { type: 'value', name: '任务数' },
      series: [
        { name: '图片', type: 'bar', stack: 'total', data: data.generation_by_day.map((i) => i.image), itemStyle: { color: '#12B3A6' } },
        { name: '视频', type: 'bar', stack: 'total', data: data.generation_by_day.map((i) => i.video), itemStyle: { color: '#7C6BF0' } },
        { name: 'TTS', type: 'bar', stack: 'total', data: data.generation_by_day.map((i) => i.tts), itemStyle: { color: '#3B73C4' } },
      ],
    })
  }
}

/**
 * 浏览器窗口尺寸变化时同步图表尺寸。
 * @returns 无
 */
function handleResize() {
  revenueChart?.resize()
  genChart?.resize()
}

onMounted(() => {
  void fetchAll()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  revenueChart?.dispose()
  genChart?.dispose()
  revenueChart = null
  genChart = null
})

// 切换时间范围自动刷新
watch(range, () => {
  void fetchAll()
})

/** 告警级别 → 提示类型映射 */
function alertType(level: string): 'error' | 'warning' | 'info' {
  if (level === 'high') return 'error'
  if (level === 'medium') return 'warning'
  return 'info'
}
</script>

<template>
  <div class="dashboard" v-loading="loading">
    <!-- 时间范围切换 -->
    <div class="dashboard__toolbar">
      <h2 class="page-title">经营分析看板</h2>
      <el-segmented v-model="range" :options="ranges" data-test="range-segmented" />
    </div>

    <!-- 指标卡 -->
    <div class="metric-grid">
      <div v-for="m in metrics" :key="m.label" class="metric-card">
        <div class="metric-card__label">{{ m.label }}</div>
        <div class="metric-card__value">{{ m.value }}</div>
        <div class="metric-card__hint">{{ m.hint }}</div>
      </div>
    </div>

    <!-- 双图 -->
    <div class="chart-grid">
      <section class="panel">
        <h3 class="panel__title">收入趋势</h3>
        <div ref="revenueRef" class="panel__chart" />
      </section>
      <section class="panel">
        <h3 class="panel__title">
          生成健康度
          <el-tag size="small" type="danger" effect="plain" class="panel__tag">失败率 {{ failRate }}</el-tag>
        </h3>
        <div ref="genRef" class="panel__chart" />
      </section>
    </div>

    <!-- 资损与告警 -->
    <section class="panel">
      <h3 class="panel__title">资损与告警</h3>
      <el-empty v-if="alerts.length === 0" description="暂无告警，一切正常" :image-size="72" />
      <el-alert
        v-for="a in alerts"
        :key="a.created_at + a.type"
        :title="`[${a.type}] ${a.message}`"
        :description="a.created_at"
        :type="alertType(a.level)"
        show-icon
        :closable="false"
        class="alert-item"
      />
    </section>
  </div>
</template>

<style scoped>
.dashboard {
  max-width: 1440px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-5);
}

.dashboard__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.page-title {
  margin: 0;
  font-size: var(--mf-fs-xl);
  font-weight: var(--mf-fw-semibold);
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: var(--mf-sp-4);
}

@media (max-width: 1200px) {
  .metric-grid {
    grid-template-columns: repeat(3, 1fr);
  }
}

.metric-card {
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-card);
  padding: var(--mf-sp-4);
}

.metric-card__label {
  font-size: var(--mf-fs-sm);
  color: var(--mf-text-3);
}

.metric-card__value {
  margin-top: var(--mf-sp-2);
  font-size: var(--mf-fs-2xl);
  font-weight: var(--mf-fw-bold);
  color: var(--mf-text);
}

.metric-card__hint {
  margin-top: var(--mf-sp-1);
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}

.chart-grid {
  display: grid;
  grid-template-columns: 3fr 2fr;
  gap: var(--mf-sp-5);
}

@media (max-width: 1100px) {
  .chart-grid {
    grid-template-columns: 1fr;
  }
}

.panel {
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-card);
  padding: var(--mf-sp-5);
}

.panel__title {
  margin: 0 0 var(--mf-sp-3);
  font-size: var(--mf-fs-lg);
  font-weight: var(--mf-fw-semibold);
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}

.panel__chart {
  height: 300px;
  width: 100%;
}

.alert-item {
  margin-bottom: var(--mf-sp-2);
}
</style>
