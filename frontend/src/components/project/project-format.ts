/**
 * 项目页面的展示规则（T30 / D08 / D09）。
 *
 * 关键点：
 * - 生命周期与任务状态分开表达：`ProjectStatus` 只来自服务端；
 * - 归档项目只读，仅保留“恢复”入口；
 * - 文档展示版本 / 大小 / 上传者 / 时间，并明确“不支持在线编辑”。
 */

import type { ApiError } from '@/api/client'
import type { ProjectStatus } from '@/api/types'
import type { DocumentVersionDTO, ProjectSummaryDTO } from '@/api/projects'

export const PROJECT_STATUS_LABEL: Readonly<Record<ProjectStatus, string>> = {
  DRAFT: '草稿',
  ACTIVE: '进行中',
  COMPLETED: '已结项',
  ARCHIVED: '已归档',
}

export const PROJECT_STATUS_ORDER: readonly ProjectStatus[] = ['DRAFT', 'ACTIVE', 'COMPLETED', 'ARCHIVED']

export function projectStatusLabel(status: ProjectStatus | string | null | undefined): string {
  if (!status) return '未知状态'
  return PROJECT_STATUS_LABEL[status as ProjectStatus] ?? String(status)
}

/**
 * 生命周期徽标配色（共享 `.badge--*` 类）。
 *
 * 颜色只是辅助：徽标文字始终由 `projectStatusLabel` 独立表达状态（UI-DESIGN §1、产品 §3.2）。
 */
export function projectStatusBadgeClass(
  status: ProjectStatus | string | null | undefined,
): string {
  switch (status) {
    case 'DRAFT':
      return 'badge--info'
    case 'ACTIVE':
      return 'badge--success'
    case 'COMPLETED':
      return 'badge--primary'
    default:
      return 'badge--neutral'
  }
}

export type ProjectAction = 'activate' | 'complete' | 'archive' | 'restore'

export interface LifecycleActionOption {
  action: ProjectAction
  label: string
  needsReason: boolean
}

/** 与 CONTRACT §9 / D08 一致的可执行动作；其余状态组合一律不提供按钮。 */
export function lifecycleActions(project: Pick<ProjectSummaryDTO, 'status'>): LifecycleActionOption[] {
  switch (project.status) {
    case 'DRAFT':
      return [
        { action: 'activate', label: '启用项目', needsReason: false },
        { action: 'archive', label: '归档项目', needsReason: true },
      ]
    case 'ACTIVE':
      return [
        { action: 'complete', label: '结项', needsReason: false },
        { action: 'archive', label: '归档项目', needsReason: true },
      ]
    case 'COMPLETED':
      return [
        { action: 'activate', label: '重新启用', needsReason: false },
        { action: 'archive', label: '归档项目', needsReason: true },
      ]
    case 'ARCHIVED':
      return [{ action: 'restore', label: '恢复项目（退出只读）', needsReason: false }]
    default:
      return []
  }
}

/** 归档项目：只读（不能编辑背景、成员、文档），仅保留恢复入口。 */
export function isProjectReadOnly(
  project: { status: ProjectStatus } | null | undefined,
): boolean {
  return project?.status === 'ARCHIVED'
}

// ---------------------------------------------------------------------------
// 软删除（删除 = 冻结为只读 + 删除标记；恢复清空标记）
// ---------------------------------------------------------------------------

/** 是否处于「已删除」状态：只看服务端返回的 `deleted_at`，不由前端推断。 */
export function isProjectDeleted(
  project: { deleted_at?: string | null } | null | undefined,
): boolean {
  return Boolean(project?.deleted_at)
}

/** 删除原因（服务端 `deleted_reason`，空值时不编造文案）。 */
export function projectDeletedReason(
  project: { deleted_reason?: string | null } | null | undefined,
): string {
  return (project?.deleted_reason ?? '').trim()
}

/**
 * 删除确认文案：明确「删除 = 软删除」的语义，避免被理解为物理删除。
 */
export const PROJECT_DELETE_HINT =
  '删除为软删除：项目冻结为只读并写入删除标记，任务执行、提交与审核历史全部保留，可随时恢复。'

/** 删除/恢复门禁类错误的中文补充（仍保留服务端原话）。 */
export const DELETE_ERROR_HINTS: Readonly<Record<string, string>> = {
  INVALID_TRANSITION: '存在进行中或待审核的任务：请先在看板处理完这些任务，或改用具名归档流程。',
  VERSION_CONFLICT: '项目已被其他操作更新：已按最新版本重试入口刷新列表，请确认后重试。',
  RESOURCE_NOT_FOUND: '项目不存在或当前身份无权访问。',
}

export function deleteErrorHint(code: string | null | undefined): string {
  if (!code) return ''
  return DELETE_ERROR_HINTS[code] ?? ''
}

export function formatBytes(size: number | null | undefined): string {
  if (size == null || Number.isNaN(size)) return '—'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(2)} MB`
}

/** 上传者：Web 代操作固定记为本地管理员（服务端 `uploaded_by_type`）。 */
export function uploaderLabel(version: DocumentVersionDTO | null | undefined): string {
  if (!version) return '—'
  if (version.uploaded_by_type === 'web_operator') return '本地管理员（Web）'
  if (version.uploaded_by_type === 'agent') return `Agent ${version.uploaded_by_agent_id ?? ''}`.trim()
  return version.uploaded_by_type
}

export function documentVersionSummary(version: DocumentVersionDTO | null | undefined): string {
  if (!version) return '无版本'
  return `v${version.number} · ${formatBytes(version.size)} · ${uploaderLabel(version)} · ${version.created_at}`
}

/** 服务端拒绝文案：直接展示原话（含错误码与请求号），不改写成成功。 */
export function describeProjectError(error: ApiError | null): string {
  return error ? error.displayMessage : ''
}

/** 归档/结项/撤权等门禁错误的中文补充说明（仍保留服务端原话）。 */
export const ERROR_HINTS: Readonly<Record<string, string>> = {
  LAST_SUPERVISOR_REQUIRED: '项目必须保留至少一名已启用主管：请先新增或启用一名主管成员。',
  INVALID_TRANSITION: '该动作在当前状态下被服务端拒绝：请先处理未完成任务或恢复项目。',
  VERSION_CONFLICT: '项目已被其他操作更新：请刷新后重试，已填写的内容会保留。',
  MEMBERSHIP_CONFLICT: '该 Agent 已经是项目成员。',
  DOCUMENT_CONFLICT: '已存在同名文档：请选择“上传为新版本”或“新建同名文档”。',
  PROJECT_NOT_ACTIVE: '项目当前不是进行中状态，该操作被拒绝。',
}

export function errorHint(error: ApiError | null): string {
  if (!error) return ''
  return ERROR_HINTS[error.code] ?? ''
}
