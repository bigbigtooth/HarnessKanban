/**
 * Agent WebAPI 封装单元测试（T29）。
 *
 * 只替换网络层（fetch 替身），客户端与幂等键逻辑保持真实：
 * - 断言请求路径、查询参数与请求体形状完全匹配服务端白名单；
 * - 断言写操作一定携带调用方提供的 Idempotency-Key，且重试不换键；
 * - 断言错误按契约码透出（不吞成空成功）；
 * - 断言任一次调用都不会把 Key 写进 localStorage/sessionStorage。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiConflictError, ApiError, newIdempotencyKey, type FetchLike } from '@/api/client'
import {
  createAgent,
  disableAgent,
  enableAgent,
  getAgent,
  listAgentsPage,
  rotateAgentKey,
  updateAgent,
  updateAgentRole,
} from '@/api/agents'
import { createFetchStub, errEnvelope, okEnvelope, pageEnvelope, trackLocalStorage } from './helpers/fetchStub'

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

const DETAIL = {
  id: '11111111-1111-4111-8111-111111111111',
  name: 'agent-a',
  role: 'WORKER',
  description: null,
  capabilities: ['ts'],
  enabled: true,
  max_concurrency: 3,
  reserved_execution_count: 1,
  available_capacity: 2,
  last_heartbeat_at: '2026-10-15T00:00:00Z',
  online_state: 'ONLINE_WORKING',
  version: 4,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-15T00:00:00Z',
  key_version: 1,
  project_ids: [],
  projects: [],
  capacity: { max_concurrency: 3, reserved_execution_count: 1, available_capacity: 2 },
  execution_summary: {
    open_count: 1,
    effective_count: 1,
    completed_count: 5,
    timeout_count: 0,
    rework_count: 1,
    total_count: 6,
    pending_reclaim_count: 0,
  },
  current_executions: [],
}

describe('Agent 列表与详情', () => {
  it('把名称/角色/启用/在线/分页映射为服务端参数，空值不发送', async () => {
    stub.reply({ status: 200, json: pageEnvelope([DETAIL]) })

    await listAgentsPage({ name: '  阿 ', role: 'SUPERVISOR', enabled: false, online: true, page: 2, pageSize: 50 })

    const url = stub.calls[0]!.url
    expect(url).toContain('/api/web/v1/agents?')
    expect(url).toContain('name=%E9%98%BF')
    expect(url).toContain('role=SUPERVISOR')
    expect(url).toContain('enabled=false')
    expect(url).toContain('online=true')
    expect(url).toContain('page=2')
    expect(url).toContain('page_size=50')
    expect(stub.calls[0]!.method).toBe('GET')
    expect(stub.calls[0]!.idempotencyKey).toBeNull()
  })

  it('空筛选只发送分页参数', async () => {
    stub.reply({ status: 200, json: pageEnvelope([]) })
    await listAgentsPage({})
    const url = stub.calls[0]!.url
    expect(url).not.toContain('name=')
    expect(url).not.toContain('role=')
    expect(url).not.toContain('enabled=')
    expect(url).not.toContain('online=')
  })

  it('详情读取使用 GET，且不产生任何浏览器持久存储写入', async () => {
    const tracker = trackLocalStorage()
    try {
      stub.reply({ status: 200, json: okEnvelope(DETAIL) })
      const detail = await getAgent('11111111-1111-4111-8111-111111111111')
      expect(detail.capacity.reserved_execution_count).toBe(1)
      expect(detail.execution_summary.effective_count).toBe(1)
      expect(stub.calls[0]!.method).toBe('GET')
      expect(tracker.writes).toEqual([])
      expect(localStorage.length).toBe(0)
      expect(sessionStorage.length).toBe(0)
    } finally {
      tracker.restore()
    }
  })
})

describe('Agent 创建与 Key 交付', () => {
  it('创建请求体只含白名单字段，并携带调用方的幂等键', async () => {
    stub.reply({ status: 201, json: okEnvelope({ ...DETAIL, agent_key: 'htk_live_secret' }) })

    const key = newIdempotencyKey()
    const result = await createAgent({ name: 'agent-a', role: 'WORKER', max_concurrency: 3 }, key)

    const call = stub.calls[0]!
    expect(call.method).toBe('POST')
    expect(call.url).toContain('/api/web/v1/agents')
    expect(call.idempotencyKey).toBe(key)
    expect(jsonBody()).toEqual({ name: 'agent-a', role: 'WORKER', max_concurrency: 3 })
    expect(result.agent_key).toBe('htk_live_secret')
  })

  it('省略容量时不发送 max_concurrency（由服务端按配置默认值创建）', async () => {
    stub.reply({ status: 201, json: okEnvelope({ ...DETAIL, agent_key: 'k' }) })
    await createAgent({ name: 'n', role: 'SUPERVISOR' }, 'idem-1')
    expect(jsonBody()).toEqual({ name: 'n', role: 'SUPERVISOR' })
  })

  it('重放响应只带 key_delivery_unavailable，不含 agent_key', async () => {
    stub.reply({
      status: 200,
      json: okEnvelope({ ...DETAIL, idempotent_replay: true, key_delivery_unavailable: true }),
    })
    const result = await createAgent({ name: 'agent-a', role: 'WORKER' }, 'idem-1')
    expect(result.idempotent_replay).toBe(true)
    expect(result.key_delivery_unavailable).toBe(true)
    expect(result.agent_key).toBeUndefined()
  })

  it('重置 Key 使用 POST /agents/{id}/rotate-key 并复用同一个键重试', async () => {
    stub.reply({ status: 503, json: errEnvelope('DATABASE_BUSY', '稍后重试') })
    stub.reply({ status: 200, json: okEnvelope({ ...DETAIL, agent_key: 'rotated' }) })

    const key = 'retry-key-1'
    await expect(rotateAgentKey(DETAIL.id, key)).rejects.toBeInstanceOf(ApiError)
    const rotated = await rotateAgentKey(DETAIL.id, key)

    expect(rotated.agent_key).toBe('rotated')
    expect(stub.calls[0]!.url).toContain(`/api/web/v1/agents/${DETAIL.id}/rotate-key`)
    expect(stub.calls[0]!.method).toBe('POST')
    expect(stub.calls[1]!.idempotencyKey).toBe(key)
    expect(stub.calls[0]!.idempotencyKey).toBe(key)
  })

  it('创建/重置不写 localStorage 与 sessionStorage', async () => {
    const tracker = trackLocalStorage()
    try {
      stub.reply({ status: 201, json: okEnvelope({ ...DETAIL, agent_key: 'plain-secret-key' }) })
      stub.reply({ status: 200, json: okEnvelope({ ...DETAIL, agent_key: 'plain-secret-key-2' }) })

      await createAgent({ name: 'a', role: 'WORKER' }, 'k1')
      await rotateAgentKey(DETAIL.id, 'k2')

      expect(tracker.writes).toEqual([])
      expect(localStorage.length).toBe(0)
      expect(sessionStorage.length).toBe(0)
      expect(JSON.stringify(localStorage)).not.toContain('plain-secret-key')
      expect(JSON.stringify(sessionStorage)).not.toContain('plain-secret-key')
    } finally {
      tracker.restore()
    }
  })

  it('启用可请求同时轮换 Key', async () => {
    stub.reply({ status: 200, json: okEnvelope({ ...DETAIL, agent_key: 'enabled-key' }) })
    const result = await enableAgent(DETAIL.id, true, 'k3')
    expect(jsonBody()).toEqual({ rotate_key: true })
    expect(result.agent_key).toBe('enabled-key')
  })
})

describe('Agent 禁用与角色变更', () => {
  it('禁用请求体字段为服务端白名单的 reclaim + reason，并回显处置与回收的执行', async () => {
    stub.reply({
      status: 200,
      json: okEnvelope({
        ...DETAIL,
        enabled: false,
        disposition: 'NOW',
        reclaimed_execution_ids: ['exec-1'],
        reason: '违规操作',
      }),
    })

    const result = await disableAgent(DETAIL.id, 'NOW', '违规操作', 'k4')

    expect(jsonBody()).toEqual({ reclaim: 'NOW', reason: '违规操作' })
    expect(result.disposition).toBe('NOW')
    expect(result.reclaimed_execution_ids).toEqual(['exec-1'])
    expect(stub.calls[0]!.url).toContain(`/agents/${DETAIL.id}/disable`)
  })

  it('禁用时服务端要求明确处置：409/400 错误按契约码透出', async () => {
    stub.reply({
      status: 400,
      json: errEnvelope('INVALID_ARGUMENT', 'Agent 持有有效执行，必须明确选择保留等待(WAIT)或立即回收(NOW)'),
    })
    await expect(disableAgent(DETAIL.id, 'WAIT', '原因', 'k5')).rejects.toMatchObject({
      code: 'INVALID_ARGUMENT',
      httpStatus: 400,
    })
  })

  it('理由为空时仍按原样发送（服务端负责拒绝），前端不伪造成功', async () => {
    stub.reply({ status: 400, json: errEnvelope('INVALID_ARGUMENT', '立即回收必须填写原因') })
    await expect(disableAgent(DETAIL.id, 'NOW', '', 'k6')).rejects.toBeInstanceOf(ApiError)
    expect(jsonBody()).toEqual({ reclaim: 'NOW', reason: '' })
  })

  it('角色变更使用 POST /agents/{id}/role 并携带 expected_version', async () => {
    stub.reply({ status: 200, json: okEnvelope({ ...DETAIL, role: 'SUPERVISOR' }) })
    const result = await updateAgentRole(DETAIL.id, 'SUPERVISOR', 7, 'k7')
    expect(jsonBody()).toEqual({ role: 'SUPERVISOR', expected_version: 7 })
    expect(result.role).toBe('SUPERVISOR')
  })

  it('版本冲突抛 ApiConflictError 并带服务端 current_version', async () => {
    stub.reply({
      status: 409,
      json: errEnvelope('VERSION_CONFLICT', '版本冲突', { details: { current_version: 9 } }),
    })
    const error = await updateAgentRole(DETAIL.id, 'WORKER', 1, 'k8').catch((thrown) => thrown)
    expect(error).toBeInstanceOf(ApiConflictError)
    expect((error as ApiConflictError).details).toMatchObject({ current_version: 9 })
  })

  it('容量低于未释放槽数时 409 CAPACITY_BELOW_RESERVED 原样透出', async () => {
    stub.reply({
      status: 409,
      json: errEnvelope('CAPACITY_BELOW_RESERVED', '容量不能低于已占槽位数', {
        details: { max_concurrency: 1, reserved_execution_count: 2 },
      }),
    })
    const error = await updateAgent(
      DETAIL.id,
      3,
      { max_concurrency: 1 },
      'k9',
    ).catch((thrown) => thrown)
    expect(error).toBeInstanceOf(ApiConflictError)
    expect((error as ApiConflictError).code).toBe('CAPACITY_BELOW_RESERVED')
    expect(jsonBody()).toEqual({ expected_version: 3, max_concurrency: 1 })
  })
})
