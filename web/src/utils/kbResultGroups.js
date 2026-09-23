import { effectiveScore, sourceKey, usesFusionScore, usesRerankScore } from './kbChunkScore.js'

/** 按知识库文件身份聚合检索片段；文件按最高相关度排序，便于判断召回质量。 */
export function groupKnowledgeChunks(chunks) {
  const groups = new Map()

  for (const item of chunks) {
    const filename = item?.metadata?.source || '未知来源'
    // 归一化后的片段把知识库标识放在 metadata 里；只看顶层会让分组携带空 kb_id，
    // 依赖它打开原文的入口（"查看完整文件"）就永远不会出现
    const kbId = sourceKey(item)
    const fileId = item?.file_id || item?.metadata?.file_id || ''
    const key = `${kbId}\u0000${fileId}\u0000${filename}`

    if (!groups.has(key)) {
      groups.set(key, {
        key,
        filename,
        kb_id: kbId,
        file_id: fileId,
        chunks: []
      })
    }
    groups.get(key).chunks.push(item)
  }

  // 片段保持传入顺序（检索相关度顺序），文件取各自最高分参与排序；
  // 排序依据按组判定：同一文件属于同一个知识库，重排与混合检索是逐库的查询参数，
  // 所以逐组取分数，跨库不按同一口径比较（否则没开重排或混合检索的库会整体拿到缺失分数）
  const bestScore = (group) => {
    const useRerank = usesRerankScore(group.chunks)
    const useFusion = !useRerank && usesFusionScore(group.chunks)
    const scores = group.chunks
      .map((chunk) => effectiveScore(chunk, useRerank, useFusion))
      .filter((score) => score !== null)
    return scores.length ? Math.max(...scores) : Number.NEGATIVE_INFINITY
  }

  return Array.from(groups.values()).sort((a, b) => {
    const aScore = bestScore(a)
    const bScore = bestScore(b)
    // 两组都没有分数时不能做差（NaN 会破坏比较器），退回文件名排序
    if (aScore === bScore) return a.filename.localeCompare(b.filename)
    return bScore - aScore
  })
}
