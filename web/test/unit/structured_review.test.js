import assert from 'node:assert/strict'
import test from 'node:test'
import { setImmediate } from 'node:timers'
import { readFileSync } from 'node:fs'
import { compileScript, parse } from 'vue/compiler-sfc'
import { createRenderer, h, nextTick, ref } from 'vue'
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
    // 空的补录文块与真实后端契约一致：不填内容不能保存
    all(root)
      .filter((n) => n.type === 'a-textarea' && n.props['aria-label'] === '文块修订内容')
      .at(-1)
      .props.onChange({ target: { value: '补录的内容' } })
    await nextTick()
    button('保存结构修订').props.onClick()
    assert.deepEqual(
      saves[0].blocks.slice(0, 2).map((b) => b.text),
      ['乙', '修订甲']
    )
    assert.match(saves[0].blocks[2].id, /^new:/)
    assert.equal(saves[0].blocks[2].text, '补录的内容')
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

test('保存前校验指出缺说明或空文块；审核校验列出需说明的页', async () => {
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
      saves = []
    const editorRef = ref(null)
    const structure = {
      pages: [
        {
          page: 1,
          width: 100,
          height: 100,
          checks: { reading_order: true, text_complete: true, relationships: true },
          note: '',
          issues: []
        }
      ],
      blocks: [
        {
          id: 'b0',
          page: 1,
          kind: 'paragraph',
          text: '',
          source_text: '',
          excluded: true,
          note: '',
          bbox: [0, 0, 10, 10]
        },
        {
          id: 'b1',
          page: 1,
          kind: 'paragraph',
          text: '',
          source_text: '',
          excluded: false,
          note: '',
          bbox: [0, 0, 10, 10]
        },
        {
          id: 'b2',
          page: 1,
          kind: 'table',
          text: '| a |',
          source_text: '| a |',
          excluded: false,
          note: '',
          bbox: [0, 0, 10, 10]
        }
      ]
    }
    app = renderer.createApp(() =>
      h(Panel, {
        ref: editorRef,
        structure,
        kbId: 'kb',
        fileId: 'f',
        version: 1,
        editable: true,
        onSave: (v) => saves.push(v)
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
    const lastAlert = () =>
      all(root)
        .filter((n) => n.type === 'a-alert' && n.props.message)
        .map((n) => n.props.message)
        .at(-1)
    await new Promise((r) => setImmediate(r))
    await nextTick()

    // 保存拦截：排除文块缺说明 + 未排除文块为空，一次指出全部问题且不发请求
    button('保存结构修订').props.onClick()
    await nextTick()
    assert.deepEqual(saves, [])
    const message = lastAlert()
    assert.match(message, /第 1 页文块 1 已勾选「不参与检索」，请填写文块修订或排除说明/)
    assert.match(message, /第 1 页文块 2 未排除且内容为空，请补充原文或勾选「不参与检索」/)

    // 补齐说明与正文后保存放行
    all(root)
      .filter((n) => n.type === 'a-input')[0]
      .props.onChange({ target: { value: '页码噪声，排除' } })
    all(root)
      .filter((n) => n.type === 'a-textarea' && n.props['aria-label'] === '文块修订内容')[1]
      .props.onChange({ target: { value: '补充的正文' } })
    await nextTick()
    button('保存结构修订').props.onClick()
    await nextTick()
    assert.equal(saves.length, 1)
    assert.equal(lastAlert(), undefined)

    // 审核范围：编辑清空了本页核验勾选，先指出未完成核验
    const problems = editorRef.value?.validate?.('approve') || []
    assert.deepEqual(problems, ['第 1 页尚未完成阅读顺序、完整性和对应关系核验'])

    // 重新勾选三项核验后，指出含排除文块与表格的页必须填写核验说明
    all(root)
      .filter((n) => n.type === 'a-checkbox')
      .slice(-3)
      .forEach((n) => n.props['onUpdate:checked'](true))
    await nextTick()
    assert.deepEqual(editorRef.value?.validate?.('approve'), [
      '第 1 页需要填写表格、图示或异常核验说明'
    ])

    // 填写本页核验说明后审核校验通过
    all(root)
      .filter(
        (n) => n.type === 'a-textarea' && String(n.props.placeholder || '').startsWith('核验说明')
      )
      .at(-1)
      .props['onUpdate:value']('已核对页码噪声并排除')
    await nextTick()
    assert.deepEqual(editorRef.value?.validate?.('approve'), [])
  } finally {
    app?.unmount()
    await server.close()
  }
})
