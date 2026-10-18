/**
 * WebAPI 客户端（T28 单一所有者文件）。
 *
 * 契约来源：docs/CONTRACT.md §3（信封）、§8（幂等），docs/DEVELOP.md D03/D15。
 *
 * 设计要点：
 * - 基地址 `/api/web/v1`（本机同源，经 Vite 代理到 Django 127.0.0.1:7101），无登录。
 * - 写操作必须由调用方传入 Idempotency-Key；客户端**绝不**在重试时另造新键。
 * - 失败一律抛 `ApiError` 子类（含 code/message/details/httpStatus/request_id），
 *   绝不把失败吞成空成功。
 * - 409 → `ApiConflictError`；503（DATABASE_BUSY/CLAIM_LOCK_TIMEOUT 等）/429 →
 *   `ApiRetryableError`，配合 `requestWithRetry`（指数退避 + 抖动，复用原键）。
 * - 支持 AbortSignal 取消，并提供单调序列号守卫（`LatestRequestGate`），
 *   过期响应不得覆盖较新的状态。
 */

import {
  CONFLICT_ERROR_CODES,
  ErrorCode,
  RETRYABLE_ERROR_CODES,
  type ErrorEnvelope,
  type ErrorPayload,
  type SuccessEnvelope,
} from './types'

export const WEB_API_BASE_URL = '/api/web/v1'

export type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'

export type QueryPrimitive = string | number | boolean

export type QueryValue = QueryPrimitive | null | undefined | readonly QueryPrimitive[]

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>

// ------------------------------------------------------------------ 幂等键

/**
 * 生成新的幂等键。调用方在**一次逻辑写操作**开始时生成一次，
 * 之后的超时/503 重试必须复用同一个键（CONTRACT.md §8）。
 */
export function newIdempotencyKey(): string {
  const cryptoObj = globalThis.crypto
  if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
    return cryptoObj.randomUUID()
  }
  if (cryptoObj && typeof cryptoObj.getRandomValues === 'function') {
    const bytes = cryptoObj.getRandomValues(new Uint8Array(16))
    bytes[6] = (bytes[6]! & 0x0f) | 0x40
    bytes[8] = (bytes[8]! & 0x3f) | 0x80
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
  }
  throw new Error('当前运行环境不支持 crypto.randomUUID，无法生成幂等键')
}

// ------------------------------------------------------------------ 错误类型

export type ApiErrorKind = 'conflict' | 'retryable' | 'client' | 'server' | 'network' | 'aborted' | 'protocol'

export interface ApiErrorInit {
  code: string
  message: string
  details?: Record<string, unknown>
  httpStatus: number
  requestId?: string | null
  kind?: ApiErrorKind
  retryable?: boolean
  cause?: unknown
}

export class ApiError extends Error {
  readonly code: string
  readonly details: Record<string, unknown>
  readonly httpStatus: number
  readonly requestId: string | null
  readonly kind: ApiErrorKind
  readonly retryable: boolean

  constructor(init: ApiErrorInit) {
    super(init.message, init.cause !== undefined ? { cause: init.cause } : undefined)
    this.name = 'ApiError'
    this.code = init.code
    this.details = init.details ?? {}
    this.httpStatus = init.httpStatus
    this.requestId = init.requestId ?? null
    this.kind = init.kind ?? 'client'
    this.retryable = init.retryable ?? false
  }

  /** 契约字段名别名（响应信封使用 `request_id`）。 */
  get request_id(): string | null {
    return this.requestId
  }

  /** 是否属于契约 §3 的冲突集合（409）。 */
  get isConflict(): boolean {
    return this.kind === 'conflict'
  }

  /** 是否应按原幂等键退避重试。 */
  get isRetryable(): boolean {
    return this.retryable
  }

  /** 可安全展示给操作人的文案（不含堆栈/SQL/磁盘路径）。 */
  get displayMessage(): string {
    const parts = [this.message]
    if (this.code) parts.push(`（${this.code}）`)
    if (this.requestId) parts.push(` 请求号 ${this.requestId}`)
    return parts.join('')
  }
}

export class ApiConflictError extends ApiError {
  constructor(init: ApiErrorInit) {
    super({ ...init, kind: 'conflict', retryable: init.retryable ?? false })
    this.name = 'ApiConflictError'
  }
}

export class ApiRetryableError extends ApiError {
  constructor(init: ApiErrorInit) {
    super({ ...init, kind: 'retryable', retryable: true })
    this.name = 'ApiRetryableError'
  }
}

export class ApiNetworkError extends ApiError {
  constructor(init: ApiErrorInit) {
    super({ ...init, kind: 'network', retryable: init.retryable ?? true })
    this.name = 'ApiNetworkError'
  }
}

export class ApiAbortError extends ApiError {
  constructor(init: Partial<ApiErrorInit> = {}) {
    super({
      code: ErrorCode.REQUEST_ABORTED,
      message: '请求已取消',
      httpStatus: 0,
      kind: 'aborted',
      retryable: false,
      ...init,
    })
    this.name = 'ApiAbortError'
  }
}

function isAbortLike(error: unknown, signal?: AbortSignal): boolean {
  if (signal?.aborted) return true
  if (error instanceof ApiAbortError) return true
  if (typeof DOMException !== 'undefined' && error instanceof DOMException && error.name === 'AbortError') {
    return true
  }
  return typeof error === 'object' && error !== null && (error as { name?: string }).name === 'AbortError'
}

// ------------------------------------------------------------------ 信封解析

export function isErrorEnvelope(body: unknown): body is ErrorEnvelope {
  if (typeof body !== 'object' || body === null) return false
  const candidate = (body as { error?: unknown }).error
  if (typeof candidate !== 'object' || candidate === null) return false
  return typeof (candidate as { code?: unknown }).code === 'string'
}

export function isSuccessEnvelope(body: unknown): body is SuccessEnvelope<unknown> {
  return typeof body === 'object' && body !== null && 'data' in (body as object)
}

/** 从成功信封取出 `data`；信封缺失时抛协议错误，绝不返回空成功。 */
export function unwrapSuccess<T>(body: unknown, requestId: string | null): T {
  if (isSuccessEnvelope(body)) {
    return (body as SuccessEnvelope<T>).data
  }
  if (isErrorEnvelope(body)) {
    throw buildErrorFromEnvelope(body, 200, requestId)
  }
  throw new ApiError({
    code: ErrorCode.INVALID_RESPONSE,
    message: '服务端响应缺少 data/request_id 信封字段',
    httpStatus: 200,
    requestId,
    kind: 'protocol',
    details: { body },
  })
}

function normalizeErrorPayload(payload: unknown): ErrorPayload {
  if (typeof payload === 'object' && payload !== null) {
    const candidate = payload as Partial<ErrorPayload>
    if (typeof candidate.code === 'string') {
      return {
        code: candidate.code,
        message: typeof candidate.message === 'string' ? candidate.message : candidate.code,
        details:
          typeof candidate.details === 'object' && candidate.details !== null
            ? (candidate.details as Record<string, unknown>)
            : {},
      }
    }
  }
  return { code: ErrorCode.INVALID_RESPONSE, message: '服务端返回了无法解析的错误结构', details: {} }
}

/** 按契约 §3 构造类型化错误：409 → conflict，503/429 → retryable。 */
export function buildErrorFromEnvelope(
  envelope: ErrorEnvelope,
  httpStatus: number,
  headerRequestId: string | null,
): ApiError {
  const payload = normalizeErrorPayload(envelope.error)
  const requestId = envelope.request_id || headerRequestId
  const common: ApiErrorInit = {
    code: payload.code,
    message: payload.message,
    details: payload.details,
    httpStatus,
    requestId,
  }
  const isConflict = httpStatus === 409 || CONFLICT_ERROR_CODES.includes(payload.code)
  const isRetryable = httpStatus === 503 || httpStatus === 429 || RETRYABLE_ERROR_CODES.includes(payload.code)
  if (isConflict) return new ApiConflictError(common)
  if (isRetryable) return new ApiRetryableError(common)
  return new ApiError({ ...common, kind: httpStatus >= 500 ? 'server' : 'client' })
}

// ------------------------------------------------------------------ 退避

export interface BackoffOptions {
  /** 首次退避基数（毫秒）。 */
  baseDelayMs?: number
  /** 单次退避上限（毫秒）。 */
  maxDelayMs?: number
  /** 抖动比例（0–1）：delay 在 [1-ratio, 1] × 指数退避 区间内随机。 */
  jitterRatio?: number
  /** 随机源，测试注入以获得确定值。 */
  random?: () => number
}

export const DEFAULT_BACKOFF: Required<Omit<BackoffOptions, 'random'>> = {
  baseDelayMs: 300,
  maxDelayMs: 8000,
  jitterRatio: 0.5,
}

/**
 * 指数退避 + 抖动。`attempt` 从 0 开始。
 * 只负责计算等待时间；重试必须复用同一个幂等键（见 `requestWithRetry`）。
 */
export function backoffDelayMs(attempt: number, options: BackoffOptions = {}): number {
  const { baseDelayMs, maxDelayMs, jitterRatio } = { ...DEFAULT_BACKOFF, ...options }
  const random = options.random ?? Math.random
  const safeAttempt = Math.max(0, Math.floor(attempt))
  const exponential = Math.min(maxDelayMs, baseDelayMs * 2 ** safeAttempt)
  const ratio = Math.min(1, Math.max(0, jitterRatio))
  const factor = 1 - ratio + ratio * random()
  return Math.round(exponential * factor)
}

export function defaultSleep(ms: number, signal?: AbortSignal): Promise<void> {
  if (ms <= 0) return Promise.resolve()
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new ApiAbortError())
      return
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(timer)
      reject(new ApiAbortError())
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

// ------------------------------------------------------------------ 序列号守卫

/** 单调递增序列号：过期响应（sequence 不再是最新）不得写回状态。 */
export class RequestSequence {
  private current = 0

  begin(): number {
    this.current += 1
    return this.current
  }

  isCurrent(sequence: number): boolean {
    return sequence === this.current
  }

  /** 使所有已发出的请求立刻过期（例如筛选条件变化后）。 */
  invalidate(): void {
    this.current += 1
  }
}

export interface GatedRequest {
  sequence: number
  signal: AbortSignal
  /** 该请求是否仍是最新；为 false 时结果必须丢弃。 */
  isCurrent: () => boolean
  /** 是否被用户取消（非被更新的请求取代）。 */
  isCancelled: () => boolean
}

/**
 * 只保留最新请求：发起新请求时中止上一个，并用序列号拒绝乱序返回。
 */
export class LatestRequestGate {
  private sequence = new RequestSequence()
  private controller: AbortController | null = null
  private cancelled = false

  begin(externalSignal?: AbortSignal): GatedRequest {
    this.controller?.abort()
    const controller = new AbortController()
    this.controller = controller
    this.cancelled = false
    if (externalSignal) {
      if (externalSignal.aborted) controller.abort()
      else externalSignal.addEventListener('abort', () => controller.abort(), { once: true })
    }
    const sequence = this.sequence.begin()
    return {
      sequence,
      signal: controller.signal,
      isCurrent: () => this.sequence.isCurrent(sequence),
      isCancelled: () => this.cancelled && !this.sequence.isCurrent(sequence),
    }
  }

  /** 用户取消：中止在途请求并使其结果过期。 */
  cancel(): void {
    this.cancelled = true
    this.sequence.invalidate()
    this.controller?.abort()
    this.controller = null
  }

  get pending(): boolean {
    return this.controller !== null && !this.controller.signal.aborted
  }
}

/**
 * 执行“只保留最新”的请求：过期或被取消时返回 `null`，
 * 调用方据此**丢弃**结果而不是覆盖新状态。
 */
export async function runLatest<T>(
  gate: LatestRequestGate,
  task: (request: GatedRequest) => Promise<T>,
): Promise<T | null> {
  const context = gate.begin()
  try {
    const result = await task(context)
    return context.isCurrent() ? result : null
  } catch (error) {
    if (error instanceof ApiAbortError && !context.isCurrent()) return null
    throw error
  }
}

// ------------------------------------------------------------------ 客户端

export interface RetryOptions extends BackoffOptions {
  /** 总尝试次数（含首次），默认 3。 */
  maxAttempts?: number
  /** 自定义可重试判定；默认使用错误的 retryable 标记。 */
  shouldRetry?: (error: ApiError, attempt: number) => boolean
  /** 等待实现，测试可注入以跳过真实定时器。 */
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>
  /** 每次退避前的观察钩子（日志/测试断言用）。 */
  onRetry?: (info: { attempt: number; delayMs: number; error: ApiError; idempotencyKey?: string }) => void
}

export interface RequestOptions {
  method?: HttpMethod
  /** JSON 请求体。 */
  body?: unknown
  /** query 参数；数组会展开为重复键。 */
  query?: Record<string, QueryValue>
  /** multipart 上传体（与 body 互斥）。 */
  multipart?: FormData
  /**
   * 调用方持有的幂等键。写操作必填且由调用方在**一次逻辑操作**开始时生成；
   * 客户端不会在重试时另造新键。
   */
  idempotencyKey?: string
  signal?: AbortSignal
  headers?: Record<string, string>
}

export interface ClientResult<T> {
  data: T
  requestId: string | null
  httpStatus: number
}

export interface WebApiClientOptions {
  baseUrl?: string
  fetchImpl?: FetchLike
  defaultHeaders?: Record<string, string>
}

const WRITE_METHODS: readonly HttpMethod[] = ['POST', 'PATCH', 'PUT', 'DELETE']

/** 访问密钥失效/缺失时广播，让应用切回登录页。 */
export const WEBUI_UNAUTHORIZED_EVENT = 'harvesstask:webui-unauthorized'

export function notifyUnauthorized(code: string): void {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent(WEBUI_UNAUTHORIZED_EVENT, { detail: { code } }))
}

function isWriteMethod(method: HttpMethod): boolean {
  return WRITE_METHODS.includes(method)
}

export function buildQueryString(query?: Record<string, QueryValue>): string {
  if (!query) return ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue
    if (Array.isArray(value)) {
      for (const item of value) params.append(key, String(item))
    } else {
      params.append(key, String(value))
    }
  }
  const encoded = params.toString()
  return encoded ? `?${encoded}` : ''
}

export class WebApiClient {
  readonly baseUrl: string
  private readonly fetchImpl: FetchLike
  private readonly defaultHeaders: Record<string, string>

  constructor(options: WebApiClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? WEB_API_BASE_URL).replace(/\/$/, '')
    this.fetchImpl = options.fetchImpl ?? ((input, init) => globalThis.fetch(input, init))
    this.defaultHeaders = { Accept: 'application/json', ...(options.defaultHeaders ?? {}) }
  }

  buildUrl(path: string, query?: Record<string, QueryValue>): string {
    const normalized = path.startsWith('/') ? path : `/${path}`
    return `${this.baseUrl}${normalized}${buildQueryString(query)}`
  }

  /** 发送请求并返回 `{data, requestId, httpStatus}`（用于需要请求号的页面）。 */
  async requestWithMeta<T>(path: string, options: RequestOptions = {}): Promise<ClientResult<T>> {
    const method: HttpMethod = options.method ?? 'GET'
    if (options.multipart && options.body !== undefined) {
      throw new ApiError({
        code: ErrorCode.INVALID_ARGUMENT,
        message: 'multipart 与 JSON body 不能同时使用',
        httpStatus: 0,
        kind: 'client',
      })
    }
    if (isWriteMethod(method) && !options.idempotencyKey) {
      // 本地拦截：服务端会返回 400 IDEMPOTENCY_KEY_REQUIRED；这里提前失败，
      // 避免调用方依赖“客户端自动造键”而破坏重试语义。
      throw new ApiError({
        code: ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        message: '写操作必须由调用方提供 Idempotency-Key（客户端不会自动生成）',
        httpStatus: 0,
        kind: 'client',
        details: { method, path },
      })
    }

    const headers: Record<string, string> = { ...this.defaultHeaders, ...(options.headers ?? {}) }
    let body: BodyInit | undefined
    if (options.multipart) {
      body = options.multipart
      // 不要手工设置 multipart boundary，交给运行时。
    } else if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json'
      body = JSON.stringify(options.body)
    }
    if (options.idempotencyKey) {
      headers['Idempotency-Key'] = options.idempotencyKey
    }

    const url = this.buildUrl(path, options.query)
    let response: Response
    try {
      response = await this.fetchImpl(url, {
        method,
        headers,
        body,
        signal: options.signal,
        credentials: 'same-origin',
      })
    } catch (error) {
      if (isAbortLike(error, options.signal)) throw new ApiAbortError({ cause: error })
      throw new ApiNetworkError({
        code: ErrorCode.NETWORK_ERROR,
        message: '无法连接本机 WebAPI（Django 127.0.0.1:7101），请确认服务已启动',
        httpStatus: 0,
        details: { url, method },
        cause: error,
      })
    }

    const headerRequestId = response.headers.get('X-Request-Id')
    const httpStatus = response.status
    let rawText: string
    try {
      rawText = await response.text()
    } catch (error) {
      if (isAbortLike(error, options.signal)) throw new ApiAbortError({ cause: error })
      throw new ApiNetworkError({
        code: ErrorCode.NETWORK_ERROR,
        message: '读取服务端响应失败',
        httpStatus,
        requestId: headerRequestId,
        details: { url, method },
        cause: error,
      })
    }

    let parsed: unknown = null
    const hasBody = rawText.trim().length > 0
    if (hasBody) {
      try {
        parsed = JSON.parse(rawText)
      } catch {
        parsed = null
      }
    }

    if (!response.ok) {
      if (isErrorEnvelope(parsed)) {
        const built = buildErrorFromEnvelope(parsed, httpStatus, headerRequestId)
        // 访问密钥缺失/失效：广播一次，应用层据此清空登录态并显示登录页。
        if (built.code === ErrorCode.WEBUI_KEY_REQUIRED || built.code === ErrorCode.WEBUI_KEY_INVALID) {
          notifyUnauthorized(built.code)
        }
        throw built
      }
      // 非契约结构（代理错误页、HTML 等）也要显式失败。
      throw new ApiError({
        code: ErrorCode.INVALID_RESPONSE,
        message: `服务端返回 HTTP ${httpStatus}，且响应体不是契约信封`,
        httpStatus,
        requestId: headerRequestId,
        kind: httpStatus >= 500 ? 'server' : 'client',
        retryable: httpStatus === 502 || httpStatus === 503 || httpStatus === 504,
        details: { body: hasBody ? rawText.slice(0, 2000) : null, url, method },
      })
    }

    if (!hasBody) {
      if (httpStatus === 204) {
        return { data: null as T, requestId: headerRequestId, httpStatus }
      }
      throw new ApiError({
        code: ErrorCode.INVALID_RESPONSE,
        message: '服务端返回成功状态但响应体为空，缺少契约信封',
        httpStatus,
        requestId: headerRequestId,
        kind: 'protocol',
        details: { url, method },
      })
    }

    const data = unwrapSuccess<T>(parsed, headerRequestId)
    const envelopeRequestId = (parsed as SuccessEnvelope<unknown>).request_id
    return { data, requestId: envelopeRequestId || headerRequestId, httpStatus }
  }

  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const result = await this.requestWithMeta<T>(path, options)
    return result.data
  }

  get<T>(path: string, options: Omit<RequestOptions, 'method' | 'body' | 'multipart'> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'GET' })
  }

  post<T>(path: string, body: unknown, options: Omit<RequestOptions, 'method' | 'body'> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'POST', body })
  }

  patch<T>(path: string, body: unknown, options: Omit<RequestOptions, 'method' | 'body'> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'PATCH', body })
  }

  put<T>(path: string, body: unknown, options: Omit<RequestOptions, 'method' | 'body'> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'PUT', body })
  }

  delete<T>(path: string, options: Omit<RequestOptions, 'method' | 'body' | 'multipart'> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'DELETE' })
  }

  /** multipart 上传（附件/文档）；写操作同样必须携带调用方的幂等键。 */
  upload<T>(
    path: string,
    formData: FormData,
    options: Omit<RequestOptions, 'method' | 'body' | 'multipart'> & { method?: 'POST' | 'PUT' | 'PATCH' } = {},
  ): Promise<T> {
    const { method = 'POST', ...rest } = options
    return this.request<T>(path, { ...rest, method, multipart: formData })
  }

  /**
   * 带退避的重试：仅重试 `retryable` 错误（503/429 及网络抖动），
   * **始终复用 `options.idempotencyKey`**——同一逻辑写操作不会换键。
   */
  async requestWithRetry<T>(path: string, options: RequestOptions = {}, retry: RetryOptions = {}): Promise<T> {
    const maxAttempts = Math.max(1, retry.maxAttempts ?? 3)
    const shouldRetry = retry.shouldRetry ?? ((error: ApiError) => error.retryable)
    const sleep = retry.sleep ?? defaultSleep

    if (isWriteMethod(options.method ?? 'GET') && !options.idempotencyKey) {
      throw new ApiError({
        code: ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        message: '写操作必须由调用方提供 Idempotency-Key（客户端不会自动生成）',
        httpStatus: 0,
        kind: 'client',
        details: { path, method: options.method },
      })
    }

    let lastError: ApiError | null = null
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      try {
        return await this.request<T>(path, options)
      } catch (error) {
        if (!(error instanceof ApiError)) throw error
        lastError = error
        const isLast = attempt === maxAttempts - 1
        if (isLast || !shouldRetry(error, attempt)) throw error
        const delayMs = backoffDelayMs(attempt, retry)
        retry.onRetry?.({ attempt: attempt + 1, delayMs, error, idempotencyKey: options.idempotencyKey })
        await sleep(delayMs, options.signal)
      }
    }
    /* c8 ignore next */
    throw lastError ?? new ApiError({ code: ErrorCode.INVALID_RESPONSE, message: '重试流程异常结束', httpStatus: 0 })
  }
}

/** 应用默认客户端：同源 `/api/web/v1`。 */
export const webApiClient = new WebApiClient()
