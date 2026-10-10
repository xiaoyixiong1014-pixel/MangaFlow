<!--
  模型效果对比页（文件级注释，PL-11，Task 10.3）。
  路由 /models/compare：选择能力（文本/图像）+ 输入提示词 + 勾选 2~4 个平台渠道
  → 调 POST /api/models/compare 并排展示各渠道生成结果（文本直接渲染、
  图像展示图片预览），含耗时与失败原因；单渠道失败不影响其他渠道。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import * as catalogApi from '@/api/catalog'
import type { Channel, CompareItem, PlatformChannel } from '@/types'

// ---------- 表单状态 ----------
/** 对比能力：llm 文本 / image 图像 */
const capability = ref<'llm' | 'image'>('llm')
/** 对比提示词 */
const prompt = ref('')
/** 选中的渠道 ID（1~4 个） */
const selectedIds = ref<number[]>([])
/** 平台渠道选项（目录接口返回） */
const platformChannels = ref<PlatformChannel[]>([])
/** 本人自有渠道（也可参与对比） */
const myChannels = ref<Channel[]>([])
/** 目录加载中 */
const loading = ref(true)

/** 能力选项 */
const CAP_OPTIONS = [
  { value: 'llm', label: '文本生成' },
  { value: 'image', label: '图像生成' },
] as const

/** 渠道健康中文映射 */
const HEALTH_LABELS: Record<string, string> = {
  healthy: '健康',
  degraded: '降级',
  down: '不可用',
  unknown: '未知',
}

/** 可选渠道统一选项（平台渠道 + 自有渠道） */
const channelOptions = computed(() => [
  ...platformChannels.value.map((c) => ({
    id: c.id,
    label: `平台 #${c.id} ${c.base_url}`,
    health: c.health_status,
  })),
  ...myChannels.value.map((c) => ({
    id: c.id,
    label: `自有 #${c.id} ${c.base_url}`,
    health: c.health_status,
  })),
])

/** 渠道 ID → 展示名映射（结果卡片标题用） */
const channelLabelOf = computed(() => {
  const map = new Map<number, string>()
  channelOptions.value.forEach((o) => map.set(o.id, o.label))
  return map
})

// ---------- 对比执行 ----------
/** 对比进行中 */
const comparing = ref(false)
/** 对比结果（与所选渠道顺序一致） */
const results = ref<CompareItem[]>([])
/** 已执行过的能力（结果区渲染文本/图像的依据） */
const resultCapability = ref<'llm' | 'image'>('llm')

/**
 * 首次进入：并行拉取模型目录（平台渠道）与自有渠道。
 * @returns 无
 */
async function loadChannels(): Promise<void> {
  loading.value = true
  try {
    const [catalog, mine] = await Promise.all([
      catalogApi.modelsCatalog(),
      catalogApi.myChannels().catch(() => [] as Channel[]),
    ])
    platformChannels.value = catalog.platform_channels
    myChannels.value = mine
  } catch {
    // 错误已由拦截器提示
  } finally {
    loading.value = false
  }
}

/**
 * 发起效果对比：校验提示词与渠道数量（2~4）后调后端逐渠道生成。
 * @returns 无
 */
async function runCompare(): Promise<void> {
  if (!prompt.value.trim()) {
    ElMessage.warning('请先输入对比提示词')
    return
  }
  if (selectedIds.value.length < 2) {
    ElMessage.warning('请至少选择 2 个渠道进行对比')
    return
  }
  if (selectedIds.value.length > 4) {
    ElMessage.warning('最多同时对比 4 个渠道')
    return
  }
  comparing.value = true
  results.value = []
  resultCapability.value = capability.value
  try {
    const resp = await catalogApi.compare({
      capability: capability.value,
      prompt: prompt.value.trim(),
      channel_ids: selectedIds.value,
    })
    results.value = resp.results
  } catch {
    // 错误已由拦截器提示
  } finally {
    comparing.value = false
  }
}

onMounted(loadChannels)
</script>

<template>
  <div class="page-container compare-view">
    <h2 class="page-title">模型效果对比</h2>
    <el-alert type="info" :closable="false" class="intro-tip" show-icon>
      <template #title>
        选择能力并勾选 2~4 个渠道，输入同一提示词并排对比各渠道的生成效果与耗时（PL-11）。
      </template>
    </el-alert>

    <!-- 表单区 -->
    <el-card shadow="never" class="form-card">
      <div class="form-row">
        <span class="form-label">对比能力</span>
        <el-radio-group v-model="capability">
          <el-radio-button v-for="c in CAP_OPTIONS" :key="c.value" :value="c.value">
            {{ c.label }}
          </el-radio-button>
        </el-radio-group>
      </div>
      <div class="form-row">
        <span class="form-label">对比渠道</span>
        <el-select
          v-model="selectedIds"
          multiple
          collapse-tags
          :multiple-limit="4"
          placeholder="选择 2~4 个渠道（平台渠道 / 我的自有渠道）"
          style="flex: 1; min-width: 320px"
          v-loading="loading"
        >
          <el-option
            v-for="o in channelOptions"
            :key="o.id"
            :label="`${o.label}（${HEALTH_LABELS[o.health] ?? o.health}）`"
            :value="o.id"
          />
        </el-select>
      </div>
      <div class="form-row form-row--top">
        <span class="form-label">提示词</span>
        <el-input
          v-model="prompt"
          type="textarea"
          :rows="3"
          maxlength="2000"
          show-word-limit
          :placeholder="capability === 'llm' ? '例如：用两句话写一个赛博朋克城市的开场旁白' : '例如：水墨风格的少年剑客站在山巅，云海翻腾'"
          style="flex: 1"
        />
      </div>
      <div class="form-actions">
        <el-button type="primary" :loading="comparing" @click="runCompare">
          {{ comparing ? '对比生成中…' : '开始对比' }}
        </el-button>
        <span class="form-hint">图像生成可能需要数十秒，请耐心等待</span>
      </div>
    </el-card>

    <!-- 结果区：并排卡片 -->
    <div v-if="results.length" class="result-grid">
      <el-card
        v-for="r in results"
        :key="r.channel_id"
        shadow="hover"
        class="result-card"
      >
        <template #header>
          <div class="result-head">
            <span class="result-title">{{ channelLabelOf.get(r.channel_id) ?? `渠道 #${r.channel_id}` }}</span>
            <el-tag size="small" :type="r.ok ? 'success' : 'danger'" effect="light">
              {{ r.ok ? '成功' : '失败' }}
            </el-tag>
            <span class="result-elapsed">{{ r.elapsed_ms }}ms</span>
          </div>
        </template>
        <div v-if="r.ok && resultCapability === 'llm'" class="result-text">{{ r.text }}</div>
        <el-image
          v-else-if="r.ok && resultCapability === 'image' && r.image_url"
          :src="r.image_url"
          :preview-src-list="[r.image_url]"
          fit="contain"
          class="result-image"
          preview-teleported
        />
        <div v-else class="result-error">
          <span aria-hidden="true">⚠️</span>
          {{ r.error || '生成失败' }}
        </div>
      </el-card>
    </div>
    <el-empty
      v-else-if="!comparing"
      description="选择渠道与提示词后点击「开始对比」"
      :image-size="90"
    />
  </div>
</template>

<style scoped>
.compare-view {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-4);
  max-width: 1080px;
  margin: 0 auto;
}
.page-title {
  margin: 0;
}
.intro-tip {
  border-radius: var(--mf-r-ctl);
}
.form-card {
  border-radius: var(--mf-r-card);
}
.form-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  margin-bottom: var(--mf-sp-3);
}
.form-row--top {
  align-items: flex-start;
}
.form-row--top .form-label {
  margin-top: 6px;
}
.form-label {
  flex: none;
  width: 70px;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.form-actions {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.form-hint {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}

/* 结果并排卡片 */
.result-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: var(--mf-sp-4);
  align-items: start;
}
.result-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.result-title {
  font-weight: var(--mf-fw-semibold);
  font-size: var(--mf-fs-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.result-elapsed {
  margin-left: auto;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
  flex: none;
}
.result-text {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.7;
  font-size: var(--mf-fs-sm);
}
.result-image {
  width: 100%;
  max-height: 360px;
  border-radius: var(--mf-r-ctl);
}
.result-error {
  color: var(--el-color-danger);
  font-size: var(--mf-fs-sm);
  line-height: 1.6;
  word-break: break-all;
}
</style>
