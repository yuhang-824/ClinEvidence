/**
 * 回答引用标记的解析与证据匹配。
 *
 * 模型按 knowledge-base Skill 的约定在论断句末输出：
 * <cite source="文件名.pdf" data-page="12" type="file">1</cite>
 * 这里只做纯数据处理，DOM 读写留在 MarkdownPreview 与消息组件里。
 */

const CITE_TAG_PREFIXES = ['<cite', '</cite']

/** 取片段声明的 PDF 页码；旧片段没有审核版本时返回空数组。 */
export function chunkPages(chunk) {
  const pages = chunk?.metadata?.source_metadata?.pages
  if (!Array.isArray(pages)) return []
  return pages.map((page) => Number(page)).filter((page) => Number.isFinite(page))
}

/**
 * 把引用标记匹配到本次回答检索到的片段。
 *
 * 返回 null 表示这条引用在检索结果里没有对应片段（文件名不存在），
 * pageMatched 为假表示文件名存在但没有片段覆盖该页码：两种情况都不应当作证据展示。
 */
export function resolveCitation(chunks, citation) {
  const source = String(citation?.source || '').trim()
  const list = Array.isArray(chunks) ? chunks : []
  if (!source || list.length === 0) return null

  const sameFile = list.filter((chunk) => String(chunk?.metadata?.source || '').trim() === source)
  if (sameFile.length === 0) return null

  const page = Number(citation?.page)
  if (Number.isFinite(page)) {
    const onPage = sameFile.find((chunk) => chunkPages(chunk).includes(page))
    if (onPage) return { chunk: onPage, page, pageMatched: true }
  }
  return { chunk: sameFile[0], page: Number.isFinite(page) ? page : null, pageMatched: false }
}

/** 从引用元素上取标记字段；page 缺失时为 null。 */
export function citationFromAttributes(attributes) {
  const source = String(attributes?.source || '').trim()
  if (!source) return null
  const page = Number(attributes?.page)
  return { source, page: Number.isFinite(page) && page > 0 ? page : null }
}

/**
 * 截掉内容末尾半截的引用标签。
 *
 * 流式输出按字素切片，`<cite source="...` 会短暂显示成字面 HTML；渲染前丢掉这个尾巴，
 * 标签完整时原样返回（已闭合的标签交给渲染管道处理）。
 */
export function stripIncompleteCitation(text) {
  const value = String(text || '')
  const lastOpen = value.lastIndexOf('<')
  if (lastOpen === -1) return value
  const tail = value.slice(lastOpen)
  if (tail.includes('>')) return value
  const lowered = tail.toLowerCase()
  // 半截标签有两种形态：标签名还没写完（`<cit`），或标签名已完整但属性/尖括号没到（`<cite source="x"`）。
  // 单独一个 `<` 是正文里的比较符号，不算半截标签。
  const isCiteFragment =
    CITE_TAG_PREFIXES.some((prefix) => lowered.startsWith(prefix)) ||
    (lowered.length >= 2 && CITE_TAG_PREFIXES.some((prefix) => prefix.startsWith(lowered)))
  return isCiteFragment ? value.slice(0, lastOpen) : value
}
