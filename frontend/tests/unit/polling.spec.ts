/**
 * 序列号守卫与轮询助手测试（T28：乱序/过期响应不倒退状态；隐藏暂停、不叠加）。
 */
import { describe, expect, it } from 'vitest'
import { LatestRequestGate, RequestSequence, runLatest } from '@/api/client'
import { DEFAULT_POLL_INTERVAL_MS, VisiblePagePoller, type VisibilitySource } from '@/stores/polling'
import { deferred } from './helpers/fetchStub'

interface FakeVisibility extends VisibilitySource {
  setHidden(next: boolean): void
  dispatchChange(): void
  listenerCount(): number
}

function createFakeVisibility(initialHidden = false): FakeVisibility {
  let hidden = initialHidden
  const listeners = new Set<() => void>()
  return {
    get hidden() {
      return hidden
    },
    setHidden(next: boolean) {
      hidden = next
    },
    dispatchChange() {
      for (const listener of listeners) listener()
    },
    addEventListener(_type, listener) {
      listeners.add(listener)
    },
    removeEventListener(_type, listener) {
      listeners.delete(listener)
    },
    listenerCount: () => listeners.size,
  }
}

function createManualClock() {
  const handlers: Array<() => void> = []
  let cleared = 0
  return {
    setIntervalFn: (handler: () => void) => {
      handlers.push(handler)
      return handlers.length as unknown as ReturnType<typeof setInterval>
    },
    clearIntervalFn: () => {
      cleared += 1
    },
    tick() {
      for (const handler of handlers) handler()
    },
    get clearedCount() {
      return cleared
    },
  }
}

describe('RequestSequence / LatestRequestGate', () => {
  it('序列号单调递增，旧序列不再是最新', () => {
    const sequence = new RequestSequence()
    const first = sequence.begin()
    const second = sequence.begin()
    expect(sequence.isCurrent(first)).toBe(false)
    expect(sequence.isCurrent(second)).toBe(true)
    sequence.invalidate()
    expect(sequence.isCurrent(second)).toBe(false)
  })

  it('runLatest：较旧的响应被丢弃（返回 null），较新的结果保留', async () => {
    const gate = new LatestRequestGate()
    const slow = deferred<string>()
    const fast = deferred<string>()

    const firstCall = runLatest(gate, () => slow.promise)
    const secondCall = runLatest(gate, () => fast.promise)

    fast.resolve('新的筛选结果')
    await expect(secondCall).resolves.toBe('新的筛选结果')

    slow.resolve('过期的旧结果')
    await expect(firstCall).resolves.toBeNull()
  })

  it('begin 新请求会中止上一个在途请求（AbortSignal 触发）', async () => {
    const gate = new LatestRequestGate()
    const first = gate.begin()
    expect(first.signal.aborted).toBe(false)

    gate.begin()
    expect(first.signal.aborted).toBe(true)
    expect(first.isCurrent()).toBe(false)
  })

  it('cancel 中止在途请求并使结果过期', async () => {
    const gate = new LatestRequestGate()
    const request = gate.begin()
    const pending = runLatest(gate, async () => {
      await deferred().promise
      return 'never'
    })
    gate.cancel()
    expect(request.signal.aborted).toBe(true)
    expect(request.isCancelled()).toBe(true)
    pending.catch(() => undefined)
  })
})

describe('VisiblePagePoller', () => {
  it('默认间隔为 10 秒', () => {
    const poller = new VisiblePagePoller({ visibility: null })
    expect(poller.intervalMs).toBe(DEFAULT_POLL_INTERVAL_MS)
    expect(poller.intervalMs).toBe(10_000)
  })

  it('页面隐藏时暂停：不发起轮询请求，恢复可见后立即刷新', async () => {
    const visibility = createFakeVisibility(true)
    const clock = createManualClock()
    let calls = 0
    const poller = new VisiblePagePoller({
      intervalMs: 1000,
      visibility,
      immediateOnStart: true,
      setIntervalFn: clock.setIntervalFn,
      clearIntervalFn: clock.clearIntervalFn,
    })
    poller.start(() => {
      calls += 1
    })

    await poller.settle()
    expect(calls).toBe(0)
    expect(poller.isPaused).toBe(true)
    expect(poller.state).toBe('paused-hidden')

    clock.tick()
    await poller.settle()
    expect(calls).toBe(0)

    visibility.setHidden(false)
    visibility.dispatchChange()
    await poller.settle()
    expect(calls).toBe(1)
    expect(poller.state).toBe('running')

    clock.tick()
    await poller.settle()
    expect(calls).toBe(2)

    poller.stop()
    expect(clock.clearedCount).toBe(1)
    expect(visibility.listenerCount()).toBe(0)
  })

  it('不叠加请求：上一次 tick 未结束则跳过本 tick', async () => {
    const clock = createManualClock()
    const gate = deferred()
    let calls = 0
    const poller = new VisiblePagePoller({
      intervalMs: 1000,
      visibility: null,
      immediateOnStart: false,
      setIntervalFn: clock.setIntervalFn,
      clearIntervalFn: clock.clearIntervalFn,
    })
    poller.start(async () => {
      calls += 1
      await gate.promise
    })

    clock.tick()
    clock.tick()
    clock.tick()
    await Promise.resolve()
    expect(calls).toBe(1)
    expect(poller.hasInFlightRequest).toBe(true)

    gate.resolve()
    await poller.settle()
    expect(poller.hasInFlightRequest).toBe(false)

    clock.tick()
    await poller.settle()
    expect(calls).toBe(2)

    poller.stop()
  })

  it('任务失败不中断轮询，错误交给 onError 钩子', async () => {
    const clock = createManualClock()
    const errors: unknown[] = []
    let calls = 0
    const poller = new VisiblePagePoller({
      intervalMs: 1000,
      visibility: null,
      immediateOnStart: false,
      setIntervalFn: clock.setIntervalFn,
      clearIntervalFn: clock.clearIntervalFn,
    })
    poller.start(
      () => {
        calls += 1
        throw new Error('boom')
      },
      (error) => errors.push(error),
    )

    clock.tick()
    await poller.settle()
    clock.tick()
    await poller.settle()

    expect(calls).toBe(2)
    expect(errors).toHaveLength(2)
    poller.stop()
  })
})
