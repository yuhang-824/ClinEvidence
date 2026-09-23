<template>
  <div class="patient-library" @click="contextMenu.visible = false">
    <datalist id="patient-category-options">
      <option v-for="category in categoryOptions" :key="category" :value="category" />
    </datalist>
    <div class="library-side">
      <div class="library-side-header">
        <div class="library-side-title">患者库</div>
        <button class="add-patient-btn" title="新增患者" @click="openCreate">＋</button>
      </div>
      <button
        v-if="selectedCategory !== undefined"
        type="button"
        class="category-back"
        @click="selectCategory(undefined)"
      >
        ‹ 返回病种分类
      </button>
      <div v-if="createPanel.visible" class="create-panel">
        <label class="category-label">
          病种
          <input
            v-model="createPanel.category"
            class="record-input"
            list="patient-category-options"
            placeholder="选择已有病种或输入新病种"
            maxlength="32"
          />
        </label>
        <input
          v-model="createPanel.displayCode"
          class="record-input"
          placeholder="脱敏编号,留空自动生成"
          maxlength="64"
          @keyup.enter="createPatient"
        />
        <div class="create-actions">
          <button class="record-secondary" @click="createPanel.visible = false">取消</button>
          <button
            class="record-primary"
            :disabled="createPanel.creating || !createPanel.category.trim()"
            @click="createPatient"
          >
            {{ createPanel.creating ? '创建中…' : '创建' }}
          </button>
        </div>
        <div v-if="createPanel.error" class="record-error">{{ createPanel.error }}</div>
      </div>
      <div v-if="loading" class="library-empty">正在加载患者...</div>
      <div v-else-if="patientsError" class="library-empty">
        {{ patientsError }} <button class="record-secondary" @click="loadPatients">重试</button>
      </div>
      <template v-else-if="selectedCategory === undefined">
        <button
          v-for="group in categoryGroups"
          :key="group.key"
          type="button"
          class="category-item"
          :title="group.category || '未分类'"
          @click="selectCategory(group.category)"
        >
          <span>{{ group.category || '未分类' }}</span>
          <span class="patient-flag">{{ group.patients.length }} 位患者</span>
        </button>
        <div v-if="!categoryGroups.length" class="library-empty">暂无可访问的患者</div>
      </template>
      <template v-else>
        <div class="category-heading">{{ selectedCategory || '未分类' }}</div>
        <button
          v-for="patient in visiblePatients"
          :key="patient.id"
          type="button"
          class="patient-item"
          :class="{ active: patient.id === selectedId }"
          @click="selectPatient(patient.id)"
          @contextmenu.prevent="openContextMenu($event, patient)"
        >
          <span class="patient-code">{{ patient.display_code }}</span>
          <span class="patient-flag">{{ patient.current_snapshot_id ? '有快照' : '无快照' }}</span>
        </button>
        <div v-if="!visiblePatients.length" class="library-empty">该病种暂无患者</div>
      </template>
    </div>

    <div
      v-if="contextMenu.visible"
      class="context-menu"
      :style="{ left: contextMenu.x + 'px', top: contextMenu.y + 'px' }"
    >
      <button class="context-item danger" @click="askDeletePatient">删除患者</button>
    </div>

    <div v-if="deleteConfirm.visible" class="chunk-overlay" @click.self="deleteConfirm.visible = false">
      <div class="chunk-panel delete-panel">
        <div class="chunk-panel-header">
          <span>删除患者 {{ deleteConfirm.displayCode }}</span>
          <button class="chunk-close" @click="deleteConfirm.visible = false">×</button>
        </div>
        <p class="delete-warning">
          将永久删除该患者的全部病例文档、版本、切块、快照与向量,绑定该患者的会话将无法再检索其资料。此操作不可撤销。
        </p>
        <label class="delete-confirm-field">
          输入患者编号 <b>{{ deleteConfirm.displayCode }}</b> 以确认
          <input v-model="deleteConfirm.input" class="record-input" placeholder="输入编号" />
        </label>
        <div class="delete-actions">
          <button class="record-secondary" @click="deleteConfirm.visible = false">取消</button>
          <button
            class="record-primary delete-btn"
            :disabled="deleteConfirm.input !== deleteConfirm.displayCode || deleting"
            @click="deletePatient"
          >
            {{ deleting ? '删除中…' : '永久删除' }}
          </button>
        </div>
        <div v-if="deleteConfirm.error" class="record-error">{{ deleteConfirm.error }}</div>
      </div>
    </div>

    <div class="library-main">
      <div v-if="!selected" class="library-placeholder">
        {{ selectedCategory === undefined ? '选择左侧病种查看患者' : '选择左侧患者查看病例库' }}
      </div>
      <div v-else-if="!library" class="library-placeholder">
        {{ libraryError || '正在加载患者病例库...' }}
      </div>
      <template v-else>
        <div class="detail-header">
          <div>
            <div class="detail-code">{{ library.patient.display_code }}</div>
            <label class="detail-category">
              病种
              <input
                v-model="categoryDraft"
                list="patient-category-options"
                maxlength="32"
                :disabled="categorySaving || library.patient.owner_uid !== userStore.uid"
                placeholder="选择或输入病种"
                @change="updatePatientCategory"
              />
            </label>
            <div v-if="categoryError" class="record-error">{{ categoryError }}</div>
            <div v-if="libraryError" class="record-error">{{ libraryError }}</div>
            <div class="detail-sub">
              状态 {{ library.patient.status }} · 当前快照
              {{ library.snapshots.find((s) => s.is_current)?.sequence ?? '无' }}
            </div>
          </div>
          <div class="header-actions">
            <button class="record-secondary upload-btn" @click="uploadVisible = true">补充病历</button>
            <button
            class="refresh-btn"
            @click="loadPatients().then(() => selectedId && loadLibrary())"
          >
            刷新
          </button>
          </div>
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

    <RecordUploadModal
      mode="patient"
      :visible="uploadVisible"
      :patient-id="selectedId"
      :display-code="selected?.display_code || ''"
      @close="uploadVisible = false"
      @published="loadLibrary"
    />

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
import { computed, ref } from 'vue'
import { clinicalApi } from '@/apis/clinical_api'
import RecordUploadModal from '@/components/RecordUploadModal.vue'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()
const patients = ref([])
const selectedId = ref('')
const selected = ref(null)
const library = ref(null)
const loading = ref(false)
const patientsError = ref('')
const libraryError = ref('')
const chunkPanel = ref({ visible: false, chunks: [] })
const uploadVisible = ref(false)
const selectedCategory = ref(undefined)
const patientCategories = ['内膜癌', '宫颈癌', '卵巢癌']
const categoryDraft = ref('')
const categoryError = ref('')
const categorySaving = ref(false)
const categoryOptions = computed(() =>
  [...new Set([...patientCategories, ...patients.value.map((patient) => patient.category).filter(Boolean)])]
    .sort((left, right) => left.localeCompare(right, 'zh-CN'))
)
const categoryGroups = computed(() => {
  const groups = new Map()
  for (const patient of patients.value) {
    const category = patient.category || null
    if (!groups.has(category)) groups.set(category, [])
    groups.get(category).push(patient)
  }
  return [...groups].map(([category, members]) => ({
    key: category === null ? 'unclassified' : `category:${category}`,
    category,
    patients: members
  })).sort((left, right) => {
    if (left.category === null && right.category === null) return 0
    if (left.category === null) return 1
    if (right.category === null) return -1
    return left.category.localeCompare(right.category, 'zh-CN')
  })
})
const visiblePatients = computed(() =>
  patients.value.filter((patient) => (patient.category || null) === selectedCategory.value)
)

/** 进入病种大类或返回分类首页。 */
const selectCategory = (category) => {
  selectedCategory.value = category
  selectedId.value = ''
  selected.value = null
  library.value = null
  libraryError.value = ''
  categoryError.value = ''
  createPanel.value.visible = false
}

const loadPatients = async () => {
  loading.value = true
  patientsError.value = ''
  try {
    patients.value = (await clinicalApi.listPatients()).sort((left, right) =>
      left.display_code.localeCompare(right.display_code, 'zh-CN', { numeric: true })
    )
    if (selectedId.value) {
      selected.value = patients.value.find((patient) => patient.id === selectedId.value) || null
      if (selected.value) selectedCategory.value = selected.value.category || null
      else selectCategory(undefined)
    }
  } catch (error) {
    patientsError.value = error?.message || '患者列表加载失败'
  } finally {
    loading.value = false
  }
}

const selectPatient = async (patientId) => {
  selectedId.value = patientId
  selected.value = patients.value.find((p) => p.id === patientId) || null
  categoryDraft.value = selected.value?.category || ''
  categoryError.value = ''
  library.value = null
  await loadLibrary()
}

const loadLibrary = async () => {
  const patientId = selectedId.value
  if (!patientId) return
  libraryError.value = ''
  try {
    const nextLibrary = await clinicalApi.getPatientLibrary(patientId)
    if (selectedId.value !== patientId) return
    library.value = nextLibrary
    categoryDraft.value = nextLibrary.patient.category || ''
    // 快照可能在本页之外(如会话上传)发布,详情拉取后同步列表徽标
    const item = patients.value.find((patient) => patient.id === patientId)
    if (item) item.current_snapshot_id = nextLibrary.patient.current_snapshot_id
  } catch (error) {
    if (selectedId.value === patientId) libraryError.value = error?.message || '病例库加载失败'
  }
}

const viewChunks = async (revisionId) => {
  const detail = await clinicalApi.getRevisionChunks(revisionId)
  chunkPanel.value = { visible: true, chunks: detail.chunks }
}

// 新增患者:编号可留空由服务端自动生成;创建后选中新患者
const createPanel = ref({ visible: false, displayCode: '', category: '', creating: false, error: '' })

const openCreate = () => {
  createPanel.value = {
    visible: true,
    displayCode: '',
    category: typeof selectedCategory.value === 'string' ? selectedCategory.value : '',
    creating: false,
    error: ''
  }
}

const createPatient = async () => {
  if (!createPanel.value.category.trim() || createPanel.value.creating) return
  createPanel.value.creating = true
  createPanel.value.error = ''
  try {
    const patient = await clinicalApi.createPatient({
      displayCode: createPanel.value.displayCode.trim() || null,
      category: createPanel.value.category.trim()
    })
    createPanel.value.visible = false
    patients.value = [...patients.value, patient].sort((left, right) =>
      left.display_code.localeCompare(right.display_code, 'zh-CN', { numeric: true })
    )
    selectCategory(patient.category)
    await selectPatient(patient.id)
  } catch (error) {
    createPanel.value.error = error?.response?.data?.detail || error?.message || '创建失败'
  } finally {
    createPanel.value.creating = false
  }
}

const updatePatientCategory = async () => {
  if (!selected.value || selected.value.owner_uid !== userStore.uid || categorySaving.value) return
  const category = categoryDraft.value.trim()
  if (!category) {
    categoryError.value = '病种不能为空'
    return
  }
  if (category === selected.value.category) return
  const patientId = selected.value.id
  categorySaving.value = true
  categoryError.value = ''
  try {
    const updated = await clinicalApi.updatePatient(patientId, { category })
    const item = patients.value.find((patient) => patient.id === patientId)
    if (item) Object.assign(item, updated)
    if (selectedId.value === patientId) {
      if (library.value?.patient) Object.assign(library.value.patient, updated)
      selectedCategory.value = updated.category
      categoryDraft.value = updated.category
    }
  } catch (error) {
    if (selectedId.value === patientId) {
      categoryError.value = error?.response?.data?.detail || error?.message || '病种更新失败'
      categoryDraft.value = selected.value.category || ''
    }
  } finally {
    categorySaving.value = false
  }
}

// 右键菜单与删除:删除仅 Owner 可执行,需输入编号二次确认
const contextMenu = ref({ visible: false, x: 0, y: 0, patient: null })
const deleteConfirm = ref({ visible: false, patient: null, displayCode: '', input: '', error: '' })
const deleting = ref(false)

const openContextMenu = (event, patient) => {
  contextMenu.value = { visible: true, x: event.clientX, y: event.clientY, patient }
}

const askDeletePatient = () => {
  const patient = contextMenu.value.patient
  contextMenu.value = { ...contextMenu.value, visible: false }
  if (!patient) return
  deleteConfirm.value = {
    visible: true,
    patient,
    displayCode: patient.display_code,
    input: '',
    error: ''
  }
}

const deletePatient = async () => {
  if (!deleteConfirm.value.patient) return
  deleting.value = true
  deleteConfirm.value.error = ''
  try {
    await clinicalApi.deletePatient(deleteConfirm.value.patient.id)
    deleteConfirm.value.visible = false
    if (selectedId.value === deleteConfirm.value.patient.id) {
      selectedId.value = ''
      selected.value = null
      library.value = null
    }
    patients.value = patients.value.filter((p) => p.id !== deleteConfirm.value.patient.id)
  } catch (error) {
    deleteConfirm.value.error = error?.response?.data?.detail || error?.message || '删除失败'
  } finally {
    deleting.value = false
  }
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

.library-side-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.add-patient-btn {
  width: 26px;
  height: 26px;
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  background: none;
  color: inherit;
  cursor: pointer;
  font-size: 15px;
  line-height: 1;
}

.add-patient-btn:hover {
  border-color: var(--primary-color, #2563eb);
  color: var(--primary-color, #2563eb);
}

.category-back {
  margin-bottom: 10px;
  padding: 4px 0;
  border: 0;
  background: transparent;
  color: var(--main-color);
  cursor: pointer;
  font-size: 12px;
}

.category-heading {
  padding: 4px 10px 10px;
  color: var(--color-text-secondary, inherit);
  font-size: 12px;
  font-weight: 600;
}

.create-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  margin-bottom: 10px;
  border: 1px solid var(--primary-color, #2563eb);
  border-radius: 8px;
}

.category-label,
.detail-category {
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: var(--color-text-secondary, inherit);
  font-size: 12px;
}

.detail-category input {
  min-width: 120px;
  padding: 5px 8px;
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 6px;
  background: var(--gray-0, transparent);
  color: inherit;
}

.create-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.record-input {
  padding: 8px 10px;
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  background: none;
  color: inherit;
  font-size: 12px;
}

.record-secondary {
  padding: 6px 12px;
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  background: none;
  color: inherit;
  cursor: pointer;
  font-size: 12px;
}

.record-primary {
  padding: 6px 12px;
  border: none;
  border-radius: 8px;
  background: var(--primary-color, #2563eb);
  color: #fff;
  cursor: pointer;
  font-size: 12px;
}

.record-primary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.record-error {
  color: var(--danger-color, #d93026);
  font-size: 12px;
}

.category-item,
.patient-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 10px;
  margin-bottom: 6px;
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  background: none;
  color: inherit;
  cursor: pointer;
}

.category-item {
  font-weight: 600;
}

.category-item span:first-child {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.category-item:hover,
.patient-item:hover {
  border-color: var(--main-color);
}

.category-back:focus-visible,
.category-item:focus-visible,
.patient-item:focus-visible {
  outline: 2px solid var(--main-color);
  outline-offset: 2px;
}

.patient-item.active {
  border-color: var(--primary-color, #2563eb);
}

.patient-code {
  min-width: 0;
  overflow: hidden;
  font-weight: 600;
  font-family: monospace;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.patient-flag {
  font-size: 11px;
  opacity: 0.65;
  white-space: nowrap;
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
  flex-wrap: wrap;
  gap: 12px;
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

.header-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.upload-btn {
  background: var(--primary-color, #2563eb);
  color: #fff;
  border-color: var(--primary-color, #2563eb);
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

.context-menu {
  position: fixed;
  z-index: 2200;
  min-width: 120px;
  padding: 4px;
  background: var(--bg-color, #fff);
  color: var(--text-color, #1f2329);
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.14);
}

.context-item {
  display: block;
  width: 100%;
  padding: 8px 12px;
  border: none;
  border-radius: 6px;
  background: none;
  color: inherit;
  text-align: left;
  cursor: pointer;
  font-size: 13px;
}

.context-item:hover {
  background: var(--border-color, #f0f1f3);
}

.context-item.danger {
  color: var(--danger-color, #d93026);
}

.delete-panel {
  width: min(440px, calc(100vw - 48px));
}

.delete-warning {
  margin: 0 0 12px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--danger-color, #d93026);
}

.delete-confirm-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  margin-bottom: 14px;
}

.delete-confirm-field input {
  padding: 8px 10px;
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  background: none;
  color: inherit;
}

.delete-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.delete-btn {
  background: var(--danger-color, #d93026);
}

.record-secondary {
  padding: 8px 14px;
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  background: none;
  color: inherit;
  cursor: pointer;
}

.record-primary {
  padding: 8px 14px;
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

.record-input {
  padding: 8px 10px;
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  background: none;
  color: inherit;
}

.record-error {
  color: var(--danger-color, #d93026);
  font-size: 12px;
}
</style>
