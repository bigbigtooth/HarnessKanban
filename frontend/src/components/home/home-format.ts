/**
 * 首页展示用的格式化助手：把后端字段翻译成 demo 风格的文案与色板。
 *
 * 时间/文件大小等通用格式化复用 `components/task/task-format.ts`，这里只放新界面
 * 独有的映射（截止日期的“还有 N 天/已逾期”、标签色板、难度徽标、项目状态）。
 */
import type { ProjectStatus } from '@/api/types'
import { DIFFICULTY_LABEL, type Difficulty } from '@/api/types'
import type { ProjectSummaryDTO } from '@/api/projects'
import type { TaskListItemDTO } from '@/api/tasks'

/** 与 demo 的标签色板一致：四套颜色循环使用。 */
export const TAG_PALETTES = ['design', 'dev', 'copy', 'ops'] as const
export type TagPalette = (typeof TAG_PALETTES)[number]

export function tagPalette(label: string | undefined, fallbackIndex = 0): TagPalette {
  if (label) {
    let hash = 0
    for (let index = 0; index < label.length; index += 1) {
      hash = (hash * 31 + label.charCodeAt(index)) >>> 0
    }
    return TAG_PALETTES[hash % TAG_PALETTES.length]
  }
  return TAG_PALETTES[fallbackIndex % TAG_PALETTES.length]
}

/** 任务的第一个标签；没有标签时退回状态名，保证卡片左侧徽标永远有内容。 */
export function taskTag(task: TaskListItemDTO, statusLabel: string): string {
  const labels = task.labels ?? []
  return labels.length > 0 ? labels[0] : statusLabel
}

export function difficultyText(difficulty: Difficulty, name?: string): string {
  return `${name ?? `L${difficulty}`} · ${DIFFICULTY_LABEL[difficulty].replace(/^L\d\s*/, '')}`
}

const DAY_MS = 24 * 60 * 60 * 1000

function startOfToday(): Date {
  const now = new Date()
  return new Date(now.getFullYear(), now.getMonth(), now.getDate())
}

function parseDate(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? null : date
}

/** 距今天数（按自然日）：负数表示已过期。 */
export function diffDays(iso: string | null | undefined): number | null {
  const date = parseDate(iso)
  if (!date) return null
  const target = new Date(date.getFullYear(), date.getMonth(), date.getDate())
  return Math.round((target.getTime() - startOfToday().getTime()) / DAY_MS)
}

export function shortDate(iso: string | null | undefined): string {
  const date = parseDate(iso)
  if (!date) return ''
  return `${date.getMonth() + 1}月${date.getDate()}日`
}

export function longDate(iso: string | null | undefined): string {
  const date = parseDate(iso)
  if (!date) return '未设置'
  const week = ['日', '一', '二', '三', '四', '五', '六'][date.getDay()]
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 · 周${week}`
}

export interface DueMeta {
  label: string
  className: string
  title: string
}

/** 截止提示：临界 2 天内标 `is-soon`，已过期标 `is-overdue`（已完成的任务不再告警）。 */
export function dueMeta(iso: string | null | undefined, isDone = false): DueMeta | null {
  const diff = diffDays(iso)
  if (diff === null) return null
  const title = longDate(iso)
  if (isDone) return { label: '已完成', className: '', title }
  if (diff < 0) return { label: `逾期 ${Math.abs(diff)} 天`, className: 'is-overdue', title }
  if (diff === 0) return { label: '今天到期', className: 'is-soon', title }
  if (diff <= 2) return { label: `还有 ${diff} 天`, className: 'is-soon', title }
  return { label: shortDate(iso), className: '', title }
}

export const PROJECT_STATUS_META: Record<ProjectStatus, { label: string; className: string; emoji: string }> = {
  DRAFT: { label: '草稿', className: 'status-planning', emoji: '🌱' },
  ACTIVE: { label: '进行中', className: 'status-active', emoji: '🫧' },
  COMPLETED: { label: '已结项', className: 'status-done', emoji: '🎀' },
  ARCHIVED: { label: '已归档', className: 'status-paused', emoji: '📦' },
}

export function projectStatusMeta(status: ProjectStatus): { label: string; className: string; emoji: string } {
  return PROJECT_STATUS_META[status] ?? { label: status, className: '', emoji: '🪐' }
}

export function isProjectDeleted(project: ProjectSummaryDTO): boolean {
  return Boolean(project.deleted_at)
}

/** 项目进度：优先用 /dashboard 的完成度，缺失时退回 0。 */
export function projectProgressPercent(rate: number | null | undefined): number {
  if (rate == null) return 0
  return Math.max(0, Math.min(100, Math.round(rate * 100)))
}

/** 状态中文名只有一处定义（components/task/task-format.ts），这里转出去给新界面用。 */
export { COLUMN_LABELS as TASK_STATUS_LABEL, statusLabel } from '@/components/task/task-format'

export interface DropAnchors {
  /** 目标位置左侧的卡片（移动后排在它后面）。 */
  beforeTaskId: string | null
  /** 目标位置右侧的卡片（移动后排在它前面）。 */
  afterTaskId: string | null
  /** false 表示拖回了原位置，不需要请求服务端。 */
  changed: boolean
}

/**
 * 把「拖到该列第 targetIndex 个位置」换算成 `POST /projects/{id}/task-order` 的锚点。
 *
 * 语义与服务端一致：`beforeTaskId` = 左邻居，`afterTaskId` = 右邻居。
 * `targetIndex` 是当前 DOM 顺序（含被拖卡片）里的插入下标。
 */
export function resolveDropAnchors(
  orderedIds: readonly string[],
  draggedId: string,
  targetIndex: number,
): DropAnchors {
  const fromIndex = orderedIds.indexOf(draggedId)
  if (fromIndex === -1) return { beforeTaskId: null, afterTaskId: null, changed: false }
  const without = orderedIds.filter((id) => id !== draggedId)
  // 拖到自己后面时下标要减 1（被拖卡片自身原先占了一个位置）
  const rawIndex = targetIndex > fromIndex ? targetIndex - 1 : targetIndex
  const insertAt = Math.max(0, Math.min(rawIndex, without.length))
  return {
    beforeTaskId: without[insertAt - 1] ?? null,
    afterTaskId: without[insertAt] ?? null,
    changed: insertAt !== fromIndex,
  }
}
