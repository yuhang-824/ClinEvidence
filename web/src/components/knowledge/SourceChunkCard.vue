<template>
  <article class="source-chunk">
    <header>
      <strong>片段 {{ (chunk.chunk_index ?? chunk.chunk_order_index ?? 0) + 1 }}</strong>
      <span v-if="meta">{{ kindLabels[meta.kind] || meta.kind }} · 版本 {{ meta.revision }}</span>
      <span v-if="meta?.pages?.length">PDF 第 {{ meta.pages.join('、') }} 页</span>
      <span v-else>PDF 页码未确认</span>
    </header>
    <p v-if="meta?.section?.length">{{ meta.section.join(' / ') }}</p>
    <pre>{{ chunk.content }}</pre>
    <p v-for="warning in meta?.warnings || []" :key="warning" class="warning">{{ warning }}</p>
    <template v-if="meta">
      <a-button size="small" :loading="loading" @click="showSource">查看来源段落</a-button>
      <a-alert v-if="error" :message="error" type="error" show-icon />
      <div v-if="source" class="source-excerpts">
        <p>审核稿版本 {{ meta.revision }} · 正文第 {{ meta.start_line }}–{{ meta.end_line }} 行</p>
        <p>以下内容来自该版本审核稿；原 PDF 请在“源文件”中对照。</p>
        <section v-for="(excerpt, i) in source.excerpts" :key="i">
          <small>{{ excerpt.role === 'context' ? '章节 / 表头上下文' : '正文' }}</small>
          <pre>{{ excerpt.text }}</pre>
        </section>
        <a-button size="small" @click="source = null">收起来源</a-button>
      </div>
    </template>
    <p v-else>历史片段没有版本来源，使用混合材料策略重新入库后可追溯。</p>
  </article>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { documentApi } from '@/apis/knowledge_api'

const props = defineProps({
  chunk: { type: Object, required: true },
  kbId: { type: String, required: true },
  fileId: { type: String, required: true },
  sourceText: { type: String, default: null }
})
const meta = computed(() => props.chunk.source_metadata)
const source = ref(null)
const error = ref('')
const loading = ref(false)
let requestId = 0
const kindLabels = {
  table: '表格',
  form: '表单',
  recommendation: '推荐意见',
  paragraph: '正文',
  code: '原格式块',
  heading: '章节标题'
}
watch(
  () => props.chunk,
  () => {
    requestId++
    source.value = null
    error.value = ''
    loading.value = false
  }
)
async function showSource() {
  const id = ++requestId
  error.value = ''
  loading.value = true
  try {
    const result =
      props.sourceText !== null
        ? {
            excerpts: meta.value.spans.map((span) => ({
              ...span,
              text: Array.from(props.sourceText).slice(span.start, span.end).join('')
            }))
          }
        : await documentApi.getChunkSource(
            props.kbId,
            props.fileId,
            props.chunk.id,
            meta.value.revision
          )
    if (id === requestId) source.value = result
  } catch (e) {
    if (id === requestId) error.value = e.message || '读取来源失败，请重试'
  } finally {
    if (id === requestId) loading.value = false
  }
}
</script>

<style scoped lang="less">
.source-chunk {
  padding: 16px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-0);
  color: var(--color-text);
  min-width: 0;
  header {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }
  p,
  small,
  header span {
    color: var(--color-text-secondary);
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    margin: 12px 0;
    max-height: 340px;
    overflow: auto;
  }
  .warning {
    color: var(--color-warning-700);
  }
  .source-excerpts {
    margin-top: 12px;
    padding: 12px;
    background: var(--gray-25);
  }
}
</style>
