<template>
  <div class="source-section patient-source-section">
    <div class="section-title">患者病历来源 ({{ chunks.length }})</div>

    <div class="patient-doc-list" v-if="documentGroups.length > 0">
      <div v-for="group in documentGroups" :key="group.key" class="doc-group-item">
        <button
          class="doc-info"
          :aria-label="`查看 ${group.name} 的检索片段`"
          @click="openDocChunksModal(group)"
        >
          <FileText :size="15" class="doc-icon" />
          <span class="doc-name" :title="group.name">{{ group.name }}</span>
          <span class="chunk-count">{{ group.chunks.length }} 个片段</span>
        </button>
      </div>
    </div>

    <div v-else class="section-empty">未找到患者病历来源</div>

    <PatientRecordChunksModal
      v-model:open="chunksModalVisible"
      :document-name="selectedGroup?.name || ''"
      :chunks="selectedGroup?.chunks || []"
    />
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { FileText } from '@lucide/vue'
import PatientRecordChunksModal from '@/components/sources/PatientRecordChunksModal.vue'

const props = defineProps({
  chunks: {
    type: Array,
    default: () => []
  }
})

const chunksModalVisible = ref(false)
const selectedGroup = ref(null)

// 按病历文件聚合检索片段;文件按最高相似度排序,组内保持检索相关度顺序,
// 与知识库来源的分组展示保持同一交互口径
const documentGroups = computed(() => {
  const groups = new Map()
  for (const chunk of props.chunks) {
    if (!chunk || !chunk.content) continue
    const name =
      chunk.document_name ||
      (chunk.document_type ? `病历文书 (${chunk.document_type})` : '患者病历')
    const key = chunk.document_id || chunk.document_name || name
    if (!groups.has(key)) {
      groups.set(key, { key, name, bestScore: null, chunks: [] })
    }
    const group = groups.get(key)
    group.chunks.push(chunk)
    if (typeof chunk.score === 'number' && Number.isFinite(chunk.score)) {
      group.bestScore = group.bestScore === null ? chunk.score : Math.max(group.bestScore, chunk.score)
    }
  }
  return Array.from(groups.values()).sort((a, b) => {
    if (a.bestScore === b.bestScore) return a.name.localeCompare(b.name)
    // 无分数的组排在有分数的组之后
    if (a.bestScore === null) return 1
    if (b.bestScore === null) return -1
    return b.bestScore - a.bestScore
  })
})

const openDocChunksModal = (group) => {
  selectedGroup.value = group
  chunksModalVisible.value = true
}
</script>

<style scoped lang="less">
.source-section {
  .section-title {
    font-size: 12px;
    color: var(--gray-700);
    margin-bottom: 8px;
    font-weight: 600;
  }
}

.patient-source-section .section-title {
  color: var(--primary-color, #2563eb);
}

.patient-doc-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.doc-group-item {
  border: 1px solid rgba(37, 99, 235, 0.2);
  border-radius: 8px;
  background: var(--gray-0);
  padding: 6px 10px;
  transition: all 0.15s ease;

  &:hover {
    background: rgba(37, 99, 235, 0.04);
    border-color: rgba(37, 99, 235, 0.35);
  }
}

.doc-info {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 0;
  border: 0;
  background: transparent;
  text-align: left;
  cursor: pointer;

  &:focus-visible {
    outline: 2px solid var(--main-400);
    outline-offset: 2px;
    border-radius: 4px;
  }

  .doc-icon {
    flex-shrink: 0;
    color: var(--primary-color, #2563eb);
  }

  .doc-name {
    font-size: 13px;
    color: var(--gray-800);
    font-weight: 500;
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .chunk-count {
    font-size: 11px;
    color: var(--primary-color, #2563eb);
    background: rgba(37, 99, 235, 0.08);
    border: 1px solid rgba(37, 99, 235, 0.2);
    padding: 1px 6px;
    border-radius: 10px;
    white-space: nowrap;
  }
}

.section-empty {
  font-size: 12px;
  color: var(--gray-600);
}
</style>
