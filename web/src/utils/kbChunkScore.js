/**
 * 片段相关度的排序依据与分数标注。
 *
 * 检索结果里 `score` 是向量/混合分，`rerank_score` 是重排分；启用重排时列表顺序由重排分
 * 决定，展示与排序都应以它为准，否则用户读到的"相关度降序"不是生成回答时的排序。
 * 排序依据按整组判定：同一组里逐条各取一种分数会混用两种量纲，把顺序排乱。
 * 重排是每个知识库自己的查询参数（`use_reranker`），所以"组"应当是同一个知识库的结果，
 * 而不是一次回答合并后的全部片段——跨库统一判定会让没开重排的库整体失去可比分数。
 */

/** 该组片段是否按重排分排序（重排作用于整组结果，不会只覆盖一部分）。 */
export function usesRerankScore(chunks) {
  return Array.isArray(chunks) && chunks.some((chunk) => typeof chunk?.rerank_score === 'number')
}

/** 取该片段在给定排序依据下的分数；缺失返回 null（排序时排最后）。 */
export function effectiveScore(chunk, useRerank = false) {
  const value = useRerank ? chunk?.rerank_score : chunk?.score
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** 按该组生效的相关度降序返回新数组，不修改入参。 */
export function sortChunksByScore(chunks) {
  const list = Array.isArray(chunks) ? [...chunks] : []
  const useRerank = usesRerankScore(list)
  const rank = (chunk) => {
    const score = effectiveScore(chunk, useRerank)
    return score === null ? Number.NEGATIVE_INFINITY : score
  }
  return list.sort((a, b) => rank(b) - rank(a))
}

/** 列表按哪种分数排序，用于给列表加标注；没有任何分数时返回空串（列表没按分数排）。 */
export function scoreSourceLabel(chunks) {
  const list = Array.isArray(chunks) ? chunks : []
  const useRerank = usesRerankScore(list)
  if (!list.some((chunk) => effectiveScore(chunk, useRerank) !== null)) return ''
  return useRerank ? '重排分' : '相似度'
}

/**
 * 合并多个来源的片段并保持各来源自己的相关度顺序。
 *
 * 每个知识库各自降序后按来源首次出现的次序拼接：跨库比大小既没有共同量纲，也会让
 * 未启用重排的知识库因为缺 `rerank_score` 而整体沉底。
 * 来源标识取自 `metadata.kb_id`：归一化后的片段只把它放在 metadata 里，没有顶层字段，
 * 只看顶层会让所有片段落进同一个桶，这个函数就退化成"整条列表统一判定"。
 */
export function sourceKey(chunk) {
  return chunk?.kb_id || chunk?.metadata?.kb_id || ''
}

export function sortChunksBySource(chunks) {
  const list = Array.isArray(chunks) ? chunks : []
  const buckets = new Map()
  for (const chunk of list) {
    const key = sourceKey(chunk)
    if (!buckets.has(key)) buckets.set(key, [])
    buckets.get(key).push(chunk)
  }
  return [...buckets.values()].flatMap((bucket) => sortChunksByScore(bucket))
}
