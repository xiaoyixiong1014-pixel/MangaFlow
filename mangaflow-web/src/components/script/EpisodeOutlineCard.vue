<!--
  剧本工作台 · 卡片② 分集大纲外壳卡（文件级注释，redesign-script-workspace Task 4 · P3）。

  由 StepEpisodesPlan 改造为卡片形态并入三栏工作区（ScriptView 卡片②渲染本组件）：
  - 卡片外壳：标题「② 分集大纲」+ 副标题（集与大纲导航 · 逐集编辑 · 单集 AI 重生成）
    + 当前阶段高亮标签；未达 outline 阶段呈锁定态（点击展开解锁引导，不挂载内容区，
    保持原「未达阶段不挂载真实子组件」行为）；
  - 内容区为 StepEpisodesPlan（内部实现，保留全部能力：集列表与大纲导航、单集大纲
    编辑表单、增删集、上移下移、分流路径 B 章节分集、确认推进、body 锁定态，以及
    Task 4 新增的每集「AI 重生成大纲」）；
  - 锁定/引导状态由容器（ScriptView）经 props 下发，点击引导回抛 toggle-guide 事件，
    引导展开态与卡片定位逻辑仍归容器统一管理。
-->
<script setup lang="ts">
import { ref } from 'vue'
import { Lock } from '@element-plus/icons-vue'
import StepEpisodesPlan from './StepEpisodesPlan.vue'
import type { ScriptPhase } from '@/api/scriptPipeline'

/** 组件入参 */
const props = defineProps<{
  /** 当前项目 ID（透传内容区的大纲域接口路径参数） */
  projectId: number
  /** 是否为当前阶段卡（outline → 高亮「当前阶段」标签） */
  active?: boolean
  /** 是否锁定（未达 outline 阶段 → 展示引导占位，不挂载内容区） */
  locked?: boolean
  /** 引导文案展开态（容器 guideOpen 状态下发） */
  guideOpen?: boolean
}>()

/** 组件事件 */
const emit = defineEmits<{
  /** 阶段变化（透传内容区回执的最新 script_phase，供容器推进卡片高亮） */
  phase: [phase: ScriptPhase | null]
  /** 选中集变化（透传内容区上报，Task 6：容器据此计算助手面板聚焦对象） */
  'focus-changed': [info: { episodeId: number | null; sequence: number | null }]
  /** 点击锁定占位体（容器切换引导展开态） */
  'toggle-guide': []
}>()

/** 内容区组件引用（转发容器下发的 reload 刷新调用） */
const innerRef = ref<InstanceType<typeof StepEpisodesPlan> | null>(null)

/** 组件暴露（Task 6：容器在助手面板采纳后调 reload 刷新大纲卡内容；未保存守卫转发给容器跨步骤拦截） */
defineExpose({
  /**
   * 重拉分集大纲列表（转发内容区 reload；锁定态内容区未挂载时静默跳过）。
   * @returns 刷新完成信号
   */
  reload: (): Promise<void> | undefined => innerRef.value?.reload(),
  /**
   * 未保存修改守卫（转发内容区；锁定态内容区未挂载时视为无修改放行）：
   * 当前集有 AI 填充/手动编辑的未保存修改时弹三选提醒（保存并切换 /
   * 不保存切换 / 取消）。
   * @returns true=允许离开；false=留在当前集
   */
  ensureSavedBeforeLeave:
    (): Promise<boolean> | undefined => innerRef.value?.ensureSavedBeforeLeave(),
})
</script>

<template>
  <el-card
    shadow="never"
    class="eo-card"
    :class="{ 'eo-card--active': props.active, 'eo-card--locked': props.locked }"
  >
    <template #header>
      <div class="eo-head">
        <span class="eo-title"><span class="eo-no">③</span> 分集大纲</span>
        <el-tag v-if="props.active" size="small" type="primary" effect="light">
          当前阶段
        </el-tag>
        <span class="eo-desc">集卡选择 · 本集大纲编辑 · 单集 AI 重生成</span>
      </div>
    </template>

    <!-- 内容区：StepEpisodesPlan（大纲导航 + 逐集编辑 + 单集重生成 + 确认推进） -->
    <StepEpisodesPlan
      v-if="!props.locked"
      ref="innerRef"
      :project-id="props.projectId"
      @phase="(p) => emit('phase', p)"
      @focus-changed="(info) => emit('focus-changed', info)"
    />

    <!-- 锁定占位体：点击展开解锁引导（状态由容器管理） -->
    <div v-else class="locked-body" role="button" @click="emit('toggle-guide')">
      <el-icon class="lock-icon"><Lock /></el-icon>
      <span class="locked-tip">尚未到达此阶段，点击查看解锁说明</span>
      <div v-if="props.guideOpen" class="locked-guide">
        完成卡片①的故事设定确认后，此处将生成分集大纲（标题 / 核心剧情点 /
        出场人物 / 情绪走向 / 单集时长），并支持逐集调整与整体确认。
      </div>
    </div>
  </el-card>
</template>

<style scoped>
/* 卡片外壳：当前阶段高亮（2px 主色描边）；未达阶段半透明锁定 */
.eo-card {
  border-radius: var(--mf-r-ctl);
  transition: border-color var(--mf-dur) var(--mf-ease), opacity var(--mf-dur) var(--mf-ease);
}
.eo-card--active {
  border: 2px solid var(--mf-brand);
}
.eo-card--locked {
  opacity: 0.55;
}
.eo-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.eo-title {
  font-weight: var(--mf-fw-semibold);
}
.eo-no {
  color: var(--mf-brand);
  margin-right: var(--mf-sp-1);
}
.eo-desc {
  margin-left: auto;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
/* 锁定卡片占位体：点击展开引导文案 */
.locked-body {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--mf-sp-2);
  padding: var(--mf-sp-6) var(--mf-sp-4);
  cursor: pointer;
}
.lock-icon {
  font-size: 22px;
  color: var(--mf-text-3);
}
.locked-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.locked-guide {
  margin-top: var(--mf-sp-2);
  padding: var(--mf-sp-3);
  max-width: 420px;
  background: var(--mf-surface-2);
  border-radius: var(--mf-r-ctl);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
}
</style>
