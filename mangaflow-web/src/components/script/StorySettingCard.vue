<!--
  剧本工作台 · 卡片① 故事设定（文件级注释，redesign-script-workspace Task 3；
  产品简化调整：空态收敛为创作输入查看/编辑/保存；角色设定已拆分为独立
  步骤卡 CharacterSettingCard，本卡不再承载人物小传区）。

  承载故事设定域：
  - 空态（未生成设定）：创作输入查看与编辑——预填新建项目时填写的创意/
    小说/大纲（GET script 的 raw_text），可修改后「保存修改」（PUT /script/input
    写回 raw_text）；分集大纲与剧本正文生成的源文本即该 raw_text，保存即应用
    到下游；本步骤不提供「生成故事设定」入口；
  - 已生成设定态：设定三段（一句话故事 / 核心冲突 / 世界观）展示 + 段级
    「编辑」（行内 textarea → PUT /script/setting 单字段合并保存，source=user，
    段落角标「已编辑」）+「重新生成」（body 阶段后端 409 锁定）；
  - 三态完整：加载骨架 / 空态编辑 / 错误 el-alert 重试。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import * as pipelineApi from '@/api/scriptPipeline'
import * as studioApi from '@/api/studio'
import { extractDetail } from '@/api/client'
import QuotableText from './QuotableText.vue'
import type { AxiosError } from 'axios'
import type {
  ScriptPhase,
  StorySetting,
} from '@/api/scriptPipeline'

/** 组件入参 */
const props = defineProps<{
  /** 当前项目 ID（取容器路由参数，故事设定域接口路径参数） */
  projectId: number
}>()

/** 组件事件 */
const emit = defineEmits<{
  /** 阶段变化（重生成等接口回执携带最新 script_phase，同步容器步骤与解锁） */
  'phase-changed': [phase: ScriptPhase | null]
}>()

// ============================================================
// 设定三段元信息（key 与后端 StorySetting 字段一一对应）
// ============================================================

/** 设定段 key（StorySetting 可编辑字段） */
type SettingField = 'logline' | 'core_conflict' | 'worldview'

/** 设定段展示元信息（标题 + 文案占位） */
const SECTIONS: Array<{ key: SettingField; label: string; rows: number; max: number }> = [
  { key: 'logline', label: '一句话故事', rows: 3, max: 200 },
  { key: 'core_conflict', label: '核心冲突', rows: 4, max: 1000 },
  { key: 'worldview', label: '世界观', rows: 5, max: 2000 },
]

// ============================================================
// 核心状态（设定内容 + 阶段）
// ============================================================

/** 首屏加载中（骨架） */
const loading = ref(false)
/** 首屏加载失败（错误 el-alert + 重试） */
const loadError = ref(false)
/** 故事设定内容块（null=空态） */
const setting = ref<StorySetting | null>(null)
/** 当前剧本创作阶段 */
const phase = ref<ScriptPhase | null>(null)

// ============================================================
// 创作输入回显（空态：显示用户在新建项目时填写的创意/小说/大纲）
// ============================================================

/** 用户原始创作输入（GET script 的 raw_text + content.input_mode；null=未取到） */
const sourceInput = ref<{ mode: 'idea' | 'novel' | 'outline'; text: string } | null>(null)
/** 创作输入加载中（空态回显骨架） */
const sourceLoading = ref(false)
/** 创意编辑草稿（空态 textarea 双向绑定；保存后写回剧本 raw_text） */
const sourceDraft = ref('')
/** 保存创作输入中 */
const savingSource = ref(false)

/**
 * 拉取用户原始创作输入（GET script：raw_text 为用户在新建弹窗填写的创意，
 * novel 模式下后端已把章节拼接全文回写该字段；input_mode 存于 content JSONB），
 * 并填充编辑草稿。无剧本行（404）或接口失败时静默置 null，空态退化为可手写。
 * @returns 无
 */
async function loadSourceInput(): Promise<void> {
  sourceLoading.value = true
  try {
    const s = await studioApi.getInput(props.projectId)
    const content = s.content as { input_mode?: unknown } | null | undefined
    const mode = content?.input_mode
    const text = (s.raw_text ?? '').trim()
    sourceInput.value = {
      mode: mode === 'novel' || mode === 'outline' ? mode : 'idea',
      text,
    }
    sourceDraft.value = text
  } catch {
    sourceInput.value = null
  } finally {
    sourceLoading.value = false
  }
}

/**
 * 保存创作输入修改（PUT /script/input 写回剧本 raw_text）。
 * 分集大纲与剧本正文生成的源文本即该 raw_text——保存即应用到下游生成。
 * @param silent 静默模式（AI 生成角色的前置保存，不弹成功 toast）
 * @returns 无
 * @throws 接口错误由拦截器统一提示，此处补充内联错误提示
 */
async function saveSourceEdit(silent = false): Promise<void> {
  if (savingSource.value) return
  const text = sourceDraft.value.trim()
  if (!text) {
    if (!silent) ElMessage.warning('创作输入不能为空')
    return
  }
  savingSource.value = true
  try {
    await studioApi.saveInput(props.projectId, {
      mode: sourceInput.value?.mode ?? 'idea',
      raw_text: text,
    })
    sourceInput.value = { mode: sourceInput.value?.mode ?? 'idea', text }
    if (!silent) ElMessage.success('创作输入已保存，将应用于分集大纲与剧本正文生成')
  } catch (e) {
    if (!silent) ElMessage.error(extractDetail(e as AxiosError))
    throw e
  } finally {
    savingSource.value = false
  }
}

/** 生成中（regenerate 请求进行时） */
const generating = ref(false)
/** 生成失败信息（非空时展示错误 el-alert + 重试；接口 detail 已由拦截器弹出） */
const genError = ref('')
/** 最近一次重生成参数（重试按钮复用） */
const lastGenInstruction = ref<string | null>(null)

/** 确认链路进行中（保存编辑 + confirmCharacters） */
const confirming = ref(false)

/** 是否已过确认（outline/body：确认按钮替换为已完成态） */
const confirmedReached = computed(() => phase.value === 'outline' || phase.value === 'body')

/** 设定是否处于 body 锁定（重生成禁用；PUT 编辑不受限） */
const settingLocked = computed(() => phase.value === 'body')

/**
 * 拉取故事设定详情并初始化；空态时拉取创作输入供查看/编辑。
 * @param allowAuto 兼容保留参数（原 auto=1 自动生成分流已随生成入口移除，不再生效）
 * @returns 无
 * @throws 接口错误由拦截器统一提示，此处仅置错误态
 */
async function init(allowAuto = true): Promise<void> {
  loading.value = true
  loadError.value = false
  try {
    const out = await pipelineApi.getStorySetting(props.projectId)
    setting.value = out.setting
    phase.value = out.phase
    emit('phase-changed', out.phase)
    // 空态：拉取用户在新建项目时填写的原始输入（创意/小说/大纲），
    // 本步骤收敛为「查看 + 编辑 + 保存创意」，保存后应用于分集大纲与剧本正文生成
    if (!out.setting) void loadSourceInput()
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

// ============================================================
// 生成 / 重生成
// ============================================================

/**
 * 执行重生成（已生成态「重新生成」入口；携带可选 instruction 走 regenerate）。
 * @param instruction 可选调整方向（undefined=无指令重生成）
 * @returns 无
 * @throws 接口错误由拦截器统一提示（409 已锁定 / 502 生成失败），此置错误态供重试
 */
async function doGenerate(instruction?: string | null): Promise<void> {
  if (generating.value) return
  generating.value = true
  genError.value = ''
  lastGenInstruction.value = instruction ?? null
  try {
    const out = await pipelineApi.regenerateStorySetting(
      props.projectId,
      instruction ? { instruction } : {},
    )
    setting.value = out.setting
    phase.value = out.phase
    emit('phase-changed', out.phase)
    ElMessage.success('故事设定已重新生成')
  } catch (e) {
    // 错误条同步展示后端 detail（拦截器 toast 消失后用户仍能看到失败原因，
    // 如 502 的「LLM 渠道…」链路信息），并保留界面重试入口
    genError.value = extractDetail(e as AxiosError)
  } finally {
    generating.value = false
  }
}

/**
 * 「重新生成」入口：ElMessageBox.prompt 收集可选 instruction 后重出设定段。
 * 已确认角色保留；未入池候选与未保存的设定编辑将随重生成刷新。
 * @returns 无
 */
async function promptRegenerate(): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt(
      '可输入本轮重生成的调整方向（如「冲突更强」「世界观更暗黑」），留空则按原创意重出。'
        + '未入池的人设候选将随重生成刷新，已确认角色保留。',
      '重新生成故事设定',
      {
        confirmButtonText: '重新生成',
        cancelButtonText: '取消',
        inputPlaceholder: '调整方向（可选，不超过 2000 字）',
        inputValidator: (v: string) => !v || v.length <= 2000 || '不超过 2000 字',
      },
    )
    await doGenerate(typeof value === 'string' ? value.trim() : '')
  } catch {
    // 用户取消
  }
}

/**
 * 生成失败后的「重试」：按最近一次参数重发（携带 instruction 重生成）。
 * @returns 无
 */
function retryGenerate(): void {
  genError.value = ''
  void doGenerate(lastGenInstruction.value)
}

// ============================================================
// 设定三段：段级行内编辑（PUT /setting 单字段合并保存）
// ============================================================

/** 当前编辑中的段（null=全部展示态） */
const editing = ref<SettingField | null>(null)
/** 编辑草稿（行内 textarea 双向绑定） */
const editDraft = ref('')
/** 单段保存中（防重复提交） */
const savingField = ref(false)

/**
 * 进入某段的行内编辑态（草稿取当前值）。
 * @param field 目标段 key
 * @returns 无
 */
function openEdit(field: SettingField): void {
  editing.value = field
  editDraft.value = setting.value?.[field] ?? ''
}

/**
 * 取消编辑，回到展示态。
 * @returns 无
 */
function cancelEdit(): void {
  editing.value = null
  editDraft.value = ''
}

/**
 * 保存当前编辑段：调 PUT /setting 单字段合并（source=user），就地更新设定块。
 * @param field 目标段 key
 * @returns 无
 * @throws 422 空串/超长由拦截器统一提示，状态回退保留编辑态
 */
async function saveField(field: SettingField): Promise<void> {
  if (!editDraft.value.trim()) return
  savingField.value = true
  try {
    const out = await pipelineApi.editStorySetting(props.projectId, { [field]: editDraft.value.trim() })
    setting.value = out.setting
    editing.value = null
    editDraft.value = ''
    ElMessage.success('设定已保存')
  } finally {
    savingField.value = false
  }
}

onMounted(() => void init())

/** 组件暴露（Task 6：容器在助手面板采纳/回退后调 reload 刷新故事设定卡） */
defineExpose({
  /**
   * 重拉故事设定详情（人物双组 + 设定三段 + 阶段；不触发 auto 自动生成分流）。
   * @returns 刷新完成信号
   */
  reload: (): Promise<void> => init(false),
})
</script>

<template>
  <div class="story-setting">
    <!-- 首屏骨架 -->
    <el-skeleton v-if="loading" :rows="7" animated />

    <!-- 首屏加载失败：错误 el-alert + 重试 -->
    <el-alert
      v-else-if="loadError"
      type="error"
      show-icon
      :closable="false"
      title="故事设定加载失败"
      description="接口错误详情已由全局提示展示，请检查网络后重试"
    >
      <el-button size="small" type="primary" class="alert-retry" @click="init(false)">重试</el-button>
    </el-alert>

    <template v-else>
      <!-- ===== 顶部动作区（仅已生成设定态）：说明文案 + 重新生成按钮。
           角色确认入池主按钮统一收拢到下方角色设定区（空态/已生成态共用，
           入池即连接分集大纲的强约束） ===== -->
      <div v-if="setting" class="ss-actions">
        <div class="ss-tip">
          基于创意与项目参数生成「一句话故事 / 核心冲突 / 世界观 / 主要人物小传」，
          可编辑调整后确认，进入分集大纲
        </div>
        <div class="ss-ops">
          <el-button
            v-if="!confirmedReached"
            :disabled="generating || settingLocked"
            :title="settingLocked ? '正文已开始生成，故事设定已锁定' : undefined"
            @click="promptRegenerate"
          >
            重新生成
          </el-button>
          <el-tag v-if="confirmedReached" size="small" type="success" effect="plain">
            故事设定已确认
          </el-tag>
        </div>
      </div>

      <!-- 生成失败重试条：description 直接展示后端 detail（如 502 的渠道失败
           链路信息），拦截器 toast 消失后用户仍可在此看到失败原因并重试 -->
      <el-alert
        v-if="genError && !generating"
        type="error"
        show-icon
        :closable="false"
        class="ss-alert"
        title="故事设定生成失败"
        :description="genError"
      >
        <el-button size="small" type="primary" class="alert-retry" @click="retryGenerate">
          重试
        </el-button>
      </el-alert>

      <!-- ===== 生成中状态区（已生成态「重新生成」进行时）===== -->
      <template v-if="generating">
        <div class="gen-state">
          <el-icon class="gen-icon is-loading"><Loading /></el-icon>
          <span class="gen-text">正在生成故事设定，通常需要数十秒…</span>
        </div>
        <el-skeleton :rows="6" animated />
      </template>

      <!-- ===== 空态：极简三层（卡片标题「① 故事设定」→ 创意内容编辑撑满 →
           右下角保存）。编辑区预填新建项目时的创意/小说/大纲，保存（PUT
           /script/input 写回 raw_text）后应用于分集大纲与剧本正文生成 ===== -->
      <div v-else-if="!setting" class="ss-empty">
        <el-skeleton v-if="sourceLoading" :rows="7" animated />
        <template v-else>
          <QuotableText source="故事设定 · 创意内容" scope="setting">
            <el-input
              v-model="sourceDraft"
              type="textarea"
              :autosize="{ minRows: 8, maxRows: 18 }"
              maxlength="12000"
              show-word-limit
              resize="vertical"
              placeholder="写下你的剧本创意、粘贴小说片段或故事大纲…"
            />
          </QuotableText>
          <div class="ss-empty__ops">
            <el-button
              type="primary"
              :loading="savingSource"
              :disabled="!sourceDraft.trim() || sourceDraft.trim() === sourceInput?.text"
              @click="saveSourceEdit"
            >
              保存修改
            </el-button>
          </div>
        </template>
      </div>

      <template v-else>
        <!-- ===== 设定三段：展示态 + 段级行内编辑 ===== -->
        <div class="ss-blocks">
          <div v-for="sec in SECTIONS" :key="sec.key" class="ss-block">
            <div class="ss-block__head">
              <span class="ss-block__title">{{ sec.label }}</span>
              <el-tag
                v-if="setting.source === 'user'"
                size="small"
                type="warning"
                effect="plain"
              >
                已编辑
              </el-tag>
              <el-button
                v-if="editing !== sec.key"
                class="ss-block__edit"
                text
                type="primary"
                size="small"
                @click="openEdit(sec.key)"
              >
                编辑
              </el-button>
            </div>
            <template v-if="editing === sec.key">
              <QuotableText :source="`故事设定 · ${sec.label}`" scope="setting">
                <el-input
                  v-model="editDraft"
                  type="textarea"
                  :rows="sec.rows"
                  :maxlength="sec.max"
                  show-word-limit
                />
              </QuotableText>
              <div class="ss-block__ops">
                <el-button size="small" @click="cancelEdit">取消</el-button>
                <el-button
                  size="small"
                  type="primary"
                  :loading="savingField"
                  :disabled="!editDraft.trim()"
                  @click="saveField(sec.key)"
                >
                  保存
                </el-button>
              </div>
            </template>
            <p
              v-else
              class="ss-block__text"
              :class="{ 'ss-block__text--empty': !setting[sec.key] }"
            >
              {{ setting[sec.key] || '（暂无内容，点击「编辑」补充）' }}
            </p>
          </div>
        </div>
      </template>
    </template>

  </div>
</template>

<style scoped>
.story-setting {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-4);
}
/* 顶部动作区：提示文案 + 操作按钮 */
.ss-actions {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.ss-tip {
  flex: 1;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
  line-height: 1.6;
}
.ss-ops {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  flex-shrink: 0;
}
/* 错误重试条 */
.ss-alert {
  border-radius: var(--mf-r-ctl);
}
.alert-retry {
  margin-top: var(--mf-sp-2);
}
/* 生成中状态区（已生成态「重新生成」进行时） */
.gen-state {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  color: var(--mf-text-2);
}
.gen-icon {
  margin-right: var(--mf-sp-1);
}
/* 设定三段展示 */
.ss-blocks {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-4);
}
.ss-block {
  padding: var(--mf-sp-3) var(--mf-sp-4);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-surface-2);
}
.ss-block__head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-2);
}
.ss-block__title {
  font-weight: var(--mf-fw-semibold);
}
.ss-block__edit {
  margin-left: auto;
}
.ss-block__text {
  margin: 0;
  color: var(--mf-text-2);
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
}
.ss-block__text--empty {
  color: var(--mf-text-3);
}
.ss-block__ops {
  display: flex;
  justify-content: flex-end;
  gap: var(--mf-sp-2);
  margin-top: var(--mf-sp-2);
}

/* ===== 空态：创意内容编辑（撑满卡片宽度）+ 右下角保存 ===== */
.ss-empty {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
}
/* 保存按钮行：右下角（与 textarea 尾部/字数统计对齐） */
.ss-empty__ops {
  display: flex;
  justify-content: flex-end;
}
</style>
