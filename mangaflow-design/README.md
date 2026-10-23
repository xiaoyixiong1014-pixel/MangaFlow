# @mangaflow/design

漫序 MangaFlow 共享设计系统 —— `mangaflow-web`（官网 + 创作端）与 `mangaflow-admin-web`（运营管理端）共用的 **UI 单一事实源**（决策记录 RD-58 / 架构文档 §3.1.4）。

包内**不耦合业务**，仅输出：

1. **设计 token** —— CSS 变量（`tokens.css`）+ JS 调色板（`tokens/index.ts`）
2. **主题切换** —— `useTheme()` / `applyTheme()` / `toggleTheme()`
3. **基础组件** —— `MfButton` `MfTag` `MfCard` `MfSidebar` `MfTopbar` `MfShell`

---

## 设计 token 一览（与全站原型实测值严格同源）

### 品牌色

| token | 浅色（官网/管理端） | 深色（创作端） | 说明 |
| ---- | ---- | ---- | ---- |
| `--mf-brand` | `#12B3A6` 墨青 | `#4FD8CA` | Studio 引擎主色 |
| `--mf-brand-soft` | `rgba(18,179,166,.10)` | `rgba(18,179,166,.18)` | 选中态/徽标底 |
| `--mf-brand-contrast` | `#FFFFFF` | `#08302C` | 品牌色上的文字 |
| `--mf-brand2` | `#7C6BF0` 藤紫 | `#9A8CF5` | Canvas 引擎辅色（选中节点描边等） |

### 语义色（基础 + soft 浅底）

| token | 浅色 | 深色 |
| ---- | ---- | ---- |
| `--mf-ok` | `#1E9E6A` | `#4CC38A` |
| `--mf-warn` | `#B87100` | `#E0A33A` |
| `--mf-danger` | `#CE4343` | `#F0736F` |
| `--mf-info` | `#3B73C4` | `#5E9BE8` |

### 中性色

| token | 浅色 | 深色 | 用途 |
| ---- | ---- | ---- | ---- |
| `--mf-bg` | `#F5F7F6` | `#0E1112` | 页面底 |
| `--mf-surface` | `#FFFFFF` | `#15191A` | 卡片/面板 |
| `--mf-surface-2` | `#FAFAF8` | `#1C2223` | 次级面板/悬浮 |
| `--mf-surface-3` | `#EFF2F1` | `#111516` | 侧栏（原型 `--sidebar`） |
| `--mf-text` / `-2` / `-3` | `#14181A` / `#5F6B68` / `#8E9A97` | `#E8EDEA` / `#9BA5A2` / `#6B7573` | 文字三级 |
| `--mf-border` / `-strong` | `rgba(0,0,0,.09)` / `.16` | `rgba(255,255,255,.09)` / `.16` | 边框两级 |

### 圆角 / 布局 / 动效

| token | 值 | 说明 |
| ---- | ---- | ---- |
| `--mf-r-card` | `20px` | 卡片圆角（原型实测，非 24px） |
| `--mf-r-ctl` | `12px` | 控件圆角（按钮/输入框） |
| `--mf-r-pill` | `999px` | 胶囊/标签 |
| `--mf-sidebar-w` | `252px` | 常驻侧栏宽（W-01 定稿） |
| `--mf-topbar-h` | `62px` | 顶栏高 |
| `--mf-dur` / `--mf-ease` | `.18s` / `cubic-bezier(.4,0,.2,1)` | 统一动效 |

> 完整 token（字号阶梯、字重、4px 间距阶、z-index、焦点环）见 `src/styles/tokens.css`。

---

## 主题策略

| 场景 | 默认主题 | 实现方式 |
| ---- | ---- | ---- |
| 官网门户 / 运营管理端 | 浅色 | `<html>` 不设或 `data-theme="light"` |
| 创作端（工作台/Studio/Canvas） | 深色 | `<html data-theme="dark">` |

`tokens.css` 中 `:root` 为浅色组，`[data-theme="dark"]` 为覆盖组。

```ts
import { initTheme, toggleTheme, useTheme } from '@mangaflow/design'

initTheme('dark')                    // 应用启动时初始化（也读 localStorage / 系统偏好）
const { theme, toggleTheme } = useTheme()
```

---

## 安装与引入（消费方）

本包为独立仓库（RD-58），通过 **npm 私服发布** 或 **git 依赖** 引入：

```bash
# 方式 A：npm 私服
npm install @mangaflow/design

# 方式 B：git 依赖（当前形态）
npm install git+<本仓库地址>
```

应用入口 `main.ts`：

```ts
import '@mangaflow/design/styles.css'          // 必引：token + base + 组件样式
// 可选：
import '@mangaflow/design/element-plus.css'    // EP 主题桥接（--el-* → --mf-*）
import '@mangaflow/design/compat.css'          // 原型旧变量名兼容（--accent 等）
```

> 引入 Element Plus 的应用**建议引桥接文件**，EP 组件即自动获得墨青/藤紫色板与 12px 控件圆角。

### 使用组件

```ts
// 方式 A：按需导入（推荐，利于 tree-shaking）
import { MfButton, MfCard, MfShell } from '@mangaflow/design'

// 方式 B：全量注册
import MangaflowDesign from '@mangaflow/design'
app.use(MangaflowDesign)
```

```vue
<MfShell :menu="menu" :active-key="active" @select="onSelect">
  <template #topbar-left>…</template>
  <template #topbar-right>…</template>
  <MfCard header="最近生成">…</MfCard>
</MfShell>
```

`MfSidebar` 的菜单结构为通用 `MfMenuGroup[]`（`{ title?, items: [{ key, label, icon?, badge?, disabled? }] }`），由应用传入——包内不含任何业务菜单。

### JS 侧取色（画布 / 图表）

```ts
import { palettes, getCssToken } from '@mangaflow/design'

palettes.dark.brand   // '#4FD8CA'，画布节点绘制用
getCssToken('brand')  // 运行时读当前主题 CSS 变量
```

---

## 本地开发

```bash
npm install
npm run dev          # 启动 playground（可视化验收 token 与组件）
npm run build        # 构建 dist（ESM + dts + css + 桥接层）
npm run typecheck    # vue-tsc 全量类型检查
```

`playground/` 为可视化验收页：色板 / 按钮 / 标签 / 圆角 / 侧栏外壳，右上角可切主题。默认菜单数据即 UI 文档 §0.6.1 定稿表（仅演示）。

---

## 双仓一致性约定（防漂移）

1. **改色必须改这里**：任何 `#12B3A6` / `#7C6BF0` 等 token 值的调整，只允许改 `src/styles/tokens.css` 与 `src/tokens/index.ts`（两者必须同步），两个应用仓库**禁止出现裸色值**。
2. **发版走变更记录**：token 或组件变更需升版本号并在两个消费仓库同步升级依赖。
3. **CI 断言**（建议，两应用仓库各配一条）：`grep -rE "#(12B3A6|7C6BF0)" src/ --include="*.vue"` 命中即构建失败。

---

## 变更记录

| 版本 | 日期 | 变更 |
| ---- | ---- | ---- |
| 0.1.0 | 2026-10-09 | 首版：token 全集（浅/深双主题）、主题切换、6 个基础组件、EP 桥接、原型兼容层、playground |
