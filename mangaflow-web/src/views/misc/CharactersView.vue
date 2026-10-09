<!--
  角色与场景总览（文件级注释）。
  跨项目角色 + 场景联合总览（数据源 studio.overviewCharacters）：
  类型 / 项目 / 关键字三层筛选 + 卡片网格，点击卡片跳转对应项目的资产管理页。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import * as studioApi from '@/api/studio'
import * as projectsApi from '@/api/projects'
import type { CrossProjectAsset, Project } from '@/types'

const router = useRouter()

/** 总览原始数据 */
const rows = ref<CrossProjectAsset[]>([])
/** 项目列表（筛选下拉） */
const projects = ref<Project[]>([])
/** 首次加载状态 */
const loading = ref(true)

/** 类型筛选：all / character / scene */
const assetType = ref('all')
/** 项目筛选（空为全部） */
const projectId = ref<number | ''>('')
/** 关键字搜索（名称/描述） */
const keyword = ref('')

/**
 * 过滤后的总览行（类型 + 项目 + 关键字，全部为本地筛选）。
 */
const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return rows.value.filter((r) => {
    if (assetType.value !== 'all' && r.asset_type !== assetType.value) return false
    if (projectId.value !== '' && r.project_id !== projectId.value) return false
    if (kw) {
      const hay = `${r.name} ${r.description ?? ''}`.toLowerCase()
      if (!hay.includes(kw)) return false
    }
    return true
  })
})

/**
 * 类型中文文案（内部函数）。
 * @param t 资产类型 character / scene
 * @returns 中文标签
 */
function typeLabel(t: string): string {
  return t === 'character' ? '角色' : '场景'
}

/**
 * 点击卡片：跳转该项目资产管理页。
 * @param row 总览行
 */
function openProject(row: CrossProjectAsset): void {
  router.push(`/projects/${row.project_id}/assets`)
}

// 首次进入：并行拉取总览与项目列表
onMounted(async () => {
  try {
    const [chars, projs] = await Promise.all([
      studioApi.overviewCharacters(),
      projectsApi.list({ page: 1, page_size: 100 }),
    ])
    rows.value = chars
    projects.value = projs.items
  } catch {
    // 错误已由拦截器 ElMessage(detail) 提示
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="page-container">
    <h2 class="page-title">角色与场景</h2>

    <!-- 筛选区 -->
    <div class="filter-bar">
      <el-select v-model="assetType" class="filter-item" style="width: 130px">
        <el-option label="全部类型" value="all" />
        <el-option label="角色" value="character" />
        <el-option label="场景" value="scene" />
      </el-select>
      <el-select
        v-model="projectId"
        class="filter-item"
        style="width: 200px"
        placeholder="全部项目"
        clearable
      >
        <el-option
          v-for="p in projects"
          :key="p.id"
          :label="p.title"
          :value="p.id"
        />
      </el-select>
      <el-input
        v-model="keyword"
        class="filter-item"
        style="width: 240px"
        placeholder="搜索名称 / 描述"
        clearable
      />
      <span class="count-tip">共 {{ filtered.length }} 项</span>
    </div>

    <!-- 加载骨架 -->
    <div v-if="loading" class="grid">
      <div v-for="i in 8" :key="i" class="asset-card">
        <div class="mf-skeleton sk-cover"></div>
        <div style="padding: 12px">
          <div class="mf-skeleton mf-skeleton--text" style="width: 60%"></div>
        </div>
      </div>
    </div>

    <!-- 空态引导 -->
    <el-empty
      v-else-if="filtered.length === 0"
      description="还没有角色或场景资产，去项目创作工作区生成吧"
    >
      <el-button type="primary" @click="router.push('/projects')">前往项目</el-button>
    </el-empty>

    <!-- 卡片网格 -->
    <div v-else class="grid">
      <div
        v-for="row in filtered"
        :key="`${row.asset_type}-${row.id}`"
        class="asset-card"
        :title="`前往项目「${row.project_title}」的资产页`"
        @click="openProject(row)"
      >
        <div class="cover">
          <el-image
            v-if="row.asset_url"
            :src="row.asset_url"
            fit="cover"
            class="cover-img"
            :preview-src-list="[row.asset_url]"
            preview-teleported
          >
            <template #error>
              <div class="cover-placeholder">🖼️ 加载失败</div>
            </template>
          </el-image>
          <div v-else class="cover-placeholder">{{ typeLabel(row.asset_type) === '角色' ? '🧑‍🎨' : '🏞️' }}</div>
          <el-tag class="type-tag" :type="row.asset_type === 'character' ? 'primary' : 'success'" size="small">
            {{ typeLabel(row.asset_type) }}
          </el-tag>
        </div>
        <div class="card-body">
          <div class="card-name" :title="row.name">{{ row.name }}</div>
          <div class="card-project" :title="row.project_title">所属项目：{{ row.project_title }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.filter-bar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  margin-bottom: var(--mf-sp-5);
  flex-wrap: wrap;
}
.count-tip {
  margin-left: auto;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: var(--mf-sp-4);
}
.asset-card {
  border-radius: var(--mf-r-card);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  overflow: hidden;
  cursor: pointer;
  transition: transform var(--mf-dur) var(--mf-ease), box-shadow var(--mf-dur) var(--mf-ease);
}
.asset-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--mf-shadow-pop);
}
.cover {
  position: relative;
  height: 150px;
  background: var(--mf-surface-2);
}
.cover-img {
  width: 100%;
  height: 100%;
  display: block;
}
.cover-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 34px;
  color: var(--mf-text-3);
}
.type-tag {
  position: absolute;
  top: 8px;
  left: 8px;
}
.card-body {
  padding: var(--mf-sp-3) var(--mf-sp-3) var(--mf-sp-4);
}
.card-name {
  font-weight: var(--mf-fw-semibold);
  font-size: var(--mf-fs-md);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-project {
  margin-top: 2px;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
