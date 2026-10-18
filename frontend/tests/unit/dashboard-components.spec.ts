/**
 * 全局看板与活动页面单元测试（T33）。
 *
 * 重点：
 * - 数字与服务端 payload 一一对应（不做本地重算）；
 * - 请求失败保留旧数据并标记过期（不显示伪造的 0，也不显示空系统）；
 * - scope 与 as_of 可见；工作中/占槽/等待回收并列；异常可跳转对应看板；
 * - 活动页最新在前、可分页与筛选，失败同样保留旧数据。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import type { FetchLike } from '@/api/client'
import { createFetchStub, errEnvelope, okEnvelope, pageEnvelope, type StubCall, type StubReply } from './helpers/fetchStub'

vi.stubGlobal('fetch', undefined as unknown as FetchLike)

const { default: DashboardView } = await import('@/views/DashboardView.vue')
const { default: ActivityView } = await import('@/views/ActivityView.vue')
const { default: DashboardWorkload } = await import('@/components/dashboard/DashboardWorkload.vue')
const { default: DashboardExceptions } = await import('@/components/dashboard/DashboardExceptions.vue')
const format = await import('@/components/dashboard/dashboard-format')

const stub = createFetchStub()

/** 视图使用真实 vue-router（scope/筛选写回 URL）：测试里用内存路由，行为与浏览器一致。 */
async function makeRouter(initial = '/'): Promise<Router> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/activity', component: { template: '<div />' } },
      { path: '/projects/:id/board', component: { template: '<div />' } },
    ],
  })
  await router.push(initial)
  await router.isReady()
  return router
}

function dashboardPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    as_of: '2026-10-15T03:00:00.123456Z',
    scope: {
      actor_type: 'web_operator',
      agent_id: null,
      role: null,
      project_id: null,
      project_ids: ['p1'],
      project_count: 1,
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
    ...overrides,
  }
}

let wrappers: VueWrapper[] = []
afterEach(() => {
  for (const wrapper of wrappers) wrapper.unmount()
  wrappers = []
  document.body.innerHTML = ''
})

beforeEach(() => {
  stub.reset()
  vi.stubGlobal('fetch', stub.fetch as unknown as FetchLike)
  document.body.innerHTML = ''
})

function track<T extends VueWrapper>(wrapper: T): T {
  wrappers.push(wrapper)
  return wrapper
}

async function mountDashboard() {
  const router = await makeRouter('/')
  return track(mount(DashboardView, { global: { plugins: [router] } }))
}

async function mountActivity(initial = '/activity') {
  const router = await makeRouter(initial)
  return track(mount(ActivityView, { global: { plugins: [router] } }))
}

// ---------------------------------------------------------------------------

describe('dashboard-format', () => {
  it('scope 标识区分本地管理员与员工可见性', () => {
    expect(
      format.scopeSummary({
        actor_type: 'web_operator',
        project_ids: ['p1'],
        project_count: 3,
        include_archived: false,
        task_visibility: 'FULL',
        archived_project_total: 0,
      } as never),
    ).toContain('全部可访问项目（3 个）')
    expect(
      format.scopeSummary({
        actor_type: 'agent',
        agent_id: 'a1',
        project_ids: ['p1'],
        project_count: 1,
        include_archived: true,
        task_visibility: 'PUBLISHED_ONLY',
        archived_project_total: 0,
      } as never),
    ).toContain('仅已发布任务（准备中不计入）')
  })

  it('空范围完成率显示“无任务”，绝不显示 0% 或 100%', () => {
    expect(
      format.completionRateText({ done: 0, total: 0, rate: null, rate_available: false, reason: 'NO_TASKS' }),
    ).toBe('无任务（不计算完成率）')
    expect(format.completionRateText({ done: 1, total: 6, rate: 0.1667, rate_available: true })).toBe(
      '16.7%（1/6）',
    )
  })

  it('异常原因与类型使用服务端机器可读值', () => {
    const item = dashboardPayload().exceptions as never as Array<import('@/api/dashboard').ExceptionItemDTO>
    expect(format.exceptionReasonLabels(item[0]!)).toEqual(['执行已过固定截止（等待回收）'])
    expect(format.exceptionKindLabel(item[0]!)).toContain('需要回收')
  })
})

describe('DashboardView', () => {
  function planDashboard(dashboardReplies: StubReply[]) {
    let dashboardIndex = 0
    return (call: StubCall): StubReply => {
      if (call.url.includes('/dashboard')) {
        const reply = dashboardReplies[Math.min(dashboardIndex, dashboardReplies.length - 1)]!
        dashboardIndex += 1
        return reply
      }
      if (call.url.includes('/projects')) return { status: 200, json: pageEnvelope([]) }
      return { status: 200, json: okEnvelope(null) }
    }
  }

  it('按服务端 payload 展示五态/难度/完成率/scope/as_of，不做本地重算', async () => {
    stub.setDefault(planDashboard([{ status: 200, json: okEnvelope(dashboardPayload()) }]))
    const wrapper = await mountDashboard()
    await flushPromises()

    expect(wrapper.find('[data-testid="dashboard-count-PREPARING"]').text()).toBe('1')
    expect(wrapper.find('[data-testid="dashboard-count-TODO"]').text()).toBe('3')
    expect(wrapper.find('[data-testid="dashboard-count-IN_PROGRESS"]').text()).toBe('1')
    expect(wrapper.find('[data-testid="dashboard-count-IN_REVIEW"]').text()).toBe('0')
    expect(wrapper.find('[data-testid="dashboard-count-DONE"]').text()).toBe('1')
    expect(wrapper.find('[data-testid="dashboard-task-total"]').text()).toContain('6')
    expect(wrapper.find('[data-testid="dashboard-completion-rate"]').text()).toBe('16.7%（1/6）')
    expect(wrapper.find('[data-testid="dashboard-difficulty-5"]').text()).toBe('1')
    expect(wrapper.find('[data-testid="dashboard-scope"]').text()).toContain('本地管理员')
    expect(wrapper.find('[data-testid="dashboard-scope"]').text()).toContain('全部可访问项目（1 个）')
    expect(wrapper.find('[data-testid="dashboard-as-of"]').text()).toContain('2026-10-15T03:00:00.123456Z')
    expect(wrapper.find('[data-testid="dashboard-stale"]').exists()).toBe(false)
  })

  it('工作中与占槽并列，等待回收单独可见（含 Agent 行标记）', async () => {
    stub.setDefault(planDashboard([{ status: 200, json: okEnvelope(dashboardPayload()) }]))
    const wrapper = await mountDashboard()
    await flushPromises()

    expect(wrapper.find('[data-testid="dashboard-effective-executions"]').text()).toContain('1')
    expect(wrapper.find('[data-testid="dashboard-reserved-slots"]').text()).toContain('2')
    expect(wrapper.find('[data-testid="dashboard-waiting-reclaim"]').text()).toContain('1')
    expect(wrapper.find('[data-testid="dashboard-open-executions"]').text()).toContain('2')
    expect(wrapper.find('[data-testid="dashboard-agent-counts"]').text()).toContain('工作中')
    expect(wrapper.find('[data-testid="dashboard-agent-waiting-a1"]').text()).toContain('等待回收 1')
    expect(wrapper.find('[data-testid="dashboard-agent-slots-a1"]').text()).toContain('2 / 3')
  })

  it('异常列表提供跳转到对应项目看板的入口', async () => {
    stub.setDefault(planDashboard([{ status: 200, json: okEnvelope(dashboardPayload()) }]))
    const wrapper = await mountDashboard()
    await flushPromises()

    const link = wrapper.find('[data-testid="dashboard-exception-link-task-1"]')
    expect(link.attributes('href')).toBe('/projects/p1/board')
    expect(wrapper.find('[data-testid="dashboard-exception-kind-task-1"]').text()).toContain('需要回收')
    expect(wrapper.find('[data-testid="dashboard-project-board-p1"]').attributes('href')).toBe(
      '/projects/p1/board',
    )
  })

  it('刷新失败：保留旧数据并标记过期原因，绝不显示 0', async () => {
    stub.setDefault(
      planDashboard([
        { status: 200, json: okEnvelope(dashboardPayload()) },
        { status: 503, json: errEnvelope('DATABASE_BUSY', '数据库繁忙，请稍后重试') },
      ]),
    )
    const wrapper = await mountDashboard()
    await flushPromises()
    expect(wrapper.find('[data-testid="dashboard-count-TODO"]').text()).toBe('3')

    await wrapper.find('[data-testid="dashboard-refresh"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="dashboard-stale"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="dashboard-stale"]').text()).toContain('数据库繁忙')
    expect(wrapper.find('[data-testid="dashboard-stale"]').text()).toContain('不是 0')
    // 旧数据仍在，且没有被清成 0
    expect(wrapper.find('[data-testid="dashboard-count-TODO"]').text()).toBe('3')
    expect(wrapper.find('[data-testid="dashboard-count-DONE"]').text()).toBe('1')
    expect(wrapper.find('[data-testid="dashboard-task-total"]').text()).toContain('6')
    expect(wrapper.find('[data-testid="state-empty"]').exists()).toBe(false)
  })

  it('首次加载失败：显示错误态（不伪造空系统）', async () => {
    stub.setDefault(
      planDashboard([{ status: 503, json: errEnvelope('DEPENDENCY_UNAVAILABLE', '统计不可用') }]),
    )
    const wrapper = await mountDashboard()
    await flushPromises()

    expect(wrapper.find('[data-testid="state-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="state-error-message"]').text()).toContain('统计不可用')
    expect(wrapper.find('[data-testid="dashboard-task-counts"]').exists()).toBe(false)
  })

  it('scope 切换（含归档）作为参数发送给服务端', async () => {
    stub.setDefault(planDashboard([{ status: 200, json: okEnvelope(dashboardPayload()) }]))
    const wrapper = await mountDashboard()
    await flushPromises()

    await wrapper.find('[data-testid="dashboard-include-archived"]').setValue(true)
    await wrapper.find('[data-testid="dashboard-scope-bar"]').trigger('submit')
    await flushPromises()

    const dashboardCalls = stub.calls.filter((call) => call.url.includes('/dashboard'))
    expect(dashboardCalls[dashboardCalls.length - 1]!.url).toContain('include_archived=true')
  })
})

describe('Dashboard 子组件（直接喂 payload）', () => {
  it('Workload 显示等待回收提示与容量', async () => {
    const wrapper = track(
      mount(DashboardWorkload, {
        props: {
          dashboard: dashboardPayload() as never,
        },
      }),
    )
    expect(wrapper.find('[data-testid="dashboard-workload-hint"]').text()).toContain('仍占用容量槽')
    expect(wrapper.find('[data-testid="dashboard-capacity-available"]').text()).toContain('4 / 6')
  })

  it('Exceptions 显示真实计数与列表上限说明', async () => {
    const wrapper = track(
      mount(DashboardExceptions, {
        props: { dashboard: dashboardPayload() as never },
        global: { plugins: [await makeRouter('/')] },
      }),
    )
    expect(wrapper.find('[data-testid="dashboard-exception-counts"]').text()).toContain('合计 1')
    expect(wrapper.find('[data-testid="dashboard-exception-returned"]').text()).toContain('上限 20')
  })
})

describe('ActivityView', () => {
  function planActivity(replies: StubReply[]) {
    let index = 0
    return (call: StubCall): StubReply => {
      if (call.url.includes('/activity')) {
        const reply = replies[Math.min(index, replies.length - 1)]!
        index += 1
        return reply
      }
      if (call.url.includes('/projects')) {
        return {
          status: 200,
          json: pageEnvelope([
            { id: 'p1', title: '项目一', status: 'ACTIVE', content_version: 1, version: 1 },
          ]),
        }
      }
      return { status: 200, json: okEnvelope(null) }
    }
  }

  const events = [
    {
      id: 'e2',
      project_id: 'p1',
      task_id: 't1',
      actor_type: 'web_operator',
      action: 'task.review',
      reason: 'E2E 驳回',
      request_id: 'req-2',
      created_at: '2026-10-15T03:00:00Z',
    },
    {
      id: 'e1',
      project_id: 'p1',
      actor_type: 'system',
      action: 'task.timeout',
      created_at: '2026-10-15T02:00:00Z',
    },
  ]

  it('按服务端顺序（最新在前）渲染，并展示原因与请求号', async () => {
    stub.setDefault(
      planActivity([{ status: 200, json: pageEnvelope(events, { total: 30, page: 1, pageSize: 20 }) }]),
    )
    const wrapper = await mountActivity()
    await flushPromises()

    const items = wrapper.findAll('[data-testid^="activity-item-"]')
    expect(items).toHaveLength(2)
    expect(items[0]!.attributes('data-testid')).toBe('activity-item-e2')
    expect(items[1]!.attributes('data-testid')).toBe('activity-item-e1')
    expect(wrapper.find('[data-testid="activity-reason-e2"]').text()).toContain('E2E 驳回')
    expect(wrapper.find('[data-testid="activity-actor-e2"]').text()).toContain('本地管理员')
    expect(wrapper.find('[data-testid="activity-actor-e1"]').text()).toContain('system')
    expect(wrapper.find('[data-testid="activity-summary"]').text()).toContain('共 30 条')
    expect(wrapper.find('[data-testid="activity-list"]').text()).toContain('req-2')
  })

  it('筛选与分页作为参数发送，清空筛选恢复默认', async () => {
    stub.setDefault(planActivity([{ status: 200, json: pageEnvelope(events, { total: 30 }) }]))
    const wrapper = await mountActivity()
    await flushPromises()

    await wrapper.find('[data-testid="activity-filter-project"]').setValue('p1')
    await wrapper.find('[data-testid="activity-filter-action"]').setValue(' task.review ')
    await wrapper.find('[data-testid="activity-filter-include-archived"]').setValue(true)
    await wrapper.find('[data-testid="activity-filters"]').trigger('submit')
    await flushPromises()

    const filtered = stub.calls.filter((call) => call.url.includes('/activity')).pop()!
    expect(filtered.url).toContain('project_id=p1')
    expect(filtered.url).toContain('action=task.review')
    expect(filtered.url).toContain('include_archived=true')
    expect(filtered.url).toContain('page=1')

    await wrapper.find('[data-testid="activity-page-next"]').trigger('click')
    await flushPromises()
    const paged = stub.calls.filter((call) => call.url.includes('/activity')).pop()!
    expect(paged.url).toContain('page=2')
  })

  it('失败保留旧数据并标记过期，而不是显示“没有活动”', async () => {
    stub.setDefault(
      planActivity([
        { status: 200, json: pageEnvelope(events, { total: 2 }) },
        { status: 503, json: errEnvelope('DATABASE_BUSY', '数据库繁忙') },
      ]),
    )
    const wrapper = await mountActivity()
    await flushPromises()
    expect(wrapper.findAll('[data-testid^="activity-item-"]')).toHaveLength(2)

    await wrapper.find('[data-testid="activity-refresh"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="activity-stale"]').text()).toContain('数据库繁忙')
    expect(wrapper.findAll('[data-testid^="activity-item-"]')).toHaveLength(2)
    expect(wrapper.find('[data-testid="activity-empty"]').exists()).toBe(false)
  })

  it('空结果由服务端确认后显示空态', async () => {
    stub.setDefault(planActivity([{ status: 200, json: pageEnvelope([], { total: 0 }) }]))
    const wrapper = await mountActivity()
    await flushPromises()
    expect(wrapper.find('[data-testid="state-empty"]').exists()).toBe(true)
  })
})
