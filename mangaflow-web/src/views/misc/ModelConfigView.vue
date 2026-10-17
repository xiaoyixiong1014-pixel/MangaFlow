<!--
  模型配置中心（文件级注释，P-04）。
  四能力 Tab（文本/图像/视频/语音）浏览模型目录（名称/默认/场景/计价/文档/健康）；
  全局默认与项目覆盖的说明条；自有 Key 管理区（渠道列表 / 添加 / 编辑换 Key /
  模型 ID 登记 / 启停 / 测试连通 / 删除）；自有渠道模型优先用于生成并免扣生成积分。
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as catalogApi from '@/api/catalog'
import type { Catalog, Channel, LogicalModel } from '@/types'

// ---------- 目录 ----------
/** 目录数据 */
const catalog = ref<Catalog | null>(null)
/** 目录加载状态 */
const loading = ref(true)

/** 能力 Tab（key 与后端 capability 一致） */
const capabilityTabs = [
  { value: 'llm', label: '文本' },
  { value: 'image', label: '图像' },
  { value: 'video', label: '视频' },
  { value: 'tts', label: '语音' },
] as const

/** 当前能力 Tab */
const capability = ref<'llm' | 'image' | 'video' | 'tts'>('llm')

/**
 * 当前能力下的模型列表。
 */
const models = computed<LogicalModel[]>(() => {
  const group = catalog.value?.capabilities.find((g) => g.capability === capability.value)
  return group?.models ?? []
})

/** 健康状态中文与颜色映射 */
const healthLabels: Record<string, { label: string; type: 'success' | 'warning' | 'danger' | 'info' }> = {
  healthy: { label: '健康', type: 'success' },
  degraded: { label: '降级', type: 'warning' },
  down: { label: '不可用', type: 'danger' },
  unknown: { label: '未知', type: 'info' },
}

// ---------- 自有渠道 ----------
/** 我的渠道列表 */
const channels = ref<Channel[]>([])
/** 渠道加载状态 */
const channelsLoading = ref(false)
/** 渠道对话框可见性 */
const channelDialogVisible = ref(false)
/** 对话框模式：create 新建 / edit 编辑 */
const dialogMode = ref<'create' | 'edit'>('create')
/** 正在编辑的渠道 ID（编辑模式） */
const editingId = ref<number | null>(null)
/** 提交中 */
const submitting = ref(false)
/** 测试中的渠道 ID 集合 */
const testingIds = ref<Set<number>>(new Set())
/** 渠道表单（api_key 编辑模式留空=保持原 Key） */
const channelForm = reactive({ name: '', base_url: '', api_key: '' })
/** 模型 ID 列表（登记到渠道 models.chat，回车/选择即添加） */
const channelModels = ref<string[]>([])
/** 当前选择的 API 格式（与后端 protocol 字段一致） */
const channelProtocol = ref<'openai' | 'openai_responses' | 'anthropic' | 'dashscope'>('openai')
/** 模型用途（登记能力键）：chat 文本对话 / image 图像生成 */
const channelCapability = ref<'chat' | 'image'>('chat')
/** 渠道真实模型列表候选（点击「获取模型列表」后填充） */
const modelOptions = ref<string[]>([])
/** 拉取模型列表中 */
const probingModels = ref(false)

/** API 格式选项（与后端 SUPPORTED_CHANNEL_PROTOCOLS 一致） */
const protocolOptions = [
  { value: 'openai', label: 'OpenAI Chat Completions 格式' },
  { value: 'openai_responses', label: 'OpenAI Responses API 格式' },
  { value: 'anthropic', label: 'Anthropic Messages 格式' },
  { value: 'dashscope', label: 'DashScope 格式（阿里云百炼原生，图像生成）' },
] as const

/**
 * 按所选协议返回请求地址的补充说明文案（内部函数）。
 * @returns 地址填写提示
 */
function baseUrlTip(): string {
  if (channelProtocol.value === 'dashscope') {
    return '请填写 DashScope 根域名（根域名或带路径均可，如 https://dashscope.aliyuncs.com 或专属实例根域名）；文生图任务端点将自动补充。'
  }
  if (channelProtocol.value === 'anthropic') {
    return '请填写 Anthropic 兼容的服务端点地址（根地址或 /v1 结尾均可）；/v1/messages 将自动补充到地址末尾。'
  }
  if (channelProtocol.value === 'openai_responses') {
    return '请填写兼容 OpenAI Responses API 的服务端点地址，不要以斜杠结尾；/responses 将自动补充到地址末尾。'
  }
  return '请填写兼容 OpenAI API 的服务端点地址，不要以斜杠结尾；/chat/completions 将自动补充到地址末尾。'
}

/**
 * 切换 API 格式（内部函数）：DashScope 原生仅支持图像生成用途，自动锁定。
 */
function onProtocolChange(): void {
  if (channelProtocol.value === 'dashscope') {
    channelCapability.value = 'image'
  }
}

/**
 * 从渠道 models 目录提取模型 ID 列表（内部函数）：chat 优先，其次 image。
 * @param ch 渠道对象
 * @returns 模型 ID 字符串数组（结构异常时为空数组）
 */
function channelModelList(ch: Channel): string[] {
  const models = ch.models ?? {}
  const list = models['chat'] ?? models['image']
  return Array.isArray(list) ? list.map(String) : []
}

/**
 * 打开新建渠道对话框（重置表单）。
 */
function openCreate(): void {
  dialogMode.value = 'create'
  editingId.value = null
  channelForm.name = ''
  channelForm.base_url = ''
  channelForm.api_key = ''
  channelModels.value = []
  channelProtocol.value = 'openai'
  channelCapability.value = 'chat'
  modelOptions.value = []
  channelDialogVisible.value = true
}

/**
 * 打开编辑渠道对话框（回填现有配置；按 models 能力键回显模型用途，
 * API Key 留空表示保持原 Key）。
 * @param ch 渠道对象
 */
function openEdit(ch: Channel): void {
  dialogMode.value = 'edit'
  editingId.value = ch.id
  channelForm.name = ch.name ?? ''
  channelForm.base_url = ch.base_url
  channelForm.api_key = ''
  channelModels.value = channelModelList(ch)
  channelProtocol.value = (ch.protocol as typeof channelProtocol.value) ?? 'openai'
  // 模型用途回显：models 仅含 image 键时视为图像渠道
  const models = ch.models ?? {}
  channelCapability.value = !models['chat'] && models['image'] ? 'image' : 'chat'
  modelOptions.value = []
  channelDialogVisible.value = true
}

/**
 * 从渠道服务端拉取真实模型列表填充下拉候选（内部函数）。
 * 需已填写接口地址与 API Key（编辑模式 Key 留空时无法拉取）。
 */
async function fetchModelOptions(): Promise<void> {
  if (!channelForm.base_url.trim() || !/^https?:\/\//.test(channelForm.base_url.trim())) {
    ElMessage.warning('请先填写合法的接口地址')
    return
  }
  if (!channelForm.api_key.trim()) {
    ElMessage.warning(dialogMode.value === 'create'
      ? '请先填写 API 密钥再获取模型列表'
      : '编辑模式下请重新输入 API 密钥后再获取模型列表')
    return
  }
  probingModels.value = true
  try {
    const resp = await catalogApi.probeModels({
      protocol: channelProtocol.value,
      base_url: channelForm.base_url.trim(),
      api_key: channelForm.api_key.trim(),
    })
    if (resp.ok && resp.models.length) {
      modelOptions.value = resp.models
      ElMessage.success(`已获取 ${resp.models.length} 个模型，可直接下拉勾选`)
    } else {
      ElMessage.warning(resp.error || '渠道未返回模型列表，可手动输入模型 ID 后回车添加')
    }
  } catch {
    // 错误已由拦截器提示
  } finally {
    probingModels.value = false
  }
}

/**
 * 拉取我的渠道列表。
 */
async function fetchChannels(): Promise<void> {
  channelsLoading.value = true
  try {
    channels.value = await catalogApi.myChannels()
  } catch {
    // 错误已由拦截器提示
  } finally {
    channelsLoading.value = false
  }
}

/**
 * 提交渠道表单（新建 / 编辑统一入口；models 目录键按所选模型用途登记：
 * chat 文本对话 / image 图像生成，protocol 随表单所选 API 格式提交）。
 */
async function submitChannel(): Promise<void> {
  if (!channelForm.base_url.trim() || !channelModels.value.length) {
    ElMessage.warning('接口地址与至少一个模型 ID 为必填')
    return
  }
  if (!/^https?:\/\//.test(channelForm.base_url.trim())) {
    ElMessage.warning('接口地址需以 http:// 或 https:// 开头')
    return
  }
  const models = { [channelCapability.value]: channelModels.value }
  submitting.value = true
  try {
    if (dialogMode.value === 'create') {
      if (!channelForm.api_key.trim()) {
        ElMessage.warning('API Key 为必填')
        return
      }
      await catalogApi.addChannel({
        name: channelForm.name.trim() || undefined,
        protocol: channelProtocol.value,
        base_url: channelForm.base_url.trim(),
        api_key: channelForm.api_key.trim(),
        models,
      })
      ElMessage.success('渠道已添加（Key 已加密保存）')
    } else if (editingId.value != null) {
      await catalogApi.updateChannel(editingId.value, {
        name: channelForm.name.trim(),
        protocol: channelProtocol.value,
        base_url: channelForm.base_url.trim(),
        models,
        // 留空 = 保持原 Key；填写则更换（服务端加密覆盖）
        ...(channelForm.api_key.trim() ? { api_key: channelForm.api_key.trim() } : {}),
      })
      ElMessage.success('渠道已更新（健康状态已重置，建议重新测试连通）')
    }
    channelDialogVisible.value = false
    await fetchChannels()
  } catch {
    // 400 base_url 非法等错误已由拦截器提示
  } finally {
    submitting.value = false
  }
}

/**
 * 切换渠道启用状态（服务端将健康状态重置为 unknown）。
 * @param ch 渠道对象
 */
async function toggleEnabled(ch: Channel): Promise<void> {
  const next = !ch.enabled
  try {
    await catalogApi.updateChannel(ch.id, { enabled: next })
    ElMessage.success(next ? '渠道已启用' : '渠道已停用')
    await fetchChannels()
  } catch {
    // 错误已由拦截器提示
  }
}

/**
 * 测试渠道连通性（成功显示 ok 与健康状态）。
 * @param ch 渠道
 */
async function testChannel(ch: Channel): Promise<void> {
  testingIds.value = new Set(testingIds.value).add(ch.id)
  try {
    const resp = await catalogApi.testChannel(ch.id)
    if (resp.ok) ElMessage.success(`连通正常（健康状态：${healthLabels[resp.health_status]?.label ?? resp.health_status}）`)
    else ElMessage.error(resp.error || '连通失败')
    await fetchChannels()
  } catch {
    // 错误已由拦截器提示
  } finally {
    const next = new Set(testingIds.value)
    next.delete(ch.id)
    testingIds.value = next
  }
}

/**
 * 删除渠道（二次确认）。
 * @param ch 渠道
 */
async function removeChannel(ch: Channel): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除渠道 ${ch.base_url} 吗？`, '删除渠道', { type: 'warning' })
  } catch {
    return
  }
  try {
    await catalogApi.deleteChannel(ch.id)
    ElMessage.success('已删除')
    await fetchChannels()
  } catch {
    // 错误已由拦截器提示
  }
}

// 首次进入：并行拉取目录与渠道
onMounted(async () => {
  try {
    const [cat] = await Promise.all([catalogApi.modelsCatalog(), fetchChannels()])
    catalog.value = cat
  } catch {
    // 错误已由拦截器提示
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="page-container">
    <div class="page-head">
      <h2 class="page-title">模型配置</h2>
      <!-- 效果对比入口（PL-11，Task 10.3） -->
      <el-button type="primary" plain @click="$router.push('/models/compare')">效果对比 →</el-button>
    </div>

    <!-- 能力 Tab -->
    <el-tabs v-model="capability" class="cap-tabs">
      <el-tab-pane v-for="t in capabilityTabs" :key="t.value" :label="t.label" :name="t.value" />
    </el-tabs>

    <!-- 配置层级说明条 -->
    <el-alert type="info" :closable="false" class="layer-tip" show-icon>
      <template #title>
        配置层级：任务级 &gt; 本项目覆盖 &gt; 全局默认。项目内可在创作工作区为当前项目单独覆盖模型；未覆盖时使用下方官方默认模型。
      </template>
    </el-alert>

    <!-- 目录骨架 -->
    <div v-if="loading" class="model-list">
      <div v-for="i in 3" :key="i" class="model-card">
        <div style="padding: 16px">
          <div class="mf-skeleton mf-skeleton--text" style="width: 40%; margin-bottom: 10px"></div>
          <div class="mf-skeleton mf-skeleton--text" style="width: 80%"></div>
        </div>
      </div>
    </div>

    <!-- 模型卡列表 -->
    <div v-else-if="models.length" class="model-list">
      <div v-for="m in models" :key="m.id" class="model-card">
        <div class="model-head">
          <span class="model-name">{{ m.name }}</span>
          <el-tag v-if="m.is_default" type="primary" effect="soft" size="small">官方默认</el-tag>
          <el-tag
            v-if="healthLabels[m.health]"
            :type="healthLabels[m.health]?.type ?? 'info'"
            effect="plain"
            size="small"
          >渠道{{ healthLabels[m.health]?.label ?? m.health }}</el-tag>
        </div>
      </div>
    </div>
    <el-empty v-else description="该能力下暂无可用模型" />

    <!-- 自有 Key 管理 -->
    <section class="channel-section">
      <div class="section-head">
        <h3 class="section-title">自有 Key 管理</h3>
        <el-button type="primary" @click="openCreate">添加渠道</el-button>
      </div>
      <el-alert type="info" :closable="false" show-icon class="key-tip">
        <template #title>
          自有渠道的模型将优先用于生成任务，使用自有 Key 时免扣生成积分（存储 / 带宽 / 编排照常计费）；
          Key 服务端加密保存，永不明文展示。
        </template>
      </el-alert>

      <el-empty
        v-if="!channelsLoading && channels.length === 0"
        description="暂无自有渠道，内置官方模型已可用"
        :image-size="80"
      />
      <el-table v-else :data="channels" v-loading="channelsLoading" class="channel-table">
        <el-table-column label="渠道" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="channel-name">{{ row.name || '未命名渠道' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="protocol" label="协议" width="90" />
        <el-table-column prop="base_url" label="接口地址" min-width="200" show-overflow-tooltip />
        <el-table-column label="模型" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">
            <code v-if="channelModelList(row).length" class="model-ids">{{ channelModelList(row).join('、') }}</code>
            <span v-else class="model-ids model-ids--empty">未登记</span>
          </template>
        </el-table-column>
        <el-table-column prop="api_key" label="API Key" width="150">
          <template #default="{ row }">
            <code class="masked-key">{{ row.api_key || '••••••' }}</code>
          </template>
        </el-table-column>
        <el-table-column label="健康" width="90">
          <template #default="{ row }">
            <el-tag
              v-if="healthLabels[row.health_status]"
              :type="healthLabels[row.health_status]?.type ?? 'info'"
              size="small"
              effect="plain"
            >{{ healthLabels[row.health_status]?.label ?? row.health_status }}</el-tag>
            <span v-else>{{ row.health_status }}</span>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" @change="toggleEnabled(row)" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              size="small"
              :loading="testingIds.has(row.id)"
              @click="testChannel(row)"
            >测试连通</el-button>
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" size="small" @click="removeChannel(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <!-- 添加 / 编辑渠道对话框 -->
    <el-dialog
      v-model="channelDialogVisible"
      :title="dialogMode === 'create' ? '添加自有渠道' : '编辑自有渠道'"
      width="520px"
    >
      <el-form label-width="100px" label-position="left">
        <el-form-item label="API 格式" required>
          <el-select v-model="channelProtocol" style="width: 100%" @change="onProtocolChange">
            <el-option
              v-for="opt in protocolOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
          <div class="field-tip">各格式均已真实接入：文本生成、图像生成、效果对比与连通性测试均按所选协议调用。</div>
        </el-form-item>
        <el-form-item label="渠道名称">
          <el-input
            v-model="channelForm.name"
            maxlength="64"
            placeholder="选填，如「我的 DeepSeek」"
          />
        </el-form-item>
        <el-form-item label="请求地址" required>
          <el-input
            v-model="channelForm.base_url"
            :placeholder="channelProtocol === 'anthropic'
              ? '例如：https://api.anthropic.com'
              : channelProtocol === 'openai_responses'
                ? '例如：https://api.openai.com/v1'
                : channelProtocol === 'dashscope'
                  ? '例如：https://dashscope.aliyuncs.com'
                  : '例如：https://api.deepseek.com/v1'"
          />
          <div class="field-tip">{{ baseUrlTip() }}</div>
        </el-form-item>
        <el-form-item label="模型用途" required>
          <el-radio-group v-model="channelCapability" :disabled="channelProtocol === 'dashscope'">
            <el-radio value="chat">文本对话</el-radio>
            <el-radio value="image">图像生成</el-radio>
          </el-radio-group>
          <div class="field-tip">
            {{ channelProtocol === 'dashscope'
              ? 'DashScope 原生格式仅支持图像生成（百炼文生图任务协议）。'
              : channelCapability === 'image'
                ? '图像渠道将优先用于图像生成任务（角色定妆 / 分镜关键帧等），使用自有 Key 免生成积分。'
                : '文本渠道将优先用于剧本创作等文本生成任务，使用自有 Key 免生成积分。' }}
          </div>
        </el-form-item>
        <el-form-item label="模型 ID" required>
          <div class="model-id-row">
            <el-select
              v-model="channelModels"
              multiple
              filterable
              allow-create
              default-first-option
              :reserve-keyword="false"
              :loading="probingModels"
              placeholder="下拉选择或输入模型 ID 后回车，可添加多个"
              style="flex: 1"
            >
              <el-option v-for="m in modelOptions" :key="m" :label="m" :value="m" />
              <template #empty>
                <div class="select-empty">输入模型 ID 后回车添加，或点击右侧「获取模型列表」</div>
              </template>
            </el-select>
            <el-button
              :loading="probingModels"
              @click="fetchModelOptions"
            >获取模型列表</el-button>
          </div>
          <div class="field-tip">
            须与渠道服务端模型名一致（如文本 deepseek-chat / gpt-5 / claude-sonnet-4-5，
            图像 qwen-image-3.0）；获取列表需已填写接口地址与 API 密钥。
          </div>
        </el-form-item>
        <el-form-item label="API 密钥" :required="dialogMode === 'create'">
          <el-input
            v-model="channelForm.api_key"
            type="password"
            show-password
            :placeholder="dialogMode === 'create' ? 'sk-...' : '留空表示保持原 Key'"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="channelDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitChannel">
          {{ dialogMode === 'create' ? '保存' : '保存修改' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-3);
}
.cap-tabs :deep(.el-tabs__header) {
  margin-bottom: var(--mf-sp-4);
}
.layer-tip {
  margin-bottom: var(--mf-sp-5);
  border-radius: var(--mf-r-ctl);
}
.model-list {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
}
.model-card {
  border-radius: var(--mf-r-card);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  padding: var(--mf-sp-4) var(--mf-sp-5);
  transition: box-shadow var(--mf-dur) var(--mf-ease);
}
.model-card:hover {
  box-shadow: var(--mf-shadow-pop);
}
.model-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.model-name {
  font-weight: var(--mf-fw-semibold);
  font-size: var(--mf-fs-md);
}
.channel-section {
  margin-top: var(--mf-sp-8);
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--mf-sp-3);
}
.section-title {
  margin: 0;
  font-size: var(--mf-fs-lg);
  font-weight: var(--mf-fw-semibold);
}
.key-tip {
  margin-bottom: var(--mf-sp-3);
  border-radius: var(--mf-r-ctl);
}
.channel-table {
  width: 100%;
  border-radius: var(--mf-r-ctl);
  overflow: hidden;
}
.masked-key {
  font-family: var(--mf-mono);
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-2);
  background: var(--mf-surface-2);
  padding: 2px 8px;
  border-radius: 6px;
}
.channel-name {
  font-weight: var(--mf-fw-medium);
}
.model-ids {
  font-family: var(--mf-mono);
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-2);
}
.model-ids--empty {
  color: var(--mf-text-3);
  font-style: italic;
}
.field-tip {
  width: 100%;
  margin-top: 4px;
  font-size: var(--mf-fs-xs);
  line-height: 1.5;
  color: var(--mf-text-3);
}
.model-id-row {
  display: flex;
  gap: var(--mf-sp-2);
  width: 100%;
  align-items: flex-start;
}
.select-empty {
  padding: 12px 0;
  text-align: center;
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}
</style>
