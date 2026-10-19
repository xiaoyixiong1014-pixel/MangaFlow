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

/** 正在切换启用状态的逻辑模型 ID 集合 */
const togglingIds = ref<Set<number>>(new Set())

/**
 * 切换系统模型的启用/停用（停用后生成仅走自有渠道 + mock 兜底）。
 * @param m 逻辑模型条目
 */
async function toggleModelEnabled(m: LogicalModel): Promise<void> {
  const next = !m.enabled
  togglingIds.value = new Set(togglingIds.value).add(m.id)
  try {
    await catalogApi.setModelEnabled(m.id, next)
    m.enabled = next
    ElMessage.success(next
      ? `已启用系统模型 ${m.name}（生成将优先自有渠道，失败回落系统渠道）`
      : `已停用系统模型 ${m.name}（生成仅使用你的自有渠道）`)
  } catch {
    // 错误已由拦截器提示
  } finally {
    const nextIds = new Set(togglingIds.value)
    nextIds.delete(m.id)
    togglingIds.value = nextIds
  }
}

/** 当前能力 Tab */
const capability = ref<'llm' | 'image' | 'video' | 'tts'>('llm')

/**
 * 当前能力下的官方模型列表。
 */
const models = computed<LogicalModel[]>(() => {
  const group = catalog.value?.capabilities.find((g) => g.capability === capability.value)
  return group?.models ?? []
})

/** 用途键 → 中文标签 */
const capabilityKeyLabels: Record<string, string> = {
  chat: '文本对话',
  image: '图像生成',
  video: '视频生成',
  tts: '语音合成',
}

/**
 * 读取渠道登记的能力键（内部函数）：models 目录第一个非空键。
 * @param ch 渠道对象
 * @returns 能力键（chat/image/video/tts）；未登记返回空串
 */
function channelCapabilityKey(ch: Channel): string {
  const models = ch.models ?? {}
  return CAPABILITY_KEYS.find((k) => {
    const v = models[k]
    return Array.isArray(v) && v.length > 0
  }) ?? ''
}

/**
 * 当前能力 Tab 下的我的模型列表（随 Tab 联动过滤）。
 */
const filteredChannels = computed<Channel[]>(() =>
  channels.value.filter((ch) => channelCapabilityKey(ch) === capability.value))

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
/** 模型用途（登记能力键）：chat 文本 / image 图像 / video 视频 / tts 语音 */
const channelCapability = ref<'chat' | 'image' | 'video' | 'tts'>('chat')
/** 渠道真实模型列表候选（点击「获取模型列表」后填充） */
const modelOptions = ref<string[]>([])
/** 拉取模型列表中 */
const probingModels = ref(false)
/** 手动输入框内容（支持逗号/换行分隔批量解析） */
const modelInput = ref('')
/** 模型列表是否已拉取展示（控制 chip 列表显隐） */
const probedOptionsShown = ref(false)

/**
 * 解析手动输入并批量加入模型 ID（内部函数）。
 * 支持中英文逗号 / 分号 / 换行 / 空格分隔，自动去重、去空白。
 */
function addModelsFromInput(): void {
  const parts = modelInput.value
    .split(/[，,;；\n\s]+/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (!parts.length) {
    ElMessage.warning('请输入至少一个模型 ID')
    return
  }
  const existing = new Set(channelModels.value)
  let added = 0
  for (const p of parts) {
    if (!existing.has(p)) {
      channelModels.value.push(p)
      existing.add(p)
      added += 1
    }
  }
  modelInput.value = ''
  if (added > 0) {
    ElMessage.success(`已添加 ${added} 个模型 ID`)
  } else {
    ElMessage.warning('输入的模型 ID 均已存在')
  }
}

/**
 * 移除已添加的模型 ID（内部函数）。
 * @param m 模型 ID
 */
function removeModel(m: string): void {
  channelModels.value = channelModels.value.filter((x) => x !== m)
}

/**
 * 点击模型列表 chip 加入 / 再点移除（内部函数）。
 * @param m 模型 ID
 */
function toggleModelOption(m: string): void {
  if (channelModels.value.includes(m)) {
    removeModel(m)
  } else {
    channelModels.value.push(m)
  }
}

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
 * 切换 API 格式（内部函数）：DashScope 原生不支持文本对话与语音合成
 * （百炼文本走兼容模式、语音走 WS 推理），自动切到图像生成。
 */
function onProtocolChange(): void {
  if (channelProtocol.value === 'dashscope' && (channelCapability.value === 'chat' || channelCapability.value === 'tts')) {
    channelCapability.value = 'image'
  }
}

/** 各模型用途的说明文案映射 */
const capabilityTips: Record<string, string> = {
  chat: '文本渠道将优先用于剧本创作等文本生成任务，使用自有 Key 免生成积分。',
  image: '图像渠道将优先用于图像生成任务（角色定妆 / 分镜关键帧等），使用自有 Key 免生成积分。',
  video: '视频渠道将优先用于视频生成任务（分镜动态化 / 图生视频等），使用自有 Key 免生成积分。',
  tts: '语音渠道将优先用于台词配音任务（按百炼 WS 推理协议调用，建议登记百炼 TTS 模型），使用自有 Key 免生成积分。',
}

/**
 * 从渠道 models 目录提取模型 ID 列表（内部函数）：按 chat > image > video > tts
 * 优先级取第一个非空能力键。
 * @param ch 渠道对象
 * @returns 模型 ID 字符串数组（结构异常时为空数组）
 */
function channelModelList(ch: Channel): string[] {
  const models = ch.models ?? {}
  const list = models['chat'] ?? models['image'] ?? models['video'] ?? models['tts']
  return Array.isArray(list) ? list.map(String) : []
}

/** 能力键优先级（编辑回显用） */
const CAPABILITY_KEYS = ['chat', 'image', 'video', 'tts'] as const

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
  modelInput.value = ''
  probedOptionsShown.value = false
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
  // 模型用途回显：按 models 目录第一个非空能力键还原
  const models = ch.models ?? {}
  const cap = CAPABILITY_KEYS.find((k) => {
    const v = models[k]
    return Array.isArray(v) && v.length > 0
  })
  channelCapability.value = (cap as typeof channelCapability.value) ?? 'chat'
  modelOptions.value = []
  modelInput.value = ''
  probedOptionsShown.value = false
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
      probedOptionsShown.value = true
      ElMessage.success(`已获取 ${resp.models.length} 个模型，点击即可加入`)
    } else {
      ElMessage.warning(resp.error || '渠道未返回模型列表，可手动输入模型 ID 后点击「添加」')
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

    <!-- 生效规则说明条 -->
    <el-alert type="info" :closable="false" class="layer-tip" show-icon>
      <template #title>
        生成顺序：我的模型（优先，免生成积分）→ 官方模型（可停用）→ 本地兜底。下方开关控制各自是否参与生成。
      </template>
    </el-alert>

    <!-- 我的模型（自有 Key，随能力 Tab 过滤） -->
    <section class="model-section">
      <div class="section-head">
        <div>
          <h3 class="section-title">我的模型</h3>
          <p class="section-sub">使用自己的 API Key，生成免扣积分，优先于官方模型</p>
        </div>
        <el-button type="primary" @click="openCreate">添加模型</el-button>
      </div>

      <el-empty
        v-if="!channelsLoading && filteredChannels.length === 0"
        :description="channels.length === 0
          ? '还没有添加模型，官方模型已可直接使用'
          : `当前「${capabilityTabs.find((t) => t.value === capability)?.label}」分类下暂无自有模型`"
        :image-size="80"
      />
      <el-table v-else :data="filteredChannels" v-loading="channelsLoading" class="channel-table">
        <el-table-column label="模型" min-width="230">
          <template #default="{ row }">
            <div class="mine-name">{{ row.name || '未命名模型' }}</div>
            <code class="model-ids">{{ channelModelList(row).join('、') }}</code>
          </template>
        </el-table-column>
        <el-table-column label="用途" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ capabilityKeyLabels[channelCapabilityKey(row)] || '未设置' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="180">
          <template #default="{ row }">
            <el-tag
              v-if="healthLabels[row.health_status]"
              :type="healthLabels[row.health_status]?.type ?? 'info'"
              size="small"
              effect="plain"
              style="margin-right: 8px"
            >{{ healthLabels[row.health_status]?.label ?? row.health_status }}</el-tag>
            <el-switch
              :model-value="row.enabled"
              inline-prompt
              active-text="启用"
              inactive-text="停用"
              @change="toggleEnabled(row)"
            />
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

    <!-- 官方模型（随能力 Tab 过滤） -->
    <section class="channel-section">
      <div class="section-head">
        <div>
          <h3 class="section-title">官方模型</h3>
          <p class="section-sub">平台提供、开箱即用；开关停用后生成仅使用我的模型</p>
        </div>
      </div>
      <!-- 目录骨架 -->
      <div v-if="loading" class="model-list">
        <div v-for="i in 3" :key="i" class="model-card">
          <div style="padding: 16px">
            <div class="mf-skeleton mf-skeleton--text" style="width: 40%; margin-bottom: 10px"></div>
            <div class="mf-skeleton mf-skeleton--text" style="width: 80%"></div>
          </div>
        </div>
      </div>
      <div v-else-if="models.length" class="model-list">
        <div v-for="m in models" :key="m.id" class="model-card" :class="{ 'model-card--disabled': !m.enabled }">
          <div class="model-head">
            <span class="model-name">{{ m.name }}</span>
            <el-tag v-if="m.is_default" type="primary" effect="soft" size="small">官方默认</el-tag>
            <el-tag
              v-if="healthLabels[m.health]"
              :type="healthLabels[m.health]?.type ?? 'info'"
              effect="plain"
              size="small"
            >渠道{{ healthLabels[m.health]?.label ?? m.health }}</el-tag>
            <div class="model-enable">
              <span class="model-enable-label">{{ m.enabled ? '已启用' : '已停用' }}</span>
              <el-switch
                :model-value="m.enabled"
                :loading="togglingIds.has(m.id)"
                @change="toggleModelEnabled(m)"
              />
            </div>
          </div>
        </div>
      </div>
      <el-empty v-else description="该能力下暂无官方模型" />
    </section>

    <!-- 添加 / 编辑我的模型对话框 -->
    <el-dialog
      v-model="channelDialogVisible"
      :title="dialogMode === 'create' ? '添加我的模型' : '编辑我的模型'"
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
        <el-form-item label="模型名称">
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
          <el-radio-group v-model="channelCapability">
            <el-radio value="chat" :disabled="channelProtocol === 'dashscope'">文本对话</el-radio>
            <el-radio value="image">图像生成</el-radio>
            <el-radio value="video">视频生成</el-radio>
            <el-radio value="tts" :disabled="channelProtocol === 'dashscope'">语音合成</el-radio>
          </el-radio-group>
          <div class="field-tip">
            {{ channelProtocol === 'dashscope' && (channelCapability === 'chat' || channelCapability === 'tts')
              ? 'DashScope 原生格式不支持文本对话与语音合成，请选择图像或视频用途。'
              : (capabilityTips[channelCapability] ?? '') }}
          </div>
        </el-form-item>
        <el-form-item label="模型 ID" required>
          <div class="model-id-block">
            <!-- 已添加的模型 ID（可删除） -->
            <div v-if="channelModels.length" class="added-tags">
              <el-tag
                v-for="m in channelModels"
                :key="m"
                closable
                size="large"
                effect="plain"
                @close="removeModel(m)"
              >{{ m }}</el-tag>
            </div>
            <div v-else class="added-tags added-tags--empty">尚未添加模型 ID</div>
            <!-- 手动输入：支持逗号 / 换行分隔批量添加 -->
            <div class="model-id-row">
              <el-input
                v-model="modelInput"
                placeholder="输入模型 ID，多个用逗号或换行分隔，可一次粘贴多个"
                @keyup.enter="addModelsFromInput"
              />
              <el-button type="primary" plain @click="addModelsFromInput">添加</el-button>
            </div>
            <!-- 从渠道拉取的真实模型列表：点击加入 / 再点移除 -->
            <div class="model-id-row">
              <el-button
                :loading="probingModels"
                @click="fetchModelOptions"
              >{{ probedOptionsShown ? '刷新模型列表' : '从渠道获取模型列表' }}</el-button>
              <span v-if="probedOptionsShown" class="field-tip model-id-tip">点击下方模型加入 / 再点移除</span>
            </div>
            <div v-if="probedOptionsShown && modelOptions.length" class="model-chips">
              <span
                v-for="m in modelOptions"
                :key="m"
                class="model-chip"
                :class="{ 'model-chip--added': channelModels.includes(m) }"
                @click="toggleModelOption(m)"
              >{{ m }}</span>
            </div>
            <div v-if="probedOptionsShown && !modelOptions.length" class="field-tip">
              渠道未返回模型列表，请手动输入
            </div>
          </div>
          <div class="field-tip">
            须与渠道服务端模型名一致（文本 deepseek-chat / gpt-5 / claude-sonnet-4-5，
            图像 qwen-image-3.0，视频 wan2.7-i2v，语音 qwen-audio-tts-flash）；
            获取列表需已填写接口地址与 API 密钥。
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
.model-enable {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.model-enable-label {
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}
.model-card--disabled {
  opacity: 0.55;
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
.section-sub {
  margin: 2px 0 0;
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}
.model-section {
  margin-bottom: var(--mf-sp-6);
}
.channel-section {
  margin-top: var(--mf-sp-6);
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--mf-sp-3);
}
.channel-table {
  width: 100%;
  border-radius: var(--mf-r-ctl);
  overflow: hidden;
}
.mine-name {
  font-weight: var(--mf-fw-medium);
  line-height: 1.4;
}
.model-ids {
  font-family: var(--mf-mono);
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-2);
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
.model-id-block {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-2);
}
.added-tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--mf-sp-2);
}
.added-tags--empty {
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}
.model-id-tip {
  width: auto;
  margin-top: 0;
  align-self: center;
}
.model-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--mf-sp-2);
  max-height: 168px;
  overflow-y: auto;
  padding: var(--mf-sp-2);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-surface-2);
}
.model-chip {
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--mf-border);
  font-family: var(--mf-mono);
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-2);
  cursor: pointer;
  user-select: none;
  transition: all var(--mf-dur) var(--mf-ease);
}
.model-chip:hover {
  border-color: var(--mf-brand);
  color: var(--mf-brand);
}
.model-chip--added {
  background: var(--mf-brand);
  border-color: var(--mf-brand);
  color: #fff;
}
</style>
