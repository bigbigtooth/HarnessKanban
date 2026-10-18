<script setup lang="ts">
/**
 * 错误态：始终显示可理解的原因与重试入口；绝不退化为“暂无数据”。
 * 展示 code / message / request_id，供本地管理员定位（不展示堆栈、SQL、路径）。
 */
import { computed } from 'vue'
import type { ApiError } from '@/api/client'

const props = withDefaults(
  defineProps<{
    error: ApiError | null
    retryLabel?: string
    showRetry?: boolean
  }>(),
  { retryLabel: '重试', showRetry: true },
)

const emit = defineEmits<{ retry: [] }>()

const message = computed(() => props.error?.message ?? '请求失败')
const code = computed(() => props.error?.code ?? '')
const requestId = computed(() => props.error?.requestId ?? '')
const hint = computed(() => {
  if (!props.error) return ''
  if (props.error.kind === 'conflict') return '当前对象已被其他操作更新，请刷新后重试，已填写的内容会为您保留。'
  if (props.error.retryable) return '服务暂时繁忙，请稍后按原操作重试（不会重复提交）。'
  return ''
})
</script>

<template>
  <div class="state-block state-error" role="alert" data-testid="state-error">
    <p class="state-title">加载失败</p>
    <p class="state-message" data-testid="state-error-message">{{ message }}</p>
    <p v-if="hint" class="state-hint">{{ hint }}</p>
    <dl class="state-meta">
      <template v-if="code">
        <dt>错误码</dt>
        <dd data-testid="state-error-code">{{ code }}</dd>
      </template>
      <template v-if="requestId">
        <dt>请求号</dt>
        <dd data-testid="state-error-request-id">{{ requestId }}</dd>
      </template>
    </dl>
    <button v-if="showRetry" type="button" class="btn btn--sm" data-testid="state-retry" @click="emit('retry')">
      {{ retryLabel }}
    </button>
  </div>
</template>

<style scoped>
.state-error {
  background: var(--color-danger-soft);
  border-color: var(--color-danger-border);
}
</style>
