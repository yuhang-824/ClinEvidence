<template>
  <section class="chunk-repair">
    <h4>调整切片边界</h4>
    <p>选择片段后可合并，或在正文中放置光标后拆分。这里只调整边界；识别错误请修改清洗稿。</p>
    <a-alert v-if="error" type="error" :message="error" show-icon />
    <div class="repair-toolbar">
      <a-select
        v-model:value="selected"
        aria-label="待修复片段"
        :options="options"
        :disabled="busy"
      />
      <a-button :disabled="busy || selected === 0" @click="mergePrevious">与上一片段合并</a-button>
      <a-button :disabled="busy || selected === parts.length - 1" @click="mergeNext"
        >与下一片段合并</a-button
      >
      <a-button :disabled="busy || !canSplit" @click="split">在光标处拆分</a-button>
      <a-popconfirm
        v-if="changed"
        title="定位清洗稿将放弃尚未保存的边界调整，是否继续？"
        @confirm="$emit('locate', parts[selected].start)"
      >
        <a-button :disabled="busy">定位清洗稿</a-button>
      </a-popconfirm>
      <a-button v-else :disabled="busy" @click="$emit('locate', parts[selected].start)"
        >定位清洗稿</a-button
      >
    </div>
    <small>前文上下文</small>
    <pre>{{ before || '文档开头' }}</pre>
    <label for="chunk-boundary-text">片段 {{ selected + 1 }} 正文（点击文字设置拆分位置）</label>
    <textarea
      id="chunk-boundary-text"
      ref="editor"
      :value="parts[selected].text"
      readonly
      :disabled="busy"
      @click="captureCursor"
      @keyup="captureCursor"
      @select="captureCursor"
    />
    <small>{{
      canSplit ? `将在正文第 ${cursor} 个字符后拆分` : '请在正文内部点击，选择两侧都有内容的位置。'
    }}</small>
    <small>后文上下文</small>
    <pre>{{ after || '文档结尾' }}</pre>
    <p>当前共 {{ parts.length }} 个片段。请核对句子、表头和条件是否完整；保存后需重新审核。</p>
    <div class="repair-toolbar">
      <a-button
        type="primary"
        :disabled="busy || !changed"
        :loading="busy"
        @click="$emit('save', cuts)"
        >保存切分修订</a-button
      >
      <a-button :disabled="busy" @click="$emit('close')">放弃调整</a-button>
    </div>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  content: { type: String, required: true },
  chunks: { type: Array, required: true },
  busy: Boolean,
  error: { type: String, default: '' }
})
defineEmits(['save', 'close', 'locate'])
const chars = Array.from(props.content)
const initial = props.chunks.slice(1).map((chunk) => chunk.start_char_pos)
const cuts = ref([...initial])
const selected = ref(0)
const editor = ref(null)
const cursor = ref(0)
const canSplit = computed(() => {
  const part = parts.value[selected.value]
  return (
    !!chars
      .slice(part.start, part.start + cursor.value)
      .join('')
      .trim() &&
    !!chars
      .slice(part.start + cursor.value, part.end)
      .join('')
      .trim()
  )
})
const changed = computed(() => JSON.stringify(cuts.value) !== JSON.stringify(initial))
const parts = computed(() => {
  const ends = [...cuts.value, chars.length]
  return [0, ...cuts.value].map((start, i) => ({
    start,
    end: ends[i],
    text: chars.slice(start, ends[i]).join('')
  }))
})
const options = computed(() =>
  parts.value.map((part, i) => ({
    value: i,
    label: `片段 ${i + 1} · ${part.text.trim().slice(0, 28)}`
  }))
)
const before = computed(() =>
  chars
    .slice(Math.max(0, parts.value[selected.value].start - 240), parts.value[selected.value].start)
    .join('')
)
const after = computed(() =>
  chars.slice(parts.value[selected.value].end, parts.value[selected.value].end + 240).join('')
)
function mergePrevious() {
  cuts.value.splice(selected.value - 1, 1)
  selected.value--
}
function mergeNext() {
  cuts.value.splice(selected.value, 1)
}
function split() {
  const part = parts.value[selected.value]
  const position = cursor.value
  if (
    !chars
      .slice(part.start, part.start + position)
      .join('')
      .trim() ||
    !chars
      .slice(part.start + position, part.end)
      .join('')
      .trim()
  )
    return
  cuts.value.splice(selected.value, 0, part.start + position)
  cursor.value = 0
}
function captureCursor() {
  cursor.value = Array.from(
    parts.value[selected.value].text.slice(0, editor.value.selectionStart)
  ).length
}
watch(selected, () => {
  cursor.value = 0
})
</script>

<style scoped lang="less">
.chunk-repair {
  padding: 16px;
  border: 1px solid var(--gray-150);
  margin-bottom: 16px;
}
.repair-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 12px 0;
}
.ant-select {
  width: min(100%, 360px);
}
textarea {
  width: 100%;
  min-height: 240px;
  background: var(--color-bg-container);
  color: var(--color-text);
  border: 1px solid var(--gray-150);
  padding: 12px;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--color-text-secondary);
  max-height: 100px;
  overflow: auto;
}
</style>
