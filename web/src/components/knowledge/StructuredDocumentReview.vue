<template>
  <div class="structured-review">
    <div class="controls">
      <strong>逐页结构审核 · {{ pages.length }} 页</strong>
      <span v-if="machineVerifiedCount" class="review-summary">
        机器核验 {{ machineVerifiedCount }} 页 · 待人工 {{ pendingReviewCount }} 页
      </span>
      <a-select v-model:value="pageNumber" aria-label="审核页码" :options="pageOptions" />
      <a-button :disabled="pageNumber <= 1" @click="pageNumber--">上一页</a-button>
      <a-button :disabled="pageNumber >= pages.length" @click="pageNumber++">下一页</a-button>
      <a-button @click="downloadJson">下载原始结构 JSON</a-button>
    </div>
    <p>
      对照原页检查顺序、遗漏与对应关系。表格保留完整行和表头；图示请写清条件、分支、动作及去向。修改后重新核验本页。
      解析器证据充分的页已由系统机器核验，只需处理被标记的页；勾选核验项或填写页面说明即可用人工核验覆盖机器结论。
    </p>
    <a-alert
      v-if="visionBlocks.length && visionGeneratedCount < visionBlocks.length"
      :message="`待审核图示自动转写 ${visionGeneratedCount}/${visionBlocks.length} 块；其余需对照原页补录或排除`"
      type="warning"
      show-icon
    />
    <a-alert v-if="error" :message="error" type="error" show-icon />
    <div class="structure-columns">
      <section ref="pageView" class="page-view">
        <a-spin v-if="loading" tip="正在读取原页..." />
        <div v-else-if="imageUrl" class="page-image">
          <img :src="imageUrl" :alt="`源 PDF 第 ${pageNumber} 页`" />
          <div v-if="selectedBlock?.bbox" class="source-box" :style="boxStyle" />
        </div>
        <a-button v-else @click="loadPage">重新加载原页</a-button>
      </section>
      <section ref="blockList" class="block-list">
        <p v-if="currentPage.auto_review" class="auto-review">
          机器核验：{{ autoReviewLabel(currentPage.auto_review) }}，如需人工复核请勾选下方核验项
        </p>
        <p v-if="pageEvidence(currentPage)" class="evidence">{{ pageEvidence(currentPage) }}</p>
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
          <details v-if="originalBlock(block.id)?.vision_graph">
            <summary>视觉模型识别的节点与箭头（请对照原图核对）</summary>
            <p v-if="originalBlock(block.id).vision_graph.title">
              标题：{{ originalBlock(block.id).vision_graph.title }}
            </p>
            <p
              v-for="node in originalBlock(block.id).vision_graph.nodes"
              :key="node.id"
            >
              节点 {{ node.id }}（{{ node.kind }}）：{{ node.text }}
            </p>
            <p
              v-for="(edge, edgeIndex) in originalBlock(block.id).vision_graph.edges"
              :key="edgeIndex"
            >
              箭头 {{ edge.from }} → {{ edge.to }}{{ edge.condition ? ` · 条件：${edge.condition}` : '' }}{{ edge.relation !== 'sequence' ? ` · 关系：${edge.relation}` : '' }}
            </p>
            <p
              v-for="(note, noteIndex) in originalBlock(block.id).vision_graph.footnotes"
              :key="noteIndex"
            >
              脚注 {{ note.marker }}：{{ note.text }}
            </p>
            <p
              v-for="(uncertainty, uncertaintyIndex) in originalBlock(block.id).vision_graph.uncertainties"
              :key="uncertaintyIndex"
            >
              待核对：{{ uncertainty }}
            </p>
          </details>
          <details v-if="originalBlock(block.id)?.vision_raw_response !== undefined">
            <summary>视觉模型原始回复（结构解析失败时可在此核对）</summary>
            <p>
              模型：{{ originalBlock(block.id).vision_model_spec }}；结束原因：{{ originalBlock(block.id).vision_finish_reason || '未提供' }}；
              输入 {{ originalBlock(block.id).vision_usage?.input_tokens ?? '未知' }} / 输出 {{ originalBlock(block.id).vision_usage?.output_tokens ?? '未知' }} tokens
            </p>
            <p v-if="originalBlock(block.id).vision_response_truncated">回复超过保存上限，仅展示前 100000 字符。</p>
            <pre>{{ originalBlock(block.id).vision_raw_response || '模型未返回正文' }}</pre>
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
            @change="noteChanged"
          />
          <div v-if="editable" class="page-actions">
            <a-button :disabled="!dirty" type="primary" @click="requestSave">保存结构修订</a-button>
          </div>
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
const visionBlocks = computed(() =>
  props.structure.blocks.filter(
    (block) => block.kind === 'relationship' && block.source_label === 'image' && !block.excluded
  )
)
const visionGeneratedCount = computed(() => visionBlocks.value.filter((block) => block.vision_graph).length)
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
const pageView = ref(null)
const blockList = ref(null)
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
// 审核需整页核验（机器核验页除外）且含表格/图示/异常内容的页必须填写说明。前端
// 先行校验，避免用户只能看到统一的"请求参数错误"。规则变更须与后端同步。
function validate(scope = 'save') {
  const problems = []
  const perPageIndex = new Map()
  for (const block of blocks.value) {
    const index = (perPageIndex.get(block.page) || 0) + 1
    perPageIndex.set(block.page, index)
    if (scope === 'save' && block.page !== pageNumber.value) continue
    const label = `第 ${block.page} 页文块 ${index}`
    if (block.excluded && !block.note.trim()) {
      problems.push(`${label} 已勾选「不参与检索」，请填写文块修订或排除说明`)
    } else if (!block.excluded && (!block.text.trim() || isPlaceholderText(block.text))) {
      // 占位正文不是内容：解析器写入的空块占位不能参与检索，补充原文后才可取消排除
      problems.push(`${label} 未排除且没有正文，请补充原文或勾选「不参与检索」`)
    }
  }
  if (scope === 'approve') {
    for (const page of pages.value) {
      // 机器核验页整页放行（含说明要求），与后端 require_structure_review 的跳过范围一致
      if (page.auto_review) continue
      const checked = isHumanVerified(page)
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
defineExpose({ locateBlock, validate, getCurrentPage, goToNextPage, rebaseline })

function getCurrentPage() {
  return pageNumber.value
}

// 保存成功后按原文页序继续审核，即使下一页已有机器或人工核验结果
function goToNextPage(fromPage) {
  const ordered = [...pages.value].sort((a, b) => a.page - b.page)
  const next = ordered[ordered.findIndex((p) => p.page === fromPage) + 1]
  if (!next) return null
  pageNumber.value = next.page
  return next.page
}
const imageUrl = ref('')
const loading = ref(false)
const error = ref('')
let requestId = 0
const payload = computed(() => ({
  blocks: blocks.value,
  pages: pages.value.map(({ page, checks, note }) => ({ page, checks, note }))
}))
// 必须是响应式值：普通变量变化不会让 dirty 这个 computed 失效
const savedSnapshot = ref(JSON.stringify(payload.value))
const dirty = computed(() => JSON.stringify(payload.value) !== savedSnapshot.value)
// 保存成功后把当前状态认作已保存基线：版本原地更新时组件不会重挂，否则保存按钮会一直可点
function rebaseline() {
  savedSnapshot.value = JSON.stringify(payload.value)
}
watch(dirty, (value) => emit('dirty-change', value))
const pageOptions = computed(() =>
  pages.value.map((p) => ({ value: p.page, label: `第 ${p.page} 页${pageSuffix(p)}` }))
)
const machineVerifiedCount = computed(() => pages.value.filter(isMachineVerified).length)
// 待人工 = 既没有机器核验，也没有人工完成三项核验
const pendingReviewCount = computed(() => pages.value.filter((p) => !isVerified(p)).length)
const currentPage = computed(() => pages.value.find((p) => p.page === pageNumber.value))
const pageBlocks = computed(() => blocks.value.filter((b) => b.page === pageNumber.value))
const checks = [
  { key: 'reading_order', label: '已核对阅读顺序' },
  { key: 'text_complete', label: '已核对全文与图中信息，无未处理遗漏' },
  { key: 'relationships', label: '已核对行列、字段、条件与分支对应关系' }
]
const AUTO_REVIEW_LABELS = {
  'blank-page/v1': '无文本层、无图像与图形、无正文文块，判定为空白页',
  'no-anomaly/v1': '解析完整、无异常、无表格图示'
}
// 机器核验与人工核验是两种来源，人工作出的结论优先展示
// 解析器取不到文字时写入的占位正文，与后端 structure.py 的前缀保持一致
const PLACEHOLDER_PREFIXES = ['[空结构块', '[表格', '[图示']
function isPlaceholderText(text) {
  const value = String(text || "").trimStart()
  return !!value && PLACEHOLDER_PREFIXES.some((prefix) => value.startsWith(prefix))
}
function isHumanVerified(page) {
  return checks.every((check) => page?.checks?.[check.key] === true)
}
function isMachineVerified(page) {
  return !isHumanVerified(page) && !!page?.auto_review
}
function isVerified(page) {
  return isHumanVerified(page) || isMachineVerified(page)
}
function pageSuffix(page) {
  if (isHumanVerified(page)) return ' · 已核验'
  if (isMachineVerified(page)) return ' · 机器核验'
  return ''
}
function autoReviewLabel(record) {
  return AUTO_REVIEW_LABELS[record?.rule] || record?.rule || '解析器证据充分'
}
// 比对数字始终展示，机器核验的放行依据不隐藏
function pageEvidence(page) {
  const check = page?.native_text_check
  if (!check) return ''
  if (check.characters === 0) return '文本层比对：该页无可用文本层，无法做独立比对'
  // 只按去重字符集口径展示数字；旧记录按字符出现次数统计，不套用同一标签
  if (check.absent_characters === undefined) return ''
  const sample = check.absent_sample ? `，如 ${check.absent_sample}` : ''
  const coverage = ((check.coverage ?? 1) * 100).toFixed(2)
  return `文本层比对：覆盖 ${coverage}%（缺 ${check.absent_characters}/${check.characters} 个字符${sample}）`
}
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
  // 与后端 revise_structure 一致：正文被修改后机器核验不再覆盖当前内容
  delete currentPage.value.auto_review
}
// 页面说明是人工对该页的判断，写说明即接管核验，不再保留机器结论
function noteChanged() {
  delete currentPage.value.auto_review
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
  if (pageView.value) pageView.value.scrollTop = 0
  if (blockList.value) blockList.value.scrollTop = 0
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
/* 保存动作跟在核验项之后：审核动线在本页核验处结束，不必回到顶部 */
.page-actions {
  display: flex;
  justify-content: flex-end;
}
.issue {
  color: var(--color-text-secondary);
}
.review-summary,
.auto-review,
.evidence {
  color: var(--color-text-secondary);
}
.review-summary {
  font-weight: normal;
}
.auto-review {
  color: var(--color-primary);
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
