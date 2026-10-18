/**
 * 任务域 WebAPI 封装（T31/T32）。
 *
 * 契约来源：docs/CONTRACT.md §3（信封/错误）、§8（幂等）、§9（动作矩阵）、
 * docs/product-design.md §13（WebAPI 端点清单）、docs/DEVELOP.md D10–D15。
 *
 * 规则：
 * - 只调用已实现并列出的端点；不新增“直接写 status”“续期”等别名；
 * - 每个写操作由调用方提供 Idempotency-Key，同一次逻辑操作的重试必须复用同一个键
 *   （`src/api/client.ts` 不会替调用方造键）；
 * - 失败一律抛 `ApiError`（含 code/message/request_id），页面据此保留输入并回显原因，
 *   绝不把失败吞成空成功。
 */

import { webApiClient, type QueryValue } from '@/api/client'
import type {
  AgentRole,
  AttachmentPurpose,
  ClaimMode,
  Difficulty,
  EndReason,
  Page,
  ReviewDecision,
  TaskStatus,
  UUID,
  UtcTimestamp,
} from '@/api/types'

// ---------------------------------------------------------------------------
// DTO（与 backend/domain/presenters.py 的字段一一对应，不做本地重命名）
// ---------------------------------------------------------------------------

/** `task_summary`。 */
export interface TaskSummaryDTO {
  id: UUID
  project_id: UUID
  number: number
  display_number: string
  title: string
  difficulty: Difficulty
  difficulty_name?: string | null
  status: TaskStatus
  claim_mode: ClaimMode
  designated_agent_id?: UUID | null
  assignee_agent_id?: UUID | null
  active_execution_id?: UUID | null
  current_submission_id?: UUID | null
  sort_order: string
  due_at?: UtcTimestamp | null
  version: number
  requirements_version?: number
  is_archived: boolean
  labels?: string[]
  created_at?: UtcTimestamp | null
  published_at?: UtcTimestamp | null
  claimed_at?: UtcTimestamp | null
  submitted_at?: UtcTimestamp | null
  completed_at?: UtcTimestamp | null
}

/** 列表项 = `task_summary` + 列表附加字段。 */
export interface TaskListItemDTO extends TaskSummaryDTO {
  description?: string | null
  acceptance_criteria?: string | null
  execution_timeout_seconds?: number | null
}

/** `execution_view`；历史项额外带 `has_submission`。 */
export interface TaskExecutionDTO {
  execution_id: UUID
  task_id: UUID
  agent_id: UUID
  sequence: number
  difficulty: Difficulty
  execution_timeout_seconds: number
  claimed_at: UtcTimestamp
  execution_deadline_at: UtcTimestamp
  ended_at?: UtcTimestamp | null
  end_reason?: EndReason | null
  progress_message?: string | null
  progress_percent?: number | null
  progress_updated_at?: UtcTimestamp | null
  context_version_id?: UUID | null
  requirements_snapshot?: Record<string, unknown>
  has_submission?: boolean
}

export interface SelfCheckItemDTO {
  criterion: string
  passed: boolean
  evidence?: string
}

export interface SubmissionLinkDTO {
  title: string
  url: string
}

/** `submission_view`；历史项额外带 `review`。 */
export interface TaskSubmissionDTO {
  submission_id: UUID
  task_id: UUID
  execution_id: UUID
  summary: string
  content?: string | null
  self_check?: SelfCheckItemDTO[]
  artifact_refs?: UUID[]
  links?: SubmissionLinkDTO[]
  requirements_version?: number
  context_version_id?: UUID | null
  submitted_by_agent_id?: UUID | null
  submitted_at: UtcTimestamp
  review?: TaskReviewDTO | null
}

/** `review_view`。 */
export interface TaskReviewDTO {
  review_id: UUID
  submission_id: UUID
  task_id: UUID
  decision: ReviewDecision
  comment?: string | null
  actor_type: string
  reviewer_agent_id?: UUID | null
  /** Web 代操作固定 false（CONTRACT.md §4）。 */
  reviewer_identity_verified: boolean
  created_at: UtcTimestamp
}

/** `activity_view`。 */
export interface ActivityEventDTO {
  id: UUID
  project_id?: UUID | null
  task_id?: UUID | null
  execution_id?: UUID | null
  submission_id?: UUID | null
  actor_type: string
  actor_agent_id?: UUID | null
  action: string
  before?: Record<string, unknown>
  after?: Record<string, unknown>
  reason?: string | null
  request_id?: string | null
  created_at: UtcTimestamp
}

/** `attachment_view`。 */
export interface AttachmentDTO {
  id: UUID
  file_id: UUID
  original_name: string
  media_type: string
  size: number
  purpose: AttachmentPurpose
  task_id?: UUID | null
  execution_id?: UUID | null
  submission_id?: UUID | null
  created_at: UtcTimestamp
}

/** `agent_summary`（任务页面选择执行者/审核主管所需的最小只读视图）。 */
export interface AgentOptionDTO {
  id: UUID
  name: string
  role: AgentRole
  enabled: boolean
  max_concurrency: number
  reserved_execution_count: number
  available_capacity?: number
  online_state?: string | null
  version: number
  project_ids?: UUID[]
}

/** `GET /tasks/{id}` 详情：`task_detail_payload`。 */
export interface TaskDetailDTO extends TaskListItemDTO {
  created_by_agent_id?: UUID | null
  /** 当前有效执行的固定截止时刻（服务端判定超时用），与 `due_at` 不同。 */
  execution_deadline_at?: UtcTimestamp | null
  attachment_count?: number
  current_execution?: TaskExecutionDTO | null
  current_submission?: TaskSubmissionDTO | null
  execution_history?: TaskExecutionDTO[]
  submission_history?: TaskSubmissionDTO[]
  review_history?: TaskReviewDTO[]
}

/** `POST /tasks` 返回（create/edit/publish/withdraw/assign/archive/restore 共用详情形状）。 */
export type TaskMutationDTO = TaskDetailDTO & { idempotent_replay?: boolean }

/** `POST /tasks/{id}/claim` 的单项执行回执。 */
export interface ClaimReceiptDTO {
  task_id: UUID
  project_id: UUID
  number: number
  display_number: string
  title: string
  status: TaskStatus
  claim_mode: ClaimMode
  version: number
  assignee_agent_id: UUID
  execution_id: UUID
  claimed_at: UtcTimestamp
  execution_timeout_seconds: number
  execution_deadline_at: UtcTimestamp
  difficulty: Difficulty
  execution?: TaskExecutionDTO
  task?: TaskSummaryDTO
  idempotent_replay?: boolean
}

export interface SubmitReceiptDTO {
  task_id: UUID
  status: TaskStatus
  version: number
  submission_id: UUID
  submitted_at: UtcTimestamp
  assignee_agent_id?: UUID | null
  task?: TaskSummaryDTO
  submission?: TaskSubmissionDTO
  execution?: TaskExecutionDTO
  idempotent_replay?: boolean
}

export interface ReviewReceiptDTO {
  task_id: UUID
  status: TaskStatus
  version: number
  current_submission_id?: UUID | null
  completed_at?: UtcTimestamp | null
  rework_assignment?: string
  task?: TaskSummaryDTO
  submission?: TaskSubmissionDTO
  review?: TaskReviewDTO
  idempotent_replay?: boolean
}

export interface ReopenReceiptDTO {
  task_id: UUID
  status: TaskStatus
  version: number
  task?: TaskSummaryDTO
  idempotent_replay?: boolean
}

export interface ProgressReceiptDTO {
  task_id: UUID
  execution_id: UUID
  progress_message?: string | null
  progress_percent?: number | null
  progress_updated_at?: UtcTimestamp | null
  idempotent_replay?: boolean
}

export interface ExecutionEndReceiptDTO {
  task_id: UUID
  status: TaskStatus
  version: number
  execution_id: UUID
  end_reason?: EndReason | null
  idempotent_replay?: boolean
}

export interface ReorderReceiptDTO {
  task: TaskSummaryDTO
  sort_order: string
  version: number
  status: TaskStatus
  renumbered: boolean
  idempotent_replay?: boolean
}

// ---------------------------------------------------------------------------
// 查询参数
// ---------------------------------------------------------------------------

/**
 * 管理面列表状态筛选：`ALL` 表示不按状态过滤（一次拿到全部五态，分页跨五态）。
 * 不传 `status` 时服务端默认只回待办（Agent 侧行为），管理面必须显式传 `ALL`。
 */
export type TaskListStatusFilter = TaskStatus | 'ALL'

export const ALL_TASK_STATUSES: TaskListStatusFilter = 'ALL'

export interface TaskListQuery {
  projectId?: UUID | null
  status?: TaskListStatusFilter | null
  difficulty?: Difficulty | null
  claimMode?: ClaimMode | null
  designatedAgentId?: UUID | null
  assigneeAgentId?: UUID | null
  keyword?: string | null
  includeArchived?: boolean
  page?: number
  pageSize?: number
}

interface RequestOptions {
  signal?: AbortSignal
}

function buildTaskListParams(query: TaskListQuery): Record<string, QueryValue> {
  const params: Record<string, QueryValue> = {}
  if (query.projectId) params.project_id = query.projectId
  if (query.status) params.status = query.status
  if (query.difficulty != null) params.difficulty = query.difficulty
  if (query.claimMode) params.claim_mode = query.claimMode
  if (query.designatedAgentId) params.designated_agent_id = query.designatedAgentId
  if (query.assigneeAgentId) params.assignee_agent_id = query.assigneeAgentId
  // 服务端对空 `q` 直接 400，空关键词必须不发该参数。
  const keyword = (query.keyword ?? '').trim()
  if (keyword) params.q = keyword
  if (query.includeArchived) params.include_archived = true
  if (query.page != null) params.page = query.page
  if (query.pageSize != null) params.page_size = query.pageSize
  return params
}

/** `GET /tasks`：按项目与筛选条件分页列出任务（服务端稳定排序）。 */
export function listTasks(query: TaskListQuery = {}, options: RequestOptions = {}): Promise<Page<TaskListItemDTO>> {
  return webApiClient.get<Page<TaskListItemDTO>>('/tasks', {
    query: buildTaskListParams(query),
    signal: options.signal,
  })
}

/** `GET /tasks/{id}`：要求、验收、当前执行、历史提交与审核。 */
export function getTask(taskId: UUID, options: RequestOptions = {}): Promise<TaskDetailDTO> {
  return webApiClient.get<TaskDetailDTO>(`/tasks/${taskId}`, { signal: options.signal })
}

/** `GET /tasks/{id}/activity`：任务活动时间线（新→旧由调用方决定展示顺序）。 */
export function listTaskActivity(
  taskId: UUID,
  query: { page?: number; pageSize?: number } = {},
  options: RequestOptions = {},
): Promise<Page<ActivityEventDTO>> {
  const params: Record<string, QueryValue> = {}
  if (query.page != null) params.page = query.page
  if (query.pageSize != null) params.page_size = query.pageSize
  return webApiClient.get<Page<ActivityEventDTO>>(`/tasks/${taskId}/activity`, {
    query: params,
    signal: options.signal,
  })
}

/** `GET /tasks/{id}/attachments`：任务附件列表（含执行产物与提交产物）。 */
export function listTaskAttachments(
  taskId: UUID,
  query: { page?: number; pageSize?: number } = {},
  options: RequestOptions = {},
): Promise<Page<AttachmentDTO>> {
  const params: Record<string, QueryValue> = {}
  if (query.page != null) params.page = query.page
  if (query.pageSize != null) params.page_size = query.pageSize
  return webApiClient.get<Page<AttachmentDTO>>(`/tasks/${taskId}/attachments`, {
    query: params,
    signal: options.signal,
  })
}

/**
 * `GET /agents`：选择执行者/审核主管所需的最小只读调用（Agent 管理页由 T29 负责）。
 * 这里只读取，不修改任何 Agent 状态。
 */
export function listAgents(
  query: { role?: AgentRole; enabled?: boolean; page?: number; pageSize?: number } = {},
  options: RequestOptions = {},
): Promise<Page<AgentOptionDTO>> {
  const params: Record<string, QueryValue> = {}
  if (query.role) params.role = query.role
  if (query.enabled != null) params.enabled = query.enabled ? 'true' : 'false'
  if (query.page != null) params.page = query.page
  params.page_size = query.pageSize ?? 100
  return webApiClient.get<Page<AgentOptionDTO>>('/agents', { query: params, signal: options.signal })
}

/** 项目只读引用（看板标题与项目内筛选需要；项目页由 T30 负责）。 */
export interface ProjectRefDTO {
  id: UUID
  title: string
  status: string
  version: number
}

// ---------------------------------------------------------------------------
// 编制（authoring）
// ---------------------------------------------------------------------------

export interface CreateTaskInput {
  projectId: UUID
  title: string
  description?: string
  acceptanceCriteria?: string
  difficulty: Difficulty
  claimMode: ClaimMode
  designatedAgentId?: UUID | null
  dueAt?: string | null
  labels?: string[]
  /** 新建任务携带的暂存附件（先经 `uploadStagingFile` 上传取得的 file_id）。 */
  attachmentFileIds?: string[]
}

/** `POST /files` 的暂存上传结果：`oss_url` 是签名 OSS 下载链接（未启用 OSS 时为 null）。 */
export interface StagedFileDTO {
  id: string
  file_id: string
  original_name: string
  media_type: string
  size: number
  sha256: string
  oss_url: string | null
}

/**
 * `POST /files`：任务编制阶段的暂存上传（multipart）。
 * 文件先落库并镜像到 OSS，随后把返回的 `file_id` 放进 `createTask` 的
 * `attachment_file_ids`；服务端绑定时会把 OSS URL 写入任务提示词。
 */
export function uploadStagingFile(
  projectId: UUID,
  file: File,
  idempotencyKey: string,
): Promise<StagedFileDTO> {
  const form = new FormData()
  form.append('file', file, file.name)
  form.append('project_id', projectId)
  return webApiClient.upload<StagedFileDTO>('/files', form, { idempotencyKey })
}

/** `POST /tasks`：始终创建 PREPARING，**不自动发布**（服务端不隐式发布）。 */
export function createTask(input: CreateTaskInput, idempotencyKey: string): Promise<TaskMutationDTO> {
  const body: Record<string, unknown> = {
    project_id: input.projectId,
    title: input.title,
    description: input.description ?? '',
    acceptance_criteria: input.acceptanceCriteria ?? '',
    difficulty: input.difficulty,
    claim_mode: input.claimMode,
  }
  if (input.claimMode === 'ASSIGNED' && input.designatedAgentId) {
    body.designated_agent_id = input.designatedAgentId
  }
  if (input.dueAt) body.due_at = input.dueAt
  if (input.labels && input.labels.length > 0) body.labels = input.labels
  if (input.attachmentFileIds && input.attachmentFileIds.length > 0) {
    body.attachment_file_ids = input.attachmentFileIds
  }
  return webApiClient.post<TaskMutationDTO>('/tasks', body, { idempotencyKey })
}

export interface EditTaskInput {
  title?: string
  description?: string
  acceptanceCriteria?: string
  difficulty?: Difficulty
  dueAt?: string | null
  labels?: string[]
}

/**
 * `PATCH /tasks/{id}`：只提交变更字段 + expected_version。
 * 实质字段（description/acceptance_criteria/difficulty/title）在非 PREPARING 状态会被服务端
 * 以 409 INVALID_TRANSITION 拒绝，前端必须回显该错误而不是静默吞掉。
 */
export function editTask(
  taskId: UUID,
  expectedVersion: number,
  input: EditTaskInput,
  idempotencyKey: string,
): Promise<TaskMutationDTO> {
  const body: Record<string, unknown> = { expected_version: expectedVersion }
  if (input.title !== undefined) body.title = input.title
  if (input.description !== undefined) body.description = input.description
  if (input.acceptanceCriteria !== undefined) body.acceptance_criteria = input.acceptanceCriteria
  if (input.difficulty !== undefined) body.difficulty = input.difficulty
  if (input.dueAt !== undefined) body.due_at = input.dueAt
  if (input.labels !== undefined) body.labels = input.labels
  return webApiClient.patch<TaskMutationDTO>(`/tasks/${taskId}`, body, { idempotencyKey })
}

/** `POST /tasks/{id}/publish`：PREPARING → TODO。失败原因原样抛出。 */
export function publishTask(
  taskId: UUID,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<TaskMutationDTO> {
  return webApiClient.post<TaskMutationDTO>(
    `/tasks/${taskId}/publish`,
    { expected_version: expectedVersion },
    { idempotencyKey },
  )
}

/** `POST /tasks/{id}/withdraw`：TODO → PREPARING，原因为必填。 */
export function withdrawTask(
  taskId: UUID,
  expectedVersion: number,
  reason: string,
  idempotencyKey: string,
): Promise<TaskMutationDTO> {
  return webApiClient.post<TaskMutationDTO>(
    `/tasks/${taskId}/withdraw`,
    { expected_version: expectedVersion, reason },
    { idempotencyKey },
  )
}

/** `POST /tasks/{id}/assign`：修改 OPEN/ASSIGNED 与指定 Agent（仅准备中/待办）。 */
export function assignTask(
  taskId: UUID,
  expectedVersion: number,
  claimMode: ClaimMode,
  designatedAgentId: UUID | null,
  idempotencyKey: string,
): Promise<TaskMutationDTO> {
  const body: Record<string, unknown> = {
    expected_version: expectedVersion,
    claim_mode: claimMode,
  }
  if (claimMode === 'ASSIGNED' && designatedAgentId) body.designated_agent_id = designatedAgentId
  return webApiClient.post<TaskMutationDTO>(`/tasks/${taskId}/assign`, body, { idempotencyKey })
}

/** `POST /tasks/{id}/archive`：独立标记，不改五态；原因为必填。 */
export function archiveTask(
  taskId: UUID,
  expectedVersion: number,
  reason: string,
  idempotencyKey: string,
): Promise<TaskMutationDTO> {
  return webApiClient.post<TaskMutationDTO>(
    `/tasks/${taskId}/archive`,
    { expected_version: expectedVersion, reason },
    { idempotencyKey },
  )
}

/** `POST /tasks/{id}/restore`：清除归档标记。 */
export function restoreTask(
  taskId: UUID,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<TaskMutationDTO> {
  return webApiClient.post<TaskMutationDTO>(
    `/tasks/${taskId}/restore`,
    { expected_version: expectedVersion },
    { idempotencyKey },
  )
}

// ---------------------------------------------------------------------------
// 批量归档 / 批量恢复（管理面逐项结果）
// ---------------------------------------------------------------------------

/** 批量结果里成功的一项（服务端 `BulkOutcome.add_success`）。 */
export interface BulkItemSuccessDTO {
  id: UUID
  /** 仅项目软删除返回；任务归档只改 `is_archived`，状态保持不变。 */
  deleted_at?: UtcTimestamp | null
  status?: TaskStatus | string | null
  idempotent_replay?: boolean
}

/** 批量结果里失败的一项：业务错误码与原因原样呈现，一项失败不影响其他项。 */
export interface BulkItemFailureDTO {
  id: UUID
  code: string
  message: string
  details?: Record<string, unknown>
}

/** `POST /tasks/bulk-*` 的逐项结果信封（`domain/bulk.py: bulk_result`）。 */
export interface BulkOperationResultDTO {
  operation: string
  requested: number
  succeeded: BulkItemSuccessDTO[]
  failed: BulkItemFailureDTO[]
  actor_type: string
}

/**
 * `POST /tasks/bulk-archive`：批量归档（= 管理面的批量删除，只改归档标记）。
 * `reason` 必填；有有效执行或处于待审核的项会以 `INVALID_TRANSITION` 失败。
 */
export function bulkArchiveTasks(
  taskIds: UUID[],
  reason: string,
  idempotencyKey: string,
): Promise<BulkOperationResultDTO> {
  return webApiClient.post<BulkOperationResultDTO>(
    '/tasks/bulk-archive',
    { task_ids: taskIds, reason },
    { idempotencyKey },
  )
}

/** `POST /tasks/bulk-restore`：批量清除归档标记（五态与历史全部保留）。 */
export function bulkRestoreTasks(
  taskIds: UUID[],
  idempotencyKey: string,
): Promise<BulkOperationResultDTO> {
  return webApiClient.post<BulkOperationResultDTO>(
    '/tasks/bulk-restore',
    { task_ids: taskIds },
    { idempotencyKey },
  )
}

// ---------------------------------------------------------------------------
// 执行（claim / progress / release / reclaim）
// ---------------------------------------------------------------------------

/**
 * `POST /tasks/{id}/claim`：Web 代领必须给出 agent_id 与 expected_version，
 * 返回唯一一条执行回执（execution_id / 固定超时秒数 / 固定截止时刻）。
 */
export function claimTask(
  taskId: UUID,
  agentId: UUID,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<ClaimReceiptDTO> {
  return webApiClient.post<ClaimReceiptDTO>(
    `/tasks/${taskId}/claim`,
    { agent_id: agentId, expected_version: expectedVersion },
    { idempotencyKey },
  )
}

/** `POST /tasks/{id}/progress`：只记录进度，不延长截止、不隐式提交。 */
export function updateProgress(
  taskId: UUID,
  executionId: UUID,
  message: string,
  percent: number | null,
  idempotencyKey: string,
): Promise<ProgressReceiptDTO> {
  const body: Record<string, unknown> = { execution_id: executionId, message }
  if (percent != null) body.percent = percent
  return webApiClient.post<ProgressReceiptDTO>(`/tasks/${taskId}/progress`, body, { idempotencyKey })
}

/** `POST /tasks/{id}/reclaim`：主管强制回收当前执行（原因必填），把任务退回待办。 */
export function reclaimTask(
  taskId: UUID,
  executionId: UUID,
  expectedVersion: number,
  reason: string,
  idempotencyKey: string,
): Promise<ExecutionEndReceiptDTO> {
  return webApiClient.post<ExecutionEndReceiptDTO>(
    `/tasks/${taskId}/reclaim`,
    { execution_id: executionId, expected_version: expectedVersion, reason },
    { idempotencyKey },
  )
}

/**
 * `POST /tasks/{id}/release`：释放是执行者自助动作。
 * 契约规定 Web 来源固定返回 403 `ROLE_FORBIDDEN`，因此页面不提供该按钮，只保留回收；
 * 该封装仅为完整映射已注册端点而存在。
 */
export function releaseTask(
  taskId: UUID,
  executionId: UUID,
  expectedVersion: number,
  reason: string,
  idempotencyKey: string,
): Promise<ExecutionEndReceiptDTO> {
  return webApiClient.post<ExecutionEndReceiptDTO>(
    `/tasks/${taskId}/release`,
    { execution_id: executionId, expected_version: expectedVersion, reason },
    { idempotencyKey },
  )
}

// ---------------------------------------------------------------------------
// 提交 / 审核 / 重开
// ---------------------------------------------------------------------------

export interface SubmitTaskInput {
  executionId: UUID
  expectedVersion: number
  summary: string
  content?: string
  selfCheck?: SelfCheckItemDTO[]
  artifacts?: UUID[]
  links?: SubmissionLinkDTO[]
}

/** `POST /tasks/{id}/submit`：正式结果唯一，结束当前执行并进入待审核。 */
export function submitTask(
  taskId: UUID,
  input: SubmitTaskInput,
  idempotencyKey: string,
): Promise<SubmitReceiptDTO> {
  const body: Record<string, unknown> = {
    execution_id: input.executionId,
    expected_version: input.expectedVersion,
    summary: input.summary,
    content: input.content ?? '',
    self_check: input.selfCheck ?? [],
    artifacts: input.artifacts ?? [],
    links: input.links ?? [],
  }
  return webApiClient.post<SubmitReceiptDTO>(`/tasks/${taskId}/submit`, body, { idempotencyKey })
}

export interface ReviewTaskInput {
  submissionId: UUID
  reviewerAgentId: UUID
  decision: ReviewDecision
  comment?: string
  expectedVersion: number
}

/**
 * `POST /tasks/{id}/review`：Web 必须显式选择一名有效主管；
 * 服务端记录 `reviewer_identity_verified=false`，驳回原因必填。
 */
export function reviewTask(
  taskId: UUID,
  input: ReviewTaskInput,
  idempotencyKey: string,
): Promise<ReviewReceiptDTO> {
  const body: Record<string, unknown> = {
    submission_id: input.submissionId,
    reviewer_agent_id: input.reviewerAgentId,
    decision: input.decision,
    comment: input.comment ?? '',
    expected_version: input.expectedVersion,
  }
  return webApiClient.post<ReviewReceiptDTO>(`/tasks/${taskId}/review`, body, { idempotencyKey })
}

/** `POST /tasks/{id}/reopen`：DONE → TODO，原因为必填，历史保留。 */
export function reopenTask(
  taskId: UUID,
  expectedVersion: number,
  reason: string,
  idempotencyKey: string,
): Promise<ReopenReceiptDTO> {
  return webApiClient.post<ReopenReceiptDTO>(
    `/tasks/${taskId}/reopen`,
    { expected_version: expectedVersion, reason },
    { idempotencyKey },
  )
}

// ---------------------------------------------------------------------------
// 附件
// ---------------------------------------------------------------------------

/**
 * `POST /tasks/{id}/attachments`：multipart 上传。
 * 提交产物必须先以 `purpose=EXECUTION_ARTIFACT` 且带 `execution_id` 上传绑定，
 * 正式提交时只引用返回的 `file_id`。
 */
export function uploadTaskAttachment(
  taskId: UUID,
  input: { file: File; purpose: 'TASK_REFERENCE' | 'EXECUTION_ARTIFACT'; executionId?: UUID },
  idempotencyKey: string,
): Promise<AttachmentDTO> {
  const form = new FormData()
  form.append('file', input.file, input.file.name)
  form.append('purpose', input.purpose)
  if (input.executionId) form.append('execution_id', input.executionId)
  return webApiClient.upload<AttachmentDTO>(`/tasks/${taskId}/attachments`, form, { idempotencyKey })
}

// ---------------------------------------------------------------------------
// 列内排序
// ---------------------------------------------------------------------------

export interface ReorderTaskInput {
  taskId: UUID
  expectedVersion: number
  /** 真实/可见相邻卡片；至少一个。服务端会自行解析全列真实邻接关系。 */
  beforeTaskId?: UUID | null
  afterTaskId?: UUID | null
  /** 只能等于任务当前状态：服务端用它拒绝“伪装成排序的跨列移动”。 */
  status: TaskStatus
}

/**
 * `POST /projects/{id}/task-order`：列内排序。
 * 只改 sort_order 与 version，绝不改状态、执行者或列。
 */
export function reorderTask(
  projectId: UUID,
  input: ReorderTaskInput,
  idempotencyKey: string,
): Promise<ReorderReceiptDTO> {
  const body: Record<string, unknown> = {
    task_id: input.taskId,
    expected_version: input.expectedVersion,
    status: input.status,
  }
  if (input.beforeTaskId) body.before_task_id = input.beforeTaskId
  if (input.afterTaskId) body.after_task_id = input.afterTaskId
  return webApiClient.post<ReorderReceiptDTO>(`/projects/${projectId}/task-order`, body, {
    idempotencyKey,
  })
}
