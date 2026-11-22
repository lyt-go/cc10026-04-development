import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  EntryRow,
  FilterReport,
  FilterReportDiff,
  FilterReportListItem,
  OverviewResult,
  PageResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export async function listFilterReports(params: Record<string, string> = {}): Promise<{
  items: FilterReportListItem[]
  total: number
}> {
  const query = new URLSearchParams(params).toString()
  const response = await request(`/api/filter-reports?${query}`)
  if (!response.ok) throw new Error('校验报告列表读取失败')
  return await response.json()
}

export async function validateFilterTypes(
  ruleId: number,
  payload: {
    samples: Record<string, unknown>[]
    declared_types?: Record<string, string>
    template_no?: string
  },
): Promise<FilterReport> {
  const response = await request(`/api/filter-reports/rules/${ruleId}`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('过滤类型校验失败')
  return await response.json()
}

export async function loadFilterReport(reportId: string): Promise<FilterReport> {
  const response = await request(`/api/filter-reports/${reportId}`)
  if (!response.ok) throw new Error('校验报告读取失败')
  return await response.json()
}

export async function compareFilterReports(reportId: string, against = ''): Promise<FilterReportDiff> {
  const query = against ? `?against=${encodeURIComponent(against)}` : ''
  const response = await request(`/api/filter-reports/${reportId}/compare${query}`)
  if (!response.ok) throw new Error('校验报告对比失败')
  return await response.json()
}

export async function formatFromReport(
  reportId: string,
  payload: { api: string; template_no?: string },
): Promise<{ ok: boolean; data: unknown; skipped: unknown[]; fields: string[] }> {
  const response = await request(`/api/filter-reports/${reportId}/format`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('按报告格式化失败')
  return await response.json()
}
