import test from 'node:test'
import assert from 'node:assert/strict'

import { createPinia, setActivePinia } from 'pinia'
import { createApp, nextTick } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createServer } from 'vite'
import { message } from 'ant-design-vue'

const storageValues = new Map()
globalThis.localStorage = {
  getItem: (key) => storageValues.get(key) ?? null,
  setItem: (key, value) => storageValues.set(key, String(value)),
  removeItem: (key) => storageValues.delete(key),
  clear: () => storageValues.clear()
}

test('从二级目录点击全部文件会清空 parent_id 并返回根目录', async (t) => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })

  try {
    const warnings = []
    t.mock.method(console, 'warn', (...args) => warnings.push(args))
    const pinia = createPinia()
    const app = createApp({})
    app.use(pinia)
    app.use(createRouter({ history: createMemoryHistory(), routes: [] }))
    setActivePinia(pinia)
    const { documentApi } = await server.ssrLoadModule('/src/apis/knowledge_api.js')
    const { useDatabaseStore } = await server.ssrLoadModule('/src/stores/database.js')
    const requests = []

    documentApi.listDocuments = async (kbId, params) => {
      requests.push({ kbId, params })
      return {
        items: [],
        page: 1,
        page_size: 100,
        total: 0,
        has_more: false,
        path_prefix: ''
      }
    }

    const store = app.runWithContext(() => useDatabaseStore())
    store.kbId = 'kb_1'
    store.fileBrowser.parentId = 'folder_2'
    store.folderBreadcrumbs = [
      { file_id: null, filename: '全部文件', path_prefix: '' },
      { file_id: 'folder_2', filename: '二级目录', path_prefix: '' }
    ]

    await store.goToFolder(0)

    assert.equal(store.fileBrowser.parentId, null)
    assert.deepEqual(store.folderBreadcrumbs, [
      { file_id: null, filename: '全部文件', path_prefix: '' }
    ])
    assert.deepEqual(requests, [
      {
        kbId: 'kb_1',
        params: {
          page: 1,
          page_size: 100,
          status: 'all',
          recursive: false
        }
      }
    ])
    assert.deepEqual(warnings, [])
  } finally {
    await server.close()
  }
})

test('知识库提交跨账号返回时，不把旧入队任务登记给新账号', async (t) => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })
  const pinia = createPinia()
  const app = createApp({})
  app.use(pinia)
  app.use(createRouter({ history: createMemoryHistory(), routes: [] }))
  setActivePinia(pinia)
  const { documentApi, databaseApi } = await server.ssrLoadModule('/src/apis/knowledge_api.js')
  const { useDatabaseStore } = await server.ssrLoadModule('/src/stores/database.js')
  const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
  const { useTaskerStore } = await server.ssrLoadModule('/src/stores/tasker.js')
  const database = app.runWithContext(() => useDatabaseStore())
  const user = useUserStore()
  const tasker = useTaskerStore()
  t.mock.method(message, 'success', () => {})
  t.mock.method(databaseApi, 'getDatabaseInfo', async () => ({ stats: { processing_count: 0 } }))
  t.mock.method(documentApi, 'listDocuments', async () => ({
    items: [],
    stats: { processing_count: 0 }
  }))
  try {
    for (const [action, apiMethod, args] of [
      ['addFiles', 'addDocuments', [{ items: ['fixture'], contentType: 'file', params: {} }]],
      ['parseFiles', 'parseDocuments', [['file']]],
      ['parsePendingFiles', 'parsePendingDocuments', []],
      ['indexFiles', 'indexDocuments', [['file']]],
      ['indexPendingFiles', 'indexPendingDocuments', []]
    ]) {
      await t.test(action, async () => {
        database.kbId = 'test-kb'
        user.token = `old-${action}`
        user.userRole = 'admin'
        let resolve
        t.mock.method(
          documentApi,
          apiMethod,
          () =>
            new Promise((done) => {
              resolve = done
            })
        )
        const pending = database[action](...args)
        user.logout()
        user.token = `new-${action}`
        user.userRole = 'admin'
        resolve({ status: 'queued', task_id: 'old-task' })
        assert.equal(await pending, true)
        assert.deepEqual(tasker.tasks, [])
        const current = database[action](...args)
        resolve({ status: 'queued', task_id: `new-${action}` })
        assert.equal(await current, true)
        assert.equal(tasker.tasks[0].id, `new-${action}`)
        tasker.reset()
      })
    }
  } finally {
    database.stopAutoRefresh()
    tasker.$dispose()
    await server.close()
  }
})

test('知识库任务执行期间文件列表自动刷新保持存活，任务结束后补刷新并自动停止', async (t) => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })
  const pinia = createPinia()
  const app = createApp({})
  app.use(pinia)
  app.use(createRouter({ history: createMemoryHistory(), routes: [] }))
  setActivePinia(pinia)
  const { documentApi, databaseApi } = await server.ssrLoadModule('/src/apis/knowledge_api.js')
  const { useDatabaseStore } = await server.ssrLoadModule('/src/stores/database.js')
  const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
  const { useTaskerStore } = await server.ssrLoadModule('/src/stores/tasker.js')
  const database = app.runWithContext(() => useDatabaseStore())
  const user = useUserStore()
  const tasker = useTaskerStore()
  user.token = 'token-live'
  user.userRole = 'admin'
  t.mock.method(message, 'success', () => {})

  let stats = { processing_count: 0 }
  let files = []
  let infoCalls = 0
  t.mock.method(databaseApi, 'getDatabaseInfo', async () => {
    infoCalls += 1
    return { stats, files: {} }
  })
  t.mock.method(documentApi, 'listDocuments', async () => ({
    items: files,
    page: 1,
    page_size: 100,
    total: files.length,
    has_more: false,
    path_prefix: ''
  }))
  t.mock.method(documentApi, 'addDocuments', async () => ({
    status: 'queued',
    task_id: 'task_live',
    message: '任务已提交'
  }))

  try {
    database.kbId = 'kb_live'

    // 上传入队：任务先于 worker 创建的文件记录存在
    const pending = database.addFiles({ items: ['a.pdf'], contentType: 'file', params: {} })
    assert.equal(await pending, true)
    assert.equal(
      tasker.tasks.some((task) => task.id === 'task_live' && task.status === 'queued'),
      true
    )
    // 1 秒后的延迟刷新拿到空列表且 stats 无处理中，自动刷新不得被关闭
    assert.equal(database.state.autoRefresh, true)

    // worker 创建文件记录并开始解析，存活的自动刷新无需手动刷新即可取到
    files = [{ file_id: 'file_1', filename: 'a.pdf', status: 'parsing' }]
    stats = { processing_count: 1 }
    await database.getDatabaseInfo(undefined, true, true)
    await database.loadDocumentFiles({ isBackground: true })
    assert.equal(database.documentFiles[0]?.status, 'parsing')
    assert.equal(database.state.autoRefresh, true)

    // 任务结束：立即补一次后台刷新拿到最终状态，随后自动停止轮询
    files = [{ file_id: 'file_1', filename: 'a.pdf', status: 'done' }]
    stats = { processing_count: 0 }
    const infoCallsBefore = infoCalls
    tasker.tasks[0].status = 'success'
    await nextTick()
    await new Promise((resolve) => setTimeout(resolve, 0))
    assert.ok(infoCalls > infoCallsBefore)
    assert.equal(database.documentFiles[0]?.status, 'done')
    assert.equal(database.state.autoRefresh, false)
  } finally {
    database.stopAutoRefresh()
    tasker.reset()
    await server.close()
  }
})
