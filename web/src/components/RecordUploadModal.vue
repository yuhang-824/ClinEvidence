<template>
  <div v-if="visible" class="record-upload-overlay" role="dialog" aria-modal="true">
    <div class="record-upload-card">
      <div class="record-upload-header">
        <span>向患者库补充病历({{ displayCode }})</span>
        <button class="record-close" aria-label="关闭" @click="emit('close')">×</button>
      </div>
      <p class="record-hint">
        该患者已有的已发布病例无需上传,直接提问即可自动检索。此入口用于补充新的病历文件
        (如术后记录、随访资料),经归属确认与审核后发布为新快照。
      </p>

      <div v-if="!batch" class="record-upload-body">
        <input ref="fileInput" type="file" accept=".pdf,.doc,.docx" @change="onFileChange" />
        <label v-if="mode === 'patient' && documents.length" class="record-field">
          归属文档
          <select v-model="targetLogicalKey" class="record-input">
            <option value="">新建文档(首次上传的病例)</option>
            <option v-for="document in documents" :key="document.id" :value="document.logical_key">
              更新:{{ document.document_type }}(当前 v{{
                document.versions[document.versions.length - 1]?.version
              }})
            </option>
          </select>
        </label>
        <label class="record-field">
          文书类型
          <input v-model="documentType" class="record-input" placeholder="medical_record" />
        </label>
        <label class="record-field">
          就诊(可选)
          <select v-model="visitId" class="record-input">
            <option value="">尚未归属就诊</option>
            <option v-for="encounter in encounters" :key="encounter.id" :value="encounter.id">
              {{ encounter.encounter_type }} {{ encounter.started_at?.slice(0, 10) || '' }}
            </option>
          </select>
        </label>
        <label class="record-field">
          文书时间(可选,影响时间线)
          <input v-model="eventStartedAt" type="datetime-local" class="record-input" />
        </label>
        <button class="record-primary" :disabled="!file || uploading" @click="upload">
          {{ uploading ? '上传中…' : '上传并创建批次' }}
        </button>
        <div v-if="errorMessage" class="record-error">{{ errorMessage }}</div>
      </div>

      <div v-else class="record-status-body">
        <div class="status-line">批次 {{ batch.id.slice(0, 8) }}</div>
        <div class="status-grid">
          <div>状态:<b>{{ batchStatusLabel(batch) }}</b></div>
          <div>身份核验:<b>{{ batch.identity_status }}</b></div>
          <div>归属:<b>{{ batch.assignment_status }}</b></div>
          <div v-if="batch.error_code">错误:<b>{{ batch.error_code }}</b></div>
        </div>

        <button
          v-if="batch.status === 'identity_check' && batch.assignment_status !== 'confirmed'"
          class="record-primary"
          :disabled="confirming"
          @click="confirmAssignment"
        >
          {{ confirming ? '已确认,解析中…' : '确认归属(该文件属于当前患者)' }}
        </button>
        <div v-if="batch.status === 'identity_check' && batch.assignment_status === 'confirmed'" class="record-hint">
          归属已确认,正在解析病例(约 30-60 秒),完成后出现审核按钮。
        </div>

        <button
          v-if="batch.status === 'review_required'"
          class="record-primary"
          :disabled="advancing"
          @click="approveAndPublish"
        >
          {{ advancing ? '审核发布中:切块并向量编码,约需 1-2 分钟,请勿关闭…' : '审核通过并发布快照' }}
        </button>
        <div v-if="advancing" class="record-hint">
          正在调用嵌入模型为每个切块编码向量,完成后自动发布;期间可离开此弹窗,
          进度可在患者库的导入批次表中查看,重新点击将从未完成步骤继续(幂等)。
        </div>

        <div v-if="batch.status === 'published'" class="record-done">
          已发布快照 {{ batch.published_snapshot_id?.slice(0, 8) }},患者检索即刻可用。
        </div>
        <div v-if="batch.status === 'failed'" class="record-error">
          导入失败,可取消后重新上传;详情查看患者库批次表。
        </div>

        <button class="record-secondary" @click="reset">再传一份</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import { clinicalApi } from '@/apis/clinical_api'

const props = defineProps({
  visible: { type: Boolean, default: false },
  threadId: { type: String, default: '' },
  patientId: { type: String, default: '' },
  displayCode: { type: String, default: '' },
  mode: { type: String, default: 'thread' }
})
const emit = defineEmits(['close', 'published'])

const file = ref(null)
const documentType = ref('medical_record')
const visitId = ref('')
const eventStartedAt = ref('')
const encounters = ref([])
const uploading = ref(false)
const advancing = ref(false)
const batch = ref(null)
const errorMessage = ref('')
let pollTimer = null

const loadEncounters = async () => {
  if (!props.patientId) return
  try {
    encounters.value = await clinicalApi.listEncounters(props.patientId)
  } catch {
    encounters.value = []
  }
}

const loadDocuments = async () => {
  if (props.mode !== 'patient' || !props.patientId) {
    documents.value = []
    return
  }
  try {
    const library = await clinicalApi.getPatientLibrary(props.patientId)
    documents.value = library.documents
  } catch {
    documents.value = []
  }
}

watch(
  () => [props.visible, props.patientId],
  ([visible]) => {
    if (visible) {
      loadEncounters()
      loadDocuments()
    }
  },
  { immediate: true }
)

const statusLabel = (status) =>
  ({
    uploaded: '已上传',
    identity_check: '身份核验/等待归属确认',
    parsing: '解析中',
    review_required: '待审核',
    indexing: '索引中',
    verifying: '校验中',
    published: '已发布',
    failed: '失败',
    cancelled: '已取消',
    identity_conflict: '身份冲突'
  })[status] || status

const batchStatusLabel = (b) => {
  if (!b) return ''
  if (b.status === 'identity_check') {
    if (b.identity_status === 'pending') return '解析中(约 30-60 秒),完成后需确认归属'
    if (b.assignment_status === 'confirmed') return '归属已确认 · 解析中(约 30-60 秒)'
    return '等待归属确认'
  }
  return statusLabel(b.status)
}

const onFileChange = (event) => {
  file.value = event.target.files?.[0] || null
}

const reset = () => {
  batch.value = null
  file.value = null
  errorMessage.value = ''
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

const stopPolling = () => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

const startPolling = () => {
  stopPolling()
  pollTimer = setInterval(async () => {
    try {
      batch.value = await clinicalApi.getImportBatch(batch.value.id)
      if (['published', 'failed', 'identity_conflict', 'review_required', 'cancelled'].includes(batch.value.status)) {
        stopPolling()
      }
    } catch {
      stopPolling()
    }
  }, 3000)
}

const documents = ref([])
const targetLogicalKey = ref('')

const upload = async () => {
  if (!file.value) return
  uploading.value = true
  errorMessage.value = ''
  try {
    let batchResult
    if (props.mode === 'patient') {
      const tmp = await clinicalApi.uploadPatientTmp(props.patientId, file.value)
      batchResult = await clinicalApi.confirmPatientUpload(props.patientId, {
        tmp_file_ids: [tmp.tmp_file_id],
        document_type: documentType.value.trim() || 'medical_record',
        visit_id: visitId.value || undefined,
        event_started_at: eventStartedAt.value ? new Date(eventStartedAt.value).toISOString() : undefined,
        target_logical_key: targetLogicalKey.value || undefined
      })
    } else {
      const tmp = await clinicalApi.uploadRecordTmp(props.threadId, file.value)
      batchResult = await clinicalApi.confirmRecordUpload(props.threadId, {
        tmp_file_ids: [tmp.tmp_file_id],
        document_type: documentType.value.trim() || 'medical_record',
        visit_id: visitId.value || undefined,
        event_started_at: eventStartedAt.value ? new Date(eventStartedAt.value).toISOString() : undefined
      })
    }
    batch.value = batchResult
    if (['identity_check', 'parsing', 'uploaded'].includes(batch.value.status)) startPolling()
  } catch (error) {
    errorMessage.value = error?.response?.data?.detail || error?.message || '上传失败'
  } finally {
    uploading.value = false
  }
}

const confirming = ref(false)

const confirmAssignment = async () => {
  errorMessage.value = ''
  confirming.value = true
  try {
    batch.value = await clinicalApi.confirmBatchAssignment(batch.value.id, 'manual')
    startPolling()
  } catch (error) {
    errorMessage.value = error?.response?.data?.detail || '归属确认失败'
  } finally {
    confirming.value = false
  }
}

const approveAndPublish = async () => {
  if (!batch.value) return
  advancing.value = true
  errorMessage.value = ''
  try {
    // 服务端一键收口:审核 → 建块 → 写向量 → 发布;每步幂等,可重试
    const result = await clinicalApi.finalizeBatch(batch.value.id)
    batch.value = result.batch
    emit('published', batch.value)
  } catch (error) {
    errorMessage.value = error?.response?.data?.detail || error?.message || '发布失败'
  } finally {
    advancing.value = false
  }
}

defineExpose({ reset })
</script>

<style scoped>
.record-upload-overlay {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.4);
}

.record-upload-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: min(460px, calc(100vw - 32px));
  max-height: 80vh;
  overflow-y: auto;
  padding: 16px;
  border-radius: 12px;
  background: var(--bg-color, #fff);
  color: var(--text-color, #1f2329);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.18);
}

.record-upload-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 600;
}

.record-close {
  border: none;
  background: none;
  font-size: 18px;
  cursor: pointer;
  color: inherit;
}

.record-hint {
  margin: 0;
  font-size: 12px;
  opacity: 0.7;
}

.record-upload-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.record-upload-body input[type='file'] {
  font-size: 12px;
}

.record-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
}

.record-input {
  padding: 8px 10px;
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  background: none;
  color: inherit;
}

.record-primary {
  padding: 9px 14px;
  border: none;
  border-radius: 8px;
  background: var(--primary-color, #2563eb);
  color: #fff;
  cursor: pointer;
}

.record-primary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.record-secondary {
  padding: 8px 12px;
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  background: none;
  color: inherit;
  cursor: pointer;
}

.record-error {
  color: var(--danger-color, #d93026);
  font-size: 12px;
}

.record-status-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.status-line {
  font-family: monospace;
  font-size: 12px;
  opacity: 0.7;
}

.status-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  font-size: 13px;
}

.record-done {
  color: var(--success-color, #1a9e5c);
  font-size: 13px;
}
</style>
