<!--
  分镜设计页（文件级注释）。

  对应原型 S-06，路由 /projects/:id/storyboard。
  功能：
  - 空态：无分镜时展示「AI 拆解分镜」大按钮（decompose → loading → 刷新列表）；
  - 顶部工具条：总时长 / 预计成本（分镜数 × 10 积分文案，RD-20 拆解阶段成本可见）、
    节奏预警徽标（点开抽屉：storyboardWarnings → 告警 el-alert 列表，RD-22）、
    「投递画布」按钮（deliverToCanvas → 成功后确认打开画布）；
  - 分镜卡片列表：vuedraggable 未安装（且禁止新增依赖），改用「上移 / 下移」按钮 +
    reorder(ordered_ids) 实现重排，注释说明为拖拽的替代实现；
  - 每卡展示：镜号 / 景别 / 运镜 / 时长 / 画面描述 / 对白 / 绑定资产 Tag
    （角色 + 场景；link_confidence < 0.7 标黄提示确认，RD-24）；
    regen_count ≥ 3 显示「建议投递画布精修」Tag（RD-25）；
  - 每卡操作：编辑（el-dialog 表单：景别/运镜/时长/画面/对白/prompt_editable/
    强绑场景 + 角色多选）、拆分（按对白行号）、合并（相邻两镜）、重生成（计数 +1）；
  - 底部「确认分镜，进入生成」→ 跳 /projects/:id/production。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Warning } from '@element-plus/icons-vue'
import * as studioApi from '@/api/studio'
import type { AssetsGroup, Storyboard, StoryboardWarning } from '@/types'

const route = useRoute()
const router = useRouter()

/** 当前项目 ID（取路由参数） */
const projectId = computed(() => Number(route.params.id))

// ============================================================
// 分镜列表
// ============================================================

/** 首屏加载中 */
const loading = ref(true)
/** 分镜列表（按 sequence 升序） */
const boards = ref<Storyboard[]>([])

/**
 * 拉取分镜列表（quiet=true 静默刷新）。
 * @param quiet 是否静默
 * @returns 无
 */
async function loadBoards(quiet = false): Promise<void> {
  if (!quiet) loading.value = true
  try {
    boards.value = await studioApi.listStoryboard(projectId.value)
  } finally {
    if (!quiet) loading.value = false
  }
}

// ============================================================
// AI 拆解
// ============================================================

/** 拆解进行中（防重复点击） */
const decomposing = ref(false)

/**
 * AI 拆解分镜（LLM 依据剧本拆出镜头序列，全量重建）。
 * @returns 无
 */
async function runDecompose(): Promise<void> {
  if (decomposing.value) return
  decomposing.value = true
  try {
    const res = await studioApi.decompose(projectId.value)
    const total = Number(res.total ?? 0)
    ElMessage.success(
      `拆解完成，共生成 ${total} 个分镜${res.degraded ? '（部分场景走了规则降级）' : ''}`,
    )
    await loadBoards(true)
  } finally {
    decomposing.value = false
  }
}

// ============================================================
// 顶部工具条统计
// ============================================================

/** 分镜总时长（秒） */
const totalDuration = computed(() => boards.value.reduce((sum, b) => sum + (b.duration || 0), 0))
/** 预估成本（每镜 10 积分文案，RD-20） */
const estimatedCost = computed(() => boards.value.length * 10)

// ============================================================
// 节奏预警抽屉（RD-22 文字化时长预警）
// ============================================================

/** 预警抽屉可见性 */
const warnVisible = ref(false)
/** 预警加载中 */
const warnLoading = ref(false)
/** 告警列表 */
const warnList = ref<StoryboardWarning[]>([])
/** 总时长与目标时长（展示用） */
const warnSummary = ref<{ total_duration: number; target_duration: number | null }>({
  total_duration: 0,
  target_duration: null,
})
/** 未读预警数徽标（拉取过一次后显示） */
const warnCount = ref<number | null>(null)

/**
 * 打开节奏预警抽屉并拉取告警。
 * @returns 无
 */
async function openWarnings(): Promise<void> {
  warnVisible.value = true
  warnLoading.value = true
  try {
    const res = await studioApi.storyboardWarnings(projectId.value)
    warnList.value = res.warnings ?? []
    warnSummary.value = {
      total_duration: res.total_duration,
      target_duration: res.target_duration ?? null,
    }
    warnCount.value = warnList.value.length
  } finally {
    warnLoading.value = false
  }
}

// ============================================================
// 重排（上移 / 下移按钮替代拖拽）
// ============================================================

/** 重排进行中（防连点） */
const reorderBusy = ref(false)

/**
 * 上移 / 下移某镜并调 reorder 落库。
 * 说明：vuedraggable 未安装（禁止新增依赖），此处用相邻交换按钮替代拖拽，
 * 交互等价：每次交换后把最新顺序整体提交 reorder(ordered_ids)。
 * @param index 当前下标
 * @param dir -1 上移 / 1 下移
 * @returns 无
 */
async function moveBoard(index: number, dir: -1 | 1): Promise<void> {
  const target = index + dir
  if (target < 0 || target >= boards.value.length || reorderBusy.value) return
  reorderBusy.value = true
  try {
    const ids = boards.value.map((b) => b.id)
    // 相邻交换
    const tmp = ids[index]!
    ids[index] = ids[target]!
    ids[target] = tmp
    await studioApi.reorder(projectId.value, ids)
    await loadBoards(true)
  } finally {
    reorderBusy.value = false
  }
}

// ============================================================
// 编辑分镜
// ============================================================

/** 编辑弹窗可见性 */
const editVisible = ref(false)
/** 编辑表单（对应 StoryboardUpdatePayload） */
const editForm = ref({
  shot_type: '',
  camera_movement: '',
  duration: 5,
  visual_description: '',
  dialogue: '',
  prompt_editable: '',
  scene_id: undefined as number | undefined,
  character_ids: [] as number[],
})
/** 正在编辑的分镜 ID */
let editId = 0
/** 场景资产选项 / 角色资产选项（表单下拉用） */
const sceneOptions = ref<{ id: number; name: string }[]>([])
const charOptions = ref<{ id: number; name: string }[]>([])
/** 保存中（防重复提交） */
const editSaving = ref(false)

/** 景别选项 */
const shotTypeOptions = ['全景', '远景', '中景', '近景', '特写']
/** 运镜选项 */
const cameraOptions = ['固定', '推镜', '拉镜', '摇镜', '移镜', '跟随']

/**
 * 打开编辑弹窗（预填当前分镜；同时拉取资产选项）。
 * @param b 目标分镜
 * @returns 无
 */
async function openEdit(b: Storyboard): Promise<void> {
  editId = b.id
  editForm.value = {
    shot_type: b.shot_type ?? '',
    camera_movement: b.camera_movement ?? '',
    duration: b.duration,
    visual_description: b.visual_description ?? '',
    dialogue: b.dialogue ?? '',
    prompt_editable: b.prompt_editable ?? '',
    scene_id: b.scene_id ?? undefined,
    character_ids: b.characters.map((c) => c.character_id),
  }
  editVisible.value = true
  // 拉取场景/角色资产作为下拉与多选选项
  if (!sceneOptions.value.length || !charOptions.value.length) {
    const group: AssetsGroup = await studioApi.listAssets(projectId.value)
    sceneOptions.value = group.scenes.map((s) => ({ id: s.id, name: s.name }))
    charOptions.value = group.characters.map((c) => ({ id: c.id, name: c.name }))
  }
}

/**
 * 保存分镜编辑（character_links 整体替换）。
 * @returns 无
 */
async function saveEdit(): Promise<void> {
  if (editSaving.value) return
  editSaving.value = true
  try {
    await studioApi.updateStoryboard(projectId.value, editId, {
      shot_type: editForm.value.shot_type || undefined,
      camera_movement: editForm.value.camera_movement || undefined,
      duration: editForm.value.duration,
      visual_description: editForm.value.visual_description,
      dialogue: editForm.value.dialogue,
      prompt_editable: editForm.value.prompt_editable,
      scene_id: editForm.value.scene_id,
      character_links: editForm.value.character_ids.map((cid) => ({ character_id: cid })),
    })
    editVisible.value = false
    ElMessage.success('分镜已更新')
    await loadBoards(true)
  } finally {
    editSaving.value = false
  }
}

// ============================================================
// 拆分（RD-21 文字级操作：按对白行号一分为二）
// ============================================================

/** 拆分弹窗可见性 */
const splitVisible = ref(false)
/** 拆分目标分镜 */
const splitBoard = ref<Storyboard | null>(null)
/** 拆分行号（1 起，前 at_line 行留本镜） */
const splitAtLine = ref(1)
/** 拆分对白行列表（展示用） */
const splitLines = computed(() =>
  (splitBoard.value?.dialogue ?? '').split('\n').filter((l) => l.trim()),
)
/** 拆分进行中 */
const splitSaving = ref(false)

/**
 * 打开拆分弹窗（无对白时提示不可拆）。
 * @param b 目标分镜
 * @returns 无
 */
function openSplit(b: Storyboard): void {
  const lines = (b.dialogue ?? '').split('\n').filter((l) => l.trim())
  if (lines.length < 2) {
    ElMessage.warning('本镜对白不足两行，无法拆分')
    return
  }
  splitBoard.value = b
  splitAtLine.value = 1
  splitVisible.value = true
}

/**
 * 确认拆分。
 * @returns 无
 */
async function confirmSplit(): Promise<void> {
  if (!splitBoard.value || splitSaving.value) return
  splitSaving.value = true
  try {
    await studioApi.split(projectId.value, splitBoard.value.id, splitAtLine.value)
    splitVisible.value = false
    ElMessage.success('拆分完成')
    await loadBoards(true)
  } finally {
    splitSaving.value = false
  }
}

// ============================================================
// 合并（选择相邻镜）
// ============================================================

/**
 * 与下一镜合并（保留当前镜，删除下一镜）。
 * @param b 当前分镜
 * @returns 无
 */
async function mergeWithNext(b: Storyboard): Promise<void> {
  const idx = boards.value.findIndex((x) => x.id === b.id)
  const next = boards.value[idx + 1]
  if (!next) {
    ElMessage.warning('已是最后一镜，无下一镜可合并')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将把 #${next.sequence} 合并进 #${b.sequence}（对白拼接、时长相加，#${next.sequence} 删除）。是否继续？`,
      '合并分镜',
      { type: 'warning' },
    )
  } catch {
    return
  }
  await studioApi.merge(projectId.value, b.id, next.id)
  ElMessage.success('合并完成')
  await loadBoards(true)
}

// ============================================================
// 重生成（RD-25：≥3 次建议投递画布）
// ============================================================

/** 重生成中的分镜 ID 集合（防重复点击） */
const regenBusyIds = ref<number[]>([])

/**
 * 重生成本镜内容（LLM 依据最新剧本/资产重写）。
 * @param b 目标分镜
 * @returns 无
 */
async function regenBoard(b: Storyboard): Promise<void> {
  if (regenBusyIds.value.includes(b.id)) return
  regenBusyIds.value = [...regenBusyIds.value, b.id]
  try {
    const res = await studioApi.regen(projectId.value, b.id)
    b.regen_count = res.regen_count
    ElMessage.success(
      res.suggest_canvas
        ? `已重生成（累计 ${res.regen_count} 次），建议投递画布精修`
        : `已重生成（累计 ${res.regen_count} 次）`,
    )
  } finally {
    regenBusyIds.value = regenBusyIds.value.filter((i) => i !== b.id)
  }
}

// ============================================================
// 投递画布
// ============================================================

/** 投递进行中 */
const delivering = ref(false)

/**
 * 投递当前分镜到 AI 画布（建 studio_linked 画布），成功后确认打开。
 * @param b 来源分镜
 * @returns 无
 */
async function deliver(b: Storyboard): Promise<void> {
  if (delivering.value) return
  delivering.value = true
  try {
    const res = await studioApi.deliverToCanvas(projectId.value, b.id)
    try {
      await ElMessageBox.confirm('已创建关联画布，是否立即打开精修？', '投递画布', {
        type: 'success',
        confirmButtonText: '打开画布',
        cancelButtonText: '稍后再说',
      })
      await router.push(`/canvas/${res.canvas_id}`)
    } catch {
      /* 用户选择稍后打开 */
    }
  } finally {
    delivering.value = false
  }
}

/**
 * 确认分镜，进入生成页。
 * @returns 无
 */
async function gotoProduction(): Promise<void> {
  await router.push(`/projects/${projectId.value}/production`)
}

onMounted(loadBoards)
</script>

<template>
  <div class="page-container sb-view">
    <div class="view-head">
      <h2 class="page-title">分镜设计</h2>
      <!-- 节奏预警徽标（RD-22） -->
      <el-badge
        :value="warnCount ?? undefined"
        :hidden="!warnCount"
        type="warning"
        class="warn-badge"
      >
        <el-button :icon="Warning" @click="openWarnings">节奏预警</el-button>
      </el-badge>
    </div>

    <!-- 首屏骨架 -->
    <el-skeleton v-if="loading" :rows="7" animated />

    <!-- 空态：AI 拆解引导 -->
    <el-card v-else-if="!boards.length" shadow="never">
      <el-empty description="还没有分镜。AI 将依据剧本逐场景拆解为镜头序列（每镜 1~3 个）">
        <el-button type="primary" size="large" :loading="decomposing" @click="runDecompose">
          AI 拆解分镜
        </el-button>
      </el-empty>
    </el-card>

    <template v-else>
      <!-- 顶部工具条：成本可见（RD-20） -->
      <div class="toolbar">
        <span>共 <b>{{ boards.length }}</b> 镜</span>
        <el-divider direction="vertical" />
        <span>总时长 <b>{{ Math.floor(totalDuration / 60) }}分{{ totalDuration % 60 }}秒</b>（{{ totalDuration }}s）</span>
        <el-divider direction="vertical" />
        <span class="toolbar-cost">预估生成消耗：{{ boards.length }} 镜 ≈ {{ estimatedCost }} 积分</span>
        <el-button class="toolbar-btn" type="primary" plain @click="runDecompose" :loading="decomposing">
          重新拆解
        </el-button>
      </div>

      <!-- 分镜卡片列表（上移/下移按钮替代拖拽） -->
      <div class="board-list">
        <el-card v-for="(b, i) in boards" :key="b.id" shadow="hover" class="board-card">
          <template #header>
            <div class="board-head">
              <span class="board-no">#{{ String(b.sequence).padStart(2, '0') }}</span>
              <el-tag size="small" effect="plain">{{ b.shot_type || '未定景别' }}</el-tag>
              <el-tag size="small" effect="plain" type="info">{{ b.camera_movement || '固定' }}</el-tag>
              <el-tag size="small" effect="plain" type="info">{{ b.duration }}s</el-tag>
              <!-- RD-25：重生成 ≥3 次建议投递画布 -->
              <el-tag v-if="b.regen_count >= 3" size="small" type="warning" effect="dark">
                建议投递画布精修
              </el-tag>
              <div class="board-head-ops">
                <el-button
                  size="small"
                  text
                  :disabled="i === 0 || reorderBusy"
                  @click="moveBoard(i, -1)"
                >
                  ↑ 上移
                </el-button>
                <el-button
                  size="small"
                  text
                  :disabled="i === boards.length - 1 || reorderBusy"
                  @click="moveBoard(i, 1)"
                >
                  ↓ 下移
                </el-button>
              </div>
            </div>
          </template>
          <p class="board-visual">{{ b.visual_description || '（无画面描述）' }}</p>
          <div v-if="b.dialogue" class="board-dialogue">
            <div v-for="(line, li) in b.dialogue.split('\n').filter((l) => l.trim())" :key="li" class="dlg-line">
              {{ line }}
            </div>
          </div>
          <!-- 绑定资产 Tag（低置信标黄，RD-24） -->
          <div class="board-assets">
            <span class="assets-label">绑定资产：</span>
            <el-tag
              v-for="c in b.characters"
              :key="c.character_id"
              size="small"
              :type="(c.link_confidence ?? 1) < 0.7 ? 'warning' : 'success'"
              :title="c.link_confidence != null ? `链接置信度 ${(c.link_confidence * 100).toFixed(0)}%` : ''"
            >
              角色·{{ c.name }}{{ (c.link_confidence ?? 1) < 0.7 ? '（低置信）' : '' }}
            </el-tag>
            <el-tag
              v-if="b.scene_id"
              size="small"
              type="success"
              effect="light"
            >
              场景·{{ b.scene_name || b.scene_id }}
            </el-tag>
            <span v-if="!b.characters.length && !b.scene_id" class="assets-none">未绑定</span>
          </div>
          <div class="board-ops">
            <el-button size="small" type="primary" plain @click="openEdit(b)">编辑</el-button>
            <el-button size="small" @click="openSplit(b)">拆分</el-button>
            <el-button size="small" @click="mergeWithNext(b)">合并下一镜</el-button>
            <el-button
              size="small"
              :loading="regenBusyIds.includes(b.id)"
              @click="regenBoard(b)"
            >
              重生成（{{ b.regen_count }}）
            </el-button>
            <el-button size="small" type="warning" plain :loading="delivering" @click="deliver(b)">
              投递画布
            </el-button>
          </div>
        </el-card>
      </div>

      <!-- 底部操作条 -->
      <div class="footer-bar">
        <span class="footer-tip">提示：顺序调整后镜号自动重排；低置信绑定建议先到资产页确认</span>
        <el-button size="large" type="primary" class="confirm-btn" @click="gotoProduction">
          确认分镜，进入生成
        </el-button>
      </div>
    </template>

    <!-- 编辑弹窗 -->
    <el-dialog v-model="editVisible" title="编辑分镜" width="640px" append-to-body>
      <el-form label-width="90px">
        <el-form-item label="景别">
          <el-select v-model="editForm.shot_type" placeholder="选择景别" clearable style="width: 200px">
            <el-option v-for="s in shotTypeOptions" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
        <el-form-item label="运镜">
          <el-select v-model="editForm.camera_movement" placeholder="选择运镜" clearable style="width: 200px">
            <el-option v-for="c in cameraOptions" :key="c" :label="c" :value="c" />
          </el-select>
        </el-form-item>
        <el-form-item label="时长（秒）">
          <el-input-number v-model="editForm.duration" :min="1" :max="30" />
        </el-form-item>
        <el-form-item label="画面描述">
          <el-input v-model="editForm.visual_description" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="对白">
          <el-input
            v-model="editForm.dialogue"
            type="textarea"
            :rows="3"
            placeholder="每行一条，格式：角色名：台词"
          />
        </el-form-item>
        <el-form-item label="提示词">
          <el-input
            v-model="editForm.prompt_editable"
            type="textarea"
            :rows="2"
            placeholder="用户可编辑层提示词（不影响一致性约束）"
          />
        </el-form-item>
        <el-form-item label="强绑场景">
          <el-select v-model="editForm.scene_id" placeholder="选择场景资产" clearable style="width: 260px">
            <el-option v-for="s in sceneOptions" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="涉及角色">
          <el-select v-model="editForm.character_ids" multiple style="width: 100%" placeholder="选择角色资产">
            <el-option v-for="c in charOptions" :key="c.id" :label="c.name" :value="c.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editSaving" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 拆分弹窗 -->
    <el-dialog v-model="splitVisible" title="拆分分镜（按对白行）" width="480px" append-to-body>
      <div class="split-lines">
        <el-radio-group v-model="splitAtLine">
          <div v-for="(line, li) in splitLines" :key="li" class="split-line">
            <el-radio :value="li + 1">前 {{ li + 1 }} 行留本镜</el-radio>
            <span class="split-line-text">{{ line }}</span>
          </div>
        </el-radio-group>
      </div>
      <template #footer>
        <el-button @click="splitVisible = false">取消</el-button>
        <el-button type="primary" :loading="splitSaving" @click="confirmSplit">拆分</el-button>
      </template>
    </el-dialog>

    <!-- 节奏预警抽屉（RD-22） -->
    <el-drawer v-model="warnVisible" title="节奏与连续性预警" size="420px" append-to-body>
      <div v-loading="warnLoading">
        <div class="warn-summary">
          总时长 {{ warnSummary.total_duration }}s
          <template v-if="warnSummary.target_duration">
            / 目标 {{ warnSummary.target_duration }}s
          </template>
        </div>
        <el-empty v-if="!warnList.length" description="未检测到节奏或绑定问题" />
        <el-alert
          v-for="(w, i) in warnList"
          :key="i"
          :title="w.message"
          :type="w.level === 'error' ? 'error' : 'warning'"
          show-icon
          :closable="false"
          class="warn-item"
        />
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.sb-view {
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
.warn-badge {
  margin-top: 6px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  padding: var(--mf-sp-3) var(--mf-sp-4);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
}
.toolbar-cost {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.toolbar-btn {
  margin-left: auto;
}
.board-list {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-4);
}
.board-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  flex-wrap: wrap;
}
.board-no {
  font-weight: var(--mf-fw-bold);
  color: var(--mf-brand);
  font-size: var(--mf-fs-lg);
}
.board-head-ops {
  margin-left: auto;
}
.board-visual {
  margin: 0 0 var(--mf-sp-2);
  line-height: 1.8;
  color: var(--mf-text);
}
.board-dialogue {
  background: var(--mf-surface-2);
  border-radius: var(--mf-r-ctl);
  padding: var(--mf-sp-2) var(--mf-sp-3);
  margin-bottom: var(--mf-sp-2);
}
.dlg-line {
  font-size: var(--mf-fs-sm);
  color: var(--mf-text-2);
  line-height: 1.8;
}
.board-assets {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: var(--mf-sp-3);
}
.assets-label {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.assets-none {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.board-ops {
  display: flex;
  flex-wrap: wrap;
  gap: var(--mf-sp-2);
}
.footer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-4);
  position: sticky;
  bottom: 0;
  padding: var(--mf-sp-3);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
}
.footer-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.confirm-btn {
  background: var(--mf-brand);
  border-color: var(--mf-brand);
  color: var(--mf-bg);
}
.split-lines {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-2);
}
.split-line {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.split-line-text {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.warn-summary {
  color: var(--mf-text-2);
  margin-bottom: var(--mf-sp-3);
}
.warn-item {
  margin-bottom: var(--mf-sp-2);
}
</style>
