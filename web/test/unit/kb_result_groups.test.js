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
