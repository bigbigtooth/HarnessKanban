/**
 * Agent 页面组件与视图单元测试（T29）。
 *
 * 覆盖安全与状态优先级要求：
 * - Key 一次性交付框：显示、关闭即清除、重放（key_delivery_unavailable）不显示任何 Key；
 * - 创建/重置 Key 之后 localStorage / sessionStorage 必须为空；
 * - 禁用优先于在线状态；有效执行数与已占容量槽分开；“等待回收”标识到期未占用的槽；
 * - 禁用表单：必须填原因；持有时必须明确 WAIT/NOW 并列出受影响任务。
 */
import { beforeEach, describe, expect, it, vi, afterEach } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import type { FetchLike } from '@/api/client'
import type { StubCall, StubReply } from './helpers/fetchStub'
import { createFetchStub, errEnvelope, okEnvelope, pageEnvelope, trackLocalStorage } from './helpers/fetchStub'

vi.stubGlobal('fetch', undefined as unknown as FetchLike)

const { default: AgentsView } = await import('@/views/AgentsView.vue')
const { default: AgentKeyDeliveryDialog } = await import('@/components/agent/AgentKeyDeliveryDialog.vue')
const { default: AgentDisableForm } = await import('@/components/agent/AgentDisableForm.vue')
const format = await import('@/components/agent/agent-format')

const stub = createFetchStub()

const AGENT_ID = '11111111-1111-4111-8111-111111111111'

function summary(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: AGENT_ID,
    name: 'worker-a',
    role: 'WORKER',
    description: '负责后端',
    capabilities: ['python'],
    enabled: true,
    max_concurrency: 3,
    reserved_execution_count: 1,
    available_capacity: 2,
    last_heartbeat_at: '2026-10-15T00:00:00Z',
    online_state: 'ONLINE_WORKING',
    version: 2,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-15T00:00:00Z',
    project_ids: ['p1'],
    ...overrides,
  }
}

function detail(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    ...summary(),
    key_version: 1,
    project_ids: ['p1'],
    projects: [{ id: 'p1', title: '示例项目', status: 'ACTIVE' }],
    capacity: { max_concurrency: 3, reserved_execution_count: 1, available_capacity: 2 },
    execution_summary: {
      open_count: 2,
      effective_count: 1,
      completed_count: 5,
      timeout_count: 1,
      rework_count: 1,
      total_count: 7,
      pending_reclaim_count: 1,
    },
    current_executions: [
      {
        execution_id: 'exec-live',
        task_id: 'task-live',
        project_id: 'p1',
        display_number: 'TASK-0001',
        difficulty: 3,
        claimed_at: '2026-10-15T00:00:00Z',
        execution_deadline_at: '2026-10-15T01:00:00Z',
        expired: false,
      },
      {
        execution_id: 'exec-stale',
        task_id: 'task-stale',
        project_id: 'p1',
        display_number: 'TASK-0002',
        difficulty: 2,
        claimed_at: '2026-10-14T00:00:00Z',
        execution_deadline_at: '2026-10-14T01:00:00Z',
        expired: true,
      },
    ],
    ...overrides,
  }
}

function isDetailUrl(url: string): boolean {
  const path = url.split('?')[0]!
  return /\/api\/web\/v1\/agents\/[^/]+$/.test(path)
}

let wrappers: VueWrapper[] = []

function trackWrapper<T extends VueWrapper>(wrapper: T): T {
  wrappers.push(wrapper)
  return wrapper
}

/**
 * BaseDialog 通过 Teleport 渲染到 body，VTU 的 wrapper.find 看不到，
 * 因此对弹窗内容统一用真实 DOM 查询 + 原生事件（与浏览器行为一致）。
 */
function bodyEl<T extends HTMLElement>(testid: string): T {
  const element = document.body.querySelector<T>(`[data-testid="${testid}"]`)
  if (!element) throw new Error(`未找到弹窗元素：${testid}`)
  return element
}

async function setBodyValue(testid: string, value: string): Promise<void> {
  const element = bodyEl<HTMLInputElement | HTMLTextAreaElement>(testid)
  element.value = value
  element.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
}

async function clickBody(testid: string): Promise<void> {
  bodyEl<HTMLButtonElement>(testid).click()
  await flushPromises()
}

async function checkBodyRadio(testid: string): Promise<void> {
  const element = bodyEl<HTMLInputElement>(testid)
  element.checked = true
  element.dispatchEvent(new Event('change', { bubbles: true }))
  await flushPromises()
}

afterEach(() => {
  for (const wrapper of wrappers) wrapper.unmount()
  wrappers = []
  document.body.innerHTML = ''
  localStorage.clear()
  sessionStorage.clear()
})

beforeEach(() => {
  stub.reset()
  vi.stubGlobal('fetch', stub.fetch as unknown as FetchLike)
  document.body.innerHTML = ''
  localStorage.clear()
  sessionStorage.clear()
})

// ---------------------------------------------------------------------------
// 格式化：状态优先级与槽位
// ---------------------------------------------------------------------------

describe('agent-format 展示规则', () => {
  it('禁用优先于在线状态；其余状态取服务端 online_state，不在本地重算', () => {
    expect(format.agentDisplayState({ enabled: false, online_state: 'ONLINE_WORKING' })).toBe('DISABLED')
    expect(format.agentStateLabel({ enabled: false, online_state: 'ONLINE_WORKING' })).toBe('已禁用')
    expect(format.agentDisplayState({ enabled: true, online_state: 'ONLINE_IDLE' })).toBe('ONLINE_IDLE')
    expect(format.agentStateLabel({ enabled: true, online_state: 'OFFLINE' })).toBe('离线')
  })

  it('有效执行与占槽分开；等待回收 = 到期未回收仍占槽', () => {
    const view = format.slotView({
      max_concurrency: 3,
      reserved_execution_count: 2,
      available_capacity: 1,
      execution_summary: {
        open_count: 2,
        effective_count: 1,
        completed_count: 0,
        timeout_count: 0,
        rework_count: 0,
        total_count: 2,
        pending_reclaim_count: 1,
      },
    })
    expect(view).toEqual({ effective: 1, reserved: 2, waitingReclaim: 1, maxConcurrency: 3, available: 1 })
  })

  it('筛选条件映射为服务端参数，空关键词不发送', () => {
    expect(format.toAgentListQuery(format.emptyAgentFilters(), 1, 20)).toEqual({
      name: null,
      role: null,
      enabled: null,
      online: null,
      page: 1,
      pageSize: 20,
    })
    expect(
      format.toAgentListQuery({ name: '  x ', role: 'WORKER', enabled: 'false', online: 'true' }, 3, 50),
    ).toEqual({ name: 'x', role: 'WORKER', enabled: false, online: true, page: 3, pageSize: 50 })
  })
})

// ---------------------------------------------------------------------------
// 一次性 Key 交付框
// ---------------------------------------------------------------------------

describe('AgentKeyDeliveryDialog（一次性 Key）', () => {
  it('展示一次性 Key，关闭后立即从 DOM 清除', async () => {
    const wrapper = trackWrapper(
      mount(AgentKeyDeliveryDialog, {
        props: { open: true, agentName: 'agent-a', agentKey: 'htk_live_secret_once', mode: 'create' as const },
      }),
    )
    await flushPromises()

    const input = document.body.querySelector<HTMLInputElement>('[data-testid="agent-key-value"]')
    expect(input?.value).toBe('htk_live_secret_once')

    await wrapper.setProps({ open: false })
    await flushPromises()
    expect(document.body.querySelector('[data-testid="agent-key-value"]')).toBeNull()
    expect(document.body.textContent ?? '').not.toContain('htk_live_secret_once')
  })

  it('重放（key_delivery_unavailable）只提示重新重置，不显示任何 Key', async () => {
    mount(AgentKeyDeliveryDialog, {
      props: {
        open: true,
        agentName: 'agent-a',
        agentKey: null,
        keyDeliveryUnavailable: true,
        mode: 'rotate' as const,
      },
    })
    await flushPromises()

    const notice = document.body.querySelector('[data-testid="agent-key-unavailable"]')
    expect(notice?.textContent).toContain('已无法再次交付，请重新重置')
    expect(document.body.querySelector('[data-testid="agent-key-value"]')).toBeNull()
  })

  it('没有 Key 也没有重放标记时，不编造 Key', async () => {
    mount(AgentKeyDeliveryDialog, {
      props: { open: true, agentName: 'agent-a', agentKey: null, mode: 'enable' as const },
    })
    await flushPromises()
    expect(document.body.querySelector('[data-testid="agent-key-empty"]')).not.toBeNull()
    expect(document.body.querySelector('[data-testid="agent-key-value"]')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// 禁用表单
// ---------------------------------------------------------------------------

describe('AgentDisableForm', () => {
  const executions = [
    {
      execution_id: 'exec-stale',
      task_id: 'task-stale',
      project_id: 'p1',
      display_number: 'TASK-0002',
      difficulty: 2,
      claimed_at: '2026-10-14T00:00:00Z',
      execution_deadline_at: '2026-10-14T01:00:00Z',
      expired: true,
    },
    {
      execution_id: 'exec-live',
      task_id: 'task-live',
      project_id: 'p1',
      display_number: 'TASK-0001',
      difficulty: 3,
      claimed_at: '2026-10-15T00:00:00Z',
      execution_deadline_at: '2026-10-15T01:00:00Z',
      expired: false,
    },
  ]

  it('没有原因时阻止提交并给出提示', async () => {
    trackWrapper(
      mount(AgentDisableForm, {
        props: { open: true, agentName: 'a', executions, reservedCount: 2 },
      }),
    )
    await flushPromises()

    await clickBody('agent-disable-submit')
    expect(bodyEl('agent-disable-local-error').textContent).toContain('必须填写原因')
  })

  it('持有执行时列出受影响任务（含等待回收）并把 WAIT/NOW 处置一起提交', async () => {
    const wrapper = trackWrapper(
      mount(AgentDisableForm, {
        props: { open: true, agentName: 'a', executions, reservedCount: 2 },
      }),
    )
    await flushPromises()

    expect(bodyEl('agent-disable-impact').textContent).toContain('TASK-0002')
    expect(bodyEl('agent-disable-impact').textContent).toContain('TASK-0001')
    expect(bodyEl('agent-disable-waiting').textContent).toBe('等待回收')

    await setBodyValue('agent-disable-reason', '违反操作规范')
    await checkBodyRadio('agent-disable-disposition-now')
    await clickBody('agent-disable-submit')

    expect(wrapper.emitted('submit')?.[0]?.[0]).toEqual({ disposition: 'NOW', reason: '违反操作规范' })
  })

  it('没有未结束执行时明确说明不会回收任务，并以默认 WAIT 提交', async () => {
    const wrapper = trackWrapper(
      mount(AgentDisableForm, { props: { open: true, agentName: 'a', executions: [], reservedCount: 0 } }),
    )
    await flushPromises()
    expect(bodyEl('agent-disable-impact-none').textContent).toContain('没有未结束执行')

    await setBodyValue('agent-disable-reason', '长期未使用')
    await clickBody('agent-disable-submit')
    expect(wrapper.emitted('submit')?.[0]?.[0]).toEqual({ disposition: 'WAIT', reason: '长期未使用' })
  })
})

// ---------------------------------------------------------------------------
// AgentsView：创建 → Key 一次性交付 → 关闭清除；重放不显示
// ---------------------------------------------------------------------------

function planAgents(createReply: StubReply | null): (call: StubCall) => StubReply {
  return (call) => {
    if (call.url.includes('/agents') && call.method === 'GET') {
      if (isDetailUrl(call.url)) return { status: 200, json: okEnvelope(detail()) }
      return { status: 200, json: pageEnvelope([summary()]) }
    }
    if (call.method === 'POST' && createReply) return createReply
    return { status: 200, json: okEnvelope({}) }
  }
}

describe('AgentsView：创建与 Key 交付', () => {
  it('创建成功后只在交付框显示一次 Key，关闭即清除，且不写任何持久存储', async () => {
    const tracker = trackLocalStorage()
    try {
      stub.setDefault(
        planAgents({ status: 201, json: okEnvelope({ ...detail(), agent_key: 'htk_live_secret_once' }) }),
      )
      const wrapper = trackWrapper(mount(AgentsView))
      await flushPromises()

      await wrapper.find('[data-testid="agent-create-open"]').trigger('click')
      await setBodyValue('agent-create-name', 'worker-a')
      await clickBody('agent-create-submit')
      await flushPromises()

      const input = document.body.querySelector<HTMLInputElement>('[data-testid="agent-key-value"]')
      expect(input?.value).toBe('htk_live_secret_once')
      expect(tracker.writes).toEqual([])
      expect(localStorage.length).toBe(0)
      expect(sessionStorage.length).toBe(0)

      // 关闭交付框：Key 从内存与 DOM 同时清除
      await clickBody('agent-key-close')

      expect(document.body.querySelector('[data-testid="agent-key-value"]')).toBeNull()
      expect(document.body.textContent ?? '').not.toContain('htk_live_secret_once')
      expect(wrapper.html()).not.toContain('htk_live_secret_once')
      expect(tracker.writes).toEqual([])
    } finally {
      tracker.restore()
    }
  })

  it('幂等重放（key_delivery_unavailable）提示重新重置且不显示 Key', async () => {
    stub.setDefault(
      planAgents({
        status: 200,
        json: okEnvelope({ ...detail(), idempotent_replay: true, key_delivery_unavailable: true }),
      }),
    )
    const wrapper = trackWrapper(mount(AgentsView))
    await flushPromises()

    await wrapper.find('[data-testid="agent-create-open"]').trigger('click')
    await setBodyValue('agent-create-name', 'worker-a')
    await clickBody('agent-create-submit')
    await flushPromises()

    expect(bodyEl('agent-key-unavailable').textContent).toContain('已无法再次交付，请重新重置')
    expect(document.body.querySelector('[data-testid="agent-key-value"]')).toBeNull()
  })

  it('创建失败保留表单内容并回显服务端原因，不出现 Key 交付框', async () => {
    stub.setDefault(planAgents({ status: 409, json: errEnvelope('AGENT_NAME_CONFLICT', 'Agent 名称已存在') }))
    const wrapper = trackWrapper(mount(AgentsView))
    await flushPromises()

    await wrapper.find('[data-testid="agent-create-open"]').trigger('click')
    await setBodyValue('agent-create-name', 'duplicated')
    await clickBody('agent-create-submit')
    await flushPromises()

    expect(bodyEl('agent-create-form').textContent).toContain('Agent 名称已存在')
    expect(bodyEl<HTMLInputElement>('agent-create-name').value).toBe('duplicated')
    expect(document.body.querySelector('[data-testid="agent-key-value"]')).toBeNull()
  })
})

describe('AgentsView：列表与详情', () => {
  it('列表按名称/角色/启用/在线筛选，并把筛选条件作为请求参数发送', async () => {
    stub.setDefault(planAgents(null))
    const wrapper = trackWrapper(mount(AgentsView))
    await flushPromises()

    expect(wrapper.find('[data-testid="agent-table"]').text()).toContain('worker-a')

    await wrapper.find('[data-testid="agent-filter-name"]').setValue('worker')
    await wrapper.find('[data-testid="agent-filter-role"]').setValue('WORKER')
    await wrapper.find('[data-testid="agent-filter-enabled"]').setValue('true')
    await wrapper.find('[data-testid="agent-filter-online"]').setValue('false')
    await wrapper.find('[data-testid="agent-filters"]').trigger('submit')
    await flushPromises()

    const listCalls = stub.calls.filter((call) => call.method === 'GET' && !isDetailUrl(call.url))
    const url = listCalls[listCalls.length - 1]!.url
    expect(url).toContain('name=worker')
    expect(url).toContain('role=WORKER')
    expect(url).toContain('enabled=true')
    expect(url).toContain('online=false')
    expect(url).toContain('page=1')
  })

  it('详情分开显示有效执行与占槽，并标记等待回收', async () => {
    stub.setDefault(planAgents(null))
    const wrapper = trackWrapper(mount(AgentsView))
    await flushPromises()

    await wrapper.find(`[data-testid="agent-row-detail-${AGENT_ID}"]`).trigger('click')
    await flushPromises()

    const panel = wrapper.find('[data-testid="agent-detail"]')
    expect(panel.find('[data-testid="agent-detail-effective"]').text()).toBe('1')
    expect(panel.find('[data-testid="agent-detail-reserved"]').text()).toBe('1 / 3')
    expect(panel.find('[data-testid="agent-detail-available"]').text()).toContain('2')
    expect(panel.find('[data-testid="agent-detail-waiting-reclaim"]').text()).toContain('等待回收')
    expect(panel.find('[data-testid="agent-detail-projects"]').text()).toContain('示例项目')
    expect(panel.find('[data-testid="agent-detail-history"]').text()).toContain('累计 7')
    expect(panel.find('[data-testid="agent-execution-waiting-reclaim"]').text()).toBe('等待回收')
  })

  it('禁用优先于在线状态，即使服务端仍返回 ONLINE_WORKING', async () => {
    stub.setDefault((call) => {
      if (call.url.includes('/agents') && call.method === 'GET') {
        if (isDetailUrl(call.url)) {
          return { status: 200, json: okEnvelope(detail({ enabled: false, online_state: 'ONLINE_WORKING' })) }
        }
        return {
          status: 200,
          json: pageEnvelope([summary({ enabled: false, online_state: 'ONLINE_WORKING' })]),
        }
      }
      return { status: 200, json: okEnvelope({}) }
    })
    const wrapper = trackWrapper(mount(AgentsView))
    await flushPromises()

    const badge = wrapper.find(`[data-testid="agent-row-state-${AGENT_ID}"]`)
    expect(badge.text()).toBe('已禁用')
    expect(badge.attributes('data-state')).toBe('DISABLED')
  })

  it('重置 Key：确认后新 Key 只出现一次，关闭清除，且执行截止不变', async () => {
    stub.setDefault((call) => {
      if (call.method === 'GET') {
        if (isDetailUrl(call.url)) return { status: 200, json: okEnvelope(detail()) }
        return { status: 200, json: pageEnvelope([summary()]) }
      }
      if (call.url.includes('/rotate-key')) {
        return { status: 200, json: okEnvelope({ ...detail({ key_version: 2 }), agent_key: 'rotated-secret' }) }
      }
      return { status: 200, json: okEnvelope({}) }
    })
    const wrapper = trackWrapper(mount(AgentsView))
    await flushPromises()

    await wrapper.find(`[data-testid="agent-row-detail-${AGENT_ID}"]`).trigger('click')
    await flushPromises()

    await wrapper.find('[data-testid="agent-rotate-key"]').trigger('click')
    await flushPromises()
    expect(document.body.textContent ?? '').toContain('执行及其固定截止时刻不会改变')
    document.body.querySelector<HTMLButtonElement>('[data-testid="agent-rotate-confirm"]')!.click()
    await flushPromises()
    await flushPromises()

    const input = document.body.querySelector<HTMLInputElement>('[data-testid="agent-key-value"]')
    expect(input?.value).toBe('rotated-secret')
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)

    document.body.querySelector<HTMLButtonElement>('[data-testid="agent-key-close"]')!.click()
    await flushPromises()
    expect(document.body.textContent ?? '').not.toContain('rotated-secret')
  })
})
