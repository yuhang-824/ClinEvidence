<template>
  <main class="ragas-page">
    <header class="ragas-header">
      <div>
        <router-link to="/patients" class="back-link">← 患者库</router-link>
        <h1>患者问答 RAGAS 测评</h1>
        <p>用真实患者绑定对话运行测试题，并评估病例与知识库证据支持的回答。</p>
      </div>
      <button class="secondary" :disabled="loading" @click="refresh">刷新</button>
    </header>

    <div v-if="pageError" class="error-state" role="alert">
      {{ pageError }} <button class="text-button" @click="refresh">重试</button>
    </div>

    <div class="ragas-columns">
      <section class="panel">
        <h2>1. 导入测试集</h2>
        <p class="hint">支持现有 100 题格式：测试题与元数据按行对应，患者映射按题号关联。病例题只可测评本人拥有且已有发布快照的患者。</p>
        <details class="format-help">
          <summary>新增题目的 JSONL 格式</summary>
          <code>{"user_input":"问题","reference":"参考答案","scope":"mixed","patient_display_code":"脱敏编号"}</code>
          <p class="hint">scope 可选 patient、knowledge、mixed；病例题和综合题填写患者脱敏编号。每行一题。</p>
        </details>
        <label>名称<input v-model.trim="upload.name" maxlength="100" placeholder="例如：患者问答 100 题" /></label>
        <label>测试题 JSONL <span class="required">必填</span><input type="file" accept=".jsonl" @change="setFile($event, 'samples')" /></label>
        <label>题目元数据 JSONL<input type="file" accept=".jsonl" @change="setFile($event, 'metadata')" /></label>
        <label>患者映射 JSONL<input type="file" accept=".jsonl" @change="setFile($event, 'scopeMap')" /></label>
        <button class="primary" :disabled="uploading || !upload.name || !upload.samples" @click="submitDataset">
          {{ uploading ? '导入中…' : '导入测试集' }}
        </button>
        <p v-if="uploadError" class="field-error" role="alert">{{ uploadError }}</p>
      </section>

      <section class="panel">
        <h2>2. 配置并运行</h2>
        <label>测试集
          <select v-model="selectedDatasetId">
            <option value="">请选择测试集</option>
            <option v-for="dataset in datasets" :key="dataset.id" :value="dataset.id">{{ dataset.name }} · {{ dataset.item_count }} 题</option>
          </select>
        </label>
        <template v-if="selectedDataset">
          <div class="coverage">
            <span>病例 {{ selectedDataset.scope_counts.patient || 0 }}</span>
            <span>知识库 {{ selectedDataset.scope_counts.knowledge || 0 }}</span>
            <span>综合 {{ selectedDataset.scope_counts.mixed || 0 }}</span>
          </div>
          <p v-if="!selectedDataset.scope_counts.mixed" class="notice">该测试集尚无综合题，不能据此判断病例与知识库联合回答效果。</p>
          <p v-if="selectedDataset.unreviewed_count" class="notice">{{ selectedDataset.unreviewed_count }} 题参考答案待临床复核；当前评分只作工程基线。</p>
        </template>
        <label>被测智能体
          <select v-model="config.agent_slug">
            <option value="">请选择智能体</option>
            <option v-for="agent in options.agents" :key="agent.slug" :value="agent.slug">{{ agent.name }}</option>
          </select>
        </label>
        <label>评判强模型
          <select v-model="config.judge_model">
            <option value="">请选择评判模型</option>
            <option v-for="model in options.judge_models" :key="model.spec" :value="model.spec">{{ model.name || model.spec }}</option>
          </select>
        </label>
        <label>RAGAS 相关性向量模型
          <select v-model="config.embedding_model">
            <option value="">请选择向量模型</option>
            <option v-for="model in options.embedding_models" :key="model.spec" :value="model.spec">{{ model.name || model.spec }}</option>
          </select>
        </label>
        <p class="hint">请选用已启用病例工具和目标知识库的智能体，以及支持结构化 JSON 输出的评判模型。运行会逐题调用被测智能体、评判模型和向量模型；没有患者快照或工具证据的题目会记录为不可评分。</p>
        <button class="primary" :disabled="starting || !canStart" @click="startRun">
          {{ starting ? '正在提交…' : '开始自动测评' }}
        </button>
        <p v-if="runError" class="field-error" role="alert">{{ runError }}</p>
      </section>
    </div>

    <section class="panel results-panel">
      <h2>3. 评估结果</h2>
      <p v-if="loading" class="hint">正在读取评估记录…</p>
      <p v-else-if="!runs.length" class="hint">暂无评估记录。导入测试集并开始测评后，进度与评分会显示在这里。</p>
      <div v-else class="run-layout">
        <div class="run-list">
          <button v-for="run in runs" :key="run.id" class="run-row" :class="{ active: run.id === selectedRunId }" @click="selectRun(run.id)">
            <strong>{{ datasetName(run.dataset_id) }}</strong>
            <span>{{ statusText(run.status) }} · {{ run.completed_items }} 题已处理</span>
            <small>{{ formatTime(run.created_at) }}</small>
          </button>
        </div>
        <div v-if="runDetail" class="run-detail">
          <div class="run-heading">
            <div>
              <h3>{{ datasetName(runDetail.dataset_id) }}</h3>
              <p>{{ statusText(runDetail.status) }} · {{ runDetail.completed_items }}/{{ runDetail.total_items }} 题已处理</p>
            </div>
            <span class="status-badge" :class="runDetail.status">{{ statusText(runDetail.status) }}</span>
          </div>
          <p v-if="runDetail.error" class="field-error">{{ runDetail.error }}</p>
          <div class="metric-grid">
            <div v-for="metric in metricDefinitions" :key="metric.key" class="metric-card">
              <span>{{ metric.label }}</span>
              <strong>{{ scoreText(runDetail.metrics[metric.key]) }}</strong>
              <small>{{ runDetail.metric_counts[metric.key] || 0 }} 题可评分</small>
            </div>
          </div>
          <p class="hint">每项均值只使用该项成功评分的题目；未运行、缺证据和评判失败不计入分母。</p>
          <div class="scope-grid">
            <div v-for="scope in scopeDefinitions" :key="scope.key" class="scope-card">
              <strong>{{ scope.label }}</strong>
              <span>{{ runDetail.scope_results[scope.key]?.completed || 0 }} 题可评分 · {{ runDetail.scope_results[scope.key]?.failed || 0 }} 题失败</span>
            </div>
          </div>
          <h3>逐题明细</h3>
          <p v-if="!runDetail.results?.length" class="hint">后台运行开始后会逐题显示结果。</p>
          <details v-for="item in runDetail.results" :key="item.index" class="item-detail">
            <summary>#{{ item.index }} {{ scopeLabel(item.scope) }} · {{ item.original_id || '无题号' }} · {{ item.status === 'completed' ? '已评分' : '失败' }}</summary>
            <p v-if="item.error" class="field-error">{{ item.error }}</p>
            <p v-if="item.agent_run_id">AgentRun: {{ item.agent_run_id }}</p>
            <div class="item-metrics"><span v-for="metric in metricDefinitions" :key="metric.key">{{ metric.label }} {{ scoreText(item.metrics?.[metric.key]) }}</span></div>
            <p v-for="(error, key) in item.metric_errors" :key="key" class="field-error">{{ key }}：{{ error }}</p>
            <p v-if="item.response"><strong>实际回答：</strong>{{ item.response }}</p>
            <p v-if="item.context_sources?.length">证据来源：病例 {{ item.context_sources.filter((source) => source === 'patient').length }} 条，知识库 {{ item.context_sources.filter((source) => source === 'knowledge').length }} 条</p>
            <p v-if="item.missing_sources?.length" class="field-error">缺少预期证据来源：{{ item.missing_sources.map((source) => source === 'patient' ? '病例' : '知识库').join('、') }}</p>
          </details>
        </div>
      </div>
    </section>
  </main>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { clinicalRagasApi } from '@/apis/clinical_ragas_api'

const metricDefinitions = [
  { key: 'context_precision', label: '上下文精确度' },
  { key: 'context_recall', label: '上下文召回率' },
  { key: 'faithfulness', label: '忠实度' },
  { key: 'answer_relevancy', label: '回答相关性' }
]
const scopeDefinitions = [
  { key: 'patient', label: '病例题' },
  { key: 'knowledge', label: '知识库题' },
  { key: 'mixed', label: '综合题' }
]
const upload = reactive({ name: '', samples: null, metadata: null, scopeMap: null })
const config = reactive({ agent_slug: '', judge_model: '', embedding_model: '' })
const options = ref({ agents: [], judge_models: [], embedding_models: [] })
const datasets = ref([])
const runs = ref([])
const selectedDatasetId = ref('')
const selectedRunId = ref('')
const runDetail = ref(null)
const loading = ref(false)
const uploading = ref(false)
const starting = ref(false)
const pageError = ref('')
const uploadError = ref('')
const runError = ref('')
let pollTimer

const selectedDataset = computed(() => datasets.value.find((item) => item.id === selectedDatasetId.value))
const canStart = computed(() => selectedDatasetId.value && config.agent_slug && config.judge_model && config.embedding_model)

function setFile(event, key) { upload[key] = event.target.files?.[0] || null }
function datasetName(id) { return datasets.value.find((item) => item.id === id)?.name || id }
function statusText(status) { return { pending: '等待中', running: '运行中', completed: '已完成', failed: '失败' }[status] || status }
function scopeLabel(scope) { return scopeDefinitions.find((item) => item.key === scope)?.label || scope }
function scoreText(score) { return typeof score === 'number' ? score.toFixed(3) : '—' }
function formatTime(value) { return value ? new Date(value).toLocaleString('zh-CN') : '' }

async function refresh() {
  loading.value = true
  pageError.value = ''
  try {
    const [nextOptions, nextDatasets, nextRuns] = await Promise.all([
      clinicalRagasApi.options(), clinicalRagasApi.listDatasets(), clinicalRagasApi.listRuns()
    ])
    options.value = nextOptions
    datasets.value = nextDatasets
    runs.value = nextRuns
    if (selectedRunId.value) await loadRun(selectedRunId.value)
    else if (runs.value.length) await selectRun(runs.value[0].id)
  } catch (error) {
    pageError.value = error?.message || '评估数据加载失败'
  } finally {
    loading.value = false
  }
}

async function loadRun(id) {
  try { runDetail.value = await clinicalRagasApi.getRun(id) }
  catch (error) { pageError.value = error?.message || '评估详情加载失败' }
}

async function selectRun(id) {
  selectedRunId.value = id
  await loadRun(id)
}

async function submitDataset() {
  uploading.value = true
  uploadError.value = ''
  try {
    const created = await clinicalRagasApi.uploadDataset(upload)
    await refresh()
    selectedDatasetId.value = created.id
    upload.samples = null
    upload.metadata = null
    upload.scopeMap = null
  } catch (error) {
    uploadError.value = error?.message || '测试集导入失败'
  } finally {
    uploading.value = false
  }
}

async function startRun() {
  starting.value = true
  runError.value = ''
  try {
    const created = await clinicalRagasApi.startRun({ dataset_id: selectedDatasetId.value, ...config })
    await refresh()
    await selectRun(created.id)
  } catch (error) {
    runError.value = error?.message || '评估提交失败'
  } finally {
    starting.value = false
  }
}

onMounted(() => {
  refresh()
  pollTimer = window.setInterval(() => {
    if (runs.value.some((run) => ['pending', 'running'].includes(run.status))) refresh()
  }, 4000)
})
onUnmounted(() => window.clearInterval(pollTimer))
</script>

<style scoped>
.ragas-page { max-width: 1380px; margin: 0 auto; padding: 30px; color: var(--text-primary, #263238); }
.ragas-header, .run-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; }
.ragas-header h1 { margin: 8px 0; font-size: 28px; }
.ragas-header p, .hint, .run-heading p { color: var(--text-secondary, #667681); line-height: 1.5; }
.back-link, .text-button { color: var(--main-700); text-decoration: none; }
.text-button { border: 0; background: transparent; cursor: pointer; }
.ragas-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin: 26px 0 20px; }
.panel { border: 1px solid var(--border-color, #e1e8ed); border-radius: 12px; background: var(--main-0, #fff); padding: 22px; }
.panel h2 { margin: 0 0 12px; font-size: 18px; }
.panel h3 { margin: 22px 0 12px; font-size: 16px; }
.panel label { display: block; margin: 16px 0; font-weight: 600; }
.panel input:not([type='file']), .panel select { display: block; width: 100%; margin-top: 7px; padding: 9px 11px; border: 1px solid #cbd7df; border-radius: 7px; color: inherit; background: var(--main-0, #fff); }
.panel input[type='file'] { display: block; width: 100%; margin-top: 8px; font-weight: 400; }
.primary, .secondary { padding: 9px 15px; border-radius: 7px; cursor: pointer; }
.primary { color: white; background: var(--main-700); border: 1px solid var(--main-700); }
.secondary { color: var(--main-700); background: transparent; border: 1px solid #cbd7df; }
button:disabled { opacity: .55; cursor: not-allowed; }
.required, .field-error { color: #b34141; }
.field-error, .notice, .error-state { line-height: 1.5; }
.notice { padding: 9px 12px; border-radius: 7px; background: #fff7df; color: #704d00; }
.error-state { margin: 20px 0; color: #b34141; }
.coverage, .item-metrics { display: flex; flex-wrap: wrap; gap: 10px; }
.format-help { margin: 12px 0; }
.format-help summary { color: var(--main-700); cursor: pointer; }
.format-help code { display: block; padding: 10px; margin-top: 8px; overflow-wrap: anywhere; background: var(--main-40); border-radius: 6px; }
.coverage span, .item-metrics span { padding: 5px 9px; border-radius: 6px; background: var(--main-40); }
.run-layout { display: grid; grid-template-columns: 250px minmax(0, 1fr); gap: 22px; }
.run-list { display: flex; flex-direction: column; gap: 7px; }
.run-row { display: flex; flex-direction: column; gap: 5px; padding: 12px; border: 1px solid #d8e2e8; border-radius: 8px; background: transparent; text-align: left; cursor: pointer; }
.run-row.active { border-color: var(--main-700); background: var(--main-40); }
.run-row span, .run-row small { color: #667681; }
.metric-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 18px; }
.metric-card, .scope-card { display: flex; flex-direction: column; gap: 6px; border: 1px solid #e1e8ed; border-radius: 8px; padding: 14px; }
.metric-card strong { font-size: 26px; }
.metric-card small, .scope-card span { color: #667681; }
.scope-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.status-badge { padding: 5px 9px; border-radius: 20px; background: var(--main-40); white-space: nowrap; }
.status-badge.failed { color: #b34141; background: #fff0ef; }
.item-detail { margin: 8px 0; padding: 12px; border: 1px solid #e1e8ed; border-radius: 8px; overflow-wrap: anywhere; }
.item-detail summary { cursor: pointer; font-weight: 600; }
@media (max-width: 900px) { .ragas-columns, .run-layout { grid-template-columns: 1fr; } .metric-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 600px) { .ragas-page { padding: 16px; } .metric-grid, .scope-grid { grid-template-columns: 1fr; } }
</style>
