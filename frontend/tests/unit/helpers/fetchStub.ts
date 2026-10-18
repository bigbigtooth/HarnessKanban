/**
 * 网络层测试替身：只替换 fetch，不替换客户端/组件逻辑（T28 测试要求）。
 */
import type { FetchLike } from '@/api/client'

export interface StubCall {
  url: string
  method: string
  headers: Record<string, string>
  idempotencyKey: string | null
  bodyText: string | null
  formData: FormData | null
  signal: AbortSignal | undefined
}

export interface StubReply {
  status?: number
  /** 以 JSON 序列化返回。 */
  json?: unknown
  /** 原始文本返回（非 JSON 场景）。 */
  text?: string
  headers?: Record<string, string>
  delayMs?: number
  /** 手动闸门：测试控制返回时机（乱序场景）。 */
  gate?: Promise<void>
  /** 模拟网络层失败（fetch reject）。 */
  networkError?: boolean
}

export type StubPlan = StubReply | ((call: StubCall, index: number) => StubReply | Promise<StubReply>)

export interface FetchStub {
  fetch: FetchLike
  calls: StubCall[]
  /** 追加若干一次性返回计划。 */
  reply(...plans: StubPlan[]): void
  /** 设置无队列匹配时的默认计划。 */
  setDefault(plan: StubPlan): void
  reset(): void
}

export function abortError(): Error {
  return new DOMException('The operation was aborted.', 'AbortError')
}

function raceWithAbort<T>(value: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return value
  if (signal.aborted) return Promise.reject(abortError())
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(abortError())
    signal.addEventListener('abort', onAbort, { once: true })
    value.then(
      (result) => {
        signal.removeEventListener('abort', onAbort)
        resolve(result)
      },
      (error) => {
        signal.removeEventListener('abort', onAbort)
        reject(error)
      },
    )
  })
}

function sleepAbortable(ms: number, signal?: AbortSignal): Promise<void> {
  return raceWithAbort(
    new Promise<void>((resolve) => setTimeout(resolve, ms)),
    signal,
  )
}

function captureCall(input: string, init: RequestInit | undefined): StubCall {
  const headers: Record<string, string> = {}
  const rawHeaders = (init?.headers ?? {}) as Record<string, string>
  for (const [key, value] of Object.entries(rawHeaders)) {
    headers[key.toLowerCase()] = String(value)
  }
  const body = init?.body
  let bodyText: string | null = null
  let formData: FormData | null = null
  if (typeof body === 'string') bodyText = body
  else if (typeof FormData !== 'undefined' && body instanceof FormData) formData = body
  return {
    url: input,
    method: (init?.method ?? 'GET').toUpperCase(),
    headers,
    idempotencyKey: headers['idempotency-key'] ?? null,
    bodyText,
    formData,
    signal: init?.signal ?? undefined,
  }
}

async function deliver(reply: StubReply, signal: AbortSignal | undefined): Promise<Response> {
  if (reply.gate) await raceWithAbort(reply.gate, signal)
  else if (reply.delayMs) await sleepAbortable(reply.delayMs, signal)
  if (signal?.aborted) throw abortError()
  if (reply.networkError) throw new TypeError('fetch failed')
  const headers = new Headers(reply.json !== undefined ? { 'Content-Type': 'application/json' } : {})
  for (const [key, value] of Object.entries(reply.headers ?? {})) headers.set(key, value)
  const bodyText = reply.json !== undefined ? JSON.stringify(reply.json) : (reply.text ?? '')
  return new Response(bodyText.length > 0 ? bodyText : null, { status: reply.status ?? 200, headers })
}

export function createFetchStub(): FetchStub {
  const calls: StubCall[] = []
  const queue: StubPlan[] = []
  let defaultPlan: StubPlan = { status: 200, json: okEnvelope(null) }

  const fetchImpl: FetchLike = async (input, init) => {
    const call = captureCall(input, init)
    calls.push(call)
    const plan = queue.shift() ?? defaultPlan
    const reply = typeof plan === 'function' ? await plan(call, calls.length - 1) : plan
    return deliver(reply, call.signal)
  }

  return {
    fetch: fetchImpl,
    calls,
    reply(...plans) {
      queue.push(...plans)
    },
    setDefault(plan) {
      defaultPlan = plan
    },
    reset() {
      calls.length = 0
      queue.length = 0
      defaultPlan = { status: 200, json: okEnvelope(null) }
    },
  }
}

export function okEnvelope(data: unknown, requestId = 'req-ok-0001'): unknown {
  return { data, request_id: requestId }
}

export function errEnvelope(
  code: string,
  message: string,
  options: { requestId?: string; details?: Record<string, unknown> } = {},
): unknown {
  return {
    error: { code, message, details: options.details ?? {} },
    request_id: options.requestId ?? 'req-err-0001',
  }
}

export function pageEnvelope<T>(items: T[], overrides: { requestId?: string; total?: number; page?: number; pageSize?: number } = {}): unknown {
  return okEnvelope(
    {
      items,
      total: overrides.total ?? items.length,
      page: overrides.page ?? 1,
      page_size: overrides.pageSize ?? 20,
    },
    overrides.requestId,
  )
}

export interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (error: unknown) => void
}

export function deferred<T = void>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

/** 记录 localStorage 的所有写入；用于“无持久化秘密/状态”断言。 */
export function trackLocalStorage(): { writes: Array<[string, string | null]>; restore: () => void } {
  const writes: Array<[string, string | null]> = []
  const originalSet = Storage.prototype.setItem
  const originalClear = Storage.prototype.clear
  Storage.prototype.setItem = function trackedSetItem(key: string, value: string) {
    writes.push([key, value])
    return originalSet.call(this, key, value)
  }
  Storage.prototype.clear = function trackedClear() {
    writes.push(['<clear>', null])
    return originalClear.call(this)
  }
  return {
    writes,
    restore: () => {
      Storage.prototype.setItem = originalSet
      Storage.prototype.clear = originalClear
    },
  }
}
