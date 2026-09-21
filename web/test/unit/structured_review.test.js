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
    assert.match(message, /第 1 页文块 2 未排除且没有正文，请补充原文或勾选「不参与检索」/)

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

test('保存跳页：定位下一张未核验的页，支持环形回绕', async () => {
  const source = readFileSync(
    new URL('../../src/components/knowledge/StructuredDocumentReview.vue', import.meta.url),
    'utf8'
  )
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom',
    plugins: [
      {
        name: 'structured-review-test',
        resolveId(id) {
          if (id === 'virtual:structure') return ' structure'
        },
        load(id) {
          if (id !== ' structure') return
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
  let root
  try {
    const { default: Panel } = await server.ssrLoadModule('virtual:structure')
    const editorRef = ref(null)
    const pending = { reading_order: false, text_complete: false, relationships: false }
    const done = { reading_order: true, text_complete: true, relationships: true }
    const structure = {
      pages: [
        { page: 1, width: 100, height: 100, checks: { ...pending }, note: '' },
        { page: 2, width: 100, height: 100, checks: { ...done }, note: '已核验' },
        { page: 3, width: 100, height: 100, checks: { ...pending }, note: '' }
      ],
      blocks: [
        { id: 'b1', page: 1, kind: 'paragraph', text: '第一页', source_text: '第一页', bbox: [0, 0, 1, 1] },
        { id: 'b2', page: 2, kind: 'paragraph', text: '第二页', source_text: '第二页', bbox: [0, 0, 1, 1] },
        { id: 'b3', page: 3, kind: 'paragraph', text: '第三页', source_text: '第三页', bbox: [0, 0, 1, 1] }
      ]
    }
    root = node('root')
    app = renderer.createApp(() =>
      h(Panel, { ref: editorRef, structure, kbId: 'kb', fileId: 'f', version: 1, editable: true })
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
    const pageCheckboxes = () => all(root).filter((n) => n.type === 'a-checkbox').slice(-3)
    await new Promise((r) => setImmediate(r))
    const editor = () => editorRef.value

    // 当前页未核验：停留在本页等待完成
    assert.equal(editor().getCurrentPage(), 1)
    editor().goToNextUnreviewedPage(1)
    assert.equal(editor().getCurrentPage(), 1)

    // 勾选第 1 页三项核验后：跳过已核验的第 2 页，落到第 3 页
    pageCheckboxes().forEach((n) => n.props['onUpdate:checked'](true))
    await nextTick()
    editor().goToNextUnreviewedPage(1)
    assert.equal(editor().getCurrentPage(), 3)

    // 第 3 页未核验：停留
    editor().goToNextUnreviewedPage(3)
    assert.equal(editor().getCurrentPage(), 3)

    // 勾选第 3 页核验后：全部页面已核验，环形查找无未核验页，停留原页
    pageCheckboxes().forEach((n) => n.props['onUpdate:checked'](true))
    await nextTick()
    const jumped = editor().goToNextUnreviewedPage(3)
    assert.equal(jumped, null)
    assert.equal(editor().getCurrentPage(), 3)
  } finally {
    app?.unmount()
    await server.close()
  }
})

test('机器核验页免人工核验：校验与跳页跳过，页面说明或编辑可接管', async () => {
  const source = readFileSync(
    new URL('../../src/components/knowledge/StructuredDocumentReview.vue', import.meta.url),
    'utf8'
  )
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom',
    plugins: [
      {
        name: 'structured-review-auto-test',
        resolveId(id) {
          if (id === 'virtual:structure-auto') return '\0structure-auto'
        },
        load(id) {
          if (id !== '\0structure-auto') return
          return compileScript(parse(source).descriptor, {
            id: 'structure-auto',
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
  let root
  try {
    const { default: Panel } = await server.ssrLoadModule('virtual:structure-auto')
    const editorRef = ref(null)
    const pending = { reading_order: false, text_complete: false, relationships: false }
    const header = {
      id: 'hdr',
      page: 1,
      kind: 'paragraph',
      text: '期刊名',
      source_text: '期刊名',
      bbox: [0, 0, 1, 1],
      excluded: true,
      note: '解析器判定为页眉/页脚或噪声，请人工确认排除'
    }
    const structure = {
      pages: [
        {
          page: 1,
          width: 100,
          height: 100,
          checks: { ...pending },
          note: '',
          issues: ['存在空结构块，请对照原文检查'],
          auto_review: { rule: 'no-anomaly/v1', parser: 'docling', parser_version: 'v1' },
          native_text_check: { characters: 1000, absent_characters: 1, absent_sample: '缺', coverage: 0.999 }
        },
        {
          page: 2,
          width: 100,
          height: 100,
          checks: { ...pending },
          note: '',
          issues: ['独立文本层比对：40/100 个字符未出现在解析结果中']
        },
        {
          page: 3,
          width: 100,
          height: 100,
          checks: { ...pending },
          note: '',
          auto_review: { rule: 'blank-page/v1', parser: 'docling', parser_version: 'v1' }
        }
      ],
      blocks: [
        { ...header },
        { id: 'b1', page: 1, kind: 'paragraph', text: '第一页', source_text: '第一页', bbox: [0, 0, 1, 1] },
        { id: 'b2', page: 2, kind: 'paragraph', text: '第二页', source_text: '第二页', bbox: [0, 0, 1, 1] },
        { ...header, id: 'hdr3', page: 3 }
      ]
    }
    root = node('root')
    app = renderer.createApp(() =>
      h(Panel, { ref: editorRef, structure, kbId: 'kb', fileId: 'f', version: 1, editable: true })
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
    const textOf = (item) => (item.text || '') + item.children.map(textOf).join('')
    const editor = () => editorRef.value
    const pageSelect = () => all(root).find((n) => n.type === 'a-select')
    const summary = () => textOf(all(root).find((n) => n.props?.class === 'review-summary')).trim()
    const pageChecks = () => all(root).filter((n) => n.type === 'a-checkbox').slice(-3)
    const pageNote = () =>
      all(root).find(
        (n) => n.type === 'a-textarea' && String(n.props?.placeholder || '').startsWith('核验说明')
      )
    const goBack = () => all(root).find((n) => n.type === 'a-button' && textOf(n) === '上一页')
    await new Promise((r) => setImmediate(r))

    // 机器核验页不计入待人工，页面标签与汇总区分两种核验来源
    assert.deepEqual(editor().validate('approve'), ['第 2 页尚未完成阅读顺序、完整性和对应关系核验'])
    assert.deepEqual(
      pageSelect().props.options.map((o) => o.label),
      ['第 1 页 · 机器核验', '第 2 页', '第 3 页 · 机器核验']
    )
    assert.equal(summary(), '机器核验 2 页 · 待人工 1 页')

    // 保存动作在本页核验下方：新审核动线结束于勾选与说明，不必回到顶部工具栏
    const saveButton = all(root).find((n) => n.type === 'a-button' && textOf(n) === '保存结构修订')
    const ancestors = []
    for (let cursor = saveButton.parent; cursor; cursor = cursor.parent) ancestors.push(cursor.props?.class)
    assert.ok(ancestors.includes('page-checks'))
    assert.ok(!ancestors.includes('controls'))
    assert.match(
      textOf(all(root).find((n) => n.props?.class === 'evidence')).replace(/\s+/g, ' '),
      /覆盖 99\.90%（缺 1\/1000 个字符/
    )

    // 跳页跳过机器核验页，落到需要人工的第 2 页
    editor().goToNextUnreviewedPage(1)
    assert.equal(editor().getCurrentPage(), 2)
    goBack().props.onClick()
    await nextTick()
    assert.equal(editor().getCurrentPage(), 1)
    assert.ok(all(root).find((n) => n.props?.class === 'auto-review'))

    // 页面说明是人工判断：写说明即接管该页，机器核验失效并重新要求三项核验
    pageNote().props['onUpdate:value']('本页机器判断有误，需重新解析')
    pageNote().props.onChange({ target: { value: '本页机器判断有误，需重新解析' } })
    await nextTick()
    assert.equal(all(root).find((n) => n.props?.class === 'auto-review'), undefined)
    assert.deepEqual(pageSelect().props.options.map((o) => o.label), ['第 1 页', '第 2 页', '第 3 页 · 机器核验'])
    assert.deepEqual(editor().validate('approve'), [
      '第 1 页尚未完成阅读顺序、完整性和对应关系核验',
      '第 2 页尚未完成阅读顺序、完整性和对应关系核验'
    ])

    // 补三项核验后第 1 页放行（说明已满足含排除文块的说明要求）
    pageChecks().forEach((n) => n.props['onUpdate:checked'](true))
    await nextTick()
    assert.deepEqual(editor().validate('approve'), ['第 2 页尚未完成阅读顺序、完整性和对应关系核验'])
    assert.equal(summary(), '机器核验 1 页 · 待人工 1 页')

    // 机器核验页上主动勾选三项核验不应再被要求填写页面说明
    all(root)
      .find((n) => n.type === 'a-button' && textOf(n) === '下一页')
      .props.onClick()
    await nextTick()
    all(root)
      .find((n) => n.type === 'a-button' && textOf(n) === '下一页')
      .props.onClick()
    await nextTick()
    assert.equal(editor().getCurrentPage(), 3)
    pageChecks().forEach((n) => n.props['onUpdate:checked'](true))
    await nextTick()
    assert.deepEqual(editor().validate('approve'), ['第 2 页尚未完成阅读顺序、完整性和对应关系核验'])
  } finally {
    app?.unmount()
    await server.close()
  }
})

test('占位文块不能参与检索：占位正文未补充时保存被拦截', async () => {
  const source = readFileSync(
    new URL('../../src/components/knowledge/StructuredDocumentReview.vue', import.meta.url),
    'utf8'
  )
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom',
    plugins: [
      {
        name: 'structured-review-placeholder-test',
        resolveId(id) {
          if (id === 'virtual:structure-placeholder') return '\0structure-placeholder'
        },
        load(id) {
          if (id !== '\0structure-placeholder') return
          return compileScript(parse(source).descriptor, {
            id: 'structure-placeholder',
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
  let root
  try {
    const { default: Panel } = await server.ssrLoadModule('virtual:structure-placeholder')
    const editorRef = ref(null)
    const checks = { reading_order: false, text_complete: false, relationships: false }
    const placeholder = '[空结构块：请对照原页补充内容或注明排除原因]'
    const structure = {
      pages: [{ page: 1, width: 100, height: 100, checks: { ...checks }, note: '' }],
      blocks: [
        { id: 'ph', page: 1, kind: 'paragraph', text: placeholder, source_text: placeholder, bbox: [0, 0, 1, 1] },
        { id: 'b', page: 1, kind: 'paragraph', text: '正文', source_text: '正文', bbox: [0, 0, 1, 1] }
      ]
    }
    root = node('root')
    app = renderer.createApp(() =>
      h(Panel, { ref: editorRef, structure, kbId: 'kb', fileId: 'f', version: 1, editable: true })
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
    const blockCheckbox = () => all(root).filter((n) => n.type === 'a-checkbox')[0]
    const blockTextarea = () => all(root).filter((n) => n.type === 'a-textarea')[0]
    const blockNote = () =>
      all(root)
        .filter((n) => n.type === 'a-input')
        .find((n) => String(n.props?.placeholder || '').includes('排除说明'))
    await new Promise((r) => setImmediate(r))

    // 占位正文不是内容：未补充且未排除时保存被拦截
    assert.deepEqual(editorRef.value.validate('save'), [
      '第 1 页文块 1 未排除且没有正文，请补充原文或勾选「不参与检索」'
    ])
    // 勾选「不参与检索」并写明原因后放行
    blockCheckbox().props.onChange({ target: { checked: true } })
    await nextTick()
    blockNote().props.onChange({ target: { value: '噪声' } })
    await nextTick()
    assert.deepEqual(editorRef.value.validate('save'), [])
    // 补充原文后可以取消排除
    blockCheckbox().props.onChange({ target: { checked: false } })
    await nextTick()
    blockTextarea().props.onChange({ target: { value: '补录的原文' } })
    await nextTick()
    assert.deepEqual(editorRef.value.validate('save'), [])
  } finally {
    app?.unmount()
    await server.close()
  }
})

test('保存成功后重设基线：保存按钮不再一直是可点状态', async () => {
  const source = readFileSync(
    new URL('../../src/components/knowledge/StructuredDocumentReview.vue', import.meta.url),
    'utf8'
  )
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom',
    plugins: [
      {
        name: 'structured-review-rebaseline-test',
        resolveId(id) {
          if (id === 'virtual:structure-rebaseline') return '\0structure-rebaseline'
        },
        load(id) {
          if (id !== '\0structure-rebaseline') return
          return compileScript(parse(source).descriptor, {
            id: 'structure-rebaseline',
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
  let root
  try {
    const { default: Panel } = await server.ssrLoadModule('virtual:structure-rebaseline')
    const editorRef = ref(null)
    const structure = {
      pages: [
        {
          page: 1,
          width: 100,
          height: 100,
          checks: { reading_order: true, text_complete: true, relationships: true },
          note: '已核对',
          auto_review: { rule: 'no-anomaly/v1' }
        }
      ],
      blocks: [{ id: 'b1', page: 1, kind: 'paragraph', text: '第一页', source_text: '第一页', bbox: [0, 0, 1, 1] }]
    }
    root = node('root')
    app = renderer.createApp(() =>
      h(Panel, { ref: editorRef, structure, kbId: 'kb', fileId: 'f', version: 1, editable: true })
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
    const textOf = (item) => (item.text || '') + item.children.map(textOf).join('')
    const saveButton = () => all(root).find((n) => n.type === 'a-button' && textOf(n) === '保存结构修订')
    await new Promise((r) => setImmediate(r))

    assert.equal(saveButton().props.disabled, true)
    all(root).filter((n) => n.type === 'a-textarea')[0].props.onChange({ target: { value: '人工改写' } })
    await nextTick()
    assert.equal(saveButton().props.disabled, false)

    // 保存成功后由面板调用：当前状态成为新基线，按钮回到禁用
    editorRef.value.rebaseline()
    await nextTick()
    assert.equal(saveButton().props.disabled, true)
  } finally {
    app?.unmount()
    await server.close()
  }
})
