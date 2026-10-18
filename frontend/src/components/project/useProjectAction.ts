/**
 * 项目写动作的幂等键与错误状态（T30）。
 *
 * 契约（CONTRACT.md §8、T28/D15）：一次逻辑动作一个幂等键；失败重试沿用原键；
 * 只有成功（或用户重新发起新动作）才换键；失败保留表单内容与错误。
 */

import { ref, type Ref } from 'vue'
import { ApiError, newIdempotencyKey } from '@/api/client'

export interface ProjectActionRunner {
  error: Ref<ApiError | null>
  busy: Ref<boolean>
  currentKey: Ref<string | null>
  run<T>(action: (idempotencyKey: string) => Promise<T>): Promise<T | null>
  retry<T>(action: (idempotencyKey: string) => Promise<T>): Promise<T | null>
  reset(): void
}

export function useProjectAction(): ProjectActionRunner {
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
