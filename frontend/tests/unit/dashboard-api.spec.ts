/**
 * 全局看板与活动 WebAPI 封装单元测试（T33）。
 *
 * 只替换网络层：断言请求参数形状，并确认统计 payload 原样透传（前端不重算、不补零）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { FetchLike } from '@/api/client'
import { getDashboard, listActivity } from '@/api/dashboard'
import { createFetchStub, errEnvelope, okEnvelope, pageEnvelope } from './helpers/fetchStub'

const stub = createFetchStub()

beforeEach(() => {
  stub.reset()
  vi.stubGlobal('fetch', stub.fetch as unknown as FetchLike)
})

const DASHBOARD = {
  as_of: '2026-10-15T03:00:00.123456Z',
  scope: {
    actor_type: 'web_operator',
    agent_id: null,
    role: null,
    project_id: null,
    project_ids: ['p1', 'p2'],
    project_count: 2,
    include_archived: false,
    task_visibility: 'FULL',
    archived_project_total: 0,
  },
  task_total: 6,
  task_status_counts: { PREPARING: 1, TODO: 3, IN_PROGRESS: 1, IN_REVIEW: 0, DONE: 1 },
  difficulty_counts: { '1': 1, '2': 2, '3': 1, '4': 1, '5': 1 },
  archived_task_total: 0,
  overdue_task_total: 1,
  completion: { done: 1, total: 6, rate: 0.1667, rate_available: true, reason: null },
  completion_rate: 0.1667,
  project_counts: { DRAFT: 0, ACTIVE: 1, COMPLETED: 0, ARCHIVED: 0 },
  project_total: 1,
  agent_counts: { total: 2, enabled: 2, disabled: 0, online: 1, offline: 0, never_connected: 1, working: 1 },
  execution_counts: {
    open_executions: 2,
    effective_executions: 1,
    waiting_reclaim_executions: 1,
    reserved_slots: 2,
    capacity_total: 6,
    capacity_available: 4,
  },
  exception_counts: {
    OVERDUE_RECLAIM_REQUIRED: 1,
    UNAVAILABLE_DESIGNATED_AGENT: 0,
    total: 1,
    returned: 1,
    limit: 20,
  },
  exceptions: [
    {
      task_id: 'task-1',
      project_id: 'p1',
      number: 1,
      display_number: 'TASK-0001',
      title: '超时任务',
      status: 'IN_PROGRESS',
      difficulty: 2,
      claim_mode: 'OPEN',
      designated_agent_id: null,
      designated_agent_name: null,
      assignee_agent_id: 'a1',
      claimed_at: '2026-10-15T01:00:00Z',
      execution_deadline_at: '2026-10-15T02:00:00Z',
      overdue_seconds: 3600,
      reasons: ['EXECUTION_OVERDUE'],
      reason: 'EXECUTION_OVERDUE',
      kind: 'OVERDUE_RECLAIM_REQUIRED',
    },
  ],
  projects: [
    {
      project_id: 'p1',
      title: '项目一',
      status: 'ACTIVE',
      task_total: 6,
      task_status_counts: { PREPARING: 1, TODO: 3, IN_PROGRESS: 1, IN_REVIEW: 0, DONE: 1 },
      archived_task_total: 0,
      overdue_task_total: 1,
      pending_review: 0,
      completion: { done: 1, total: 6, rate: 0.1667, rate_available: true, reason: null },
      completion_rate: 0.1667,
    },
  ],
  agents: [
    {
      agent_id: 'a1',
      name: 'worker-a',
      role: 'WORKER',
      enabled: true,
      online_state: 'ONLINE_WORKING',
      last_heartbeat_at: '2026-10-15T02:59:00Z',
      effective_executions: 1,
      waiting_reclaim_executions: 1,
      reserved_slots: 2,
      max_concurrency: 3,
      available_capacity: 1,
    },
  ],
}

describe('GET /dashboard', () => {
  it('无 scope 参数时不发送 project_id/include_archived', async () => {
    stub.reply({ status: 200, json: okEnvelope(DASHBOARD) })
    await getDashboard()
    const url = stub.calls[0]!.url
    expect(url).toContain('/api/web/v1/dashboard')
    expect(url).not.toContain('project_id=')
    expect(url).not.toContain('include_archived=')
    expect(stub.calls[0]!.method).toBe('GET')
  })

  it('scope 参数按服务端名字发送', async () => {
    stub.reply({ status: 200, json: okEnvelope(DASHBOARD) })
    await getDashboard({ projectId: 'p1', includeArchived: true })
    const url = stub.calls[0]!.url
    expect(url).toContain('project_id=p1')
    expect(url).toContain('include_archived=true')
  })

  it('统计 payload 原样透传：as_of/scope/五态/难度/执行/异常都不被前端改写或补零', async () => {
    stub.reply({ status: 200, json: okEnvelope(DASHBOARD) })
    const data = await getDashboard()
    expect(data.as_of).toBe('2026-10-15T03:00:00.123456Z')
    expect(data.scope.project_count).toBe(2)
    expect(data.scope.task_visibility).toBe('FULL')
    expect(data.task_status_counts).toEqual({ PREPARING: 1, TODO: 3, IN_PROGRESS: 1, IN_REVIEW: 0, DONE: 1 })
    expect(data.task_total).toBe(6)
    expect(data.difficulty_counts).toEqual({ '1': 1, '2': 2, '3': 1, '4': 1, '5': 1 })
    expect(data.execution_counts.effective_executions).toBe(1)
    expect(data.execution_counts.reserved_slots).toBe(2)
    expect(data.execution_counts.waiting_reclaim_executions).toBe(1)
    expect(data.exceptions).toHaveLength(1)
    expect(data.exceptions[0]!.reasons).toEqual(['EXECUTION_OVERDUE'])
    expect(data.projects[0]!.task_status_counts.TODO).toBe(3)
    expect(data.agents[0]!.waiting_reclaim_executions).toBe(1)
  })

  it('失败时把错误抛给调用方（由页面保留旧数据并标记过期，绝不返回空统计）', async () => {
    stub.reply({ status: 503, json: errEnvelope('DATABASE_BUSY', '数据库繁忙，请稍后重试') })
    const error = await getDashboard().catch((thrown) => thrown)
    expect(error).toMatchObject({ code: 'DATABASE_BUSY', retryable: true })
  })
})

describe('GET /activity', () => {
  it('分页与筛选参数按服务端名字发送，空动作不发 action', async () => {
    stub.reply({ status: 200, json: pageEnvelope([]) })
    await listActivity({ page: 2, pageSize: 50, action: '   ' })
    const url = stub.calls[0]!.url
    expect(url).toContain('page=2')
    expect(url).toContain('page_size=50')
    expect(url).not.toContain('action=')
  })

  it('项目/任务/动作/归档筛选都映射到服务端参数', async () => {
    stub.reply({ status: 200, json: pageEnvelope([]) })
    await listActivity({ projectId: 'p1', taskId: 't1', action: 'task.review', includeArchived: true })
    const url = stub.calls[0]!.url
    expect(url).toContain('project_id=p1')
    expect(url).toContain('task_id=t1')
    expect(url).toContain('action=task.review')
    expect(url).toContain('include_archived=true')
  })

  it('活动分页形状原样返回（items/total/page/page_size）', async () => {
    stub.reply({
      status: 200,
      json: pageEnvelope(
        [
          { id: 'e2', action: 'task.review', actor_type: 'web_operator', created_at: '2026-10-15T03:00:00Z' },
          { id: 'e1', action: 'task.publish', actor_type: 'web_operator', created_at: '2026-10-15T02:00:00Z' },
        ],
        { total: 42, page: 1, pageSize: 20 },
      ),
    })
    const page = await listActivity()
    expect(page.total).toBe(42)
    expect(page.items.map((item) => item.id)).toEqual(['e2', 'e1'])
  })
})
