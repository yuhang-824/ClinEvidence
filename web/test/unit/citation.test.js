import assert from 'node:assert/strict'
import test from 'node:test'

import { chunkPages, citationFromAttributes, resolveCitation, stripIncompleteCitation } from '../../src/utils/citation.js'
import {
  effectiveScore,
  scoreSourceLabel,
  sortChunksByScore,
  sortChunksBySource,
  usesRerankScore
} from '../../src/utils/kbChunkScore.js'
import { MessageProcessor } from '../../src/utils/messageProcessor.js'
import { createMarkdownRenderer } from '../../src/utils/markdown_preview.js'

const chunk = (file, pages, content = '片断正文') => ({
  kb_name: '指南',
  content,
  score: 0.8,
  metadata: {
    source: file,
    file_id: 'file_1',
    kb_id: 'kb_1',
    chunk_id: `${file}-${pages.join('_')}`,
    source_metadata: { pages, section: ['3 治疗'], revision: 2 }
  }
})

test('引用标记按文件名与页码匹配检索片段', () => {
  const chunks = [chunk('指南.pdf', [12, 13]), chunk('共识.pdf', [5])]

  const matched = resolveCitation(chunks, { source: '指南.pdf', page: 13 })
  assert.equal(matched.chunk.content, '片断正文')
  assert.equal(matched.page, 13)
  assert.equal(matched.pageMatched, true)

  // 只给文件名：仍能定位到该文件的片段，但不声称页码已核对
  const fileOnly = resolveCitation(chunks, { source: '共识.pdf', page: null })
  assert.equal(fileOnly.chunk.metadata.source, '共识.pdf')
  assert.equal(fileOnly.pageMatched, false)
})

test('伪造的文件名或页码不会被当作证据', () => {
  const chunks = [chunk('指南.pdf', [12])]

  // 文件名不存在：没有对应片段
  assert.equal(resolveCitation(chunks, { source: '不存在的文件.pdf', page: 12 }), null)
  // 文件名存在但该页码不在任何片段里：返回片段但标记页码未命中
  const wrongPage = resolveCitation(chunks, { source: '指南.pdf', page: 99 })
  assert.equal(wrongPage.pageMatched, false)
  assert.equal(wrongPage.page, 99)
  // 空候选与缺 source 都不得命中
  assert.equal(resolveCitation([], { source: '指南.pdf', page: 12 }), null)
  assert.equal(resolveCitation(chunks, { source: '', page: 12 }), null)
})

test('引用元素的来源与页码解析', () => {
  assert.deepEqual(citationFromAttributes({ source: '指南.pdf', page: '12' }), { source: '指南.pdf', page: 12 })
  assert.deepEqual(citationFromAttributes({ source: '指南.pdf' }), { source: '指南.pdf', page: null })
  // 页码缺失或非法时只保留文件名，不冒充已核对的页码
  assert.equal(citationFromAttributes({ page: '12' }), null)
  assert.deepEqual(citationFromAttributes({ source: '指南.pdf', page: 'abc' }), { source: '指南.pdf', page: null })
  assert.deepEqual(citationFromAttributes({ source: '指南.pdf', page: '0' }), { source: '指南.pdf', page: null })
  assert.deepEqual(chunkPages(chunk('指南.pdf', [12, 13])), [12, 13])
  assert.deepEqual(chunkPages({ metadata: {} }), [])
})

test('流式半截引用标签不会显示成字面文本', () => {
  assert.equal(stripIncompleteCitation('结论如下<cite source="指南.pdf"'), '结论如下')
  assert.equal(stripIncompleteCitation('结论如下<cit'), '结论如下')
  assert.equal(stripIncompleteCitation('结论如下</cite'), '结论如下')
  assert.equal(stripIncompleteCitation('结论如下</cit'), '结论如下')
  // 完整标签与普通文本原样保留
  const complete = '结论<cite source="指南.pdf" data-page="12" type="file">1</cite>'
  assert.equal(stripIncompleteCitation(complete), complete)
  assert.equal(stripIncompleteCitation('正文 < 3 且 > 1'), '正文 < 3 且 > 1')
})

test('引用元素进入渲染结果并保留来源与页码属性', () => {
  const renderer = createMarkdownRenderer({ themeName: 'github-light', highlighter: null })
  const html = renderer.render('筛查建议<cite source="指南.pdf" data-page="12" type="file">1</cite>。')

  assert.match(html, /<cite source="指南\.pdf" data-page="12" type="file">1<\/cite>/)
})

test('来源归一化保留页码与章节，供引用与来源面板使用', () => {
  const conv = {
    messages: [
      {
        type: 'ai',
        tool_calls: [
          {
            name: 'query_kb',
            tool_call_result: {
              content: JSON.stringify({
                kb_id: 'kb_1',
                results: [
                  {
                    id: 'chunk_1',
                    kb_id: 'kb_1',
                    file_id: 'file_1',
                    content: '片断正文',
                    metadata: {
                      source: '指南.pdf',
                      file_id: 'file_1',
                      chunk_id: 'chunk_1',
                      chunk_index: 0,
                      source_metadata: { pages: [12], section: ['3 治疗'], revision: 2 }
                    }
                  }
                ]
              })
            }
          }
        ]
      }
    ]
  }

  const chunks = MessageProcessor.extractKnowledgeChunksFromConversation(conv, [
    { kb_id: 'kb_1', name: '指南库' }
  ])

  assert.equal(chunks.length, 1)
  assert.equal(chunks[0].kb_name, '指南库')
  assert.equal(chunks[0].metadata.kb_id, 'kb_1')
  assert.deepEqual(chunks[0].metadata.source_metadata.pages, [12])
  // 归一化后的片段必须能直接支撑引用解析
  assert.equal(resolveCitation(chunks, { source: '指南.pdf', page: 12 }).pageMatched, true)
})

test('来源归一化补齐相似度与重排分，来源列表才能按相关度排序', () => {
  const conv = {
    messages: [
      {
        type: 'ai',
        tool_calls: [
          {
            name: 'query_kb',
            tool_call_result: {
              content: JSON.stringify({
                kb_id: 'kb_1',
                results: [
                  {
                    id: 'weak',
                    kb_id: 'kb_1',
                    file_id: 'file_1',
                    content: '相关性较低的片段',
                    metadata: { source: '指南.pdf', chunk_id: 'weak', score: 0.31 }
                  },
                  {
                    id: 'strong',
                    kb_id: 'kb_1',
                    file_id: 'file_1',
                    content: '相关性最高的片段',
                    metadata: { source: '指南.pdf', chunk_id: 'strong', score: 0.87, rerank_score: 0.12 }
                  }
                ]
              })
            }
          }
        ]
      }
    ]
  }

  const chunks = MessageProcessor.extractKnowledgeChunksFromConversation(conv, [])
  // 分数在内置工具结果的 metadata 里，归一化必须提到顶层，并据此降序
  assert.deepEqual(
    chunks.map((chunk) => [chunk.content, chunk.score, chunk.rerank_score ?? null]),
    [
      ['相关性最高的片段', 0.87, 0.12],
      ['相关性较低的片段', 0.31, null]
    ]
  )
  assert.equal(chunks[0].metadata.score, 0.87)
  assert.equal(chunks[0].metadata.rerank_score, 0.12)
})

test('正文里的比较符号不会被当成半截标签吃掉', () => {
  // 单独一个 `<` 是正文内容，不是流式半截标签
  assert.equal(stripIncompleteCitation('条件 a < b 且 c <'), '条件 a < b 且 c <')
  assert.equal(stripIncompleteCitation('剂量 < 5mg'), '剂量 < 5mg')
  // 真正的半截标签仍然要被截掉
  assert.equal(stripIncompleteCitation('结论<cit'), '结论')
  assert.equal(stripIncompleteCitation('结论<cite source="指南.pdf"'), '结论')
})

test('启用重排时列表按重排分排序，与生成回答时的顺序一致', () => {
  const conv = {
    messages: [
      {
        type: 'ai',
        tool_calls: [
          {
            name: 'query_kb',
            tool_call_result: {
              content: JSON.stringify({
                kb_id: 'kb_1',
                results: [
                  {
                    id: 'vector-first',
                    kb_id: 'kb_1',
                    file_id: 'file_1',
                    content: '向量分高但重排分低',
                    metadata: { source: '指南.pdf', chunk_id: 'a', score: 0.92, rerank_score: 0.18 }
                  },
                  {
                    id: 'rerank-first',
                    kb_id: 'kb_1',
                    file_id: 'file_1',
                    content: '向量分低但重排分高',
                    metadata: { source: '指南.pdf', chunk_id: 'b', score: 0.55, rerank_score: 0.91 }
                  }
                ]
              })
            }
          }
        ]
      }
    ]
  }

  const chunks = MessageProcessor.extractKnowledgeChunksFromConversation(conv, [])
  // 排序依据是重排分：上面那条不会因为向量分更高而排前面
  assert.deepEqual(
    chunks.map((chunk) => chunk.content),
    ['向量分低但重排分高', '向量分高但重排分低']
  )
  assert.deepEqual(
    chunks.map((chunk) => effectiveScore(chunk, usesRerankScore(chunks))),
    [0.91, 0.18]
  )
  assert.equal(scoreSourceLabel(chunks), '重排分')
})

test('只有部分片段带重排分时整组按重排分排序，不混用两种量纲', () => {
  const mixed = [
    { content: '只有向量分，分数更高', score: 0.95 },
    { content: '带重排分，向量分低', score: 0.2, rerank_score: 0.4 },
    { content: '只有向量分，分数更低', score: 0.1 }
  ]

  // 有重排分就整组按重排分：缺分的排最后，不会拿向量分与重排分直接比大小
  assert.equal(usesRerankScore(mixed), true)
  assert.equal(scoreSourceLabel(mixed), '重排分')
  assert.deepEqual(
    sortChunksByScore(mixed).map((item) => item.content),
    ['带重排分，向量分低', '只有向量分，分数更高', '只有向量分，分数更低']
  )
  // 全组都没有重排分时才退回向量分
  const plain = [{ content: 'a', score: 0.1 }, { content: 'b', score: 0.9 }]
  assert.equal(scoreSourceLabel(plain), '相似度')
  assert.deepEqual(sortChunksByScore(plain).map((item) => item.content), ['b', 'a'])
  // 不修改入参
  assert.equal(mixed[0].content, '只有向量分，分数更高')
})

test('多个知识库合并时各按自己的依据排序，未开重排的库不被沉底', () => {
  // 普通库排在前面：它没有 rerank_score，若按"整条合并列表统一判定"就会被判为缺失分数
  // 而排到最后，与下面期望的顺序不同——这条用例因此能真的拦住那种实现
  const conv = {
    messages: [
      {
        type: 'ai',
        tool_calls: [
          {
            name: 'query_kb',
            tool_call_result: {
              content: JSON.stringify({
                kb_id: 'kb_plain',
                results: [
                  {
                    id: 'p1',
                    kb_id: 'kb_plain',
                    file_id: 'f2',
                    content: '普通库第一',
                    metadata: { source: 'p.pdf', score: 0.7 }
                  },
                  {
                    id: 'p2',
                    kb_id: 'kb_plain',
                    file_id: 'f2',
                    content: '普通库第二',
                    metadata: { source: 'p.pdf', score: 0.5 }
                  }
                ]
              })
            }
          },
          {
            name: 'query_kb',
            tool_call_result: {
              content: JSON.stringify({
                kb_id: 'kb_rerank',
                results: [
                  {
                    id: 'r-low',
                    kb_id: 'kb_rerank',
                    file_id: 'f1',
                    content: '重排低',
                    metadata: { source: 'r.pdf', score: 0.9, rerank_score: 0.1 }
                  },
                  {
                    id: 'r-high',
                    kb_id: 'kb_rerank',
                    file_id: 'f1',
                    content: '重排高',
                    metadata: { source: 'r.pdf', score: 0.2, rerank_score: 0.8 }
                  }
                ]
              })
            }
          }
        ]
      }
    ]
  }

  const chunks = MessageProcessor.extractKnowledgeChunksFromConversation(conv, [])
  // 归一化只把知识库标识放在 metadata 里，分桶必须认它，否则两库会并成一个桶
  assert.deepEqual(
    chunks.map((chunk) => chunk.kb_id ?? chunk.metadata.kb_id),
    ['kb_plain', 'kb_plain', 'kb_rerank', 'kb_rerank']
  )
  assert.deepEqual(
    chunks.map((chunk) => chunk.content),
    ['普通库第一', '普通库第二', '重排高', '重排低']
  )
  assert.deepEqual(
    sortChunksBySource(chunks).map((chunk) => chunk.content),
    ['普通库第一', '普通库第二', '重排高', '重排低']
  )
})

test('完全没有分数时不宣称按某种分数排序', () => {
  // 外部只读知识库的结果没有分数，列表保持检索返回顺序，标注不能凭空说"按相似度降序"
  assert.equal(scoreSourceLabel([{ score: null }, { score: null }]), '')
  assert.equal(scoreSourceLabel([]), '')
  assert.equal(scoreSourceLabel([{ score: 0 }]), '相似度')
})
