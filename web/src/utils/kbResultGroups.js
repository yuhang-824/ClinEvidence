/** 按知识库文件身份聚合检索片段；文件按最高相关度排序，便于判断召回质量。 */
export function groupKnowledgeChunks(chunks) {
  const groups = new Map()

  for (const item of chunks) {
    const filename = item?.metadata?.source || '未知来源'
    const kbId = item?.kb_id || ''
    const fileId = item?.file_id || ''
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

  // 片段保持传入顺序（检索相关度顺序），文件取各自最高分参与排序
  const bestScore = (group) => {
    const scores = group.chunks
      .map((chunk) => chunk?.score)
      .filter((score) => typeof score === 'number' && Number.isFinite(score))
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
