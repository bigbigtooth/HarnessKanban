/**
 * 可见页面轮询助手（T28）。
 *
 * 依据 D15：可见页面默认 10 秒轮询，`document.hidden` 时暂停，动作后可手动刷新；
 * 轮询不得叠加在途请求（同一 tick 若上次未结束直接跳过）。
 */

export interface VisibilitySource {
  readonly hidden: boolean
  addEventListener(type: 'visibilitychange', listener: () => void): void
  removeEventListener(type: 'visibilitychange', listener: () => void): void
}

export type PollerState = 'stopped' | 'running' | 'paused-hidden'

export interface PollerOptions {
  /** 轮询间隔，默认 10000ms。 */
  intervalMs?: number
  /** 可见性来源；默认为浏览器 document（无 document 时为始终可见）。 */
  visibility?: VisibilitySource | null
  /** 启动时是否立即执行一次，默认 true。 */
  immediateOnStart?: boolean
  /** 从隐藏恢复为可见时是否立即执行一次，默认 true。 */
  refreshOnVisible?: boolean
  setIntervalFn?: (handler: () => void, timeout: number) => ReturnType<typeof setInterval>
  clearIntervalFn?: (handle: ReturnType<typeof setInterval>) => void
}

export const DEFAULT_POLL_INTERVAL_MS = 10_000

function defaultVisibility(): VisibilitySource | null {
  if (typeof document === 'undefined') return null
  return document as unknown as VisibilitySource
}

export class VisiblePagePoller {
  readonly intervalMs: number
  private readonly visibility: VisibilitySource | null
  private readonly immediateOnStart: boolean
  private readonly refreshOnVisible: boolean
  private readonly setIntervalFn: (handler: () => void, timeout: number) => ReturnType<typeof setInterval>
  private readonly clearIntervalFn: (handle: ReturnType<typeof setInterval>) => void

  private timer: ReturnType<typeof setInterval> | null = null
  private running = false
  private inFlight: Promise<void> | null = null
  private task: (() => void | Promise<void>) | null = null
  private lastError: unknown = null
  private onErrorHandler: ((error: unknown) => void) | null = null
  /** 用户显式暂停（独立于 document.hidden）。 */
  private manuallyPaused = false

  constructor(options: PollerOptions = {}) {
    this.intervalMs = options.intervalMs ?? DEFAULT_POLL_INTERVAL_MS
    this.visibility = options.visibility === undefined ? defaultVisibility() : options.visibility
    this.immediateOnStart = options.immediateOnStart ?? true
    this.refreshOnVisible = options.refreshOnVisible ?? true
    this.setIntervalFn = options.setIntervalFn ?? ((handler, timeout) => setInterval(handler, timeout))
    this.clearIntervalFn = options.clearIntervalFn ?? ((handle) => clearInterval(handle))
  }

  get state(): PollerState {
    if (!this.running) return 'stopped'
    if (this.isHidden() || this.manuallyPaused) return 'paused-hidden'
    return 'running'
  }

  get isRunning(): boolean {
    return this.running
  }

  /** 当前是否因页面隐藏或手动暂停而停止取数。 */
  get isPaused(): boolean {
    return this.isHidden() || this.manuallyPaused
  }

  get hasInFlightRequest(): boolean {
    return this.inFlight !== null
  }

  get currentError(): unknown {
    return this.lastError
  }

  start(task: () => void | Promise<void>, onError?: (error: unknown) => void): void {
    this.stop()
    this.task = task
    this.onErrorHandler = onError ?? null
    this.running = true
    this.manuallyPaused = false
    this.visibility?.addEventListener('visibilitychange', this.handleVisibilityChange)
    this.timer = this.setIntervalFn(() => {
      void this.tick()
    }, this.intervalMs)
    if (this.immediateOnStart && !this.isPaused) {
      void this.tick()
    }
  }

  stop(): void {
    this.running = false
    this.manuallyPaused = false
    this.task = null
    this.visibility?.removeEventListener('visibilitychange', this.handleVisibilityChange)
    if (this.timer !== null) {
      this.clearIntervalFn(this.timer)
      this.timer = null
    }
  }

  /** 显式暂停（保留定时器与任务，恢复即继续）。 */
  pause(): void {
    this.manuallyPaused = true
  }

  resume(): void {
    this.manuallyPaused = false
    if (this.running && !this.isHidden()) void this.tick()
  }

  /** 手动触发一次（动作后刷新）；仍遵守可见性与“不叠加”约束。 */
  async trigger(): Promise<void> {
    await this.tick()
  }

  /** 等待当前在途 tick 结束（测试与关闭流程使用）。 */
  async settle(): Promise<void> {
    await (this.inFlight ?? Promise.resolve())
  }

  private isHidden(): boolean {
    return this.visibility?.hidden === true
  }

  private handleVisibilityChange = (): void => {
    if (!this.running) return
    if (this.refreshOnVisible && !this.isHidden() && !this.manuallyPaused) {
      void this.tick()
    }
  }

  private async tick(): Promise<void> {
    if (!this.running || !this.task) return
    if (this.isPaused) return
    if (this.inFlight) return // 上一次请求未结束：跳过本 tick，不叠加
    const task = this.task
    let release!: () => void
    const token = new Promise<void>((resolve) => {
      release = resolve
    })
    this.inFlight = token
    try {
      await task()
      this.lastError = null
    } catch (error) {
      this.lastError = error
      this.onErrorHandler?.(error)
    } finally {
      this.inFlight = null
      release()
    }
  }
}

/** 创建可见页面轮询器（默认 10s，隐藏时暂停）。 */
export function createPoller(options: PollerOptions = {}): VisiblePagePoller {
  return new VisiblePagePoller(options)
}
