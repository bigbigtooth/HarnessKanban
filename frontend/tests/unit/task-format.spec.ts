/**
 * 任务看板展示层单元测试（T31/T32）：文案、时间与动作映射。
 * 只测纯函数（无业务判定）：超时/可否提交等一律以服务端为准。
 */
import { describe, expect, it } from 'vitest'
import { TaskStatus } from '@/api/types'
import {
  ACTION_LABELS,
  activityActionLabel,
  availableActions,
  claimModeLabel,
  difficultyLabel,
  endReasonLabel,
  executionDeadlineOf,
  formatDateTime,
  formatDateTimeWithZone,
  formatDuration,
  formatFileSize,
  formatRemaining,
  formatTimeoutSeconds,
  isAwaitingReclaim,
  isOverdueDueAt,
  reviewDecisionLabel,
  statusLabel,
  transitionAction,
  transitionRefusalMessage,
} from '@/components/task/task-format'
import type { TaskDetailDTO, TaskListItemDTO } from '@/api/tasks'

function taskFixture(overrides: Partial<TaskListItemDTO> = {}): TaskListItemDTO {
  return {
    id: 'task-1',
    project_id: 'project-1',
    number: 7,
    display_number: 'TASK-0007',
    title: '样本任务',
    difficulty: 3,
    status: TaskStatus.TODO,
    claim_mode: 'OPEN',
    designated_agent_id: null,
    assignee_agent_id: null,
    active_execution_id: null,
    current_submission_id: null,
    sort_order: '1000.0',
    due_at: null,
    version: 2,
    is_archived: false,
    ...overrides,
  }
}

describe('任务展示文案', () => {
  it('五态与五级难度都带文字标签，不依赖颜色', () => {
    expect(statusLabel(TaskStatus.PREPARING)).toBe('准备中')
    expect(statusLabel(TaskStatus.TODO)).toBe('待办')
    expect(statusLabel(TaskStatus.IN_PROGRESS)).toBe('进行中')
    expect(statusLabel(TaskStatus.IN_REVIEW)).toBe('待验收')
    expect(statusLabel(TaskStatus.DONE)).toBe('已完成')
    expect(difficultyLabel(1)).toBe('L1 入门')
    expect(difficultyLabel(5)).toBe('L5 极难')
    expect(claimModeLabel('OPEN')).toBe('开放抢单')
    expect(claimModeLabel('ASSIGNED')).toBe('指定领取')
    expect(endReasonLabel('TIMEOUT')).toBe('超时回收')
    expect(reviewDecisionLabel('REJECT')).toBe('驳回')
    expect(activityActionLabel('task.submitted')).toBe('提交结果')
    expect(activityActionLabel('收尾动作')).toBe('收尾动作')
  })

  it('动作文案覆盖所有可用动作', () => {
    expect(Object.keys(ACTION_LABELS)).toContain('claim')
    expect(ACTION_LABELS.submit).toBe('提交结果')
    expect(ACTION_LABELS.reclaim).toBe('回收执行')
  })
})

describe('时间展示（UTC 存储、Asia/Shanghai 展示）', () => {
  it('按 Asia/Shanghai 格式化并标注时区', () => {
    expect(formatDateTime('2026-10-15T00:00:00Z')).toBe('2026-10-15 08:00:00')
    expect(formatDateTimeWithZone('2026-10-15T00:00:00Z')).toBe('2026-10-15 08:00:00（Asia/Shanghai）')
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime('不是时间')).toBe('—')
  })

  it('剩余时长只是展示，不参与超时判定', () => {
    const now = new Date('2026-10-15T00:00:00Z').getTime()
    expect(formatRemaining('2026-10-15T01:02:03Z', now)).toBe('剩余 1 小时 2 分')
    expect(formatRemaining('2026-10-14T23:59:00Z', now)).toContain('已到截止时刻')
    expect(formatRemaining(null, now)).toBe('—')
    expect(formatDuration(90_000)).toBe('1 分')
    expect(formatDuration(3_600_000)).toBe('1 小时')
    expect(formatTimeoutSeconds(1800)).toBe('1800 秒（30 分）')
    expect(formatFileSize(2048)).toBe('2.0 KB')
  })
})

describe('执行截止与异常标记', () => {
  const now = new Date('2026-10-15T01:00:00Z').getTime()

  it('行动中且已过固定截止 → 等待回收；未过截止或非进行中 → 无标记', () => {
    const running: TaskDetailDTO = {
      ...taskFixture({ status: TaskStatus.IN_PROGRESS }),
      current_execution: {
        execution_id: 'exec-1',
        task_id: 'task-1',
        agent_id: 'agent-1',
        sequence: 1,
        difficulty: 3,
        execution_timeout_seconds: 7200,
        claimed_at: '2026-10-15T00:00:00Z',
        execution_deadline_at: '2026-10-15T00:30:00Z',
      },
      execution_deadline_at: '2026-10-15T00:30:00Z',
    }
    expect(isAwaitingReclaim(running, now)).toBe(true)
    expect(executionDeadlineOf(running)).toBe('2026-10-15T00:30:00Z')

    const future: TaskDetailDTO = {
      ...running,
      execution_deadline_at: '2026-10-15T02:00:00Z',
      current_execution: { ...running.current_execution!, execution_deadline_at: '2026-10-15T02:00:00Z' },
    }
    expect(isAwaitingReclaim(future, now)).toBe(false)

    const todo = taskFixture({ status: TaskStatus.TODO })
    expect(isAwaitingReclaim(todo, now)).toBe(false)
    expect(executionDeadlineOf(todo)).toBeNull()
  })

  it('计划截止 due_at 与执行截止是两个不同字段', () => {
    const task = taskFixture({ due_at: '2026-10-15T00:30:00Z', status: TaskStatus.TODO })
    expect(isOverdueDueAt(task, now)).toBe(true)
    const done = taskFixture({ due_at: '2026-10-15T00:30:00Z', status: TaskStatus.DONE })
    expect(isOverdueDueAt(done, now)).toBe(false)
  })
})

describe('跨列拖拽 → 业务动作映射（唯一允许集合）', () => {
  it('允许的转换映射到对应动作', () => {
    expect(transitionAction(TaskStatus.PREPARING, TaskStatus.TODO)).toBe('publish')
    expect(transitionAction(TaskStatus.TODO, TaskStatus.PREPARING)).toBe('withdraw')
    expect(transitionAction(TaskStatus.TODO, TaskStatus.IN_PROGRESS)).toBe('claim')
    expect(transitionAction(TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW)).toBe('submit')
    expect(transitionAction(TaskStatus.IN_PROGRESS, TaskStatus.TODO)).toBe('reclaim')
    expect(transitionAction(TaskStatus.IN_REVIEW, TaskStatus.DONE)).toBe('review')
    expect(transitionAction(TaskStatus.IN_REVIEW, TaskStatus.TODO)).toBe('review')
    expect(transitionAction(TaskStatus.DONE, TaskStatus.TODO)).toBe('reopen')
  })

  it('非法转换返回 null 并给出拒绝文案（不假装移动成功）', () => {
    expect(transitionAction(TaskStatus.TODO, TaskStatus.DONE)).toBeNull()
    expect(transitionAction(TaskStatus.PREPARING, TaskStatus.IN_PROGRESS)).toBeNull()
    expect(transitionAction(TaskStatus.IN_PROGRESS, TaskStatus.DONE)).toBeNull()
    expect(transitionRefusalMessage(TaskStatus.TODO, TaskStatus.DONE)).toContain('非法转换')
    expect(transitionRefusalMessage(TaskStatus.TODO, TaskStatus.DONE)).toContain('待办 → 已完成')
  })

  it('可用动作随状态变化，且不提供“改状态”或“续期”动作', () => {
    expect(availableActions(taskFixture({ status: TaskStatus.PREPARING }))).toEqual([
      'publish',
      'edit',
      'assign',
      'archive',
    ])
    expect(availableActions(taskFixture({ status: TaskStatus.TODO }))).toEqual([
      'claim',
      'withdraw',
      'edit',
      'assign',
      'archive',
    ])
    expect(availableActions(taskFixture({ status: TaskStatus.IN_PROGRESS }))).toEqual([
      'submit',
      'progress',
      'reclaim',
      'archive',
    ])
    // 待审核不能归档、也不能领取
    expect(availableActions(taskFixture({ status: TaskStatus.IN_REVIEW }))).toEqual(['review'])
    // 有有效执行时不提供归档（服务端也会拒绝）
    expect(
      availableActions(taskFixture({ status: TaskStatus.IN_PROGRESS, active_execution_id: 'exec-1' })),
    ).toEqual(['submit', 'progress', 'reclaim'])
    expect(availableActions(taskFixture({ status: TaskStatus.DONE }))).toEqual(['reopen', 'archive'])
    // 已归档只提供恢复
    expect(availableActions(taskFixture({ is_archived: true }))).toEqual(['restore'])
    // 没有任何“改状态/续期”动作
    const kinds = Object.values(ACTION_LABELS).join(' ')
    expect(kinds).not.toContain('续期')
    expect(kinds).not.toContain('设置状态')
  })
})
