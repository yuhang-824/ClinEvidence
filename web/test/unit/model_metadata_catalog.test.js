import test from 'node:test'
import assert from 'node:assert/strict'
import { providers } from '@opencode-ai/models/snapshot'
import { createServer } from 'vite'
import { resolveModelDisplayMetadata } from '../../src/utils/modelMetadata.js'

test('构建投影只包含两家线上聊天供应商，并保留其显示结果', async () => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })
  try {
    const { loadModelMetadataCatalog } = await server.ssrLoadModule('/src/utils/modelMetadata.js')
    const { providers: compact } = await loadModelMetadataCatalog()
    assert.deepEqual(Object.keys(compact).sort(), ['deepseek', 'minimax-cn'])
    for (const [providerId, provider] of Object.entries(providers).filter(([id]) =>
      ['deepseek', 'minimax-cn'].includes(id)
    )) {
      assert.deepEqual(Object.keys(compact[providerId].models), Object.keys(provider.models))
      for (const id of Object.keys(provider.models)) {
        assert.deepEqual(
          resolveModelDisplayMetadata(compact, providerId, { id }),
          resolveModelDisplayMetadata(providers, providerId, { id }),
          `${providerId}/${id}`
        )
      }
    }
    assert.ok(JSON.stringify(compact).length < JSON.stringify(providers).length * 0.4)
    const remote = {
      id: 'missing',
      input_modalities: ['image'],
      context_length: 1234,
      pricing: { prompt: '0.000001', completion: '0.000002' }
    }
    assert.deepEqual(resolveModelDisplayMetadata(compact, 'missing', remote), {
      matched: false,
      inputModalities: ['image'],
      context: 1234,
      contextLabel: '1K',
      isOneMillionContext: false,
      vision: true,
      price: { input: 1, output: 2 }
    })
  } finally {
    await server.close()
  }
})
