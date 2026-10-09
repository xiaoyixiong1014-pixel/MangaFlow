/**
 * 当前项目 store（文件级注释）。
 *
 * StudioLayout 与各创作阶段页（剧本/资产/分镜/生成/发布）共享当前项目详情，
 * 进入项目工作区时由 StudioLayout 拉取并写入。
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

  /**
   * 设置当前项目（StudioLayout 拉取详情后调用）。
   * @param project 项目详情
   */
  function setProject(project: ProjectDetail): void {
    currentProject.value = project
  }

  /** 清空当前项目（离开 /projects/:id 工作区时调用） */
  function clearProject(): void {
    currentProject.value = null
  }

  return { currentProject, setProject, clearProject }
})
