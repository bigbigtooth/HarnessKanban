/**
 * 全局看板与活动 WebAPI 封装（T33）。
 *
 * 契约来源：`backend/webapi/routes/dashboard.py`、`backend/domain/dashboard_service.py`
 * （其 docstring 说明了 payload 的构成与 scope 语义）、docs/DEVELOP.md D17、
 * docs/CONTRACT.md §3。
 *
 * 规则：
 * - 统计**只**来自这一条响应：`as_of`、`scope`、`task_status_counts`、`difficulty_counts`、
 *   `completion`、`execution_counts`、`agent_counts`、`exception_counts`、`exceptions`、
 *   `projects`、`agents`。前端绝不用各页面缓存重新拼数字，也不把失败显示成 0；
 * - `execution_counts.effective_executions` 与 `reserved_slots` 并列展示：
 *   到期未回收的执行不计入“工作中”，但**仍然占槽**（`waiting_reclaim_executions`）；
 * - 活动列表由 `GET /activity` 提供，服务端已按时间倒序（最新在前）。
 */

import { webApiClient, type QueryValue } from '@/api/client'
import type { ProjectStatus, TaskStatus, UUID, UtcTimestamp } from '@/api/types'
import type { ActivityEventDTO } from '@/api/tasks'

export type { ActivityEventDTO }

// ---------------------------------------------------------------------------
// DTO（与 dashboard_service._dashboard 的 payload 一一对应）
// ---------------------------------------------------------------------------

/** 可见性标记：`FULL` = 含准备中；`PUBLISHED_ONLY` = 只看已发布（员工视角）。 */
export type TaskVisibility = 'FULL' | 'PUBLISHED_ONLY'

export interface DashboardScopeDTO {
  actor_type: string
  agent_id?: UUID | null
  role?: string | null
  project_id?: UUID | null
  project_ids: UUID[]
  project_count: number
  include_archived: boolean
  task_visibility: TaskVisibility
  archived_project_total: number
}

export interface CompletionDTO {
  done: number
  total: number
  /** 空范围时为 null：绝不显示成 100% 或 0%。 */
  rate: number | null
  rate_available: boolean
  /** 空范围原因，例如 `NO_TASKS`。 */
  reason?: string | null
}

export interface AgentCountsDTO {
  total: number
  enabled: number
  disabled: number
  online: number
  offline: number
  never_connected: number
  /** 有效执行数 > 0 的 Agent 数（工作中的 Agent）。 */
  working: number
}

export interface ExecutionCountsDTO {
  /** 未结束的执行（含到期未回收）。 */
  open_executions: number
  /** 未结束且未过期：真正工作中的执行。 */
  effective_executions: number
  /** 到期未回收（等待回收）的执行数：不工作但仍占槽。 */
  waiting_reclaim_executions: number
  /** 服务端权威的已占容量槽数。 */
  reserved_slots: number
  capacity_total: number
  capacity_available: number
}

export interface AgentRowDTO {
  agent_id: UUID
  name: string
  role: string
  enabled: boolean
  online_state: string
  last_heartbeat_at?: UtcTimestamp | null
  effective_executions: number
  waiting_reclaim_executions: number
  reserved_slots: number
  max_concurrency: number
  available_capacity: number
}

export interface ExceptionItemDTO {
  task_id: UUID
  project_id: UUID
  number: number
  display_number: string
  title: string
  status: TaskStatus
  difficulty: number
  claim_mode: string
  designated_agent_id?: UUID | null
  designated_agent_name?: string | null
  assignee_agent_id?: UUID | null
  claimed_at?: UtcTimestamp | null
  execution_deadline_at?: UtcTimestamp | null
  overdue_seconds?: number | null
  /** 机器可读原因（EXECUTION_OVERDUE / DESIGNATED_AGENT_DISABLED / DESIGNATED_AGENT_NOT_MEMBER）。 */
  reasons: string[]
  reason?: string | null
  /** OVERDUE_RECLAIM_REQUIRED / UNAVAILABLE_DESIGNATED_AGENT / 组合。 */
  kind?: string | null
}

export interface ExceptionCountsDTO {
  OVERDUE_RECLAIM_REQUIRED: number
  UNAVAILABLE_DESIGNATED_AGENT: number
  total: number
  /** 列表实际返回条数（上限 20；计数始终是真实总数）。 */
  returned: number
  limit: number
}

export interface DashboardProjectRowDTO {
  project_id: UUID
  title: string
  status: ProjectStatus
  task_total: number
  task_status_counts: Record<TaskStatus, number>
  archived_task_total: number
  overdue_task_total: number
  pending_review: number
  completion: CompletionDTO
  completion_rate: number | null
}

export interface DashboardDTO {
  as_of: UtcTimestamp
  scope: DashboardScopeDTO
  task_total: number
  task_status_counts: Record<TaskStatus, number>
  difficulty_counts: Record<string, number>
  archived_task_total: number
  overdue_task_total: number
  completion: CompletionDTO
  completion_rate: number | null
  project_counts: Record<string, number>
  project_total: number
  agent_counts: AgentCountsDTO
  execution_counts: ExecutionCountsDTO
  exception_counts: ExceptionCountsDTO
  exceptions: ExceptionItemDTO[]
  projects: DashboardProjectRowDTO[]
  agents: AgentRowDTO[]
}

// ---------------------------------------------------------------------------
// 请求
// ---------------------------------------------------------------------------

export interface DashboardQuery {
  projectId?: UUID | null
  includeArchived?: boolean
}

interface RequestOptions {
  signal?: AbortSignal
}

/** `GET /dashboard`：一次请求返回全部统计（scope 与 as_of 由服务端给出）。 */
export function getDashboard(
  query: DashboardQuery = {},
  options: RequestOptions = {},
): Promise<DashboardDTO> {
  const params: Record<string, QueryValue> = {}
  if (query.projectId) params.project_id = query.projectId
  if (query.includeArchived != null) params.include_archived = query.includeArchived
  return webApiClient.get<DashboardDTO>('/dashboard', { query: params, signal: options.signal })
}

export interface ActivityQuery {
  projectId?: UUID | null
  taskId?: UUID | null
  action?: string | null
  includeArchived?: boolean
  page?: number
  pageSize?: number
}

/** `GET /activity`：服务端按 `-created_at, -id` 返回最新在前的分页活动。 */
export function listActivity(
  query: ActivityQuery = {},
  options: RequestOptions = {},
): Promise<import('@/api/types').Page<ActivityEventDTO>> {
  const params: Record<string, QueryValue> = {}
  if (query.projectId) params.project_id = query.projectId
  if (query.taskId) params.task_id = query.taskId
  const action = (query.action ?? '').trim()
  if (action) params.action = action
  if (query.includeArchived != null) params.include_archived = query.includeArchived
  if (query.page != null) params.page = query.page
  if (query.pageSize != null) params.page_size = query.pageSize
  return webApiClient.get<import('@/api/types').Page<ActivityEventDTO>>('/activity', {
    query: params,
    signal: options.signal,
  })
}
