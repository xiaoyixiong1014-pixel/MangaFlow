<!--
  剧本工作台 · 步骤 2 角色设定页（文件级注释，P-1 范式级重构 Task 10.2）。

  功能：
  - 角色卡网格：姓名 / 定位（lead 主角 / support 配角 / villain 反派 / extra 群像）/
    年龄 / 性格 / 外貌 / 动机 / 弧光 / 关系（→目标：关系 标签组）；
  - 顶部「生成角色」：调 generate 基于剧本输入生成 4~8 个候选人设卡（覆盖前二次确认）；
  - 单卡操作：编辑（dialog 表单，字段校验与后端约束一致）、新增（空表单）、
    删除（confirm 后调批量编辑 remove）、重新生成（loading 态，成功后原位替换该卡）；
  - 底部固定操作条：「确认角色，进入分集大纲」调 confirm（缺省字段沿用候选值），
    成功后 emit phase 推进步骤条；已确认卡片置灰标记，编辑/删除禁用（提示走资产库）；
  - 错误处理：409/502 由 client 拦截器统一弹后端 detail，组件内只做状态回退。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { Plus, RefreshRight } from '@element-plus/icons-vue'
import * as pipelineApi from '@/api/scriptPipeline'
import type { CharacterProfile, RoleType, ScriptPhase } from '@/api/scriptPipeline'

/** 组件入参 */
const props = defineProps<{
  /** 当前项目 ID（角色域接口路径参数） */
  projectId: number
}>()

/** 组件事件 */
const emit = defineEmits<{
  /** 阶段变化（生成/确认等接口回执携带最新 script_phase，通知容器推进步骤条） */
  phase: [phase: ScriptPhase | null]
}>()

// ============================================================
// 角色列表状态（候选 planned + 已入池 confirmed 双列表合并渲染）
// ============================================================

/** 首屏加载中（骨架） */
const loading = ref(false)
/** 候选角色列表（config.script_characters） */
const planned = ref<CharacterProfile[]>([])
/** 已入池角色列表（confirmed_at 非空） */
const confirmedList = ref<CharacterProfile[]>([])

/** 已确认角色名集合（候选同名视为已完成确认，避免重复展示） */
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
const ROLE_TYPE_OPTIONS: Array<{ value: RoleType; label: string }> = [
  { value: 'lead', label: '主角' },
  { value: 'support', label: '配角' },
  { value: 'villain', label: '反派' },
  { value: 'extra', label: '群像' },
]

/**
 * 拉取角色双列表并同步阶段给容器。
 * @returns 无
 */
async function load(): Promise<void> {
  loading.value = true
  try {
    const out = await pipelineApi.listCharacters(props.projectId)
    planned.value = out.planned
    confirmedList.value = out.confirmed
    emit('phase', out.phase)
  } finally {
    loading.value = false
  }
}

// ============================================================
// 生成角色（LLM 批量生成候选，覆盖前二次确认）
// ============================================================

/** 生成中（按钮 loading 与禁用） */
const generating = ref(false)

/**
 * 生成角色班底候选：已有候选时先二次确认（后端整体覆盖候选列表）。
 * @returns 无
 */
async function handleGenerate(): Promise<void> {
  if (planned.value.length > 0) {
    try {
      await ElMessageBox.confirm(
        '将基于剧本输入重新生成全部候选角色，现有候选（含未确认的手工修改）将被覆盖。是否继续？',
        '生成角色',
        { type: 'warning', confirmButtonText: '重新生成', cancelButtonText: '取消' },
      )
    } catch {
      return // 用户取消
    }
  }
  generating.value = true
  try {
    const out = await pipelineApi.generateCharacters(props.projectId)
    planned.value = out.characters
    emit('phase', out.phase)
    ElMessage.success(`已生成 ${out.characters.length} 个候选角色，可编辑调整后确认`)
  } finally {
    generating.value = false
  }
}

// ============================================================
// 编辑 / 新增候选（dialog 表单，校验口径与后端字段约束一致）
// ============================================================

/** 编辑弹窗可见性 */
const dialogVisible = ref(false)
/** 编辑目标候选 key（null=新增；值为候选临时 candidate_id） */
const editingKey = ref<string | null>(null)
/** 保存中（防重复提交） */
const saving = ref(false)
/** 表单引用（校验触发） */
const formRef = ref<FormInstance>()

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
  /** 角色性别（男性/女性/中性，可空=未设定） */
  gender: string
  /** 性格设定 */
  personality: string
  /** 外观设定 */
  appearance: string
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
    name: '', role_type: null, age: '', gender: '', personality: '',
    appearance: '', motivation: '', arc: '', relations: [],
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
 * 打开编辑弹窗（携带候选现有设定）。
 * @param item 目标角色卡
 * @returns 无
 */
function openEdit(item: DisplayCharacter): void {
  editingKey.value = item.candidate_id
  form.value = {
    name: item.name,
    role_type: item.role_type,
    age: item.age ?? '',
    gender: item.gender ?? '',
    personality: item.personality ?? '',
    appearance: item.appearance ?? '',
    motivation: item.motivation ?? '',
    arc: item.arc ?? '',
    relations: item.relations.map((r) => ({ target: r.target, relation: r.relation })),
  }
  dialogVisible.value = true
}

/**
 * 打开新增弹窗（空表单；保存时按 name 匹配未命中即新增候选）。
 * @returns 无
 */
function openCreate(): void {
  editingKey.value = null
  form.value = emptyForm()
  dialogVisible.value = true
}

/**
 * 保存表单：调批量编辑接口 upsert 单条（编辑带候选临时 ID，新增仅按 name）。
 * @returns 无
 */
async function handleSave(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  saving.value = true
  try {
    // 过滤掉半填的关系行（target/relation 缺一的行视为无效）
    const relations = form.value.relations
      .map((r) => ({ target: r.target.trim(), relation: r.relation.trim() }))
      .filter((r) => r.target && r.relation)
    const entry: pipelineApi.CharacterUpsertPayload = {
      name: form.value.name.trim(),
      role_type: form.value.role_type,
      age: form.value.age.trim() || null,
      gender: form.value.gender.trim() || null,
      personality: form.value.personality.trim() || null,
      appearance: form.value.appearance.trim() || null,
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
    emit('phase', out.phase)
    dialogVisible.value = false
    ElMessage.success(editingKey.value ? '角色设定已保存' : '已新增角色候选')
  } finally {
    saving.value = false
  }
}

// ============================================================
// 删除 / 重新生成 / 确认入池
// ============================================================

/**
 * 删除候选角色（confirm 后调批量编辑 remove；已确认角色按钮禁用不会进入）。
 * RD-08 说明：候选角色尚未落库、无数字资产 ID，无法调 entities/impact 影响面
 * 预览；已确认角色在本页无删除/编辑入口，故影响面预览改在资产库页（AssetsView
 * 单删角色）接入。
 * @param item 目标候选卡
 * @returns 无
 */
async function handleRemove(item: DisplayCharacter): Promise<void> {
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
  emit('phase', out.phase)
  ElMessage.success('已删除候选角色')
}

/** 正在重新生成的角色 key（候选临时 ID 或已入池数字 ID 串），用于按钮 loading */
const regeneratingKey = ref<string | null>(null)

/**
 * 单角色重新生成：成功后按来源原位替换该卡（候选按 candidate_id，已入池按数字 id）。
 * @param item 目标角色卡
 * @returns 无
 */
async function handleRegenerate(item: DisplayCharacter): Promise<void> {
  const key = item.candidate_id ?? (item.id != null ? String(item.id) : '')
  if (!key) return
  try {
    await ElMessageBox.confirm(
      `将基于剧本输入重新设计「${item.name}」的全部人设设定，现有设定将被覆盖。是否继续？`,
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
    emit('phase', out.phase)
    ElMessage.success(`「${nc.name}」已重新生成`)
  } finally {
    regeneratingKey.value = null
  }
}

/** 待确认候选（未出现在已确认名单中的候选，确认按钮可用性依据） */
const pendingCandidates = computed(() =>
  planned.value.filter((c) => !confirmedNames.value.has(c.name)))

/** 是否全部完成确认（无可确认候选且存在已入池角色） */
const allConfirmed = computed(
  () => pendingCandidates.value.length === 0 && confirmedList.value.length > 0)

/** 确认请求进行中 */
const confirming = ref(false)

/**
 * 确认角色入池并推进阶段：把全部未确认候选按现值提交（缺省字段由后端沿用候选值），
 * 成功后 emit phase（容器推进到分集大纲）并重新拉取双列表刷新「已确认」标记。
 * @returns 无
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
        gender: c.gender,
        personality: c.personality,
        appearance: c.appearance,
        motivation: c.motivation,
        arc: c.arc,
        relations: c.relations,
      })),
    })
    ElMessage.success(`已确认 ${out.confirmed_count} 个角色，进入分集大纲`)
    emit('phase', out.phase)
    await load()
  } finally {
    confirming.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="step-characters" v-loading="loading">
    <!-- 顶部：生成入口 + 提示文案 -->
    <div class="top-bar">
      <div class="top-tip">基于剧本输入生成 4~8 个角色人设卡，可编辑调整后确认入池</div>
      <div class="top-ops">
        <el-button :disabled="generating" @click="openCreate">
          <el-icon><Plus /></el-icon>&nbsp;手动新增
        </el-button>
        <el-button type="primary" :loading="generating" @click="handleGenerate">
          {{ planned.length ? '重新生成角色' : '生成角色' }}
        </el-button>
      </div>
    </div>

    <!-- 角色卡网格 -->
    <el-empty
      v-if="!loading && displayItems.length === 0"
      description="还没有角色人设卡，点击右上角「生成角色」基于剧本输入创建"
    />
    <el-row v-else :gutter="16" class="char-grid">
      <el-col
        v-for="item in displayItems"
        :key="item.candidate_id ?? item.id"
        :xs="24"
        :sm="12"
        :md="8"
      >
        <el-card
          shadow="never"
          class="char-card"
          :class="{ 'char-card--confirmed': item.isConfirmed }"
        >
          <div class="char-head">
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
          <div class="char-fields">
            <div v-if="item.gender" class="char-field">
              <span class="char-label">性别</span><span class="char-value">{{ item.gender }}</span>
            </div>
            <div v-if="item.age" class="char-field">
              <span class="char-label">年龄</span><span class="char-value">{{ item.age }}</span>
            </div>
            <div v-if="item.personality" class="char-field">
              <span class="char-label">性格</span><span class="char-value">{{ item.personality }}</span>
            </div>
            <div v-if="item.appearance" class="char-field">
              <span class="char-label">外貌</span><span class="char-value">{{ item.appearance }}</span>
            </div>
            <div v-if="item.motivation" class="char-field">
              <span class="char-label">动机</span><span class="char-value">{{ item.motivation }}</span>
            </div>
            <div v-if="item.arc" class="char-field">
              <span class="char-label">弧光</span><span class="char-value">{{ item.arc }}</span>
            </div>
          </div>
          <div v-if="item.relations.length" class="char-rels">
            <el-tag
              v-for="(r, ri) in item.relations"
              :key="ri"
              size="small"
              type="info"
              effect="plain"
              class="rel-tag"
            >
              → {{ r.target }}：{{ r.relation }}
            </el-tag>
          </div>
          <div class="char-ops">
            <el-tooltip
              content="已确认角色请到资产库修改"
              placement="top"
              :disabled="!item.isConfirmed"
            >
              <span>
                <el-button
                  size="small"
                  text
                  type="primary"
                  :disabled="item.isConfirmed"
                  @click="openEdit(item)"
                >
                  编辑
                </el-button>
              </span>
            </el-tooltip>
            <el-button
              size="small"
              text
              type="primary"
              :loading="regeneratingKey === (item.candidate_id ?? String(item.id))"
              @click="handleRegenerate(item)"
            >
              <el-icon v-if="regeneratingKey !== (item.candidate_id ?? String(item.id))">
                <RefreshRight />
              </el-icon>&nbsp;重新生成
            </el-button>
            <el-tooltip
              content="已确认角色不可删除，请到资产库管理"
              placement="top"
              :disabled="!item.isConfirmed"
            >
              <span>
                <el-button
                  size="small"
                  text
                  type="danger"
                  :disabled="item.isConfirmed"
                  @click="handleRemove(item)"
                >
                  删除
                </el-button>
              </span>
            </el-tooltip>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 底部固定操作条：确认角色，推进分集大纲 -->
    <div class="footer-bar">
      <div class="footer-tip">
        <template v-if="allConfirmed">角色已完成确认入池，可进入下一步</template>
        <template v-else-if="!pendingCandidates.length">请先生成或手动新增角色候选</template>
        <template v-else>确认后 {{ pendingCandidates.length }} 个候选角色将写入项目角色资产库</template>
      </div>
      <el-button
        size="large"
        type="primary"
        class="confirm-btn"
        :disabled="!pendingCandidates.length"
        :loading="confirming"
        @click="handleConfirm"
      >
        确认角色，进入分集大纲
      </el-button>
    </div>

    <!-- 编辑 / 新增弹窗 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editingKey ? '编辑角色' : '新增角色候选'"
      width="640px"
      append-to-body
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="72px">
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
        <el-form-item label="性别">
          <el-select v-model="form.gender" placeholder="角色性别（生图性别锚定）" clearable style="width: 100%">
            <el-option label="男性" value="男性" />
            <el-option label="女性" value="女性" />
            <el-option label="中性" value="中性" />
          </el-select>
        </el-form-item>
        <el-form-item label="性格">
          <el-input v-model="form.personality" type="textarea" :rows="2" placeholder="性格设定" />
        </el-form-item>
        <el-form-item label="外貌">
          <el-input v-model="form.appearance" type="textarea" :rows="2" placeholder="外观设定（确认后映射角色 description 摘要）" />
        </el-form-item>
        <el-form-item label="动机">
          <el-input v-model="form.motivation" type="textarea" :rows="2" placeholder="行为动机" />
        </el-form-item>
        <el-form-item label="弧光">
          <el-input v-model="form.arc" type="textarea" :rows="2" placeholder="人物弧光（角色成长线）" />
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
            <el-button text type="danger" @click="form.relations.splice(ri, 1)">移除</el-button>
          </div>
        </el-form-item>
        <el-form-item label=" ">
          <el-button text type="primary" @click="form.relations.push({ target: '', relation: '' })">
            + 添加人物关系
          </el-button>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.step-characters {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-5);
}
.top-bar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.top-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.top-ops {
  margin-left: auto;
  display: flex;
  gap: var(--mf-sp-2);
}
.char-grid {
  row-gap: var(--mf-sp-4);
}
/* 已确认卡片整体置灰，标记不可再编辑 */
.char-card--confirmed {
  opacity: 0.72;
  background: var(--mf-surface-2);
}
.char-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-2);
}
.char-name {
  font-weight: var(--mf-fw-semibold);
  font-size: var(--mf-fs-md);
}
.char-fields {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.char-field {
  display: flex;
  gap: 6px;
  font-size: var(--mf-fs-sm);
  line-height: 1.6;
}
.char-label {
  flex-shrink: 0;
  color: var(--mf-text-3);
}
.char-label::after {
  content: '：';
}
.char-value {
  color: var(--mf-text-2);
  word-break: break-word;
}
.char-rels {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: var(--mf-sp-2);
}
.rel-tag {
  max-width: 100%;
}
.char-ops {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-1);
  margin-top: var(--mf-sp-3);
  padding-top: var(--mf-sp-2);
  border-top: 1px dashed var(--mf-border);
}
.footer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-4);
  position: sticky;
  bottom: 0;
  z-index: 10;
  padding: var(--mf-sp-3);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
}
.footer-tip {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.confirm-btn {
  background: var(--mf-brand);
  border-color: var(--mf-brand);
  color: var(--mf-bg);
}
.confirm-btn:hover {
  opacity: 0.9;
}
/* 关系编辑行：目标 / 描述 / 移除按钮水平排布 */
.rel-edit-row {
  display: flex;
  align-items: flex-start;
  gap: var(--mf-sp-2);
  width: 100%;
}
.rel-edit-inner {
  flex: 1;
  margin-bottom: 0 !important;
}
</style>
