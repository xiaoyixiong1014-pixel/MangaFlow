<!--
  剧本创作页 · 左侧参数栏（文件级注释，redesign-script-workspace Task 1）。

  只读展示当前项目创作参数（spec D5：参数栏只读 + 跳转项目设置）：
  - 数据源：GET /api/projects/{id}（projectsApi.get → ProjectDetail，对应后端
    ProjectOut：genre/style/fidelity/aspect_ratio/target_duration）+ 分集/阶段摘要
    （pipelineApi.getPipelineStatus → ScriptPipelineStatusOut）；
  - 忠实度三档语义提示：free=AI 可大胆增改反转 / outline=AI 按大纲扩写 /
    faithful=仅做最保守扩写；
  - 预估消耗为静态估算（集数 × 单集固定单价；项目暂无成本估算工具函数，
    后续接入计费域后替换）；
  - 三态：加载骨架 / 接口失败 el-alert 可重试 / 空值占位「—」；
  - 底部「修改参数」按钮：路由表（src/router/index.ts，已查证）尚无独立项目
    设置页，暂以提示占位，待后续任务交付设置页后替换为 router.push 跳转。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import * as projectsApi from '@/api/projects'
import * as pipelineApi from '@/api/scriptPipeline'
import type { ScriptPipelineStatusOut } from '@/api/scriptPipeline'
import type { ProjectDetail } from '@/types'

/** 组件入参 */
const props = defineProps<{
  /** 当前项目 ID（项目详情与流水线状态接口路径参数） */
  projectId: number
}>()

/** 项目详情加载中（骨架屏） */
const loading = ref(true)
/** 项目详情加载失败（展示 el-alert + 重试） */
const loadError = ref(false)
/** 项目详情（ProjectOut 字段来源） */
const project = ref<ProjectDetail | null>(null)
/** 流水线状态（阶段/集数摘要；拉取失败不阻塞参数展示，置 null 后显示「—」） */
const status = ref<ScriptPipelineStatusOut | null>(null)

/** 忠实度档位 → 中文标签 + 语义提示映射（与 NewProjectDialog 三档一致） */
const FIDELITY_META: Record<string, { label: string; hint: string }> = {
  free: { label: '自由发挥', hint: 'AI 可大胆增改反转' },
  outline: { label: '遵循大纲', hint: 'AI 按大纲扩写' },
  faithful: { label: '严格忠实', hint: '仅做最保守扩写' },
}

/** script_phase → 中文阶段标签（D4：characters 取值不变，UI 改称「故事设定」） */
const PHASE_LABELS: Record<string, string> = {
  idea: '创意',
  characters: '故事设定',
  outline: '分集大纲',
  body: '剧本正文',
}

/**
 * 忠实度中文标签。
 * @param fidelity 忠实度档位标识（可空）
 * @returns 中文标签；未设置返回「—」
 */
function fidelityLabel(fidelity?: string | null): string {
  if (!fidelity) return '—'
  return FIDELITY_META[fidelity]?.label ?? fidelity
}

/**
 * 忠实度语义提示文案。
 * @param fidelity 忠实度档位标识（可空）
 * @returns 语义提示；未设置返回「—」
 */
function fidelityHint(fidelity?: string | null): string {
  if (!fidelity) return '—'
  return FIDELITY_META[fidelity]?.hint ?? '—'
}

/**
 * 当前阶段中文标签（null = 快速模式 / 未进入流水线）。
 */
const phaseLabel = computed<string>(() => {
  const p = status.value?.script_phase
  if (p == null) return '快速模式 / 未开始'
  return PHASE_LABELS[p] ?? p
})

/**
 * 总集数（流水线摘要 episode_count；状态缺失时 null → 模板显示「—」）。
 */
const episodeCount = computed<number | null>(
  () => status.value?.summary?.episode_count ?? null,
)

/**
 * 已生成正文集数（流水线摘要 body_count）。
 * 说明：ScriptPipelineStatusOut.summary 无「已确认大纲」计数，此处以已生成
 * 正文集数近似展示创作进度。
 */
const bodyCount = computed<number | null>(() => status.value?.summary?.body_count ?? null)

/** 单集静态估算单价（积分）。项目暂无成本估算接口，采用「集数 × 单集固定单价」静态展示 */
const PER_EPISODE_COST = 30

/**
 * 预估消耗文案（集数 × 单集固定单价的静态估算；无集数时显示「—」）。
 */
const costEstimate = computed<string>(() => {
  const n = episodeCount.value
  if (!n || n <= 0) return '—'
  return `约 ${n * PER_EPISODE_COST} 积分（静态估算）`
})

/**
 * 拉取项目详情与流水线状态。
 * 项目详情失败进入错误态（可重试）；流水线状态失败静默降级（进度区显示「—」）。
 * @returns 无
 * @throws 接口错误已由 client 拦截器统一弹窗提示
 */
async function load(): Promise<void> {
  loading.value = true
  loadError.value = false
  try {
    project.value = await projectsApi.get(props.projectId)
  } catch {
    loadError.value = true
    return
  } finally {
    loading.value = false
  }
  // 流水线状态独立拉取：失败不阻塞参数展示（进度区占位「—」）
  try {
    status.value = await pipelineApi.getPipelineStatus(props.projectId)
  } catch {
    status.value = null
  }
}

/**
 * 点击「修改参数」：当前路由表尚无独立项目设置页（已查证 src/router/index.ts），
 * 暂以提示占位；后续任务交付设置页后替换为 router.push 跳转。
 * @returns 无
 */
function openSettings(): void {
  ElMessage.info('项目设置页即将开放，当前可在项目列表管理项目')
}

onMounted(load)
</script>

<template>
  <el-card shadow="never" class="param-card">
    <template #header>
      <div class="param-head">项目参数</div>
    </template>

    <!-- 加载骨架 -->
    <el-skeleton v-if="loading" :rows="6" animated />

    <!-- 失败态：可重试 -->
    <el-alert v-else-if="loadError" type="error" :closable="false" title="项目参数加载失败">
      <el-button size="small" type="primary" plain @click="load">重试</el-button>
    </el-alert>

    <!-- 就绪态：只读参数展示（空值占位「—」） -->
    <template v-else>
      <div class="param-row">
        <span class="param-label">体裁</span>
        <span class="param-value">{{ project?.genre || '—' }}</span>
      </div>
      <div class="param-row">
        <span class="param-label">视觉风格</span>
        <span class="param-value">{{ project?.style || '—' }}</span>
      </div>
      <div class="param-row">
        <span class="param-label">忠实度</span>
        <span class="param-value">
          {{ fidelityLabel(project?.fidelity) }}
          <span class="param-hint">{{ fidelityHint(project?.fidelity) }}</span>
        </span>
      </div>
      <div class="param-row">
        <span class="param-label">输出比例</span>
        <span class="param-value">{{ project?.aspect_ratio || '—' }}</span>
      </div>
      <div class="param-row">
        <span class="param-label">目标时长</span>
        <span class="param-value">
          {{ project?.target_duration ? `${project.target_duration} 秒` : '—' }}
        </span>
      </div>

      <el-divider class="param-divider">进度</el-divider>
      <div class="param-row">
        <span class="param-label">当前阶段</span>
        <span class="param-value">{{ phaseLabel }}</span>
      </div>
      <div class="param-row">
        <span class="param-label">集数</span>
        <span class="param-value">{{ episodeCount ? `共 ${episodeCount} 集` : '—' }}</span>
      </div>
      <div class="param-row">
        <span class="param-label">正文进度</span>
        <span class="param-value">{{ bodyCount ? `已生成正文 ${bodyCount} 集` : '—' }}</span>
      </div>
      <div class="param-row">
        <span class="param-label">预估消耗</span>
        <span class="param-value">{{ costEstimate }}</span>
      </div>

      <el-button class="edit-btn" plain @click="openSettings">修改参数</el-button>
    </template>
  </el-card>
</template>

<style scoped>
.param-card {
  align-self: start;
}
.param-head {
  font-weight: var(--mf-fw-semibold);
}
.param-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--mf-sp-2);
  padding: var(--mf-sp-2) 0;
  border-bottom: 1px dashed var(--mf-border);
}
.param-row:last-of-type {
  border-bottom: none;
}
.param-label {
  flex-shrink: 0;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.param-value {
  color: var(--mf-text);
  font-size: var(--mf-fs-sm);
  text-align: right;
  word-break: break-all;
}
.param-hint {
  display: block;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.param-divider {
  margin: var(--mf-sp-3) 0 var(--mf-sp-1);
}
.edit-btn {
  width: 100%;
  margin-top: var(--mf-sp-4);
}
</style>
