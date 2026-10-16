<!--
  剧本工作台 · 卡片③ 剧本正文外壳卡（文件级注释，redesign-script-workspace Task 4 · P3）。

  由 StepEpisodeBody 改造为卡片形态并入三栏工作区（ScriptView 卡片③渲染本组件）：
  - 卡片外壳：标题「④ 剧本正文」+ 副标题（三视图）+ 当前阶段高亮标签；未达 body
    阶段呈锁定态（点击展开解锁引导，不挂载内容区，保持原「未达阶段不挂载真实
    子组件」行为）；
  - 内容区为 StepEpisodeBody（内部实现：集卡横滑列表、SSE 流式生成与中止、
    文章体 / 场次结构 / 漫剧格式三视图、保存 base_version 乐观锁 + 保存后自动
    分镜拆解传下游、重生成本集、导出；原 AI 优化 / 体检面板已删除，能力后续
    迁入容器右侧 AI 助手面板实现；原 StepIdea「场景卡列表」迁入即场次结构
    视图的卡片化呈现，不另建重复展示区）；
  - 锁定/引导状态由容器（ScriptView）经 props 下发，点击引导回抛 toggle-guide
    事件；流式中止仍由内部 StepEpisodeBody 的 onDeactivated/onBeforeUnmount 承担。
-->
<script setup lang="ts">
import { ref } from 'vue'
import { Lock } from '@element-plus/icons-vue'
import StepEpisodeBody from './StepEpisodeBody.vue'
import type { ScriptPhase } from '@/api/scriptPipeline'

/** 组件入参 */
const props = defineProps<{
  /** 当前项目 ID（透传内容区的正文/优化/体检等接口路径参数） */
  projectId: number
  /** 当前剧本创作阶段（透传内容区：body=大纲已确认；非 body 时内容区给出引导提示） */
  phase?: ScriptPhase | null
  /** 是否为当前阶段卡（body → 高亮「当前阶段」标签） */
  active?: boolean
  /** 是否锁定（未达 body 阶段 → 展示引导占位，不挂载内容区） */
  locked?: boolean
  /** 引导文案展开态（容器 guideOpen 状态下发） */
  guideOpen?: boolean
}>()

/** 组件事件 */
const emit = defineEmits<{
  /** 选中集变化（透传内容区上报，Task 6：容器据此计算助手面板聚焦对象） */
  'focus-changed': [info: { episodeId: number | null; sequence: number | null }]
  /** 点击锁定占位体（容器切换引导展开态） */
  'toggle-guide': []
}>()

/** 内容区组件引用（转发容器下发的 reload 刷新调用） */
const innerRef = ref<InstanceType<typeof StepEpisodeBody> | null>(null)

/** 组件暴露（Task 6：容器在助手面板采纳后调 reload 刷新正文卡内容） */
defineExpose({
  /**
   * 重拉分集列表与当前集正文（转发内容区 reload；锁定态内容区未挂载时静默跳过）。
   * @returns 刷新完成信号
   */
  reload: (): Promise<void> | undefined => innerRef.value?.reload(),
})
</script>

<template>
  <el-card
    shadow="never"
    class="sb-card"
    :class="{ 'sb-card--active': props.active, 'sb-card--locked': props.locked }"
  >
    <template #header>
      <div class="sb-head">
        <span class="sb-title"><span class="sb-no">④</span> 剧本正文</span>
        <el-tag v-if="props.active" size="small" type="primary" effect="light">
          当前阶段
        </el-tag>
        <span class="sb-desc">文章体 / 场次结构 / 漫剧格式</span>
      </div>
    </template>

    <!-- 内容区：StepEpisodeBody（流式生成 + 三视图 + 保存后自动传下游） -->
    <StepEpisodeBody
      v-if="!props.locked"
      ref="innerRef"
      :project-id="props.projectId"
      :phase="props.phase"
      @focus-changed="(info) => emit('focus-changed', info)"
    />

    <!-- 锁定占位体：点击展开解锁引导（状态由容器管理） -->
    <div v-else class="locked-body" role="button" @click="emit('toggle-guide')">
      <el-icon class="lock-icon"><Lock /></el-icon>
      <span class="locked-tip">尚未到达此阶段，点击查看解锁说明</span>
      <div v-if="props.guideOpen" class="locked-guide">
        确认分集大纲后，此处将逐集生成与编辑剧本正文（文章体 / 场次结构 /
        漫剧格式三视图），并提供 AI 优化、体检与确认出口。
      </div>
    </div>
  </el-card>
</template>

<style scoped>
/* 卡片外壳：当前阶段高亮（2px 主色描边）；未达阶段半透明锁定 */
.sb-card {
  border-radius: var(--mf-r-ctl);
  transition: border-color var(--mf-dur) var(--mf-ease), opacity var(--mf-dur) var(--mf-ease);
  /* 声明为容器（inline-size 锁宽度方向，高度不受影响）：
     内部 StepEpisodeBody 的 @container 降级以本卡片实际宽度为基准，
     而非视口宽度——三栏工作台下视口宽不代表本卡片够宽 */
  container-type: inline-size;
  container-name: sbcard;
}
.sb-card--active {
  border: 2px solid var(--mf-brand);
}
.sb-card--locked {
  opacity: 0.55;
}
.sb-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.sb-title {
  font-weight: var(--mf-fw-semibold);
}
.sb-no {
  color: var(--mf-brand);
  margin-right: var(--mf-sp-1);
}
.sb-desc {
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
