<!--
  提示词库（文件级注释，P-02）。
  公共 / 我的两个 Tab；能力/分类/搜索筛选；卡片列表（内容 3 行截断点击展开）。
  我的 Tab：新建 / 编辑 / 删除 / JSON 导入；公共卡支持"应用"（复制到剪贴板，演示级联动）。
  说明：现有 prompts.list 封装不支持 scope/search 参数，本页经 client 直连补齐
  （scope=public|private、search 模糊），不改动已有 api 模块。
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import client from '@/api/client'
import * as promptsApi from '@/api/prompts'
import type { PageResult, Prompt, PromptPayload } from '@/types'

// ---------- 状态 ----------
/** 当前范围 Tab：public / private */
const scope = ref<'public' | 'private'>('public')
/** 能力筛选（空为全部） */
const capability = ref('')
/** 分类筛选（空为全部） */
const category = ref('')
/** 搜索关键字 */
const search = ref('')
/** 列表 */
const items = ref<Prompt[]>([])
/** 总数 */
const total = ref(0)
/** 页码 */
const page = ref(1)
/** 每页条数 */
const pageSize = ref(9)
/** 加载状态 */
const loading = ref(true)
/** 内容展开的提示词 ID 集合 */
const expanded = ref<Set<number>>(new Set())

/** 能力选项（与产品能力对齐） */
const capabilityOptions = ['角色定妆', '分镜画面', '运镜', '配音', '通用']

/**
 * 筛选后的分类选项：从当前列表与全量结果中合并去重（简单起见由用户输入或选择）。
 */
const categoryOptions = computed(() => {
  const set = new Set<string>()
  for (const p of items.value) if (p.category) set.add(p.category)
  return [...set]
})

/**
 * 标签数组解析（内部函数）：后端 tags 为 JSONB（数组或逗号字符串）。
 * @param tags 原始 tags
 * @returns 标签字符串数组
 */
function tagList(tags: unknown): string[] {
  if (Array.isArray(tags)) return tags.map(String)
  if (typeof tags === 'string' && tags) return tags.split(/[,，]/).map((t) => t.trim()).filter(Boolean)
  return []
}

// ---------- 编辑对话框 ----------
/** 对话框可见性 */
const dialogVisible = ref(false)
/** 编辑中的提示词 ID（新建为 null） */
const editingId = ref<number | null>(null)
/** 表单数据 */
const form = reactive<{ title: string; capability: string; category: string; content: string; tags: string }>({
  title: '',
  capability: '通用',
  category: '',
  content: '',
  tags: '',
})

// ---------- JSON 导入对话框 ----------
/** 导入对话框可见性 */
const importVisible = ref(false)
/** 导入 JSON 文本 */
const importText = ref('')

/**
 * 拉取提示词列表（按 scope/capability/category/search 分页查询）。
 */
async function fetchList(): Promise<void> {
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      scope: scope.value,
      page: page.value,
      page_size: pageSize.value,
    }
    if (capability.value) params.capability = capability.value
    if (category.value) params.category = category.value
    if (search.value.trim()) params.search = search.value.trim()
    const resp = await client.get<PageResult<Prompt>>('/prompts', { params })
    items.value = resp.data.items
    total.value = resp.data.total
  } catch {
    // 错误已由拦截器 ElMessage(detail) 提示
  } finally {
    loading.value = false
  }
}

/**
 * Tab / 筛选变化：重置页码重新查询。
 */
function onFilterChange(): void {
  page.value = 1
  expanded.value = new Set()
  fetchList()
}

/**
 * 切换内容展开态。
 * @param id 提示词 ID
 */
function toggleExpand(id: number): void {
  const next = new Set(expanded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expanded.value = next
}

/** 打开新建对话框 */
function openCreate(): void {
  editingId.value = null
  Object.assign(form, { title: '', capability: '通用', category: '', content: '', tags: '' })
  dialogVisible.value = true
}

/**
 * 打开编辑对话框（预填表单）。
 * @param p 提示词
 */
function openEdit(p: Prompt): void {
  editingId.value = p.id
  Object.assign(form, {
    title: p.title,
    capability: p.capability ?? '通用',
    category: p.category ?? '',
    content: p.content,
    tags: tagList(p.tags).join(','),
  })
  dialogVisible.value = true
}

/**
 * 提交新建/编辑表单。
 */
async function submitForm(): Promise<void> {
  if (!form.title.trim() || !form.content.trim()) {
    ElMessage.warning('标题与内容为必填项')
    return
  }
  const payload: PromptPayload = {
    title: form.title.trim(),
    content: form.content,
    capability: form.capability,
    category: form.category.trim() || undefined,
    tags: form.tags.split(/[,，]/).map((t) => t.trim()).filter(Boolean),
  }
  try {
    if (editingId.value == null) {
      await promptsApi.create(payload)
      ElMessage.success('新建成功')
    } else {
      await promptsApi.update(editingId.value, payload)
      ElMessage.success('已保存')
    }
    dialogVisible.value = false
    await fetchList()
  } catch {
    // 错误已由拦截器提示
  }
}

/**
 * 删除提示词（仅我的，二次确认）。
 * @param p 提示词
 */
async function removeItem(p: Prompt): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除提示词「${p.title}」吗？`, '删除提示词', { type: 'warning' })
  } catch {
    return
  }
  try {
    await promptsApi.remove(p.id)
    ElMessage.success('已删除')
    await fetchList()
  } catch {
    // 错误已由拦截器提示
  }
}

/**
 * 提交 JSON 导入（textarea 粘贴 JSON 数组，逐项校验后调 import 接口）。
 */
async function submitImport(): Promise<void> {
  let parsed: unknown
  try {
    parsed = JSON.parse(importText.value)
  } catch {
    ElMessage.error('JSON 格式错误，请检查后重试')
    return
  }
  if (!Array.isArray(parsed) || parsed.length === 0) {
    ElMessage.error('请粘贴形如 [{"title":..., "content":...}] 的数组')
    return
  }
  const list: PromptPayload[] = (parsed as Record<string, unknown>[]).map((raw) => ({
    title: String(raw.title ?? ''),
    content: String(raw.content ?? ''),
    capability: raw.capability != null ? String(raw.capability) : undefined,
    category: raw.category != null ? String(raw.category) : undefined,
    tags: raw.tags,
  })).filter((p) => p.title && p.content)
  if (list.length === 0) {
    ElMessage.error('导入项须至少包含 title 与 content')
    return
  }
  try {
    const resp = await promptsApi.importPrompts(list)
    ElMessage.success(`成功导入 ${resp.imported} 条提示词`)
    importVisible.value = false
    importText.value = ''
    scope.value = 'private'
    page.value = 1
    await fetchList()
  } catch {
    // 错误已由拦截器提示
  }
}

/**
 * 应用公共提示词（演示级联动）：复制到剪贴板，提示可在新建项目/创作页粘贴使用。
 * @param p 提示词
 */
async function applyPrompt(p: Prompt): Promise<void> {
  try {
    await navigator.clipboard.writeText(p.content)
    ElMessage.success('提示词已复制，已在新建项目/创作页可用，直接粘贴即可')
  } catch {
    ElMessage.warning('复制失败，请手动选择内容复制')
  }
}

// 首次进入拉取公共库
onMounted(fetchList)
</script>

<template>
  <div class="page-container">
    <h2 class="page-title">提示词库</h2>

    <el-tabs v-model="scope" class="scope-tabs" @tab-change="onFilterChange">
      <el-tab-pane label="公共提示词" name="public" />
      <el-tab-pane label="我的提示词" name="private" />
    </el-tabs>

    <!-- 筛选行 -->
    <div class="filter-bar">
      <el-select v-model="capability" style="width: 140px" placeholder="全部能力" clearable @change="onFilterChange">
        <el-option v-for="c in capabilityOptions" :key="c" :label="c" :value="c" />
      </el-select>
      <el-select v-model="category" style="width: 160px" placeholder="全部分类" clearable filterable @change="onFilterChange">
        <el-option v-for="c in categoryOptions" :key="c" :label="c" :value="c" />
      </el-select>
      <el-input v-model="search" style="width: 220px" placeholder="搜索标题 / 内容" clearable @keyup.enter="onFilterChange" />
      <el-button @click="onFilterChange">搜索</el-button>
      <span class="spacer" />
      <template v-if="scope === 'private'">
        <el-button @click="importVisible = true">导入 JSON</el-button>
        <el-button type="primary" @click="openCreate">新建提示词</el-button>
      </template>
    </div>

    <!-- 加载骨架 -->
    <div v-if="loading" class="grid">
      <div v-for="i in 6" :key="i" class="prompt-card">
        <div style="padding: 16px">
          <div class="mf-skeleton mf-skeleton--text" style="width: 50%; margin-bottom: 10px"></div>
          <div class="mf-skeleton mf-skeleton--text" style="width: 90%"></div>
          <div class="mf-skeleton mf-skeleton--text" style="width: 80%"></div>
        </div>
      </div>
    </div>

    <!-- 空态 -->
    <el-empty v-else-if="items.length === 0" description="还没有提示词，从公共库收藏灵感或新建一个" >
      <el-button v-if="scope === 'private'" type="primary" @click="openCreate">创建第一个提示词</el-button>
    </el-empty>

    <!-- 卡片网格 -->
    <template v-else>
      <div class="grid">
        <div v-for="p in items" :key="p.id" class="prompt-card">
          <div class="card-head">
            <span class="title" :title="p.title">{{ p.title }}</span>
            <el-tag v-if="p.capability" size="small" type="primary" effect="soft">{{ p.capability }}</el-tag>
          </div>
          <div
            class="content"
            :class="{ expanded: expanded.has(p.id) }"
            @click="toggleExpand(p.id)"
          >{{ p.content }}</div>
          <div class="tags" v-if="tagList(p.tags).length">
            <el-tag v-for="t in tagList(p.tags)" :key="t" size="small" type="info" effect="plain">{{ t }}</el-tag>
          </div>
          <div class="actions">
            <span class="date">{{ (p.created_at ?? '').slice(0, 10) }}</span>
            <span class="spacer" />
            <el-button v-if="scope === 'public'" link type="primary" @click="applyPrompt(p)">应用</el-button>
            <template v-else>
              <el-button link type="primary" @click="openEdit(p)">编辑</el-button>
              <el-button link type="danger" @click="removeItem(p)">删除</el-button>
            </template>
          </div>
        </div>
      </div>
      <div class="pager">
        <!-- 数据不足一页时隐藏分页器，避免对少量数据渲染无效翻页控件 -->
        <el-pagination
          v-if="total > pageSize"
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[9, 18, 36]"
          layout="total, sizes, prev, pager, next"
          @current-change="fetchList"
          @size-change="onFilterChange"
        />
      </div>
    </template>

    <!-- 新建/编辑对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editingId == null ? '新建提示词' : '编辑提示词'"
      width="560px"
    >
      <el-form label-width="80px" label-position="left">
        <el-form-item label="标题" required>
          <el-input v-model="form.title" maxlength="60" show-word-limit placeholder="一句话概括用途" />
        </el-form-item>
        <el-form-item label="能力">
          <el-select v-model="form.capability" style="width: 100%">
            <el-option v-for="c in capabilityOptions" :key="c" :label="c" :value="c" />
          </el-select>
        </el-form-item>
        <el-form-item label="分类">
          <el-input v-model="form.category" maxlength="30" placeholder="如：科幻 / 国风 / 日常" />
        </el-form-item>
        <el-form-item label="内容" required>
          <el-input v-model="form.content" type="textarea" :rows="6" maxlength="2000" show-word-limit />
        </el-form-item>
        <el-form-item label="标签">
          <el-input v-model="form.tags" placeholder="多个标签用逗号分隔，如：赛博朋克, 夜景" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitForm">保存</el-button>
      </template>
    </el-dialog>

    <!-- JSON 导入对话框 -->
    <el-dialog v-model="importVisible" title="从 JSON 导入提示词" width="560px">
      <p class="import-tip">
        粘贴 JSON 数组，每项包含 title 与 content（可选 capability / category / tags）。
      </p>
      <el-input v-model="importText" type="textarea" :rows="10" placeholder='[{"title":"示例","content":"..."}]' />
      <template #footer>
        <el-button @click="importVisible = false">取消</el-button>
        <el-button type="primary" @click="submitImport">导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.scope-tabs :deep(.el-tabs__header) {
  margin-bottom: var(--mf-sp-4);
}
.filter-bar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-5);
  flex-wrap: wrap;
}
.spacer {
  flex: 1;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: var(--mf-sp-4);
}
.prompt-card {
  display: flex;
  flex-direction: column;
  border-radius: var(--mf-r-card);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  padding: var(--mf-sp-4);
  transition: transform var(--mf-dur) var(--mf-ease), box-shadow var(--mf-dur) var(--mf-ease);
}
.prompt-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--mf-shadow-pop);
}
.card-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-2);
}
.title {
  font-weight: var(--mf-fw-semibold);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.content {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  cursor: pointer;
  min-height: 66px;
}
.content.expanded {
  display: block;
  -webkit-line-clamp: unset;
}
.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: var(--mf-sp-2);
}
.actions {
  display: flex;
  align-items: center;
  margin-top: var(--mf-sp-3);
  padding-top: var(--mf-sp-2);
  border-top: 1px dashed var(--mf-border);
}
.date {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--mf-sp-5);
}
.import-tip {
  margin: 0 0 var(--mf-sp-2);
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
</style>
