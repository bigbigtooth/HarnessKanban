/**
 * Agent 页面的展示格式化（T29）。
 *
 * 契约来源：docs/DEVELOP.md D16、docs/product-design.md §4/§9。
 *
 * 铁律：
 * - 在线状态由**服务端**推导（`online_state`），前端不根据心跳时间自己重算“是否超时”；
 * - 禁用状态优先于在线状态（D16：disabled 优先）；
 * - 有效执行数（未结束且未过期的执行）与已占容量槽（reserved_execution_count）分别展示，
 *   二者不同：到期未回收的执行不再“工作中”，但仍然占槽，界面显示“等待回收”。
 */

import type { ApiError } from '@/api/client'
import type { AgentRole } from '@/api/types'
import type {
  AgentExecutionBriefDTO,
  AgentExecutionSummaryDTO,
  AgentListQuery,
  AgentSummaryDTO,
  AgentOnlineState,
} from '@/api/agents'

/** 五种展示状态（与 `agent_service.ONLINE_STATES` 一致，前端不做二次推导）。 */
export const ONLINE_STATE_LABEL: Readonly<Record<AgentOnlineState, string>> = {
  NEVER_CONNECTED: '未连接',
  ONLINE_IDLE: '在线空闲',
  ONLINE_WORKING: '在线工作',
  OFFLINE: '离线',
  DISABLED: '已禁用',
}

export const ONLINE_STATE_ORDER: readonly AgentOnlineState[] = [
  'DISABLED',
  'ONLINE_WORKING',
  'ONLINE_IDLE',
  'OFFLINE',
  'NEVER_CONNECTED',
]

export const AGENT_ROLE_LABEL: Readonly<Record<AgentRole, string>> = {
  SUPERVISOR: '主管',
  WORKER: '员工',
}

/**
 * 展示状态 → 共享徽标配色类（`src/styles/components.css`）。
 * 只决定颜色；状态一律以文字表达（product §3.2），颜色不承担语义。
 */
export const ONLINE_STATE_BADGE_CLASS: Readonly<Record<AgentOnlineState, string>> = {
  ONLINE_WORKING: 'badge--success',
  ONLINE_IDLE: 'badge--info',
  OFFLINE: 'badge--warning',
  NEVER_CONNECTED: 'badge--neutral',
  DISABLED: 'badge--neutral',
}

/** 头像展示用的名字首字（无名字时为空串，由调用方决定占位）。 */
export function agentInitial(name: string | null | undefined): string {
  return (name ?? '').trim().slice(0, 1)
}

// ---------------------------------------------------------------------------
// 时间展示（UTC 存储，默认按 Asia/Shanghai 显示并标注时区；UTC 原值由调用方放在 title 上）
// ---------------------------------------------------------------------------

export const AGENT_DISPLAY_TIME_ZONE = 'Asia/Shanghai'

const SHANGHAI_PARTS = new Intl.DateTimeFormat('zh-CN', {
  timeZone: AGENT_DISPLAY_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

/** ISO 字符串 → `YYYY-MM-DD HH:mm:ss（Asia/Shanghai）`；空值返回空串。 */
export function formatAgentTime(value: string | null | undefined): string {
  if (!value) return ''
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  const parts = SHANGHAI_PARTS.formatToParts(parsed)
  const pick = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((part) => part.type === type)?.value ?? '00'
  return `${pick('year')}-${pick('month')}-${pick('day')} ${pick('hour')}:${pick('minute')}:${pick(
    'second',
  )}（${AGENT_DISPLAY_TIME_ZONE}）`
}

export function agentRoleLabel(role: AgentRole | string | null | undefined): string {
  if (role === 'SUPERVISOR') return AGENT_ROLE_LABEL.SUPERVISOR
  if (role === 'WORKER') return AGENT_ROLE_LABEL.WORKER
  return '未知角色'
}

/**
 * 展示状态：禁用优先（即使服务端给出了别的 online_state，禁用也压过在线）。
 * 除此之外一律使用服务端返回值，绝不用本地时钟重新判定在线/离线。
 */
export function agentDisplayState(agent: {
  enabled: boolean
  online_state?: AgentOnlineState | null
}): AgentOnlineState {
  if (!agent.enabled) return 'DISABLED'
  return agent.online_state ?? 'NEVER_CONNECTED'
}

export function agentStateLabel(agent: {
  enabled: boolean
  online_state?: AgentOnlineState | null
}): string {
  return ONLINE_STATE_LABEL[agentDisplayState(agent)]
}

/** 服务端给出的展示状态文案；无法识别的枚举原样回显，不伪装成其它状态。 */
export function onlineStateLabel(state: string | null | undefined): string {
  if (!state) return '—'
  return ONLINE_STATE_LABEL[state as AgentOnlineState] ?? state
}

/** 该执行已过固定截止、仍占槽：显示“等待回收”。 */
export function executionWaitingReclaim(brief: AgentExecutionBriefDTO): boolean {
  return brief.expired === true
}

export interface SlotView {
  /** 有效执行数（未结束且未过期）：真正在工作的执行。 */
  effective: number
  /** 服务端权威的已占容量槽数（含到期未回收）。 */
  reserved: number
  /** 到期未回收、仍占槽的执行数。 */
  waitingReclaim: number
  maxConcurrency: number
  available: number
}

export function slotView(agent: {
  max_concurrency: number
  reserved_execution_count: number
  available_capacity?: number
  execution_summary?: AgentExecutionSummaryDTO | null
}): SlotView {
  const effective = agent.execution_summary?.effective_count ?? 0
  const reserved = agent.reserved_execution_count
  const waitingReclaim =
    agent.execution_summary?.pending_reclaim_count ?? Math.max(0, reserved - effective)
  const available = agent.available_capacity ?? Math.max(0, agent.max_concurrency - reserved)
  return { effective, reserved, waitingReclaim, maxConcurrency: agent.max_concurrency, available }
}

export function slotSummaryText(agent: AgentSummaryDTO & { execution_summary?: AgentExecutionSummaryDTO | null }): string {
  const view = slotView(agent)
  return `有效执行 ${view.effective} · 占槽 ${view.reserved}/${view.maxConcurrency}`
}

export function listCellSlotText(agent: AgentSummaryDTO): string {
  return `占槽 ${agent.reserved_execution_count}/${agent.max_concurrency}`
}

/** 执行历史摘要（completed/timeout/rework/total 为累计历史，不进入“有效执行”）。 */
export function executionHistoryText(summary: AgentExecutionSummaryDTO): string {
  return `累计 ${summary.total_count} · 已提交 ${summary.completed_count} · 超时 ${summary.timeout_count} · 返工轮次 ${summary.rework_count}`
}

/** 错误文案：500 以上与不可重试错误保留服务端原话，绝不吞成空成功。 */
export function describeAgentError(error: ApiError | null): string {
  if (!error) return ''
  return error.displayMessage
}

/** 受影响任务的展示编号（禁用/撤权时的“影响面”列表）。 */
export function affectedTaskLabels(briefs: AgentExecutionBriefDTO[]): string[] {
  return briefs.map((brief) => {
    const number = brief.display_number ?? brief.task_id
    const state = executionWaitingReclaim(brief) ? '等待回收' : '执行中'
    return `${number}（${state}，截止 ${brief.execution_deadline_at}）`
  })
}

// ---------------------------------------------------------------------------
// 列表筛选（名称 / 角色 / 启用 / 在线 / 状态）
// ---------------------------------------------------------------------------

export type TriState = '' | 'true' | 'false'

/**
 * 「状态」下拉的取值：
 * `''` 全部（默认只看未删除）/ `'true'` 已启用 / `'false'` 已禁用 / `'deleted'` 已删除。
 */
export type AgentStatusFilterValue = '' | 'true' | 'false' | 'deleted'

export interface AgentListFilters {
  name: string
  role: '' | AgentRole
  enabled: TriState
  online: TriState
  /**
   * `'true'` = 已删除（回收站）视图：请求带 `deleted=true`，此时忽略启用筛选。
   * 其余取值（含缺省）= 未删除（与现状一致）；`emptyAgentFilters()` 始终给出该字段。
   */
  deleted?: TriState
}

export function emptyAgentFilters(): AgentListFilters {
  return { name: '', role: '', enabled: '', online: '', deleted: '' }
}

export function hasAgentFilters(filters: AgentListFilters): boolean {
  return Boolean(filters.name || filters.role || filters.enabled || filters.online || filters.deleted)
}

/** 把界面筛选映射为 `GET /agents` 的查询参数（空值不发送）。 */
export function toAgentListQuery(
  filters: AgentListFilters,
  page: number,
  pageSize: number,
): AgentListQuery {
  const query: AgentListQuery = {
    name: filters.name.trim() ? filters.name.trim() : null,
    role: filters.role || null,
    enabled: filters.enabled === '' ? null : filters.enabled === 'true',
    online: filters.online === '' ? null : filters.online === 'true',
    page,
    pageSize,
  }
  if (filters.deleted === 'true') {
    // 已删除视图：服务端 deleted=true 只返回已删除的，启用筛选不再参与。
    query.deleted = true
    query.enabled = null
  }
  return query
}

/** 筛选说明文案（用于“当前筛选”提示与空态解释）。 */
export function agentFilterSummary(filters: AgentListFilters): string {
  const parts: string[] = []
  if (filters.name.trim()) parts.push(`名称包含“${filters.name.trim()}”`)
  if (filters.role) parts.push(`角色 ${agentRoleLabel(filters.role)}`)
  if (filters.enabled) parts.push(`启用状态 ${filters.enabled === 'true' ? '已启用' : '已禁用'}`)
  if (filters.online) parts.push(`在线状态 ${filters.online === 'true' ? '在线' : '不在线'}`)
  if (filters.deleted === 'true') parts.push('状态 已删除')
  return parts.length > 0 ? parts.join('、') : '未设置筛选条件'
}

/** 状态下拉当前值：已删除优先，其次启用/禁用。 */
export function agentStatusFilterValue(
  filters: Pick<AgentListFilters, 'enabled' | 'deleted'>,
): AgentStatusFilterValue {
  if (filters.deleted === 'true') return 'deleted'
  if (filters.enabled === 'true') return 'true'
  if (filters.enabled === 'false') return 'false'
  return ''
}

/** 用状态下拉的值改写筛选：选「已删除」时清空启用筛选，选启用/禁用/全部时清空已删除。 */
export function withAgentStatusFilter(
  filters: AgentListFilters,
  status: AgentStatusFilterValue,
): AgentListFilters {
  if (status === 'deleted') return { ...filters, enabled: '', deleted: 'true' }
  return { ...filters, enabled: status, deleted: '' }
}

// ---------------------------------------------------------------------------
// 软删除展示（列表 / 详情 / 批量回执）
// ---------------------------------------------------------------------------

/** 已删除徽标文字：颜色只是辅助，文字独立表达状态。 */
export const AGENT_DELETED_BADGE_LABEL = '已删除'

/** 是否软删除：只认服务端的 `deleted_at`，前端不自行推导。 */
export function isAgentDeleted(agent: { deleted_at?: string | null } | null | undefined): boolean {
  return Boolean(agent?.deleted_at)
}

/** 删除原因（服务端为空时不编造内容，给出占位）。 */
export function agentDeletedReasonText(agent: { deleted_reason?: string | null }): string {
  const reason = (agent.deleted_reason ?? '').trim()
  return reason || '（未填写原因）'
}

/** 已删除徽标的 title：删除时间（Asia/Shanghai）与原因。 */
export function agentDeletedTooltip(agent: {
  deleted_at?: string | null
  deleted_reason?: string | null
}): string {
  if (!agent.deleted_at) return ''
  return `删除于 ${formatAgentTime(agent.deleted_at)} · 原因：${agentDeletedReasonText(agent)}`
}

/** 批量操作名（服务端 `operation`）→ 中文标签。 */
export function bulkOperationLabel(operation: string | null | undefined): string {
  if (operation === 'agent.bulk_delete') return '批量删除'
  if (operation === 'agent.bulk_restore') return '批量恢复'
  return operation || '批量操作'
}

/** 批量回执摘要：请求 / 成功 / 失败项数。 */
export function bulkResultSummaryText(result: {
  operation: string
  requested: number
  succeeded: readonly unknown[]
  failed: readonly unknown[]
}): string {
  return `${bulkOperationLabel(result.operation)}：请求 ${result.requested} 项，成功 ${result.succeeded.length} 项，失败 ${result.failed.length} 项`
}

/** 单项失败文案：服务端原因 + 错误码（原样展示，绝不吞成成功）。 */
export function bulkFailureText(item: { message: string; code: string }): string {
  return `${item.message}（${item.code}）`
}

/** 单项失败的可展开细节（details 为空时不显示）。 */
export function bulkFailureDetailsText(details?: Record<string, unknown> | null): string {
  if (!details || Object.keys(details).length === 0) return ''
  try {
    return JSON.stringify(details)
  } catch {
    return ''
  }
}
