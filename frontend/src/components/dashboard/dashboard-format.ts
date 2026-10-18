/**
 * 全局看板展示规则（T33 / D17）。
 *
 * - 所有数字都来自 `GET /dashboard` 的同一份响应；本文件只做文案与格式化；
 * - 异常原因与“等待回收”使用服务端给出的机器可读原因，不自行推断；
 * - 完成率在空范围（`rate_available=false`，`reason=NO_TASKS`）时明确显示“无任务”，
 *   绝不显示成 0% 或 100%。
 */

import type { ApiError } from '@/api/client'
import type { DashboardScopeDTO, ExceptionItemDTO } from '@/api/dashboard'

/** 异常原因（服务端 `REASON_*`）。 */
export const EXCEPTION_REASON_LABEL: Readonly<Record<string, string>> = {
  EXECUTION_OVERDUE: '执行已过固定截止（等待回收）',
  DESIGNATED_AGENT_DISABLED: '指定 Agent 已禁用',
  DESIGNATED_AGENT_NOT_MEMBER: '指定 Agent 已不在项目成员中',
}

export const EXCEPTION_KIND_LABEL: Readonly<Record<string, string>> = {
  OVERDUE_RECLAIM_REQUIRED: '需要回收（等待回收的槽）',
  UNAVAILABLE_DESIGNATED_AGENT: '指定执行者不可用',
  'OVERDUE_RECLAIM_REQUIRED+UNAVAILABLE_DESIGNATED_AGENT': '需要回收 + 指定执行者不可用',
}

export function exceptionReasonLabels(item: ExceptionItemDTO): string[] {
  return (item.reasons ?? []).map((reason) => EXCEPTION_REASON_LABEL[reason] ?? reason)
}

export function exceptionKindLabel(item: ExceptionItemDTO): string {
  if (!item.kind) return '需关注'
  return EXCEPTION_KIND_LABEL[item.kind] ?? item.kind
}

/** 任务可见性说明：员工范围只统计已发布任务，准备中恒为 0。 */
export function taskVisibilityLabel(scope: DashboardScopeDTO | null | undefined): string {
  if (!scope) return '未知口径'
  return scope.task_visibility === 'PUBLISHED_ONLY'
    ? '仅已发布任务（准备中不计入）'
    : '全部任务（含准备中）'
}

/** scope 标识：范围来源、项目数、归档开关与可见性。 */
export function scopeSummary(scope: DashboardScopeDTO | null | undefined): string {
  if (!scope) return '统计范围未知'
  const actor =
    scope.actor_type === 'web_operator'
      ? '本地管理员'
      : scope.actor_type === 'agent'
        ? `Agent（${scope.agent_id ?? '未知'}）`
        : scope.actor_type
  const projects = scope.project_id
    ? `单个项目 ${scope.project_id}`
    : `全部可访问项目（${scope.project_count} 个）`
  const archive = scope.include_archived ? '含归档' : '不含归档'
  return `${actor} · ${projects} · ${archive} · ${taskVisibilityLabel(scope)}`
}

/** 完成率：空范围返回 null 的展示文案。 */
export function completionRateText(completion: {
  rate: number | null
  rate_available: boolean
  done: number
  total: number
  reason?: string | null
} | null): string {
  if (!completion) return '—'
  if (!completion.rate_available || completion.rate === null) {
    return completion.reason === 'NO_TASKS' ? '无任务（不计算完成率）' : '不可用'
  }
  return `${(completion.rate * 100).toFixed(1)}%（${completion.done}/${completion.total}）`
}

export function percentText(rate: number | null | undefined): string {
  if (rate == null) return '无任务'
  return `${(rate * 100).toFixed(1)}%`
}

/** 失败原因（保留服务端原话 + 错误码 + 请求号）。 */
export function describeDashboardError(error: ApiError | null): string {
  return error ? error.displayMessage : ''
}
