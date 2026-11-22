/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type FilterReportSummary = {
  sample_count: number
  formattable_count: number
  pending_type_count: number
  excluded_count: number
  range_comparable_count: number
  undeclared_fields: string[]
  conflict_output_columns: string[]
  backfill_fields: { field: string; column: string; rule_no: string; hint: string }[]
}

export type FilterReportRow = {
  row_no: number
  sample_key: string
  signature: string
  values: Record<string, string | number | null>
  type_state: string
  pending_fields: string[]
  filter_state: string
  filter_reason: string
  range_comparable: boolean
  verdict: string
  verdict_reason: string
  formattable: boolean
}

export type FilterReportField = {
  field: string
  declared_type: string
  declaration_source: string
  declaration_column: string
  status: string
  in_scope: boolean
  needs_declaration: boolean
  observed_types: Record<string, number>
  has_leading_zero: boolean
  backfill_hint: string
  mapping_output_columns: string[]
}

export type FilterReportMapping = {
  resolution: {
    output_column: string
    declared_output_column: string
    source_field: string
    source_type: string
    conflict: boolean
  }[]
  conflicts: {
    output_column: string
    sources: string[]
    categories: Record<string, string>
    conflict: boolean
    resolution: string
    actual_output_columns: string[]
  }[]
  output_columns: string[]
}

export type FilterReport = {
  report_id: string
  report_key: string
  revision: number
  previous_report_id: string | null
  rule_no: string
  rule_version: string
  api: string
  filter_field: string
  filter_condition: string
  filter_mode: string
  criteria_text: string
  null_handling: string
  template_no: string
  template_version: string
  batch_fingerprint: string
  checked_fields: string[]
  fields: FilterReportField[]
  rows: FilterReportRow[]
  mapping: FilterReportMapping
  summary: FilterReportSummary
}

export type FilterReportListItem = {
  report_id: string
  revision: number
  rule_no: string
  rule_version: string
  template_no: string
  template_version: string
  batch_fingerprint: string
  created_at: string
  summary: FilterReportSummary
}

export type FilterReportDiff = {
  ok: boolean
  report_key: string
  template_changed: boolean
  rule_changed: boolean
  changed_count: number
  previous: {
    report_id: string
    revision: number
    template_version: string
    rule_version: string
    summary: FilterReportSummary
  }
  current: {
    report_id: string
    revision: number
    template_version: string
    rule_version: string
    summary: FilterReportSummary
  }
  changes: {
    signature: string
    sample_key: string
    before_verdict: string
    after_verdict: string
    changed: boolean
    before_reasons: string[]
    after_reasons: string[]
  }[]
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
