/**
 * WebAPI 客户端单元测试（网络层用 fetch 替身，不 mock 业务逻辑）。
 * 覆盖 CONTRACT.md §3（信封/错误）、§8（幂等键复用）与 D15（409/503 处理）。
 */
import { describe, expect, it } from 'vitest'
import {
  ApiAbortError,
  ApiConflictError,
  ApiError,
  ApiNetworkError,
  ApiRetryableError,
  WebApiClient,
  backoffDelayMs,
  buildQueryString,
  newIdempotencyKey,
  unwrapSuccess,
} from '@/api/client'
import { createFetchStub, deferred, errEnvelope, okEnvelope } from './helpers/fetchStub'

function makeClient(stub = createFetchStub()) {
  const client = new WebApiClient({ fetchImpl: stub.fetch })
  return { client, stub }
}

describe('WebApiClient 信封解析', () => {
  it('成功信封：返回 data，并保留 request_id 与 X-Request-Id', async () => {
    const { client, stub } = makeClient()
    stub.reply({
      status: 200,
      json: okEnvelope({ id: 'task-1', title: '真实数据' }, 'req-from-body'),
      headers: { 'X-Request-Id': 'req-from-body' },
    })

    const result = await client.requestWithMeta<{ id: string }>('/tasks/task-1')

    expect(result.data).toEqual({ id: 'task-1', title: '真实数据' })
    expect(result.requestId).toBe('req-from-body')
    expect(result.httpStatus).toBe(200)
    expect(stub.calls[0]!.url).toBe('/api/web/v1/tasks/task-1')
    expect(stub.calls[0]!.method).toBe('GET')
  })

  it('错误信封：透出 code/message/details/httpStatus/request_id', async () => {
    const { client, stub } = makeClient()
    stub.reply({
      status: 404,
      json: errEnvelope('RESOURCE_NOT_FOUND', '任务不存在或不可访问', {
        requestId: 'req-404',
        details: { task_id: 'missing' },
      }),
    })

    const error = await client.request('/tasks/missing').catch((thrown: unknown) => thrown)

    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.code).toBe('RESOURCE_NOT_FOUND')
    expect(apiError.message).toBe('任务不存在或不可访问')
    expect(apiError.details).toEqual({ task_id: 'missing' })
    expect(apiError.httpStatus).toBe(404)
    expect(apiError.request_id).toBe('req-404')
    expect(apiError.retryable).toBe(false)
  })

  it('成功状态但响应体缺失信封：显式失败，不伪装成空成功', async () => {
    const { client, stub } = makeClient()
    stub.reply({ status: 200, text: '' })
    await expect(client.request('/dashboard')).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })

    stub.reset()
    stub.reply({ status: 200, json: { request_id: 'req-x' } })
    await expect(client.request('/dashboard')).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })

  it('非 JSON 的服务端错误（代理页/HTML）：抛出带 httpStatus 的显式错误', async () => {
    const { client, stub } = makeClient()
    stub.reply({ status: 500, text: '<html>error</html>' })

    const error = (await client.request('/dashboard').catch((thrown: unknown) => thrown)) as ApiError
    expect(error).toBeInstanceOf(ApiError)
    expect(error.code).toBe('INVALID_RESPONSE')
    expect(error.httpStatus).toBe(500)
    expect(error.kind).toBe('server')
  })

  it('网络层失败：ApiNetworkError（可重试），不是空成功', async () => {
    const { client, stub } = makeClient()
    stub.reply({ networkError: true })

    const error = (await client.request('/dashboard').catch((thrown: unknown) => thrown)) as ApiError
    expect(error).toBeInstanceOf(ApiNetworkError)
    expect(error.code).toBe('NETWORK_ERROR')
    expect(error.retryable).toBe(true)
  })
})

describe('幂等键契约', () => {
  it('写操作缺少幂等键：本地拦截，且不发出请求', async () => {
    const { client, stub } = makeClient()

    const error = (await client.post('/tasks', { title: 'x' }).catch((thrown: unknown) => thrown)) as ApiError

    expect(error.code).toBe('IDEMPOTENCY_KEY_REQUIRED')
    expect(stub.calls).toHaveLength(0)
  })

  it('503 重试复用同一个幂等键（绝不换键）', async () => {
    const { client, stub } = makeClient()
    const key = 'key-fixed-0001'
    stub.reply({ status: 503, json: errEnvelope('DATABASE_BUSY', '数据库繁忙，请按原幂等键稍后重试') })
    stub.reply({ status: 200, json: okEnvelope({ id: 'task-2', status: 'TODO' }, 'req-ok-2') })

    const result = await client.requestWithRetry<{ id: string }>(
      '/tasks/task-2/publish',
      { method: 'POST', body: { expected_version: 3 }, idempotencyKey: key },
      { maxAttempts: 3, sleep: async () => {}, random: () => 1 },
    )

    expect(result).toEqual({ id: 'task-2', status: 'TODO' })
    expect(stub.calls).toHaveLength(2)
    expect(stub.calls[0]!.idempotencyKey).toBe(key)
    expect(stub.calls[1]!.idempotencyKey).toBe(key)
    expect(stub.calls[0]!.idempotencyKey).toBe(stub.calls[1]!.idempotencyKey)
  })

  it('503 的退避次数用尽后抛出可重试错误，且所有尝试使用同一键', async () => {
    const { client, stub } = makeClient()
    const key = 'key-fixed-0002'
    const retries: number[] = []
    stub.setDefault({ status: 503, json: errEnvelope('CLAIM_LOCK_TIMEOUT', '等待全局领取锁超时') })

    const error = (await client
      .requestWithRetry(
        '/tasks/task-3/claim',
        { method: 'POST', body: { agent_id: 'a1' }, idempotencyKey: key },
        {
          maxAttempts: 3,
          sleep: async () => {},
          onRetry: (info) => retries.push(info.delayMs),
        },
      )
      .catch((thrown: unknown) => thrown)) as ApiError

    expect(error).toBeInstanceOf(ApiRetryableError)
    expect(error.code).toBe('CLAIM_LOCK_TIMEOUT')
    expect(error.retryable).toBe(true)
    expect(stub.calls).toHaveLength(3)
    expect(new Set(stub.calls.map((call) => call.idempotencyKey))).toEqual(new Set([key]))
    expect(retries).toHaveLength(2)
  })

  it('409 冲突是类型化错误且不重试（保留表单、刷新对象由页面处理）', async () => {
    const { client, stub } = makeClient()
    stub.reply({ status: 409, json: errEnvelope('VERSION_CONFLICT', '资源版本已变化，请重新读取当前状态') })

    const error = (await client
      .requestWithRetry(
        '/tasks/task-1/publish',
        { method: 'POST', body: {}, idempotencyKey: 'key-conflict-1' },
        { maxAttempts: 3, sleep: async () => {} },
      )
      .catch((thrown: unknown) => thrown)) as ApiError

    expect(error).toBeInstanceOf(ApiConflictError)
    expect(error.isConflict).toBe(true)
    expect(error.retryable).toBe(false)
    expect(stub.calls).toHaveLength(1)
  })

  it('400 业务错误不重试', async () => {
    const { client, stub } = makeClient()
    stub.setDefault({ status: 400, json: errEnvelope('INVALID_ARGUMENT', '难度参数不合法') })

    const error = (await client
      .requestWithRetry('/tasks', { method: 'POST', body: {}, idempotencyKey: 'key-400' }, { maxAttempts: 4, sleep: async () => {} })
      .catch((thrown: unknown) => thrown)) as ApiError

    expect(error.code).toBe('INVALID_ARGUMENT')
    expect(stub.calls).toHaveLength(1)
  })

  it('newIdempotencyKey 生成 UUID 且每次不同', () => {
    const first = newIdempotencyKey()
    const second = newIdempotencyKey()
    expect(first).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)
    expect(first).not.toBe(second)
  })

  it('backoffDelayMs：指数增长且抖动落在约定区间', () => {
    const deterministic = { baseDelayMs: 100, maxDelayMs: 1000, jitterRatio: 0.5 }
    expect(backoffDelayMs(0, { ...deterministic, random: () => 1 })).toBe(100)
    expect(backoffDelayMs(1, { ...deterministic, random: () => 1 })).toBe(200)
    expect(backoffDelayMs(2, { ...deterministic, random: () => 1 })).toBe(400)
    expect(backoffDelayMs(2, { ...deterministic, random: () => 0 })).toBe(200)
    // 上限封顶
    expect(backoffDelayMs(10, { ...deterministic, random: () => 1 })).toBe(1000)
  })
})

describe('multipart 与查询串', () => {
  it('上传使用 FormData，不设置 JSON Content-Type，仍携带幂等键', async () => {
    const { client, stub } = makeClient()
    stub.reply({ status: 200, json: okEnvelope({ id: 'file-1' }, 'req-upload') })

    const form = new FormData()
    form.append('purpose', 'SUBMISSION_ARTIFACT')
    form.append('file', new Blob(['demo']), 'artifact.txt')

    const result = await client.upload<{ id: string }>('/tasks/task-9/attachments', form, { idempotencyKey: 'key-upload-1' })

    expect(result).toEqual({ id: 'file-1' })
    const call = stub.calls[0]!
    expect(call.method).toBe('POST')
    expect(call.formData).not.toBeNull()
    expect(call.formData!.get('purpose')).toBe('SUBMISSION_ARTIFACT')
    expect(call.headers['content-type']).toBeUndefined()
    expect(call.idempotencyKey).toBe('key-upload-1')
  })

  it('查询串：跳过空值并展开数组（等级参数多值）', () => {
    expect(buildQueryString({ status: 'TODO', page: 1, page_size: 20, q: undefined, archived: null })).toBe(
      '?status=TODO&page=1&page_size=20',
    )
    expect(buildQueryString({ l1: 1, l2: 1 })).toBe('?l1=1&l2=1')
    expect(buildQueryString()).toBe('')
  })

  it('unwrapSuccess 对错误信封抛错（防止把错误当成功）', () => {
    expect(() => unwrapSuccess(errEnvelope('VERSION_CONFLICT', '冲突'), 'req-1')).toThrowError(ApiConflictError)
  })
})

describe('取消与乱序', () => {
  it('AbortSignal 取消请求：抛出 ApiAbortError', async () => {
    const { client, stub } = makeClient()
    const gate = deferred()
    stub.reply({ gate: gate.promise, json: okEnvelope({ never: 'used' }) })
    const controller = new AbortController()

    const pending = client.request('/tasks', { signal: controller.signal })
    controller.abort()
    const error = (await pending.catch((thrown: unknown) => thrown)) as ApiError

    expect(error).toBeInstanceOf(ApiAbortError)
    expect(error.code).toBe('REQUEST_ABORTED')
    gate.resolve()
  })

  it('同一客户端并发请求使用不同信号，先发出者被取消不影响后续结果', async () => {
    const { client, stub } = makeClient()
    const slow = deferred()
    stub.reply({ gate: slow.promise, json: okEnvelope({ old: true }, 'req-old') })
    stub.reply({ status: 200, json: okEnvelope({ fresh: true }, 'req-new') })

    const controller = new AbortController()
    const first = client.request<{ old?: boolean; fresh?: boolean }>('/tasks', { signal: controller.signal })
    controller.abort()
    const second = await client.request<{ fresh?: boolean }>('/tasks')

    expect(second).toEqual({ fresh: true })
    await expect(first).rejects.toBeInstanceOf(ApiAbortError)
    slow.resolve()
  })
})
