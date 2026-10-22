<!--
  创作工作区布局（文件级注释）。
  结构：顶部条（返回 + 项目名 + 剧集切换器 + 五阶段步骤条）+ 路由出口。
  五阶段：①剧本 ②资产 ③分镜 ④生成 ⑤发布，当前路由高亮、可点击互跳。
  进入时拉取项目详情写入 project store，供各阶段页共享。
  S-06：步骤条旁增加剧集切换器（选项来自项目详情 episodes）——选中剧集写入
  project store（currentEpisodeId），剧本/资产/分镜/生成/发布等依赖页 watch 后
  按 episode_id 重新加载；按集工作流必须选中具体剧集（「默认剧集」占位项已移除，
  项目加载后自动选中第 1 集；项目无剧集时切换器禁用，各页按无集上下文运行）。
-->
<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useProjectStore } from '@/stores/project'
import * as projectsApi from '@/api/projects'
import { ElMessage } from 'element-plus'

const route = useRoute()
const router = useRouter()
const projectStore = useProjectStore()

/** 当前项目 ID（取路由参数） */
const projectId = computed(() => Number(route.params.id))

/** 当前项目剧集列表（项目详情内嵌摘要，S-06） */
const episodes = computed(() => projectStore.currentProject?.episodes ?? [])

/**
 * 切换器选中值（展示用）：直接映射 store 的 currentEpisodeId。
 * null（未选中）时 el-select 显示 placeholder（仅发生在无剧集项目）。
 */
const selectedEpisode = computed({
  get: () => projectStore.currentEpisodeId ?? undefined,
  set: (val: number) => projectStore.setCurrentEpisode(val),
})

// 「默认剧集」占位项移除后：项目剧集加载完成且尚未选中时，自动选中第 1 集
// （保证按集工作流始终有明确的集上下文；无剧集项目保持 null 不强选）
watch(
  episodes,
  (list) => {
    if (projectStore.currentEpisodeId == null && list.length) {
      projectStore.setCurrentEpisode(list[0]!.id)
    }
  },
  { immediate: true },
)

/** 五阶段步骤定义（key 对应工作区子路由片段） */
const steps = [
  { key: 'script', label: '① 剧本', to: '/script' },
  { key: 'assets', label: '② 资产', to: '/assets' },
  { key: 'storyboard', label: '③ 分镜', to: '/storyboard' },
  { key: 'production', label: '④ 生成', to: '/production' },
  { key: 'publish', label: '⑤ 发布', to: '/publish' },
] as const

/** 当前激活阶段（取路径第二段：/projects/:id/<stage>[...]） */
const activeStep = computed(() => {
  const seg = route.path.split('/')[3] ?? ''
  // 实体抽取子页归属"剧本"阶段
  if (seg === 'script') return 'script'
  return seg
})

/**
 * 跳转阶段路由。
 * @param to 阶段子路径
 */
function goto(to: string): void {
  router.push(`/projects/${projectId.value}${to}`)
}

// 进入工作区：拉取项目详情写入共享 store
onMounted(async () => {
  try {
    projectStore.setProject(await projectsApi.get(projectId.value))
  } catch {
    ElMessage.error('项目加载失败')
    router.replace('/projects')
  }
})
</script>

<template>
  <div class="studio-layout">
    <header class="studio-layout__header">
      <button type="button" class="back-btn" title="返回项目列表" @click="router.push('/projects')">
        ←
      </button>
      <span class="project-name">{{ projectStore.currentProject?.title ?? '加载中…' }}</span>

      <!-- 剧集切换器（S-06）：仅列实际剧集，项目加载后自动选中第 1 集；
           无剧集项目禁用（各页按无集上下文运行，不传 episode_id） -->
      <el-select
        v-model="selectedEpisode"
        class="episode-select"
        size="small"
        :disabled="!episodes.length"
        :placeholder="episodes.length ? '选择剧集' : '暂无剧集'"
        aria-label="剧集切换"
      >
        <el-option
          v-for="ep in episodes"
          :key="ep.id"
          :value="ep.id"
          :label="`第${ep.sequence}集 · ${ep.title}`"
        />
      </el-select>

      <!-- 五阶段步骤条 -->
      <nav class="stage-bar" aria-label="创作阶段">
        <button
          v-for="s in steps"
          :key="s.key"
          type="button"
          class="stage-item"
          :class="{ 'stage-item--active': activeStep === s.key }"
          @click="goto(s.to)"
        >
          {{ s.label }}
        </button>
      </nav>
    </header>

    <main class="studio-layout__main">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.studio-layout {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--mf-bg);
  color: var(--mf-text);
}
.studio-layout__header {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-4);
  height: var(--mf-topbar-h);
  padding: 0 var(--mf-sp-5);
  /* 玻璃底与环境氛围层连续（与 DefaultLayout 顶栏一致） */
  background: var(--mf-topbar-bg, var(--mf-surface));
  backdrop-filter: blur(14px) saturate(140%);
  -webkit-backdrop-filter: blur(14px) saturate(140%);
  border-bottom: 1px solid var(--mf-border);
  flex-shrink: 0;
}
.back-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: none;
  border-radius: var(--mf-r-ctl);
  background: transparent;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-lg);
  cursor: pointer;
  transition: background var(--mf-dur) var(--mf-ease), color var(--mf-dur) var(--mf-ease);
}
.back-btn:hover {
  background: var(--mf-surface-2);
  color: var(--mf-text);
}
.project-name {
  font-size: var(--mf-fs-lg);
  font-weight: var(--mf-fw-semibold);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 280px;
}

/* 剧集切换器（S-06） */
.episode-select {
  width: 180px;
  flex-shrink: 0;
}

/* 步骤条 */
.stage-bar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-left: auto;
}
.stage-item {
  height: 32px;
  padding: 0 var(--mf-sp-4);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-pill);
  background: transparent;
  color: var(--mf-text-2);
  font-family: var(--mf-font);
  font-size: var(--mf-fs-sm);
  cursor: pointer;
  transition: all var(--mf-dur) var(--mf-ease);
}
.stage-item:hover {
  border-color: var(--mf-brand);
  color: var(--mf-brand);
}
.stage-item--active {
  background: var(--mf-brand-soft);
  border-color: transparent;
  color: var(--mf-brand);
  font-weight: var(--mf-fw-semibold);
}

.studio-layout__main {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
</style>
