<template>
  <div class="review-panel">
    <a-spin v-if="loading" tip="正在加载清洗记录..." />
    <a-alert v-else-if="error" type="error" :message="error" show-icon>
      <template #action><a-button @click="load">重试</a-button></template>
    </a-alert>
    <template v-else>
      <div class="review-toolbar">
        <a-select
          v-if="latest"
          v-model:value="selected"
          aria-label="内容版本"
          :options="versionOptions"
          :disabled="dirty || busy"
        />
        <span v-if="current">{{ current.approved_at ? '已审核' : '待审核' }}</span>
        <span v-if="published">当前检索版本：{{ published.version }}</span>
        <a-button :disabled="dirty || busy" @click="load">重新加载</a-button>
        <a-button v-if="dirty" :disabled="busy" @click="draft = current.content"
          >撤销未保存修改</a-button
        >
        <a-button
          v-if="!latest"
          :disabled="!data.can_manage"
          :loading="busy"
          @click="act('prepare')"
          >生成清洗稿</a-button
        >
        <template v-else>
          <a-button :disabled="!editable || !dirty" :loading="busy" @click="act('save')"
            >保存修订</a-button
          >
          <a-button
            type="primary"
            :disabled="!editable || dirty || !!latest.approved_at"
            :loading="busy"
            @click="act('approve')"
            >审核通过</a-button
          >
          <a-button
            :disabled="!editable || dirty || !latest.approved_at || !preview"
            :loading="busy"
            @click="indexDocument"
            >切片入库</a-button
          >
        </template>
      </div>
      <p class="review-note">
        审核针对已保存的最新版本。修订后需重新审核、入库；已有检索片段在重新入库前保持原版本。
      </p>
      <a-alert v-if="actionError" :message="actionError" type="error" show-icon />
      <template v-if="current">
        <div class="chunk-preview-controls">
          <strong>混合材料切片</strong>
          <label
            >目标长度（估算 tokens）
            <a-input-number v-model:value="chunkSize" :min="64" :max="4096" :disabled="busy" />
          </label>
          <a-button
            :disabled="dirty || busy || current !== latest"
            :loading="previewLoading"
            @click="previewChunks"
            >预览切片</a-button
          >
          <span>保存后预览，审核通过后按此预览入库。</span>
        </div>
        <a-alert v-if="previewError" :message="previewError" type="error" show-icon />
        <details v-if="preview" open class="chunk-preview-list">
          <summary>待入库片段：{{ preview.chunks.length }} 个 · 版本 {{ preview.version }}</summary>
          <SourceChunkCard
            v-for="chunk in preview.chunks.slice((previewPage - 1) * 10, previewPage * 10)"
            :key="chunk.id"
            :chunk="chunk"
            :kb-id="kbId"
            :file-id="fileId"
            :source-text="current.content"
          />
          <a-pagination
            v-model:current="previewPage"
            :total="preview.chunks.length"
            :page-size="10"
            :show-size-changer="false"
          />
        </details>
        <details class="quality-report">
          <summary>
            清洗与核验记录（{{ current.report.changes?.length || 0 }} 处处理，{{
              current.report.warnings?.length || 0
            }}
            条提示）
          </summary>
          <p>提示用于辅助检查，不能证明无漏页、漏栏或识别错误。</p>
          <p v-for="(warning, i) in current.report.warnings || []" :key="`w${i}`">
            {{ warning.page ? `第 ${warning.page} 页：` : '' }}{{ warning.message }}
          </p>
          <div
            v-for="(change, i) in current.report.changes || []"
            :key="`c${i}`"
            class="change-record"
          >
            <span>第 {{ change.page }} 页 · 第 {{ change.line }} 行</span>
            <pre>{{ change.before }}</pre>
            <span>→</span>
            <pre>{{ change.after || '已移除' }}</pre>
          </div>
        </details>
        <div class="review-columns">
          <section>
            <a-radio-group v-model:value="sourceMode" button-style="solid">
              <a-radio-button value="text">原始解析</a-radio-button>
              <a-radio-button value="pdf">源文件</a-radio-button>
            </a-radio-group>
            <pre v-if="sourceMode === 'text'" class="original-text">{{ rawContent }}</pre>
            <div v-else class="source-preview"><slot name="source" /></div>
          </section>
          <section>
            <label for="review-content">{{
              editable ? '清洗稿 / 人工修订' : '历史版本（只读）'
            }}</label>
            <a-textarea
              id="review-content"
              v-model:value="draft"
              :readonly="!editable"
              :maxlength="2000000"
            />
            <small
              >{{ current.created_by }} · {{ current.created_at
              }}<template v-if="current.approved_at">
                · 审核：{{ current.approved_by }} / {{ current.approved_at }}</template
              ></small
            >
          </section>
        </div>
      </template>
    </template>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { documentApi } from '@/apis/knowledge_api'
import SourceChunkCard from '@/components/knowledge/SourceChunkCard.vue'

const props = defineProps({
  kbId: { type: String, required: true },
  fileId: { type: String, required: true }
})
const emit = defineEmits(['dirty-change'])
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const actionError = ref('')
const data = ref({ revisions: [] })
const selected = ref(null)
const draft = ref('')
const sourceMode = ref('text')
const chunkSize = ref(512)
const preview = ref(null)
const previewLoading = ref(false)
const previewError = ref('')
const previewPage = ref(1)
let previewRequestId = 0
let requestId = 0
const latest = computed(() => data.value.revisions.at(-1))
const published = computed(
  () =>
    data.value.revisions
      .filter((r) => r.indexed_at)
      .sort((a, b) => b.indexed_at.localeCompare(a.indexed_at))[0]
)
const current = computed(() => data.value.revisions.find((r) => r.version === selected.value))
const editable = computed(
  () =>
    data.value.can_manage &&
    current.value === latest.value &&
    !['parsing', 'indexing'].includes(data.value.file_status) &&
    !busy.value
)
const dirty = computed(() => !!current.value && draft.value !== current.value.content)
watch(dirty, (value) => emit('dirty-change', value))
onBeforeUnmount(() => emit('dirty-change', false))
const versionOptions = computed(() =>
  data.value.revisions.map((r) => ({
    label: `版本 ${r.version}${r.approved_at ? ' · 已审核' : ''}`,
    value: r.version
  }))
)
const rawContent = computed(
  () =>
    data.value.revisions.filter((r) => r.version <= selected.value && r.raw_content !== null).at(-1)
      ?.raw_content || ''
)
watch(current, (value) => {
  draft.value = value?.content || ''
})
watch([draft, selected, chunkSize], () => {
  previewRequestId++
  preview.value = null
  previewLoading.value = false
  previewError.value = ''
})

async function previewChunks() {
  const id = ++previewRequestId
  preview.value = null
  previewLoading.value = true
  previewError.value = ''
  try {
    const result = await documentApi.previewDocumentChunks(props.kbId, props.fileId, {
      version: current.value.version,
      chunk_token_num: chunkSize.value
    })
    if (id === previewRequestId) {
      preview.value = result
      previewPage.value = 1
    }
  } catch (e) {
    if (id === previewRequestId) previewError.value = e.message || '预览失败，请重试'
  } finally {
    if (id === previewRequestId) previewLoading.value = false
  }
}

function accept(result) {
  data.value = result
  selected.value = result.revisions.at(-1)?.version ?? null
}
async function load() {
  previewRequestId++
  preview.value = null
  const id = ++requestId
  loading.value = true
  error.value = ''
  try {
    const result = await documentApi.getDocumentReview(props.kbId, props.fileId)
    if (id === requestId) accept(result)
  } catch (e) {
    if (id === requestId) error.value = e.message || '加载失败'
  } finally {
    if (id === requestId) loading.value = false
  }
}
async function act(action) {
  busy.value = true
  actionError.value = ''
  try {
    accept(
      await documentApi.changeDocumentReview(props.kbId, props.fileId, {
        action,
        version: latest.value?.version || 0,
        ...(action === 'save' ? { content: draft.value } : {})
      })
    )
    message.success(action === 'approve' ? '已审核，可执行切片入库' : '已保存清洗稿')
  } catch (e) {
    actionError.value = e.message || '操作失败，请重新加载后重试'
  } finally {
    busy.value = false
  }
}
async function indexDocument() {
  busy.value = true
  actionError.value = ''
  try {
    await documentApi.indexDocuments(props.kbId, [props.fileId], preview.value.params)
    message.success('入库任务已提交，可在任务列表查看结果')
    await load()
  } catch (e) {
    actionError.value = e.message || '提交入库失败'
  } finally {
    busy.value = false
  }
}
watch(() => [props.kbId, props.fileId], load, { immediate: true })
</script>

<style scoped lang="less">
.review-panel {
  height: 100%;
  padding: 16px;
  overflow: auto;
  color: var(--color-text);
}
.review-toolbar {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
  .ant-select {
    min-width: 160px;
  }
}
.chunk-preview-controls {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  margin-bottom: 12px;
  span {
    color: var(--color-text-secondary);
  }
}
.chunk-preview-list {
  margin-bottom: 16px;
  max-height: 55vh;
  overflow: auto;
  .source-chunk {
    margin: 12px 0;
  }
}
.review-note,
small {
  color: var(--color-text-secondary);
}
.quality-report {
  padding: 12px;
  background: var(--gray-25);
  margin-bottom: 12px;
}
.change-record {
  border-top: 1px solid var(--gray-150);
  padding: 8px;
  pre {
    white-space: pre-wrap;
  }
}
.review-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  height: 55vh;
  section {
    display: flex;
    flex-direction: column;
    min-width: 0;
    gap: 8px;
  }
  textarea {
    flex: 1;
    resize: none;
  }
}
.original-text,
.source-preview {
  flex: 1;
  min-height: 0;
  overflow: auto;
  border: 1px solid var(--gray-150);
  padding: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
@media (max-width: 800px) {
  .review-columns {
    grid-template-columns: 1fr;
    height: auto;
    section {
      height: 50vh;
    }
  }
}
</style>
