/**
 * 首页组件单元测试：任务看板（筛选/卡片/空列）、任务详情浮窗、项目网格与设置浮窗。
 *
 * 网络层用 fetch 替身（`helpers/fetchStub`）；store 状态直接铺好，避免与取数顺序耦合。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import type { FetchLike } from '@/api/client'
import { createFetchStub, errEnvelope, okEnvelope, pageEnvelope } from './helpers/fetchStub'

import HomeBoard from '@/components/home/HomeBoard.vue'
import HomeProjects from '@/components/home/HomeProjects.vue'
import HomeTaskCard from '@/components/home/HomeTaskCard.vue'
import SettingsDialog from '@/components/home/SettingsDialog.vue'
import TaskDetailModal from '@/components/home/TaskDetailModal.vue'
import {
  agentList,
  taskList,
  projectList,
  homeFilters,
  resetHomeState,
  projectTaskIndex,
  dashboardState,
} from '@/stores/home'
import { hideToast } from '@/components/home/feedback'

const stub = createFetchStub()

/** 浮窗会 Teleport 到 body，断言用 document 查询（沿用既有组件测试的做法）。 */
function bodyEl<T extends Element = HTMLElement>(selector: string): T {
  const element = document.body.querySelector<T>(selector)
  if (!element) throw new Error(`未在 body 中找到 ${selector}`)
  return element
}

function sheetText(): string {
  return document.body.querySelector('.modal-layer .sheet')?.textContent ?? ''
}

function makeTask(overrides: Record<string, unknown> = {}) {
  return {
    id: 't-1',
    project_id: 'p-1',
    number: 1,
    display_number: 'TASK-0001',
    title: '写一份验收说明',
    description: '把流程写清楚',
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

function seedStore() {
  taskList.items = [
    makeTask({ id: 't-1', status: 'TODO' }),
    makeTask({ id: 't-2', status: 'IN_PROGRESS', assignee_agent_id: 'a-1', claim_mode: 'ASSIGNED', designated_agent_id: 'a-1' }),
    makeTask({ id: 't-3', status: 'DONE', number: 3 }),
  ] as never
  taskList.total = 3
  taskList.status = 'ready'
  projectList.items = [
    { id: 'p-1', title: 'wwg', summary: '示例项目', status: 'ACTIVE', version: 1, content_version: 1 },
  ] as never
  projectList.status = 'ready'
  agentList.items = [
    { id: 'a-1', name: '小七', role: 'WORKER', enabled: true, max_concurrency: 1, reserved_execution_count: 0, version: 1, online_state: 'ONLINE_IDLE' },
  ] as never
  agentList.status = 'ready'
  dashboardState.data = {
    projects: [{ project_id: 'p-1', title: 'wwg', status: 'ACTIVE', task_total: 3, completion_rate: 0.33, pending_review: 1, overdue_task_total: 0 }],
  } as never
  projectTaskIndex.workers = new Map([['p-1', ['a-1']]])
}

beforeEach(() => {
  stub.reset()
  resetHomeState()
  hideToast()
  vi.stubGlobal('fetch', stub.fetch as unknown as FetchLike)
})

afterEach(() => {
  vi.unstubAllGlobals()
  resetHomeState()
  document.body.innerHTML = ''
})

describe('HomeBoard（任务模块）', () => {
  it('五列各自渲染任务数量，默认不带项目筛选', () => {
    seedStore()
    const wrapper = mount(HomeBoard)
    const columns = wrapper.findAll('.column')
    expect(columns).toHaveLength(5)
    expect(columns.map((column) => column.find('.col-name h2').text())).toEqual([
      '准备中',
      '待办',
      '进行中',
      '待验收',
      '已完成',
    ])
    expect(wrapper.find('[data-testid="column-count-TODO"]').text()).toBe('1')
    expect(wrapper.find('[data-testid="column-count-DONE"]').text()).toBe('1')
    expect(homeFilters.projectId).toBe('')
  })

  it('选择项目后带上 project_id 重新取数（默认全部 → 指定项目）', async () => {
    seedStore()
    stub.setDefault({ json: pageEnvelope([]) })
    const wrapper = mount(HomeBoard)
    await wrapper.find('[data-testid="filter-project"]').setValue('p-1')
    await flushPromises()
    expect(stub.calls.some((call) => call.url.includes('project_id=p-1'))).toBe(true)
    expect(wrapper.find('.board-ident strong').text()).toBe('wwg')
  })

  it('空列给出提示而不是留白；点击卡片发出 openTask', async () => {
    seedStore()
    const wrapper = mount(HomeBoard)
    // 准备中与待验收两列没有卡片：空列必须给出提示，而不是留白
    expect(wrapper.findAll('.empty-col').length).toBe(2)
    await wrapper.find('article.task-card').trigger('click')
    expect(wrapper.emitted('openTask')?.[0]?.[0]).toMatchObject({ id: 't-1' })
  })

  it('列头「+」按钮发出 createTask 并带上列状态', async () => {
    seedStore()
    const wrapper = mount(HomeBoard)
    await wrapper.findAll('.col-add')[1]!.trigger('click')
    expect(wrapper.emitted('createTask')?.[0]).toEqual(['TODO'])
  })
})

describe('HomeTaskCard', () => {
  it('展示编号 / 标签 / 难度 / 项目 / 领取模式，已完成不显示逾期告警', () => {
    const wrapper = mount(HomeTaskCard, {
      props: { task: makeTask({ status: 'DONE', due_at: '2020-01-01T00:00:00Z' }) as never, index: 0 },
    })
    expect(wrapper.text()).toContain('TASK-0001')
    expect(wrapper.text()).toContain('前端')
    expect(wrapper.text()).toContain('L2')
    // 已完成：截止信息不再带逾期告警，右侧是完成徽标
    expect(wrapper.find('.meta-due').classes()).not.toContain('is-overdue')
    expect(wrapper.find('.meta-due').text()).toBe('已完成')
    expect(wrapper.find('.done-badge').exists()).toBe(true)
  })

  it('有执行者时显示头像，未指派时给出占位而不是假装有人负责', () => {
    const wrapper = mount(HomeTaskCard, {
      props: {
        task: makeTask() as never,
        index: 0,
        owner: { name: '小七', avatar: { emoji: '🐰', color: '#fff', label: '小', title: '小七' } },
      },
    })
    expect(wrapper.find('.card-owner').attributes('title')).toBe('小七')
    const noOwner = mount(HomeTaskCard, { props: { task: makeTask() as never, index: 0 } })
    expect(noOwner.find('.card-owner--empty').exists()).toBe(true)
  })
})

describe('TaskDetailModal（任务详情浮窗）', () => {
  it('并行拉取任务/活动/附件，展示要求、验收标准与可用动作', async () => {
    stub.reply(
      {
        json: okEnvelope({
          ...makeTask({ status: 'IN_REVIEW' }),
          acceptance_criteria: '验收：能复现',
          current_submission: { submission_id: 's-1', summary: '已提交', content: '内容', self_check: [], submitted_at: '2026-10-01T00:00:00Z' },
          review_history: [],
        }),
      },
      { json: pageEnvelope([{ id: 'e-1', action: 'task.submitted', actor_type: 'agent', created_at: '2026-10-01T00:00:00Z' }]) },
      { json: pageEnvelope([{ id: 'f-1', file_id: 'file-1', original_name: 'deliverable.md', media_type: 'text/markdown', size: 2048, purpose: 'EXECUTION_ARTIFACT', created_at: '2026-10-01T00:00:00Z' }]) },
    )
    const wrapper = mount(TaskDetailModal, {
      props: { task: makeTask({ status: 'IN_REVIEW' }) as never },
      attachTo: document.body,
    })
    await flushPromises()
    await flushPromises()
    await flushPromises()

    const paths = stub.calls.map((call) => call.url.split('?')[0])
    expect(paths).toEqual(['/api/web/v1/tasks/t-1', '/api/web/v1/tasks/t-1/activity', '/api/web/v1/tasks/t-1/attachments'])
    const text = sheetText()
    expect(text).toContain('验收：能复现')
    expect(text).toContain('deliverable.md')
    expect(text).toContain('待验收')

    // 「编辑」有独立按钮，动作区不重复出现
    const actionLabels = Array.from(document.body.querySelectorAll('.task-form-footer .btn')).map((btn) => btn.textContent?.trim())
    expect(actionLabels.filter((label) => label === '编辑要求')).toHaveLength(1)
    expect(actionLabels).toContain('审核')
    wrapper.unmount()
  })

  it('服务端失败时显示错误与请求号，不假装详情为空', async () => {
    stub.reply({ status: 404, json: errEnvelope('RESOURCE_NOT_FOUND', '任务不存在', { requestId: 'req-404' }) })
    const wrapper = mount(TaskDetailModal, {
      props: { task: makeTask() as never },
      attachTo: document.body,
    })
    await flushPromises()
    await flushPromises()
    expect(bodyEl('[data-testid="task-detail-error"]').textContent).toContain('任务不存在')
    wrapper.unmount()
  })
})

describe('HomeProjects（项目模块）', () => {
  function seedProjects() {
    seedStore()
    projectList.items = [
      { id: 'p-1', title: '进行中项目', summary: '薄荷色', status: 'ACTIVE', version: 1, content_version: 1 },
      { id: 'p-2', title: '结项项目', summary: '', status: 'COMPLETED', version: 1, content_version: 1 },
      { id: 'p-3', title: '删除的项目', summary: '', status: 'ARCHIVED', version: 1, content_version: 1, deleted_at: '2026-10-01T00:00:00Z' },
    ] as never
  }

  it('状态胶囊带真实数量，点击后只显示对应状态的项目', async () => {
    seedProjects()
    const wrapper = mount(HomeProjects)
    const chips = wrapper.findAll('[data-project-filter]').map((chip) => chip.text().replace(/\s+/g, ' '))
    expect(chips.slice(0, 3)).toEqual(['全部 2', '草稿 0', '进行中 1'])
    // 默认「全部」不显示已删除项目
    expect(wrapper.findAll('article.project-card')).toHaveLength(2)

    await wrapper.find('[data-project-filter="COMPLETED"]').trigger('click')
    expect(wrapper.findAll('article.project-card h3').map((el) => el.text())).toEqual(['结项项目'])

    await wrapper.find('[data-project-filter="deleted"]').trigger('click')
    expect(wrapper.findAll('article.project-card h3').map((el) => el.text())).toEqual(['删除的项目'])
  })

  it('搜索同时匹配标题与简介，命中为空时给出清空入口', async () => {
    seedProjects()
    const wrapper = mount(HomeProjects)
    await wrapper.find('[data-testid="project-search"]').setValue('薄荷')
    expect(wrapper.findAll('article.project-card')).toHaveLength(1)
    await wrapper.find('[data-testid="project-search"]').setValue('不存在的项目')
    expect(wrapper.findAll('article.project-card')).toHaveLength(0)
    expect(wrapper.find('.empty-projects').exists()).toBe(true)
    expect(wrapper.text()).toContain('清空筛选')
  })

  it('新建入口既在页头也在网格尾部，点击都会请求打开表单', async () => {
    seedProjects()
    const wrapper = mount(HomeProjects)
    await wrapper.find('[data-testid="new-project"]').trigger('click')
    await wrapper.find('[data-testid="new-project-tile"]').trigger('click')
    expect(wrapper.emitted('createProject')).toHaveLength(2)
  })

  it('项目卡片上的执行者来自独立索引，不受任务模块筛选影响', () => {
    seedProjects()
    projectTaskIndex.workers = new Map([['p-1', ['a-1']]])
    const wrapper = mount(HomeProjects)
    expect(wrapper.findAll('article.project-card')[0]!.findAll('.avatar').length).toBe(1)
  })
})

describe('SettingsDialog（设置浮窗）', () => {
  const settingsPayload = {
    llm: { base_url: 'https://api.example.com/v1', model: 'demo-model', temperature: 0.5, max_tokens: 1024, system_prompt: '', api_key_set: true },
    env: [
      { key: 'LOG_LEVEL', group: '基础', description: '日志级别', secret: false, source: 'environment', value: 'INFO', has_value: true },
      { key: 'MYSQL_PASSWORD', group: '数据库', description: 'MySQL 密码', secret: true, source: 'unset', value: null, has_value: false },
    ],
    env_groups: ['基础', '数据库', '自定义'],
    updated_at: '2026-10-17T18:45:36Z',
    settings_file: '/tmp/runtime_settings.json',
    notes: '',
  }

  function mountSettings() {
    return mount(SettingsDialog, { props: { open: true }, attachTo: document.body })
  }

  it('默认打开 LLM 页：回显已保存配置，密文只显示“已设置”且留空保持', async () => {
    stub.reply({ json: okEnvelope(settingsPayload) })
    const wrapper = mountSettings()
    await flushPromises()
    expect((bodyEl<HTMLInputElement>('[data-testid="llm-base-url"]')).value).toBe('https://api.example.com/v1')
    const apiKey = bodyEl<HTMLInputElement>('[data-testid="llm-api-key"]')
    expect(apiKey.value).toBe('')
    expect(apiKey.placeholder).toContain('留空保持')
    wrapper.unmount()
  })

  it('保存只提交被改动的字段：没填 api_key 就不带上它', async () => {
    stub.reply({ json: okEnvelope(settingsPayload) }, { json: okEnvelope({ ...settingsPayload, updated_at: '2026-10-18T00:00:00Z' }) })
    const wrapper = mountSettings()
    await flushPromises()
    await new DOMWrapper(bodyEl('[data-testid="llm-model"]')).setValue('qwen-max')
    await new DOMWrapper(bodyEl('[data-testid="settings-save"]')).trigger('click')
    await flushPromises()
    const body = JSON.parse(stub.calls[1]!.bodyText!)
    expect(body.llm.model).toBe('qwen-max')
    expect('api_key' in body.llm).toBe(false)
    expect(body.env).toEqual({})
    expect(stub.calls[1]!.idempotencyKey).toBeTruthy()
    expect(bodyEl('[data-testid="settings-notice"]').textContent).toContain('已保存')
    wrapper.unmount()
  })

  it('环境变量页按分组展示来源；勾选清除后提交 null', async () => {
    stub.reply({ json: okEnvelope(settingsPayload) }, { json: okEnvelope(settingsPayload) })
    const wrapper = mountSettings()
    await flushPromises()
    await new DOMWrapper(bodyEl('[data-testid="settings-tab-env"]')).trigger('click')
    await flushPromises()
    const groups = Array.from(document.body.querySelectorAll('.settings-card-head strong')).map((el) => el.textContent)
    expect(groups).toContain('基础')
    expect(sheetText()).toContain('环境变量')
    expect(sheetText()).toContain('未设置')

    await new DOMWrapper(bodyEl('[data-testid="env-clear-LOG_LEVEL"]')).setValue(true)
    await new DOMWrapper(bodyEl('[data-testid="settings-save"]')).trigger('click')
    await flushPromises()
    const body = JSON.parse(stub.calls[1]!.bodyText!)
    expect(body.env).toEqual({ LOG_LEVEL: null })
    wrapper.unmount()
  })

  it('测试连接把不可达结果原样展示（不抛异常、不谎报成功）', async () => {
    stub.reply(
      { json: okEnvelope(settingsPayload) },
      { json: okEnvelope({ ok: false, message: '无法连接 https://api.example.com/v1/models（ConnectError）', url: '', status_code: null, latency_ms: 12, model_count: null, model_found: null, models: [] }) },
    )
    const wrapper = mountSettings()
    await flushPromises()
    await new DOMWrapper(bodyEl('[data-testid="llm-test"]')).trigger('click')
    await flushPromises()
    await flushPromises()
    expect(bodyEl('[data-testid="llm-probe-result"]').textContent).toContain('无法连接')
    wrapper.unmount()
  })
})
