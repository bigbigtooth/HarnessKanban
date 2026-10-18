/**
 * 任务动作的幂等键与错误状态（T31/T32 共用）。
 *
 * 契约（CONTRACT.md §8、D15）：
 * - 一次逻辑动作只生成一个幂等键；网络超时/503/409 重试必须复用同一个键；
 * - 只有动作**成功**（或用户主动重新打开表单）才允许换键；
 * - 失败保留表单内容并回显服务端原因，绝不伪装成功。
 */

import { ref, type Ref } from 'vue'
import { ApiError, newIdempotencyKey } from '@/api/client'

export interface TaskActionRunner {
  /** 最近一次失败（成功时清空）。 */
  error: Ref<ApiError | null>
  /** 请求进行中：期间禁止重复点击。 */
  busy: Ref<boolean>
  /** 已使用的幂等键（用于展示/调试，不落盘）。 */
  currentKey: Ref<string | null>
  /** 复用一个新键执行动作；成功时清空错误与键，失败时保留键以便按原键重试。 */
  run<T>(task: (idempotencyKey: string) => Promise<T>): Promise<T | null>
  /** 人工重试：沿用原键再执行同一个动作。 */
  retry<T>(task: (idempotencyKey: string) => Promise<T>): Promise<T | null>
  reset(): void
}

export function useTaskAction(): TaskActionRunner {
  const error = ref<ApiError | null>(null)
  const busy = ref(false)
  const currentKey = ref<string | null>(null)

  async function execute<T>(task: (idempotencyKey: string) => Promise<T>): Promise<T | null> {
    if (busy.value) return null
    if (!currentKey.value) currentKey.value = newIdempotencyKey()
    const key = currentKey.value
    busy.value = true
    error.value = null
    try {
      const result = await task(key)
      // 成功：本次逻辑动作结束，换键（下一次动作是新动作）。
      currentKey.value = null
      error.value = null
      return result
    } catch (thrown) {
      error.value = thrown instanceof ApiError ? thrown : null
      throw thrown
    } finally {
      busy.value = false
    }
  }

  return {
    error,
    busy,
    currentKey,
    run: execute,
    retry: execute,
    reset() {
      error.value = null
      currentKey.value = null
      busy.value = false
    },
  }
}
