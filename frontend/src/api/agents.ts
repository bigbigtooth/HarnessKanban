/**
 * Agent 域 WebAPI 封装（T29）。
 *
 * 契约来源：docs/CONTRACT.md §3（信封/错误）、§8（幂等）、docs/DEVELOP.md D06/D16、
 * docs/product-design.md §4/§9，字段与 `backend/webapi/routes/agents.py` +
 * `domain/agent_service.py` + `domain/presenters.py:agent_summary` 一一对应。
 *
 * 规则：
 * - 只调用已注册端点（`/agents`、`/agents/{id}`、`/agents/{id}/delete`、`/agents/{id}/rotate-key`、
 *   `/agents/{id}/enable`、`/agents/{id}/disable`、`/agents/{id}/role`、`/agents/bulk-delete`、
 *   `/agents/bulk-restore`），不新增角色/状态别名，`role`/`enabled` 只能走业务动作端点，
 *   绝不通过通用 PATCH 改写；
 * - 每个写操作由调用方提供 Idempotency-Key，同一次逻辑操作的重试必须复用同一个键；
 * - **明文 Key 只存在于 create / rotate / enable(rotate_key=true) 的响应里**，本模块不缓存、
 *   不持久化、不重放它；调用方必须在弹窗内存中一次性展示，关闭即清除。
 */

import { webApiClient, type QueryValue } from '@/api/client'
import type { AgentRole, IdempotentReplayFlag, Page, UUID, UtcTimestamp } from '@/api/types'

// ---------------------------------------------------------------------------
// DTO（字段名与后端 presenter 完全一致，不做本地重命名）
// ---------------------------------------------------------------------------

/** 服务端在线状态（`agent_service.ONLINE_STATES`）；只读展示，前端不自行推导。 */
export type AgentOnlineState =
  | 'NEVER_CONNECTED'
  | 'ONLINE_IDLE'
  | 'ONLINE_WORKING'
  | 'OFFLINE'
  | 'DISABLED'

/** `agent_summary`。 */
export interface AgentSummaryDTO {
  id: UUID
  name: string
  role: AgentRole
  description?: string | null
  capabilities?: string[]
  enabled: boolean
  max_concurrency: number
  reserved_execution_count: number
  available_capacity?: number
  last_heartbeat_at?: UtcTimestamp | null
  /** 服务端推导的展示状态；禁用优先，前端不得用“未超时”重新计算。 */
  online_state?: AgentOnlineState | null
  version: number
  created_at?: UtcTimestamp | null
  updated_at?: UtcTimestamp | null
  /** 软删除时间（ISO 字符串）；`null`/缺失 = 未删除。 */
  deleted_at?: UtcTimestamp | null
  /** 删除原因（服务端同时写入审计）；未删除时为空串。 */
  deleted_reason?: string
  /** 列表项附带的授权项目 ID（详情用 `projects`）。 */
  project_ids?: UUID[]
}

/** 授权项目（详情 `projects[]`）。 */
export interface AgentProjectRefDTO {
  id: UUID
  title: string
  status: string
}

/** 容量（详情 `capacity`）。 */
export interface AgentCapacityDTO {
  max_concurrency: number
  reserved_execution_count: number
  available_capacity: number
}

/** 执行计数（`agent_service.execution_summary`）。 */
export interface AgentExecutionSummaryDTO {
  open_count: number
  /** 未结束且未过期的执行 = 有效执行。 */
  effective_count: number
  completed_count: number
  timeout_count: number
  rework_count: number
  total_count: number
  /** 到期未回收、仍占槽的执行数。 */
  pending_reclaim_count: number
}

/** 当前未结束的执行（详情 `current_executions[]`）。 */
export interface AgentExecutionBriefDTO {
  execution_id: UUID
  task_id: UUID
  project_id?: UUID | null
  display_number?: string | null
  difficulty: number
  claimed_at: UtcTimestamp
  execution_deadline_at: UtcTimestamp
  /** 服务端判定：已过执行截止（等待回收），仍占槽。 */
  expired: boolean
}

/** `GET /agents/{id}` 详情。绝不包含 key 原文、摘要或指纹。 */
export interface AgentDetailDTO extends AgentSummaryDTO {
  key_version: number
  project_ids: UUID[]
  projects: AgentProjectRefDTO[]
  capacity: AgentCapacityDTO
  execution_summary: AgentExecutionSummaryDTO
  current_executions: AgentExecutionBriefDTO[]
}

/** `POST /agents` 响应：唯一一次携带明文 `agent_key`。 */
export interface AgentCreateResultDTO extends AgentDetailDTO, IdempotentReplayFlag {
  /** 仅在首次成功响应出现；重放响应只带 `key_delivery_unavailable`。 */
  agent_key?: string | null
  key_delivery_unavailable?: boolean
}

/** `POST /agents/{id}/rotate-key` / `enable(rotate_key)` 响应。 */
export interface AgentKeyDeliveryDTO extends AgentDetailDTO, IdempotentReplayFlag {
  agent_key?: string | null
  key_delivery_unavailable?: boolean
}

/** `POST /agents/{id}/disable` 响应。 */
export interface AgentDisableResultDTO extends AgentDetailDTO, IdempotentReplayFlag {
  disposition: 'WAIT' | 'NOW'
  reclaimed_execution_ids: UUID[]
  reason: string
}

/**
 * `POST /agents/{id}/delete` 响应：与禁用同形（服务端共用 `_change_agent_availability`），
 * 另带软删除字段 `deleted_at` / `deleted_reason`。
 */
export type AgentDeleteResultDTO = AgentDisableResultDTO

/** 批量操作单项成功回执（服务端 `bulk_result`）。 */
export interface AgentBulkSuccessDTO {
  id: UUID
  /** 删除后的软删除时间；恢复（enable）时为 null。 */
  deleted_at: UtcTimestamp | null
  /** 服务端回执里的状态字段（Agent 批量结果中为 null，不自行编造）。 */
  status: string | null
  /** 该子项是否命中了幂等重放。 */
  idempotent_replay: boolean
}

/** 批量操作单项失败原因：一项失败不影响其他项，由页面逐项展示。 */
export interface AgentBulkFailureDTO {
  id: UUID
  code: string
  message: string
  details?: Record<string, unknown>
}

/** `POST /agents/bulk-delete`、`POST /agents/bulk-restore` 的逐项结果信封。 */
export interface AgentBulkResultDTO {
  operation: string
  requested: number
  succeeded: AgentBulkSuccessDTO[]
  failed: AgentBulkFailureDTO[]
  actor_type: string
}

/** `POST /agents/{id}/enable` 响应（未轮换时没有 `agent_key`）。 */
export type AgentEnableResultDTO = AgentDetailDTO & IdempotentReplayFlag & { agent_key?: string | null }

/** `POST /agents/{id}/role` 响应。 */
export type AgentRoleResultDTO = AgentDetailDTO & IdempotentReplayFlag

/** `PATCH /agents/{id}` 响应。 */
export type AgentUpdateResultDTO = AgentDetailDTO & IdempotentReplayFlag

export interface AgentListQuery {
  /** 名称模糊匹配（服务端 `name__icontains`）。 */
  name?: string | null
  role?: AgentRole | null
  enabled?: boolean | null
  /** true = 启用且 90 秒内有心跳；false = 其余（含禁用与未连接）。 */
  online?: boolean | null
  /**
   * true = 只看已删除（回收站视图）；false/不传 = 只看未删除。
   * 页面选「已删除」时才发送该参数。
   */
  deleted?: boolean | null
  page?: number
  pageSize?: number
}

interface RequestOptions {
  signal?: AbortSignal
}

function buildAgentListParams(query: AgentListQuery): Record<string, QueryValue> {
  const params: Record<string, QueryValue> = {}
  const name = (query.name ?? '').trim()
  if (name) params.name = name
  if (query.role) params.role = query.role
  if (query.enabled != null) params.enabled = query.enabled ? 'true' : 'false'
  if (query.online != null) params.online = query.online ? 'true' : 'false'
  if (query.deleted != null) params.deleted = query.deleted ? 'true' : 'false'
  if (query.page != null) params.page = query.page
  if (query.pageSize != null) params.page_size = query.pageSize
  return params
}

/** `GET /agents`：名称 / 角色 / 启用 / 在线筛选 + 分页。 */
export function listAgentsPage(
  query: AgentListQuery = {},
  options: RequestOptions = {},
): Promise<Page<AgentSummaryDTO>> {
  return webApiClient.get<Page<AgentSummaryDTO>>('/agents', {
    query: buildAgentListParams(query),
    signal: options.signal,
  })
}

/** `GET /agents/{id}`：容量、授权项目、当前执行与历史计数。 */
export function getAgent(agentId: UUID, options: RequestOptions = {}): Promise<AgentDetailDTO> {
  return webApiClient.get<AgentDetailDTO>(`/agents/${agentId}`, { signal: options.signal })
}

export interface CreateAgentInput {
  name: string
  role: AgentRole
  /** 省略时由服务端按配置默认值创建（不替服务端编造数字）。 */
  max_concurrency?: number
  description?: string
  capabilities?: string[]
}

/**
 * `POST /agents`：创建 Agent 并由服务端**唯一一次**返回明文 Key。
 * 响应重放（`idempotent_replay=true`）时服务端返回 `key_delivery_unavailable=true`，
 * 调用方必须提示重新重置，不能假装展示旧 Key。
 */
export function createAgent(
  input: CreateAgentInput,
  idempotencyKey: string,
): Promise<AgentCreateResultDTO> {
  const body: Record<string, unknown> = {
    name: input.name,
    role: input.role,
  }
  if (input.max_concurrency !== undefined) body.max_concurrency = input.max_concurrency
  if (input.description !== undefined) body.description = input.description
  if (input.capabilities !== undefined) body.capabilities = input.capabilities
  return webApiClient.post<AgentCreateResultDTO>('/agents', body, { idempotencyKey })
}

export interface UpdateAgentInput {
  name?: string
  description?: string
  capabilities?: string[]
  max_concurrency?: number
}

/**
 * `PATCH /agents/{id}`：只改基础属性。容量低于未释放槽数时服务端返回
 * 409 `CAPACITY_BELOW_RESERVED`，页面必须回显服务端原因。
 */
export function updateAgent(
  agentId: UUID,
  expectedVersion: number,
  input: UpdateAgentInput,
  idempotencyKey: string,
): Promise<AgentUpdateResultDTO> {
  const body: Record<string, unknown> = { expected_version: expectedVersion }
  if (input.name !== undefined) body.name = input.name
  if (input.description !== undefined) body.description = input.description
  if (input.capabilities !== undefined) body.capabilities = input.capabilities
  if (input.max_concurrency !== undefined) body.max_concurrency = input.max_concurrency
  return webApiClient.patch<AgentUpdateResultDTO>(`/agents/${agentId}`, body, { idempotencyKey })
}

/** `POST /agents/{id}/rotate-key`：旧 Key 立即失效，新 Key 一次性交付。 */
export function rotateAgentKey(
  agentId: UUID,
  idempotencyKey: string,
): Promise<AgentKeyDeliveryDTO> {
  return webApiClient.post<AgentKeyDeliveryDTO>(`/agents/${agentId}/rotate-key`, {}, { idempotencyKey })
}

/** `POST /agents/{id}/enable`：可选同时轮换 Key（同样一次性交付）。 */
export function enableAgent(
  agentId: UUID,
  rotateKey: boolean,
  idempotencyKey: string,
): Promise<AgentEnableResultDTO> {
  return webApiClient.post<AgentEnableResultDTO>(
    `/agents/${agentId}/enable`,
    { rotate_key: rotateKey },
    { idempotencyKey },
  )
}

/**
 * `POST /agents/{id}/disable`：全局禁用。
 * 该 Agent 仍持有未结束执行时，服务端要求明确选择 `WAIT`（等待固定截止）或
 * `NOW`（立即回收，且必须填写原因）。
 *
 * 请求字段固定为服务端白名单的 `reclaim` + `reason`（路由 `AGENT_DISABLE_ALLOWED_FIELDS`）；
 * 响应里的 `disposition` 是服务端回显的最终处置。
 */
export function disableAgent(
  agentId: UUID,
  disposition: 'WAIT' | 'NOW',
  reason: string,
  idempotencyKey: string,
): Promise<AgentDisableResultDTO> {
  return webApiClient.post<AgentDisableResultDTO>(
    `/agents/${agentId}/disable`,
    { reclaim: disposition, reason },
    { idempotencyKey },
  )
}

/** 软删除入参：原因必填；仍持有有效执行时 `reclaim` 必填。 */
export interface DeleteAgentInput {
  /** 删除原因（必填，服务端 400 `INVALID_ARGUMENT` 拒绝空原因）。 */
  reason: string
  /** 该 Agent 仍持有有效执行时必须给出：`WAIT` 等待到期 / `NOW` 立即回收。 */
  reclaim?: 'WAIT' | 'NOW' | null
}

/**
 * `POST /agents/{id}/delete`：软删除（从列表/候选/统计中消失，Key 立即失效，
 * 执行、提交与审核历史保留，可通过 `enable` 恢复）。
 *
 * 请求字段固定为服务端白名单的 `reason` + `reclaim`（路由 `AGENT_DISABLE_ALLOWED_FIELDS`）；
 * 服务端的拒绝原因（缺少原因、缺少处置、最后主管保护）由调用方原样展示。
 */
export function deleteAgent(
  agentId: UUID,
  input: DeleteAgentInput,
  idempotencyKey: string,
): Promise<AgentDeleteResultDTO> {
  return webApiClient.post<AgentDeleteResultDTO>(
    `/agents/${agentId}/delete`,
    { reason: input.reason, reclaim: input.reclaim ?? null },
    { idempotencyKey },
  )
}

export interface BulkDeleteAgentsInput {
  agentIds: UUID[]
  reason: string
  reclaim?: 'WAIT' | 'NOW' | null
}

/**
 * `POST /agents/bulk-delete`：逐项执行、逐项返回结果——**一项失败不影响其他项**，
 * 调用方必须把 `failed[]` 的原因与错误码逐项展示给管理员。
 */
export function bulkDeleteAgents(
  input: BulkDeleteAgentsInput,
  idempotencyKey: string,
): Promise<AgentBulkResultDTO> {
  return webApiClient.post<AgentBulkResultDTO>(
    '/agents/bulk-delete',
    { agent_ids: input.agentIds, reason: input.reason, reclaim: input.reclaim ?? null },
    { idempotencyKey },
  )
}

/** `POST /agents/bulk-restore`：恢复（启用 + 清空删除标记），返回同样的逐项结果结构。 */
export function bulkRestoreAgents(
  agentIds: UUID[],
  idempotencyKey: string,
): Promise<AgentBulkResultDTO> {
  return webApiClient.post<AgentBulkResultDTO>(
    '/agents/bulk-restore',
    { agent_ids: agentIds },
    { idempotencyKey },
  )
}

/** `POST /agents/{id}/role`：全局角色变更（受最后主管保护约束）。 */
export function updateAgentRole(
  agentId: UUID,
  role: AgentRole,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<AgentRoleResultDTO> {
  return webApiClient.post<AgentRoleResultDTO>(
    `/agents/${agentId}/role`,
    { role, expected_version: expectedVersion },
    { idempotencyKey },
  )
}
