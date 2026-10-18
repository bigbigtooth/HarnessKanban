<script setup lang="ts">
/**
 * 回收执行表单（T32）：进行中 → 待办。
 *
 * 契约说明：`release`（主动释放）是执行者自助动作，Web 来源固定 403 ROLE_FORBIDDEN，
 * 因此本地管理台只提供「回收（RECLAIM）」并在此明确说明，不假装能代替 Agent 释放。
 * 回收必须填写原因，并携带当前 execution_id 与 expected_version（单次结束、只释放一次槽）。
 */
import { computed, ref } from 'vue'
import BaseDialog from '@/components/BaseDialog.vue'
import { reclaimTask, type ExecutionEndReceiptDTO, type TaskDetailDTO, type TaskListItemDTO } from '@/api/tasks'
import { endReasonLabel, formatDateTimeWithZone, formatRemaining } from './task-format'
import { useTaskAction } from './useTaskAction'
import TaskErrorNotice from './TaskErrorNotice.vue'

const props = defineProps<{
  task: TaskListItemDTO
  detail: TaskDetailDTO | null
  nowMs: number
}>()

const emit = defineEmits<{
  close: []
  ended: [receipt: ExecutionEndReceiptDTO]
  conflict: [taskId: string]
}>()

const action = useTaskAction()
const reason = ref('')
const localError = ref('')
const receipt = ref<ExecutionEndReceiptDTO | null>(null)

const execution = computed(() => props.detail?.current_execution ?? null)
const deadline = computed(() => execution.value?.execution_deadline_at ?? null)
const awaitingReclaim = computed(() => {
  if (!deadline.value) return false
  const parsed = new Date(deadline.value).getTime()
  return !Number.isNaN(parsed) && parsed <= props.nowMs
})

async function submit(): Promise<void> {
  localError.value = ''
  if (!execution.value) {
    localError.value = '当前没有有效执行，无法回收（请刷新任务状态）。'
    return
  }
  if (!reason.value.trim()) {
    localError.value = '回收必须填写原因。'
    return
  }
  try {
    const result = await action.run((key) =>
      reclaimTask(
        props.task.id,
        execution.value!.execution_id,
        props.detail?.version ?? props.task.version,
        reason.value.trim(),
        key,
      ),
    )
    if (!result) return
    receipt.value = result
    emit('ended', result)
  } catch {
    // 失败保留输入；冲突（409）时通知调用方回读服务端状态。
    if (action.error.value?.isConflict) emit('conflict', props.task.id)
  }
}

function onClose(): void {
  action.reset()
  emit('close')
}
</script>

<template>
  <BaseDialog :open="true" :title="`回收执行 ${task.display_number}`" @close="onClose">
    <div class="form-body">
      <section class="panel">
        <div class="panel-head">
          <p class="panel-title">当前执行</p>
          <span class="badge" :class="awaitingReclaim ? 'badge--warning' : 'badge--info'">
            {{ awaitingReclaim ? '已到服务端固定截止时刻，等待回收' : '尚未到截止时刻' }}
          </span>
        </div>
        <div class="panel-body panel-body--tight">
          <dl class="meta-list" data-testid="reclaim-summary">
            <dt>当前执行 ID</dt>
            <dd data-testid="reclaim-execution-id">
              <code class="mono">{{ execution?.execution_id ?? '无' }}</code>
            </dd>
            <dt>执行者</dt>
            <dd data-testid="reclaim-agent">{{ execution?.agent_id ?? '—' }}</dd>
            <dt>固定截止时刻</dt>
            <dd class="tabular" data-testid="reclaim-deadline">
              {{ deadline ? `${formatDateTimeWithZone(deadline)}（${formatRemaining(deadline, nowMs)}）` : '—' }}
            </dd>
            <dt>超时状态</dt>
            <dd data-testid="reclaim-timeout-state">
              {{ awaitingReclaim ? '已到服务端固定截止时刻，等待回收' : '尚未到截止时刻' }}
            </dd>
          </dl>
        </div>
      </section>

      <p class="notice notice--info" data-testid="release-note">
        「主动释放」只能由持有该执行的 Agent 通过 AgentAPI 发起（本地 Web 来源会被 403 拒绝），
        管理台只能执行强制回收；回收会结束本轮执行并释放一次容量槽，任务退回待办。
      </p>

      <form v-if="!receipt" class="form-grid" @submit.prevent="submit">
        <label class="field">
          <span class="field-label">回收原因（必填）<span class="field-required" aria-hidden="true">*</span></span>
          <textarea v-model="reason" class="textarea" rows="3" data-testid="reclaim-reason" />
        </label>

        <p v-if="localError" class="notice notice--error" role="alert" data-testid="reclaim-local-error">
          {{ localError }}
        </p>
        <TaskErrorNotice :error="action.error.value" />
      </form>

      <section v-else class="panel receipt" role="status" data-testid="reclaim-receipt">
        <div class="panel-head">
          <p class="panel-title">已回收：任务回到待办，需要重新领取</p>
          <span class="badge badge--success">已回收</span>
        </div>
        <div class="panel-body">
          <dl class="meta-list">
            <dt>执行 ID</dt>
            <dd data-testid="reclaim-receipt-execution"><code class="mono">{{ receipt.execution_id }}</code></dd>
            <dt>结束原因</dt>
            <dd data-testid="reclaim-receipt-reason">{{ endReasonLabel(receipt.end_reason) }}</dd>
            <dt>任务状态</dt>
            <dd data-testid="reclaim-receipt-status">{{ receipt.status }}</dd>
          </dl>
        </div>
      </section>
    </div>

    <template #footer>
      <button type="button" class="btn" data-testid="reclaim-cancel" @click="onClose">
        {{ receipt ? '关闭' : '取消' }}
      </button>
      <button
        v-if="!receipt"
        type="button"
        class="btn btn--danger"
        :disabled="action.busy.value"
        data-testid="reclaim-submit"
        @click="submit"
      >
        {{ action.busy.value ? '回收中…' : '确认回收' }}
      </button>
    </template>
  </BaseDialog>
</template>

<style scoped>
.form-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.receipt {
  border-color: var(--color-success-border);
}
</style>
