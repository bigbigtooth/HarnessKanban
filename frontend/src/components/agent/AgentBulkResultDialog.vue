<script setup lang="ts">
/**
 * 批量操作回执弹窗（T30）：逐项成功 / 失败。
 *
 * 服务端对批量删除与批量恢复都返回同一结构（`domain/bulk.py:bulk_result`）：
 * `{ operation, requested, succeeded[], failed[] }`，**一项失败不影响其他项**，
 * 因此这里必须把每项失败的原因与错误码原样列出，绝不把「部分失败」显示成整体成功。
 *
 * 版式：摘要 → 失败项（原因 + 错误码 + details）→ 成功项 → 页脚关闭。
 */
import { computed } from 'vue'
import type { AgentBulkResultDTO } from '@/api/agents'
import BaseDialog from '@/components/BaseDialog.vue'
import {
  bulkFailureDetailsText,
  bulkFailureText,
  bulkOperationLabel,
  bulkResultSummaryText,
  formatAgentTime,
} from './agent-format'

const props = withDefaults(
  defineProps<{
    open: boolean
    result?: AgentBulkResultDTO | null
    /** id → 名称：让管理员看到名字而不是裸 ID。 */
    names?: Record<string, string>
  }>(),
  { result: null, names: () => ({}) },
)

const emit = defineEmits<{ close: [] }>()

const operationLabel = computed(() => bulkOperationLabel(props.result?.operation))
const succeeded = computed(() => props.result?.succeeded ?? [])
const failed = computed(() => props.result?.failed ?? [])
const isRestore = computed(() => props.result?.operation === 'agent.bulk_restore')

function displayName(id: string): string {
  return props.names[id] || id
}
</script>

<template>
  <BaseDialog
    :open="open"
    :title="`${operationLabel}结果`"
    description="逐项结果：一项失败不影响其他项。"
    size="lg"
    @close="emit('close')"
  >
    <div v-if="result" class="stack agent-bulk-result" data-testid="agent-bulk-result">
      <p
        class="notice"
        :class="failed.length > 0 ? 'notice--warning' : 'notice--success'"
        role="status"
        data-testid="agent-bulk-result-summary"
      >
        {{ bulkResultSummaryText(result) }}
      </p>

      <section v-if="failed.length > 0" class="section">
        <div class="section-head">
          <h4 class="section-title">失败项（{{ failed.length }}）</h4>
          <p class="section-hint">按服务端返回的原因与错误码原样列出，修正后可重新提交。</p>
        </div>
        <ul class="bulk-list" data-testid="agent-bulk-result-failed">
          <li
            v-for="item in failed"
            :key="item.id"
            class="bulk-item"
            :data-testid="`agent-bulk-result-failed-${item.id}`"
          >
            <div class="row row--wrap">
              <span class="strong" :data-testid="`agent-bulk-result-failed-name-${item.id}`">
                {{ displayName(item.id) }}
              </span>
              <span class="badge badge--danger" data-testid="agent-bulk-result-failed-code">
                {{ item.code }}
              </span>
            </div>
            <p class="bulk-item__message" data-testid="agent-bulk-result-failed-message">
              {{ bulkFailureText(item) }}
            </p>
            <p
              v-if="bulkFailureDetailsText(item.details)"
              class="mono text-xs muted bulk-item__details"
              data-testid="agent-bulk-result-failed-details"
            >
              {{ bulkFailureDetailsText(item.details) }}
            </p>
          </li>
        </ul>
      </section>

      <section v-if="succeeded.length > 0" class="section">
        <div class="section-head">
          <h4 class="section-title">成功项（{{ succeeded.length }}）</h4>
        </div>
        <ul class="bulk-list" data-testid="agent-bulk-result-succeeded">
          <li
            v-for="item in succeeded"
            :key="item.id"
            class="bulk-item bulk-item--ok"
            :data-testid="`agent-bulk-result-succeeded-${item.id}`"
          >
            <div class="row row--wrap">
              <span class="strong" :data-testid="`agent-bulk-result-succeeded-name-${item.id}`">
                {{ displayName(item.id) }}
              </span>
              <span class="badge badge--success">{{ isRestore ? '已恢复' : '已删除' }}</span>
              <span v-if="!isRestore && item.deleted_at" class="tabular text-xs muted">
                删除于 {{ formatAgentTime(item.deleted_at) }}
              </span>
            </div>
          </li>
        </ul>
      </section>

      <p class="section-hint">列表已自动刷新；失败项的服务端原因见上方。</p>
    </div>

    <template #footer>
      <button type="button" class="btn btn--primary" data-testid="agent-bulk-result-close" @click="emit('close')">
        关闭
      </button>
    </template>
  </BaseDialog>
</template>

<style scoped>
.bulk-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  list-style: none;
  margin: 0;
  padding: 0;
}

.bulk-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-danger-border);
  border-radius: var(--radius-md);
  background: var(--color-danger-soft);
}

.bulk-item--ok {
  border-color: var(--color-border);
  background: var(--color-surface-muted);
}

.bulk-item__message {
  margin: 0;
  color: var(--color-danger);
  font-size: var(--text-sm);
}

.bulk-item__details {
  margin: 0;
  word-break: break-all;
}
</style>
