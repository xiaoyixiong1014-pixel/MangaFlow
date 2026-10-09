<!--
  资产管理页（文件级注释）。

  对应原型 S-04（资产管理）+ S-05（资产变体面板），路由 /projects/:id/assets。
  功能：
  - 四 Tab（角色/场景/道具/风格）+ 「生成全部主照」批量按钮（确认框提示张数与积分）；
  - 角色卡网格：有图 el-image 预览 / 无图占位渐变 + 生成按钮；状态 Tag（pending/
    generating/done/failed，generating 期间每 3s 轮询刷新直至全部终态）；
    face_locked 显示 el-icon Lock 徽标；
  - 角色操作：生成定妆照（generateCharacterPortrait→轮询）/确认并锁定（lock→confirm）/
    派生变体（S-05 抽屉，RD-14 继承声明：面部特征必须继承——默认勾选且禁用）/
    重生成 / 编辑提示词（双层抽屉：锁定层只读 visual_prompt + 可编辑层 textarea）/
    上传替换（el-upload 本地选择，演示不落库）/ 删除（confirm + 软删）；
  - 场景卡/道具卡简化：编辑提示词 + 锁定切换；场景无图显示「空场景」占位（RD-19）；
  - 风格 Tab：卡列表 + 设为默认（updateStyle is_default）；
  - 一致性校验：consistencyCheck → el-dialog 并排对比（原图 + 变体图）+
    相似度进度条（passed 绿 / 未通过红）。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Lock } from '@element-plus/icons-vue'
import * as studioApi from '@/api/studio'
import type {
  AssetVariant,
  AssetsGroup,
  CharacterAsset,
  ConsistencyCheckResult,
  PropAsset,
  SceneAsset,
  StyleAsset,
} from '@/types'

const route = useRoute()

/** 当前项目 ID（取路由参数） */
const projectId = computed(() => Number(route.params.id))

// ============================================================
// 资产列表
// ============================================================

/** 首屏加载中（骨架屏） */
const loading = ref(true)
/** 四类资产聚合 */
const assets = ref<AssetsGroup>({ characters: [], scenes: [], props: [], styles: [] })
/** 当前激活 Tab */
const activeTab = ref<'characters' | 'scenes' | 'props' | 'styles'>('characters')

/**
 * 拉取四类资产列表（quiet=true 时不切换骨架屏，供轮询使用）。
 * @param quiet 是否静默刷新
 * @returns 无
 */
async function loadAssets(quiet = false): Promise<void> {
  if (!quiet) loading.value = true
  try {
    assets.value = await studioApi.listAssets(projectId.value)
  } finally {
    if (!quiet) loading.value = false
  }
}

/** 轮询定时器句柄（有角色 generating 时每 3s 刷新） */
let pollTimer: ReturnType<typeof setInterval> | null = null

/**
 * 若存在生成中的角色则启动 3s 轮询，全部终态后自动停止。
 * @returns 无
 */
function ensurePolling(): void {
  const busy = assets.value.characters.some(
    (c) => c.generation_status === 'generating' || c.generation_status === 'pending',
  )
  if (busy && !pollTimer) {
    pollTimer = setInterval(async () => {
      await loadAssets(true)
      const stillBusy = assets.value.characters.some(
        (c) => c.generation_status === 'generating' || c.generation_status === 'pending',
      )
      if (!stillBusy && pollTimer) {
        clearInterval(pollTimer)
        pollTimer = null
        ElMessage.success('全部定妆照生成完成')
      }
    }, 3000)
  }
}

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
})

// ============================================================
// 状态 Tag
// ============================================================

/** 生成状态 → Tag 文案 */
const statusLabel: Record<string, string> = {
  pending: '排队中',
  generating: '生成中',
  done: '已完成',
  failed: '失败',
}
/** 生成状态 → Tag 类型 */
const statusType: Record<string, 'info' | 'warning' | 'success' | 'danger'> = {
  pending: 'info',
  generating: 'warning',
  done: 'success',
  failed: 'danger',
}

// ============================================================
// 定妆照生成（单个 / 全部）
// ============================================================

/** 单张生成请求进行中的角色 ID 集合（防重复点击） */
const genBusyIds = ref<number[]>([])

/**
 * 发起单张定妆照生成并进入轮询。
 * @param c 目标角色
 * @returns 无
 */
async function generatePortrait(c: CharacterAsset): Promise<void> {
  if (genBusyIds.value.includes(c.id)) return
  genBusyIds.value = [...genBusyIds.value, c.id]
  try {
    await studioApi.generateCharacterPortrait(projectId.value, c.id)
    ElMessage.success(`已提交「${c.name}」定妆照生成任务（冻结 10 积分）`)
    await loadAssets(true)
    ensurePolling()
  } finally {
    genBusyIds.value = genBusyIds.value.filter((i) => i !== c.id)
  }
}

/**
 * 生成全部主照：对所有缺图角色批量提交（确认框提示张数与预估积分）。
 * @returns 无
 */
async function generateAll(): Promise<void> {
  const targets = assets.value.characters.filter(
    (c) => !c.asset_url && c.generation_status !== 'generating',
  )
  if (!targets.length) {
    ElMessage.info('所有角色都已有主照')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将为 ${targets.length} 个角色生成主定妆照，预计消耗 ${targets.length * 10} 积分。是否继续？`,
      '生成全部主照',
      { type: 'info', confirmButtonText: '开始生成' },
    )
  } catch {
    return
  }
  for (const c of targets) {
    await studioApi.generateCharacterPortrait(projectId.value, c.id).catch(() => undefined)
  }
  ElMessage.success('批量任务已提交')
  await loadAssets(true)
  ensurePolling()
}

// ============================================================
// 锁定 / 解锁面部层（RD-12）
// ============================================================

/**
 * 确认并锁定角色面部层（confirm 二次确认）。
 * @param c 目标角色
 * @returns 无
 */
async function lockFace(c: CharacterAsset): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `锁定后「${c.name}」的面部特征将在全项目保持一致，重生成不再影响面部。是否锁定？`,
      '锁定面部',
      { type: 'warning' },
    )
  } catch {
    return
  }
  const updated = await studioApi.lockCharacter(projectId.value, c.id)
  Object.assign(c, updated)
  ElMessage.success('面部已锁定')
}

/**
 * 解锁角色面部层。
 * @param c 目标角色
 * @returns 无
 */
async function unlockFace(c: CharacterAsset): Promise<void> {
  const updated = await studioApi.unlockCharacter(projectId.value, c.id)
  Object.assign(c, updated)
  ElMessage.success('面部已解锁')
}

// ============================================================
// 编辑提示词抽屉（RD-04 分层：锁定层只读 + 可编辑层）
// ============================================================

/** 提示词抽屉可见性 */
const promptVisible = ref(false)
/** 当前编辑对象类别 */
const promptKind = ref<'character' | 'scene' | 'prop'>('character')
/** 当前编辑对象名称（仅展示） */
const promptName = ref('')
/** 锁定层内容（系统标准化提示词，只读展示） */
const promptLockedText = ref('')
/** 可编辑层文本（用户补充/覆盖） */
const promptEditableText = ref('')
/** 保存中（防重复提交） */
const promptSaving = ref(false)
/** 当前编辑对象引用（保存后回写） */
let promptTarget: CharacterAsset | SceneAsset | PropAsset | null = null

/**
 * 打开提示词抽屉（角色/场景/道具通用）。
 * @param target 资产对象
 * @param kind 类别
 * @returns 无
 */
function openPromptDrawer(
  target: CharacterAsset | SceneAsset | PropAsset,
  kind: 'character' | 'scene' | 'prop',
): void {
  promptTarget = target
  promptKind.value = kind
  promptName.value = target.name
  promptLockedText.value = target.visual_prompt
  promptEditableText.value = target.visual_prompt
  promptVisible.value = true
}

/**
 * 保存可编辑层提示词（按类别分发到对应更新接口）。
 * @returns 无
 */
async function savePrompt(): Promise<void> {
  if (!promptTarget || promptSaving.value) return
  promptSaving.value = true
  try {
    const text = promptEditableText.value.trim()
    if (!text) {
      ElMessage.warning('提示词不能为空')
      return
    }
    if (promptKind.value === 'character') {
      Object.assign(
        promptTarget,
        await studioApi.updateCharacter(projectId.value, promptTarget.id, { visual_prompt: text }),
      )
    } else if (promptKind.value === 'scene') {
      Object.assign(
        promptTarget,
        await studioApi.updateScene(projectId.value, promptTarget.id, { visual_prompt: text }),
      )
    } else {
      Object.assign(
        promptTarget,
        await studioApi.updateProp(projectId.value, promptTarget.id, { visual_prompt: text }),
      )
    }
    promptLockedText.value = text
    promptVisible.value = false
    ElMessage.success('提示词已更新')
  } finally {
    promptSaving.value = false
  }
}

// ============================================================
// S-05 变体抽屉（RD-14 继承声明）
// ============================================================

/** 变体抽屉可见性 */
const variantVisible = ref(false)
/** 当前变体抽屉的角色 */
const variantChar = ref<CharacterAsset | null>(null)
/** 变体列表 */
const variantList = ref<AssetVariant[]>([])
/** 变体列表加载中 */
const variantLoading = ref(false)
/** 变体类型选项（角色） */
const variantTypeOptions = [
  { label: '服饰', value: 'outfit' },
  { label: '年龄', value: 'age' },
  { label: '其他', value: 'other' },
]
/** 新增变体表单 */
const variantForm = ref({ variant_type: 'outfit', name: '' })
/** 面部特征继承（RD-14：换装必继承 → 默认勾选且禁用） */
const inheritFace = ref(true)
/** 造型独立声明（勾选后服饰/画风作为独立特征生成） */
const outfitIndependent = ref(false)
/** 提交中（防重复点击） */
const variantSubmitting = ref(false)

/**
 * 打开变体抽屉并加载变体列表。
 * @param c 目标角色
 * @returns 无
 */
async function openVariantDrawer(c: CharacterAsset): Promise<void> {
  variantChar.value = c
  variantVisible.value = true
  variantLoading.value = true
  try {
    variantList.value = await studioApi.variants(projectId.value, c.id)
  } finally {
    variantLoading.value = false
  }
}

/**
 * 新增造型变体：inherit_mask 必填（RD-14），面部继承强制 true。
 * @returns 无
 */
async function submitVariant(): Promise<void> {
  if (!variantChar.value || variantSubmitting.value) return
  if (!variantForm.value.name.trim()) {
    ElMessage.warning('请填写变体名称')
    return
  }
  if (!inheritFace.value) {
    // 理论上不可达（checkbox 禁用且默认勾选），双保险阻断
    ElMessage.warning('面部特征必须继承（换装不换脸）')
    return
  }
  variantSubmitting.value = true
  try {
    await studioApi.addVariant(projectId.value, variantChar.value.id, {
      variant_type: variantForm.value.variant_type,
      name: variantForm.value.name.trim(),
      // RD-14 继承声明：face 恒继承；style 独立性由用户勾选决定
      inherit_mask: {
        face: true,
        style: !outfitIndependent.value,
        outfit_independent: outfitIndependent.value,
      },
    })
    ElMessage.success('变体已创建')
    variantForm.value = { variant_type: 'outfit', name: '' }
    outfitIndependent.value = false
    variantList.value = await studioApi.variants(projectId.value, variantChar.value.id)
  } finally {
    variantSubmitting.value = false
  }
}

/** 变体类型 → 中文文案 */
const variantTypeLabel: Record<string, string> = {
  outfit: '服饰',
  age: '年龄',
  lighting: '光线',
  other: '其他',
}

// ============================================================
// 一致性校验（RD-13：并排对比 + 相似度内部判定）
// ============================================================

/** 校验弹窗可见性 */
const ccVisible = ref(false)
/** 校验目标角色 */
const ccChar = ref<CharacterAsset | null>(null)
/** 变体选项（供选择与主形象比对的变体） */
const ccVariants = ref<AssetVariant[]>([])
/** 选中的变体 ID（空 = 与主形象自身比对） */
const ccVariantId = ref<number | undefined>(undefined)
/** 校验进行中 */
const ccLoading = ref(false)
/** 校验结果 */
const ccResult = ref<ConsistencyCheckResult | null>(null)

/**
 * 打开一致性校验弹窗并加载该角色变体。
 * @param c 目标角色
 * @returns 无
 */
async function openConsistency(c: CharacterAsset): Promise<void> {
  ccChar.value = c
  ccResult.value = null
  ccVariantId.value = undefined
  ccVisible.value = true
  try {
    ccVariants.value = await studioApi.variants(projectId.value, c.id)
  } catch {
    ccVariants.value = []
  }
}

/**
 * 执行一致性校验。
 * @returns 无
 */
async function runConsistency(): Promise<void> {
  if (!ccChar.value || ccLoading.value) return
  ccLoading.value = true
  try {
    ccResult.value = await studioApi.consistencyCheck(projectId.value, {
      character_id: ccChar.value.id,
      variant_id: ccVariantId.value,
    })
  } finally {
    ccLoading.value = false
  }
}

/** 右侧对比图（选中变体图，无则回退主形象图） */
const ccRightUrl = computed(
  () =>
    ccVariants.value.find((v) => v.id === ccVariantId.value)?.asset_url
    ?? ccChar.value?.asset_url
    ?? '',
)

// ============================================================
// 上传替换（演示）
// ============================================================

/**
 * 上传替换选择文件回调（演示环境不真传后端）。
 * @param file 选择的文件
 * @returns 无
 */
function onUploadChange(file: { name: string }): void {
  ElMessage.info(`演示环境：已选择「${file.name}」，上传替换暂不落库后端`)
}

// ============================================================
// 删除角色
// ============================================================

/**
 * 删除角色（confirm + 软删除）。
 * @param c 目标角色
 * @returns 无
 */
async function removeCharacter(c: CharacterAsset): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除角色「${c.name}」吗？删除后可联系管理员恢复。`, '删除角色', {
      type: 'warning',
    })
  } catch {
    return
  }
  await studioApi.deleteCharacter(projectId.value, c.id)
  ElMessage.success('角色已删除')
  await loadAssets(true)
}

// ============================================================
// 场景 / 道具锁定切换
// ============================================================

/**
 * 切换场景锁定状态。
 * @param s 目标场景
 * @returns 无
 */
async function toggleSceneLock(s: SceneAsset): Promise<void> {
  const updated = await studioApi.updateScene(projectId.value, s.id, { is_locked: !s.is_locked })
  Object.assign(s, updated)
}

/**
 * 切换道具锁定状态。
 * @param p 目标道具
 * @returns 无
 */
async function togglePropLock(p: PropAsset): Promise<void> {
  const updated = await studioApi.updateProp(projectId.value, p.id, { is_locked: !p.is_locked })
  Object.assign(p, updated)
}

// ============================================================
// 风格设为默认
// ============================================================

/**
 * 把风格设为项目默认（后端自动清除其他默认）。
 * @param st 目标风格
 * @returns 无
 */
async function setDefaultStyle(st: StyleAsset): Promise<void> {
  const updated = await studioApi.updateStyle(projectId.value, st.id, { is_default: true })
  Object.assign(st, updated)
  ElMessage.success(`已将「${st.name}」设为默认风格`)
  await loadAssets(true)
}

onMounted(() => {
  void loadAssets().then(ensurePolling)
})
</script>

<template>
  <div class="page-container assets-view">
    <div class="view-head">
      <h2 class="page-title">资产管理</h2>
      <el-button
        v-if="activeTab === 'characters' && assets.characters.length"
        type="primary"
        plain
        @click="generateAll"
      >
        生成全部主照
      </el-button>
    </div>

    <!-- 首屏骨架 -->
    <el-skeleton v-if="loading" :rows="6" animated />

    <el-empty v-else-if="!assets.characters.length && !assets.scenes.length && !assets.props.length && !assets.styles.length"
      description="还没有资产，请先回到剧本页完成「实体提取确认」"
    >
      <el-button type="primary" @click="$router.push(`/projects/${projectId}/script/extract`)">
        去实体提取
      </el-button>
    </el-empty>

    <el-tabs v-else v-model="activeTab" type="border-card">
      <!-- ============ 角色 Tab ============ -->
      <el-tab-pane :label="`角色 (${assets.characters.length})`">
        <div class="card-grid">
          <el-card v-for="c in assets.characters" :key="c.id" shadow="hover" class="asset-card">
            <div class="img-wrap">
              <el-image
                v-if="c.asset_url"
                :src="c.asset_url"
                :preview-src-list="[c.asset_url]"
                fit="cover"
                class="asset-img"
                preview-teleported
              />
              <div v-else class="img-placeholder">
                <span>{{ c.name }}</span>
                <el-button
                  size="small"
                  type="primary"
                  :loading="genBusyIds.includes(c.id) || c.generation_status === 'generating'"
                  @click="generatePortrait(c)"
                >
                  生成定妆照
                </el-button>
              </div>
              <!-- 面部锁定徽标 -->
              <el-tooltip v-if="c.face_locked" content="面部已锁定" placement="top">
                <el-icon class="lock-badge"><Lock /></el-icon>
              </el-tooltip>
              <el-tag
                class="status-tag"
                size="small"
                effect="dark"
                :type="statusType[c.generation_status] ?? 'info'"
              >
                {{ statusLabel[c.generation_status] ?? c.generation_status }}
              </el-tag>
            </div>
            <div class="card-info">
              <div class="card-name">{{ c.name }}</div>
              <div class="card-desc">{{ c.description || '暂无描述' }}</div>
              <div class="card-ops">
                <el-button
                  v-if="c.asset_url && !c.face_locked"
                  size="small"
                  type="success"
                  plain
                  @click="lockFace(c)"
                >
                  确认并锁定
                </el-button>
                <el-button v-if="c.face_locked" size="small" text @click="unlockFace(c)">解锁</el-button>
                <el-button size="small" text type="primary" @click="openVariantDrawer(c)">变体</el-button>
                <el-dropdown trigger="click" @command="(cmd: string) => {
                  if (cmd === 'regen') generatePortrait(c)
                  else if (cmd === 'prompt') openPromptDrawer(c, 'character')
                  else if (cmd === 'cc') openConsistency(c)
                  else if (cmd === 'del') removeCharacter(c)
                }">
                  <el-button size="small" text>更多</el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="regen">重生成</el-dropdown-item>
                      <el-dropdown-item command="prompt">编辑提示词</el-dropdown-item>
                      <el-dropdown-item command="cc">一致性校验</el-dropdown-item>
                      <el-dropdown-item command="del" divided>删除</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
                <!-- 上传替换（演示：本地选择不落库） -->
                <el-upload
                  :auto-upload="false"
                  :show-file-list="false"
                  accept="image/*"
                  :on-change="(f: { name: string }) => onUploadChange(f)"
                >
                  <el-button size="small" text>上传替换</el-button>
                </el-upload>
              </div>
            </div>
          </el-card>
          <el-empty
            v-if="!assets.characters.length"
            description="暂无角色资产"
            class="grid-empty"
          />
        </div>
      </el-tab-pane>

      <!-- ============ 场景 Tab ============ -->
      <el-tab-pane :label="`场景 (${assets.scenes.length})`">
        <div class="card-grid">
          <el-card v-for="s in assets.scenes" :key="s.id" shadow="hover" class="asset-card">
            <div class="img-wrap">
              <!-- 场景无图：空场景占位（RD-19） -->
              <div v-if="!s.asset_url" class="img-placeholder scene-empty">
                <span class="scene-empty-tag">空场景</span>
                <span>{{ s.name }}</span>
              </div>
              <el-image
                v-else
                :src="s.asset_url"
                :preview-src-list="[s.asset_url]"
                fit="cover"
                class="asset-img"
                preview-teleported
              />
              <el-tag v-if="s.is_locked" class="status-tag" size="small" effect="dark" type="success">
                已锁定
              </el-tag>
            </div>
            <div class="card-info">
              <div class="card-name">{{ s.name }}</div>
              <div class="card-desc">{{ s.description || '暂无描述' }}</div>
              <div class="card-ops">
                <el-button size="small" text type="primary" @click="openPromptDrawer(s, 'scene')">
                  编辑提示词
                </el-button>
                <el-button size="small" text @click="toggleSceneLock(s)">
                  {{ s.is_locked ? '解锁' : '锁定' }}
                </el-button>
              </div>
            </div>
          </el-card>
          <el-empty v-if="!assets.scenes.length" description="暂无场景资产" class="grid-empty" />
        </div>
      </el-tab-pane>

      <!-- ============ 道具 Tab ============ -->
      <el-tab-pane :label="`道具 (${assets.props.length})`">
        <div class="card-grid">
          <el-card v-for="p in assets.props" :key="p.id" shadow="hover" class="asset-card">
            <div class="img-wrap">
              <div v-if="!p.asset_url" class="img-placeholder">
                <span>{{ p.name }}</span>
              </div>
              <el-image
                v-else
                :src="p.asset_url"
                :preview-src-list="[p.asset_url]"
                fit="cover"
                class="asset-img"
                preview-teleported
              />
              <el-tag v-if="p.is_locked" class="status-tag" size="small" effect="dark" type="success">
                已锁定
              </el-tag>
            </div>
            <div class="card-info">
              <div class="card-name">{{ p.name }}</div>
              <div class="card-desc">{{ p.description || '暂无描述' }}</div>
              <div class="card-ops">
                <el-button size="small" text type="primary" @click="openPromptDrawer(p, 'prop')">
                  编辑提示词
                </el-button>
                <el-button size="small" text @click="togglePropLock(p)">
                  {{ p.is_locked ? '解锁' : '锁定' }}
                </el-button>
              </div>
            </div>
          </el-card>
          <el-empty v-if="!assets.props.length" description="暂无道具资产" class="grid-empty" />
        </div>
      </el-tab-pane>

      <!-- ============ 风格 Tab ============ -->
      <el-tab-pane :label="`风格 (${assets.styles.length})`">
        <div class="card-grid">
          <el-card v-for="st in assets.styles" :key="st.id" shadow="hover" class="asset-card">
            <div class="img-wrap">
              <div v-if="!st.reference_url" class="img-placeholder">
                <span>{{ st.name }}</span>
              </div>
              <el-image
                v-else
                :src="st.reference_url"
                :preview-src-list="[st.reference_url]"
                fit="cover"
                class="asset-img"
                preview-teleported
              />
              <el-tag v-if="st.is_default" class="status-tag" size="small" effect="dark">
                默认
              </el-tag>
            </div>
            <div class="card-info">
              <div class="card-name">{{ st.name }}</div>
              <div class="card-desc">{{ st.preset_code || st.visual_prompt }}</div>
              <div class="card-ops">
                <el-button
                  size="small"
                  type="primary"
                  plain
                  :disabled="st.is_default"
                  @click="setDefaultStyle(st)"
                >
                  设为默认
                </el-button>
              </div>
            </div>
          </el-card>
          <el-empty v-if="!assets.styles.length" description="暂无风格资产" class="grid-empty" />
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- ============ 提示词编辑抽屉（两层：锁定层只读 + 可编辑层） ============ -->
    <el-drawer v-model="promptVisible" :title="`编辑提示词 · ${promptName}`" size="440px" append-to-body>
      <div class="layer-label">
        系统锁定层（只读，约束角色/场景一致性的标准化提示词）
      </div>
      <div class="locked-layer">{{ promptLockedText || '（空）' }}</div>
      <div class="layer-label">用户可编辑层（保存后覆盖生效）</div>
      <el-input
        v-model="promptEditableText"
        type="textarea"
        :rows="6"
        placeholder="调整细节描述，例：侧光、胶片质感、唯美二次元"
      />
      <div class="drawer-ops">
        <el-button @click="promptVisible = false">取消</el-button>
        <el-button type="primary" :loading="promptSaving" @click="savePrompt">保存</el-button>
      </div>
    </el-drawer>

    <!-- ============ S-05 变体抽屉 ============ -->
    <el-drawer
      v-model="variantVisible"
      :title="`${variantChar?.name ?? ''} · 造型与变体`"
      size="480px"
      append-to-body
    >
      <div v-loading="variantLoading">
        <div class="layer-label">已有变体</div>
        <el-empty
          v-if="!variantList.length"
          description="暂无变体，可新增造型 / 光线变体"
          :image-size="72"
        />
        <div v-else class="variant-list">
          <div v-for="v in variantList" :key="v.id" class="variant-item">
            <el-image
              v-if="v.asset_url"
              :src="v.asset_url"
              :preview-src-list="[v.asset_url]"
              fit="cover"
              class="variant-img"
              preview-teleported
            />
            <div v-else class="variant-img variant-img--empty">待生成</div>
            <div class="variant-meta">
              <span class="variant-name">{{ v.name }}</span>
              <el-tag size="small" type="info" effect="plain">
                {{ variantTypeLabel[v.variant_type] ?? v.variant_type }}
              </el-tag>
            </div>
          </div>
        </div>

        <el-divider>新增变体</el-divider>
        <el-form label-width="90px">
          <el-form-item label="变体名称" required>
            <el-input v-model="variantForm.name" maxlength="50" placeholder="例：战斗装" />
          </el-form-item>
          <el-form-item label="变体类型" required>
            <el-select v-model="variantForm.variant_type" style="width: 100%">
              <el-option
                v-for="opt in variantTypeOptions"
                :key="opt.value"
                :label="opt.label"
                :value="opt.value"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="继承声明">
            <div class="inherit-box">
              <!-- RD-14：面部特征必须继承（换装必继承），默认勾选且禁用 -->
              <el-checkbox v-model="inheritFace" disabled>面部特征（脸型 / 五官 / 发色）必须继承</el-checkbox>
              <el-checkbox v-model="outfitIndependent">造型独立（服饰 / 画风作为独立特征生成）</el-checkbox>
              <div class="inherit-tip">说明：不勾选的项将作为独立特征生成</div>
            </div>
          </el-form-item>
        </el-form>
        <div class="drawer-ops">
          <el-button
            type="primary"
            :disabled="!inheritFace"
            :loading="variantSubmitting"
            @click="submitVariant"
          >
            生成变体
          </el-button>
        </div>
      </div>
    </el-drawer>

    <!-- ============ 一致性校验弹窗 ============ -->
    <el-dialog v-model="ccVisible" :title="`一致性校验 · ${ccChar?.name ?? ''}`" width="680px" append-to-body>
      <div class="cc-select-row">
        <span>对比对象：</span>
        <el-select v-model="ccVariantId" placeholder="主形象" clearable style="width: 220px">
          <el-option
            v-for="v in ccVariants"
            :key="v.id"
            :label="`${v.name}（${variantTypeLabel[v.variant_type] ?? v.variant_type}）`"
            :value="v.id"
          />
        </el-select>
        <el-button type="primary" :loading="ccLoading" @click="runConsistency">开始校验</el-button>
      </div>
      <div class="cc-compare">
        <div class="cc-pane">
          <div class="cc-pane-title">主形象</div>
          <el-image
            v-if="ccChar?.asset_url"
            :src="ccChar.asset_url"
            fit="contain"
            class="cc-img"
            :preview-src-list="[ccChar.asset_url]"
            preview-teleported
          />
          <div v-else class="cc-img cc-img--empty">暂无主照</div>
        </div>
        <div class="cc-pane">
          <div class="cc-pane-title">对比图</div>
          <el-image
            v-if="ccRightUrl"
            :src="ccRightUrl"
            fit="contain"
            class="cc-img"
            :preview-src-list="[ccRightUrl]"
            preview-teleported
          />
          <div v-else class="cc-img cc-img--empty">暂无对比图</div>
        </div>
      </div>
      <template v-if="ccResult">
        <div class="cc-sim-row">
          <span>相似度：</span>
          <el-progress
            :percentage="Math.round(ccResult.similarity * 100)"
            :stroke-width="14"
            :color="ccResult.passed ? '#67c23a' : '#f56c6c'"
            class="cc-progress"
          />
          <el-tag :type="ccResult.passed ? 'success' : 'danger'" effect="dark">
            {{ ccResult.passed ? '通过' : '未通过' }}
          </el-tag>
        </div>
        <div class="cc-report">{{ ccResult.report }}</div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.assets-view {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-5);
}
.view-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.view-head .page-title {
  margin: 0;
}
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: var(--mf-sp-4);
}
.grid-empty {
  grid-column: 1 / -1;
}
.asset-card :deep(.el-card__body) {
  padding: 0;
}
.img-wrap {
  position: relative;
  height: 180px;
  border-radius: var(--mf-r-ctl) var(--mf-r-ctl) 0 0;
  overflow: hidden;
}
.asset-img {
  width: 100%;
  height: 100%;
  display: block;
}
.img-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--mf-sp-2);
  background: linear-gradient(135deg, var(--mf-surface-2), var(--mf-border));
  color: var(--mf-text-2);
}
.scene-empty-tag {
  font-size: var(--mf-fs-sm);
  color: var(--mf-text-3);
  border: 1px dashed var(--mf-text-3);
  border-radius: var(--mf-r-pill);
  padding: 0 8px;
}
.lock-badge {
  position: absolute;
  top: 8px;
  left: 8px;
  background: rgba(0, 0, 0, 0.55);
  color: #ffd04b;
  border-radius: 50%;
  padding: 5px;
}
.status-tag {
  position: absolute;
  top: 8px;
  right: 8px;
}
.card-info {
  padding: var(--mf-sp-3);
}
.card-name {
  font-weight: var(--mf-fw-semibold);
}
.card-desc {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
  margin: 4px 0 var(--mf-sp-2);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 34px;
}
.card-ops {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 2px;
}
.layer-label {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  margin: var(--mf-sp-2) 0;
}
.locked-layer {
  background: var(--mf-surface-2);
  border-radius: var(--mf-r-ctl);
  padding: var(--mf-sp-3);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
  margin-bottom: var(--mf-sp-4);
}
.drawer-ops {
  margin-top: var(--mf-sp-4);
  text-align: right;
}
.variant-list {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
}
.variant-item {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.variant-img {
  width: 72px;
  height: 72px;
  border-radius: var(--mf-r-ctl);
  flex-shrink: 0;
}
.variant-img--empty {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--mf-surface-2);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.variant-meta {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.variant-name {
  font-weight: var(--mf-fw-semibold);
}
.inherit-box {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.inherit-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.cc-select-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  margin-bottom: var(--mf-sp-4);
}
.cc-compare {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--mf-sp-4);
}
.cc-pane-title {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  margin-bottom: var(--mf-sp-2);
  text-align: center;
}
.cc-img {
  width: 100%;
  height: 220px;
  background: var(--mf-surface-2);
  border-radius: var(--mf-r-ctl);
}
.cc-img--empty {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--mf-text-3);
}
.cc-sim-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  margin-top: var(--mf-sp-4);
}
.cc-progress {
  flex: 1;
}
.cc-report {
  margin-top: var(--mf-sp-3);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
}
</style>
