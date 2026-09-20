<template>
  <div class="structured-review">
    <div class="controls">
      <strong>逐页结构审核 · {{ pages.length }} 页</strong>
      <a-select v-model:value="pageNumber" aria-label="审核页码" :options="pageOptions" />
      <a-button :disabled="pageNumber <= 1" @click="pageNumber--">上一页</a-button>
      <a-button :disabled="pageNumber >= pages.length" @click="pageNumber++">下一页</a-button>
      <a-button @click="downloadJson">下载原始结构 JSON</a-button>
      <a-button v-if="editable" :disabled="!dirty" type="primary" @click="requestSave"
        >保存结构修订</a-button
      >
    </div>
    <p>
      对照原页检查顺序、遗漏与对应关系。表格保留完整行和表头；图示请写清条件、分支、动作及去向。修改后重新核验本页。
    </p>
    <a-alert v-if="error" :message="error" type="error" show-icon />
    <div class="structure-columns">
      <section class="page-view">
        <a-spin v-if="loading" tip="正在读取原页..." />
        <div v-else-if="imageUrl" class="page-image">
          <img :src="imageUrl" :alt="`源 PDF 第 ${pageNumber} 页`" />
          <div v-if="selectedBlock?.bbox" class="source-box" :style="boxStyle" />
        </div>
        <a-button v-else @click="loadPage">重新加载原页</a-button>
      </section>
      <section class="block-list">
        <p v-for="(issue, i) in currentPage.issues || []" :key="i" class="issue">
          需核对：{{ issue }}
        </p>
        <article
          v-for="(block, index) in pageBlocks"
          :key="block.id"
          :data-block-id="block.id"
          :class="{ selected: selected === block.id }"
          @click="selected = block.id"
        >
          <div class="controls">
            <strong>文块 {{ index + 1 }}</strong>
            <a-select
              :value="block.kind"
              :options="kindOptions"
              :disabled="!editable"
              aria-label="文块类型"
              @change="setBlock(block, 'kind', $event)"
            />
            <a-input-number
              v-if="block.kind === 'heading'"
              v-model:value="block.level"
              :min="1"
              :max="6"
              :disabled="!editable"
              aria-label="标题层级"
              @change="changed"
            />
            <a-button :disabled="!editable || index === 0" @click="move(block, -1)">上移</a-button>
            <a-button
              :disabled="!editable || index === pageBlocks.length - 1"
              @click="move(block, 1)"
              >下移</a-button
            >
            <a-checkbox
              :checked="block.excluded"
              :disabled="!editable"
              @change="setBlock(block, 'excluded', $event.target.checked)"
              >不参与检索</a-checkbox
            >
          </div>
          <details>
            <summary>原始解析文字（保留不覆盖）</summary>
            <pre>{{ originalBlock(block.id)?.source_text || '人工补录' }}</pre>
          </details>
          <a-textarea
            :value="block.text"
            :readonly="!editable"
            :auto-size="{ minRows: 3, maxRows: 18 }"
            :maxlength="200000"
            aria-label="文块修订内容"
            @change="setBlock(block, 'text', $event.target.value)"
          />
          <a-input
            :value="block.note"
            :readonly="!editable"
            :maxlength="4000"
            placeholder="文块修订或排除说明"
            @change="setBlock(block, 'note', $event.target.value)"
          />
        </article>
        <a-button v-if="editable" @click="addBlock">补录遗漏文块</a-button>
        <div class="page-checks">
          <strong>本页核验</strong>
          <a-checkbox
            v-for="check in checks"
            :key="check.key"
            v-model:checked="currentPage.checks[check.key]"
            :disabled="!editable || !imageUrl || loading"
            >{{ check.label }}</a-checkbox
          >
          <a-textarea
            v-model:value="currentPage.note"
            :readonly="!editable"
            :maxlength="4000"
            placeholder="核验说明：表格行列、图示路径、排除内容或异常页须记录核对依据与处理结果"
          />
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { documentApi } from '@/apis/knowledge_api'

const props = defineProps({
  structure: { type: Object, required: true },
  kbId: { type: String, required: true },
  fileId: { type: String, required: true },
  version: { type: Number, required: true },
  editable: Boolean
})
const emit = defineEmits(['save', 'dirty-change'])
const blocks = ref(
  props.structure.blocks.map(({ id, page, text, kind, level, excluded, note }) => ({
    id,
    page,
    text,
    kind,
    level: level || 2,
    excluded: !!excluded,
    note: note || ''
  }))
)
const pages = ref(
  props.structure.pages.map((p) => ({ ...p, checks: { ...p.checks }, note: p.note || '' }))
)
const pageNumber = ref(1)
const selected = ref(null)
async function locateBlock(id) {
  const block = originalBlock(id)
  if (!block) return
  pageNumber.value = block.page
  await nextTick()
  selected.value = id
  await nextTick()
  document.querySelector(`[data-block-id="${CSS.escape(id)}"]`)?.scrollIntoView({ block: 'center' })
}
// 与后端 structure.py 的保存/审核契约保持一致：排除必须留说明、未排除不得为空、
// 审核需整页核验且含表格/图示/排除内容的页必须填写说明。前端先行校验，
// 避免用户只能看到统一的"请求参数错误"。规则变更须与后端同步。
function validate(scope = 'save') {
  const problems = []
  const perPageIndex = new Map()
  for (const block of blocks.value) {
    const index = (perPageIndex.get(block.page) || 0) + 1
    perPageIndex.set(block.page, index)
    const label = `第 ${block.page} 页文块 ${index}`
    if (block.excluded && !block.note.trim()) {
      problems.push(`${label} 已勾选「不参与检索」，请填写文块修订或排除说明`)
    } else if (!block.excluded && !block.text.trim()) {
      problems.push(`${label} 未排除且内容为空，请补充原文或勾选「不参与检索」`)
    }
  }
  if (scope === 'approve') {
    for (const page of pages.value) {
      const checked = checks.every((check) => page.checks[check.key])
      if (!checked) {
        problems.push(`第 ${page.page} 页尚未完成阅读顺序、完整性和对应关系核验`)
        continue
      }
      const special = blocks.value.some(
        (block) =>
          block.page === page.page &&
          (block.kind === 'table' || block.kind === 'relationship' || block.excluded)
      )
      if ((special || (page.issues?.length ?? 0) > 0) && !page.note.trim()) {
        problems.push(`第 ${page.page} 页需要填写表格、图示或异常核验说明`)
      }
    }
  }
  return problems
}
function requestSave() {
  const problems = validate('save')
  if (problems.length) {
    error.value = problems.join('；')
    return
  }
  error.value = ''
  emit('save', payload.value)
}
defineExpose({ locateBlock, validate })
const imageUrl = ref('')
const loading = ref(false)
const error = ref('')
let requestId = 0
const payload = computed(() => ({
  blocks: blocks.value,
  pages: pages.value.map(({ page, checks, note }) => ({ page, checks, note }))
}))
const initial = JSON.stringify(payload.value)
const dirty = computed(() => JSON.stringify(payload.value) !== initial)
watch(dirty, (value) => emit('dirty-change', value))
const pageOptions = computed(() =>
  pages.value.map((p) => ({
    value: p.page,
    label: `第 ${p.page} 页${Object.values(p.checks).every(Boolean) ? ' · 已核验' : ''}`
  }))
)
const currentPage = computed(() => pages.value.find((p) => p.page === pageNumber.value))
const pageBlocks = computed(() => blocks.value.filter((b) => b.page === pageNumber.value))
const checks = [
  { key: 'reading_order', label: '已核对阅读顺序' },
  { key: 'text_complete', label: '已核对全文与图中信息，无未处理遗漏' },
  { key: 'relationships', label: '已核对行列、字段、条件与分支对应关系' }
]
const kindOptions = [
  { value: 'heading', label: '标题' },
  { value: 'paragraph', label: '正文' },
  { value: 'table', label: '表格' },
  { value: 'relationship', label: '图示 / 流程关系' }
]
const originalBlock = (id) => props.structure.blocks.find((b) => b.id === id)
const selectedBlock = computed(() => originalBlock(selected.value))
const boxStyle = computed(() => {
  const [left, top, right, bottom] = selectedBlock.value.bbox
  return {
    left: `${(left / currentPage.value.width) * 100}%`,
    top: `${(top / currentPage.value.height) * 100}%`,
    width: `${((right - left) / currentPage.value.width) * 100}%`,
    height: `${((bottom - top) / currentPage.value.height) * 100}%`
  }
})
function setBlock(block, field, value) {
  block[field] = value
  changed()
}
function changed() {
  for (const key of Object.keys(currentPage.value.checks)) currentPage.value.checks[key] = false
}
function move(block, direction) {
  const index = blocks.value.indexOf(block)
  const next = index + direction
  if (blocks.value[next]?.page !== block.page) return
  blocks.value.splice(index, 1)
  blocks.value.splice(next, 0, block)
  changed()
}
function addBlock() {
  const index = blocks.value.findLastIndex((b) => b.page <= pageNumber.value) + 1
  blocks.value.splice(index, 0, {
    id: `new:${crypto.randomUUID()}`,
    page: pageNumber.value,
    kind: 'paragraph',
    text: '',
    note: '',
    excluded: false
  })
  changed()
}
async function loadPage() {
  const id = ++requestId
  loading.value = true
  error.value = ''
  selected.value = null
  if (imageUrl.value) URL.revokeObjectURL(imageUrl.value)
  imageUrl.value = ''
  try {
    const blob = await documentApi.getReviewSource(
      props.kbId,
      props.fileId,
      props.version,
      pageNumber.value
    )
    if (id === requestId) imageUrl.value = URL.createObjectURL(blob)
  } catch (e) {
    if (id === requestId) error.value = e.message || '原页加载失败'
  } finally {
    if (id === requestId) loading.value = false
  }
}
async function downloadJson() {
  try {
    const blob = await documentApi.getReviewSource(props.kbId, props.fileId, props.version)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `structure-v${props.version}.json`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) {
    error.value = e.message || '下载失败'
  }
}
watch(pageNumber, loadPage, { immediate: true })
onBeforeUnmount(() => {
  requestId++
  if (imageUrl.value) URL.revokeObjectURL(imageUrl.value)
})
</script>

<style scoped>
.controls {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 10px;
}
.controls .ant-select {
  min-width: 140px;
}
.structure-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.page-view,
.block-list {
  max-height: 75vh;
  overflow: auto;
}
.page-image {
  position: relative;
}
.page-image img {
  display: block;
  width: 100%;
}
.source-box {
  position: absolute;
  border: 2px solid var(--main-color);
  background: #168da322;
  pointer-events: none;
}
article {
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
article.selected {
  border-color: var(--main-color);
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 220px;
  overflow: auto;
}
.page-checks {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px 0;
}
.issue {
  color: var(--color-text-secondary);
}
@media (max-width: 900px) {
  .structure-columns {
    grid-template-columns: 1fr;
  }
  .page-view {
    max-height: 45vh;
  }
}
</style>
