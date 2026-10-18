/**
 * 项目域 WebAPI 封装单元测试（T30）。
 *
 * 只替换网络层：断言请求路径、白名单字段、multipart 内容与幂等键复用，
 * 以及“背景必须单独请求”“同名文档不被静默覆盖”的接口形状。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiConflictError, type FetchLike } from '@/api/client'
import {
  activateProject,
  addMember,
  archiveDocument,
  archiveProject,
  completeProject,
  createProject,
  fileDownloadUrl,
  getDocument,
  getProject,
  listMembers,
  listProjectDocuments,
  listProjectsPage,
  removeMember,
  restoreProject,
  updateProjectBackground,
  updateProjectMetadata,
  uploadDocumentVersion,
  uploadProjectDocument,
} from '@/api/projects'
import { createFetchStub, errEnvelope, okEnvelope, pageEnvelope } from './helpers/fetchStub'

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

const PROJECT_ID = '22222222-2222-4222-8222-222222222222'
const DOC_ID = '33333333-3333-4333-8333-333333333333'
const AGENT_ID = '44444444-4444-4444-8444-444444444444'

function projectDetail(overrides: Record<string, unknown> = {}): Record<string, unknown> {
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

describe('项目列表与详情', () => {
  it('列表参数：状态、包含归档与分页', async () => {
    stub.reply({ status: 200, json: pageEnvelope([]) })
    await listProjectsPage({ status: 'ACTIVE', includeArchived: false, page: 2, pageSize: 10 })
    const url = stub.calls[0]!.url
    expect(url).toContain('status=ACTIVE')
    expect(url).toContain('include_archived=false')
    expect(url).toContain('page=2')
    expect(url).toContain('page_size=10')
  })

  it('详情返回背景与当前上下文版本指针', async () => {
    stub.reply({ status: 200, json: okEnvelope(projectDetail()) })
    const project = await getProject(PROJECT_ID)
    expect(project.background).toBe('背景文本')
    expect(project.current_context_version_number).toBe(1)
    expect(stub.calls[0]!.method).toBe('GET')
  })

  it('创建项目不发送 status/version 等只读字段，创建结果由服务端决定为草稿', async () => {
    stub.reply({
      status: 201,
      json: okEnvelope({ ...projectDetail({ title: '新项目', status: 'DRAFT' }), idempotent_replay: false }),
    })
    const created = await createProject({ title: '新项目', summary: '摘要' }, 'k1')
    expect(jsonBody()).toEqual({ title: '新项目', summary: '摘要' })
    expect(created.status).toBe('DRAFT')
    expect(stub.calls[0]!.idempotencyKey).toBe('k1')
  })
})

describe('元数据与背景必须分开提交', () => {
  it('元数据 PATCH 只含 title/summary/expected_version，绝不带 background', async () => {
    stub.reply({ status: 200, json: okEnvelope(projectDetail({ title: '改名' })) })
    await updateProjectMetadata(PROJECT_ID, 2, { title: '改名' }, 'k2')
    const body = jsonBody()
    expect(body).toEqual({ expected_version: 2, title: '改名' })
    expect(body).not.toHaveProperty('background')
  })

  it('背景 PATCH 是独立请求，只含 background/expected_version，并返回新上下文版本', async () => {
    stub.reply({
      status: 200,
      json: okEnvelope({
        ...projectDetail({ background: '新背景', content_version: 2, version: 3 }),
        current_context_version: {
          id: 'ctx-2',
          project_id: PROJECT_ID,
          number: 2,
          background_snapshot: '新背景',
          document_version_refs: [],
          created_at: '2026-10-15T01:00:00Z',
        },
      }),
    })
    const result = await updateProjectBackground(PROJECT_ID, '新背景', 2, 'k3')
    const body = jsonBody()
    expect(body).toEqual({ background: '新背景', expected_version: 2 })
    expect(body).not.toHaveProperty('title')
    expect(result.current_context_version?.number).toBe(2)
    expect(result.content_version).toBe(2)
  })
})

describe('生命周期动作', () => {
  it('四个动作各自调用独立端点并携带 expected_version', async () => {
    stub.reply({ status: 200, json: okEnvelope(projectDetail({ status: 'ACTIVE', version: 3 })) })
    stub.reply({ status: 200, json: okEnvelope(projectDetail({ status: 'COMPLETED', version: 4 })) })
    stub.reply({ status: 200, json: okEnvelope(projectDetail({ status: 'ARCHIVED', version: 5 })) })
    stub.reply({ status: 200, json: okEnvelope(projectDetail({ status: 'DRAFT', version: 6 })) })

    await activateProject(PROJECT_ID, 2, 'k4')
    await completeProject(PROJECT_ID, 3, 'k5')
    await archiveProject(PROJECT_ID, 4, '归档原因', 'k6')
    await restoreProject(PROJECT_ID, 5, 'k7')

    expect(jsonBody(0)).toEqual({ expected_version: 2 })
    expect(jsonBody(1)).toEqual({ expected_version: 3 })
    expect(jsonBody(2)).toEqual({ expected_version: 4, reason: '归档原因' })
    expect(jsonBody(3)).toEqual({ expected_version: 5 })
    expect(stub.calls[0]!.url).toContain(`/projects/${PROJECT_ID}/activate`)
    expect(stub.calls[1]!.url).toContain(`/projects/${PROJECT_ID}/complete`)
    expect(stub.calls[2]!.url).toContain(`/projects/${PROJECT_ID}/archive`)
    expect(stub.calls[3]!.url).toContain(`/projects/${PROJECT_ID}/restore`)
  })

  it('服务端拒绝（最后主管）原样抛出，且不伪装成功', async () => {
    stub.reply({
      status: 409,
      json: errEnvelope('LAST_SUPERVISOR_REQUIRED', '启用项目前必须至少指定一名已启用主管'),
    })
    const error = await activateProject(PROJECT_ID, 1, 'k8').catch((thrown) => thrown)
    expect(error).toBeInstanceOf(ApiConflictError)
    expect((error as ApiConflictError).code).toBe('LAST_SUPERVISOR_REQUIRED')
  })
})

describe('成员授权与撤权', () => {
  it('成员列表分页参数', async () => {
    stub.reply({ status: 200, json: pageEnvelope([]) })
    await listMembers(PROJECT_ID, { pageSize: 50 })
    expect(stub.calls[0]!.url).toContain(`/projects/${PROJECT_ID}/members?`)
    expect(stub.calls[0]!.url).toContain('page_size=50')
  })

  it('新增成员使用 PUT 且带幂等键', async () => {
    stub.reply({ status: 201, json: okEnvelope({ id: 'm1', agent_id: AGENT_ID }) })
    await addMember(PROJECT_ID, AGENT_ID, 'k9')
    expect(stub.calls[0]!.method).toBe('PUT')
    expect(stub.calls[0]!.url).toContain(`/projects/${PROJECT_ID}/members/${AGENT_ID}`)
    expect(stub.calls[0]!.idempotencyKey).toBe('k9')
  })

  it('撤权携带 disposition 与 reason，并返回回收的执行', async () => {
    stub.reply({
      status: 200,
      json: okEnvelope({
        membership_id: 'm1',
        project_id: PROJECT_ID,
        agent_id: AGENT_ID,
        role: 'WORKER',
        disposition: 'NOW',
        reason: '违规',
        reclaimed_execution_ids: ['exec-1'],
      }),
    })
    const result = await removeMember(PROJECT_ID, AGENT_ID, { disposition: 'NOW', reason: '违规' }, 'k10')
    expect(stub.calls[0]!.method).toBe('DELETE')
    expect(jsonBody()).toEqual({ disposition: 'NOW', reason: '违规' })
    expect(result.reclaimed_execution_ids).toEqual(['exec-1'])
  })

  it('未持有执行时可省略 disposition（仅发送 reason）', async () => {
    stub.reply({ status: 200, json: okEnvelope({ disposition: 'WAIT', reclaimed_execution_ids: [] }) })
    await removeMember(PROJECT_ID, AGENT_ID, { reason: '' }, 'k11')
    expect(jsonBody()).toEqual({ reason: '' })
  })

  it('缺少处置时服务端 400 原样抛出', async () => {
    stub.reply({
      status: 400,
      json: errEnvelope('INVALID_ARGUMENT', '该成员在本项目持有有效执行，必须明确选择等待到期(WAIT)或立即回收(NOW)'),
    })
    const error = await removeMember(PROJECT_ID, AGENT_ID, {}, 'k12').catch((thrown) => thrown)
    expect(error).toMatchObject({ code: 'INVALID_ARGUMENT', httpStatus: 400 })
  })
})

describe('文档', () => {
  it('上传新文档使用 multipart，字段为 file/name，缺省不发送 allow_duplicate_name', async () => {
    stub.reply({ status: 201, json: okEnvelope({ id: DOC_ID, name: 'a.txt' }) })
    const file = new File(['hello'], 'a.txt', { type: 'text/plain' })
    await uploadProjectDocument(PROJECT_ID, { file, name: 'a.txt' }, 'k13')

    const call = stub.calls[0]!
    expect(call.method).toBe('POST')
    expect(call.url).toContain(`/projects/${PROJECT_ID}/documents`)
    expect(call.idempotencyKey).toBe('k13')
    expect(call.formData?.get('name')).toBe('a.txt')
    expect(call.formData?.get('allow_duplicate_name')).toBeNull()
    expect(call.formData?.get('file')).toBeInstanceOf(File)
  })

  it('同名冲突：显式允许同名时发送 allow_duplicate_name=true', async () => {
    stub.reply({ status: 409, json: errEnvelope('DOCUMENT_CONFLICT', '同名文档已存在') })
    const file = new File(['hello'], 'a.txt', { type: 'text/plain' })
    const error = await uploadProjectDocument(PROJECT_ID, { file, name: 'a.txt' }, 'k14').catch(
      (thrown) => thrown,
    )
    expect(error).toBeInstanceOf(ApiConflictError)
    expect((error as ApiConflictError).code).toBe('DOCUMENT_CONFLICT')

    stub.reply({ status: 201, json: okEnvelope({ id: 'doc-2', name: 'a.txt' }) })
    await uploadProjectDocument(PROJECT_ID, { file, name: 'a.txt', allowDuplicateName: true }, 'k15')
    expect(stub.calls[1]!.formData?.get('allow_duplicate_name')).toBe('true')
  })

  it('新版本上传走文档版本端点（同一文档 number+1）', async () => {
    stub.reply({ status: 201, json: okEnvelope({ id: DOC_ID, current_version: { number: 2 } }) })
    const file = new File(['v2'], 'a.txt', { type: 'text/plain' })
    const result = await uploadDocumentVersion(DOC_ID, file, 'k16')
    expect(stub.calls[0]!.url).toContain(`/documents/${DOC_ID}/versions`)
    expect(stub.calls[0]!.formData?.get('file')).toBeInstanceOf(File)
    expect(result.current_version?.number).toBe(2)
  })

  it('文档详情返回 append-only 版本历史', async () => {
    stub.reply({
      status: 200,
      json: okEnvelope({
        id: DOC_ID,
        name: 'a.txt',
        versions: [
          { id: 'v2', number: 2, size: 200, uploaded_by_type: 'web_operator' },
          { id: 'v1', number: 1, size: 100, uploaded_by_type: 'agent' },
        ],
      }),
    })
    const detail = await getDocument(DOC_ID)
    expect(detail.versions.map((version) => version.number)).toEqual([2, 1])
  })

  it('移出参考列表使用 DELETE，不删除历史', async () => {
    stub.reply({ status: 200, json: okEnvelope({ id: DOC_ID, is_archived: true }) })
    const result = await archiveDocument(DOC_ID, 'k17')
    expect(stub.calls[0]!.method).toBe('DELETE')
    expect(stub.calls[0]!.url).toContain(`/documents/${DOC_ID}`)
    expect(result.is_archived).toBe(true)
  })

  it('文档列表与受控下载地址', async () => {
    stub.reply({ status: 200, json: pageEnvelope([{ id: DOC_ID, name: 'a.txt' }]) })
    const page = await listProjectDocuments(PROJECT_ID)
    expect(page.items).toHaveLength(1)
    expect(fileDownloadUrl('file-1')).toBe('/api/web/v1/files/file-1/download')
  })
})
