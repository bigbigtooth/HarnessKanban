/**
 * 项目域 WebAPI 封装（T30）。
 *
 * 契约来源：docs/CONTRACT.md §3/§8/§9、docs/DEVELOP.md D08/D09、
 * docs/product-design.md §2/§5/§13，字段与 `backend/webapi/routes/projects.py`、
 * `projects_lifecycle.py`、`documents.py`、`files.py` 以及
 * `domain/presenters.py`（`project_summary` / `document_view` / `document_version_view`）
 * 一一对应。
 *
 * 规则：
 * - 生命周期只走动作端点（activate/complete/archive/restore），绝不 PATCH `status`；
 * - **背景信息单独修改**：服务端拒绝与其他字段混在同一请求里（否则 400），
 *   每次背景更新都会推进 `content_version` 并追加一个不可变上下文版本；
 * - 文档上传为 multipart；同名冲突由服务端 409 `DOCUMENT_CONFLICT` 判定，
 *   前端必须显式让用户选择“新版本”或“新文档”，不做静默覆盖；
 * - 移出参考列表 = DELETE 文档（只把 `is_archived` 置真，历史版本仍可读）；
 * - 所有写操作由调用方提供 Idempotency-Key，同一次逻辑操作的重试复用同一个键。
 */

import { webApiClient, type QueryValue } from '@/api/client'
import type { AgentRole, Page, ProjectStatus, UUID, UtcTimestamp } from '@/api/types'

// ---------------------------------------------------------------------------
// DTO（与后端 presenter 字段一一对应）
// ---------------------------------------------------------------------------

/** `project_summary`。 */
export interface ProjectSummaryDTO {
  id: UUID
  title: string
  summary?: string | null
  status: ProjectStatus
  archived_from_status?: ProjectStatus | null
  /** 软删除标记（删除 = 冻结为只读 + 打标记，数据不物理删除）。 */
  deleted_at?: UtcTimestamp | null
  deleted_reason?: string | null
  content_version: number
  version: number
  created_at?: UtcTimestamp | null
  updated_at?: UtcTimestamp | null
}

/** `project_detail`（= summary + background）与当前上下文版本指针。 */
export interface ProjectDetailDTO extends ProjectSummaryDTO {
  background?: string | null
  current_context_version_id?: UUID | null
  current_context_version_number?: number | null
}

/** `context_version_view`。 */
export interface ProjectContextVersionDTO {
  id: UUID
  project_id: UUID
  number: number
  background_snapshot?: string | null
  document_version_refs?: Array<Record<string, unknown>>
  created_at: UtcTimestamp
}

/** 项目写动作回执（生命周期 / 元数据 / 背景共用详情形状）。 */
export type ProjectMutationDTO = ProjectDetailDTO & {
  idempotent_replay?: boolean
  /** 背景更新与文档变更会返回新上下文版本。 */
  current_context_version?: ProjectContextVersionDTO | null
}

/** `member_view`。 */
export interface ProjectMemberDTO {
  id: UUID
  project_id: UUID
  agent_id: UUID
  agent_name: string
  role: AgentRole
  enabled: boolean
  online_state?: string | null
  last_heartbeat_at?: UtcTimestamp | null
  created_at: UtcTimestamp
}

/** `DELETE /projects/{id}/members/{agent_id}` 回执。 */
export interface MemberRemovalDTO {
  membership_id: UUID
  project_id: UUID
  agent_id: UUID
  role: AgentRole
  disposition: 'WAIT' | 'NOW'
  reason: string
  reclaimed_execution_ids: UUID[]
  idempotent_replay?: boolean
}

/** `document_version_view`。 */
export interface DocumentVersionDTO {
  id: UUID
  document_id: UUID
  number: number
  file_id: UUID
  original_name: string
  media_type: string
  size: number
  sha256?: string
  uploaded_by_type: string
  uploaded_by_agent_id?: UUID | null
  created_at: UtcTimestamp
}

/** `_document_item`（document_view + current_version）。 */
export interface DocumentItemDTO {
  id: UUID
  project_id: UUID
  name: string
  is_archived: boolean
  current_version_id?: UUID | null
  current_version_number?: number | null
  file_id?: UUID | null
  created_at?: UtcTimestamp | null
  updated_at?: UtcTimestamp | null
  current_version?: DocumentVersionDTO | null
  /** 上传/移出返回的新上下文版本（列表项没有）。 */
  current_context_version?: ProjectContextVersionDTO | null
}

/** `GET /documents/{id}`：含 append-only 版本历史。 */
export interface DocumentDetailDTO extends DocumentItemDTO {
  versions: DocumentVersionDTO[]
  already_archived?: boolean
}

// ---------------------------------------------------------------------------
// 查询与读
// ---------------------------------------------------------------------------

export interface ProjectListQuery {
  status?: ProjectStatus | null
  includeArchived?: boolean
  /** `deleted=true` 只看已删除的项目（回收站视图）；默认只看未删除的。 */
  deleted?: boolean
  page?: number
  pageSize?: number
}

interface RequestOptions {
  signal?: AbortSignal
}

function buildProjectListParams(query: ProjectListQuery): Record<string, QueryValue> {
  const params: Record<string, QueryValue> = {}
  if (query.status) params.status = query.status
  params.include_archived = query.includeArchived === true
  if (query.deleted) params.deleted = true
  if (query.page != null) params.page = query.page
  if (query.pageSize != null) params.page_size = query.pageSize
  return params
}

/** `GET /projects`：按状态筛选、可选包含归档（默认包含，服务端默认也是包含）。 */
export function listProjectsPage(
  query: ProjectListQuery = {},
  options: RequestOptions = {},
): Promise<Page<ProjectSummaryDTO>> {
  return webApiClient.get<Page<ProjectSummaryDTO>>('/projects', {
    query: buildProjectListParams(query),
    signal: options.signal,
  })
}

/** `GET /projects/{id}`：详情 + 当前上下文版本号。 */
export function getProject(projectId: UUID, options: RequestOptions = {}): Promise<ProjectDetailDTO> {
  return webApiClient.get<ProjectDetailDTO>(`/projects/${projectId}`, { signal: options.signal })
}

// ---------------------------------------------------------------------------
// 创建 / 元数据 / 背景
// ---------------------------------------------------------------------------

export interface CreateProjectInput {
  title: string
  summary?: string
  background?: string
}

/** `POST /projects`：始终创建为 DRAFT（服务端不接受 status 字段）。 */
export function createProject(
  input: CreateProjectInput,
  idempotencyKey: string,
): Promise<ProjectMutationDTO> {
  const body: Record<string, unknown> = { title: input.title }
  if (input.summary !== undefined) body.summary = input.summary
  if (input.background !== undefined) body.background = input.background
  return webApiClient.post<ProjectMutationDTO>('/projects', body, { idempotencyKey })
}

export interface UpdateProjectMetadataInput {
  title?: string
  summary?: string
}

/** `PATCH /projects/{id}`：只改标题/摘要；带 `background` 会被服务端拒绝（必须单独请求）。 */
export function updateProjectMetadata(
  projectId: UUID,
  expectedVersion: number,
  input: UpdateProjectMetadataInput,
  idempotencyKey: string,
): Promise<ProjectMutationDTO> {
  const body: Record<string, unknown> = { expected_version: expectedVersion }
  if (input.title !== undefined) body.title = input.title
  if (input.summary !== undefined) body.summary = input.summary
  return webApiClient.patch<ProjectMutationDTO>(`/projects/${projectId}`, body, { idempotencyKey })
}

/**
 * `PATCH /projects/{id}`（仅 background）：独立请求，服务端据此推进 `content_version`
 * 并追加新的上下文版本；响应带 `current_context_version`。
 */
export function updateProjectBackground(
  projectId: UUID,
  background: string,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<ProjectMutationDTO> {
  return webApiClient.patch<ProjectMutationDTO>(
    `/projects/${projectId}`,
    { background, expected_version: expectedVersion },
    { idempotencyKey },
  )
}

// ---------------------------------------------------------------------------
// 生命周期（动作端点，绝不 PATCH status）
// ---------------------------------------------------------------------------

/** `POST /projects/{id}/activate`：DRAFT/COMPLETED → ACTIVE；需要已启用主管成员。 */
export function activateProject(
  projectId: UUID,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<ProjectMutationDTO> {
  return webApiClient.post<ProjectMutationDTO>(
    `/projects/${projectId}/activate`,
    { expected_version: expectedVersion },
    { idempotencyKey },
  )
}

/** `POST /projects/{id}/complete`：ACTIVE → COMPLETED；要求所有未归档任务 DONE。 */
export function completeProject(
  projectId: UUID,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<ProjectMutationDTO> {
  return webApiClient.post<ProjectMutationDTO>(
    `/projects/${projectId}/complete`,
    { expected_version: expectedVersion },
    { idempotencyKey },
  )
}

/** `POST /projects/{id}/archive`：任意 → ARCHIVED；原因必填；进行中/待审核任务会阻止。 */
export function archiveProject(
  projectId: UUID,
  expectedVersion: number,
  reason: string,
  idempotencyKey: string,
): Promise<ProjectMutationDTO> {
  return webApiClient.post<ProjectMutationDTO>(
    `/projects/${projectId}/archive`,
    { expected_version: expectedVersion, reason },
    { idempotencyKey },
  )
}

/** `POST /projects/{id}/restore`：ARCHIVED → 归档前状态（缺省 DRAFT）。 */
export function restoreProject(
  projectId: UUID,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<ProjectMutationDTO> {
  return webApiClient.post<ProjectMutationDTO>(
    `/projects/${projectId}/restore`,
    { expected_version: expectedVersion },
    { idempotencyKey },
  )
}

/**
 * `POST /projects/{id}/delete`：软删除（冻结为只读 + 删除标记），`reason` 必填。
 * 存在进行中/待审核任务时服务端返回 409 `INVALID_TRANSITION`，原因原样抛出。
 */
export function deleteProject(
  projectId: UUID,
  reason: string,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<ProjectMutationDTO> {
  return webApiClient.post<ProjectMutationDTO>(
    `/projects/${projectId}/delete`,
    { reason, expected_version: expectedVersion },
    { idempotencyKey },
  )
}

// ---------------------------------------------------------------------------
// 批量删除 / 批量恢复（逐项结果，一项失败不影响其他项）
// ---------------------------------------------------------------------------

/** 批量结果里成功的一项（`domain/bulk.py: BulkOutcome.add_success`）。 */
export interface ProjectBulkItemSuccessDTO {
  id: UUID
  deleted_at?: UtcTimestamp | null
  status?: ProjectStatus | string | null
  idempotent_replay?: boolean
}

/** 批量结果里失败的一项：业务错误码与原因原样呈现。 */
export interface ProjectBulkItemFailureDTO {
  id: UUID
  code: string
  message: string
  details?: Record<string, unknown>
}

/** `POST /projects/bulk-*` 的逐项结果信封。 */
export interface ProjectBulkResultDTO {
  operation: string
  requested: number
  succeeded: ProjectBulkItemSuccessDTO[]
  failed: ProjectBulkItemFailureDTO[]
  actor_type: string
}

/** `POST /projects/bulk-delete`：批量软删除，`reason` 必填。 */
export function bulkDeleteProjects(
  projectIds: UUID[],
  reason: string,
  idempotencyKey: string,
): Promise<ProjectBulkResultDTO> {
  return webApiClient.post<ProjectBulkResultDTO>(
    '/projects/bulk-delete',
    { project_ids: projectIds, reason },
    { idempotencyKey },
  )
}

/** `POST /projects/bulk-restore`：批量恢复到删除前的生命周期。 */
export function bulkRestoreProjects(
  projectIds: UUID[],
  idempotencyKey: string,
): Promise<ProjectBulkResultDTO> {
  return webApiClient.post<ProjectBulkResultDTO>(
    '/projects/bulk-restore',
    { project_ids: projectIds },
    { idempotencyKey },
  )
}

// ---------------------------------------------------------------------------
// 成员
// ---------------------------------------------------------------------------

/** `GET /projects/{id}/members`：授权成员（角色取自 Agent）。 */
export function listMembers(
  projectId: UUID,
  query: { page?: number; pageSize?: number } = {},
  options: RequestOptions = {},
): Promise<Page<ProjectMemberDTO>> {
  const params: Record<string, QueryValue> = { page_size: query.pageSize ?? 100 }
  if (query.page != null) params.page = query.page
  return webApiClient.get<Page<ProjectMemberDTO>>(`/projects/${projectId}/members`, {
    query: params,
    signal: options.signal,
  })
}

/** `PUT /projects/{id}/members/{agent_id}`：授权（已存在则 409 MEMBERSHIP_CONFLICT）。 */
export function addMember(
  projectId: UUID,
  agentId: UUID,
  idempotencyKey: string,
): Promise<ProjectMemberDTO> {
  return webApiClient.put<ProjectMemberDTO>(`/projects/${projectId}/members/${agentId}`, {}, {
    idempotencyKey,
  })
}

export interface RemoveMemberInput {
  /** 成员仍持有未结束执行时服务端要求显式选择；无执行时可省略。 */
  disposition?: 'WAIT' | 'NOW'
  reason?: string
}

/**
 * `DELETE /projects/{id}/members/{agent_id}`：撤权。
 * 持有未结束执行时必须显式选择 `WAIT`（等固定截止）或 `NOW`（立即回收，原因必填），
 * 否则服务端 400；`NOW` 会在同一事务内结束执行、任务退回待办。
 */
export function removeMember(
  projectId: UUID,
  agentId: UUID,
  input: RemoveMemberInput,
  idempotencyKey: string,
): Promise<MemberRemovalDTO> {
  const body: Record<string, unknown> = {}
  if (input.disposition) body.disposition = input.disposition
  if (input.reason !== undefined) body.reason = input.reason
  return webApiClient.request<MemberRemovalDTO>(`/projects/${projectId}/members/${agentId}`, {
    method: 'DELETE',
    body,
    idempotencyKey,
  })
}

// ---------------------------------------------------------------------------
// 文档
// ---------------------------------------------------------------------------

/** `GET /projects/{id}/documents`：当前参考列表（不含已移出）。 */
export function listProjectDocuments(
  projectId: UUID,
  query: { page?: number; pageSize?: number } = {},
  options: RequestOptions = {},
): Promise<Page<DocumentItemDTO>> {
  const params: Record<string, QueryValue> = { page_size: query.pageSize ?? 50 }
  if (query.page != null) params.page = query.page
  return webApiClient.get<Page<DocumentItemDTO>>(`/projects/${projectId}/documents`, {
    query: params,
    signal: options.signal,
  })
}

/** `GET /documents/{id}`：详情 + 版本历史。 */
export function getDocument(documentId: UUID, options: RequestOptions = {}): Promise<DocumentDetailDTO> {
  return webApiClient.get<DocumentDetailDTO>(`/documents/${documentId}`, { signal: options.signal })
}

export interface UploadDocumentInput {
  file: File
  /** 省略时用文件名作为文档名。 */
  name?: string
  /** 明确选择“新建同名文档”时才置 true（服务端据此放行同名）。 */
  allowDuplicateName?: boolean
}

/**
 * `POST /projects/{id}/documents`：上传新文档（multipart）。
 * 同名时服务端返回 409 `DOCUMENT_CONFLICT`，调用方必须让用户显式选择新版本或新文档。
 */
export function uploadProjectDocument(
  projectId: UUID,
  input: UploadDocumentInput,
  idempotencyKey: string,
): Promise<DocumentItemDTO> {
  const form = new FormData()
  form.append('file', input.file, input.file.name)
  if (input.name) form.append('name', input.name)
  if (input.allowDuplicateName) form.append('allow_duplicate_name', 'true')
  return webApiClient.upload<DocumentItemDTO>(`/projects/${projectId}/documents`, form, {
    idempotencyKey,
  })
}

/** `POST /documents/{id}/versions`：同一文档的新版本（number + 1，永不覆盖历史）。 */
export function uploadDocumentVersion(
  documentId: UUID,
  file: File,
  idempotencyKey: string,
): Promise<DocumentItemDTO> {
  const form = new FormData()
  form.append('file', file, file.name)
  return webApiClient.upload<DocumentItemDTO>(`/documents/${documentId}/versions`, form, {
    idempotencyKey,
  })
}

/** `DELETE /documents/{id}`：移出当前参考列表（历史版本与文件仍保留可读）。 */
export function archiveDocument(
  documentId: UUID,
  idempotencyKey: string,
): Promise<DocumentItemDTO> {
  return webApiClient.request<DocumentItemDTO>(`/documents/${documentId}`, {
    method: 'DELETE',
    body: {},
    idempotencyKey,
  })
}

/**
 * 受控下载地址（`GET /files/{id}/download`）。
 * 服务端逐次做权限校验；前端只提供链接，不伪造本地副本。
 */
export function fileDownloadUrl(fileId: UUID): string {
  return `${webApiClient.baseUrl}/files/${fileId}/download`
}
