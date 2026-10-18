/**
 * Agent 写动作的幂等键与错误状态（T29）。
 *
 * 契约（CONTRACT.md §8、T28/D15）：
 * - 一次逻辑动作只生成一个幂等键；
 * - 网络失败 / 503 / 409 后的重试必须复用同一个键；
 * - 只有动作成功、或用户主动重新发起一次新的逻辑动作时才换键；
 * - 失败保留表单内容与错误，绝不伪装成功。
 */

import { ref, type Ref } from 'vue'
import { ApiError, newIdempotencyKey } from '@/api/client'

export interface AgentActionRunner {
  error: Ref<ApiError | null>
  busy: Ref<boolean>
  /** 当前逻辑动作使用的幂等键（仅内存，不落盘）。 */
  currentKey: Ref<string | null>
  /** 执行一次逻辑动作：首次生成键，失败后按原键重试。 */
  run<T>(action: (idempotencyKey: string) => Promise<T>): Promise<T | null>
  /** 显式重试：沿用原键。 */
  retry<T>(action: (idempotencyKey: string) => Promise<T>): Promise<T | null>
  /** 开始一次全新的逻辑动作（丢弃旧键）。 */
  reset(): void
}

export function useAgentAction(): AgentActionRunner {
  const error = ref<ApiError | null>(null)
  const busy = ref(false)
  const currentKey = ref<string | null>(null)

  async function execute<T>(action: (idempotencyKey: string) => Promise<T>): Promise<T | null> {
    if (busy.value) return null
    if (!currentKey.value) currentKey.value = newIdempotencyKey()
    const key = currentKey.value
    busy.value = true
    error.value = null
    try {
      const result = await action(key)
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
