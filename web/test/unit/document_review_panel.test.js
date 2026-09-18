import assert from 'node:assert/strict'
import test from 'node:test'
import { setImmediate } from 'node:timers'
import { readFileSync } from 'node:fs'
import { compileScript, parse } from 'vue/compiler-sfc'
import { createRenderer, h, nextTick } from 'vue'
import { createServer } from 'vite'

test('未保存修订不能审核或入库；保存后审核最新版本；失败保留草稿', async () => {
  const calls = []
  let fail = false
  let record = {
    file_status: 'parsed',
    can_manage: true,
    revisions: [
      {
        version: 1,
        content: 'cleaned',
        raw_content: 'raw',
        report: { changes: [], warnings: [] },
        approved_at: null
      }
    ]
  }
  globalThis.__reviewApi = {
    getDocumentReview: async () => structuredClone(record),
    changeDocumentReview: async (_kb, _file, payload) => {
      calls.push(payload)
      if (fail) throw new Error('版本冲突')
      if (payload.action === 'save' || payload.action === 'boundaries')
        record.revisions.push({
          ...record.revisions.at(-1),
          version: record.revisions.at(-1).version + 1,
          content: payload.content ?? record.revisions.at(-1).content,
          approved_at: null,
          report:
            payload.action === 'boundaries'
              ? { chunk_boundaries: payload.boundaries === null ? null : [...payload.boundaries] }
              : {},
          raw_content: null
        })
      else record.revisions.at(-1).approved_at = '2026-09-18'
      return structuredClone(record)
    },
    previewDocumentChunks: async (_kb, _file, payload) => ({
      version: payload.version,
      chunks: [0, ...(record.revisions.at(-1).report.chunk_boundaries ?? [3])].map((start, i) => ({
        id: String(i),
        start_char_pos: start,
        content: record.revisions.at(-1).content.slice(start)
      })),
      params: { review_version: payload.version, chunk_preset_id: 'mixed' }
    }),
    indexDocuments: async (_kb, _file, params) => calls.push({ index: params })
  }
  const server = await createServer({
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true, hmr: false },
    appType: 'custom',
    plugins: [
      {
        name: 'review-component-test',
        resolveId(id) {
          if (id === 'virtual:review-test') return '\0' + id
          if (id === 'virtual:repair-editor') return '\0virtual:repair-editor'
        },
        load(id) {
          if (id === '\0virtual:repair-editor') {
            const source = readFileSync(
              new URL('../../src/components/knowledge/ChunkRepairEditor.vue', import.meta.url),
              'utf8'
            )
            return compileScript(parse(source).descriptor, {
              id: 'repair-editor',
              inlineTemplate: true
            }).content
          }
          if (id !== '\0virtual:review-test') return
          const source = readFileSync(
            new URL('../../src/components/knowledge/DocumentReviewPanel.vue', import.meta.url),
            'utf8'
          )
          return compileScript(parse(source).descriptor, {
            id: 'review-test',
            inlineTemplate: true
          })
            .content.replace(
              /from '@\/components\/knowledge\/ChunkRepairEditor.vue'/,
              "from 'virtual:repair-editor'"
            )
            .replace(
              /import \{ message \} from 'ant-design-vue'/,
              'const message = { success() {} }'
            )
            .replace(
              /import \{ documentApi \} from '@\/apis\/knowledge_api'/,
              'const documentApi = globalThis.__reviewApi'
            )
            .replace(
              /import SourceChunkCard from '[^']+'/,
              'const SourceChunkCard = { render() { return null } }'
            )
        }
      }
    ]
  })
  let app
  try {
    const { default: Panel } = await server.ssrLoadModule('virtual:review-test')
    const node = (type) => ({ type, children: [], props: {}, parent: null })
    const renderer = createRenderer({
      createElement: node,
      createText: (text) => ({ ...node('text'), text }),
      createComment: () => node('comment'),
      insert(child, parent, anchor = null) {
        if (child.parent) {
          const old = child.parent.children.indexOf(child)
          if (old >= 0) child.parent.children.splice(old, 1)
        }
        child.parent = parent
        const index = anchor ? parent.children.indexOf(anchor) : -1
        if (index >= 0) parent.children.splice(index, 0, child)
        else parent.children.push(child)
      },
      remove(child) {
        child.parent.children.splice(child.parent.children.indexOf(child), 1)
      },
      setText(child, text) {
        child.text = text
      },
      setElementText(child, text) {
        child.text = text
        child.children = []
      },
      parentNode: (child) => child.parent,
      nextSibling: (child) =>
        child.parent?.children[child.parent.children.indexOf(child) + 1] || null,
      patchProp(child, key, _old, value) {
        child.props[key] = value
      }
    })
    const root = node('root')
    const find = (item, predicate) =>
      predicate(item) ? item : item.children.map((c) => find(c, predicate)).find(Boolean)
    const text = (item) => (item.text || '') + item.children.map(text).join('')
    const button = (label) => find(root, (item) => item.type === 'button' && text(item) === label)
    const flush = async () => {
      await new Promise((resolve) => setImmediate(resolve))
      await nextTick()
    }
    app = renderer.createApp(() => h(Panel, { kbId: 'kb', fileId: 'file' }))
    for (const name of [
      'a-spin',
      'a-alert',
      'a-select',
      'a-button',
      'a-radio-group',
      'a-radio-button',
      'a-textarea',
      'a-input-number',
      'a-pagination',
      'a-popconfirm'
    ]) {
      app.component(name, {
        setup:
          (_props, { attrs, slots }) =>
          () =>
            h(name === 'a-button' ? 'button' : name, attrs, slots.default?.())
      })
    }
    app.mount(root)
    await flush()
    const editor = () => find(root, (item) => item.type === 'a-textarea')
    editor().props['onUpdate:value']('edited')
    await flush()
    assert.equal(button('审核通过').props.disabled, true)
    assert.equal(button('切片入库').props.disabled, true)
    await button('保存修订').props.onClick()
    await flush()
    assert.equal(calls[0].content, 'edited')
    assert.equal(button('审核通过').props.disabled, false)
    await button('审核通过').props.onClick()
    await flush()
    assert.equal(calls[1].version, 2)
    assert.equal(button('切片入库').props.disabled, true)
    await button('预览切片').props.onClick()
    await flush()
    assert.equal(button('切片入库').props.disabled, false)
    const sizeInput = find(root, (item) => item.type === 'a-input-number')
    sizeInput.props['onUpdate:value'](128)
    await flush()
    assert.equal(button('切片入库').props.disabled, true)
    await button('预览切片').props.onClick()
    await flush()
    await button('切片入库').props.onClick()
    await flush()
    assert.equal(calls[2].index.review_version, 2)
    await button('预览切片').props.onClick()
    await flush()
    await button('修复切片').props.onClick()
    await flush()
    assert.equal(button('切片入库').props.disabled, true)
    assert.equal(button('在光标处拆分').props.disabled, true)
    const boundaryEditor = find(root, (item) => item.type === 'textarea')
    boundaryEditor.selectionStart = 1
    boundaryEditor.props.onClick()
    await flush()
    assert.equal(button('在光标处拆分').props.disabled, false)
    await button('在光标处拆分').props.onClick()
    await flush()
    assert.ok(text(root).includes('当前共 3 个片段'))
    await button('与下一片段合并').props.onClick()
    await flush()
    assert.ok(text(root).includes('当前共 2 个片段'))
    await button('与下一片段合并').props.onClick()
    await flush()
    fail = true
    await button('保存切分修订').props.onClick()
    await flush()
    assert.ok(text(root).includes('当前共 1 个片段'))
    assert.ok(find(root, (item) => item.props.message === '版本冲突'))
    fail = false
    await button('保存切分修订').props.onClick()
    await flush()
    assert.deepEqual(calls.at(-1).boundaries, [])
    assert.equal(button('切片入库').props.disabled, true)
    assert.equal(button('审核通过').props.disabled, false)
    fail = true
    editor().props['onUpdate:value']('keep my draft')
    await flush()
    await button('保存修订').props.onClick()
    await flush()
    assert.equal(editor().props.value, 'keep my draft')
    assert.ok(find(root, (item) => item.props.message === '版本冲突'))
  } finally {
    app?.unmount()
    await server.close()
    delete globalThis.__reviewApi
  }
})
