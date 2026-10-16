/**
 * 漫剧格式渲染器自测脚本（文件级注释，redesign-script-workspace Task 4）。
 *
 * 运行方式（项目根目录）：`npx tsx .run/test_manga_format.ts`
 * 覆盖断言：场景标题行（全字段/部分字段/空值回退标题）、动作行、
 * 音效行、四种台词 type（对白含情绪/旁白/独白/画外音）、空台词跳过、
 * character_id 角色名回填、整集拼接与空数组兜底。
 * 全部断言通过后输出「manga-format 自测全部通过」并 exit 0。
 */
import assert from 'node:assert/strict'

import { renderMangaScene, renderMangaScript } from '../mangaflow-web/src/utils/manga-format.ts'

// ---------------------------------------------------------------------------
// 1. 完整场景：场景标题（地点/时段/内外景）+ 动作 + 音效 + 四种台词 type
// ---------------------------------------------------------------------------
const fullScene = {
  scene_no: 1,
  title: '雨夜重逢',
  plot: '两人在天台重逢',
  action: '暴雨中，林小满冲上天台，纸箱散落一地。',
  location: '学校天台',
  time_of_day: 'night',
  int_ext: 'ext',
  sfx: ['雨声', ' 雷声 ', ''],
  emotion: '压抑',
  dialogue: [
    { character: '林小满', line: '你终于肯见我了。', type: '对白', emotion: '哽咽' },
    { character: '', line: '雨越下越大，两人隔着天台对望。', type: '旁白' },
    { character: '陈默', line: '原来我一直不敢承认……', type: '独白', emotion: '' },
    { character: '陈默', line: '小满，别走。', type: '画外音' },
    { character: '林小满', line: '   ', type: '对白' }, // 空台词 → 跳过
  ],
  duration_sec: 60,
}

const fullText = renderMangaScene(fullScene as never)
assert.equal(fullText, [
  '【场景：学校天台，夜，外景】',
  '暴雨中，林小满冲上天台，纸箱散落一地。',
  '【音效：雨声、雷声】',
  '林小满（哽咽）：你终于肯见我了。',
  '旁白：雨越下越大，两人隔着天台对望。',
  '陈默（独白）：原来我一直不敢承认……',
  '画外音·陈默：小满，别走。',
].join('\n'), '完整场景渲染应符合漫剧规范格式')

// ---------------------------------------------------------------------------
// 2. 空值优雅跳过：无地点/时段/内外景 → 回退场次标题；无情绪省略括号；
//    type 缺省按对白处理；无动作/无音效时不产生对应行
// ---------------------------------------------------------------------------
const bareScene = {
  scene_no: 3,
  title: '教室对峙',
  plot: '两人争执',
  action: null,
  location: null,
  time_of_day: null,
  int_ext: null,
  sfx: null,
  dialogue: [
    { character: '林小满', line: '这稿子我不会改。', type: null, emotion: null },
  ],
  duration_sec: null,
}
assert.equal(
  renderMangaScene(bareScene as never),
  ['【场景：教室对峙】', '林小满：这稿子我不会改。'].join('\n'),
  '空字段场景应优雅跳过并回退标题',
)

// ---------------------------------------------------------------------------
// 3. character_id 回填角色名：character 缺失时按 id 查角色档案；
//    无法解析时兜底「未知角色」；时段/内外景未知英文值原样透出
// ---------------------------------------------------------------------------
const characters = [
  { id: 1, name: '林小满' },
  { id: 2, name: '陈默' },
]
const idScene = {
  scene_no: 2,
  title: '',
  plot: '',
  action: '走廊尽头的灯闪了一下。',
  location: '教学楼走廊',
  time_of_day: 'dusk',
  int_ext: 'int',
  sfx: [],
  dialogue: [
    { character: '', line: '别再提那天的事。', type: '对白', emotion: '低声', character_id: 2 },
    { character: '', line: '（回声）', type: '独白', character_id: 99 },
  ],
  duration_sec: 30,
}
assert.equal(renderMangaScene(idScene as never, characters), [
  '【场景：教学楼走廊，暮，内景】',
  '走廊尽头的灯闪了一下。',
  '陈默（低声）：别再提那天的事。',
  '未知角色（独白）：（回声）',
].join('\n'), 'character_id 应回填角色名，未知 id 兜底未知角色')

// ---------------------------------------------------------------------------
// 4. 整集渲染：场次块以空行分隔；空数组返回空串
// ---------------------------------------------------------------------------
const all = renderMangaScript([fullScene, bareScene] as never, characters)
assert.equal(all, [fullText, renderMangaScene(bareScene as never)].join('\n\n'),
  '整集渲染应以空行分隔场次块')
assert.equal(renderMangaScript([] as never), '', '空场次列表应渲染为空串')

console.log('manga-format 自测全部通过（4 组断言场景）')
