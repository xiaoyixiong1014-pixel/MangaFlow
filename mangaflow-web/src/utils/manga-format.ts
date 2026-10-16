/**
 * 漫剧规范格式渲染器（文件级注释，redesign-script-workspace Task 4 · P3）。
 *
 * 把单集正文的结构化场次（ScriptBodyContent.scenes，字段与后端
 * RD-03/episode_scenes 结构对齐：action/location/time_of_day/int_ext/sfx/
 * emotion + dialogue[].type/emotion/character_id）渲染为「漫剧规范文本」：
 * - 场景标题行：【场景：{地点}，{时段}，{内/外景}】（字段空值优雅跳过，
 *   全空时回退为场次标题）；
 * - 动作行：action 文本；sfx 存在时追加【音效：…】行；
 * - 台词行：按 dialogue.type 区分标注——
 *   · 对白（缺省）→「角色名（情绪）：台词」（无情绪时省略括号）；
 *   · 旁白 →「旁白：台词」；
 *   · 独白 →「角色名（独白）：台词」；
 *   · 画外音 →「画外音·角色名：台词」。
 *
 * 设计约束：纯函数、无副作用、不修改入参；仅做类型导入（import type），
 * 不引入 api 模块运行时代码；供剧本正文卡片「漫剧格式」只读视图与复制
 * 导出使用（redesign-script-workspace spec「漫剧规范格式渲染」条目）。
 */
import type { EpisodeBodyDialogue, EpisodeBodyScene } from '@/api/scriptPipeline'

/** 场次输入类型（与 scriptPipeline.ts 的 EpisodeBodyScene 对齐，别名导出便于调用方表意） */
export type SceneItem = EpisodeBodyScene

/** 台词条目类型（与 scriptPipeline.ts 的 EpisodeBodyDialogue 对齐） */
export type DialogueItem = EpisodeBodyDialogue

/** 角色最小档案（character_id → 角色名回填的可选映射来源） */
export interface MangaCharacterRef {
  /** 角色资产 ID */
  id: number
  /** 角色名 */
  name: string
}

/** 时段英文值 → 中文标注（与后端 episode_scenes 提示词白名单一致） */
const TIME_OF_DAY_LABELS: Record<string, string> = {
  day: '日',
  night: '夜',
  dawn: '晨',
  dusk: '暮',
}

/** 内外景英文值 → 中文标注（int/ext 白名单） */
const INT_EXT_LABELS: Record<string, string> = {
  int: '内景',
  ext: '外景',
}

/**
 * 任意值归一为去空白字符串（null/undefined → ''，其余 String 化后 trim）。
 * @param v 任意字段值
 * @returns 归一后的字符串
 */
function cleanText(v: unknown): string {
  return v == null ? '' : String(v).trim()
}

/**
 * 按已确认角色档案回填角色名（dialogue.character 缺失时以 character_id 查表）。
 * @param dialogue 台词条目
 * @param characters 可选角色档案列表（id → name）
 * @returns 角色名（无法解析时返回空串，由调用方决定兜底文案）
 */
function resolveCharacterName(
  dialogue: DialogueItem,
  characters?: MangaCharacterRef[],
): string {
  const direct = cleanText(dialogue.character)
  if (direct) return direct
  if (characters && dialogue.character_id != null) {
    const hit = characters.find((c) => c.id === dialogue.character_id)
    if (hit) return cleanText(hit.name)
  }
  return ''
}

/**
 * 渲染单条台词为漫剧规范行（按 type 区分标注，空台词返回 null 由调用方跳过）。
 * @param dialogue 台词条目（type 缺省按「对白」处理）
 * @param characters 可选角色档案列表（character_id 回填角色名）
 * @returns string 台词行文本；line 为空时返回 null（优雅跳过）
 */
function formatDialogueLine(
  dialogue: DialogueItem,
  characters?: MangaCharacterRef[],
): string | null {
  const line = cleanText(dialogue.line)
  if (!line) return null
  const type = cleanText(dialogue.type) || '对白'
  if (type === '旁白') {
    return `旁白：${line}`
  }
  if (type === '独白') {
    const name = resolveCharacterName(dialogue, characters) || '未知角色'
    return `${name}（独白）：${line}`
  }
  if (type === '画外音') {
    const name = resolveCharacterName(dialogue, characters) || '未知角色'
    return `画外音·${name}：${line}`
  }
  // 对白（含未知类型兜底）：「角色名（情绪）：台词」，无情绪省略括号
  const name = resolveCharacterName(dialogue, characters) || '未知角色'
  const emotion = cleanText(dialogue.emotion)
  return emotion ? `${name}（${emotion}）：${line}` : `${name}：${line}`
}

/**
 * 渲染单个场次为漫剧规范文本块（场景标题行 + 动作/音效行 + 台词行）。
 * @param scene 场次结构化条目
 * @param characters 可选角色档案列表（character_id → 角色名回填）
 * @returns string 该场次的漫剧格式文本（多行，行间以 \n 连接）
 */
export function renderMangaScene(
  scene: SceneItem,
  characters?: MangaCharacterRef[],
): string {
  const lines: string[] = []

  // 场景标题行：【场景：地点，时段，内/外景】（空值优雅跳过；全空回退场次标题）
  const location = cleanText(scene.location)
  const timeRaw = cleanText(scene.time_of_day)
  const timeLabel = timeRaw ? (TIME_OF_DAY_LABELS[timeRaw] ?? timeRaw) : ''
  const intRaw = cleanText(scene.int_ext)
  const intLabel = intRaw ? (INT_EXT_LABELS[intRaw] ?? intRaw) : ''
  const headerParts = [location, timeLabel, intLabel].filter(Boolean)
  lines.push(
    headerParts.length
      ? `【场景：${headerParts.join('，')}】`
      : `【场景：${cleanText(scene.title) || `场景 ${scene.scene_no}`}】`,
  )

  // 动作行 + 音效行（sfx 逐项过滤空值后以「、」连接）
  const action = cleanText(scene.action)
  if (action) lines.push(action)
  const sfx = (Array.isArray(scene.sfx) ? scene.sfx : [])
    .map((s) => cleanText(s))
    .filter(Boolean)
  if (sfx.length) lines.push(`【音效：${sfx.join('、')}】`)

  // 台词行（空台词优雅跳过）
  const dialogue = Array.isArray(scene.dialogue) ? scene.dialogue : []
  for (const item of dialogue) {
    const rendered = formatDialogueLine(item, characters)
    if (rendered) lines.push(rendered)
  }
  return lines.join('\n')
}

/**
 * 渲染整集结构化场次为漫剧规范文本（场次块之间以空行分隔）。
 * @param scenes 结构化场次列表（可空数组 → 返回空串）
 * @param characters 可选角色档案列表（character_id → 角色名回填）
 * @returns string 整集漫剧格式文本
 */
export function renderMangaScript(
  scenes: SceneItem[],
  characters?: MangaCharacterRef[],
): string {
  const list = Array.isArray(scenes) ? scenes : []
  return list
    .map((scene) => renderMangaScene(scene, characters))
    .filter((block) => block.length > 0)
    .join('\n\n')
}
