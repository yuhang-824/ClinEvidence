<template>
  <div class="source-section patient-source-section">
    <div class="section-title">患者病历来源 ({{ chunks.length }})</div>
    <div class="patient-chunk-list">
      <div v-for="(chunk, index) in chunks" :key="chunk.chunk_id || index" class="patient-chunk-item">
        <div class="patient-chunk-meta">
          <span class="channel-tag">患者病历</span>
          <span class="doc-type">{{ chunk.document_type || '病历' }}</span>
          <span v-if="chunk.page_number" class="doc-page">第 {{ chunk.page_number }} 页</span>
          <span v-if="chunk.snapshot_sequence" class="doc-snapshot">快照 #{{ chunk.snapshot_sequence }}</span>
        </div>
        <pre class="patient-chunk-content">{{ chunk.content }}</pre>
      </div>
    </div>
    <div v-if="!chunks.length" class="section-empty">未找到患者病历来源</div>
  </div>
</template>

<script setup>
defineProps({
  chunks: {
    type: Array,
    default: () => []
  }
})
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

.patient-chunk-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.patient-chunk-item {
  border: 1px solid rgba(37, 99, 235, 0.25);
  border-radius: 8px;
  padding: 8px 10px;
  background: rgba(37, 99, 235, 0.03);
}

.patient-chunk-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 11px;
  margin-bottom: 6px;
}

.channel-tag {
  padding: 1px 8px;
  border-radius: 9px;
  background: rgba(37, 99, 235, 0.12);
  color: var(--primary-color, #2563eb);
  font-weight: 600;
}

.doc-type,
.doc-page,
.doc-snapshot {
  color: var(--gray-700);
}

.patient-chunk-content {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  font-family: inherit;
  max-height: 140px;
  overflow-y: auto;
}

.section-empty {
  font-size: 12px;
  color: var(--gray-600);
}
</style>
