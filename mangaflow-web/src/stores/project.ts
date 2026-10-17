/**
 * 当前项目 store（文件级注释）。
 *
 * StudioLayout 与各创作阶段页（剧本/资产/分镜/生成/发布）共享当前项目详情，
 * 进入项目工作区时由 StudioLayout 拉取并写入。
 * S-06：额外维护「当前选中剧集」——切换器写入，各依赖页 watch 后刷新；
 * null 表示「默认剧集」（未绑定剧集，请求不携带 episode_id）。
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'
import type { ProjectDetail } from '@/types'

/**
 * 当前项目 store：创作工作区共享的项目上下文。
 * @returns state/actions 集合
 */
export const useProjectStore = defineStore('project', () => {
  /** 当前打开的项目详情（离开工作区置空） */
  const currentProject = ref<ProjectDetail | null>(null)
  /** 当前选中剧集 ID（null = 默认剧集/不传 episode_id；S-06） */
  const currentEpisodeId = ref<number | null>(null)

  /**
   * 设置当前项目（StudioLayout 拉取详情后调用；剧集选择重置为 null，
   * 由 StudioLayout 的 watch 自动选中第 1 集——「默认剧集」占位项已移除）。
   * @param project 项目详情
   */
  function setProject(project: ProjectDetail): void {
    currentProject.value = project
    currentEpisodeId.value = null
  }

  /**
   * 切换当前剧集（剧集切换器触发；依赖页 watch 该状态后重新加载）。
   * @param episodeId 剧集 ID（null 仅用于清空上下文，正常路径始终选具体剧集）
   */
  function setCurrentEpisode(episodeId: number | null): void {
    currentEpisodeId.value = episodeId
  }

  /** 清空当前项目（离开 /projects/:id 工作区时调用） */
  function clearProject(): void {
    currentProject.value = null
    currentEpisodeId.value = null
  }

  return { currentProject, currentEpisodeId, setProject, setCurrentEpisode, clearProject }
})
