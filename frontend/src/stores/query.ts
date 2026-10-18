/**
 * 请求协调 store（T28 所有者文件）。
 *
 * 依据 D15/T28：
 * - 页面状态区分加载 / 空 / 失败 / 已取消；失败绝不显示为空列表。
 * - 筛选条件由页面保存，取消请求不清空筛选。
 * - 列表请求用序列号 + 取消控制，过期响应不能覆盖新筛选。
 * - 可见页面 10 秒轮询（`VisiblePagePoller`），隐藏时暂停且不叠加请求。
 * - 不写 localStorage（无密钥、无持久化状态）。
 */

import { computed, ref, type ComputedRef, type Ref } from 'vue'
import { defineStore } from 'pinia'
import { ApiAbortError, ApiError, LatestRequestGate } from '@/api/client'
import type { Page } from '@/api/types'
import { createPoller, DEFAULT_POLL_INTERVAL_MS, type PollerOptions, type VisiblePagePoller } from './polling'

export type FilterValue = string | number | boolean | null | undefined | readonly (string | number | boolean)[]
export type FilterRecord = Record<string, FilterValue>

export type QueryStatus = 'idle' | 'loading' | 'ready' | 'empty' | 'error' | 'cancelled'

export interface CoordinatorContext {
  readonly filters: Readonly<FilterRecord>
  readonly page: number
  readonly pageSize: number
  readonly signal: AbortSignal
}

export type PageFetcher<T> = (context: CoordinatorContext) => Promise<Page<T>>

export interface FetchOptions {
  /** 静默刷新（轮询用）：不切换到 loading，失败保留当前数据并记录 error。 */
  silent?: boolean
}

export interface QueryCoordinator<T> {
  filters: Ref<FilterRecord>
  page: Ref<number>
  pageSize: Ref<number>
  total: Ref<number>
  items: Ref<T[]>
  status: Ref<QueryStatus>
  error: Ref<ApiError | null>
  requestId: Ref<string | null>
  updatedAt: Ref<number | null>
  readonly isEmpty: ComputedRef<boolean>
  readonly hasItems: ComputedRef<boolean>
  readonly isLoading: ComputedRef<boolean>
  readonly hasError: ComputedRef<boolean>
  readonly filterSignature: ComputedRef<string>
  setFilter(key: string, value: FilterValue): void
  setFilters(patch: FilterRecord): void
  clearFilters(): void
  setPage(page: number): void
  setPageSize(pageSize: number): void
  fetch(fetcher: PageFetcher<T>, options?: FetchOptions): Promise<void>
  refresh(): Promise<void>
  cancel(): void
  startPolling(fetcher: PageFetcher<T>, options?: PollerOptions): void
  stopPolling(): void
  readonly poller: VisiblePagePoller
}

function errorFromUnknown(error: unknown): ApiError {
  if (error instanceof ApiError) return error
  return new ApiError({
    code: 'NETWORK_ERROR',
    message: error instanceof Error ? error.message : '未知请求失败',
    httpStatus: 0,
    kind: 'network',
    retryable: true,
  })
}

export function createQueryCoordinator<T>(initial: { page?: number; pageSize?: number } = {}): QueryCoordinator<T> {
  const filters = ref<FilterRecord>({}) as Ref<FilterRecord>
  const page = ref(initial.page ?? 1)
  const pageSize = ref(initial.pageSize ?? 20)
  const total = ref(0)
  const items = ref([]) as Ref<T[]>
  const status = ref<QueryStatus>('idle')
  const error = ref<ApiError | null>(null)
  const requestId = ref<string | null>(null)
  const updatedAt = ref<number | null>(null)

  const gate = new LatestRequestGate()
  let activeFetcher: PageFetcher<T> | null = null

  const isEmpty = computed(() => status.value === 'empty')
  const hasItems = computed(() => items.value.length > 0)
  const isLoading = computed(() => status.value === 'loading')
  const hasError = computed(() => error.value !== null)
  const filterSignature = computed(() => JSON.stringify(filters.value))

  function setFilter(key: string, value: FilterValue): void {
    filters.value = { ...filters.value, [key]: value }
  }

  function setFilters(patch: FilterRecord): void {
    filters.value = { ...filters.value, ...patch }
  }

  function clearFilters(): void {
    filters.value = {}
  }

  function setPage(next: number): void {
    page.value = Math.max(1, Math.floor(next))
  }

  function setPageSize(next: number): void {
    pageSize.value = Math.max(1, Math.min(100, Math.floor(next)))
  }

  /**
   * 执行一次列表请求。返回后：
   * - 最新成功 → ready/empty；失败 → error（保留筛选与已知数据）；
   * - 被更新请求取代的过期响应 → 直接丢弃（不写任何状态）；
   * - 用户取消 → status='cancelled'，筛选与分页保持不变。
   */
  async function fetch(fetcher: PageFetcher<T>, options: FetchOptions = {}): Promise<void> {
    activeFetcher = fetcher
    const context = gate.begin()
    const silent = options.silent === true
    if (!silent) {
      status.value = 'loading'
      error.value = null
    }
    try {
      const result = await fetcher({
        filters: { ...filters.value },
        page: page.value,
        pageSize: pageSize.value,
        signal: context.signal,
      })
      if (!context.isCurrent()) return // 过期响应：丢弃
      items.value = result.items
      total.value = result.total
      page.value = result.page
      pageSize.value = result.page_size
      status.value = result.items.length > 0 ? 'ready' : 'empty'
      error.value = null
      updatedAt.value = Date.now()
    } catch (thrown) {
      const apiError = errorFromUnknown(thrown)
      if (!context.isCurrent()) return // 已被更新的请求取代：不写状态
      if (apiError instanceof ApiAbortError || apiError.code === 'REQUEST_ABORTED') {
        status.value = 'cancelled'
        error.value = null
        return
      }
      error.value = apiError
      // 静默轮询失败且已有数据：保留已知数据，错误由 hasError/error 呈现（不伪装成空列）。
      status.value = silent && items.value.length > 0 ? 'ready' : 'error'
    }
  }

  /** 动作后刷新：复用最近一次 fetcher 与当前筛选。 */
  async function refresh(): Promise<void> {
    if (!activeFetcher) return
    await fetch(activeFetcher, { silent: items.value.length > 0 })
  }

  /** 取消在途请求；筛选条件与分页保持不变。 */
  function cancel(): void {
    gate.cancel()
    status.value = 'cancelled'
    error.value = null
  }

  let poller: VisiblePagePoller = createPoller({
    intervalMs: DEFAULT_POLL_INTERVAL_MS,
    immediateOnStart: false,
  })

  /**
   * 启动可见页面轮询：默认 10s，`document.hidden` 时暂停，恢复可见即刷新。
   * tick 复用同一 fetcher（当前筛选/分页），并走静默刷新与序列号守卫。
   */
  function startPolling(fetcher: PageFetcher<T>, options: PollerOptions = {}): void {
    activeFetcher = fetcher
    poller.stop()
    poller = createPoller({
      intervalMs: DEFAULT_POLL_INTERVAL_MS,
      immediateOnStart: false,
      ...options,
    })
    poller.start(
      async () => {
        await fetch(fetcher, { silent: true })
      },
      () => undefined,
    )
  }

  function stopPolling(): void {
    poller.stop()
  }

  const coordinator: QueryCoordinator<T> = {
    filters,
    page,
    pageSize,
    total,
    items,
    status,
    error,
    requestId,
    updatedAt,
    isEmpty,
    hasItems,
    isLoading,
    hasError,
    filterSignature,
    setFilter,
    setFilters,
    clearFilters,
    setPage,
    setPageSize,
    fetch,
    refresh,
    cancel,
    startPolling,
    stopPolling,
    get poller(): VisiblePagePoller {
      return poller
    },
  }
  return coordinator
}

/**
 * Pinia store：应用级默认请求协调器（T29–T33 的页面在此之上挂各自 fetcher）。
 * 仅保存内存状态，不写 localStorage。
 */
export const useQueryCoordinationStore = defineStore('query-coordination', () => {
  const coordinator = createQueryCoordinator<unknown>()
  return coordinator
})
