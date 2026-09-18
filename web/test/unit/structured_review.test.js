import assert from 'node:assert/strict'
import test from 'node:test'
import { setImmediate } from 'node:timers'
import { readFileSync } from 'node:fs'
import { compileScript, parse } from 'vue/compiler-sfc'
import { createRenderer, h, nextTick } from 'vue'
import { createServer } from 'vite'

test('原页加载后核验；编辑清除勾选；排序和补录保存为结构字段', async () => {
  const source = readFileSync(
    new URL('../../src/components/knowledge/StructuredDocumentReview.vue', import.meta.url),
    'utf8'
  )
  const server = await createServer({
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true, hmr: false },
    appType: 'custom',
    plugins: [
      {
        name: 'structured-review-test',
        resolveId(id) {
          if (id === 'virtual:structure') return '\0structure'
        },
        load(id) {
          if (id !== '\0structure') return
          return compileScript(parse(source).descriptor, {
            id: 'structure',
            inlineTemplate: true
          }).content.replace(
            /import \{ documentApi \} from '@\/apis\/knowledge_api'/,
            'const documentApi = { getReviewSource: async () => new Blob(["fixture"]) }'
          )
        }
      }
    ]
  })
  const node = (type) => ({ type, children: [], props: {}, parent: null })
  const renderer = createRenderer({
    createElement: node,
    createText: (text) => ({ ...node('text'), text }),
    createComment: () => node('comment'),
    insert(child, parent, anchor) {
      if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1)
      child.parent = parent
      const i = anchor ? parent.children.indexOf(anchor) : -1
      if (i < 0) parent.children.push(child)
      else parent.children.splice(i, 0, child)
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
  let app
  try {
    const { default: Panel } = await server.ssrLoadModule('virtual:structure')
    const root = node('root'),
      saves = [],
      dirties = []
    const checks = { reading_order: true, text_complete: true, relationships: true }
    const structure = {
      pages: [{ page: 1, width: 100, height: 100, checks, note: 'checked' }],
      blocks: ['甲', '乙'].map((text, i) => ({
        id: 'b' + i,
        page: 1,
        kind: 'paragraph',
        text,
        source_text: text,
        bbox: [0, 0, 10, 10]
      }))
    }
    app = renderer.createApp(() =>
      h(Panel, {
        structure,
        kbId: 'kb',
        fileId: 'f',
        version: 1,
        editable: true,
        onSave: (v) => saves.push(JSON.parse(JSON.stringify(v))),
        onDirtyChange: (v) => dirties.push(v)
      })
    )
    for (const name of [
      'a-button',
      'a-select',
      'a-checkbox',
      'a-input',
      'a-textarea',
      'a-input-number',
      'a-alert',
      'a-spin'
    ]) {
      app.component(name, {
        inheritAttrs: false,
        setup(_, { attrs, slots }) {
          return () => h(name, attrs, slots.default?.())
        }
      })
    }
    app.mount(root)
    const all = (item) => [item, ...item.children.flatMap(all)]
    const text = (item) => (item.text || '') + item.children.map(text).join('')
    const button = (label) => all(root).find((n) => n.type === 'a-button' && text(n) === label)
    await new Promise((r) => setImmediate(r))
    await nextTick()
    assert.equal(
      all(root)
        .filter((n) => n.type === 'a-checkbox')
        .at(-1).props.disabled,
      false
    )
    all(root)
      .find((n) => n.props['aria-label'] === '文块修订内容')
      .props.onChange({ target: { value: '修订甲' } })
    await nextTick()
    assert.equal(dirties.at(-1), true)
    assert.equal(button('保存结构修订').props.disabled, false)
    assert.ok(
      all(root)
        .filter((n) => n.type === 'a-checkbox')
        .slice(-3)
        .every((n) => n.props.checked === false)
    )
    button('下移').props.onClick()
    await nextTick()
    button('补录遗漏文块').props.onClick()
    await nextTick()
    button('保存结构修订').props.onClick()
    assert.deepEqual(
      saves[0].blocks.slice(0, 2).map((b) => b.text),
      ['乙', '修订甲']
    )
    assert.match(saves[0].blocks[2].id, /^new:/)
    assert.equal('bbox' in saves[0].blocks[0], false)
    assert.equal(structure.blocks[0].text, '甲')
    assert.deepEqual(saves[0].pages[0].checks, {
      reading_order: false,
      text_complete: false,
      relationships: false
    })
  } finally {
    app?.unmount()
    await server.close()
  }
})
