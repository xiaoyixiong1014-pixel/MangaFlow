<!--
  模型配置中心（文件级注释，P-04）。
  四能力 Tab（文本/图像/视频/语音）浏览模型目录（名称/默认/场景/计价/文档/健康）；
  全局默认与项目覆盖的说明条；自有 Key 管理区（渠道列表 / 添加 / 测试连通 / 删除）。
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as catalogApi from '@/api/catalog'
import type { Catalog, Channel, LogicalModel } from '@/types'

// ---------- 目录 ----------
/** 目录数据 */
const catalog = ref<Catalog | null>(null)
/** 目录加载状态 */
const loading = ref(true)

/** 能力 Tab（key 与后端 capability 一致） */
const capabilityTabs = [
  { value: 'llm', label: '文本' },
  { value: 'image', label: '图像' },
  { value: 'video', label: '视频' },
  { value: 'tts', label: '语音' },
] as const

/** 当前能力 Tab */
const capability = ref<'llm' | 'image' | 'video' | 'tts'>('llm')

/**
 * 当前能力下的模型列表。
 */
const models = computed<LogicalModel[]>(() => {
  const group = catalog.value?.capabilities.find((g) => g.capability === capability.value)
  return group?.models ?? []
})

/** 健康状态中文与颜色映射 */
const healthLabels: Record<string, { label: string; type: 'success' | 'warning' | 'danger' | 'info' }> = {
  healthy: { label: '健康', type: 'success' },
  degraded: { label: '降级', type: 'warning' },
  down: { label: '不可用', type: 'danger' },
  unknown: { label: '未知', type: 'info' },
}

/**
 * 计价规则 JSONB 转展示行（内部函数）：对象转 key: value 列表，标量直接单行。
 * @param price 计价规则原始值
 * @returns 形如 "每张: 4 积分" 的字符串数组
 */
function priceRows(price: unknown): string[] {
  if (price == null) return []
  if (typeof price === 'object' && !Array.isArray(price)) {
    return Object.entries(price as Record<string, unknown>).map(([k, v]) => `${k}: ${String(v)}`)
  }
  return [String(price)]
}

// ---------- 自有渠道 ----------
/** 我的渠道列表 */
const channels = ref<Channel[]>([])
/** 渠道加载状态 */
const channelsLoading = ref(false)
/** 添加渠道对话框可见性 */
const channelDialogVisible = ref(false)
/** 提交中 */
const submitting = ref(false)
/** 测试中的渠道 ID 集合 */
const testingIds = ref<Set<number>>(new Set())
/** 渠道表单 */
const channelForm = reactive({ base_url: '', api_key: '' })

/**
 * 拉取我的渠道列表。
 */
async function fetchChannels(): Promise<void> {
  channelsLoading.value = true
  try {
    channels.value = await catalogApi.myChannels()
  } catch {
    // 错误已由拦截器提示
  } finally {
    channelsLoading.value = false
  }
}

/**
 * 提交添加渠道（protocol 固定 openai）。
 */
async function submitChannel(): Promise<void> {
  if (!channelForm.base_url.trim() || !channelForm.api_key.trim()) {
    ElMessage.warning('接口地址与 API Key 均为必填')
    return
  }
  if (!/^https?:\/\//.test(channelForm.base_url.trim())) {
    ElMessage.warning('接口地址需以 http:// 或 https:// 开头')
    return
  }
  submitting.value = true
  try {
    await catalogApi.addChannel({
      protocol: 'openai',
      base_url: channelForm.base_url.trim(),
      api_key: channelForm.api_key.trim(),
    })
    ElMessage.success('渠道已添加（Key 已加密保存）')
    channelDialogVisible.value = false
    channelForm.base_url = ''
    channelForm.api_key = ''
    await fetchChannels()
  } catch {
    // 400 base_url 非法等错误已由拦截器提示
  } finally {
    submitting.value = false
  }
}

/**
 * 测试渠道连通性（成功显示 ok 与健康状态）。
 * @param ch 渠道
 */
async function testChannel(ch: Channel): Promise<void> {
  testingIds.value = new Set(testingIds.value).add(ch.id)
  try {
    const resp = await catalogApi.testChannel(ch.id)
    if (resp.ok) ElMessage.success(`连通正常（健康状态：${healthLabels[resp.health_status]?.label ?? resp.health_status}）`)
    else ElMessage.error(resp.error || '连通失败')
    await fetchChannels()
  } catch {
    // 错误已由拦截器提示
  } finally {
    const next = new Set(testingIds.value)
    next.delete(ch.id)
    testingIds.value = next
  }
}

/**
 * 删除渠道（二次确认）。
 * @param ch 渠道
 */
async function removeChannel(ch: Channel): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除渠道 ${ch.base_url} 吗？`, '删除渠道', { type: 'warning' })
  } catch {
    return
  }
  try {
    await catalogApi.deleteChannel(ch.id)
    ElMessage.success('已删除')
    await fetchChannels()
  } catch {
    // 错误已由拦截器提示
  }
}

// 首次进入：并行拉取目录与渠道
onMounted(async () => {
  try {
    const [cat] = await Promise.all([catalogApi.modelsCatalog(), fetchChannels()])
    catalog.value = cat
  } catch {
    // 错误已由拦截器提示
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="page-container">
    <div class="page-head">
      <h2 class="page-title">模型配置</h2>
      <!-- 效果对比入口（PL-11，Task 10.3） -->
      <el-button type="primary" plain @click="$router.push('/models/compare')">效果对比 →</el-button>
    </div>

    <!-- 能力 Tab -->
    <el-tabs v-model="capability" class="cap-tabs">
      <el-tab-pane v-for="t in capabilityTabs" :key="t.value" :label="t.label" :name="t.value" />
    </el-tabs>

    <!-- 配置层级说明条 -->
    <el-alert type="info" :closable="false" class="layer-tip" show-icon>
      <template #title>
        配置层级：任务级 &gt; 本项目覆盖 &gt; 全局默认。项目内可在创作工作区为当前项目单独覆盖模型；未覆盖时使用下方官方默认模型。
      </template>
    </el-alert>

    <!-- 目录骨架 -->
    <div v-if="loading" class="model-list">
      <div v-for="i in 3" :key="i" class="model-card">
        <div style="padding: 16px">
          <div class="mf-skeleton mf-skeleton--text" style="width: 40%; margin-bottom: 10px"></div>
          <div class="mf-skeleton mf-skeleton--text" style="width: 80%"></div>
        </div>
      </div>
    </div>

    <!-- 模型卡列表 -->
    <div v-else-if="models.length" class="model-list">
      <div v-for="m in models" :key="m.id" class="model-card">
        <div class="model-head">
          <span class="model-name">{{ m.name }}</span>
          <el-tag v-if="m.is_default" type="primary" effect="soft" size="small">官方默认</el-tag>
          <el-tag
            v-if="healthLabels[m.health]"
            :type="healthLabels[m.health]?.type ?? 'info'"
            effect="plain"
            size="small"
          >渠道{{ healthLabels[m.health]?.label ?? m.health }}</el-tag>
        </div>
        <p class="scene" v-if="m.scene_desc">{{ m.scene_desc }}</p>
        <div class="meta">
          <div class="price" v-if="priceRows(m.price_per_unit).length">
            <span class="meta-label">计价</span>
            <span class="price-rows">
              <span v-for="(r, i) in priceRows(m.price_per_unit)" :key="i" class="price-row">{{ r }}</span>
            </span>
          </div>
          <a v-if="m.doc_url" :href="m.doc_url" target="_blank" rel="noopener noreferrer" class="doc-link">
            查看文档 ↗
          </a>
        </div>
      </div>
    </div>
    <el-empty v-else description="该能力下暂无可用模型" />

    <!-- 自有 Key 管理 -->
    <section class="channel-section">
      <div class="section-head">
        <h3 class="section-title">自有 Key 管理</h3>
        <el-button type="primary" @click="channelDialogVisible = true">添加渠道</el-button>
      </div>
      <el-alert type="info" :closable="false" show-icon class="key-tip">
        <template #title>
          使用自有 Key 时免生成积分，仍按基础费计费（存储 / 带宽 / 编排）；Key 服务端加密保存，永不明文展示。
        </template>
      </el-alert>

      <el-empty
        v-if="!channelsLoading && channels.length === 0"
        description="暂无自有渠道，内置官方模型已可用"
        :image-size="80"
      />
      <el-table v-else :data="channels" v-loading="channelsLoading" class="channel-table">
        <el-table-column prop="protocol" label="协议" width="90" />
        <el-table-column prop="base_url" label="接口地址" min-width="220" show-overflow-tooltip />
        <el-table-column prop="api_key" label="API Key" width="160">
          <template #default="{ row }">
            <code class="masked-key">{{ row.api_key || '••••••' }}</code>
          </template>
        </el-table-column>
        <el-table-column label="健康" width="110">
          <template #default="{ row }">
            <el-tag
              v-if="healthLabels[row.health_status]"
              :type="healthLabels[row.health_status]?.type ?? 'info'"
              size="small"
              effect="plain"
            >{{ healthLabels[row.health_status]?.label ?? row.health_status }}</el-tag>
            <span v-else>{{ row.health_status }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              size="small"
              :loading="testingIds.has(row.id)"
              @click="testChannel(row)"
            >测试连通</el-button>
            <el-button link type="danger" size="small" @click="removeChannel(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <!-- 添加渠道对话框 -->
    <el-dialog v-model="channelDialogVisible" title="添加自有渠道" width="480px">
      <el-form label-width="90px" label-position="left">
        <el-form-item label="协议">
          <el-input model-value="openai" disabled />
        </el-form-item>
        <el-form-item label="接口地址" required>
          <el-input v-model="channelForm.base_url" placeholder="https://api.example.com/v1" />
        </el-form-item>
        <el-form-item label="API Key" required>
          <el-input
            v-model="channelForm.api_key"
            type="password"
            show-password
            placeholder="sk-..."
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="channelDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitChannel">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-3);
}
.cap-tabs :deep(.el-tabs__header) {
  margin-bottom: var(--mf-sp-4);
}
.layer-tip {
  margin-bottom: var(--mf-sp-5);
  border-radius: var(--mf-r-ctl);
}
.model-list {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
}
.model-card {
  border-radius: var(--mf-r-card);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  padding: var(--mf-sp-4) var(--mf-sp-5);
  transition: box-shadow var(--mf-dur) var(--mf-ease);
}
.model-card:hover {
  box-shadow: var(--mf-shadow-pop);
}
.model-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.model-name {
  font-weight: var(--mf-fw-semibold);
  font-size: var(--mf-fs-md);
}
.scene {
  margin: var(--mf-sp-2) 0;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.meta {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-5);
  flex-wrap: wrap;
}
.meta-label {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
  margin-right: var(--mf-sp-2);
}
.price {
  display: flex;
  align-items: center;
}
.price-rows {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.price-row {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  font-family: var(--mf-mono);
}
.doc-link {
  color: var(--mf-brand);
  font-size: var(--mf-fs-sm);
  text-decoration: none;
}
.doc-link:hover {
  text-decoration: underline;
}
.channel-section {
  margin-top: var(--mf-sp-8);
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--mf-sp-3);
}
.section-title {
  margin: 0;
  font-size: var(--mf-fs-lg);
  font-weight: var(--mf-fw-semibold);
}
.key-tip {
  margin-bottom: var(--mf-sp-3);
  border-radius: var(--mf-r-ctl);
}
.channel-table {
  width: 100%;
  border-radius: var(--mf-r-ctl);
  overflow: hidden;
}
.masked-key {
  font-family: var(--mf-mono);
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-2);
  background: var(--mf-surface-2);
  padding: 2px 8px;
  border-radius: 6px;
}
</style>
