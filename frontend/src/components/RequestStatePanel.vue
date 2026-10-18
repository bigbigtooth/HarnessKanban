<script setup lang="ts">
/**
 * 请求状态面板：把协调 store 的状态映射为显式 UI 状态。
 * loading → 加载态；empty → 空态；error → 错误态（含重试）；cancelled → 可重新加载。
 * ready/idle 时不渲染任何占位（由页面渲染真实内容）。
 */
import type { ApiError } from '@/api/client'
import LoadingState from './LoadingState.vue'
import EmptyState from './EmptyState.vue'
import ErrorState from './ErrorState.vue'

withDefaults(
  defineProps<{
    status: 'idle' | 'loading' | 'ready' | 'empty' | 'error' | 'cancelled'
    error?: ApiError | null
    loadingLabel?: string
    emptyTitle?: string
    emptyDescription?: string
    retryLabel?: string
  }>(),
  {
    error: null,
    loadingLabel: '加载中…',
    emptyTitle: '暂无数据',
    emptyDescription: '',
    retryLabel: '重试',
  },
)

const emit = defineEmits<{ retry: [] }>()
</script>

<template>
  <LoadingState v-if="status === 'loading'" :label="loadingLabel" />
  <ErrorState
    v-else-if="status === 'error'"
    :error="error ?? null"
    :retry-label="retryLabel"
    @retry="emit('retry')"
  />
  <EmptyState v-else-if="status === 'empty'" :title="emptyTitle" :description="emptyDescription" />
  <div v-else-if="status === 'cancelled'" class="state-block state-cancelled" role="status" data-testid="panel-cancelled">
    <p>请求已取消。筛选条件保持不变。</p>
    <button type="button" class="btn btn--sm" data-testid="panel-reload" @click="emit('retry')">重新加载</button>
  </div>
</template>

<style scoped>
.state-cancelled {
  color: var(--color-text-secondary);
}
</style>
