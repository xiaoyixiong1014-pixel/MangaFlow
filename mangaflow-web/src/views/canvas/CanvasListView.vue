<!--
  AI 画布列表（文件级注释，C-01）。
  模板区三卡（剧本→分镜图 / 角色三视图 / 风格试验）+ 我的画布网格
  （打开 / 复制 / 导出 JSON / 归档演示 / 删除）+ JSON 导入建画布 + 分页。
  所有操作走真实 canvas API：create / duplicate(以同名+副本重建) / exportCanvas / remove / importCanvas。
-->
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadRequestOptions, UploadFile } from 'element-plus'
import * as canvasApi from '@/api/canvas'
import type { CanvasGraph, CanvasListItem, PageResult } from '@/types'

const router = useRouter()

// ---------- 模板区 ----------
/** 模板卡定义（template 与后端 CANVAS_TEMPLATES 一致） */
const templates = [
  {
    value: 'script_to_storyboard',
    title: '剧本 → 分镜图',
    desc: '铺好剧本解析、分镜与生图节点，一键进入文生图流水线',
    icon: '📜',
  },
  {
    value: 'character_sheet',
    title: '角色三视图',
    desc: '一次出正面 / 侧面 / 半身并排对比，快速定角色造型',
    icon: '🧑‍🎨',
  },
  {
    value: 'style_experiment',
    title: '风格试验',
    desc: '同提示词多风格批量对比，找到最合适的画面风格',
    icon: '🎨',
  },
]

// ---------- 列表 ----------
/** 画布列表 */
const items = ref<CanvasListItem[]>([])
/** 总数 */
const total = ref(0)
/** 页码 */
const page = ref(1)
/** 每页条数 */
const pageSize = ref(12)
/** 加载状态 */
const loading = ref(true)
/** 创建/复制等操作锁 */
const busy = ref(false)

/**
 * 拉取画布列表。
 */
async function fetchList(): Promise<void> {
  loading.value = true
  try {
    const resp: PageResult<CanvasListItem> = await canvasApi.list({ page: page.value, page_size: pageSize.value })
    items.value = resp.items
    total.value = resp.total
  } catch {
    // 错误已由拦截器 ElMessage(detail) 提示
  } finally {
    loading.value = false
  }
}

/**
 * 使用模板创建画布并跳转编辑器。
 * @param tpl 模板标识
 */
async function createFromTemplate(tpl: string): Promise<void> {
  busy.value = true
  try {
    const names: Record<string, string> = {
      script_to_storyboard: '剧本到分镜画布',
      character_sheet: '角色三视图画布',
      style_experiment: '风格试验画布',
    }
    const graph = await canvasApi.create({ title: `${names[tpl] ?? '新画布'} ${fmtNow()}`, template: tpl })
    ElMessage.success('画布已创建')
    router.push(`/canvas/${graph.canvas.id}`)
  } catch {
    // 错误已由拦截器提示
  } finally {
    busy.value = false
  }
}

/**
 * 打开画布。
 * @param c 画布项
 */
function openCanvas(c: CanvasListItem): void {
  router.push(`/canvas/${c.id}`)
}

/**
 * 复制画布：导出完整图后以「同名+副本」无模板导入重建。
 * @param c 画布项
 */
async function duplicateCanvas(c: CanvasListItem): Promise<void> {
  busy.value = true
  try {
    const graph = await canvasApi.exportCanvas(c.id)
    // 复制 = 新建无模板画布 + 原图全量导入（去掉画布基础信息）
    const created = await canvasApi.importCanvas({
      title: `${c.title} 副本`,
      graph: {
        nodes: graph.nodes.map((n) => ({
          node_type: n.node_type,
          position_x: n.position_x,
          position_y: n.position_y,
          size_w: n.size_w ?? undefined,
          size_h: n.size_h ?? undefined,
          data: n.data ?? undefined,
          config: n.config ?? undefined,
          z_index: n.z_index,
          parent_group_id: n.parent_group_id ?? undefined,
        })),
        edges: graph.edges.map((e) => ({
          source_node_id: e.source_node_id,
          target_node_id: e.target_node_id,
          source_port: e.source_port ?? undefined,
          target_port: e.target_port ?? undefined,
        })),
      },
    })
    ElMessage.success('已复制为副本')
    router.push(`/canvas/${created.canvas.id}`)
  } catch {
    // 错误已由拦截器提示
  } finally {
    busy.value = false
  }
}

/**
 * 导出画布 JSON 并下载为文件。
 * @param c 画布项
 */
async function exportCanvas(c: CanvasListItem): Promise<void> {
  try {
    const graph = await canvasApi.exportCanvas(c.id)
    const blob = new Blob([JSON.stringify(graph, null, 2)], { type: 'application/json' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${c.title || 'canvas'}-${c.id}.json`
    a.click()
    URL.revokeObjectURL(a.href)
    ElMessage.success('已导出 JSON')
  } catch {
    // 错误已由拦截器提示
  }
}

/**
 * 归档（演示级提示：当前版本未提供归档存储）。
 */
function archiveCanvas(): void {
  ElMessage.info('归档功能为演示项：正式版将支持归档到独立分区')
}

/**
 * 删除画布（二次确认，软删除）。
 * @param c 画布项
 */
async function removeCanvas(c: CanvasListItem): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除画布「${c.title}」吗？其节点与连线将一并移除。`, '删除画布', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await canvasApi.remove(c.id)
    ElMessage.success('已删除')
    await fetchList()
  } catch {
    // 错误已由拦截器提示
  }
}

/**
 * 导入 JSON：读取 .json 文件 → importCanvas 建画布 → 跳编辑器。
 * @param options el-upload 上下文
 */
async function importJson(options: UploadRequestOptions): Promise<void> {
  const raw = options.file as File
  let text: string
  try {
    text = await raw.text()
  } catch {
    ElMessage.error('读取文件失败')
    return
  }
  let graph: CanvasGraph
  try {
    graph = JSON.parse(text) as CanvasGraph
    if (!graph?.nodes || !Array.isArray(graph.nodes)) throw new Error('bad format')
  } catch {
    ElMessage.error('JSON 格式错误：须为画布导出的完整图（含 nodes/edges）')
    return
  }
  busy.value = true
  try {
    const name = graph.canvas?.title ? `${graph.canvas.title} (导入)` : `导入画布 ${fmtNow()}`
    const created = await canvasApi.importCanvas({
      title: name,
      graph: {
        nodes: graph.nodes.map((n) => ({
          id: n.id,
          node_type: n.node_type,
          position_x: n.position_x,
          position_y: n.position_y,
          size_w: n.size_w ?? undefined,
          size_h: n.size_h ?? undefined,
          data: n.data ?? undefined,
          config: n.config ?? undefined,
          z_index: n.z_index,
        })),
        edges: graph.edges.map((e) => ({
          source_node_id: e.source_node_id,
          target_node_id: e.target_node_id,
          source_port: e.source_port ?? undefined,
          target_port: e.target_port ?? undefined,
        })),
      },
    })
    ElMessage.success('导入成功')
    router.push(`/canvas/${created.canvas.id}`)
  } catch {
    // 400 成环/类型非法等错误已由拦截器提示
  } finally {
    busy.value = false
  }
}

/**
 * el-upload on-change 触发的导入入口（auto-upload=false 时由 change 驱动）。
 * @param uploadFile 上传文件
 */
async function onImportFile(uploadFile: UploadFile): Promise<void> {
  if (!uploadFile.raw) return
  await importJson({ file: uploadFile.raw } as unknown as UploadRequestOptions)
}

/**
 * 当前时间短格式（内部函数）：用于自动命名。
 * @returns 形如 10-09 14:20 的字符串
 */
function fmtNow(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/**
 * 来源 Tag 文案。
 * @param t 画布类型
 */
function typeLabel(t: string): string {
  return t === 'studio_linked' ? 'Studio 投递' : '独立画布'
}

onMounted(fetchList)
</script>

<template>
  <div class="page-container">
    <div class="title-row">
      <h2 class="page-title">AI 画布</h2>
      <span class="spacer" />
      <el-upload :show-file-list="false" accept=".json" :auto-upload="false" :on-change="onImportFile">
        <el-button :disabled="busy">导入 JSON</el-button>
      </el-upload>
    </div>

    <!-- 模板区 -->
    <section class="tpl-section">
      <h3 class="section-title">从模板开始</h3>
      <div class="tpl-grid">
        <div v-for="t in templates" :key="t.value" class="tpl-card">
          <div class="tpl-icon" aria-hidden="true">{{ t.icon }}</div>
          <div class="tpl-name">{{ t.title }}</div>
          <p class="tpl-desc">{{ t.desc }}</p>
          <el-button type="primary" round :disabled="busy" @click="createFromTemplate(t.value)">使用模板</el-button>
        </div>
      </div>
    </section>

    <!-- 我的画布 -->
    <section class="mine-section">
      <h3 class="section-title">我的画布</h3>

      <!-- 骨架 -->
      <div v-if="loading" class="grid">
        <div v-for="i in 6" :key="i" class="card">
          <div class="mf-skeleton sk-thumb"></div>
          <div style="padding: 12px">
            <div class="mf-skeleton mf-skeleton--text" style="width: 60%"></div>
          </div>
        </div>
      </div>

      <!-- 空态引导 -->
      <el-empty v-else-if="items.length === 0" description="还没有画布，试试从上方模板开始" />

      <template v-else>
        <div class="grid">
          <div v-for="c in items" :key="c.id" class="card" @click="openCanvas(c)">
            <!-- 缩略 / 占位 -->
            <div class="thumb">
              <el-image v-if="c.thumbnail_url" :src="c.thumbnail_url" fit="cover" class="thumb-img">
                <template #error>
                  <div class="thumb-fallback">🧩</div>
                </template>
              </el-image>
              <div v-else class="thumb-fallback">🧩</div>
            </div>
            <!-- 信息 -->
            <div class="card-body">
              <div class="card-title" :title="c.title">{{ c.title }}</div>
              <div class="card-meta">
                <span>{{ c.node_count }} 节点 · {{ c.edge_count }} 连线</span>
                <el-tag size="small" :type="c.type === 'studio_linked' ? 'warning' : 'info'" effect="plain">
                  {{ typeLabel(c.type) }}
                </el-tag>
              </div>
              <div class="card-time">更新于 {{ (c.updated_at ?? '').slice(5, 16).replace('T', ' ') }}</div>
              <!-- hover 操作 -->
              <div class="card-actions" @click.stop>
                <el-button link type="primary" size="small" @click="openCanvas(c)">打开</el-button>
                <el-button link size="small" :disabled="busy" @click="duplicateCanvas(c)">复制</el-button>
                <el-button link size="small" @click="exportCanvas(c)">导出</el-button>
                <el-button link size="small" @click="archiveCanvas()">归档</el-button>
                <el-button link type="danger" size="small" @click="removeCanvas(c)">删除</el-button>
              </div>
            </div>
          </div>
        </div>
        <div class="pager">
          <el-pagination
            v-model:current-page="page"
            v-model:page-size="pageSize"
            :total="total"
            :page-sizes="[12, 24, 48]"
            layout="total, sizes, prev, pager, next"
            @current-change="fetchList"
            @size-change="() => { page = 1; fetchList() }"
          />
        </div>
      </template>
    </section>
  </div>
</template>

<style scoped>
.title-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.title-row .page-title {
  margin-bottom: var(--mf-sp-6);
}
.spacer {
  flex: 1;
}
.tpl-section {
  margin-bottom: var(--mf-sp-8);
}
.section-title {
  margin: 0 0 var(--mf-sp-3);
  font-size: var(--mf-fs-lg);
  font-weight: var(--mf-fw-semibold);
}
.tpl-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: var(--mf-sp-4);
}
.tpl-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--mf-sp-2);
  padding: var(--mf-sp-5);
  border-radius: var(--mf-r-card);
  background: linear-gradient(150deg, var(--mf-brand2-soft), var(--mf-surface) 65%);
  border: 1px solid var(--mf-border);
  transition: transform var(--mf-dur) var(--mf-ease), box-shadow var(--mf-dur) var(--mf-ease);
}
.tpl-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--mf-shadow-pop);
}
.tpl-icon {
  font-size: 30px;
  line-height: 1;
}
.tpl-name {
  font-weight: var(--mf-fw-semibold);
  font-size: var(--mf-fs-md);
}
.tpl-desc {
  margin: 0 0 var(--mf-sp-2);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
  min-height: 44px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: var(--mf-sp-4);
}
.card {
  border-radius: var(--mf-r-card);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  overflow: hidden;
  cursor: pointer;
  transition: transform var(--mf-dur) var(--mf-ease), box-shadow var(--mf-dur) var(--mf-ease);
}
.card:hover {
  transform: translateY(-3px);
  box-shadow: var(--mf-shadow-pop);
}
.sk-thumb,
.thumb {
  height: 120px;
  background: var(--mf-surface-2);
}
.thumb-img {
  width: 100%;
  height: 100%;
  display: block;
}
.thumb-fallback {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 36px;
  opacity: 0.6;
}
.card-body {
  padding: var(--mf-sp-3);
}
.card-title {
  font-weight: var(--mf-fw-semibold);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-2);
  margin-top: 4px;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.card-time {
  margin-top: 2px;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.card-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-top: var(--mf-sp-2);
  padding-top: var(--mf-sp-2);
  border-top: 1px dashed var(--mf-border);
  opacity: 0;
  transition: opacity var(--mf-dur) var(--mf-ease);
}
.card:hover .card-actions {
  opacity: 1;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--mf-sp-5);
}
</style>
