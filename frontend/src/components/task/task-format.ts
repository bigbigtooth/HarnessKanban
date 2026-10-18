/**
 * 任务看板展示与动作映射（T31/T32）。
 *
 * 这里只做展示层格式化与「状态 → 业务动作」的映射，不持有任何业务判定：
 * - 超时/是否允许提交等判断一律以服务端返回为准，前端倒计时只是展示；
 * - 所有文案都带文字状态，不依赖颜色表达（product §3.2）。
 */

import type { ApiError } from '@/api/client'
import {
  DIFFICULTY_LABEL,
  TaskStatus,
  type ClaimMode,
  type Difficulty,
  type EndReason,
  type TaskStatus as TaskStatusType,
} from '@/api/types'
import type { TaskDetailDTO, TaskListItemDTO } from '@/api/tasks'

// ---------------------------------------------------------------------------
// 状态与枚举文案
// ---------------------------------------------------------------------------

export const COLUMN_LABELS: Readonly<Record<TaskStatusType, string>> = {
  [TaskStatus.PREPARING]: '准备中',
  [TaskStatus.TODO]: '待办',
  [TaskStatus.IN_PROGRESS]: '进行中',
  [TaskStatus.IN_REVIEW]: '待验收',
  [TaskStatus.DONE]: '已完成',
}

export function statusLabel(status: TaskStatusType): string {
  return COLUMN_LABELS[status] ?? status
}

export function difficultyLabel(difficulty: Difficulty | null | undefined): string {
  if (difficulty == null) return '未知难度'
  return DIFFICULTY_LABEL[difficulty] ?? `难度 ${difficulty}`
}

export function claimModeLabel(mode: ClaimMode | null | undefined): string {
  if (mode === 'OPEN') return '开放抢单'
  if (mode === 'ASSIGNED') return '指定领取'
  return '领取模式未知'
}

export function endReasonLabel(reason: EndReason | null | undefined): string {
  switch (reason) {
    case 'RELEASE':
      return '主动释放'
    case 'RECLAIM':
      return '主管回收'
    case 'TIMEOUT':
      return '超时回收'
    case 'SUBMIT':
      return '正式提交'
    default:
      return '执行结束'
  }
}

export function reviewDecisionLabel(decision: string | null | undefined): string {
  if (decision === 'APPROVE') return '通过'
  if (decision === 'REJECT') return '驳回'
  return '未决策'
}

/** 活动动作名 → 中文文案（历史动作命名不统一，这里集中映射，见 DELIVERY-STATUS §7.3）。 */
export function activityActionLabel(action: string): string {
  const map: Record<string, string> = {
    created: '创建任务',
    updated: '更新任务',
    published: '发布任务',
    withdrawn: '撤回发布',
    assigned: '修改领取安排',
    archived: '归档任务',
    restored: '恢复任务',
    claimed: '领取任务',
    'claim.no_matching_task': '自动领取无匹配任务',
    'task.reordered': '列内排序',
    'task.submitted': '提交结果',
    'task.approved': '审核通过',
    'task.rejected': '审核驳回',
    'task.reopened': '重新打开',
    progress_updated: '更新进度',
    released: '释放执行',
    reclaimed: '回收执行',
    timed_out: '超时回收',
  }
  return map[action] ?? action
}

// ---------------------------------------------------------------------------
// 时间展示（UTC 存储，默认按 Asia/Shanghai 显示并标注时区）
// ---------------------------------------------------------------------------

const SHANGHAI_PARTS = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

export const DISPLAY_TIME_ZONE = 'Asia/Shanghai'

/** ISO 字符串 → `YYYY-MM-DD HH:mm:ss`（Asia/Shanghai）。无效值显示 `—`。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '—'
  const parts = SHANGHAI_PARTS.formatToParts(parsed)
  const pick = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((part) => part.type === type)?.value ?? '00'
  return `${pick('year')}-${pick('month')}-${pick('day')} ${pick('hour')}:${pick('minute')}:${pick('second')}`
}

/** 带时区标注的展示文案（明确“时间由服务端固定，客户端只显示”）。 */
export function formatDateTimeWithZone(value: string | null | undefined): string {
  const formatted = formatDateTime(value)
  return formatted === '—' ? formatted : `${formatted}（${DISPLAY_TIME_ZONE}）`
}

/** 剩余时长展示（仅展示用途，不决定超时）。 */
export function formatRemaining(deadlineIso: string | null | undefined, nowMs: number): string {
  if (!deadlineIso) return '—'
  const deadline = new Date(deadlineIso).getTime()
  if (Number.isNaN(deadline)) return '—'
  const diff = deadline - nowMs
  if (diff <= 0) {
    const overdue = Math.abs(diff)
    return `已到截止时刻（超出 ${formatDuration(overdue)}）`
  }
  return `剩余 ${formatDuration(diff)}`
}

export function formatDuration(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const days = Math.floor(totalSeconds / 86400)
  const hours = Math.floor((totalSeconds % 86400) / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const parts: string[] = []
  if (days > 0) parts.push(`${days} 天`)
  if (hours > 0) parts.push(`${hours} 小时`)
  if (minutes > 0) parts.push(`${minutes} 分`)
  // 只有在前面的粒度都为空时才显示秒，避免“30 分 0 秒”这类噪声。
  if (parts.length === 0) parts.push(`${seconds} 秒`)
  return parts.join(' ')
}

export function formatTimeoutSeconds(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  return `${seconds} 秒（${formatDuration(seconds * 1000)}）`
}

export function formatFileSize(size: number | null | undefined): string {
  if (size == null) return '—'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

/** 执行截止时刻（优先取详情里的当前执行；列表项没有该字段时返回 null）。 */
export function executionDeadlineOf(task: TaskListItemDTO | TaskDetailDTO | null | undefined): string | null {
  if (!task) return null
  const detail = task as TaskDetailDTO
  if (detail.current_execution?.execution_deadline_at) return detail.current_execution.execution_deadline_at
  return detail.execution_deadline_at ?? null
}

/**
 * 异常标记：进行中且已过执行截止时刻 → “等待回收”（由系统扫描回收，客户端不自行改状态）。
 */
export function isAwaitingReclaim(
  task: TaskListItemDTO | TaskDetailDTO | null | undefined,
  nowMs: number,
): boolean {
  if (!task || task.status !== TaskStatus.IN_PROGRESS) return false
  const deadline = executionDeadlineOf(task)
  if (!deadline) return false
  const parsed = new Date(deadline).getTime()
  if (Number.isNaN(parsed)) return false
  return parsed <= nowMs
}

export function isOverdueDueAt(task: TaskListItemDTO | null | undefined, nowMs: number): boolean {
  if (!task?.due_at) return false
  const parsed = new Date(task.due_at).getTime()
  if (Number.isNaN(parsed)) return false
  return parsed <= nowMs && task.status !== TaskStatus.DONE
}

// ---------------------------------------------------------------------------
// 动作映射（“唯一允许集合”，CONTRACT.md §9）
// ---------------------------------------------------------------------------

export type TaskActionKind =
  | 'edit'
  | 'publish'
  | 'withdraw'
  | 'assign'
  | 'archive'
  | 'restore'
  | 'claim'
  | 'progress'
  | 'reclaim'
  | 'submit'
  | 'review'
  | 'reopen'

export const ACTION_LABELS: Readonly<Record<TaskActionKind, string>> = {
  edit: '编辑要求',
  publish: '发布',
  withdraw: '撤回发布',
  assign: '修改领取安排',
  archive: '归档',
  restore: '恢复归档',
  claim: '领取',
  progress: '更新进度',
  reclaim: '回收执行',
  submit: '提交结果',
  review: '审核',
  reopen: '重新打开',
}

/**
 * 跨列拖拽 → 业务动作。返回 null 表示该转换不在允许集合内（必须拒绝并回读服务端状态）。
 */
export function transitionAction(from: TaskStatusType, to: TaskStatusType): TaskActionKind | null {
  const table: Record<string, TaskActionKind> = {
    [`${TaskStatus.PREPARING}>${TaskStatus.TODO}`]: 'publish',
    [`${TaskStatus.TODO}>${TaskStatus.PREPARING}`]: 'withdraw',
    [`${TaskStatus.TODO}>${TaskStatus.IN_PROGRESS}`]: 'claim',
    [`${TaskStatus.IN_PROGRESS}>${TaskStatus.IN_REVIEW}`]: 'submit',
    [`${TaskStatus.IN_PROGRESS}>${TaskStatus.TODO}`]: 'reclaim',
    [`${TaskStatus.IN_REVIEW}>${TaskStatus.DONE}`]: 'review',
    [`${TaskStatus.IN_REVIEW}>${TaskStatus.TODO}`]: 'review',
    [`${TaskStatus.DONE}>${TaskStatus.TODO}`]: 'reopen',
  }
  return table[`${from}>${to}`] ?? null
}

export function transitionRefusalMessage(from: TaskStatusType, to: TaskStatusType): string {
  return `非法转换：${statusLabel(from)} → ${statusLabel(to)} 不在允许的动作集合内，卡片已按服务端状态回退。`
}

/** 卡片/详情可用的动作（键盘与窄屏入口与拖拽使用同一套动作）。 */
export function availableActions(task: TaskListItemDTO, options: { allowArchiving?: boolean } = {}): TaskActionKind[] {
  const allowArchiving = options.allowArchiving ?? true
  const actions: TaskActionKind[] = []
  if (task.is_archived) {
    actions.push('restore')
    return actions
  }
  switch (task.status) {
    case TaskStatus.PREPARING:
      actions.push('publish', 'edit', 'assign')
      break
    case TaskStatus.TODO:
      actions.push('claim', 'withdraw', 'edit', 'assign')
      break
    case TaskStatus.IN_PROGRESS:
      actions.push('submit', 'progress', 'reclaim')
      break
    case TaskStatus.IN_REVIEW:
      actions.push('review')
      break
    case TaskStatus.DONE:
      actions.push('reopen')
      break
    default:
      break
  }
  // 归档是独立标记：有有效执行或处于待审核时服务端会拒绝，这里只提供入口。
  if (allowArchiving && task.status !== TaskStatus.IN_REVIEW && !task.active_execution_id) {
    actions.push('archive')
  }
  return actions
}

// ---------------------------------------------------------------------------
// 错误解释（409/400/403/413/503 保留输入并说明原因）
// ---------------------------------------------------------------------------

export interface ErrorExplanation {
  /** 服务端原始 message。 */
  message: string
  /** 面向操作人的建议/原因说明。 */
  hint: string
  code: string
  requestId: string | null
}

const ERROR_HINTS: Record<string, string> = {
  VERSION_CONFLICT: '任务已被其他操作更新：已重新读取服务端最新状态，请确认后重试（您填写的内容仍保留）。',
  INVALID_TRANSITION: '当前状态不允许该操作。若需修改实质要求，请先撤回到准备中。',
  PROJECT_NOT_ACTIVE: '项目当前生命周期不允许写操作（需先启用项目）。',
  EXECUTION_TIMEOUT: '已过服务端固定执行截止时刻，写入被拒绝；请等待回收后重新领取。',
  EXECUTION_NOT_CURRENT: '该执行已不是当前有效执行，请刷新后按最新轮次操作。',
  TASK_ALREADY_CLAIMED: '任务已被其他执行者领取，已回读最新卡片。',
  CAPACITY_EXCEEDED: '所选 Agent 可用容量已满，请先处理其正在进行的执行。',
  SELF_REVIEW_FORBIDDEN: '审核人不能是提交者本人，请另选一名主管。',
  ROLE_FORBIDDEN: '当前角色/来源不允许该动作（释放需由执行 Agent 发起）。',
  REVIEW_ALREADY_DECIDED: '该提交已有正式审核决定，不能再次决策。',
  SUBMISSION_EXISTS: '该执行已存在正式提交，不能重复提交。',
  RESOURCE_NOT_FOUND: '对象不存在或当前身份无权访问，已回读服务端状态。',
  INVALID_ARGUMENT: '请求参数不合法，请修正后按原幂等键重试。',
  IDEMPOTENCY_KEY_REUSED: '同一幂等键被用于不同的请求内容，请重新打开表单再试。',
  IDEMPOTENCY_IN_PROGRESS: '同一操作正在处理中，请稍候按原键重试。',
  FILE_TOO_LARGE: '文件超过大小上限。',
  UNSUPPORTED_MEDIA_TYPE: '文件类型不被允许。',
  ATTACHMENT_LIMIT_EXCEEDED: '附件数量或单次提交产物数量超限。',
  DATABASE_BUSY: '数据库繁忙，可按原幂等键重试（不会重复执行业务）。',
  CLAIM_LOCK_TIMEOUT: '领取锁等待超时，请稍后按原幂等键重试。',
  NETWORK_ERROR: '网络请求失败：服务端可能已提交，请先查看最新状态再按原幂等键重试。',
}

export function describeApiError(error: ApiError): ErrorExplanation {
  return {
    message: error.message,
    hint: ERROR_HINTS[error.code] ?? '',
    code: error.code,
    requestId: error.requestId,
  }
}
