<template>
  <div class="patient-library">
    <div class="library-side">
      <div class="library-side-title">患者库</div>
      <button
        v-for="patient in patients"
        :key="patient.id"
        class="patient-item"
        :class="{ active: patient.id === selectedId }"
        @click="selectPatient(patient.id)"
      >
        <span class="patient-code">{{ patient.display_code }}</span>
        <span class="patient-flag">{{ patient.current_snapshot_id ? '有快照' : '无快照' }}</span>
      </button>
      <div v-if="!patients.length && !loading" class="library-empty">暂无可访问的患者</div>
    </div>

    <div class="library-main">
      <div v-if="!selected" class="library-placeholder">选择左侧患者查看病例库</div>
      <template v-else>
        <div class="detail-header">
          <div>
            <div class="detail-code">{{ library.patient.display_code }}</div>
            <div class="detail-sub">
              状态 {{ library.patient.status }} · 当前快照
              {{ library.snapshots.find((s) => s.is_current)?.sequence ?? '无' }}
            </div>
          </div>
          <button class="refresh-btn" @click="loadLibrary">刷新</button>
        </div>

        <section class="detail-section">
          <h3>时间线(快照演进)</h3>
          <div class="timeline">
            <div
              v-for="snapshot in library.snapshots"
              :key="snapshot.id"
              class="timeline-item"
              :class="{ superseded: snapshot.status !== 'published' }"
            >
              <div class="timeline-dot"></div>
              <div class="timeline-body">
                <div class="timeline-title">
                  快照 #{{ snapshot.sequence }}
                  <span v-if="snapshot.is_current" class="tag current">当前</span>
                  <span v-else class="tag">历史</span>
                </div>
                <div class="timeline-sub">
                  发布于 {{ snapshot.published_at || '—' }} · {{ snapshot.member_count }} 份文档 ·
                  {{ snapshot.status === 'published' ? '可检索' : '已被新版本取代' }}
                </div>
              </div>
            </div>
            <div v-if="!library.snapshots.length" class="section-empty">尚无已发布快照</div>
          </div>
        </section>

        <section class="detail-section">
          <h3>病例文档(版本演进)</h3>
          <div v-for="document in library.documents" :key="document.id" class="document-card">
            <div class="document-title">
              {{ document.document_type }}
              <span class="document-key">{{ document.logical_key }}</span>
              <span v-if="document.event_started_at" class="document-key">
                文书时间 {{ document.event_started_at.slice(0, 10) }}
              </span>
            </div>
            <div v-for="version in document.versions" :key="version.id" class="version-row">
              <span class="version-badge">v{{ version.version }}</span>
              <span class="version-meta">
                上传 {{ version.uploaded_at?.slice(0, 19).replace('T', ' ') }} · {{ version.status }} ·
                hash {{ version.content_hash }}
              </span>
              <span
                v-for="revision in version.revisions"
                :key="revision.id"
                class="revision-pill"
                :class="revision.status"
              >
                修订 r{{ revision.revision_version }} · {{ revision.status }}
                <template v-if="revision.status === 'approved'"> · {{ revision.chunk_count }} 块</template>
                <button class="chunk-view-btn" @click="viewChunks(revision.id)">查看切块</button>
              </span>
            </div>
          </div>
          <div v-if="!library.documents.length" class="section-empty">暂无病例文档</div>
        </section>

        <section class="detail-section">
          <h3>导入批次</h3>
          <table v-if="library.batches.length" class="batch-table">
            <thead>
              <tr>
                <th>批次</th><th>来源</th><th>状态</th><th>身份</th><th>归属</th><th>错误</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="batch in library.batches" :key="batch.id">
                <td class="mono">{{ batch.id.slice(0, 8) }}</td>
                <td>{{ batch.source_kind }}</td>
                <td>{{ batch.status }}</td>
                <td>{{ batch.identity_status }}</td>
                <td>{{ batch.assignment_status }}</td>
                <td>{{ batch.error_code || '—' }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="section-empty">暂无导入批次</div>
        </section>
      </template>
    </div>

    <div v-if="chunkPanel.visible" class="chunk-overlay" @click.self="chunkPanel.visible = false">
      <div class="chunk-panel">
        <div class="chunk-panel-header">
          <span>切块明细(共 {{ chunkPanel.chunks.length }} 块)</span>
          <button class="chunk-close" @click="chunkPanel.visible = false">×</button>
        </div>
        <div v-for="chunk in chunkPanel.chunks" :key="chunk.chunk_id" class="chunk-item">
          <div class="chunk-meta">
            #{{ chunk.chunk_index }} · 第 {{ chunk.page_number ?? '?' }} 页 · 字符
            {{ chunk.char_start }}–{{ chunk.char_end }}
          </div>
          <pre class="chunk-content">{{ chunk.content }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { clinicalApi } from '@/apis/clinical_api'

const patients = ref([])
const selectedId = ref('')
const selected = ref(null)
const library = ref(null)
const loading = ref(false)
const chunkPanel = ref({ visible: false, chunks: [] })

const loadPatients = async () => {
  loading.value = true
  try {
    patients.value = await clinicalApi.listPatients()
    if (patients.value.length && !selectedId.value) selectPatient(patients.value[0].id)
  } finally {
    loading.value = false
  }
}

const selectPatient = async (patientId) => {
  selectedId.value = patientId
  selected.value = patients.value.find((p) => p.id === patientId) || null
  await loadLibrary()
}

const loadLibrary = async () => {
  if (!selectedId.value) return
  library.value = await clinicalApi.getPatientLibrary(selectedId.value)
}

const viewChunks = async (revisionId) => {
  const detail = await clinicalApi.getRevisionChunks(revisionId)
  chunkPanel.value = { visible: true, chunks: detail.chunks }
}

loadPatients()
</script>

<style scoped>
.patient-library {
  display: flex;
  height: calc(100vh - 60px);
}

.library-side {
  width: 220px;
  border-right: 1px solid var(--border-color, #e5e6eb);
  overflow-y: auto;
  padding: 12px;
}

.library-side-title {
  font-weight: 600;
  margin-bottom: 12px;
}

.patient-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
  padding: 10px;
  margin-bottom: 6px;
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  background: none;
  color: inherit;
  cursor: pointer;
}

.patient-item.active {
  border-color: var(--primary-color, #2563eb);
}

.patient-code {
  font-weight: 600;
  font-family: monospace;
}

.patient-flag {
  font-size: 11px;
  opacity: 0.65;
}

.library-empty,
.library-placeholder,
.section-empty {
  padding: 24px;
  text-align: center;
  opacity: 0.6;
  font-size: 13px;
}

.library-main {
  flex: 1;
  overflow-y: auto;
  padding: 20px 28px;
}

.detail-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.detail-code {
  font-size: 22px;
  font-weight: 700;
  font-family: monospace;
}

.detail-sub {
  font-size: 12px;
  opacity: 0.7;
  margin-top: 4px;
}

.refresh-btn {
  padding: 6px 14px;
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  background: none;
  color: inherit;
  cursor: pointer;
}

.detail-section {
  margin-bottom: 26px;
}

.detail-section h3 {
  font-size: 14px;
  margin: 0 0 10px;
}

.timeline {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.timeline-item {
  display: flex;
  gap: 12px;
  position: relative;
  padding-bottom: 14px;
}

.timeline-item:not(:last-child)::before {
  content: '';
  position: absolute;
  left: 5px;
  top: 16px;
  bottom: 0;
  width: 2px;
  background: var(--border-color, #e5e6eb);
}

.timeline-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--primary-color, #2563eb);
  margin-top: 3px;
  flex-shrink: 0;
}

.timeline-item.superseded .timeline-dot {
  background: var(--border-color, #b9bdc4);
}

.timeline-title {
  font-weight: 600;
  font-size: 13px;
}

.timeline-sub {
  font-size: 12px;
  opacity: 0.7;
}

.tag {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  background: var(--border-color, #eceef1);
  margin-left: 6px;
}

.tag.current {
  background: rgba(37, 99, 235, 0.12);
  color: var(--primary-color, #2563eb);
}

.document-card {
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 10px;
  padding: 12px 14px;
  margin-bottom: 10px;
}

.document-title {
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 8px;
}

.document-key {
  font-family: monospace;
  font-size: 11px;
  opacity: 0.6;
  margin-left: 8px;
}

.version-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 6px 0;
  font-size: 12px;
}

.version-badge {
  font-weight: 700;
  font-family: monospace;
}

.version-meta {
  opacity: 0.7;
}

.revision-pill {
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  padding: 2px 8px;
}

.revision-pill.approved {
  border-color: rgba(34, 154, 92, 0.5);
}

.chunk-view-btn {
  border: none;
  background: none;
  color: var(--primary-color, #2563eb);
  cursor: pointer;
  font-size: 12px;
  margin-left: 6px;
}

.batch-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.batch-table th,
.batch-table td {
  text-align: left;
  padding: 6px 10px;
  border-bottom: 1px solid var(--border-color, #e5e6eb);
}

.mono {
  font-family: monospace;
}

.chunk-overlay {
  position: fixed;
  inset: 0;
  z-index: 2100;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
}

.chunk-panel {
  width: min(760px, calc(100vw - 48px));
  max-height: 80vh;
  overflow-y: auto;
  background: var(--bg-color, #fff);
  color: var(--text-color, #1f2329);
  border-radius: 12px;
  padding: 16px;
}

.chunk-panel-header {
  display: flex;
  justify-content: space-between;
  font-weight: 600;
  margin-bottom: 12px;
}

.chunk-close {
  border: none;
  background: none;
  font-size: 18px;
  cursor: pointer;
  color: inherit;
}

.chunk-item {
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  padding: 10px;
  margin-bottom: 10px;
}

.chunk-meta {
  font-size: 11px;
  opacity: 0.65;
  margin-bottom: 6px;
}

.chunk-content {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  font-family: inherit;
}
</style>
