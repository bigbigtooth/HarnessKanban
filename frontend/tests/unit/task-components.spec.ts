/**
 * 任务看板组件与页面状态单元测试（T31/T32）。
 *
 * 网络层用 fetch 替身（`helpers/fetchStub`），组件、表单校验、幂等键与状态逻辑保持真实；
 * 不 mock 业务规则，只验证“界面把服务端事实如实呈现、失败不伪造成成功”。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import type { FetchLike } from '@/api/client'
import { TaskStatus } from '@/api/types'
import type { AgentOptionDTO, TaskDetailDTO, TaskListItemDTO } from '@/api/tasks'
import TaskClaimForm from '@/components/task/TaskClaimForm.vue'
import TaskSubmitForm from '@/components/task/TaskSubmitForm.vue'
import {
  activeFilterCount,
  agentDirectory,
  agentName,
  assignableAgents,
  ensureAgentDirectory,
  filterSignature,
  filtersForScope,
  resetAgentDirectory,
  resetFiltersForScope,
  setFiltersForScope,
  supervisorCandidates,
} from '@/components/task/boardState'
import { createFetchStub, errEnvelope, okEnvelope, pageEnvelope } from './helpers/fetchStub'

const stub = createFetchStub()

beforeEach(() => {
  stub.reset()
  resetAgentDirectory()
  vi.stubGlobal('fetch', stub.fetch as unknown as FetchLike)
})

afterEach(() => {
  document.body.innerHTML = ''
})

// ---------------------------------------------------------------------------
// 夹具
// ---------------------------------------------------------------------------

function taskFixture(overrides: Partial<TaskListItemDTO> = {}): TaskListItemDTO {
  return {
    id: 'task-1',
    project_id: 'project-1',
    number: 3,
    display_number: 'TASK-0003',
    title: '实现任务看板',
    difficulty: 2,
    status: TaskStatus.IN_PROGRESS,
    claim_mode: 'ASSIGNED',
    designated_agent_id: 'agent-1',
    assignee_agent_id: 'agent-1',
    active_execution_id: 'exec-1',
    current_submission_id: null,
    sort_order: '1000',
    due_at: '2026-10-16T10:00:00Z',
    version: 4,
    is_archived: false,
    ...overrides,
  }
}

function detailFixture(overrides: Partial<TaskDetailDTO> = {}): TaskDetailDTO {
  return {
    ...taskFixture(),
    description: '任务要求文本',
    acceptance_criteria: '- 验收标准一',
    attachment_count: 2,
    current_execution: {
      execution_id: 'exec-1',
      task_id: 'task-1',
      agent_id: 'agent-1',
      sequence: 1,
      difficulty: 2,
      execution_timeout_seconds: 1800,
      claimed_at: '2026-10-15T00:00:00Z',
      execution_deadline_at: '2026-10-15T00:30:00Z',
    },
    execution_deadline_at: '2026-10-15T00:30:00Z',
    ...overrides,
  }
}

function agentFixture(id: string, overrides: Partial<AgentOptionDTO> = {}): AgentOptionDTO {
  return {
    id,
    name: `agent-${id}`,
    role: 'SUPERVISOR',
    enabled: true,
    max_concurrency: 3,
    reserved_execution_count: 0,
    version: 1,
    ...overrides,
  }
}
// ---------------------------------------------------------------------------
// 领取表单
// ---------------------------------------------------------------------------

describe('TaskClaimForm（代领）', () => {
  it('未确认不能领取；确认后展示唯一执行回执（execution_id / 固定超时 / 固定截止）', async () => {
    stub.reply({ status: 200, json: pageEnvelope([agentFixture('agent-1', { role: 'WORKER', name: '李某' })]) })
    stub.reply({
      status: 200,
      json: okEnvelope({
        task_id: 'task-1',
        display_number: 'TASK-0003',
        status: 'IN_PROGRESS',
        version: 5,
        assignee_agent_id: 'agent-1',
        execution_id: 'exec-9',
        execution_timeout_seconds: 1800,
        execution_deadline_at: '2026-10-15T09:30:00Z',
      }),
    })

    const wrapper = mount(TaskClaimForm, {
      props: { task: taskFixture({ status: TaskStatus.TODO }), projectId: 'project-1' },
      attachTo: document.body,
    })
    await flushPromises()

    const select = new DOMWrapper(document.body.querySelector('[data-testid="claim-agent"]')!)
    await select.setValue('agent-1')
    await new DOMWrapper(document.body.querySelector('[data-testid="claim-submit"]')!).trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[data-testid="claim-local-error"]')?.textContent).toContain('请先勾选确认')

    await new DOMWrapper(document.body.querySelector('[data-testid="claim-confirm-check"]')!).setValue(true)
    await new DOMWrapper(document.body.querySelector('[data-testid="claim-submit"]')!).trigger('click')
    await flushPromises()

    const receipt = document.body.querySelector('[data-testid="claim-receipt"]')
    expect(receipt).not.toBeNull()
    expect(receipt!.querySelector('[data-testid="claim-execution-id"]')?.textContent?.trim()).toBe('exec-9')
    expect(receipt!.querySelector('[data-testid="claim-execution-timeout"]')?.textContent).toContain('1800 秒')
    expect(receipt!.querySelector('[data-testid="claim-execution-deadline"]')?.textContent).toContain('Asia/Shanghai')
    // 海报里明确了“不接受续期/服务端判定”
    expect(document.body.querySelector('[data-testid="claim-warning"]')?.textContent).toContain('不支持续期')

    const posted = stub.calls.find((call) => call.method === 'POST')
    expect(posted?.url).toBe('/api/web/v1/tasks/task-1/claim')
    expect(JSON.parse(posted!.bodyText!)).toEqual({ agent_id: 'agent-1', expected_version: 4 })
    wrapper.unmount()
  })
})

// ---------------------------------------------------------------------------
// 提交表单
// ---------------------------------------------------------------------------

describe('TaskSubmitForm（提交）', () => {
  it('摘要必填；服务端拒绝时保留输入并按同一幂等键重试，不伪造成功', async () => {
    stub.setDefault({
      status: 409,
      json: errEnvelope('SUBMISSION_EXISTS', '该执行已存在正式提交', {
        requestId: 'req-submit-conflict',
      }),
    })

    const wrapper = mount(TaskSubmitForm, {
      props: {
        task: taskFixture(),
        detail: detailFixture({ acceptance_criteria: '- 验收标准一\n- 验收标准二' }),
        nowMs: Date.parse('2026-10-15T00:00:00Z'),
      },
      attachTo: document.body,
    })
    await flushPromises()

    // 自检项由验收标准生成（服务端数据）
    const criteria = Array.from(
      document.body.querySelectorAll<HTMLInputElement>('[data-testid^="selfcheck-criterion-"]'),
    ).map((element) => element.value)
    expect(criteria).toEqual(['验收标准一', '验收标准二'])

    await new DOMWrapper(document.body.querySelector('[data-testid="submit-submit"]')!).trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[data-testid="submit-local-error"]')?.textContent).toContain('结果摘要')

    const summary = new DOMWrapper(document.body.querySelector('[data-testid="submit-summary"]')!)
    await summary.setValue('E2E 提交摘要')
    await new DOMWrapper(document.body.querySelector('[data-testid="submit-submit"]')!).trigger('click')
    await flushPromises()

    expect(document.body.querySelector('[data-testid="action-error-code"]')?.textContent?.trim()).toBe(
      'SUBMISSION_EXISTS',
    )
    // 输入保留，且没有出现成功回执
    expect((document.body.querySelector('[data-testid="submit-summary"]') as HTMLInputElement).value).toBe(
      'E2E 提交摘要',
    )
    expect(document.body.querySelector('[data-testid="submit-receipt"]')).toBeNull()

    // 重试：同一逻辑动作必须复用同一幂等键
    await new DOMWrapper(document.body.querySelector('[data-testid="submit-submit"]')!).trigger('click')
    await flushPromises()
    const posts = stub.calls.filter((call) => call.method === 'POST')
    expect(posts).toHaveLength(2)
    expect(posts[0]!.idempotencyKey).toBe(posts[1]!.idempotencyKey)
    expect(posts[0]!.bodyText).toBe(posts[1]!.bodyText)
    wrapper.unmount()
  })
})

// ---------------------------------------------------------------------------
// 页面状态（筛选与 Agent 目录）
// ---------------------------------------------------------------------------

describe('看板筛选状态与 Agent 目录', () => {
  it('筛选按项目保存，重置只影响该项目；签名随筛选变化', () => {
    const a = filtersForScope('project-a')
    expect(activeFilterCount(a)).toBe(0)
    setFiltersForScope('project-a', { difficulty: 5, keyword: '雨林' })
    expect(activeFilterCount(filtersForScope('project-a'))).toBe(2)
    expect(filtersForScope('project-b').difficulty).toBeNull()
    expect(filterSignature(filtersForScope('project-a'))).not.toBe(filterSignature(filtersForScope('project-b')))

    resetFiltersForScope('project-a')
    expect(activeFilterCount(filtersForScope('project-a'))).toBe(0)
  })

  it('Agent 目录按页取完（page_size 上限 100），候选按角色与项目过滤', async () => {
    const first = Array.from({ length: 100 }, (_, index) =>
      agentFixture(`agent-${index}`, {
        name: `agent-${index}`,
        role: index === 0 ? 'WORKER' : 'SUPERVISOR',
        project_ids: index < 2 ? ['project-1'] : ['project-2'],
      }),
    )
    stub.reply({ status: 200, json: pageEnvelope(first, { total: 102, page: 1, pageSize: 100 }) })
    stub.reply({
      status: 200,
      json: pageEnvelope(
        [agentFixture('agent-100', { name: '最新主管', project_ids: ['project-1'] }), agentFixture('agent-101')],
        { total: 102, page: 2, pageSize: 100 },
      ),
    })

    await ensureAgentDirectory(true)
    expect(agentDirectory.status).toBe('ready')
    expect(agentDirectory.items).toHaveLength(102)
    expect(stub.calls.map((call) => call.url)).toEqual([
      '/api/web/v1/agents?page=1&page_size=100',
      '/api/web/v1/agents?page=2&page_size=100',
    ])
    expect(agentName('agent-100')).toBe('最新主管')
    expect(agentName('missing-agent')).toContain('未知 Agent')

    const projectAgents = assignableAgents('project-1').map((agent) => agent.id)
    expect(projectAgents).toContain('agent-100')
    expect(projectAgents).not.toContain('agent-50')
    // 审核候选只保留主管，且可排除提交者
    const candidates = supervisorCandidates('project-1', 'agent-100').map((agent) => agent.id)
    expect(candidates).not.toContain('agent-100')
    expect(candidates).not.toContain('agent-0')
  })
})
