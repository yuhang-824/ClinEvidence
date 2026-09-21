import assert from 'node:assert/strict'
import test from 'node:test'

import { groupKnowledgeChunks } from '../../src/utils/kbResultGroups.js'

test('同名文件按知识库和文件身份分别聚合', () => {
  const groups = groupKnowledgeChunks([
    { kb_id: 'kb-1', file_id: 'file-1', content: 'A', metadata: { source: 'guide.md' } },
    { kb_id: 'kb-2', file_id: 'file-2', content: 'B', metadata: { source: 'guide.md' } },
    { kb_id: 'kb-1', file_id: 'file-1', content: 'C', metadata: { source: 'guide.md' } }
  ])

  assert.equal(groups.length, 2)
  assert.deepEqual(
    groups.map((group) => [group.kb_id, group.file_id, group.chunks.length]),
    [
      ['kb-1', 'file-1', 2],
      ['kb-2', 'file-2', 1]
    ]
  )
})

test('文件按最高相关度排序，缺失分数的文件排在最后', () => {
  const groups = groupKnowledgeChunks([
    { kb_id: 'kb-1', file_id: 'file-a', score: 0.42, metadata: { source: 'b-guide.md' } },
    { kb_id: 'kb-1', file_id: 'file-b', score: 0.88, metadata: { source: 'a-guide.md' } },
    { kb_id: 'kb-1', file_id: 'file-b', score: 0.55, metadata: { source: 'a-guide.md' } },
    { kb_id: 'kb-1', file_id: 'file-c', metadata: { source: 'c-guide.md' } }
  ])

  assert.deepEqual(
    groups.map((group) => group.filename),
    ['a-guide.md', 'b-guide.md', 'c-guide.md']
  )
  // 同文件内保持传入的检索顺序
  assert.deepEqual(
    groups[0].chunks.map((chunk) => chunk.score),
    [0.88, 0.55]
  )
})

test('同分文件退回按文件名排序，结果稳定', () => {
  const groups = groupKnowledgeChunks([
    { kb_id: 'kb-1', file_id: 'file-b', score: 0.7, metadata: { source: 'b.md' } },
    { kb_id: 'kb-1', file_id: 'file-a', score: 0.7, metadata: { source: 'a.md' } }
  ])

  assert.deepEqual(
    groups.map((group) => group.filename),
    ['a.md', 'b.md']
  )
})

test('两组都缺分数时退回文件名排序', () => {
  const groups = groupKnowledgeChunks([
    { kb_id: 'kb-1', file_id: 'file-z', metadata: { source: 'zz-无分数.md' } },
    { kb_id: 'kb-1', file_id: 'file-a', metadata: { source: 'aa-无分数.md' } }
  ])

  assert.deepEqual(
    groups.map((group) => group.filename),
    ['aa-无分数.md', 'zz-无分数.md']
  )
})

test('未启用重排的知识库不会因为缺重排分而整体沉底', () => {
  const groups = groupKnowledgeChunks([
    // kb-1 开了重排：重排分高于向量分，但另一个库没有重排分
    { kb_id: 'kb-1', file_id: 'file-1', content: 'A', score: 0.3, rerank_score: 0.2, metadata: { source: 'a.md' } },
    // kb-2 没开重排：只有向量分，必须用自己的分数参与排序而不是被判为缺失
    { kb_id: 'kb-2', file_id: 'file-2', content: 'B', score: 0.9, metadata: { source: 'b.md' } }
  ])

  assert.deepEqual(
    groups.map((group) => group.filename),
    ['b.md', 'a.md']
  )
})

test('分组带上 metadata 里的知识库与文件标识，打开原文的入口才出现', () => {
  // 归一化后的片段只有 metadata 里有 kb_id/file_id；丢了它们，依赖分组的"查看完整文件"不会渲染
  const groups = groupKnowledgeChunks([
    { content: 'A', metadata: { source: 'a.md', kb_id: 'kb-1', file_id: 'file-1' } },
    { content: 'B', metadata: { source: 'a.md', kb_id: 'kb-1', file_id: 'file-1' } }
  ])

  assert.equal(groups.length, 1)
  assert.equal(groups[0].kb_id, 'kb-1')
  assert.equal(groups[0].file_id, 'file-1')
})
