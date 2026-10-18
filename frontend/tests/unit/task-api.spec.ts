/**
 * 任务 WebAPI 封装单元测试（T31/T32）：只替换网络层（fetch 替身），
 * 客户端与幂等键逻辑保持真实。断言请求形状与错误透出，不 mock 业务规则。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiConflictError, ApiError, type FetchLike } from '@/api/client'
import {
  archiveTask,
  assignTask,
  claimTask,
  createTask,
  editTask,
  getTask,
  listTaskActivity,
  listTaskAttachments,
  listTasks,
  listAgents,
  publishTask,
  reclaimTask,
  releaseTask,
  reopenTask,
  reorderTask,
  restoreTask,
  reviewTask,
  submitTask,
  updateProgress,
  uploadStagingFile,
  uploadTaskAttachment,
  withdrawTask,
} from '@/api/tasks'
import { createFetchStub, deferred, errEnvelope, okEnvelope, pageEnvelope } from './helpers/fetchStub'

const stub = createFetchStub()

beforeEach(() => {
  stub.reset()
  vi.stubGlobal('fetch', stub.fetch as unknown as FetchLike)
})

function jsonBody<T = Record<string, unknown>>(callIndex = 0): T {
  const call = stub.calls[callIndex]
  expect(call?.bodyText).toBeTruthy()
  return JSON.parse(call!.bodyText!) as T
}

describe('任务列表查询', () => {
  it('把筛选条件按服务端参数名序列化，空关键词不发 q', async () => {
    stub.reply({ status: 200, json: pageEnvelope([]) })

    await listTasks({
      projectId: 'project-1',
      status: 'IN_REVIEW',
      difficulty: 4,
      claimMode: 'ASSIGNED',
      designatedAgentId: 'agent-9',
      assigneeAgentId: 'agent-8',
      keyword: '   ',
      includeArchived: true,
      page: 2,
      pageSize: 50,
    })

    const url = stub.calls[0]!.url
    expect(url).toContain('/api/web/v1/tasks?')
    expect(url).toContain('project_id=project-1')
    expect(url).toContain('status=IN_REVIEW')
    expect(url).toContain('difficulty=4')
    expect(url).toContain('claim_mode=ASSIGNED')
    expect(url).toContain('designated_agent_id=agent-9')
    expect(url).toContain('assignee_agent_id=agent-8')
    expect(url).toContain('include_archived=true')
    expect(url).toContain('page=2')
    expect(url).toContain('page_size=50')
    expect(url).not.toContain('q=')
    expect(stub.calls[0]!.method).toBe('GET')
  })

  it('详情/活动/附件/Agent 目录路径正确', async () => {
    stub.reply(
      { status: 200, json: okEnvelope({ id: 'task-1' }) },
      { status: 200, json: pageEnvelope([]) },
      { status: 200, json: pageEnvelope([]) },
      { status: 200, json: pageEnvelope([]) },
    )
    await getTask('task-1')
    await listTaskActivity('task-1', { pageSize: 50 })
    await listTaskAttachments('task-1', { page: 1, pageSize: 50 })
    await listAgents({ page: 2, pageSize: 100, enabled: true })
    expect(stub.calls.map((call) => call.url)).toEqual([
      '/api/web/v1/tasks/task-1',
      '/api/web/v1/tasks/task-1/activity?page_size=50',
      '/api/web/v1/tasks/task-1/attachments?page=1&page_size=50',
      '/api/web/v1/agents?enabled=true&page=2&page_size=100',
    ])
  })
})

describe('编制与状态动作', () => {
  it('创建只发 POST /tasks，不自动发布；OPEN 模式不带 designated_agent_id', async () => {
    stub.reply({ status: 200, json: okEnvelope({ id: 'task-1' }) })
    await createTask(
      {
        projectId: 'project-1',
        title: '新任务',
        description: '要求',
        acceptanceCriteria: '验收',
        difficulty: 3,
        claimMode: 'OPEN',
        designatedAgentId: 'agent-1',
      },
      'key-create-1',
    )
    expect(stub.calls).toHaveLength(1)
    expect(stub.calls[0]!.method).toBe('POST')
    expect(stub.calls[0]!.url).toBe('/api/web/v1/tasks')
    expect(stub.calls[0]!.idempotencyKey).toBe('key-create-1')
    const body = jsonBody()
    expect(body.claim_mode).toBe('OPEN')
    expect(body).not.toHaveProperty('designated_agent_id')
    expect(body).not.toHaveProperty('status')
  })

  it('ASSIGNED 创建必须带指定 Agent；due_at 以 UTC ISO 提交', async () => {
    stub.reply({ status: 200, json: okEnvelope({ id: 'task-1' }) })
    await createTask(
      {
        projectId: 'project-1',
        title: '指定任务',
        difficulty: 2,
        claimMode: 'ASSIGNED',
        designatedAgentId: 'agent-7',
        dueAt: '2026-10-16T02:00:00.000Z',
      },
      'key-create-2',
    )
    const body = jsonBody()
    expect(body.claim_mode).toBe('ASSIGNED')
    expect(body.designated_agent_id).toBe('agent-7')
    expect(body.due_at).toBe('2026-10-16T02:00:00.000Z')
  })

  it('PATCH 只提交变更字段与 expected_version；发布/撤回/改派/归档/恢复命中既定端点', async () => {
    stub.reply(
      { status: 200, json: okEnvelope({ id: 'task-1' }) },
      { status: 200, json: okEnvelope({ id: 'task-1' }) },
      { status: 200, json: okEnvelope({ id: 'task-1' }) },
      { status: 200, json: okEnvelope({ id: 'task-1' }) },
      { status: 200, json: okEnvelope({ id: 'task-1' }) },
      { status: 200, json: okEnvelope({ id: 'task-1' }) },
      { status: 200, json: okEnvelope({ id: 'task-1' }) },
    )
    await editTask('task-1', 5, { acceptanceCriteria: '验收改写' }, 'key-patch')
    await publishTask('task-1', 5, 'key-publish')
    await withdrawTask('task-1', 6, '要求需要重写', 'key-withdraw')
    await assignTask('task-1', 7, 'ASSIGNED', 'agent-3', 'key-assign')
    await archiveTask('task-1', 8, '不再需要', 'key-archive')
    await restoreTask('task-1', 9, 'key-restore')

    expect(stub.calls.map((call) => [call.method, call.url])).toEqual([
      ['PATCH', '/api/web/v1/tasks/task-1'],
      ['POST', '/api/web/v1/tasks/task-1/publish'],
      ['POST', '/api/web/v1/tasks/task-1/withdraw'],
      ['POST', '/api/web/v1/tasks/task-1/assign'],
      ['POST', '/api/web/v1/tasks/task-1/archive'],
      ['POST', '/api/web/v1/tasks/task-1/restore'],
    ])
    expect(jsonBody(0)).toEqual({ expected_version: 5, acceptance_criteria: '验收改写' })
    expect(jsonBody(1)).toEqual({ expected_version: 5 })
    expect(jsonBody(2)).toEqual({ expected_version: 6, reason: '要求需要重写' })
    expect(jsonBody(3)).toEqual({ expected_version: 7, claim_mode: 'ASSIGNED', designated_agent_id: 'agent-3' })
    expect(jsonBody(4)).toEqual({ expected_version: 8, reason: '不再需要' })
    expect(jsonBody(5)).toEqual({ expected_version: 9 })
  })
})

describe('执行、提交与审核载荷', () => {
  it('代领必须带 agent_id 与 expected_version；回收带 execution_id 与原因', async () => {
    stub.reply(
      { status: 200, json: okEnvelope({ execution_id: 'exec-1' }) },
      { status: 200, json: okEnvelope({ execution_id: 'exec-1' }) },
      { status: 200, json: okEnvelope({ execution_id: 'exec-1' }) },
      { status: 200, json: okEnvelope({ execution_id: 'exec-1' }) },
    )
    await claimTask('task-1', 'agent-2', 3, 'key-claim')
    await updateProgress('task-1', 'exec-1', '做到一半', 50, 'key-progress')
    await reclaimTask('task-1', 'exec-1', 4, '执行异常', 'key-reclaim')
    await releaseTask('task-1', 'exec-1', 4, '自己释放', 'key-release')

    expect(stub.calls[0]!.url).toBe('/api/web/v1/tasks/task-1/claim')
    expect(jsonBody(0)).toEqual({ agent_id: 'agent-2', expected_version: 3 })
    expect(jsonBody(1)).toEqual({ execution_id: 'exec-1', message: '做到一半', percent: 50 })
    expect(jsonBody(2)).toEqual({ execution_id: 'exec-1', expected_version: 4, reason: '执行异常' })
    expect(stub.calls[3]!.url).toBe('/api/web/v1/tasks/task-1/release')
  })

  it('正式提交包含 execution_id/expected_version/summary/content/self_check/artifacts/links', async () => {
    stub.reply({ status: 200, json: okEnvelope({ submission_id: 'sub-1' }) })
    await submitTask(
      'task-1',
      {
        executionId: 'exec-1',
        expectedVersion: 4,
        summary: '结果摘要',
        content: '正文',
        selfCheck: [{ criterion: '验收 A', passed: true, evidence: '日志' }],
        artifacts: ['file-1'],
        links: [{ title: 'PR', url: 'https://example.test/pr/1' }],
      },
      'key-submit',
    )
    expect(stub.calls[0]!.url).toBe('/api/web/v1/tasks/task-1/submit')
    expect(jsonBody()).toEqual({
      execution_id: 'exec-1',
      expected_version: 4,
      summary: '结果摘要',
      content: '正文',
      self_check: [{ criterion: '验收 A', passed: true, evidence: '日志' }],
      artifacts: ['file-1'],
      links: [{ title: 'PR', url: 'https://example.test/pr/1' }],
    })
  })

  it('审核必须显式给出 submission_id 与 reviewer_agent_id', async () => {
    stub.reply(
      { status: 200, json: okEnvelope({ status: 'DONE' }) },
      { status: 200, json: okEnvelope({ status: 'TODO' }) },
    )
    await reviewTask(
      'task-1',
      {
        submissionId: 'sub-1',
        reviewerAgentId: 'supervisor-1',
        decision: 'APPROVE',
        comment: '通过',
        expectedVersion: 6,
      },
      'key-approve',
    )
    await reopenTask('task-1', 7, '需要返工', 'key-reopen')
    expect(jsonBody(0)).toEqual({
      submission_id: 'sub-1',
      reviewer_agent_id: 'supervisor-1',
      decision: 'APPROVE',
      comment: '通过',
      expected_version: 6,
    })
    expect(stub.calls[1]!.url).toBe('/api/web/v1/tasks/task-1/reopen')
    expect(jsonBody(1)).toEqual({ expected_version: 7, reason: '需要返工' })
  })

  it('列内排序只发排序端点，带 status 断言与单锚点，不写 status', async () => {
    stub.reply({ status: 200, json: okEnvelope({ sort_order: '1500', version: 9 }) })
    await reorderTask(
      'project-1',
      { taskId: 'task-1', expectedVersion: 8, status: 'TODO', beforeTaskId: 'task-2' },
      'key-order',
    )
    expect(stub.calls[0]!.url).toBe('/api/web/v1/projects/project-1/task-order')
    expect(jsonBody()).toEqual({
      task_id: 'task-1',
      expected_version: 8,
      status: 'TODO',
      before_task_id: 'task-2',
    })
  })

  it('产物走 multipart，带 purpose 与 execution_id', async () => {
    stub.reply({ status: 201, json: okEnvelope({ file_id: 'file-1' }) })
    const file = new File(['内容'], 'artifact.txt', { type: 'text/plain' })
    await uploadTaskAttachment('task-1', { file, purpose: 'EXECUTION_ARTIFACT', executionId: 'exec-1' }, 'key-file')

    expect(stub.calls[0]!.url).toBe('/api/web/v1/tasks/task-1/attachments')
    expect(stub.calls[0]!.idempotencyKey).toBe('key-file')
    const form = stub.calls[0]!.formData
    expect(form).not.toBeNull()
    expect(form!.get('purpose')).toBe('EXECUTION_ARTIFACT')
    expect(form!.get('execution_id')).toBe('exec-1')
    expect((form!.get('file') as File).name).toBe('artifact.txt')
  })
})

describe('幂等键与错误透出', () => {
  it('同一逻辑动作重试必须复用同一个键（客户端不会另造键）', async () => {
    stub.reply(
      { status: 503, json: errEnvelope('DATABASE_BUSY', '数据库繁忙') },
      { status: 200, json: okEnvelope({ submission_id: 'sub-1' }) },
    )
    const key = 'key-reuse-1'
    const payload = {
      executionId: 'exec-1',
      expectedVersion: 4,
      summary: '摘要',
    }
    await submitTask('task-1', payload, key).catch(() => undefined)
    await submitTask('task-1', payload, key)
    expect(stub.calls[0]!.idempotencyKey).toBe(key)
    expect(stub.calls[1]!.idempotencyKey).toBe(key)
    expect(stub.calls[0]!.bodyText).toBe(stub.calls[1]!.bodyText)
  })

  it('409 以 ApiConflictError 抛出并保留错误码/请求号（页面据此保留输入并回显原因）', async () => {
    stub.reply({
      status: 409,
      json: errEnvelope('INVALID_TRANSITION', '任务已发布，实质要求只能先撤回到准备中再修改', {
        requestId: 'req-conflict',
        details: { status: 'TODO', fields: ['acceptance_criteria'] },
      }),
    })
    const error = await editTask('task-1', 3, { acceptanceCriteria: '新验收' }, 'key-conflict').catch(
      (thrown: unknown) => thrown,
    )
    expect(error).toBeInstanceOf(ApiConflictError)
    const apiError = error as ApiError
    expect(apiError.code).toBe('INVALID_TRANSITION')
    expect(apiError.message).toContain('撤回到准备中')
    expect(apiError.requestId).toBe('req-conflict')
    expect(apiError.isConflict).toBe(true)
  })

  it('详情读取失败不会被吞成空数据', async () => {
    const gate = deferred<void>()
    stub.reply({ status: 404, json: errEnvelope('RESOURCE_NOT_FOUND', '任务不存在或当前身份无权访问'), gate: gate.promise })
    const pending = getTask('task-missing').catch((thrown: unknown) => thrown)
    gate.resolve()
    const error = await pending
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('RESOURCE_NOT_FOUND')
  })
})

describe('新建任务的暂存附件（OSS）', () => {
  it('uploadStagingFile 发 multipart POST /files 并携带 project_id 与幂等键', async () => {
    stub.reply({
      status: 201,
      json: okEnvelope({ file_id: 'file-1', oss_url: 'https://bucket.endpoint/key?sig=1', original_name: 'a.md' }),
    })
    const file = new File(['# spec\n'], '规格.md', { type: 'text/markdown' })
    const staged = await uploadStagingFile('project-1', file, 'key-upload-1')
    expect(staged.file_id).toBe('file-1')
    expect(staged.oss_url).toContain('https://bucket.endpoint/')
    const call = stub.calls[0]!
    expect(call.method).toBe('POST')
    expect(call.url).toBe('/api/web/v1/files')
    expect(call.idempotencyKey).toBe('key-upload-1')
    expect(call.formData).not.toBeNull()
    expect(call.formData!.get('project_id')).toBe('project-1')
    expect(call.formData!.get('file')).toBeInstanceOf(File)
  })

  it('createTask 带 attachmentFileIds 时序列化为 attachment_file_ids；为空时不发送', async () => {
    stub.reply(
      { status: 200, json: okEnvelope({ id: 'task-9' }) },
      { status: 200, json: okEnvelope({ id: 'task-10' }) },
    )
    await createTask(
      {
        projectId: 'project-1',
        title: '带附件',
        difficulty: 2,
        claimMode: 'OPEN',
        attachmentFileIds: ['file-1', 'file-2'],
      },
      'key-a',
    )
    expect(jsonBody(0).attachment_file_ids).toEqual(['file-1', 'file-2'])

    await createTask(
      { projectId: 'project-1', title: '无附件', difficulty: 2, claimMode: 'OPEN' },
      'key-b',
    )
    expect(jsonBody(1)).not.toHaveProperty('attachment_file_ids')
  })
})
