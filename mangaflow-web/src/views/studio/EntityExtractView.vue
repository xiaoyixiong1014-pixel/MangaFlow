<!--
  实体提取确认页（文件级注释）。

  对应原型 S-03，路由 /projects/:id/script/extract（剧本阶段子页）。
  功能：
  - 进入自动调 script.extract()（规则 + LLM 双路），进行中展示伪进度条；
  - 完成 fence 结果后分三个 Tab（角色/场景/道具）以表格勾选（默认全选）、
    展开行编辑 name/description/visual_prompt，别名 el-tag 动态增删；
  - confidence < 0.7 的行标黄并挂「低置信」Tag；
  - 冲突裁决区：fences.conflicts 每条 el-card 候选名 radio 必选（保留该名、
    其余候选并入别名）；未裁决完时「生成资产卡」按钮禁用（RD-06 阻断提交）；
  - 底部实时统计将生成的角色/场景/道具数量与预估积分（每张 10 积分）；
  - 确认走 confirmExtract(勾选项)，成功跳 /projects/:id/assets。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { TableInstance } from 'element-plus'
import * as studioApi from '@/api/studio'
import { useProjectStore } from '@/stores/project'
import type { ExtractedEntity, ExtractResult, ScriptOut } from '@/types'

const route = useRoute()
const router = useRouter()
const projectStore = useProjectStore()

/** 当前项目 ID（取路由参数） */
const projectId = computed(() => Number(route.params.id))

/** 当前选中剧集 ID（S-06 全局剧集切换器；null = 默认剧集/全部，抽取聚合全部剧本行） */
const currentEpisodeId = computed(() => projectStore.currentEpisodeId)

/** 当前剧集条目（展示集名；null = 默认剧集上下文） */
const currentEpisode = computed(
  () => projectStore.currentProject?.episodes.find((e) => e.id === currentEpisodeId.value) ?? null,
)

/** 当前抽取范围文案（顶部统计行用） */
const scopeLabel = computed(() =>
  currentEpisode.value ? `第${currentEpisode.value.sequence}集 · ${currentEpisode.value.title}` : '全部剧集',
)

/** 项目画风（风格资产将按它自动创建；抽取不产风格，确认入池时落池） */
const projectStyle = computed(() => projectStore.currentProject?.style ?? '')

// 切换全局剧集：自动按新范围重新抽取（按集工作流的核心联动）
watch(currentEpisodeId, () => {
  if (extracted.value || extracting.value) void runExtract()
})

// ============================================================
// 提取流程状态
// ============================================================

/** 提取进行中 */
const extracting = ref(false)
/** 伪进度百分比（完成前最高推进到 95，等待接口返回后置 100） */
const fakeProgress = ref(0)
/** 伪进度定时器句柄 */
let progressTimer: ReturnType<typeof setInterval> | null = null
/** 是否已完成过一次提取（决定展示结果区还是引导态） */
const extracted = ref(false)
/** 提取结果（含 conflicts） */
const result = ref<ExtractResult | null>(null)
/** 上次提取返回的剧本（用于「重新提取」前校验剧本存在） */
let scriptOut: ScriptOut | null = null

/** 可编辑实体行（在 ExtractedEntity 基础上加勾选、提示词与别名编辑态） */
interface EditableEntity extends ExtractedEntity {
  /** 是否勾选生成资产（默认全选） */
  checked: boolean
  /** 视觉提示词（缺省模板预填，可编辑） */
  visual_prompt: string
}

/** 三类可编辑实体列表 */
const characters = ref<EditableEntity[]>([])
const scenes = ref<EditableEntity[]>([])
const props = ref<EditableEntity[]>([])

/** 三个表格引用（用于默认全选） */
const charTable = ref<TableInstance | null>(null)
const sceneTable = ref<TableInstance | null>(null)
const propTable = ref<TableInstance | null>(null)

/**
 * 后端实体条目转可编辑行（visual_prompt 缺省用模板预填，用户可改）。
 * @param e 抽取实体
 * @param kind 实体类别（决定提示词模板文案）
 * @returns 可编辑行
 */
function toEditable(e: ExtractedEntity, kind: 'character' | 'scene' | 'prop'): EditableEntity {
  const desc = e.description || e.name
  const tpl
    = kind === 'character'
      ? `统一风格插画，${e.name}，${desc}，全身立绘，纯色背景，高细节`
      : kind === 'scene'
        ? `统一风格插画，场景：${e.name}，${desc}，全景背景图，无人物`
        : `统一风格插画，道具：${e.name}，${desc}，特写，纯色背景`
  return {
    ...e,
    low_confidence: e.low_confidence ?? e.confidence < 0.7,
    checked: true,
    // 视图编辑字段：description 直接可改；visual_prompt 给默认模板
    description: desc,
    visual_prompt: tpl,
  }
}

/**
 * 启动伪进度定时器（300ms 步进，随机增量，95% 封顶等待真实完成）。
 * @returns 无
 */
function startFakeProgress(): void {
  fakeProgress.value = 0
  progressTimer = setInterval(() => {
    if (fakeProgress.value < 95) {
      fakeProgress.value = Math.min(95, fakeProgress.value + Math.ceil(Math.random() * 7))
    }
  }, 300)
}

/**
 * 停止伪进度定时器并把进度推满。
 * @returns 无
 */
function stopFakeProgress(): void {
  if (progressTimer) {
    clearInterval(progressTimer)
    progressTimer = null
  }
  fakeProgress.value = 100
}

/**
 * 执行实体提取（进入页面自动调用；「重新提取」复用）。
 * 按集工作流：未选集（默认剧集上下文）时提示用户先选集，不执行抽取。
 * @returns 无
 */
async function runExtract(): Promise<void> {
  if (extracting.value) return // 防重复点击
  if (currentEpisodeId.value == null) {
    // 按集工作流：资产须在选定集数后按集生成
    ElMessage.warning('请在左上角选择好集数')
    return
  }
  extracting.value = true
  extracted.value = false
  result.value = null
  startFakeProgress()
  try {
    // 首次进入需先确认剧本存在（404 时全局拦截器提示；按集上下文定位该集剧本行）
    if (!scriptOut) {
      scriptOut = await studioApi.getInput(projectId.value, currentEpisodeId.value ?? undefined)
    }
    const res = await studioApi.extract(projectId.value, currentEpisodeId.value ?? undefined)
    result.value = res
    characters.value = res.characters.map((c) => toEditable(c, 'character'))
    scenes.value = res.scenes.map((s) => toEditable(s, 'scene'))
    props.value = res.props.map((p) => toEditable(p, 'prop'))
    // 冲突裁决状态重置（默认未裁决）
    choices.value = res.conflicts.map(() => '')
    extracted.value = true
    // 默认全选：等表格渲染完成后逐组勾选
    await nextTick()
    for (const [tbl, list] of [
      [charTable, characters],
      [sceneTable, scenes],
      [propTable, props],
    ] as const) {
      for (const row of list.value) tbl.value?.toggleRowSelection(row, true)
    }
  } finally {
    stopFakeProgress()
    extracting.value = false
  }
}

/**
 * 「重新提取」确认框 + 重跑（覆盖当前编辑与勾选）。
 * @returns 无
 */
async function reExtract(): Promise<void> {
  try {
    await ElMessageBox.confirm('重新提取将覆盖当前编辑与勾选结果，是否继续？', '重新提取', {
      type: 'warning',
    })
  } catch {
    return
  }
  await runExtract()
}

// ============================================================
// 勾选同步（表格 selection → checked 字段）
// ============================================================

/**
 * 同步某组表格的勾选状态到行数据（selection-change 回调）。
 * @param list 行列表
 * @param selection 当前勾选行
 * @returns 无
 */
function syncSelection(list: EditableEntity[], selection: EditableEntity[]): void {
  for (const row of list) row.checked = selection.includes(row)
}

// ============================================================
// 别名编辑（el-tag 动态增删）
// ============================================================

/** 正在输入的别名（key 为 `group-index`，value 为输入内容） */
const aliasInput = reactive<Record<string, string>>({})
/** 别名输入框可见性（同上 key） */
const aliasInputVisible = reactive<Record<string, boolean>>({})

/**
 * 新增别名。
 * @param list 目标列表
 * @param key 输入框键
 * @param row 目标行
 * @returns 无
 */
function addAlias(list: EditableEntity[], key: string, row: EditableEntity): void {
  const val = (aliasInput[key] ?? '').trim()
  if (val && !row.aliases.includes(val)) {
    row.aliases = [...row.aliases, val]
  }
  aliasInput[key] = ''
  aliasInputVisible[key] = false
}

/**
 * 删除别名。
 * @param row 目标行
 * @param alias 别名文本
 * @returns 无
 */
function removeAlias(row: EditableEntity, alias: string): void {
  row.aliases = row.aliases.filter((a) => a !== alias)
}

// ============================================================
// 冲突裁决（RD-06：未裁决完阻断提交）
// ============================================================

/** 每条冲突的裁决选择（候选名，空串表示未裁决） */
const choices = ref<string[]>([])

/** 是否存在未裁决冲突（决定「生成资产卡」按钮是否禁用） */
const hasUnresolved = computed(() => choices.value.some((c) => !c))

// ============================================================
// 统计与提交
// ============================================================

/** 将生成的角色数 */
const checkedChars = computed(() => characters.value.filter((c) => c.checked))
/** 将生成的场景数 */
const checkedScenes = computed(() => scenes.value.filter((s) => s.checked))
/** 将生成的道具数 */
const checkedProps = computed(() => props.value.filter((p) => p.checked))
/** 预估积分（每张 10 积分文案） */
const estimatedCost = computed(
  () => (checkedChars.value.length + checkedScenes.value.length + checkedProps.value.length) * 10,
)

/** 提交进行中（防重复点击） */
const submitting = ref(false)

/**
 * 应用冲突裁决：把落选候选名并入保留名的 aliases（提交时生效）。
 * @returns 无
 */
function mergedCharacters(): EditableEntity[] {
  if (!result.value) return characters.value
  const merged = characters.value.map((c) => ({ ...c, aliases: [...c.aliases] }))
  result.value.conflicts.forEach((conf, i) => {
    const keep = choices.value[i]
    if (!keep) return
    const target = merged.find((c) => c.name === keep)
    if (!target) return
    for (const cand of conf.candidates) {
      if (cand !== keep && !target.aliases.includes(cand)) target.aliases.push(cand)
    }
  })
  return merged
}

/**
 * 确认并生成资产卡：汇总勾选项 → confirmExtract → 跳资产管理页。
 * @returns 无
 */
async function submitConfirm(): Promise<void> {
  if (submitting.value) return
  if (!checkedChars.value.length && !checkedScenes.value.length && !checkedProps.value.length) {
    ElMessage.warning('请至少勾选一个实体')
    return
  }
  submitting.value = true
  try {
    const finalChars = mergedCharacters()
    await studioApi.confirmExtract(projectId.value, {
      characters: finalChars
        .filter((c) => c.checked)
        .map((c) => ({
          name: c.name,
          aliases: c.aliases,
          description: c.description,
          visual_prompt: c.visual_prompt,
        })),
      scenes: checkedScenes.value.map((s) => ({
        name: s.name,
        description: s.description,
        visual_prompt: s.visual_prompt,
      })),
      props: checkedProps.value.map((p) => ({
        name: p.name,
        description: p.description,
        visual_prompt: p.visual_prompt,
      })),
      // 按集工作流：确认名单 union 落该集启用名单（资产管理页按集视图据此显示）
      episode_id: currentEpisodeId.value ?? undefined,
    })
    ElMessage.success('资产已生成，进入资产管理')
    await router.push(`/projects/${projectId.value}/assets`)
  } finally {
    submitting.value = false
  }
}

/** 低置信行标黄样式（RD-06/RD-24 标黄约定） */
const lowConfRowClass = ({ row }: { row: EditableEntity }): string =>
  row.low_confidence ? 'low-conf-row' : ''

// ============================================================
// P0-2 抽取统计反馈（消除场景/道具为 0 的静默失败）
// ============================================================

/**
 * 抽取统计行文案（来源 + 计数）。
 * @returns 统计文案；无 stats（旧后端）时返回空串不展示
 */
const statsText = computed((): string => {
  const stats = result.value?.stats
  if (!stats) return ''
  const sourceLabel = stats.source === 'llm' ? 'AI 模型' : '规则兜底'
  return `解析出角色 ${stats.characters} · 场景 ${stats.scenes} · 道具 ${stats.props}（抽取来源：${sourceLabel}）`
})

onMounted(runExtract)
onBeforeUnmount(() => {
  if (progressTimer) clearInterval(progressTimer)
})
</script>

<template>
  <div class="page-container extract-view">
    <div class="view-head">
      <h2 class="page-title">实体提取确认</h2>
      <el-button text type="primary" :disabled="extracting" @click="reExtract">重新提取</el-button>
    </div>

    <!-- 提取进行中：伪进度条 -->
    <el-card v-if="extracting" shadow="never">
      <div class="progress-line">
        <span>正在从剧本中提取角色、场景与道具…</span>
        <el-progress :percentage="fakeProgress" class="progress-bar" />
      </div>
    </el-card>

    <!-- 引导空态：提取失败 / 重置后 / 未选集 -->
    <el-card v-else-if="!extracted" shadow="never">
      <el-empty
        :description="currentEpisodeId == null
          ? '尚未选择集数：请在左上角选择好集数后开始提取（角色/场景/道具将按该集分集大纲与剧本正文提取）'
          : `尚未完成实体提取（范围：${scopeLabel}）`"
      >
        <el-button type="primary" @click="runExtract">开始提取</el-button>
      </el-empty>
    </el-card>

    <template v-else>
      <!-- P0-2 抽取统计反馈：解析计数 + 来源 + 抽取范围与风格说明 -->
      <el-alert
        v-if="statsText"
        :type="result?.scenes.length ? 'info' : 'warning'"
        show-icon
        :closable="false"
        :title="statsText"
        :description="result?.scenes.length
          ? `抽取范围：${scopeLabel}；风格资产将按项目画风「${projectStyle || '默认画风'}」在确认入池时自动创建`
          : '未识别到场景实体：分镜将无法强绑场景资产。请检查该集剧本是否含「内景/外景」标记或场景名，或在下方场景 Tab 手动补充。'"
      />

      <!-- 冲突裁决区（RD-06：必须裁决才能提交） -->
      <el-card v-if="result && result.conflicts.length" shadow="never" class="conflict-card">
        <template #header>
          <div class="card-head">
            <span>别名冲突裁决</span>
            <el-tag v-if="hasUnresolved" type="danger" size="small">存在未裁决冲突，将阻断提交</el-tag>
            <el-tag v-else type="success" size="small">已全部裁决</el-tag>
          </div>
        </template>
        <el-card
          v-for="(conf, i) in result.conflicts"
          :key="i"
          shadow="never"
          class="conflict-item"
        >
          <div class="conflict-title">
            冲突 #{{ i + 1 }}（{{ conf.type === 'alias' ? '别名归并' : conf.type }}）
            <el-tag size="small" type="info" effect="plain">建议保留：{{ conf.suggestion }}</el-tag>
          </div>
          <el-radio-group v-model="choices[i]">
            <el-radio v-for="cand in conf.candidates" :key="cand" :value="cand">
              保留「{{ cand }}」{{ cand !== conf.suggestion ? '（其余并入别名）' : '' }}
            </el-radio>
          </el-radio-group>
        </el-card>
      </el-card>

      <!-- 三类实体 Tab -->
      <el-tabs type="border-card" class="entity-tabs">
        <el-tab-pane :label="`角色 (${characters.length})`">
          <el-table
            ref="charTable"
            :data="characters"
            :row-class-name="lowConfRowClass"
            @selection-change="(sel: EditableEntity[]) => syncSelection(characters, sel)"
          >
            <el-table-column type="selection" width="46" />
            <el-table-column label="名称" min-width="120">
              <template #default="{ row }">
                <el-input v-model="row.name" size="small" />
              </template>
            </el-table-column>
            <el-table-column label="别名" min-width="200">
              <template #default="{ row, $index }">
                <div class="alias-wrap">
                  <el-tag
                    v-for="a in row.aliases"
                    :key="a"
                    closable
                    size="small"
                    @close="removeAlias(row, a)"
                  >
                    {{ a }}
                  </el-tag>
                  <el-input
                    v-if="aliasInputVisible[`c-${$index}`]"
                    v-model="aliasInput[`c-${$index}`]"
                    size="small"
                    style="width: 90px"
                    @keyup.enter="addAlias(characters, `c-${$index}`, row)"
                    @blur="addAlias(characters, `c-${$index}`, row)"
                  />
                  <el-button v-else size="small" text @click="aliasInputVisible[`c-${$index}`] = true">
                    + 别名
                  </el-button>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="描述" min-width="180">
              <template #default="{ row }">
                <el-input v-model="row.description" size="small" type="textarea" :rows="1" />
              </template>
            </el-table-column>
            <el-table-column label="置信度" width="150">
              <template #default="{ row }">
                <el-tag size="small" :type="row.low_confidence ? 'warning' : 'success'">
                  {{ row.low_confidence ? '低置信' : '高' }} {{ (row.confidence * 100).toFixed(0) }}%
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column type="expand">
              <template #default="{ row }">
                <div class="expand-body">
                  <span class="expand-label">视觉提示词：</span>
                  <el-input v-model="row.visual_prompt" type="textarea" :rows="2" />
                </div>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane :label="`场景 (${scenes.length})`">
          <el-table
            ref="sceneTable"
            :data="scenes"
            :row-class-name="lowConfRowClass"
            @selection-change="(sel: EditableEntity[]) => syncSelection(scenes, sel)"
          >
            <el-table-column type="selection" width="46" />
            <el-table-column label="名称" min-width="140">
              <template #default="{ row }">
                <el-input v-model="row.name" size="small" />
              </template>
            </el-table-column>
            <el-table-column label="描述" min-width="260">
              <template #default="{ row }">
                <el-input v-model="row.description" size="small" type="textarea" :rows="1" />
              </template>
            </el-table-column>
            <el-table-column label="置信度" width="150">
              <template #default="{ row }">
                <el-tag size="small" :type="row.low_confidence ? 'warning' : 'success'">
                  {{ row.low_confidence ? '低置信' : '高' }} {{ (row.confidence * 100).toFixed(0) }}%
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column type="expand">
              <template #default="{ row }">
                <div class="expand-body">
                  <span class="expand-label">视觉提示词：</span>
                  <el-input v-model="row.visual_prompt" type="textarea" :rows="2" />
                </div>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane :label="`道具 (${props.length})`">
          <el-table
            ref="propTable"
            :data="props"
            :row-class-name="lowConfRowClass"
            @selection-change="(sel: EditableEntity[]) => syncSelection(props, sel)"
          >
            <el-table-column type="selection" width="46" />
            <el-table-column label="名称" min-width="140">
              <template #default="{ row }">
                <el-input v-model="row.name" size="small" />
              </template>
            </el-table-column>
            <el-table-column label="描述" min-width="260">
              <template #default="{ row }">
                <el-input v-model="row.description" size="small" type="textarea" :rows="1" />
              </template>
            </el-table-column>
            <el-table-column label="置信度" width="150">
              <template #default="{ row }">
                <el-tag size="small" :type="row.low_confidence ? 'warning' : 'success'">
                  {{ row.low_confidence ? '低置信' : '高' }} {{ (row.confidence * 100).toFixed(0) }}%
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column type="expand">
              <template #default="{ row }">
                <div class="expand-body">
                  <span class="expand-label">视觉提示词：</span>
                  <el-input v-model="row.visual_prompt" type="textarea" :rows="2" />
                </div>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>

      <!-- 底部统计与提交条 -->
      <div class="footer-bar">
        <div class="stat-line">
          将生成
          <b>{{ checkedChars.length }}</b> 角色 /
          <b>{{ checkedScenes.length }}</b> 场景 /
          <b>{{ checkedProps.length }}</b> 道具
          <span class="stat-cost">预估消耗：约 {{ checkedChars.length + checkedScenes.length + checkedProps.length }} 张图 = {{ estimatedCost }} 积分</span>
        </div>
        <el-button
          size="large"
          type="primary"
          class="confirm-btn"
          :disabled="hasUnresolved"
          :loading="submitting"
          @click="submitConfirm"
        >
          生成资产卡，进入资产管理
        </el-button>
      </div>
      <el-alert
        v-if="hasUnresolved"
        type="error"
        show-icon
        :closable="false"
        title="存在未裁决的别名冲突：请先在上方「别名冲突裁决」中为每条冲突选择保留名，才能提交生成资产"
      />
    </template>
  </div>
</template>

<style scoped>
.extract-view {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-5);
}
.view-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-4);
}
.view-head .page-title {
  margin: 0;
}
.progress-line {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-4);
}
.progress-bar {
  flex: 1;
}
.card-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.conflict-card .conflict-item {
  margin-bottom: var(--mf-sp-3);
  border: 1px dashed var(--mf-border-strong);
}
.conflict-item:last-child {
  margin-bottom: 0;
}
.conflict-title {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-2);
  font-weight: var(--mf-fw-semibold);
}
.alias-wrap {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
}
.expand-body {
  display: flex;
  align-items: flex-start;
  gap: var(--mf-sp-2);
  padding: var(--mf-sp-2) var(--mf-sp-4);
}
.expand-label {
  white-space: nowrap;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  padding-top: 4px;
}
/* 低置信行标黄 */
:deep(.low-conf-row) {
  background: var(--el-color-warning-light-9);
}
.footer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-4);
  position: sticky;
  bottom: 0;
  /* sticky 吸底时浮于 positioned 内容之上，避免滚动穿透重叠 */
  z-index: 10;
  padding: var(--mf-sp-3);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
}
.stat-line {
  font-size: var(--mf-fs-md);
}
.stat-cost {
  margin-left: var(--mf-sp-4);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.confirm-btn {
  background: var(--mf-brand);
  border-color: var(--mf-brand);
  color: var(--mf-bg);
}
</style>
