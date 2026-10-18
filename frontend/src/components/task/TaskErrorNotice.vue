<script setup lang="ts">
/**
 * 任务动作错误提示：显示服务端原因、错误码、请求号与处理建议。
 * 409/冲突不改变用户已填写的内容，只提示并让调用方回读最新状态。
 */
import { computed } from 'vue'
import type { ApiError } from '@/api/client'
import { describeApiError } from './task-format'

const props = defineProps<{ error: ApiError | null }>()

const explanation = computed(() => (props.error ? describeApiError(props.error) : null))
const isConflict = computed(() => props.error?.kind === 'conflict')
</script>

<template>
  <div
    v-if="explanation"
    class="notice notice--error action-error"
    role="alert"
    :data-testid="isConflict ? 'action-conflict' : 'action-error'"
  >
    <span class="action-error__mark" aria-hidden="true">!</span>
    <div class="action-error__body">
      <p class="action-error__title">
        {{ isConflict ? '操作冲突（已保留填写内容）' : '操作失败（已保留填写内容）' }}
      </p>
      <p class="action-error__message" data-testid="action-error-message">{{ explanation.message }}</p>
      <p v-if="explanation.hint" class="action-error__hint" data-testid="action-error-hint">
        {{ explanation.hint }}
      </p>
      <p class="action-error__meta">
        错误码 <code class="mono" data-testid="action-error-code">{{ explanation.code }}</code>
        <template v-if="explanation.requestId">
          ；请求号 <code class="mono" data-testid="action-error-request-id">{{ explanation.requestId }}</code>
        </template>
      </p>
    </div>
  </div>
</template>

<style scoped>
.action-error {
  align-items: flex-start;
}

.action-error__mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.1rem;
  height: 1.1rem;
  margin-top: 0.15rem;
  border-radius: var(--radius-pill);
  background: var(--color-danger);
  color: var(--color-text-inverse);
  font-size: var(--text-xs);
  font-weight: var(--weight-bold);
  line-height: 1;
  flex: none;
}

.action-error__body {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

.action-error__title {
  margin: 0;
  font-weight: var(--weight-semibold);
}

.action-error__message,
.action-error__hint,
.action-error__meta {
  margin: 0;
}

.action-error__hint {
  color: var(--color-text-secondary);
}

.action-error__meta {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.action-error__meta code {
  color: var(--color-text-secondary);
}
</style>
