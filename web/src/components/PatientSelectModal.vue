<template>
  <div v-if="visible" class="patient-select-overlay" role="dialog" aria-modal="true">
    <div class="patient-select-card">
      <div class="patient-select-header">
        <span>选择患者</span>
        <button class="patient-select-close" aria-label="关闭" @click="emit('close')">×</button>
      </div>
      <p class="patient-select-hint">诊疗会话将绑定一名患者且创建后不可更换;查看其他患者请新建会话。</p>
      <div class="patient-select-body">
        <div v-if="loading" class="patient-select-state">加载中…</div>
        <div v-else-if="errorMessage" class="patient-select-state patient-select-error">{{ errorMessage }}</div>
        <div v-else-if="!patients.length" class="patient-select-state">暂无可访问的患者</div>
        <ul v-else class="patient-select-list">
          <li v-for="patient in patients" :key="patient.id">
            <button
              class="patient-select-item"
              :disabled="patient.status !== 'active'"
              @click="emit('select', patient)"
            >
              <span class="patient-code">{{ patient.display_code }}</span>
              <span class="patient-status">{{ patient.status === 'active' ? '可接诊' : patient.status }}</span>
            </button>
          </li>
        </ul>
      </div>
      <div class="patient-select-footer">
        <div class="patient-create-fields">
          <input
            v-model="newDisplayCode"
            class="patient-select-input"
            aria-label="新患者脱敏编号"
            placeholder="脱敏编号(可留空自动生成)"
            maxlength="64"
          />
          <input
            v-model="newCategory"
            class="patient-select-input"
            aria-label="患者病种"
            list="clinical-patient-category-options"
            placeholder="选择或输入病种"
            maxlength="32"
          />
          <datalist id="clinical-patient-category-options">
            <option v-for="category in categoryOptions" :key="category" :value="category" />
          </datalist>
        </div>
        <button
          class="patient-select-create"
          :disabled="creating || !newCategory.trim()"
          @click="createPatient"
        >
          {{ creating ? '创建中…' : '创建患者' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { clinicalApi } from '@/apis/clinical_api'

const props = defineProps({
  visible: { type: Boolean, default: false }
})
const emit = defineEmits(['select', 'close'])

const patients = ref([])
const loading = ref(false)
const errorMessage = ref('')
const newDisplayCode = ref('')
const creating = ref(false)
const PATIENT_CATEGORIES = ['内膜癌', '宫颈癌', '卵巢癌']
const newCategory = ref('')
const categoryOptions = computed(() =>
  [...new Set([...PATIENT_CATEGORIES, ...patients.value.map((patient) => patient.category).filter(Boolean)])]
    .sort((left, right) => left.localeCompare(right, 'zh-CN'))
)

const loadPatients = async () => {
  loading.value = true
  errorMessage.value = ''
  try {
    patients.value = (await clinicalApi.listPatients()).sort((left, right) =>
      left.display_code.localeCompare(right.display_code, 'zh-CN', { numeric: true })
    )
  } catch (error) {
    errorMessage.value = error?.message || '患者列表加载失败'
  } finally {
    loading.value = false
  }
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) loadPatients()
  }
)

const createPatient = async () => {
  if (!newCategory.value.trim() || creating.value) return
  creating.value = true
  errorMessage.value = ''
  try {
    const patient = await clinicalApi.createPatient({
      displayCode: newDisplayCode.value.trim() || null,
      category: newCategory.value.trim()
    })
    newDisplayCode.value = ''
    newCategory.value = ''
    patients.value = [...patients.value, patient].sort((left, right) =>
      left.display_code.localeCompare(right.display_code, 'zh-CN', { numeric: true })
    )
    emit('select', patient)
  } catch (error) {
    errorMessage.value = error?.message || '患者创建失败'
  } finally {
    creating.value = false
  }
}
</script>

<style scoped>
.patient-select-overlay {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.4);
}

.patient-select-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: min(420px, calc(100vw - 32px));
  max-height: 70vh;
  padding: 16px;
  border-radius: 12px;
  background: var(--bg-color, #fff);
  color: var(--text-color, #1f2329);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.18);
}

.patient-select-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 16px;
  font-weight: 600;
}

.patient-select-close {
  border: none;
  background: none;
  font-size: 18px;
  cursor: pointer;
  color: inherit;
}

.patient-select-hint {
  margin: 0;
  font-size: 12px;
  opacity: 0.7;
}

.patient-select-body {
  flex: 1;
  overflow-y: auto;
  min-height: 96px;
}

.patient-select-state {
  padding: 24px 0;
  text-align: center;
  font-size: 13px;
  opacity: 0.7;
}

.patient-select-error {
  color: var(--danger-color, #d93026);
  opacity: 1;
}

.patient-select-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.patient-select-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  background: none;
  color: inherit;
  cursor: pointer;
  text-align: left;
}

.patient-select-item:hover:not(:disabled) {
  border-color: var(--primary-color, #2563eb);
}

.patient-select-item:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.patient-code {
  font-weight: 600;
  font-family: monospace;
}

.patient-status {
  font-size: 12px;
  opacity: 0.7;
}

.patient-select-footer {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.patient-create-fields {
  display: flex;
  flex: 1 1 100%;
  gap: 8px;
}

.patient-select-input {
  flex: 1;
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--border-color, #e5e6eb);
  border-radius: 8px;
  background: none;
  color: inherit;
}

.patient-select-create {
  padding: 8px 14px;
  border: none;
  border-radius: 8px;
  background: var(--primary-color, #2563eb);
  color: #fff;
  cursor: pointer;
}

.patient-select-create:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
