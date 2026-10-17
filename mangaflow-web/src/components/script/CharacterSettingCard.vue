<!--
  剧本工作台 · 卡片② 角色设定（文件级注释，自 StorySettingCard 拆分为独立步骤）。

  职责：智能体依据用户创意（剧本 raw_text）确定角色班底，人设卡展示与编辑，
  确认入池后分集大纲生成即以该名单为强约束（连接下游），确认动作推进
  script_phase characters → outline（容器据此跃迁到分集大纲步）：
  - 无候选：AI 生成 CTA（虚线引导 + 「AI 生成角色设定」主按钮）；
    创意未保存时后端以「请先保存剧本输入」守卫拦截（新建项目创建时已自动写入）；
  - 有候选：人设卡单行横滑（与分集大纲/剧本正文一致，超出横向滑动选择）——
    卡片为摘要态（姓名/定位/年龄/性格两行截断），点击选中（主色描边高亮）；
  - 下方角色编辑区（原点击弹出详情弹窗已迁移至此）：点击卡片后显示在卡片栏
    下方——候选=编辑表单（删除/保存），已确认=只读详情（单角色重生成，修改
    请到资产库），手动新增=空表单；操作按钮统一收拢在编辑区标题行；
  - 确认入池主按钮：全部未确认候选入池（characters 表），confirmedReached
    （outline/body）后卡片置灰、按钮隐藏并显示「已确认入池」标签。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { Lock, Plus, RefreshRight } from '@element-plus/icons-vue'
import * as pipelineApi from '@/api/scriptPipeline'
import { extractDetail } from '@/api/client'
import QuotableText from './QuotableText.vue'
import type { AxiosError } from 'axios'
import type { CharacterProfile, RoleType, ScriptPhase } from '@/api/scriptPipeline'

/** 组件入参 */
const props = defineProps<{
  /** 当前项目 ID（角色域接口路径参数） */
  projectId: number
  /** 是否为当前查看步骤（外壳高亮） */
  active?: boolean
}>()

/** 组件事件 */
const emit = defineEmits<{
  /** 阶段变化（生成/确认等回执携带最新 script_phase，容器据此推进步骤与解锁） */
  'phase-changed': [phase: ScriptPhase | null]
}>()

// ============================================================
// 核心状态
// ============================================================

/** 首屏加载中（骨架） */
const loading = ref(false)
/** 首屏加载失败（错误 el-alert + 重试） */
const loadError = ref(false)
/** 人物候选列表（config.script_characters） */
const planned = ref<CharacterProfile[]>([])
/** 已入池角色列表（confirmed_at 非空） */
const confirmedList = ref<CharacterProfile[]>([])
/** 当前剧本创作阶段 */
const phase = ref<ScriptPhase | null>(null)

/** 角色班底生成中（AI 依据创意产出候选） */
const generatingChars = ref(false)
/** 确认入池进行中 */
const confirming = ref(false)

/** 是否已过确认（outline/body：卡片置灰禁编、确认按钮隐藏） */
const confirmedReached = computed(() => phase.value === 'outline' || phase.value === 'body')

/**
 * 拉取角色双列表并初始化（复用故事设定详情接口，取 planned/confirmed/phase）。
 * @param _allowAuto 兼容保留参数（无自动分流）
 * @returns 无
 * @throws 接口错误由拦截器统一提示，此处仅置错误态
 */
async function init(_allowAuto = true): Promise<void> {
  loading.value = true
  loadError.value = false
  try {
    const out = await pipelineApi.getStorySetting(props.projectId)
    planned.value = out.planned
    confirmedList.value = out.confirmed
    phase.value = out.phase
    emit('phase-changed', out.phase)
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

onMounted(() => void init())

// ============================================================
// AI 生成角色班底
// ============================================================

/**
 * AI 生成角色设定（智能体依据用户创意确定角色班底）：
 * 调 characters/generate——后端基于剧本 raw_text（创意）产出候选写入
 * config.script_characters，NULL/idea 阶段推进为 characters；创意未保存时
 * 由后端「请先保存剧本输入」守卫拦截（新建项目创建时已自动写入）。
 * @returns 无
 * @throws 接口错误由拦截器统一提示
 */
async function handleGenerateChars(): Promise<void> {
  if (generatingChars.value) return
  generatingChars.value = true
  try {
    const out = await pipelineApi.generateCharacters(props.projectId)
    planned.value = out.characters
    phase.value = out.phase
    emit('phase-changed', out.phase)
    ElMessage.success(`已生成 ${out.characters.length} 个角色候选，可编辑后确认入池`)
  } catch (e) {
    ElMessage.error(extractDetail(e as AxiosError))
  } finally {
    generatingChars.value = false
  }
}

// ============================================================
// 卡片展示结构（双列表合并）
// ============================================================

const confirmedNames = computed(() => new Set(confirmedList.value.map((c) => c.name)))

/** 卡片展示结构：角色档案 + 是否已确认标记 */
interface DisplayCharacter extends CharacterProfile {
  /** 是否已完成确认入池 */
  isConfirmed: boolean
}

/**
 * 合并渲染列表：已入池卡片在前，候选中未确认的追加在后（同名候选不再重复展示）。
 */
const displayItems = computed<DisplayCharacter[]>(() => {
  const items: DisplayCharacter[] = confirmedList.value.map((c) => ({ ...c, isConfirmed: true }))
  for (const cand of planned.value) {
    if (confirmedNames.value.has(cand.name)) continue
    items.push({ ...cand, isConfirmed: false })
  }
  return items
})

/** 角色定位标签元信息（tag 颜色 + 中文文案） */
const ROLE_TYPE_META: Record<RoleType, { label: string; tag: 'primary' | 'success' | 'danger' | 'info' }> = {
  lead: { label: '主角', tag: 'primary' },
  support: { label: '配角', tag: 'success' },
  villain: { label: '反派', tag: 'danger' },
  extra: { label: '群像', tag: 'info' },
}

/** 定位下拉选项（与后端 RoleType 白名单一致） */
const ROLE_TYPE_OPTIONS: Array<{ value: RoleType, label: string }> = [
  { value: 'lead', label: '主角' },
  { value: 'support', label: '配角' },
  { value: 'villain', label: '反派' },
  { value: 'extra', label: '群像' },
]

// ============================================================
// 下方编辑区（原弹窗迁移：点击角色卡在卡栏下方显示详情/编辑表单）
// ============================================================

/** 下方编辑区可见性（点击卡片或手动新增时打开，关闭按钮收起） */
const panelOpen = ref(false)
/** 编辑区模式：true=只读详情（已确认角色）；false=编辑/新增（候选） */
const detailMode = ref(false)
/** 当前编辑区对应的角色卡（删除/重生成操作的目标；新增模式为 null） */
const activeItem = ref<DisplayCharacter | null>(null)
/** 编辑目标候选 key（null=新增；值为候选临时 candidate_id） */
const editingKey = ref<string | null>(null)
/** 保存中（防重复提交） */
const savingChar = ref(false)
/** 表单引用（校验触发） */
const formRef = ref<FormInstance>()

/**
 * 把角色卡数据填充为编辑表单（选择卡片/保存后刷新/重生成后刷新共用）。
 * @param item 目标角色卡
 * @returns 无
 */
function fillForm(item: DisplayCharacter): void {
  form.value = {
    name: item.name,
    role_type: item.role_type,
    age: item.age ?? '',
    personality: item.personality ?? '',
    appearance: item.appearance ?? '',
    outfit: item.outfit ?? '',
    style_note: item.style_note ?? '',
    motivation: item.motivation ?? '',
    arc: item.arc ?? '',
    relations: item.relations.map((r) => ({ target: r.target, relation: r.relation })),
  }
}

/** 关系草稿行（编辑态中间结构） */
interface RelationDraft {
  /** 关系目标角色名 */
  target: string
  /** 关系描述 */
  relation: string
}

/** 角色编辑表单结构 */
interface CharacterForm {
  /** 角色名 */
  name: string
  /** 角色定位 */
  role_type: RoleType | null
  /** 年龄设定 */
  age: string
  /** 性格设定 */
  personality: string
  /** 形象特征（角色圣经视觉段一：发型/五官/体型/肤色） */
  appearance: string
  /** 服装配饰完整拆解（角色圣经视觉段二：上装/下装/鞋履/配饰） */
  outfit: string
  /** 风格与画质特点（角色圣经视觉段三：画风/光线/画质） */
  style_note: string
  /** 行为动机 */
  motivation: string
  /** 人物弧光 */
  arc: string
  /** 人物关系草稿行 */
  relations: RelationDraft[]
}

/**
 * 空白表单工厂（新增态）。
 * @returns 空白角色表单
 */
function emptyForm(): CharacterForm {
  return {
    name: '', role_type: null, age: '', personality: '',
    appearance: '', outfit: '', style_note: '',
    motivation: '', arc: '', relations: [],
  }
}

/** 当前表单数据（弹窗内双向绑定） */
const form = ref<CharacterForm>(emptyForm())

/** 表单校验规则（name 必填 ≤50、age ≤20，与后端 Pydantic 约束一致） */
const rules: FormRules = {
  name: [
    { required: true, message: '请输入角色名', trigger: 'blur' },
    { max: 50, message: '角色名不超过 50 字', trigger: 'blur' },
  ],
  age: [{ max: 20, message: '年龄表述不超过 20 字', trigger: 'blur' }],
}

/**
 * 关系行动态校验规则（target/relation 均必填且限长，与后端 CharacterRelation 一致）。
 * @param field 校验字段（target / relation）
 * @returns 校验规则数组
 */
function relationRules(field: 'target' | 'relation'): FormRules[string] {
  const max = field === 'target' ? 50 : 100
  const label = field === 'target' ? '关系目标' : '关系描述'
  return [
    { required: true, message: `请输入${label}`, trigger: 'blur' },
    { max, message: `${label}不超过 ${max} 字`, trigger: 'blur' },
  ]
}

/**
 * 选中角色卡并在下方编辑区显示：候选 → 编辑表单；已确认 → 只读详情。
 * @param item 目标角色卡
 * @returns 无
 */
function selectChar(item: DisplayCharacter): void {
  panelOpen.value = true
  detailMode.value = item.isConfirmed
  activeItem.value = item
  editingKey.value = item.isConfirmed ? null : item.candidate_id
  fillForm(item)
}

/**
 * 从当前双列表中取回指定角色（编辑区重生成/保存成功后刷新表单用）。
 * @param item 操作前的角色卡
 * @returns 更新后的角色卡（未找到为 null）
 */
function findRefreshed(item: DisplayCharacter): DisplayCharacter | null {
  const key = item.candidate_id ?? String(item.id)
  return (
    displayItems.value.find(
      (c) => (c.candidate_id ?? String(c.id ?? '')) === key,
    ) ?? null
  )
}

/**
 * 切换到新增模式：下方编辑区显示空表单（保存时按 name 匹配未命中即新增候选）。
 * @returns 无
 */
function openCreateChar(): void {
  panelOpen.value = true
  detailMode.value = false
  activeItem.value = null
  editingKey.value = null
  form.value = emptyForm()
}

/**
 * 收起下方编辑区（关闭/取消按钮）。
 * @returns 无
 */
function closePanel(): void {
  panelOpen.value = false
  activeItem.value = null
}

/** 当前选中角色的 key（卡片区高亮用；新增模式为 null） */
const selectedKey = computed(() =>
  activeItem.value ? (activeItem.value.candidate_id ?? String(activeItem.value.id)) : null)

/**
 * 保存表单：调批量编辑接口 upsert 单条（编辑带候选临时 ID，新增仅按 name）。
 * 保存成功后编辑区保持打开并刷新为落库后的最新数据（新增后自动切到新卡编辑态）。
 * @returns 无
 * @throws 接口错误由拦截器统一提示，编辑区保留表单
 */
async function handleSaveChar(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  savingChar.value = true
  try {
    // 过滤掉半填的关系行（target/relation 缺一的行视为无效）
    const relations = form.value.relations
      .map((r) => ({ target: r.target.trim(), relation: r.relation.trim() }))
      .filter((r) => r.target && r.relation)
    const entry: pipelineApi.CharacterUpsertPayload = {
      name: form.value.name.trim(),
      role_type: form.value.role_type,
      age: form.value.age.trim() || null,
      personality: form.value.personality.trim() || null,
      appearance: form.value.appearance.trim() || null,
      outfit: form.value.outfit.trim() || null,
      style_note: form.value.style_note.trim() || null,
      motivation: form.value.motivation.trim() || null,
      arc: form.value.arc.trim() || null,
      relations,
    }
    // 编辑已有候选：携带候选临时 ID（后端 id 优先匹配，支持改名）；新增不传 id
    if (editingKey.value) entry.id = editingKey.value
    const out = await pipelineApi.editCharacters(props.projectId, {
      upsert: [entry],
      remove_ids: [],
      remove_names: [],
    })
    planned.value = out.characters
    emit('phase-changed', out.phase)
    ElMessage.success(editingKey.value ? '角色设定已保存' : '已新增角色候选')
    // 刷新编辑区：编辑按 key 找回新卡（支持改名）；新增按 name 找回新候选卡
    const refreshed = editingKey.value && activeItem.value
      ? findRefreshed(activeItem.value)
      : displayItems.value.find((c) => !c.isConfirmed && c.name === entry.name) ?? null
    if (refreshed) {
      activeItem.value = refreshed
      editingKey.value = refreshed.candidate_id
      detailMode.value = refreshed.isConfirmed
      fillForm(refreshed)
    }
  } finally {
    savingChar.value = false
  }
}

// ---------- 删除 / 单角色重生成 ----------

/**
 * 删除候选角色（confirm 后调批量编辑 remove；已确认角色按钮禁用不会进入）。
 * @param item 目标候选卡
 * @returns 无
 * @throws 接口错误由拦截器统一提示
 */
async function handleRemoveChar(item: DisplayCharacter): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除候选角色「${item.name}」吗？`, '删除角色', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch {
    return // 用户取消
  }
  // 候选必有临时 ID；异常缺失时兜底按 name 移除（后端 remove_names 同样支持）
  const out = await pipelineApi.editCharacters(props.projectId, {
    upsert: [],
    remove_ids: item.candidate_id ? [item.candidate_id] : [],
    remove_names: item.candidate_id ? [] : [item.name],
  })
  planned.value = out.characters
  emit('phase-changed', out.phase)
  ElMessage.success('已删除候选角色')
  // 从编辑区删除时收起编辑区
  if (activeItem.value?.candidate_id === item.candidate_id) closePanel()
}

/** 正在重新生成的角色 key（候选临时 ID 或已入池数字 ID 串），用于按钮 loading */
const regeneratingKey = ref<string | null>(null)

/**
 * 单角色重新生成：成功后按来源原位替换该卡（候选按 candidate_id，已入池按数字 id）。
 * @param item 目标角色卡
 * @returns 无
 * @throws 502 LLM 生成失败由拦截器统一提示
 */
async function handleRegenerateChar(item: DisplayCharacter): Promise<void> {
  const key = item.candidate_id ?? (item.id != null ? String(item.id) : '')
  if (!key) return
  try {
    await ElMessageBox.confirm(
      `将基于创意输入重新设计「${item.name}」的全部人设设定，现有设定将被覆盖。是否继续？`,
      '重新生成角色',
      { type: 'warning', confirmButtonText: '重新生成', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  regeneratingKey.value = key
  try {
    const out = await pipelineApi.regenerateCharacter(props.projectId, key)
    const nc = out.character
    if (nc.source === 'planned') {
      planned.value = planned.value.map((c) => (c.candidate_id === nc.candidate_id ? nc : c))
    } else {
      confirmedList.value = confirmedList.value.map((c) => (c.id === nc.id ? nc : c))
    }
    emit('phase-changed', out.phase)
    ElMessage.success(`「${nc.name}」已重新生成`)
    // 编辑区内重生成时同步刷新表单与目标卡引用
    if (activeItem.value) {
      const refreshed = findRefreshed(activeItem.value)
      if (refreshed) {
        activeItem.value = refreshed
        fillForm(refreshed)
      }
    }
  } finally {
    regeneratingKey.value = null
  }
}

// ============================================================
// 确认入池（连接分集大纲）
// ============================================================

/** 待确认候选（未出现在已确认名单中的候选，确认按钮可用性依据） */
const pendingCandidates = computed(() =>
  planned.value.filter((c) => !confirmedNames.value.has(c.name)))

/**
 * 确认角色入池并推进分集大纲：全部未确认候选调 confirmCharacters 入池
 * （characters 表成为全链路角色事实源），phase 推进 outline 后分集大纲
 * 生成即以该名单为强约束。
 * @returns 无
 * @throws 接口错误由拦截器统一提示，状态回退不推进
 */
async function handleConfirm(): Promise<void> {
  if (!pendingCandidates.value.length) return
  confirming.value = true
  try {
    const out = await pipelineApi.confirmCharacters(props.projectId, {
      characters: pendingCandidates.value.map((c) => ({
        name: c.name,
        role_type: c.role_type,
        age: c.age,
        personality: c.personality,
        appearance: c.appearance,
        outfit: c.outfit,
        style_note: c.style_note,
        motivation: c.motivation,
        arc: c.arc,
        relations: c.relations,
      })),
    })
    ElMessage.success(`角色设定已确认（${out.confirmed_count} 个人物入池），进入分集大纲`)
    phase.value = out.phase
    emit('phase-changed', out.phase)
    // 刷新双列表（确认标记置灰展示）；候选已全部入池，收起下方编辑区
    const detail = await pipelineApi.getStorySetting(props.projectId)
    planned.value = detail.planned
    confirmedList.value = detail.confirmed
    closePanel()
  } finally {
    confirming.value = false
  }
}

/** 组件暴露（容器在助手面板采纳/回退后调 reload 刷新） */
defineExpose({
  /**
   * 重拉角色双列表（不触发生成）。
   * @returns 刷新完成信号
   */
  reload: (): Promise<void> => init(false),
})
</script>

<template>
  <el-card shadow="never" class="cs-card" :class="{ 'cs-card--active': props.active }">
    <template #header>
      <div class="cs-head">
        <span class="cs-title"><span class="cs-no">②</span> 角色设定</span>
        <el-tag size="small" type="primary" effect="light">当前步骤</el-tag>
        <span class="cs-desc">AI 依创意确定角色班底 · 确认入池后约束分集大纲</span>
      </div>
    </template>

    <!-- 首屏骨架 -->
    <el-skeleton v-if="loading" :rows="6" animated />

    <!-- 首屏加载失败 -->
    <el-alert
      v-else-if="loadError"
      type="error"
      show-icon
      :closable="false"
      title="角色设定加载失败"
      description="接口错误详情已由全局提示展示，请检查网络后重试"
    >
      <el-button size="small" type="primary" @click="init(false)">重试</el-button>
    </el-alert>

    <template v-else>
      <div class="char-section">
        <div class="char-head">
          <span class="char-title">主要人物小传</span>
          <el-tag v-if="confirmedReached" size="small" type="success" effect="dark">
            已确认入池
          </el-tag>
          <div class="char-head__ops">
            <el-button
              v-if="displayItems.length && !confirmedReached"
              size="small"
              :loading="generatingChars"
              @click="handleGenerateChars"
            >
              重新生成角色
            </el-button>
            <el-button size="small" :disabled="generatingChars" @click="openCreateChar">
              <el-icon><Plus /></el-icon>&nbsp;手动新增
            </el-button>
          </div>
        </div>

        <!-- 无候选：AI 生成 CTA（依据已保存的创意确定角色班底） -->
        <div v-if="displayItems.length === 0" class="char-gen-cta">
          <p class="char-gen-tip">
            AI 将根据你的创意确定主要角色班底（姓名 / 定位 / 年龄 / 性格 / 外貌 / 动机 / 弧光 / 关系），
            确认入池后用于分集大纲与剧本正文生成
          </p>
          <el-button type="primary" :loading="generatingChars" @click="handleGenerateChars">
            AI 生成角色设定
          </el-button>
        </div>

        <!-- 人设卡单行横滑（与分集大纲/剧本正文一致）：不换行、超出横向滑动选择；
             卡片为摘要态（姓名 + 定位 + 性格两行预览），点击在下方编辑区查看/修改全部字段 -->
        <div v-else class="char-cards">
          <el-card
            v-for="item in displayItems"
            :key="item.candidate_id ?? item.id"
            shadow="never"
            class="char-card"
            :class="{
              'char-card--confirmed': item.isConfirmed,
              'char-card--clickable': !item.isConfirmed,
              'char-card--active': (item.candidate_id ?? String(item.id)) === selectedKey,
            }"
            role="button"
            :title="item.isConfirmed ? '查看角色详情（已确认，请到资产库修改）' : '点击查看与编辑角色'"
            @click="selectChar(item)"
          >
            <div class="char-card__head">
              <span class="char-name">{{ item.name }}</span>
              <el-tag
                v-if="item.role_type"
                size="small"
                :type="ROLE_TYPE_META[item.role_type].tag"
                effect="plain"
              >
                {{ ROLE_TYPE_META[item.role_type].label }}
              </el-tag>
              <el-tag v-if="item.isConfirmed" size="small" type="success" effect="dark">
                已确认
              </el-tag>
            </div>
            <div v-if="item.age" class="char-field">
              <span class="char-label">年龄</span><span class="char-value">{{ item.age }}</span>
            </div>
            <div class="char-field">
              <span class="char-label">性格</span>
              <span class="char-value char-value--clamp">{{ item.personality || '—' }}</span>
            </div>
            <div class="char-field">
              <span class="char-label">服装</span>
              <span class="char-value char-value--clamp">{{ item.outfit || '—' }}</span>
            </div>
          </el-card>
        </div>

        <!-- ===== 下方角色编辑区（原弹窗迁移，点击卡片展开）：候选=编辑表单（可删除）；
             已确认=只读详情（字段禁编，可单角色重生成，修改请到资产库）；操作按钮
             统一收拢在编辑区标题行（与分集大纲编辑区一致） ===== -->
        <div v-if="panelOpen" class="char-editor">
          <div class="char-editor__head">
            <span class="char-editor__title">
              {{
                detailMode
                  ? `角色详情 · ${activeItem?.name ?? ''}`
                  : editingKey
                    ? `编辑角色 · ${activeItem?.name ?? ''}`
                    : '新增角色候选'
              }}
            </span>
            <span v-if="detailMode" class="char-editor__hint">已确认角色请到资产库修改</span>
            <div class="char-editor__ops">
              <!-- 只读详情（已确认）：重生成可用，修改到资产库 -->
              <el-button
                v-if="detailMode && activeItem"
                :loading="regeneratingKey === (activeItem.candidate_id ?? String(activeItem.id))"
                @click="handleRegenerateChar(activeItem)"
              >
                重新生成
              </el-button>
              <!-- 编辑候选：删除 + 取消 + 保存 -->
              <el-button
                v-if="!detailMode && editingKey && activeItem"
                text
                type="danger"
                @click="handleRemoveChar(activeItem)"
              >
                删除
              </el-button>
              <el-button @click="closePanel">{{ detailMode ? '关闭' : '取消' }}</el-button>
              <el-button
                v-if="!detailMode"
                type="primary"
                :loading="savingChar"
                @click="handleSaveChar"
              >
                保存
              </el-button>
            </div>
          </div>
          <el-form
            ref="formRef"
            :model="form"
            :rules="rules"
            label-width="72px"
            :disabled="detailMode"
            class="char-editor__form"
          >
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="角色名" prop="name">
                  <el-input v-model="form.name" maxlength="50" placeholder="角色名（必填）" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="定位">
                  <el-select v-model="form.role_type" placeholder="选择定位" clearable style="width: 100%">
                    <el-option
                      v-for="opt in ROLE_TYPE_OPTIONS"
                      :key="opt.value"
                      :label="opt.label"
                      :value="opt.value"
                    />
                  </el-select>
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item label="年龄" prop="age">
              <el-input v-model="form.age" maxlength="20" placeholder="弹性表述，如「约30岁」「少年」" />
            </el-form-item>
            <el-form-item label="性格">
              <QuotableText :source="`角色设定 · ${activeItem?.name ?? '新增'} · 性格`" scope="characters" class="quote-field">
                <el-input v-model="form.personality" type="textarea" :rows="2" placeholder="性格设定" />
              </QuotableText>
            </el-form-item>
            <el-form-item label="形象特征">
              <QuotableText :source="`角色设定 · ${activeItem?.name ?? '新增'} · 形象特征`" scope="characters" class="quote-field">
                <el-input v-model="form.appearance" type="textarea" :rows="2" placeholder="形象特征：发型/五官/体型/肤色等（生图一致性锁定）" />
              </QuotableText>
            </el-form-item>
            <el-form-item label="服装配饰">
              <QuotableText :source="`角色设定 · ${activeItem?.name ?? '新增'} · 服装配饰`" scope="characters" class="quote-field">
                <el-input v-model="form.outfit" type="textarea" :rows="3" placeholder="服装配饰完整拆解：上装/下装/鞋履/配饰/标志性物品，具体到颜色与材质" />
              </QuotableText>
            </el-form-item>
            <el-form-item label="风格画质">
              <QuotableText :source="`角色设定 · ${activeItem?.name ?? '新增'} · 风格画质`" scope="characters" class="quote-field">
                <el-input v-model="form.style_note" type="textarea" :rows="2" placeholder="风格与画质：画风/光线/画质关键词（与项目视觉风格一致）" />
              </QuotableText>
            </el-form-item>
            <el-form-item label="动机">
              <QuotableText :source="`角色设定 · ${activeItem?.name ?? '新增'} · 动机`" scope="characters" class="quote-field">
                <el-input v-model="form.motivation" type="textarea" :rows="2" placeholder="行为动机" />
              </QuotableText>
            </el-form-item>
            <el-form-item label="弧光">
              <QuotableText :source="`角色设定 · ${activeItem?.name ?? '新增'} · 弧光`" scope="characters" class="quote-field">
                <el-input v-model="form.arc" type="textarea" :rows="2" placeholder="人物弧光（角色成长线）" />
              </QuotableText>
            </el-form-item>
            <el-form-item
              v-for="(r, ri) in form.relations"
              :key="ri"
              :label="`关系 ${ri + 1}`"
              :prop="`relations.${ri}.target`"
              :rules="relationRules('target')"
            >
              <div class="rel-edit-row">
                <el-input v-model="r.target" maxlength="50" placeholder="目标角色名" />
                <el-form-item
                  :prop="`relations.${ri}.relation`"
                  :rules="relationRules('relation')"
                  class="rel-edit-inner"
                >
                  <el-input v-model="r.relation" maxlength="100" placeholder="关系描述，如「宿敌」" />
                </el-form-item>
                <el-button v-if="!detailMode" text type="danger" @click="form.relations.splice(ri, 1)">
                  移除
                </el-button>
              </div>
            </el-form-item>
            <el-form-item v-if="!detailMode" label=" ">
              <el-button text type="primary" @click="form.relations.push({ target: '', relation: '' })">
                + 添加人物关系
              </el-button>
            </el-form-item>
          </el-form>
        </div>

        <!-- 确认入池主按钮（连接分集大纲：入池名单即大纲生成的角色强约束） -->
        <div v-if="!confirmedReached && displayItems.length" class="char-confirm">
          <el-button
            type="primary"
            :loading="confirming"
            :disabled="generatingChars || !pendingCandidates.length"
            @click="handleConfirm"
          >
            确认角色，进入分集大纲{{ pendingCandidates.length ? `（${pendingCandidates.length} 个待入池）` : '' }}
          </el-button>
        </div>
      </div>
    </template>
  </el-card>
</template>

<style scoped>
/* 外壳卡：当前查看步骤高亮（2px 主色描边） */
.cs-card {
  border-radius: var(--mf-r-ctl);
  transition: border-color var(--mf-dur) var(--mf-ease);
}
.cs-card--active {
  border: 2px solid var(--mf-brand);
}
.cs-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.cs-title {
  font-weight: var(--mf-fw-semibold);
}
.cs-no {
  color: var(--mf-brand);
  margin-right: var(--mf-sp-1);
}
.cs-desc {
  margin-left: auto;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}

/* 角色区：标题行 + 网格 + 确认行 */
.char-section {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-4);
}
.char-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.char-title {
  font-weight: var(--mf-fw-semibold);
}
.char-head__ops {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}

/* AI 生成 CTA（无候选引导） */
.char-gen-cta {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--mf-sp-3);
  padding: var(--mf-sp-5) var(--mf-sp-4);
  border: 1px dashed var(--mf-border);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-surface-2, var(--mf-surface));
}
.char-gen-tip {
  margin: 0;
  max-width: 560px;
  text-align: center;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
}

/* 人设卡单行横滑（与分集大纲 ep-cards / 剧本正文 ep-list 同款）：
   不换行、超出横向滑动选择，6px 细滚动条 */
.char-cards {
  display: flex;
  flex-direction: row;
  flex-wrap: nowrap;
  align-items: stretch;
  gap: var(--mf-sp-2);
  overflow-x: auto;
  padding-bottom: var(--mf-sp-1);
}
.char-cards::-webkit-scrollbar {
  height: 6px;
}
.char-cards::-webkit-scrollbar-thumb {
  border-radius: 3px;
  background: var(--mf-border);
}
/* 人设卡 */
.char-card {
  flex-shrink: 0;
  width: 210px;
}
.char-card :deep(.el-card__body) {
  padding: var(--mf-sp-3);
}
.char-card__head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-1);
  margin-bottom: var(--mf-sp-2);
}
.char-name {
  font-weight: var(--mf-fw-semibold);
  margin-right: var(--mf-sp-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.char-field {
  display: flex;
  align-items: flex-start;
  gap: var(--mf-sp-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.6;
  margin-bottom: var(--mf-sp-1);
}
.char-label {
  flex-shrink: 0;
  color: var(--mf-text-3);
}
.char-value {
  flex: 1;
  min-width: 0;
  word-break: break-word;
}
/* 长文本字段两行截断（点击展开全文，再点收起） */
.char-value--clamp {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  cursor: pointer;
}
.char-value--clamp:hover {
  color: var(--mf-brand);
}
/* 人物关系 tag 列：约束在卡片内，超长省略号（修复溢出） */
.char-rels {
  display: flex;
  flex-wrap: wrap;
  gap: var(--mf-sp-1);
  margin-top: var(--mf-sp-2);
  overflow: hidden;
}
.rel-tag {
  max-width: 100%;
  margin: 0;
}
.rel-tag :deep(.el-tag__content) {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.char-ops {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-1);
  margin-top: var(--mf-sp-2);
  padding-top: var(--mf-sp-2);
  border-top: 1px dashed var(--mf-border);
}
/* 确认入池主按钮行：右下角 */
.char-confirm {
  display: flex;
  justify-content: flex-end;
}
/* 摘要卡点击态：候选卡 hover 主色描边；选中卡主色描边高亮（新增模式无高亮） */
.char-card--clickable {
  cursor: pointer;
  transition: border-color var(--mf-dur) var(--mf-ease), transform var(--mf-dur) var(--mf-ease);
}
.char-card--clickable:hover {
  border-color: var(--mf-brand);
  transform: translateY(-1px);
}
.char-card--active {
  border-color: var(--mf-brand);
  box-shadow: 0 0 0 2px var(--mf-brand-soft);
}

/* 下方角色编辑区（原弹窗迁移）：标题行（模式标题 + 提示 + 操作按钮）+ 表单 */
.char-editor {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
  padding: var(--mf-sp-4);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-surface-2, var(--mf-surface));
}
.char-editor__head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--mf-sp-2);
}
.char-editor__title {
  font-weight: var(--mf-fw-semibold);
}
.char-editor__hint {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.char-editor__ops {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.char-editor__form {
  min-width: 0;
}
/* 选中文本引用包装：占满表单行宽度（编辑区 textarea 使用） */
.quote-field {
  width: 100%;
}
/* 已确认卡片置灰 */
.char-card--confirmed {
  opacity: 0.72;
}
</style>
