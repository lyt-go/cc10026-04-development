<template>
  <section class="page" data-testid="filter-type-report">
    <header class="page-head">
      <div>
        <h2>过滤类型校验报告</h2>
        <p class="page-desc">
          按过滤规则对字段样本做类型校验：先说明哪些行可进入格式化、哪些行类型待核，
          再把报告关联到规则版本与模板版本，供结果格式化按报告取数。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/data_filter">返回数据过滤</RouterLink>
      </div>
    </header>

    <form class="analysis-form" @submit.prevent="runValidation">
      <label>
        过滤规则
        <select v-model.number="selectedRuleId" data-testid="rule-select">
          <option :value="0" disabled>请选择过滤规则</option>
          <option v-for="rule in rules" :key="rule.id" :value="rule.id">
            {{ rule['规则编号'] }} · {{ rule['关联API'] }} · {{ rule['过滤字段'] }} {{ rule['过滤条件'] }}
          </option>
        </select>
      </label>
      <label>
        模板
        <select v-model="selectedTemplateNo" data-testid="template-select">
          <option value="">不关联模板</option>
          <option v-for="tpl in templates" :key="tpl.id" :value="tpl['模板编号']">
            {{ tpl['模板编号'] }} · v{{ tpl['模板版本'] }} · {{ tpl['字段映射'] }}
          </option>
        </select>
      </label>
      <button class="btn" type="button" data-testid="load-preset" @click="loadPreset">载入三类样本</button>
      <button class="btn primary" type="submit" data-testid="run-validation" :disabled="loading || !selectedRuleId">
        {{ loading ? '校验中…' : '生成校验报告' }}
      </button>
    </form>

    <label class="sample-editor">
      <span>字段样本（JSON 数组，前导零编码请保留字符串形式）</span>
      <textarea v-model="samplesText" data-testid="samples-input" rows="6" spellcheck="false" />
    </label>

    <p v-if="errorMessage" class="error-text" data-testid="report-error">{{ errorMessage }}</p>

    <template v-if="report">
      <div class="stat-row" data-testid="report-summary">
        <article class="stat-card">
          <span class="stat-label">样本总数</span>
          <strong class="stat-value">{{ report.summary.sample_count }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">可进入格式化</span>
          <strong class="stat-value">{{ report.summary.formattable_count }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">类型待核</span>
          <strong class="stat-value">{{ report.summary.pending_type_count }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">可参与范围比较</span>
          <strong class="stat-value">{{ report.summary.range_comparable_count }}</strong>
        </article>
      </div>

      <p class="report-meta">
        报告 {{ report.report_id }}（第 {{ report.revision }} 版）
        · 规则 {{ report.rule_no }} / {{ report.rule_version }}
        · 模板 {{ report.template_no || '未关联' }} / {{ report.template_version || '—' }}
        · 判定口径：{{ report.criteria_text }}
      </p>

      <h3>逐行判定</h3>
      <table class="data-table" data-testid="report-rows">
        <thead>
          <tr>
            <th>行</th>
            <th>样本</th>
            <th>类型状态</th>
            <th>过滤判定</th>
            <th>范围比较</th>
            <th>结论</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in report.rows" :key="row.sample_key" :data-testid="`sample-row-${row.row_no}`">
            <td>{{ row.row_no }}</td>
            <td class="mono">{{ JSON.stringify(row.values) }}</td>
            <td>{{ row.type_state }}</td>
            <td>{{ row.filter_state }}</td>
            <td>{{ row.range_comparable ? '可比较' : '不可比较' }}</td>
            <td>
              <span :class="['verdict-tag', verdictClass(row.verdict)]">{{ row.verdict }}</span>
            </td>
            <td class="muted">{{ row.verdict_reason || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <h3>字段类型与补齐位置</h3>
      <table class="data-table" data-testid="report-fields">
        <thead>
          <tr>
            <th>字段</th>
            <th>声明类型</th>
            <th>声明来源</th>
            <th>观测类型</th>
            <th>状态</th>
            <th>补齐位置</th>
            <th>补齐建议</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="field in report.fields" :key="field.field">
            <td>{{ field.field }}<span v-if="!field.in_scope" class="muted">（旁列）</span></td>
            <td>{{ field.declared_type || '—' }}</td>
            <td>{{ field.declaration_source || '—' }}</td>
            <td class="mono">{{ JSON.stringify(field.observed_types) }}</td>
            <td>{{ field.status }}</td>
            <td>{{ field.needs_declaration ? field.declaration_column : '—' }}</td>
            <td class="muted">{{ field.backfill_hint || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <template v-if="report.mapping.conflicts.length">
        <h3>同名列映射消歧</h3>
        <table class="data-table" data-testid="report-conflicts">
          <thead>
            <tr><th>结果列</th><th>来源字段</th><th>类型</th><th>实际输出列</th><th>处理</th></tr>
          </thead>
          <tbody>
            <tr v-for="conflict in report.mapping.conflicts" :key="conflict.output_column">
              <td>{{ conflict.output_column }}</td>
              <td class="mono">{{ conflict.sources.join('、') }}</td>
              <td class="mono">{{ Object.entries(conflict.categories).map(([k, v]) => `${k}=${v}`).join('；') }}</td>
              <td class="mono">{{ conflict.actual_output_columns.join('、') }}</td>
              <td class="muted">{{ conflict.resolution }}</td>
            </tr>
          </tbody>
        </table>
      </template>

      <div class="page-actions report-actions">
        <button class="btn" type="button" data-testid="format-from-report" @click="runFormatting">
          按报告生成格式化结果
        </button>
        <button
          class="btn"
          type="button"
          data-testid="compare-report"
          :disabled="!report.previous_report_id"
          @click="runCompare"
        >
          对比上一版判定
        </button>
      </div>

      <template v-if="formatResult">
        <h3>格式化结果（数据源与字段范围来自报告）</h3>
        <p class="report-meta">
          实际输出字段：{{ (formatResult.fields || []).join('、') || '—' }}
          · 跳过的行：{{ formatResult.skipped.length }}
        </p>
        <pre class="result-box" data-testid="format-result">{{ JSON.stringify(formatResult.data, null, 2) }}</pre>
      </template>

      <template v-if="diff">
        <h3>新旧判定对比</h3>
        <p class="report-meta">
          {{ diff.previous.report_id }}（模板 {{ diff.previous.template_version }} / 规则 {{ diff.previous.rule_version }}）
          → {{ diff.current.report_id }}（模板 {{ diff.current.template_version }} / 规则 {{ diff.current.rule_version }}）
          · 变化 {{ diff.changed_count }} 行
        </p>
        <table class="data-table" data-testid="report-diff">
          <thead>
            <tr><th>样本</th><th>旧判定</th><th>新判定</th><th>是否变化</th></tr>
          </thead>
          <tbody>
            <tr v-for="change in diff.changes" :key="change.signature" :data-changed="String(change.changed)">
              <td class="mono">{{ change.sample_key }}</td>
              <td>{{ change.before_verdict }}</td>
              <td>{{ change.after_verdict }}</td>
              <td>{{ change.changed ? '是' : '否' }}</td>
            </tr>
          </tbody>
        </table>
      </template>
    </template>

    <h3>历史校验报告</h3>
    <table class="data-table" data-testid="report-history">
      <thead>
        <tr><th>报告</th><th>版本</th><th>规则</th><th>模板</th><th>样本/可格式化/待核</th><th>生成时间</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="item in history" :key="item.report_id">
          <td class="mono">{{ item.report_id }}</td>
          <td>第 {{ item.revision }} 版</td>
          <td>{{ item.rule_no }} / {{ item.rule_version }}</td>
          <td>{{ item.template_no || '—' }} / {{ item.template_version || '—' }}</td>
          <td>{{ item.summary.sample_count }} / {{ item.summary.formattable_count }} / {{ item.summary.pending_type_count }}</td>
          <td class="muted">{{ item.created_at }}</td>
          <td><button class="link" type="button" @click="openReport(item.report_id)">查看</button></td>
        </tr>
        <tr v-if="!history.length">
          <td colspan="7" class="empty-state">暂无校验报告，先选择规则并生成</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  compareFilterReports,
  formatFromReport,
  listEntries,
  listFilterReports,
  loadFilterReport,
  validateFilterTypes,
} from '@/api/backend-service'
import type {
  EntryRow,
  FilterReport,
  FilterReportDiff,
  FilterReportListItem,
} from '@/data/types'

const PRESET = [
  { 编码: '007', 数量: 7 },
  { 编码: '8', 数量: 12 },
  { 编码: 'A12', 数量: '' },
]

const rules = ref<EntryRow[]>([])
const templates = ref<EntryRow[]>([])
const history = ref<FilterReportListItem[]>([])
const selectedRuleId = ref(0)
const selectedTemplateNo = ref('')
const samplesText = ref(JSON.stringify(PRESET, null, 2))
const report = ref<FilterReport | null>(null)
const diff = ref<FilterReportDiff | null>(null)
const formatResult = ref<{ ok: boolean; data: unknown; skipped: unknown[]; fields: string[] } | null>(null)
const loading = ref(false)
const errorMessage = ref('')

function verdictClass(verdict: string): string {
  if (verdict === '可进入格式化') return 'ok'
  if (verdict === '类型待核') return 'warn'
  return 'muted'
}

function loadPreset() {
  samplesText.value = JSON.stringify(PRESET, null, 2)
  errorMessage.value = ''
}

async function loadLookups() {
  const [rulePage, templatePage] = await Promise.all([
    listEntries('data_filter', { size: '200' }),
    listEntries('result_format', { size: '200' }),
  ])
  rules.value = rulePage.items
  templates.value = templatePage.items
  if (!selectedRuleId.value && rules.value.length) selectedRuleId.value = Number(rules.value[0].id)
}

async function refreshHistory() {
  const payload = await listFilterReports()
  history.value = [...payload.items].reverse()
}

async function runValidation() {
  errorMessage.value = ''
  diff.value = null
  formatResult.value = null
  let samples: Record<string, unknown>[]
  try {
    samples = JSON.parse(samplesText.value)
    if (!Array.isArray(samples) || !samples.length) throw new Error('样本必须是非空 JSON 数组')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '样本 JSON 解析失败'
    return
  }
  loading.value = true
  try {
    report.value = await validateFilterTypes(selectedRuleId.value, {
      samples,
      template_no: selectedTemplateNo.value,
    })
    await refreshHistory()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '校验失败'
  } finally {
    loading.value = false
  }
}

async function openReport(reportId: string) {
  errorMessage.value = ''
  diff.value = null
  formatResult.value = null
  try {
    report.value = await loadFilterReport(reportId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '报告读取失败'
  }
}

async function runFormatting() {
  if (!report.value) return
  errorMessage.value = ''
  try {
    formatResult.value = await formatFromReport(report.value.report_id, {
      api: report.value.api,
      template_no: report.value.template_no,
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '格式化失败'
  }
}

async function runCompare() {
  if (!report.value) return
  errorMessage.value = ''
  try {
    diff.value = await compareFilterReports(report.value.report_id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '报告对比失败'
  }
}

onMounted(async () => {
  try {
    await Promise.all([loadLookups(), refreshHistory()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '初始化失败'
  }
})
</script>

<style scoped>
.sample-editor { display: block; margin-bottom: 12px; }
.sample-editor span { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.sample-editor textarea {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
.report-meta { font-size: 12px; color: var(--muted); margin: 6px 0 12px; }
.report-actions { margin: 12px 0; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.muted { color: var(--muted); }
.verdict-tag { padding: 2px 8px; border-radius: 999px; font-size: 12px; background: #eef2f7; }
.verdict-tag.ok { background: #dcfce7; color: #166534; }
.verdict-tag.warn { background: #fef3c7; color: #92400e; }
.result-box {
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px;
  font-size: 12px;
  overflow: auto;
  max-height: 260px;
}
</style>
