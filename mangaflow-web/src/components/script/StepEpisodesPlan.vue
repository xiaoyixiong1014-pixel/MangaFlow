<!--
  剧本工作台 · 卡片② 分集大纲页（文件级注释，P-1 范式级重构 Task 10.3；Task 3 扩展路径 B 分流；
  redesign-script-workspace Task 4：大纲导航迁入 + 单集大纲 AI 重生成，作为 EpisodeOutlineCard 内部实现）。

  功能（布局对齐剧本正文：上方集卡横排 + 点击后下方显示该集内容）：
  - 上方集卡横向流（点击选中，选中高亮）：第N集 + 标题（两行截断）+ 确认
    标记 + 时长；卡流末尾「新增集」（复用既有剧集 POST /episodes 创建空集，
    流水线大纲域无单独新增接口）；
  - 下方本集大纲编辑区：标题行 = 第N集 · 标题 + 「AI 一键填充」按钮（新接口
    outline/autofill：依据故事设定 / 角色设定 / 本集已填内容 / 前后集钩子，
    只补全未填写字段——已填字段原样保留；建议值**不落库**，回填表单后用户
    自行调整或经 AI 助手改写，最后点「保存本集大纲」走 PUT outline 落库
    留痕）+ 表单（标题 / logline / 核心事件 / 出场角色（选项来自已确认角色
    名单）/ 情绪走向 / 单集时长 30~3600s）；保存调 PUT outline（空字段不传
    =后端不修改）；
  - 空态首次生成 CTA（集卡区虚线引导卡：目标集数 1~100 缺省 1 + 生成分集
    大纲按钮；整体重生成入口已移除——单集调整走「AI 一键填充」+ 手动编辑）；
    body 阶段大纲锁定：生成/填充禁用，仅允许单集文案微调；
  - 阶段推进（产品简化：无「确认大纲」动作）：保存任意一集大纲（PUT
    outline）时后端自动把 script_phase 从 outline 推进到 body——保存即
    定稿，正文步（步骤 4）随之解锁，可直接逐集生成正文；保存响应携带
    最新 phase，本组件同步容器步骤高亮；
  - 分流路径 B（redesign-script-workspace Task 3，小说/粘贴大纲项目）：未生成
    分集大纲时在顶部展示小说解析进度——已解析章节直接显示章节列表摘要，未解析
    则提供上传/解析入口；「按章节生成分集大纲」前按章节数推定集数（后端缺省
    每章一集），超过 EPISODE_CONFIRM_THRESHOLD 时弹窗确认后才调用生成；
  - 错误处理：409/502 由 client 拦截器统一弹后端 detail，组件内只做状态回退。
-->
<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules, UploadRequestOptions } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import * as pipelineApi from '@/api/scriptPipeline'
import QuotableText from './QuotableText.vue'
import type {
  EpisodeOutlineEditPayload,
  EpisodePlanItem,
  NovelChapterOut,
  ScriptPhase,
} from '@/api/scriptPipeline'
import * as episodesApi from '@/api/episodes'

/**
 * 长篇集数确认阈值（spec 分流路径 B）：按章节推定的分集数超过该值时，
 * 生成前弹窗「您的小说预计拆解为 N 集，是否确认？」，用户确认后方生成。
 * 默认 10 集，可按需调整。
 */
const EPISODE_CONFIRM_THRESHOLD = 10

/** 组件入参 */
const props = defineProps<{
  /** 当前项目 ID（大纲域接口路径参数） */
  projectId: number
}>()

/** 组件事件 */
const emit = defineEmits<{
  /** 阶段变化（生成/确认等接口回执携带最新 script_phase，通知容器推进步骤条） */
  phase: [phase: ScriptPhase | null]
  /** 选中集变化（redesign-script-workspace Task 6：容器据此计算助手面板聚焦对象） */
  'focus-changed': [info: { episodeId: number | null; sequence: number | null }]
}>()

/**
 * 当前选中集的聚焦信息（剧集 ID + 序号；未选中时均为 null）。
 * @returns 聚焦信息对象
 */
function focusInfo(): { episodeId: number | null; sequence: number | null } {
  const ep = selectedEpisode.value
  return { episodeId: ep?.episode_id ?? null, sequence: ep?.sequence ?? null }
}

/** 组件暴露（Task 6：容器在助手面板采纳后调 reload 刷新大纲列表；未保存守卫供跨步骤切换拦截） */
defineExpose({
  /** 重拉分集大纲列表与阶段（保持当前选中集） */
  reload: reloadPlan,
  /**
   * 未保存修改守卫（跨步骤切换前调用）：有 AI 填充/手动编辑的未保存修改时
   * 弹三选提醒（保存并切换 / 不保存切换 / 取消）。
   * @returns true=允许离开；false=留在当前集
   */
  ensureSavedBeforeLeave,
})

// ============================================================
// 数据加载（分集大纲 + 已确认角色名单）
// ============================================================

/** 首屏加载中（骨架） */
const loading = ref(false)
/** 分集大纲列表（sequence 升序） */
const episodes = ref<EpisodePlanItem[]>([])
/** 当前剧本创作阶段（body = 大纲锁定） */
const phase = ref<ScriptPhase | null>(null)

/** 大纲是否已锁定（body 阶段：生成与重排禁用，仅允许单集文案微调） */
const locked = computed(() => phase.value === 'body')

/** 已确认角色名单（出场角色多选选项来源） */
const characterOptions = ref<string[]>([])

/**
 * 重新拉取分集大纲并同步阶段；选中集被删除时回退到第一集。
 * @returns 无
 * @throws 请求失败由拦截器统一提示
 */
async function reloadPlan(): Promise<void> {
  const plan = await pipelineApi.getEpisodePlan(props.projectId)
  episodes.value = plan.episodes
  phase.value = plan.phase
  emit('phase', plan.phase)
  if (!episodes.value.some((e) => e.episode_id === selectedId.value)) {
    selectedId.value = episodes.value[0]?.episode_id ?? null
  }
  syncForm()
}

/**
 * 拉取已确认角色名单（失败不阻断页面，选项降级为空并允许自由创建）。
 * @returns 无
 */
async function loadCharacterOptions(): Promise<void> {
  try {
    const out = await pipelineApi.listCharacters(props.projectId)
    characterOptions.value = out.confirmed.map((c) => c.name)
  } catch {
    characterOptions.value = []
  }
}

onMounted(async () => {
  loading.value = true
  try {
    await Promise.all([reloadPlan(), loadCharacterOptions(), loadNovelChapters()])
  } finally {
    loading.value = false
  }
})

// ============================================================
// 分流路径 B（Task 3）：小说解析进度 + 按章节分集（预计集数确认）
// ============================================================

/** 已解析的小说章节列表（空 = 未解析；仅 novel 模式解析后存在） */
const novelChapters = ref<NovelChapterOut[]>([])
/** 上传解析中 */
const novelParsing = ref(false)
/** 按章节生成分集大纲中 */
const chaptersPlanning = ref(false)

/**
 * 拉取暂存的小说章节列表（GET 不 404，未解析返回空列表；失败降级为空不阻断页面）。
 * @returns 无
 */
async function loadNovelChapters(): Promise<void> {
  try {
    const out = await pipelineApi.getNovelChapters(props.projectId)
    novelChapters.value = out.chapters
  } catch {
    novelChapters.value = []
  }
}

/**
 * 上传小说文件并解析章节（el-upload 自定义上传）：成功后刷新章节摘要区，
 * 原始文本由后端回写剧本 raw_text。
 * @param options el-upload 上传参数（含 file）
 * @returns 无
 * @throws 接口错误已由 client 拦截器统一提示
 */
async function handleNovelUpload(options: UploadRequestOptions): Promise<void> {
  novelParsing.value = true
  try {
    const out = await pipelineApi.parseNovel(props.projectId, options.file)
    novelChapters.value = out.chapters
    ElMessage.success(`已解析 ${out.chapter_count} 个章节，可一键按章节生成分集大纲`)
  } catch {
    // 业务错误已由 client 拦截器统一提示
  } finally {
    novelParsing.value = false
  }
}

/**
 * 按章节生成分集大纲（from_chapters 分支，路径 B 主链路）：
 * 推定集数 = 章节数（后端缺省每章一集）；超过 EPISODE_CONFIRM_THRESHOLD 时
 * 先弹窗「您的小说预计拆解为 N 集，是否确认？」，确认后方调用生成。
 * @returns 无
 * @throws 409 未确认角色 / 未解析章节 / 大纲已锁定由拦截器提示
 */
async function generatePlanFromChapters(): Promise<void> {
  if (chaptersPlanning.value) return
  const estimated = novelChapters.value.length
  if (!estimated) return
  if (estimated > EPISODE_CONFIRM_THRESHOLD) {
    try {
      await ElMessageBox.confirm(
        `您的小说预计拆解为 ${estimated} 集，是否确认？`,
        '确认分集数量',
        { type: 'warning', confirmButtonText: '确认生成', cancelButtonText: '取消' },
      )
    } catch {
      return // 用户取消
    }
  }
  chaptersPlanning.value = true
  try {
    const out = await pipelineApi.generateEpisodePlan(props.projectId, { from_chapters: true })
    episodes.value = out.episodes
    phase.value = out.phase
    emit('phase', out.phase)
    if (!episodes.value.some((e) => e.episode_id === selectedId.value)) {
      selectedId.value = episodes.value[0]?.episode_id ?? null
    }
    syncForm()
    ElMessage.success(`已按章节生成 ${out.episodes.length} 集大纲，可逐集调整后确认`)
  } catch {
    // 409 未确认角色 / 未解析章节等由拦截器提示
  } finally {
    chaptersPlanning.value = false
  }
}

// ============================================================
// 左侧集列表：选中 / 折叠 / 重排 / 新增 / 删除
// ============================================================

/** 当前选中集 ID（null=未选中） */
const selectedId = ref<number | null>(null)

/** 当前选中集条目 */
const selectedEpisode = computed(
  () => episodes.value.find((e) => e.episode_id === selectedId.value) ?? null)

/** 选中文本引用的来源标签（大纲编辑区「添加对话中」用） */
const outlineQuoteSource = computed(
  () => `分集大纲 · 第 ${selectedEpisode.value?.sequence ?? '?'} 集`)

// 选中集变化（含列表刷新后的默认选中回退）时上报，供容器联动助手面板聚焦（Task 6）
watch(selectedId, () => {
  emit('focus-changed', focusInfo())
})

/**
 * 选中某集并把其大纲字段同步进编辑表单；当前集有未保存修改（AI 一键填充
 * 或手动编辑）时先弹三选提醒（保存并切换 / 不保存切换 / 取消留步）。
 * @param ep 目标集条目
 * @returns 无
 */
async function selectEpisode(ep: EpisodePlanItem): Promise<void> {
  if (ep.episode_id === selectedId.value) return
  if (!(await ensureSavedBeforeLeave())) return
  selectedId.value = ep.episode_id
  syncForm()
}

/** 新增集请求进行中 */
const adding = ref(false)

/**
 * 新增空集：复用既有剧集创建接口（流水线大纲域无「新增空集」接口），
 * sequence 自动取项目内 max+1，创建后选中新集由右侧表单补全大纲；
 * 当前集有未保存修改时先弹三选提醒（保存并新增 / 不保存新增 / 取消）。
 * @returns 无
 */
async function addEpisode(): Promise<void> {
  if (!(await ensureSavedBeforeLeave())) return
  adding.value = true
  try {
    const nextSeq = (episodes.value[episodes.value.length - 1]?.sequence ?? 0) + 1
    const created = await episodesApi.create(props.projectId, { title: `第 ${nextSeq} 集` })
    await reloadPlan()
    selectedId.value = created.id
    syncForm()
    ElMessage.success('已新增空集，可点击「AI 一键填充」补全大纲，或手动填写后保存')
  } finally {
    adding.value = false
  }
}

// ============================================================
// 单集大纲 AI 一键填充（建议值回填表单，保存由 PUT outline 承接）
// ============================================================

/** AI 一键填充进行中（按钮 loading 与防重入） */
const autofilling = ref(false)

/**
 * AI 一键填充本集大纲：调 outline/autofill 新接口——后端依据故事设定、
 * 角色设定、本集已填内容与前后集钩子，只补全未填写字段（已填字段原样
 * 返回），**建议值不落库**；此处把建议值回填进编辑表单，用户可继续手动
 * 调整或经 AI 助手改写，最后点「保存本集大纲」（PUT outline）落库留痕。
 * 响应携带 filled_fields（本次被填充的字段名清单）时 toast 提示补全范围。
 * @returns 无
 * @throws 404/409/502 已由 client 拦截器统一提示，此处仅回退 loading
 */
async function handleAutofill(): Promise<void> {
  if (!selectedEpisode.value || autofilling.value) return
  autofilling.value = true
  try {
    const out = await pipelineApi.autofillEpisodeOutline(
      props.projectId, selectedEpisode.value.episode_id)
    // 建议值回填表单（不写回 episodes 列表——未保存的建议不污染集卡展示）；
    // 程序化回填期间抑制 dirty watch，回填后按实际填充情况显式置脏——
    // AI 确实补全了字段 → 置脏（切换时会触发未保存提醒），未做修改 → 保持干净
    suspendDirty = true
    form.value.title = out.title
    form.value.logline = out.logline ?? ''
    form.value.core_event = out.core_event ?? ''
    form.value.characters = out.characters
    form.value.emotion_curve = out.emotion_curve ?? ''
    form.value.duration_sec = out.duration_sec
    formRef.value?.clearValidate()
    nextTick(() => {
      suspendDirty = false
      dirty.value = out.filled_fields.length > 0
    })
    if (out.filled_fields.length) {
      ElMessage.success(`已补全：${out.filled_fields.join('、')}，请确认后保存本集大纲`)
    } else {
      ElMessage.info('本集内容已填写完整，AI 未做修改')
    }
  } finally {
    autofilling.value = false
  }
}

// ============================================================
// 生成分集大纲（LLM，覆盖前二次确认）
// ============================================================

/** 生成中（按钮 loading 与禁用） */
const generating = ref(false)
/** 目标集数（1~100，缺省 1） */
const targetEpisodes = ref(1)

/**
 * 生成分集大纲（仅空态入口调用；已有大纲后不提供整体重生成，
 * 单集调整走「重新生成本集大纲」）；body 阶段后端拒绝（409），
 * 前端按钮已禁用兜底。
 * @returns 无
 */
async function handleGenerate(): Promise<void> {
  generating.value = true
  try {
    const out = await pipelineApi.generateEpisodePlan(props.projectId, {
      target_episodes: targetEpisodes.value,
    })
    episodes.value = out.episodes
    phase.value = out.phase
    emit('phase', out.phase)
    if (!episodes.value.some((e) => e.episode_id === selectedId.value)) {
      selectedId.value = episodes.value[0]?.episode_id ?? null
    }
    syncForm()
    ElMessage.success(`已生成 ${out.episodes.length} 集大纲，可逐集调整后确认`)
  } finally {
    generating.value = false
  }
}

// ============================================================
// 本集大纲编辑表单（PUT outline，空字段不传=后端不修改）
// ============================================================

/** 大纲编辑表单结构 */
interface OutlineForm {
  /** 单集标题 */
  title: string
  /** 一句话梗概 */
  logline: string
  /** 核心事件 */
  core_event: string
  /** 出场角色名列表 */
  characters: string[]
  /** 情绪走向 */
  emotion_curve: string
  /** 单集时长（秒，可空） */
  duration_sec: number | null
}

/** 表单引用（校验触发） */
const formRef = ref<FormInstance>()
/** 编辑表单数据 */
const form = ref<OutlineForm>({
  title: '', logline: '', core_event: '', characters: [], emotion_curve: '', duration_sec: null,
})
/** 保存中（防重复提交） */
const saving = ref(false)

/** 表单校验规则（title 必填 ≤100，与后端列约束一致） */
const rules: FormRules = {
  title: [
    { required: true, message: '请输入单集标题', trigger: 'blur' },
    { max: 100, message: '标题不超过 100 字', trigger: 'blur' },
  ],
}

/** 新增空集时创建接口写入的占位标题（「第 X 集」），表单中视为未填写清空展示 */
const PLACEHOLDER_TITLE_RE = /^第\s*\d+\s*集$/

// ============================================================
// 未保存修改守卫（AI 一键填充 / 手动编辑后切换集或步骤会丢失，切换前弹窗提醒）
// ============================================================

/** 编辑表单是否有未保存修改（AI 填充回填或用户手动编辑后置真，同步库值后复位） */
const dirty = ref(false)

/** 库值同步/程序化回填期间抑制 dirty watch 误报（nextTick 后复位） */
let suspendDirty = false

// 表单任意字段变化即视为脏（覆盖标题/梗概/核心事件/角色/情绪/时长全部输入项）；
// syncForm 与 autofill 回填走 suspendDirty 抑制，由各自逻辑显式控制 dirty
watch(form, () => {
  if (suspendDirty) return
  dirty.value = true
}, { deep: true })

/**
 * 把选中集的大纲字段同步进编辑表单；占位标题（「第 X 集」）在表单中清空——
 * 由用户手填或「AI 一键填充」生成真实标题（保存时 title 必填校验兜底）。
 * 同步后表单与库值一致，未保存修改标记复位（回填期间抑制 dirty watch）。
 * @returns 无
 */
function syncForm(): void {
  suspendDirty = true
  const ep = selectedEpisode.value
  const rawTitle = ep?.title ?? ''
  form.value = {
    title: PLACEHOLDER_TITLE_RE.test(rawTitle.trim()) ? '' : rawTitle,
    logline: ep?.logline ?? '',
    core_event: ep?.core_event ?? '',
    characters: [...(ep?.characters ?? [])],
    emotion_curve: ep?.emotion_curve ?? '',
    duration_sec: ep?.duration_sec ?? null,
  }
  dirty.value = false
  nextTick(() => { suspendDirty = false })
}

/**
 * 离开当前集前的未保存守卫：无修改直接放行；有修改弹三选弹窗——
 * 「保存并切换」走既有 PUT outline（校验不过则留在当前集）、
 * 「不保存切换」放弃修改直接放行、点关闭/ESC 视为取消留在当前集。
 * @returns true=允许离开（无修改/已保存/放弃修改）；false=留在当前集
 */
async function ensureSavedBeforeLeave(): Promise<boolean> {
  if (!dirty.value || !selectedEpisode.value) return true
  try {
    await ElMessageBox.confirm(
      `第 ${selectedEpisode.value.sequence} 集有未保存的修改（含 AI 一键填充内容），` +
      '直接切换后这些内容将丢失。',
      '未保存提醒',
      {
        confirmButtonText: '保存并切换',
        cancelButtonText: '不保存切换',
        distinguishCancelAndClose: true,
        type: 'warning',
      },
    )
  } catch (action) {
    // cancel=点了「不保存切换」→ 放弃修改放行；close（X/ESC/遮罩）→ 取消留步
    return action === 'cancel'
  }
  // 保存失败（表单校验不过 / 接口报错已由拦截器 toast）→ 留在当前集
  return await handleSave().catch(() => false)
}

/**
 * 保存本集大纲：仅提交有值字段（后端 None=不修改；logline/core_event 有
 * min_length=1 校验，空串不可提交），成功后就地更新列表条目。
 * @returns 保存是否成功（校验失败/请求失败返回 false，供切换守卫判断留步）
 */
async function handleSave(): Promise<boolean> {
  const ep = selectedEpisode.value
  if (!ep) return false
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return false
  saving.value = true
  try {
    const payload: EpisodeOutlineEditPayload = { title: form.value.title.trim() }
    if (form.value.logline.trim()) payload.logline = form.value.logline.trim()
    if (form.value.core_event.trim()) payload.core_event = form.value.core_event.trim()
    if (form.value.characters.length) payload.characters = [...form.value.characters]
    if (form.value.emotion_curve.trim()) payload.emotion_curve = form.value.emotion_curve.trim()
    if (form.value.duration_sec != null) payload.duration_sec = form.value.duration_sec
    const updated = await pipelineApi.editEpisodeOutline(props.projectId, ep.episode_id, payload)
    episodes.value = episodes.value
      .map((e) => (e.episode_id === updated.episode_id ? updated : e))
      .sort((a, b) => a.sequence - b.sequence)
    syncForm()
    // 阶段同步：后端保存大纲时自动推进 outline→body（取消「确认大纲」闸口，
    // 保存即定稿），响应携带最新 phase 时同步容器步骤与正文步解锁
    if (updated.phase) {
      phase.value = updated.phase
      emit('phase', updated.phase)
    }
    ElMessage.success('本集大纲已保存，可进入剧本正文生成')
    return true
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="step-episodes" v-loading="loading">
    <!-- body 阶段锁定提示（整体生成/重排停用；单集微调与 AI 一键填充仍可用） -->
    <el-alert
      v-if="locked"
      class="lock-alert"
      type="warning"
      show-icon
      :closable="false"
      title="正文已开始生成，分集大纲整体生成与重排已停用：仍可单集文案微调、新增集并使用「AI 一键填充」补全"
    />

    <!-- 分流路径 B：小说改编分集前置区（未生成分集大纲时展示解析进度/上传入口） -->
    <el-card
      v-if="!loading && !episodes.length && !locked"
      shadow="never"
      class="novel-plan"
    >
      <template v-if="novelChapters.length">
        <div class="novel-plan__head">
          <span class="novel-plan__title">
            小说改编：已解析 {{ novelChapters.length }} 个章节，可按章节自动切分为集
          </span>
          <el-button
            size="small"
            type="primary"
            :loading="chaptersPlanning"
            @click="generatePlanFromChapters"
          >
            按章节生成分集大纲
          </el-button>
        </div>
        <div class="novel-plan__list">
          <el-tag
            v-for="c in novelChapters.slice(0, 5)"
            :key="c.index"
            size="small"
            type="info"
            effect="plain"
            class="novel-plan__chip"
          >
            第{{ c.index }}章 {{ c.title }}（{{ c.char_count }} 字）
          </el-tag>
          <span v-if="novelChapters.length > 5" class="novel-plan__more">
            …共 {{ novelChapters.length }} 章
          </span>
        </div>
      </template>
      <template v-else>
        <div class="novel-plan__head">
          <span class="novel-plan__title">
            小说改编项目可先上传小说解析章节，按章节自动分集（预计超过
            {{ EPISODE_CONFIRM_THRESHOLD }} 集时会先确认）
          </span>
          <el-upload :show-file-list="false" accept=".txt,.epub" :http-request="handleNovelUpload">
            <el-button size="small" :loading="novelParsing">上传小说解析（.txt / .epub）</el-button>
          </el-upload>
        </div>
      </template>
    </el-card>

    <!-- 主体（对齐剧本正文布局）：上方集卡横排（点击选中该集）+ 下方该集大纲编辑区 -->
    <div class="plan-stack">
      <!-- 上方：集卡横向流（第N集 + 标题 + 状态 + 时长），点击切换下方编辑内容；
           空态为首次生成 CTA（目标集数 + 生成按钮，承接原顶部条的入口职责） -->
      <div class="ep-cards">
        <div v-if="!loading && episodes.length === 0" class="plan-gen-cta">
          <p class="plan-gen-tip">
            基于已确认角色与剧本输入，AI 生成分集大纲
          </p>
          <div class="plan-gen-ops">
            <span class="plan-gen-label">目标集数</span>
            <el-input-number v-model="targetEpisodes" :min="1" :max="100" size="small" />
            <el-button type="primary" :loading="generating" :disabled="locked" @click="handleGenerate">
              生成分集大纲
            </el-button>
          </div>
        </div>
        <template v-else-if="episodes.length">
          <div
            v-for="ep in episodes"
            :key="ep.episode_id"
            class="ep-card"
            :class="{ 'ep-card--active': ep.episode_id === selectedId }"
            role="button"
            @click="selectEpisode(ep)"
          >
            <div class="ep-card__head">
              <span class="ep-seq">第{{ ep.sequence }}集</span>
              <el-tag v-if="ep.confirmed_at" size="small" type="success" effect="plain">已确认</el-tag>
            </div>
            <div class="ep-card__title">{{ ep.title }}</div>
            <div class="ep-card__meta">
              <span v-if="ep.duration_sec" class="ep-duration">{{ ep.duration_sec }}s</span>
            </div>
          </div>
          <el-button
            class="add-ep-card"
            text
            type="primary"
            :loading="adding"
            @click="addEpisode"
          >
            <el-icon><Plus /></el-icon>&nbsp;新增集
          </el-button>
        </template>
      </div>

      <!-- 下方：选中集的大纲编辑区（标题行右侧「AI 一键填充」：依据故事设定/
           角色设定/已填内容补全空缺字段，建议值回填表单由用户确认后保存） -->
      <section class="ep-editor">
        <el-card v-if="selectedEpisode" shadow="never">
          <template #header>
            <div class="editor-head">
              <span class="editor-title">第 {{ selectedEpisode.sequence }} 集 · {{ selectedEpisode.title }}</span>
              <div class="editor-ops">
                <el-button
                  size="small"
                  type="primary"
                  plain
                  :loading="autofilling"
                  title="依据故事设定与角色设定，AI 补全本集未填写的内容（已填写内容保留）；空集将全量填充"
                  @click="handleAutofill"
                >
                  AI 一键填充
                </el-button>
              </div>
            </div>
          </template>
          <el-form ref="formRef" :model="form" :rules="rules" label-width="88px">
            <el-form-item label="标题" prop="title">
              <el-input v-model="form.title" maxlength="100" placeholder="单集标题" />
            </el-form-item>
            <el-form-item label="一句话梗概">
              <QuotableText :source="outlineQuoteSource" scope="episode_outline" :target-episode-id="selectedEpisode?.episode_id ?? null" class="quote-field">
                <el-input
                  v-model="form.logline"
                  maxlength="200"
                  show-word-limit
                  placeholder="本集一句话梗概（logline）"
                />
              </QuotableText>
            </el-form-item>
            <el-form-item label="核心事件">
              <QuotableText :source="outlineQuoteSource" scope="episode_outline" :target-episode-id="selectedEpisode?.episode_id ?? null" class="quote-field">
                <el-input
                  v-model="form.core_event"
                  type="textarea"
                  :rows="4"
                  placeholder="本集核心事件（剧情主推进）"
                />
              </QuotableText>
            </el-form-item>
            <el-form-item label="出场角色">
              <el-select
                v-model="form.characters"
                multiple
                filterable
                allow-create
                default-first-option
                placeholder="选择或输入出场角色名"
                style="width: 100%"
              >
                <el-option v-for="name in characterOptions" :key="name" :label="name" :value="name" />
              </el-select>
            </el-form-item>
            <el-form-item label="情绪走向">
              <QuotableText :source="outlineQuoteSource" scope="episode_outline" :target-episode-id="selectedEpisode?.episode_id ?? null" class="quote-field">
                <el-input v-model="form.emotion_curve" placeholder="如：压抑 → 爆发 → 释然" />
              </QuotableText>
            </el-form-item>
            <el-form-item label="单集时长">
              <el-input-number
                v-model="form.duration_sec"
                :min="30"
                :max="3600"
                :step="30"
                :controls="false"
                placeholder="30 ~ 3600 秒"
              />
              <span class="duration-unit">秒</span>
            </el-form-item>
          </el-form>
          <div class="editor-actions">
            <el-button type="primary" :loading="saving" @click="handleSave">保存本集大纲</el-button>
          </div>
        </el-card>
        <el-empty v-else description="从上方选择一集进行大纲编辑" />
      </section>
    </div>
  </div>
</template>

<style scoped>
.step-episodes {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-5);
}
.lock-alert {
  border-radius: var(--mf-r-ctl);
}
/* 空态首次生成 CTA（承接原顶部条入口；虚线引导卡与角色设定 CTA 同款形态） */
.plan-gen-cta {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--mf-sp-3);
  padding: var(--mf-sp-5) var(--mf-sp-4);
  border: 1px dashed var(--mf-border);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-surface-2, var(--mf-surface));
}
.plan-gen-tip {
  margin: 0;
  text-align: center;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
}
.plan-gen-ops {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.plan-gen-label {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
/* 分流路径 B：小说改编分集前置区 */
.novel-plan {
  border-style: dashed;
}
.novel-plan__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-3);
}
.novel-plan__title {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.6;
}
.novel-plan__list {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-top: var(--mf-sp-2);
}
.novel-plan__chip {
  max-width: 320px;
}
.novel-plan__more {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
/* 主体上下堆叠（对齐剧本正文布局）：上方集卡横向流 + 下方该集编辑区 */
.plan-stack {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-4);
}
/* 集卡单行流：不换行、超出横向滑动选择（flex-shrink:0 防卡片被压缩，
   保证每卡保持自身宽度、溢出走横向滚动条） */
.ep-cards {
  display: flex;
  flex-wrap: nowrap;
  align-items: stretch;
  gap: var(--mf-sp-3);
  overflow-x: auto;
  padding-bottom: var(--mf-sp-1);
}
.ep-cards::-webkit-scrollbar {
  height: 6px;
}
.ep-cards::-webkit-scrollbar-thumb {
  border-radius: 3px;
  background: var(--mf-border);
}
.ep-card {
  flex-shrink: 0;
  min-width: 220px;
  max-width: 260px;
  padding: var(--mf-sp-3);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-surface);
  cursor: pointer;
  transition: border-color var(--mf-dur) var(--mf-ease), background var(--mf-dur) var(--mf-ease),
    transform var(--mf-dur) var(--mf-ease);
}
.ep-card:hover {
  border-color: var(--mf-brand);
  transform: translateY(-1px);
}
.ep-card--active {
  border-color: var(--mf-brand);
  background: var(--mf-brand-soft);
  box-shadow: 0 0 0 1px var(--mf-brand) inset;
}
.ep-card__head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.ep-seq {
  flex-shrink: 0;
  color: var(--mf-brand);
  font-weight: var(--mf-fw-semibold);
  font-size: var(--mf-fs-sm);
}
.ep-card__title {
  margin-top: var(--mf-sp-1);
  font-weight: var(--mf-fw-semibold);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}
.ep-card__meta {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  margin-top: var(--mf-sp-1);
  min-height: 18px;
}
.ep-duration {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.add-ep-card {
  align-self: stretch;
  min-height: 64px;
}
.editor-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  font-weight: var(--mf-fw-semibold);
}
.editor-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.editor-ops {
  margin-left: auto;
  display: flex;
  align-items: center;
  flex-shrink: 0;
}
/* 该集编辑区（选中集表单） */
.ep-editor {
  min-width: 0;
}
.duration-unit {
  margin-left: var(--mf-sp-2);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.editor-actions {
  margin-top: var(--mf-sp-2);
  text-align: right;
}
/* 选中文本引用包装：占满表单行宽度（大纲编辑区字段使用） */
.quote-field {
  width: 100%;
}
</style>
