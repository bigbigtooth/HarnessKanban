/**
 * API 类型：docs/CONTRACT.md §2/§3 的 TypeScript 副本。
 *
 * 单一事实源是 backend/domain/contracts.py（枚举）与 CONTRACT.md（信封、错误表）。
 * 本文件不得自行新增状态、角色或错误码；发现缺口先回到主线程修订契约。
 */

// ---------------------------------------------------------------- 枚举 (§2)

export const AgentRole = {
  SUPERVISOR: 'SUPERVISOR',
  WORKER: 'WORKER',
} as const
export type AgentRole = (typeof AgentRole)[keyof typeof AgentRole]
export const AGENT_ROLES: readonly AgentRole[] = [AgentRole.SUPERVISOR, AgentRole.WORKER]

export const TaskStatus = {
  PREPARING: 'PREPARING',
  TODO: 'TODO',
  IN_PROGRESS: 'IN_PROGRESS',
  IN_REVIEW: 'IN_REVIEW',
  DONE: 'DONE',
} as const
export type TaskStatus = (typeof TaskStatus)[keyof typeof TaskStatus]
/** 五列看板固定顺序（D15）：不得重排、不得新增第六态。 */
export const TASK_STATUS_COLUMNS: readonly TaskStatus[] = [
  TaskStatus.PREPARING,
  TaskStatus.TODO,
  TaskStatus.IN_PROGRESS,
  TaskStatus.IN_REVIEW,
  TaskStatus.DONE,
]

export const ClaimMode = {
  OPEN: 'OPEN',
  ASSIGNED: 'ASSIGNED',
} as const
export type ClaimMode = (typeof ClaimMode)[keyof typeof ClaimMode]

export const ProjectStatus = {
  DRAFT: 'DRAFT',
  ACTIVE: 'ACTIVE',
  COMPLETED: 'COMPLETED',
  ARCHIVED: 'ARCHIVED',
} as const
export type ProjectStatus = (typeof ProjectStatus)[keyof typeof ProjectStatus]

export const ReviewDecision = {
  APPROVE: 'APPROVE',
  REJECT: 'REJECT',
} as const
export type ReviewDecision = (typeof ReviewDecision)[keyof typeof ReviewDecision]

export const EndReason = {
  RELEASE: 'RELEASE',
  RECLAIM: 'RECLAIM',
  TIMEOUT: 'TIMEOUT',
  SUBMIT: 'SUBMIT',
} as const
export type EndReason = (typeof EndReason)[keyof typeof EndReason]

export const ActorType = {
  WEB_OPERATOR: 'web_operator',
  AGENT: 'agent',
  SYSTEM: 'system',
} as const
export type ActorType = (typeof ActorType)[keyof typeof ActorType]

export const AttachmentPurpose = {
  TASK_REFERENCE: 'TASK_REFERENCE',
  EXECUTION_ARTIFACT: 'EXECUTION_ARTIFACT',
  SUBMISSION_ARTIFACT: 'SUBMISSION_ARTIFACT',
} as const
export type AttachmentPurpose = (typeof AttachmentPurpose)[keyof typeof AttachmentPurpose]

export const DeliveryState = {
  PENDING: 'PENDING',
  PUBLISHED: 'PUBLISHED',
  PROCESSED: 'PROCESSED',
  NEEDS_ATTENTION: 'NEEDS_ATTENTION',
} as const
export type DeliveryState = (typeof DeliveryState)[keyof typeof DeliveryState]

export const IdempotencyState = {
  IN_PROGRESS: 'IN_PROGRESS',
  DONE: 'DONE',
} as const
export type IdempotencyState = (typeof IdempotencyState)[keyof typeof IdempotencyState]

/** 难度 1–5（整数）；展示标签 L1–L5。 */
export type Difficulty = 1 | 2 | 3 | 4 | 5
export const DIFFICULTIES: readonly Difficulty[] = [1, 2, 3, 4, 5]

export type DifficultyLevelName = 'L1' | 'L2' | 'L3' | 'L4' | 'L5'
export const DIFFICULTY_LEVEL_NAME: Readonly<Record<Difficulty, DifficultyLevelName>> = {
  1: 'L1',
  2: 'L2',
  3: 'L3',
  4: 'L4',
  5: 'L5',
}
export const DIFFICULTY_LABEL: Readonly<Record<Difficulty, string>> = {
  1: 'L1 入门',
  2: 'L2 简单',
  3: 'L3 中等',
  4: 'L4 困难',
  5: 'L5 极难',
}
/** 固定超时映射（秒）；与 CONTRACT.md §2 一致，前端只做展示。 */
export const DIFFICULTY_TIMEOUT_SECONDS: Readonly<Record<Difficulty, number>> = {
  1: 900,
  2: 1800,
  3: 7200,
  4: 28800,
  5: 86400,
}

// ---------------------------------------------------------------- 错误码 (§3)

export const ErrorCode = {
  INVALID_ARGUMENT: 'INVALID_ARGUMENT',
  IDEMPOTENCY_KEY_REQUIRED: 'IDEMPOTENCY_KEY_REQUIRED',
  UNSUPPORTED_PARAMETER: 'UNSUPPORTED_PARAMETER',
  AGENT_TOKEN_MISSING: 'AGENT_TOKEN_MISSING',
  AGENT_TOKEN_INVALID: 'AGENT_TOKEN_INVALID',
  AGENT_DISABLED: 'AGENT_DISABLED',
  ROLE_FORBIDDEN: 'ROLE_FORBIDDEN',
  SELF_REVIEW_FORBIDDEN: 'SELF_REVIEW_FORBIDDEN',
  REQUEST_ORIGIN_FORBIDDEN: 'REQUEST_ORIGIN_FORBIDDEN',
  WEBUI_KEY_REQUIRED: 'WEBUI_KEY_REQUIRED',
  WEBUI_KEY_INVALID: 'WEBUI_KEY_INVALID',
  RESOURCE_NOT_FOUND: 'RESOURCE_NOT_FOUND',
  METHOD_NOT_ALLOWED: 'METHOD_NOT_ALLOWED',
  VERSION_CONFLICT: 'VERSION_CONFLICT',
  TASK_ALREADY_CLAIMED: 'TASK_ALREADY_CLAIMED',
  TASK_DIFFICULTY_MISMATCH: 'TASK_DIFFICULTY_MISMATCH',
  INVALID_TRANSITION: 'INVALID_TRANSITION',
  EXECUTION_TIMEOUT: 'EXECUTION_TIMEOUT',
  CAPACITY_EXCEEDED: 'CAPACITY_EXCEEDED',
  IDEMPOTENCY_KEY_REUSED: 'IDEMPOTENCY_KEY_REUSED',
  IDEMPOTENCY_IN_PROGRESS: 'IDEMPOTENCY_IN_PROGRESS',
  EXECUTION_NOT_CURRENT: 'EXECUTION_NOT_CURRENT',
  REVIEW_ALREADY_DECIDED: 'REVIEW_ALREADY_DECIDED',
  AGENT_NAME_CONFLICT: 'AGENT_NAME_CONFLICT',
  MEMBERSHIP_CONFLICT: 'MEMBERSHIP_CONFLICT',
  LAST_SUPERVISOR_REQUIRED: 'LAST_SUPERVISOR_REQUIRED',
  PROJECT_NOT_ACTIVE: 'PROJECT_NOT_ACTIVE',
  SUBMISSION_EXISTS: 'SUBMISSION_EXISTS',
  CAPACITY_BELOW_RESERVED: 'CAPACITY_BELOW_RESERVED',
  TASK_NUMBER_CONFLICT: 'TASK_NUMBER_CONFLICT',
  ATTACHMENT_LIMIT_EXCEEDED: 'ATTACHMENT_LIMIT_EXCEEDED',
  DOCUMENT_CONFLICT: 'DOCUMENT_CONFLICT',
  FILE_TOO_LARGE: 'FILE_TOO_LARGE',
  UNSUPPORTED_MEDIA_TYPE: 'UNSUPPORTED_MEDIA_TYPE',
  RATE_LIMITED: 'RATE_LIMITED',
  DATABASE_BUSY: 'DATABASE_BUSY',
  CLAIM_LOCK_TIMEOUT: 'CLAIM_LOCK_TIMEOUT',
  CLAIM_LOCK_MISSING: 'CLAIM_LOCK_MISSING',
  DEPENDENCY_UNAVAILABLE: 'DEPENDENCY_UNAVAILABLE',
  /** 客户端本地错误码（非服务端契约，仅用于本地校验/传输失败）。 */
  INVALID_RESPONSE: 'INVALID_RESPONSE',
  NETWORK_ERROR: 'NETWORK_ERROR',
  REQUEST_ABORTED: 'REQUEST_ABORTED',
} as const
export type ErrorCode = (typeof ErrorCode)[keyof typeof ErrorCode]

/** 契约错误表：HTTP 状态 → 错误码（CONTRACT.md §3）。 */
export const ERROR_CODES_BY_STATUS: Readonly<Record<number, readonly string[]>> = {
  400: ['INVALID_ARGUMENT', 'IDEMPOTENCY_KEY_REQUIRED', 'UNSUPPORTED_PARAMETER'],
  401: ['AGENT_TOKEN_MISSING', 'AGENT_TOKEN_INVALID', 'AGENT_DISABLED'],
  403: ['ROLE_FORBIDDEN', 'SELF_REVIEW_FORBIDDEN', 'REQUEST_ORIGIN_FORBIDDEN', 'WEBUI_KEY_REQUIRED', 'WEBUI_KEY_INVALID'],
  404: ['RESOURCE_NOT_FOUND'],
  405: ['METHOD_NOT_ALLOWED'],
  409: [
    'VERSION_CONFLICT',
    'TASK_ALREADY_CLAIMED',
    'TASK_DIFFICULTY_MISMATCH',
    'INVALID_TRANSITION',
    'EXECUTION_TIMEOUT',
    'CAPACITY_EXCEEDED',
    'IDEMPOTENCY_KEY_REUSED',
    'IDEMPOTENCY_IN_PROGRESS',
    'EXECUTION_NOT_CURRENT',
    'REVIEW_ALREADY_DECIDED',
    'AGENT_NAME_CONFLICT',
    'MEMBERSHIP_CONFLICT',
    'LAST_SUPERVISOR_REQUIRED',
    'PROJECT_NOT_ACTIVE',
    'SUBMISSION_EXISTS',
    'CAPACITY_BELOW_RESERVED',
    'TASK_NUMBER_CONFLICT',
    'ATTACHMENT_LIMIT_EXCEEDED',
    'DOCUMENT_CONFLICT',
  ],
  413: ['FILE_TOO_LARGE'],
  415: ['UNSUPPORTED_MEDIA_TYPE'],
  429: ['RATE_LIMITED'],
  503: ['DATABASE_BUSY', 'CLAIM_LOCK_TIMEOUT', 'CLAIM_LOCK_MISSING', 'DEPENDENCY_UNAVAILABLE'],
} as const

/** 需要“按原幂等键退避重试”的错误码（D15/T28：503 原键退避）。 */
export const RETRYABLE_ERROR_CODES: readonly string[] = [
  'DATABASE_BUSY',
  'CLAIM_LOCK_TIMEOUT',
  'DEPENDENCY_UNAVAILABLE',
  'RATE_LIMITED',
] as const

/** 冲突类错误码：前端必须保留表单内容、刷新当前对象后再提示（D15）。 */
export const CONFLICT_ERROR_CODES: readonly string[] = ERROR_CODES_BY_STATUS[409] as readonly string[]

// ---------------------------------------------------------------- 信封 (§3)

export interface SuccessEnvelope<T> {
  data: T
  request_id: string
}

export interface ErrorPayload {
  code: string
  message: string
  details: Record<string, unknown>
}

export interface ErrorEnvelope {
  error: ErrorPayload
  request_id: string
}

/** 列表分页形状：`data` 内 `items/total/page/page_size`（默认 20、最大 100）。 */
export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export const DEFAULT_PAGE_SIZE = 20
export const MAX_PAGE_SIZE = 100

// ---------------------------------------------------------------- DTO (§11/§13)

/** 所有核心实体使用 UUID 对外标识。 */
export type UUID = string
/** ISO-8601 UTC 时间字符串。 */
export type UtcTimestamp = string

export interface AgentDTO {
  id: UUID
  name: string
  role: AgentRole
  description?: string | null
  capabilities?: string[]
  enabled: boolean
  key_version?: number
  max_concurrency: number
  reserved_execution_count: number
  last_heartbeat_at?: UtcTimestamp | null
  version: number
  created_at?: UtcTimestamp
}

export interface ProjectDTO {
  id: UUID
  title: string
  summary?: string | null
  background?: string | null
  status: ProjectStatus
  content_version?: number
  version: number
  archived_from_status?: ProjectStatus | null
  created_at?: UtcTimestamp
}

export interface ProjectMemberDTO {
  id: UUID
  project_id: UUID
  agent_id: UUID
  agent_name?: string
  agent_role?: AgentRole
  created_at?: UtcTimestamp
}

export interface DocumentDTO {
  id: UUID
  project_id: UUID
  name: string
  current_version_id?: UUID | null
  current_version_number?: number
  is_archived: boolean
  created_at?: UtcTimestamp
}

export interface TaskDTO {
  id: UUID
  project_id: UUID
  /** 项目内整数编号，对外显示 `TASK-%04d`。 */
  number: number
  title: string
  description?: string | null
  acceptance_criteria?: string | null
  difficulty: Difficulty
  status: TaskStatus
  claim_mode: ClaimMode
  designated_agent_id?: UUID | null
  assignee_agent_id?: UUID | null
  active_execution_id?: UUID | null
  current_submission_id?: UUID | null
  /** 列内排序使用 Decimal 字符串，禁止用浮点累积。 */
  sort_order: string
  due_at?: UtcTimestamp | null
  requirements_version?: number
  version: number
  is_archived: boolean
  created_at?: UtcTimestamp
  updated_at?: UtcTimestamp
}

/** 展示编号：`TASK-0001`。 */
export function formatTaskNumber(number: number): string {
  return `TASK-${String(number).padStart(4, '0')}`
}

export interface TaskExecutionDTO {
  id: UUID
  task_id: UUID
  agent_id: UUID
  sequence: number
  difficulty_snapshot: Difficulty
  execution_timeout_seconds: number
  context_version_id?: UUID | null
  claimed_at: UtcTimestamp
  execution_deadline_at: UtcTimestamp
  ended_at?: UtcTimestamp | null
  end_reason?: EndReason | null
  progress?: { message?: string; percent?: number | null; updated_at?: UtcTimestamp } | null
}

export interface TaskSubmissionDTO {
  id: UUID
  execution_id: UUID
  summary: string
  content?: string | null
  self_check?: string | null
  artifact_refs?: UUID[]
  links?: string[]
  submitted_at: UtcTimestamp
}

export interface TaskReviewDTO {
  id: UUID
  submission_id: UUID
  decision: ReviewDecision
  comment?: string | null
  actor_type: ActorType
  reviewer_agent_id?: UUID | null
  /** Web 代操作固定为 false（CONTRACT.md §4）。 */
  reviewer_identity_verified: boolean
  created_at: UtcTimestamp
}

export interface ActivityEventDTO {
  id: UUID
  project_id?: UUID | null
  task_id?: UUID | null
  actor_type: ActorType
  actor_agent_id?: UUID | null
  action: string
  before?: Record<string, unknown> | null
  after?: Record<string, unknown> | null
  reason?: string | null
  request_id?: string
  created_at: UtcTimestamp
}

export interface DashboardDTO {
  as_of: UtcTimestamp
  scope?: Record<string, unknown>
  /** 五态计数（Task 为统计单位；历史执行/提交/审核不进入计数，D17）。 */
  task_status_counts?: Record<TaskStatus, number>
  difficulty_counts?: Partial<Record<Difficulty, number>>
  project_counts?: Partial<Record<ProjectStatus, number>>
  agent_counts?: { online?: number; working?: number; offline?: number; never_connected?: number }
  exception_counts?: Record<string, number>
  completion_rate?: number | null
  [key: string]: unknown
}

/** 幂等回执重放标记：服务端成功重放时附加（CONTRACT.md §8）。 */
export interface IdempotentReplayFlag {
  idempotent_replay?: boolean
}
