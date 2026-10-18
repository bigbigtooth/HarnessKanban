/**
 * 项目页面组件与视图单元测试（T30）。
 *
 * 覆盖：
 * - 生命周期按钮与归档原因门禁；
 * - 成员撤权：受影响任务列表 + WAIT/NOW 显式处置 + NOW 必须有原因；归档只读；
 * - 文档：同名冲突显式选择新版本/新文档、版本与上传者展示、归档只读；
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import type { FetchLike } from '@/api/client'
import { createFetchStub, errEnvelope, okEnvelope, type StubCall, type StubReply } from './helpers/fetchStub'

vi.stubGlobal('fetch', undefined as unknown as FetchLike)

const { default: ProjectLifecyclePanel } = await import('@/components/project/ProjectLifecyclePanel.vue')
const { default: ProjectMembersPanel } = await import('@/components/project/ProjectMembersPanel.vue')
const { default: ProjectDocumentsPanel } = await import('@/components/project/ProjectDocumentsPanel.vue')
const format = await import('@/components/project/project-format')

const stub = createFetchStub()

const PROJECT_ID = '22222222-2222-4222-8222-222222222222'
const DOC_ID = '33333333-3333-4333-8333-333333333333'
const AGENT_ID = '44444444-4444-4444-8444-444444444444'

function detail(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: PROJECT_ID,
    title: '示例项目',
    summary: '摘要',
    background: '背景文本',
    status: 'DRAFT',
    archived_from_status: null,
    content_version: 1,
    version: 2,
    current_context_version_id: 'ctx-1',
    current_context_version_number: 1,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-15T00:00:00Z',
    ...overrides,
  }
}

function documentItem(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: DOC_ID,
    project_id: PROJECT_ID,
    name: 'a.txt',
    is_archived: false,
    current_version_id: 'v1',
    current_version_number: 1,
    file_id: 'file-1',
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    current_version: {
      id: 'v1',
      document_id: DOC_ID,
      number: 1,
      file_id: 'file-1',
      original_name: 'a.txt',
      media_type: 'text/plain',
      size: 2048,
      uploaded_by_type: 'web_operator',
      uploaded_by_agent_id: null,
      created_at: '2026-10-01T00:00:00Z',
    },
    ...overrides,
  }
}

const members = [
  {
    id: 'm1',
    project_id: PROJECT_ID,
    agent_id: AGENT_ID,
    agent_name: 'worker-a',
    role: 'WORKER',
    enabled: true,
    online_state: 'ONLINE_IDLE',
    last_heartbeat_at: '2026-10-15T00:00:00Z',
    created_at: '2026-10-01T00:00:00Z',
  },
]

let wrappers: VueWrapper[] = []
afterEach(() => {
  for (const wrapper of wrappers) wrapper.unmount()
  wrappers = []
  document.body.innerHTML = ''
})

function track<T extends VueWrapper>(wrapper: T): T {
  wrappers.push(wrapper)
  return wrapper
}

function bodyEl<T extends HTMLElement>(testid: string): T {
  const element = document.body.querySelector<T>(`[data-testid="${testid}"]`)
  if (!element) throw new Error(`未找到元素：${testid}`)
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

beforeEach(() => {
  stub.reset()
  vi.stubGlobal('fetch', stub.fetch as unknown as FetchLike)
  document.body.innerHTML = ''
})

// ---------------------------------------------------------------------------

describe('project-format', () => {
  it('状态标签与只读判定', () => {
    expect(format.projectStatusLabel('DRAFT')).toBe('草稿')
    expect(format.projectStatusLabel('ACTIVE')).toBe('进行中')
    expect(format.projectStatusLabel('COMPLETED')).toBe('已结项')
    expect(format.projectStatusLabel('ARCHIVED')).toBe('已归档')
    expect(format.isProjectReadOnly({ status: 'ARCHIVED' })).toBe(true)
    expect(format.isProjectReadOnly({ status: 'ACTIVE' })).toBe(false)
  })

  it('按状态给出可执行的生命周期动作（归档仅归档项目提供恢复）', () => {
    expect(format.lifecycleActions({ status: 'DRAFT' }).map((item) => item.action)).toEqual(['activate', 'archive'])
    expect(format.lifecycleActions({ status: 'ACTIVE' }).map((item) => item.action)).toEqual(['complete', 'archive'])
    expect(format.lifecycleActions({ status: 'COMPLETED' }).map((item) => item.action)).toEqual(['activate', 'archive'])
    expect(format.lifecycleActions({ status: 'ARCHIVED' }).map((item) => item.action)).toEqual(['restore'])
  })

  it('文件大小与上传者展示', () => {
    expect(format.formatBytes(512)).toBe('512 B')
    expect(format.formatBytes(2048)).toBe('2.0 KB')
    expect(format.formatBytes(3 * 1024 * 1024)).toBe('3.00 MB')
    expect(
      format.uploaderLabel({
        id: 'v1',
        document_id: DOC_ID,
        number: 1,
        file_id: 'f',
        original_name: 'a',
        media_type: 'text/plain',
        size: 1,
        uploaded_by_type: 'web_operator',
        created_at: '2026-10-01T00:00:00Z',
      }),
    ).toBe('本地管理员（Web）')
  })
})

describe('ProjectLifecyclePanel', () => {
  it('归档必须填写原因，否则本地阻止', async () => {
    const wrapper = track(mount(ProjectLifecyclePanel, { props: { project: detail() as never } }))
    await wrapper.find('[data-testid="project-action-archive"]').trigger('click')
    await clickBody('project-archive-confirm')
    expect(wrapper.find('[data-testid="project-lifecycle-local-error"]').text()).toContain('必须填写原因')
    expect(wrapper.emitted('action')).toBeUndefined()

    await setBodyValue('project-archive-reason', '历史项目下线')
    await clickBody('project-archive-confirm')
    expect(wrapper.emitted('action')?.[0]?.[0]).toEqual({ action: 'archive', reason: '历史项目下线' })
  })

  it('归档状态只提供恢复入口并回显归档前状态', async () => {
    const wrapper = track(
      mount(ProjectLifecyclePanel, {
        props: { project: detail({ status: 'ARCHIVED', archived_from_status: 'ACTIVE' }) as never },
      }),
    )
    expect(wrapper.find('[data-testid="project-action-restore"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="project-action-archive"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="project-archived-from"]').text()).toContain('进行中')
  })

  it('服务端拒绝（未完成任务/最后主管）原样显示', async () => {
    const { ApiConflictError } = await import('@/api/client')
    const wrapper = track(
      mount(ProjectLifecyclePanel, {
        props: {
          project: detail({ status: 'ACTIVE' }) as never,
          error: new ApiConflictError({
            code: 'INVALID_TRANSITION',
            message: '存在未完成的任务，不能结项',
            httpStatus: 409,
          }),
        },
      }),
    )
    expect(wrapper.find('[data-testid="state-error-message"]').text()).toContain('不能结项')
  })
})

describe('ProjectMembersPanel', () => {
  function mountPanel(overrides: Record<string, unknown> = {}) {
    return track(
      mount(ProjectMembersPanel, {
        props: {
          projectId: PROJECT_ID,
          members: members as never,
          agents: [
            { id: AGENT_ID, name: 'worker-a', role: 'WORKER', enabled: true, max_concurrency: 2, reserved_execution_count: 0, version: 1 },
            { id: 'agent-2', name: 'worker-b', role: 'WORKER', enabled: true, max_concurrency: 2, reserved_execution_count: 0, version: 1 },
          ] as never,
          ...overrides,
        },
      }),
    )
  }

  it('移除成员时读取真实执行并列出受影响任务；NOW 必须有原因', async () => {
    stub.setDefault({
      status: 200,
      json: okEnvelope({
        id: AGENT_ID,
        capacity: { max_concurrency: 3, reserved_execution_count: 1, available_capacity: 2 },
        execution_summary: {
          open_count: 1,
          effective_count: 0,
          completed_count: 0,
          timeout_count: 0,
          rework_count: 0,
          total_count: 1,
          pending_reclaim_count: 1,
        },
        current_executions: [
          {
            execution_id: 'exec-1',
            task_id: 'task-1',
            project_id: PROJECT_ID,
            display_number: 'TASK-0007',
            difficulty: 2,
            claimed_at: '2026-10-14T00:00:00Z',
            execution_deadline_at: '2026-10-14T01:00:00Z',
            expired: true,
          },
          {
            execution_id: 'exec-2',
            task_id: 'task-2',
            project_id: 'other-project',
            display_number: 'TASK-0008',
            difficulty: 1,
            claimed_at: '2026-10-15T00:00:00Z',
            execution_deadline_at: '2026-10-15T01:00:00Z',
            expired: false,
          },
        ],
      }),
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.find(`[data-testid="project-member-remove-${AGENT_ID}"]`).trigger('click')
    await flushPromises()
    await flushPromises()

    const impact = bodyEl('project-member-remove-impact')
    expect(impact.textContent).toContain('TASK-0007')
    expect(impact.textContent).toContain('等待回收')
    // 只统计本项目内的执行
    expect(impact.textContent).not.toContain('TASK-0008')

    await checkBodyRadio('project-member-disposition-now')
    await clickBody('project-member-remove-confirm')
    expect(bodyEl('project-member-remove-local-error').textContent).toContain('必须填写原因')

    await setBodyValue('project-member-remove-reason', '撤权并回收')
    await clickBody('project-member-remove-confirm')
    expect(wrapper.emitted('remove')?.[0]?.[0]).toEqual({
      agentId: AGENT_ID,
      disposition: 'NOW',
      reason: '撤权并回收',
    })
  })

  it('没有未结束执行时 disposition 为 null（不需要额外处置）', async () => {
    stub.setDefault({
      status: 200,
      json: okEnvelope({
        id: AGENT_ID,
        capacity: { max_concurrency: 3, reserved_execution_count: 0, available_capacity: 3 },
        execution_summary: {
          open_count: 0,
          effective_count: 0,
          completed_count: 0,
          timeout_count: 0,
          rework_count: 0,
          total_count: 0,
          pending_reclaim_count: 0,
        },
        current_executions: [],
      }),
    })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.find(`[data-testid="project-member-remove-${AGENT_ID}"]`).trigger('click')
    await flushPromises()
    await flushPromises()
    expect(bodyEl('project-member-remove-impact-none').textContent).toContain('没有未结束执行')
    await clickBody('project-member-remove-confirm')
    expect(wrapper.emitted('remove')?.[0]?.[0]).toEqual({ agentId: AGENT_ID, disposition: null, reason: '' })
  })

  it('归档只读：不提供新增与移除入口，并提示恢复后可修改', async () => {
    const wrapper = mountPanel({ readOnly: true })
    await flushPromises()
    expect(wrapper.find('[data-testid="project-members-readonly"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="project-member-add"]').exists()).toBe(false)
    expect(wrapper.find(`[data-testid="project-member-remove-${AGENT_ID}"]`).attributes('disabled')).toBeDefined()
  })

  it('新增只在选择后提交，已授权成员不出现在可选项里', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find('[data-testid="project-member-add-select"]').text()).not.toContain('worker-a')
    expect(wrapper.find('[data-testid="project-member-add-select"]').text()).toContain('worker-b')

    await wrapper.find('[data-testid="project-member-add"]').trigger('click')
    expect(wrapper.find('[data-testid="project-member-add-error"]').text()).toContain('请先选择')

    await wrapper.find('[data-testid="project-member-add-select"]').setValue('agent-2')
    await wrapper.find('[data-testid="project-member-add"]').trigger('click')
    expect(wrapper.emitted('add')?.[0]?.[0]).toBe('agent-2')
  })
})

describe('ProjectDocumentsPanel', () => {
  function mountPanel(overrides: Record<string, unknown> = {}) {
    return track(
      mount(ProjectDocumentsPanel, {
        props: {
          projectId: PROJECT_ID,
          documents: [documentItem()] as never,
          ...overrides,
        },
      }),
    )
  }

  function attachFile(wrapper: VueWrapper, testid: string, file: File): Promise<void> {
    const input = wrapper.find(`[data-testid="${testid}"]`)
    const element = input.element as HTMLInputElement
    Object.defineProperty(element, 'files', { value: [file], configurable: true })
    return input.trigger('change')
  }

  it('列表展示版本/大小/上传者，并说明不支持在线编辑', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const meta = wrapper.find(`[data-testid="project-document-meta-${DOC_ID}"]`).text()
    expect(meta).toContain('v1')
    expect(meta).toContain('2.0 KB')
    expect(meta).toContain('本地管理员（Web）')
    expect(wrapper.find('[data-testid="project-documents-hint"]').text()).toContain('不提供在线编辑')
    expect(wrapper.find(`[data-testid="project-document-download-${DOC_ID}"]`).attributes('href')).toBe(
      '/api/web/v1/files/file-1/download',
    )
  })

  it('同名冲突：显式选择“上传为新版本”走版本端点（不静默覆盖）', async () => {
    stub.setDefault((call: StubCall): StubReply => {
      if (call.method === 'POST' && call.url.includes('/documents/') && call.url.includes('/versions')) {
        return {
          status: 201,
          json: okEnvelope(
            documentItem({
              current_version: {
                ...(documentItem().current_version as Record<string, unknown>),
                number: 2,
              },
            }),
          ),
        }
      }
      return { status: 409, json: errEnvelope('DOCUMENT_CONFLICT', '同名文档已存在：请显式选择上传新版本或新建同名文档') }
    })
    const wrapper = mountPanel()
    await flushPromises()

    await attachFile(wrapper, 'project-document-file', new File(['v2'], 'a.txt', { type: 'text/plain' }))
    await wrapper.find('[data-testid="project-document-upload"]').trigger('click')
    await flushPromises()
    expect(bodyEl('project-document-conflict-name').textContent).toBe('a.txt')

    await clickBody('project-document-conflict-new-version')
    const versionCall = stub.calls.find((call) => call.url.includes('/versions'))
    expect(versionCall?.url).toContain(`/documents/${DOC_ID}/versions`)
    expect(wrapper.find('[data-testid="project-documents-notice"]').text()).toContain('v2')
    expect(wrapper.emitted('changed')?.[0]?.[0]).toMatchObject({ message: expect.stringContaining('v2') })
  })

  it('同名冲突：显式选择“新建同名文档”时发送 allow_duplicate_name=true', async () => {
    stub.setDefault((call: StubCall): StubReply => {
      if (call.formData?.get('allow_duplicate_name') === 'true') {
        return { status: 201, json: okEnvelope(documentItem({ id: 'doc-2' })) }
      }
      return { status: 409, json: errEnvelope('DOCUMENT_CONFLICT', '同名文档已存在') }
    })
    const wrapper = mountPanel()
    await flushPromises()

    await attachFile(wrapper, 'project-document-file', new File(['again'], 'a.txt', { type: 'text/plain' }))
    await wrapper.find('[data-testid="project-document-upload"]').trigger('click')
    await flushPromises()
    await clickBody('project-document-conflict-new-document')

    const created = stub.calls.find((call) => call.formData?.get('allow_duplicate_name') === 'true')
    expect(created).toBeTruthy()
    expect(created!.url).toContain(`/projects/${PROJECT_ID}/documents`)
  })

  it('归档只读：隐藏上传与新版本入口，并提示恢复后可修改', async () => {
    const wrapper = mountPanel({ readOnly: true })
    await flushPromises()
    expect(wrapper.find('[data-testid="project-documents-readonly"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="project-document-upload"]').exists()).toBe(false)
    expect(wrapper.find(`[data-testid="project-document-new-version-${DOC_ID}"]`).exists()).toBe(false)
    expect(wrapper.find(`[data-testid="project-document-archive-${DOC_ID}"]`).exists()).toBe(false)
  })

  it('移出参考列表：先确认再调用 DELETE（不假装收集原因）', async () => {
    stub.setDefault({ status: 200, json: okEnvelope(documentItem({ is_archived: true })) })
    const wrapper = mountPanel()
    await flushPromises()
    expect(stub.calls.filter((call) => call.method === 'DELETE')).toHaveLength(0)

    await wrapper.find(`[data-testid="project-document-archive-${DOC_ID}"]`).trigger('click')
    await flushPromises()
    expect(bodyEl('project-document-archive-confirm-text').textContent).toContain('不会删除任何历史版本')

    await clickBody('project-document-archive-confirm')
    const deleteCall = stub.calls.find((call) => call.method === 'DELETE')
    expect(deleteCall?.url).toContain(`/documents/${DOC_ID}`)
    expect(wrapper.find('[data-testid="project-documents-notice"]').text()).toContain('已移出参考列表')
  })
})

// ---------------------------------------------------------------------------
// ProjectsView
// ---------------------------------------------------------------------------

