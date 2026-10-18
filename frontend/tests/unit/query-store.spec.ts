/**
 * 请求协调 store 测试：失败不显示为空列表、取消保留筛选、乱序不倒退状态、隐藏暂停轮询。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { ApiError } from '@/api/client'
import type { Page } from '@/api/types'
import { createQueryCoordinator, useQueryCoordinationStore, type CoordinatorContext } from '@/stores/query'
import { deferred, type Deferred } from './helpers/fetchStub'

interface NavRow {
  id: string
  status: string
}

function page(items: NavRow[], overrides: Partial<Page<NavRow>> = {}): Page<NavRow> {
  return { items, total: items.length, page: 1, page_size: 20, ...overrides }
}

describe('createQueryCoordinator', () => {
  it('成功加载：ready + 数据落位；空数组 → empty（而不是错误）', async () => {
    const coordinator = createQueryCoordinator<NavRow>()

    await coordinator.fetch(async () => page([{ id: 't1', status: 'TODO' }]))
    expect(coordinator.status.value).toBe('ready')
    expect(coordinator.items.value).toHaveLength(1)
    expect(coordinator.error.value).toBeNull()
    expect(coordinator.updatedAt.value).not.toBeNull()

    await coordinator.fetch(async () => page([]))
    expect(coordinator.status.value).toBe('empty')
    expect(coordinator.items.value).toHaveLength(0)
  })

  it('请求失败：status=error 且透出错误码，绝不伪装成空列表', async () => {
    const coordinator = createQueryCoordinator<NavRow>()

    await coordinator.fetch(async () => {
      throw new ApiError({
        code: 'DATABASE_BUSY',
        message: '数据库繁忙',
        httpStatus: 503,
        requestId: 'req-busy-1',
        kind: 'retryable',
        retryable: true,
      })
    })

    expect(coordinator.status.value).toBe('error')
    expect(coordinator.isEmpty.value).toBe(false)
    expect(coordinator.error.value?.code).toBe('DATABASE_BUSY')
    expect(coordinator.error.value?.requestId).toBe('req-busy-1')
    expect(coordinator.hasError.value).toBe(true)
  })

  it('取消请求保留筛选条件，且迟到的响应不得写回状态', async () => {
    const coordinator = createQueryCoordinator<NavRow>()
    coordinator.setFilter('status', 'TODO')
    coordinator.setFilter('project_id', 'p1')
    const gate = deferred<Page<NavRow>>()

    const pending = coordinator.fetch(async () => gate.promise)
    expect(coordinator.status.value).toBe('loading')

    coordinator.cancel()
    expect(coordinator.status.value).toBe('cancelled')
    expect(coordinator.filters.value).toEqual({ status: 'TODO', project_id: 'p1' })
    expect(coordinator.page.value).toBe(1)
    expect(coordinator.error.value).toBeNull()

    gate.resolve(page([{ id: 'late', status: 'TODO' }]))
    await pending
    expect(coordinator.items.value).toHaveLength(0)
    expect(coordinator.status.value).toBe('cancelled')
    expect(coordinator.filters.value).toEqual({ status: 'TODO', project_id: 'p1' })
  })

  it('乱序响应：过期结果被丢弃，新筛选的数据保留', async () => {
    const coordinator = createQueryCoordinator<NavRow>()
    const slow: Deferred<Page<NavRow>> = deferred()
    const fast: Deferred<Page<NavRow>> = deferred()
    const received: CoordinatorContext[] = []

    coordinator.setFilter('status', 'TODO')
    const first = coordinator.fetch(async (context) => {
      received.push(context)
      return slow.promise
    })

    coordinator.setFilter('status', 'DONE')
    const second = coordinator.fetch(async (context) => {
      received.push(context)
      return fast.promise
    })

    fast.resolve(page([{ id: 'newer', status: 'DONE' }]))
    await second
    expect(coordinator.items.value).toEqual([{ id: 'newer', status: 'DONE' }])
    expect(coordinator.status.value).toBe('ready')

    slow.resolve(page([{ id: 'stale', status: 'TODO' }]))
    await first

    expect(coordinator.items.value).toEqual([{ id: 'newer', status: 'DONE' }])
    expect(coordinator.filters.value).toEqual({ status: 'DONE' })
    expect(received[0]!.filters).toEqual({ status: 'TODO' })
    expect(received[1]!.filters).toEqual({ status: 'DONE' })
  })

  it('静默轮询失败：保留已有数据并记录错误（不闪回空列）', async () => {
    const coordinator = createQueryCoordinator<NavRow>()
    await coordinator.fetch(async () => page([{ id: 't1', status: 'TODO' }]))

    await coordinator.fetch(async () => {
      throw new ApiError({ code: 'NETWORK_ERROR', message: '连接失败', httpStatus: 0, kind: 'network', retryable: true })
    }, { silent: true })

    expect(coordinator.items.value).toHaveLength(1)
    expect(coordinator.status.value).toBe('ready')
    expect(coordinator.error.value?.code).toBe('NETWORK_ERROR')
  })

  it('隐藏页面暂停轮询、恢复后继续取数（不叠加请求）', async () => {
    const coordinator = createQueryCoordinator<NavRow>()
    let fetches = 0
    const handlers: Array<() => void> = []
    const visibility = {
      hidden: true,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }

    coordinator.startPolling(
      async () => {
        fetches += 1
        return page([])
      },
      {
        intervalMs: 1000,
        visibility,
        immediateOnStart: true,
        setIntervalFn: (handler: () => void) => {
          handlers.push(handler)
          return handlers.length as unknown as ReturnType<typeof setInterval>
        },
        clearIntervalFn: () => undefined,
      },
    )

    handlers.forEach((handler) => handler())
    await coordinator.poller.settle()
    expect(fetches).toBe(0)
    expect(coordinator.poller.isPaused).toBe(true)

    visibility.hidden = false
    handlers.forEach((handler) => handler())
    await coordinator.poller.settle()
    expect(fetches).toBe(1)
    expect(coordinator.status.value).toBe('empty')

    coordinator.stopPolling()
  })
})

describe('useQueryCoordinationStore（Pinia）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('store 暴露筛选、分页与页面状态 API，并可完成一次真实 fetch 流程', async () => {
    const store = useQueryCoordinationStore()
    expect(store.status).toBe('idle')
    expect(store.filters).toEqual({})

    store.setFilters({ status: 'TODO', project_id: 'p1' })
    store.setPageSize(50)
    expect(store.filters).toEqual({ status: 'TODO', project_id: 'p1' })
    expect(store.pageSize).toBe(50)

    await store.fetch(async () => ({
      items: [{ id: 't1' }],
      total: 1,
      page: 1,
      page_size: 50,
    }))

    expect(store.status).toBe('ready')
    expect(store.items).toHaveLength(1)
    expect(store.total).toBe(1)
    expect(store.hasItems).toBe(true)
  })
})
