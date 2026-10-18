/**
 * 首页格式化助手单元测试：截止日期语义、标签色板、项目状态与进度。
 */
import { describe, expect, it } from 'vitest'
import {
  PROJECT_STATUS_META,
  resolveDropAnchors,
  TASK_STATUS_LABEL,
  diffDays,
  difficultyText,
  dueMeta,
  isProjectDeleted,
  longDate,
  projectProgressPercent,
  projectStatusMeta,
  shortDate,
  tagPalette,
  taskTag,
} from '@/components/home/home-format'
import type { ProjectSummaryDTO } from '@/api/projects'
import type { TaskListItemDTO } from '@/api/tasks'

function isoDaysFromNow(days: number): string {
  const date = new Date()
  date.setDate(date.getDate() + days)
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}T00:00:00Z`
}

function baseTask(overrides: Partial<TaskListItemDTO> = {}): TaskListItemDTO {
  return {
    id: 't-1',
    project_id: 'p-1',
    number: 3,
    display_number: 'TASK-0003',
    title: '标题',
    difficulty: 2,
    status: 'TODO',
    claim_mode: 'OPEN',
    sort_order: '1',
    version: 1,
    is_archived: false,
    labels: [],
    ...overrides,
  } as TaskListItemDTO
}

describe('截止日期', () => {
  it('今天 / 临界两天 / 已逾期各自带出不同语气与样式', () => {
    expect(dueMeta(isoDaysFromNow(0))?.label).toBe('今天到期')
    expect(dueMeta(isoDaysFromNow(0))?.className).toBe('is-soon')
    expect(dueMeta(isoDaysFromNow(2))?.label).toBe('还有 2 天')
    expect(dueMeta(isoDaysFromNow(2))?.className).toBe('is-soon')
    expect(dueMeta(isoDaysFromNow(-3))?.label).toBe('逾期 3 天')
    expect(dueMeta(isoDaysFromNow(-3))?.className).toBe('is-overdue')
  })

  it('已完成的任务不再显示逾期告警，缺少日期时返回 null', () => {
    expect(dueMeta(isoDaysFromNow(-5), true)).toEqual({ label: '已完成', className: '', title: expect.any(String) })
    expect(dueMeta(null)).toBeNull()
    expect(dueMeta('not-a-date')).toBeNull()
  })

  it('天数差按自然日计算，日期文案固定为中文月日', () => {
    expect(diffDays(isoDaysFromNow(1))).toBe(1)
    expect(shortDate('2026-10-03T00:00:00Z')).toMatch(/10月\d+日/)
    expect(longDate('2026-10-03T00:00:00Z')).toContain('2026年')
  })
})

describe('标签与难度', () => {
  it('标签色板由标签名稳定决定（同名同色）', () => {
    expect(tagPalette('前端')).toBe(tagPalette('前端'))
    expect(['design', 'dev', 'copy', 'ops']).toContain(tagPalette('前端'))
  })

  it('没有标签时回落到状态名，卡片不会空着', () => {
    expect(taskTag(baseTask(), TASK_STATUS_LABEL.TODO)).toBe('待办')
    expect(taskTag(baseTask({ labels: ['设计'] }), TASK_STATUS_LABEL.TODO)).toBe('设计')
  })

  it('难度文案带等级与中文说明', () => {
    expect(difficultyText(4)).toBe('L4 · 困难')
    expect(difficultyText(1, 'L1')).toBe('L1 · 入门')
  })
})

describe('项目状态与进度', () => {
  it('四种服务端状态都有中文标签与配色', () => {
    expect(projectStatusMeta('DRAFT').label).toBe('草稿')
    expect(projectStatusMeta('ACTIVE').className).toBe('status-active')
    expect(projectStatusMeta('COMPLETED').label).toBe('已结项')
    expect(projectStatusMeta('ARCHIVED').label).toBe('已归档')
    expect(Object.keys(PROJECT_STATUS_META)).toHaveLength(4)
  })

  it('完成度：null 视为 0，超界被夹在 0..100', () => {
    expect(projectProgressPercent(null)).toBe(0)
    expect(projectProgressPercent(0.426)).toBe(43)
    expect(projectProgressPercent(1.5)).toBe(100)
    expect(projectProgressPercent(-1)).toBe(0)
  })

  it('软删除的项目被识别出来', () => {
    const active = { id: 'p-1', title: 'A', status: 'ACTIVE', version: 1, content_version: 1 } as ProjectSummaryDTO
    const deleted = { ...active, deleted_at: '2026-10-01T00:00:00Z' } as ProjectSummaryDTO
    expect(isProjectDeleted(active)).toBe(false)
    expect(isProjectDeleted(deleted)).toBe(true)
  })
})

describe('拖拽排序锚点', () => {
  const ids = ['a', 'b', 'c']

  it('往下拖：拖到第三张之后 → 左邻居是 c、右邻居为空', () => {
    // 把 a 拖到列表末尾（DOM 下标 3）
    expect(resolveDropAnchors(ids, 'a', 3)).toEqual({
      beforeTaskId: 'c',
      afterTaskId: null,
      changed: true,
    })
  })

  it('往下拖到中间：a 放到 b 之后 → 左 b、右 c', () => {
    // 拖到 c 之前（DOM 下标 2）；被拖卡片自身占位要减一
    expect(resolveDropAnchors(ids, 'a', 2)).toEqual({
      beforeTaskId: 'b',
      afterTaskId: 'c',
      changed: true,
    })
  })

  it('往上拖：c 拖到最前 → 左邻居为空、右邻居是 a', () => {
    expect(resolveDropAnchors(ids, 'c', 0)).toEqual({
      beforeTaskId: null,
      afterTaskId: 'a',
      changed: true,
    })
  })

  it('拖回原位置 → changed=false，不发请求', () => {
    expect(resolveDropAnchors(ids, 'b', 1).changed).toBe(false)
    expect(resolveDropAnchors(ids, 'a', 0).changed).toBe(false)
    expect(resolveDropAnchors(ids, 'c', 3).changed).toBe(false)
  })

  it('只有一张卡时拖动不算变化；未知 id 也不会抛错', () => {
    expect(resolveDropAnchors(['only'], 'only', 0).changed).toBe(false)
    expect(resolveDropAnchors(['only'], 'only', 1).changed).toBe(false)
    expect(resolveDropAnchors(ids, 'missing', 0)).toEqual({ beforeTaskId: null, afterTaskId: null, changed: false })
  })
})
