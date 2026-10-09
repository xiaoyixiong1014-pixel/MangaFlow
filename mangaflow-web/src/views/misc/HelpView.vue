<!--
  帮助中心（文件级注释）。
  公开页：常见问题手风琴（6 条）+ 画布快捷键说明表。
-->
<script setup lang="ts">
import { ref } from 'vue'

/** 当前展开的 FAQ 项 */
const activeNames = ref<string[]>(['faq-1'])

/** 常见问题列表（标题 + 富文本段落） */
const faqs = [
  {
    key: 'faq-1',
    title: '如何创建漫剧项目？',
    blocks: [
      '登录后进入「漫剧项目」页，点击右上角「新建项目」，填写项目标题、体裁与视觉风格即可创建。',
      '也可以在「AI 工作台」直接用一句话描述创作意图，由智能体引导你完成建项与后续流程。',
    ],
  },
  {
    key: 'faq-2',
    title: '如何生成剧本？',
    blocks: [
      '进入项目创作工作区的「剧本创作」阶段，选择输入模式（创意点子 / 小说原文 / 大纲）并粘贴或输入原文。',
      '点击「AI 生成」，系统会流式产出结构化剧本（故事梗概、分集大纲、台词本），生成后可手动编辑保存并做实体抽取。',
    ],
  },
  {
    key: 'faq-3',
    title: '积分如何计费？',
    blocks: [
      '生成类任务（图像 / 视频 / 配音）按次冻结积分，任务失败自动退回；文本与编排类操作通常不计费。',
      '可在「计费与配额」页查看可用积分、冻结积分与每月消耗流水；余额不足时请及时充值，已提交的任务会继续完成。',
    ],
  },
  {
    key: 'faq-4',
    title: '如何分享我的作品？',
    blocks: [
      '作品合成成片后，在创作工作区「发布」阶段选择成片版本并设置有效期（可选提取码），即可生成 /share/{token} 分享链接。',
      '任何人无需登录即可通过链接观看；你可以随时撤回链接，撤回后访问将展示失效页。',
    ],
  },
  {
    key: 'faq-5',
    title: '模型不可用怎么办？',
    blocks: [
      '「模型配置」页会展示每个模型的渠道健康状态；主渠道异常时平台会自动降级到备用模型并在界面明确提示，效果可能略有差异。',
      '也可以在「自有 Key 管理」中添加自己的 OpenAI 协议渠道，添加后可点击「测试连通」验证可用性。',
    ],
  },
  {
    key: 'faq-6',
    title: '数据安全说明',
    blocks: [
      '你的项目、资产与素材仅本人可见；自有渠道 API Key 采用服务端加密存储，任何界面都不会明文展示。',
      '分享链接仅暴露所选成片内容，可随时撤回；平台对生成内容设有机审与人审闸门以保障合规。',
    ],
  },
]

/** 快捷键说明表 */
const shortcuts = [
  { keys: 'Ctrl + S', desc: '保存画布（立即全量同步）' },
  { keys: 'Ctrl + Z', desc: '撤销上一步操作' },
  { keys: 'Delete', desc: '删除画布中选中的节点' },
  { keys: 'F', desc: '画布适配视图（全部节点入视口）' },
]
</script>

<template>
  <div class="help-page">
    <header class="help-hero">
      <div class="help-logo" aria-hidden="true">漫序</div>
      <h1 class="help-title">帮助中心</h1>
      <p class="help-sub">创作漫剧过程中常见问题的答案与快捷键速查</p>
    </header>

    <section class="help-section">
      <h2 class="section-title">常见问题</h2>
      <el-collapse v-model="activeNames" class="help-collapse">
        <el-collapse-item v-for="f in faqs" :key="f.key" :name="f.key" :title="f.title">
          <p v-for="(b, i) in f.blocks" :key="i" class="faq-block">{{ b }}</p>
        </el-collapse-item>
      </el-collapse>
    </section>

    <section class="help-section">
      <h2 class="section-title">画布快捷键</h2>
      <el-table :data="shortcuts" class="help-table" size="large">
        <el-table-column prop="keys" label="快捷键" width="200">
          <template #default="{ row }">
            <kbd class="kbd">{{ row.keys }}</kbd>
          </template>
        </el-table-column>
        <el-table-column prop="desc" label="功能说明" />
      </el-table>
    </section>

    <footer class="help-foot">
      <router-link to="/" class="foot-link">← 返回漫序 MangaFlow 首页</router-link>
    </footer>
  </div>
</template>

<style scoped>
.help-page {
  min-height: 100vh;
  padding: var(--mf-sp-9) var(--mf-sp-5) var(--mf-sp-10);
  max-width: 860px;
  margin: 0 auto;
  color: var(--mf-text);
}
.help-hero {
  text-align: center;
  margin-bottom: var(--mf-sp-8);
}
.help-logo {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  border-radius: var(--mf-r-ctl);
  background: linear-gradient(135deg, var(--mf-brand), var(--mf-brand2));
  color: var(--mf-brand-contrast);
  font-weight: var(--mf-fw-bold);
  font-size: var(--mf-fs-lg);
  margin-bottom: var(--mf-sp-4);
}
.help-title {
  margin: 0 0 var(--mf-sp-2);
  font-size: var(--mf-fs-3xl);
  font-weight: var(--mf-fw-bold);
}
.help-sub {
  margin: 0;
  color: var(--mf-text-2);
}
.help-section {
  margin-bottom: var(--mf-sp-8);
}
.section-title {
  margin: 0 0 var(--mf-sp-4);
  font-size: var(--mf-fs-xl);
  font-weight: var(--mf-fw-semibold);
}
.help-collapse {
  border-radius: var(--mf-r-ctl);
}
.faq-block {
  margin: 0 0 var(--mf-sp-2);
  color: var(--mf-text-2);
  line-height: 1.8;
}
.help-table {
  width: 100%;
  border-radius: var(--mf-r-ctl);
  overflow: hidden;
}
.kbd {
  display: inline-block;
  padding: 2px 10px;
  border: 1px solid var(--mf-border-strong);
  border-bottom-width: 2px;
  border-radius: 6px;
  background: var(--mf-surface-2);
  font-family: var(--mf-mono);
  font-size: var(--mf-fs-sm);
}
.help-foot {
  text-align: center;
  margin-top: var(--mf-sp-9);
}
.foot-link {
  color: var(--mf-brand);
  text-decoration: none;
  font-size: var(--mf-fs-sm);
}
.foot-link:hover {
  text-decoration: underline;
}
</style>
