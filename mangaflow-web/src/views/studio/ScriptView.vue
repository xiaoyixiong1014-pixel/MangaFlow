<!--
  剧本创作页 · 三栏工作台容器（文件级注释，redesign-script-workspace Task 1；
  Task 3 装配故事设定卡；后续调整：中部改为单区域三步切换）。

  对应原型 S-02，路由 /projects/:id/script。本页为「左中右三栏 + 三步切换」工作台：
  - 顶部通栏：页标题「剧本创作 · 项目名」+ 右侧工具区（版本历史抽屉入口 /
    剧本导出下拉 TXT·Fountain / 项目设置图标按钮）；
  - 下方三栏 CSS Grid：左 260px 参数栏（ParamSidebar，只读参数 + 跳转设置）、
    中间自适应单区域四步工作区、右 360px AI 助手面板（AssistantPanel，
    对话式共创：assist 预览/diff 采纳 + 三组快捷 Chips + 历史版本对比与回退；
    聚焦对象随中部当前显示步骤联动）；
  - 中部单区域四步切换：故事设定 / 角色设定 / 分集大纲 / 剧本正文四个步骤
    共用同一块内容区域，同一时刻仅显示一步——顶部步骤导航条（序号 + 标题 +
    完成/锁定状态，点击直达已达步骤）+ 右侧「上一步 / 下一步」按钮随意切换；
    未达的下一格允许进入并显示锁定引导（引导先完成上一步），更远步骤禁用；
  - 步骤内容：①故事设定（StorySettingCard：创作输入查看/编辑/保存 + 已生成
    设定三段）②角色设定（CharacterSettingCard：AI 依创意生成班底 + 确认入池）
    ③分集大纲（EpisodeOutlineCard：大纲导航 + 逐集编辑 + 单集 AI 重生成）
    ④剧本正文（ScriptBodyCard：文章体/场次结构/漫剧格式三视图 + AI 优化 +
    体检）；
  - 阶段驱动：script_phase 映射最远可达步骤（NULL/idea→①、characters→②、
    outline→③、body→④，与阶段值一一对应）。阶段初值取角色列表接口回执
    phase（pipeline 各接口响应均携带最新 phase，子组件 @phase / @phase-changed
    事件同步，仅在阶段跃迁时跟随跳转）；刷新后按 phase 恢复定位到当前步；
  - keep-alive：三步互斥渲染经同一 keep-alive 切换，编辑状态与已加载短语
    在步骤间往返时保留；子组件流式中止行为保留——StepEpisodeBody 的
    onDeactivated（步骤切走时触发）与 onBeforeUnmount 兜底中止仍在；
  - 小屏 <1200px：三栏折叠为单栏纵向（中部工作区在前，参数栏与助手面板依次
    排在其下）；
  - 版本历史抽屉（VersionHistoryDrawer）与导出下拉自 StepIdea 工具行上移至此；
    导出作用于全部集（顶部通栏无剧集上下文，不携带 episode_id）。
-->
<script setup lang="ts">
import { computed, onMounted, provide, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowDown, Setting } from '@element-plus/icons-vue'
import * as pipelineApi from '@/api/scriptPipeline'
import type {
  AssistantAppliedPayload,
  AssistantFocus,
  ScriptExportFormat,
  ScriptPhase,
} from '@/api/scriptPipeline'
import { useProjectStore } from '@/stores/project'
import StorySettingCard from '@/components/script/StorySettingCard.vue'
import CharacterSettingCard from '@/components/script/CharacterSettingCard.vue'
import EpisodeOutlineCard from '@/components/script/EpisodeOutlineCard.vue'
import ScriptBodyCard from '@/components/script/ScriptBodyCard.vue'
import ParamSidebar from '@/components/script/ParamSidebar.vue'
import AssistantPanel from '@/components/script/AssistantPanel.vue'
import VersionHistoryDrawer from '@/components/script/VersionHistoryDrawer.vue'
import { QUOTE_MAX, SCRIPT_QUOTE_KEY } from '@/components/script/quoteBridge'
import type { ScriptQuoteBridge, ScriptQuoteItem } from '@/components/script/quoteBridge'

const route = useRoute()
const projectStore = useProjectStore()

/** 当前项目 ID（取路由参数，透传给三栏子组件） */
const projectId = computed(() => Number(route.params.id))

/** 当前剧本创作阶段（NULL=快速模式；由 pipeline 接口回执同步） */
const phase = ref<ScriptPhase | null>(null)
/** 阶段初始化中（决定首屏定位，期间展示骨架） */
const booting = ref(true)

/** 三步定义 → 四步：故事设定 / 角色设定 / 分集大纲 / 剧本正文（角色设定独立成步） */
const STEPS = [
  { key: 'setting', label: '故事设定' },
  { key: 'characters', label: '角色设定' },
  { key: 'outline', label: '分集大纲' },
  { key: 'body', label: '剧本正文' },
] as const

/**
 * script_phase → 最远可达步骤映射：NULL/idea→①、characters→②、outline→③、
 * body→④（角色设定独立成步后与阶段值一一对应）。
 * @param p 剧本创作阶段（null 表示未进入流水线）
 * @returns 最远可达步骤号（1~4）
 */
function phaseToCard(p: ScriptPhase | null): number {
  if (p === 'characters') return 2
  if (p === 'outline') return 3
  if (p === 'body') return 4
  return 1
}

/** 已到达的最远步骤（导航可切换上限；同时是锁定判断基准） */
const maxCard = computed(() => phaseToCard(phase.value))

/** 当前显示的步骤（1~3；同一时刻仅此步骤渲染在内容区） */
const viewCard = ref(1)

/**
 * 子组件回执的最新阶段（大纲确认→body 等），同步最远可达步骤与解锁；
 * 仅当阶段**真正前进**（旧值→新值跃迁，如确认故事设定 characters→outline）
 * 时才跟随跳到新步骤卡；用户回看上一步时子组件 emit 的是相同阶段值，
 * 不发生跃迁、视图不被拉扯（修复：phase=body 时点故事设定被拽回正文的跳转 bug）。
 * @param p 最新剧本创作阶段
 * @returns 无
 */
function setPhase(p: ScriptPhase | null): void {
  const prevCard = phaseToCard(phase.value)
  phase.value = p
  const nextCard = phaseToCard(p)
  if (nextCard > prevCard) viewCard.value = nextCard
}

/**
 * 切换到指定步骤（导航条点击 / 上一步下一步 / 子组件「下一步」统一入口）。
 * 允许范围：已达步骤 1~maxCard 自由往返；maxCard+1 允许进入（显示锁定引导，
 * 与原「下一格可预览解锁条件」语义一致）；更远步骤夹回 maxCard+1。
 * 从分集大纲步（③）切走前先过未保存守卫：当前集有 AI 一键填充/手动编辑
 * 的未保存修改时弹三选提醒（保存并切换 / 不保存切换 / 取消留步），
 * 用户取消或保存失败则不离开。
 * @param card 目标步骤号（1~4）
 * @returns 无
 */
async function goToCard(card: number): Promise<void> {
  if (viewCard.value === 3 && card !== 3) {
    const ok = await outlineCardRef.value?.ensureSavedBeforeLeave()
    if (ok === false) return
  }
  viewCard.value = Math.min(Math.max(1, card), maxCard.value + 1)
}

/**
 * 步骤导航条单项状态：active=当前显示；done=已达且非当前（显示 ✓）；
 * lockable=最远可达的下一格（可进入但呈锁定样式，点击显示引导）；
 * locked=更远步骤（禁用，不可点击）。
 * @param step 步骤号（1~3）
 * @returns 状态标识
 */
function stepState(step: number): 'active' | 'done' | 'lockable' | 'locked' {
  if (step === viewCard.value) return 'active'
  if (step <= maxCard.value) return 'done'
  if (step === maxCard.value + 1) return 'lockable'
  return 'locked'
}

// ============================================================
// 助手面板聚焦联动（Task 6）：卡片选中集上报 → focus 计算 → 面板下发
// ============================================================

/** 选中集聚焦信息（卡片②③内容区经 focus-changed 事件上报） */
interface EpisodeFocusInfo {
  /** 剧集 ID（未选中为 null） */
  episodeId: number | null
  /** 剧集序号（用于「第 N 集」标签；未选中为 null） */
  sequence: number | null
}

/** 卡片②（分集大纲）当前选中集上报值 */
const outlineFocus = ref<EpisodeFocusInfo>({ episodeId: null, sequence: null })
/** 卡片③（剧本正文）当前选中集上报值 */
const bodyFocus = ref<EpisodeFocusInfo>({ episodeId: null, sequence: null })

/**
 * 汇总助手面板聚焦对象：步骤①②→故事设定（scope=setting 不传 target；
 * 角色设定步的对话改写沿用设定语境——assist 无角色作用域）；③→当前选中集
 * 分集大纲；④→当前选中集剧本正文；episode 两作用域未选中集时 label 置
 * 「未选择集」（target 为 null，面板发送前置校验拦截）。
 */
const assistFocus = computed<AssistantFocus>(() => {
  if (viewCard.value === 1) {
    return { scope: 'setting', label: '故事设定' }
  }
  if (viewCard.value === 2) {
    return { scope: 'setting', label: '角色设定 · 改写作用于故事设定' }
  }
  if (viewCard.value === 3) {
    const f = outlineFocus.value
    return f.episodeId != null
      ? { scope: 'episode_outline', target: f.episodeId, label: `第 ${f.sequence ?? '?'} 集 · 分集大纲` }
      : { scope: 'episode_outline', target: null, label: '未选择集' }
  }
  const b = bodyFocus.value
  return b.episodeId != null
    ? { scope: 'episode_body', target: b.episodeId, label: `第 ${b.sequence ?? '?'} 集 · 剧本正文` }
    : { scope: 'episode_body', target: null, label: '未选择集' }
})

// ============================================================
// 选中文本引用桥（quoteBridge）：QuotableText「添加对话中」入队 →
// AssistantPanel 引用区消费（随指令发 quote-rewrite 优化）
// ============================================================

/** 当前引用队列（QuotableText 入队、AssistantPanel 展示与消费） */
const scriptQuotes = ref<ScriptQuoteItem[]>([])
/** 引用自增 ID */
let quoteSeq = 0

/** 引用桥实体（provide 给 QuotableText 与 AssistantPanel） */
const quoteBridge: ScriptQuoteBridge = {
  quotes: scriptQuotes,
  /**
   * 添加一条引用（超出上限时提示并忽略；同来源+同文本去重不重复入队）。
   * @param source 来源展示标签
   * @param scope 引用作用域
   * @param text 选中的原文
   * @param targetEpisodeId 目标剧集 ID（可选）
   */
  add(source, scope, text, targetEpisodeId) {
    if (scriptQuotes.value.length >= QUOTE_MAX) {
      ElMessage.warning(`引用最多 ${QUOTE_MAX} 条，请先发送或移除已有引用`)
      return
    }
    if (scriptQuotes.value.some((q) => q.source === source && q.text === text)) {
      ElMessage.info('该内容已在引用中')
      return
    }
    scriptQuotes.value.push({
      id: ++quoteSeq, source, scope, text, targetEpisodeId: targetEpisodeId ?? null,
    })
    ElMessage.success('已添加到 AI 助手对话')
  },
  /**
   * 移除指定引用（AssistantPanel 引用区 × 按钮）。
   * @param id 引用 ID
   */
  remove(id) {
    scriptQuotes.value = scriptQuotes.value.filter((q) => q.id !== id)
  },
}
provide(SCRIPT_QUOTE_KEY, quoteBridge)

/**
 * 卡片②/③内容区选中集变化上报（focus-changed 透传）。
 * @param which 目标卡片状态（'outline' | 'body'）
 * @param info 选中集信息
 * @returns 无
 */
function onEpisodeFocus(which: 'outline' | 'body', info: EpisodeFocusInfo): void {
  if (which === 'outline') outlineFocus.value = info
  else bodyFocus.value = info
}

// ============================================================
// 助手面板 applied 刷新链路（Task 6）
// ============================================================

/** 各步骤卡组件引用（applied 后调用各自暴露的 reload 刷新） */
const settingCardRef = ref<InstanceType<typeof StorySettingCard> | null>(null)
const charactersCardRef = ref<InstanceType<typeof CharacterSettingCard> | null>(null)
const outlineCardRef = ref<InstanceType<typeof EpisodeOutlineCard> | null>(null)
const bodyCardRef = ref<InstanceType<typeof ScriptBodyCard> | null>(null)

/**
 * 助手面板采纳/回退完成回调：按 payload.scope 刷新对应卡片
 * （setting→卡片①②、episode_outline→卡片③、episode_body→卡片④；
 * scope 缺省（回退快照等全项目动作）刷新全部已解锁卡片）。
 * @param payload applied 事件载荷（scope/target/phase）
 * @returns 无
 */
function handleAssistApplied(payload: AssistantAppliedPayload): void {
  if (!payload.scope || payload.scope === 'setting') {
    void settingCardRef.value?.reload()
    void charactersCardRef.value?.reload()
  }
  if (!payload.scope || payload.scope === 'episode_outline') void outlineCardRef.value?.reload()
  if (!payload.scope || payload.scope === 'episode_body') void bodyCardRef.value?.reload()
}

// ============================================================
// 步骤①说明：快速模式链路已随产品简化移除（原 StepIdea 快速入口不再渲染，
// 文件保留以备回滚）；故事设定步骤收敛为「查看/编辑创作输入 + 保存」
// ============================================================

// ============================================================
// 顶部通栏：版本历史 / 导出 / 项目设置
// ============================================================

/** 版本历史抽屉显隐（V-01 快照回退，自 StepIdea 工具行上移） */
const versionVisible = ref(false)

/** 导出中的格式（按钮 loading），null 表示空闲 */
const exporting = ref<ScriptExportFormat | null>(null)

/**
 * 导出剧本（TXT / Fountain）。顶部通栏无剧集上下文，导出全部集
 * （不携带 episode_id，与原「缺省导出全部集」口径一致）。
 * @param format 导出格式
 * @returns 无
 * @throws 接口错误已由 client 拦截器统一弹窗提示
 */
async function doExport(format: ScriptExportFormat): Promise<void> {
  if (exporting.value) return
  exporting.value = format
  try {
    await pipelineApi.exportScript(projectId.value, format)
    ElMessage.success(`已导出 ${format === 'fountain' ? 'Fountain' : 'TXT'} 文件`)
  } finally {
    exporting.value = null
  }
}

/**
 * 导出下拉命令分发（el-dropdown command → 导出格式）。
 * @param command 下拉项命令值
 * @returns 无
 */
function handleExportCommand(command: string | number | object): void {
  if (command === 'txt' || command === 'fountain') void doExport(command)
}

/**
 * 点击项目设置图标按钮：当前路由表（src/router/index.ts，已查证）尚无独立
 * 项目设置页，暂以提示占位；后续任务交付设置页后替换为 router.push 跳转。
 * @returns 无
 */
function openProjectSettings(): void {
  ElMessage.info('项目设置页即将开放，当前可在项目列表管理项目')
}

onMounted(async () => {
  // 刷新后按 script_phase 恢复定位：阶段取自角色列表接口回执的 phase 字段，
  // 并把显示步骤定位到当前阶段对应卡（如 outline→步骤②）
  try {
    const out = await pipelineApi.listCharacters(projectId.value)
    phase.value = out.phase
    viewCard.value = phaseToCard(out.phase)
  } catch {
    // 阶段拉取失败（拦截器已提示后端 detail）：保持默认步骤①，不阻断页面
  } finally {
    booting.value = false
  }
})
</script>

<template>
  <div class="page-container script-view">
    <!-- ===== 顶部通栏：标题 + 版本历史 / 导出 / 项目设置 ===== -->
    <div class="top-bar">
      <h2 class="page-title">剧本创作 · {{ projectStore.currentProject?.title ?? '' }}</h2>
      <div class="top-ops">
        <!-- 版本历史入口（V-01：快照列表 + 回退，抽屉组件见下） -->
        <el-button size="small" @click="versionVisible = true">版本历史</el-button>
        <!-- 剧本导出：TXT / Fountain（导出全部集） -->
        <el-dropdown trigger="click" @command="handleExportCommand">
          <el-button size="small" :loading="!!exporting">
            导出剧本<el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="txt">导出 TXT</el-dropdown-item>
              <el-dropdown-item command="fountain">导出 Fountain</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <!-- 项目设置入口（路由表暂无设置页，见 openProjectSettings 注释） -->
        <el-button
          size="small"
          :icon="Setting"
          title="项目设置"
          aria-label="项目设置"
          @click="openProjectSettings"
        />
      </div>
    </div>

    <!-- 阶段初始化骨架（确定首屏定位前不渲染三栏内容） -->
    <el-skeleton v-if="booting" :rows="8" animated />

    <!-- ===== 三栏工作台：左参数栏 / 中三卡片 / 右助手面板 ===== -->
    <div v-else class="tri-grid">
      <!-- 左：参数栏（只读参数 + 预估消耗 + 阶段集数 + 修改参数） -->
      <ParamSidebar class="col-param" :project-id="projectId" />

      <!-- 中：单区域三步工作区（步骤导航条 + 互斥内容区，同一时刻仅显示一步） -->
      <section class="col-work">
        <!-- 步骤导航条：步骤胶囊（点击直达已达步骤；上一步/下一步按钮已移除） -->
        <div class="step-nav">
          <button
            v-for="(s, i) in STEPS"
            :key="s.key"
            type="button"
            class="step-pill"
            :class="`step-pill--${stepState(i + 1)}`"
            :disabled="stepState(i + 1) === 'locked'"
            @click="goToCard(i + 1)"
          >
            <span class="step-pill__no">{{ stepState(i + 1) === 'done' ? '✓' : ['①', '②', '③', '④'][i] }}</span>
            <span class="step-pill__label">{{ s.label }}</span>
          </button>
        </div>

        <!-- 互斥内容区：keep-alive 缓存各步骤实例，往返切换保留编辑状态与短语。
             注意：keep-alive 内必须是纯 v-if/v-else-if 分支链（不得夹注释等任何
             其他节点，否则编译报 "expects exactly one child component"）：
             步骤①=StorySettingCard（创作输入查看/编辑/保存 + 已生成设定态）；
             步骤②=CharacterSettingCard（角色设定独立步：AI 生成班底 + 确认入池）；
             步骤③=EpisodeOutlineCard（**不锁定**：创意/角色确认后即可生成大纲，
             后端 plan 支持 characters/NULL→outline 直进并有未保存输入守卫兜底）；
             步骤④=ScriptBodyCard（locked=maxCard<4：大纲确认前正文不可生成） -->
        <div class="step-stage">
          <keep-alive>
            <el-card
              v-if="viewCard === 1"
              key="card-setting"
              shadow="never"
              class="ws-card ws-card--active"
            >
              <template #header>
                <div class="ws-head">
                  <span class="ws-title"><span class="ws-no">①</span> 故事设定</span>
                  <el-tag size="small" type="primary" effect="light">当前步骤</el-tag>
                </div>
              </template>
              <StorySettingCard
                ref="settingCardRef"
                :project-id="projectId"
                @phase-changed="setPhase"
              />
            </el-card>
            <CharacterSettingCard
              v-else-if="viewCard === 2"
              key="card-characters"
              ref="charactersCardRef"
              :project-id="projectId"
              :active="true"
              @phase-changed="setPhase"
            />
            <EpisodeOutlineCard
              v-else-if="viewCard === 3"
              key="card-outline"
              ref="outlineCardRef"
              :project-id="projectId"
              :active="true"
              @phase="setPhase"
              @focus-changed="(info) => onEpisodeFocus('outline', info)"
            />
            <ScriptBodyCard
              v-else-if="viewCard === 4"
              key="card-body"
              ref="bodyCardRef"
              :project-id="projectId"
              :phase="phase"
              :active="true"
              :locked="maxCard < 4"
              :guide-open="maxCard < 4"
              @focus-changed="(info) => onEpisodeFocus('body', info)"
            />
          </keep-alive>
        </div>
      </section>

      <!-- 右：AI 助手常驻面板（Task 6 对话式共创：assist 预览/采纳 + Chips +
           历史版本对比回退；applied 回调刷新对应卡片） -->
      <AssistantPanel
        class="col-assist"
        :project-id="projectId"
        :focus="assistFocus"
        @applied="handleAssistApplied"
      />
    </div>

    <!-- 版本历史抽屉（自 StepIdea 工具行上移至顶部通栏） -->
    <VersionHistoryDrawer v-model="versionVisible" :project-id="projectId" />
  </div>
</template>

<style scoped>
.script-view {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-4);
}
/* 顶部通栏：标题 + 工具区 */
.top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-4);
}
.page-title {
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.top-ops {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  flex-shrink: 0;
}
/* 三栏栅格：左 260px 参数栏 / 中间自适应 / 右 360px 助手面板，栏间距 16px */
.tri-grid {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr) 360px;
  gap: var(--mf-sp-4);
  align-items: start;
}
/* 两侧栏悬停（sticky）：页面在 .studio-layout__main 内滚动时，参数栏与助手
   面板钉在可视区顶部不随内容滚走；max-height 限制为「视口 - 顶栏 - 上下
   边距」（顶栏实际高度约 56px，取 120px 安全余量），超高时栏内自滚动 */
.col-param,
.col-assist {
  position: sticky;
  top: var(--mf-sp-4);
  max-height: calc(100vh - 120px);
  overflow-y: auto;
}
/* 中部单区域三步工作区：导航条 + 互斥内容区，纵向排列 */
.col-work {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-4);
  min-width: 0;
}
/* 步骤导航条：步骤胶囊（点击直达，已移除上一步/下一步按钮） */
.step-nav {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--mf-sp-2);
}
/* 步骤胶囊：序号 + 标题；四态（active 主色描边 / done 完成打勾 /
   lockable 下一格虚线锁样式 / locked 禁用灰化不可点） */
.step-pill {
  display: inline-flex;
  align-items: center;
  gap: var(--mf-sp-1);
  padding: 6px 14px;
  border: 1px solid var(--mf-border, #363636);
  border-radius: 999px;
  background: transparent;
  color: inherit;
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  transition: border-color var(--mf-dur) var(--mf-ease), color var(--mf-dur) var(--mf-ease),
    background-color var(--mf-dur) var(--mf-ease);
}
.step-pill:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}
.step-pill--active {
  border-color: var(--mf-brand);
  color: var(--mf-brand);
  font-weight: var(--mf-fw-semibold);
}
.step-pill--done {
  color: var(--mf-brand);
}
.step-pill--done:hover {
  border-color: var(--mf-brand);
}
.step-pill--lockable {
  border-style: dashed;
  opacity: 0.75;
}
.step-pill--lockable:hover {
  border-color: var(--mf-brand);
  opacity: 1;
}
.step-pill__no {
  font-weight: var(--mf-fw-semibold);
}
/* 工作区步骤①外壳：互斥模式下始终处于显示态（主色描边）；步骤②③外壳样式
   由 EpisodeOutlineCard / ScriptBodyCard 组件自带（eo-card / sb-card） */
.ws-card {
  border-radius: var(--mf-r-ctl);
}
.ws-card--active {
  border: 2px solid var(--mf-brand);
}
.ws-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.ws-title {
  font-weight: var(--mf-fw-semibold);
}
.ws-no {
  color: var(--mf-brand);
  margin-right: var(--mf-sp-1);
}
/* 小屏 <1200px：三栏折叠为单栏纵向（中部在前，参数栏/助手面板依次在下） */
@media (max-width: 1199px) {
  .tri-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .col-work {
    order: 1;
  }
  .col-param {
    order: 2;
  }
  .col-assist {
    order: 3;
  }
}
</style>
