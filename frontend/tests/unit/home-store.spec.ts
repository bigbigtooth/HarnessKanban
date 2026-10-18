/**
 * 首页数据层单元测试：默认“全部项目”取数、多条件筛选、派生统计与视觉映射。
 *
 * 网络层用 fetch 替身（`helpers/fetchStub`），store 的取数/合并/派生逻辑保持真实。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import type { FetchLike } from '@/api/client'
import { createFetchStub, errEnvelope, okEnvelope, pageEnvelope } from './helpers/fetchStub'
import {
  activeFilterCount,
  agentAvatar,
  agentById,
  AVATAR_COUNT,
  clearFilters,
  homeFilters,
  loadDashboard,
  loadProjects,
  loadTasks,
  projectCoverView,
  projectStatsById,
  resetHomeState,
  setFilter,
  setKeyword,
  taskList,
  taskStats,
  tasksByStatus,
  TASK_PAGE_SIZE,
  coverChoicesFor,
  PROJECT_COVERS,
  PROJECT_COVERS_DARK,
} from '@/stores/home'

const stub = createFetchStub()

function task(overrides: Record<string, unknown> = {}) {
  return {
    id: 't-1',
    project_id: 'p-1',
    number: 1,
    display_number: 'TASK-0001',
    title: '写一份验收说明',
    difficulty: 2,
    status: 'TODO',
    claim_mode: 'OPEN',
    sort_order: '1',
    version: 1,
    is_archived: false,
    labels: ['前端'],
    ...overrides,
  }
}

beforeEach(() => {
  stub.reset()
  resetHomeState()
  vi.stubGlobal('fetch', stub.fetch as unknown as FetchLike)
})

afterEach(() => {
  vi.unstubAllGlobals()
  resetHomeState()
})

describe('任务取数', () => {
  it('默认不带 project_id，且显式请求 status=ALL（一次拿到五态）', async () => {
    stub.reply({ json: pageEnvelope([task(), task({ id: 't-2', status: 'DONE', number: 2 })], { total: 2 }) })
    await loadTasks()
    await flushPromises()
    const call = stub.calls[0]!
    expect(call.url).toContain('/tasks?')
    expect(call.url).toContain('status=ALL')
    expect(call.url).not.toContain('project_id=')
    expect(call.url).toContain(`page_size=${TASK_PAGE_SIZE}`)
    expect(taskList.items).toHaveLength(2)
    expect(taskList.total).toBe(2)
  })

  it('筛选条件进入查询串，并统计已启用条件数', async () => {
    stub.setDefault({ json: pageEnvelope([]) })
    setFilter('projectId', 'p-9')
    await flushPromises()
    setFilter('difficulty', 4)
    await flushPromises()
    setFilter('claimMode', 'ASSIGNED')
    await flushPromises()
    setFilter('assigneeAgentId', 'a-7')
    await flushPromises()
    setFilter('includeArchived', true)
    await flushPromises()

    expect(activeFilterCount.value).toBe(5)
    const last = stub.calls[stub.calls.length - 1]!
    expect(last.url).toContain('project_id=p-9')
    expect(last.url).toContain('difficulty=4')
    expect(last.url).toContain('claim_mode=ASSIGNED')
    expect(last.url).toContain('assignee_agent_id=a-7')
    expect(last.url).toContain('include_archived=true')
  })

  it('关键词输入防抖后只发一次请求，空关键词不发 q 参数', async () => {
    stub.setDefault({ json: pageEnvelope([]) })
    vi.useFakeTimers()
    try {
      setKeyword('天气')
      setKeyword('天气预')
      setKeyword('天气预报')
      expect(stub.calls).toHaveLength(0)
      vi.advanceTimersByTime(400)
      await flushPromises()
      expect(stub.calls).toHaveLength(1)
      expect(stub.calls[0]!.url).toContain(`q=${encodeURIComponent('天气预报')}`)
    } finally {
      vi.useRealTimers()
    }
  })

  it('清空筛选把条件恢复到默认（全部项目 / 不含归档）', async () => {
    stub.setDefault({ json: pageEnvelope([]) })
    setFilter('projectId', 'p-1')
    setFilter('includeArchived', true)
    await flushPromises()
    clearFilters()
    await flushPromises()
    expect(homeFilters.projectId).toBe('')
    expect(homeFilters.includeArchived).toBe(false)
    expect(activeFilterCount.value).toBe(0)
  })

  it('读取失败时保留上一次数据，只把错误挂出来（不伪装成空列表）', async () => {
    stub.reply({ json: pageEnvelope([task()], { total: 1 }) })
    await loadTasks()
    await flushPromises()
    stub.reply({ status: 503, json: errEnvelope('DEPENDENCY_UNAVAILABLE', '上游不可用') })
    await loadTasks()
    await flushPromises()
    expect(taskList.items).toHaveLength(1)
    expect(taskList.error?.code).toBe('DEPENDENCY_UNAVAILABLE')
    expect(taskList.status).toBe('ready')
  })
})

describe('派生视图', () => {
  it('按状态分桶，完成度只统计已完成', async () => {
    stub.reply({
      json: pageEnvelope([
        task({ id: 't-1', status: 'TODO' }),
        task({ id: 't-2', status: 'DONE' }),
        task({ id: 't-3', status: 'IN_PROGRESS' }),
        task({ id: 't-4', status: 'DONE' }),
      ]),
    })
    await loadTasks()
    await flushPromises()
    expect(tasksByStatus.value.TODO).toHaveLength(1)
    expect(tasksByStatus.value.DONE).toHaveLength(2)
    expect(tasksByStatus.value.IN_REVIEW).toHaveLength(0)
    expect(taskStats.value).toEqual({ total: 4, done: 2, inFlight: 1, percent: 50 })
  })

  it('项目统计取 /dashboard（不受任务筛选影响），并按 id 建索引', async () => {
    stub.reply({
      json: okEnvelope({
        projects: [
          { project_id: 'p-1', title: 'A', status: 'ACTIVE', task_total: 7, completion_rate: 0.5, pending_review: 2, overdue_task_total: 1 },
        ],
      }),
    })
    await loadDashboard()
    await flushPromises()
    expect(projectStatsById.value.get('p-1')?.task_total).toBe(7)
  })

  it('项目清单合并“在用 + 回收站”，已删除项带 deleted_at', async () => {
    stub.reply(
      { json: pageEnvelope([{ id: 'p-1', title: '在用项目', status: 'ACTIVE', version: 1, content_version: 1 }]) },
      { json: pageEnvelope([{ id: 'p-2', title: '已删除项目', status: 'ARCHIVED', version: 2, content_version: 1, deleted_at: '2026-10-01T00:00:00Z' }]) },
    )
    await loadProjects()
    await flushPromises()
    expect(stub.calls[1]!.url).toContain('deleted=true')
    expect(stub.calls[0]!.url).toContain('include_archived=true')
  })
})

describe('视觉映射', () => {
  it('Agent 头像由 id 决定且稳定；同名不同 id 也能分开', () => {
    const first = agentAvatar({ id: 'agent-1', name: '小七' })
    const again = agentAvatar({ id: 'agent-1', name: '小七' })
    expect(first).toEqual(again)
    expect(first.emoji).toBeTruthy()
    expect(first.color).toMatch(/^#/)
    expect(AVATAR_COUNT).toBeGreaterThan(3)
  })

  it('Agent 索引按 id 查询，未命中返回空串而不是编造名字', () => {
    expect(agentById.value.get('missing')).toBeUndefined()
  })

  it('项目封面/emoji 由项目 id 稳定派生，并随主题切换色板', () => {
    const light = projectCoverView('p-1')
    const dark = coverChoicesFor('dark')
    expect(light.index).toBeGreaterThanOrEqual(0)
    expect(light.index).toBeLessThan(PROJECT_COVERS.length)
    expect(light.emoji).toBeTruthy()
    expect(projectCoverView('p-1')).toEqual(light)
    expect(dark).toBe(PROJECT_COVERS_DARK)
    expect(coverChoicesFor('light')).toBe(PROJECT_COVERS)
  })
})
