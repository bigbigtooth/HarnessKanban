<script setup lang="ts">
/**
 * 进度更新表单（T32）：只写当前执行的简短消息与可选百分比。
 * 进度不改变固定截止时刻，也不因 percent=100 隐式提交或完成任务（服务端同样保证）。
 */
import { computed, ref } from 'vue'
import BaseDialog from '@/components/BaseDialog.vue'
import { updateProgress, type ProgressReceiptDTO, type TaskDetailDTO, type TaskListItemDTO } from '@/api/tasks'
import { formatDateTimeWithZone, formatRemaining } from './task-format'
import { useTaskAction } from './useTaskAction'
import TaskErrorNotice from './TaskErrorNotice.vue'

const props = defineProps<{
  task: TaskListItemDTO
  detail: TaskDetailDTO | null
  nowMs: number
}>()

const emit = defineEmits<{
  close: []
  updated: [receipt: ProgressReceiptDTO]
  conflict: [taskId: string]
}>()

const action = useTaskAction()
const message = ref(props.detail?.current_execution?.progress_message ?? '')
const percent = ref<string | number>(
  props.detail?.current_execution?.progress_percent == null
    ? ''
    : String(props.detail.current_execution.progress_percent),
)
const localError = ref('')
const receipt = ref<ProgressReceiptDTO | null>(null)

const execution = computed(() => props.detail?.current_execution ?? null)
const deadline = computed(() => execution.value?.execution_deadline_at ?? null)

async function submit(): Promise<void> {
  localError.value = ''
  if (!execution.value) {
    localError.value = '当前没有有效执行，无法更新进度。'
    return
  }
  let parsedPercent: number | null = null
  // `v-model` 在 type=number 输入上会绑定数字（空值为 ''），这里统一按文本判断再转数字。
  const percentText =
    percent.value == null ? '' : typeof percent.value === 'string' ? percent.value.trim() : String(percent.value)
  if (percentText) {
    const value = Number(percentText)
    if (!Number.isInteger(value) || value < 0 || value > 100) {
      localError.value = '进度百分比必须是 0 到 100 的整数。'
      return
    }
    parsedPercent = value
  }
  try {
    const result = await action.run((key) =>
      updateProgress(props.task.id, execution.value!.execution_id, message.value, parsedPercent, key),
    )
    if (!result) return
    receipt.value = result
    emit('updated', result)
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
  <BaseDialog :open="true" :title="`更新进度 ${task.display_number}`" @close="onClose">
    <div class="form-body">
      <p class="notice notice--info" data-testid="progress-note">
        进度只用于展示；固定执行截止时刻不会因为进度或心跳变化（{{ deadline ? formatDateTimeWithZone(deadline) : '无当前执行' }}，{{ formatRemaining(deadline, nowMs) }}，服务端判定）。
      </p>

      <form v-if="!receipt" class="form-grid" @submit.prevent="submit">
        <label class="field">
          <span class="field-label">进度消息</span>
          <textarea v-model="message" class="textarea" rows="2" data-testid="progress-message" />
        </label>

        <label class="field">
          <span class="field-label">百分比（可选，0–100 整数）</span>
          <input
            v-model="percent"
            class="input"
            type="number"
            min="0"
            max="100"
            step="1"
            data-testid="progress-percent"
          />
        </label>

        <p v-if="localError" class="notice notice--error" role="alert" data-testid="progress-local-error">
          {{ localError }}
        </p>
        <TaskErrorNotice :error="action.error.value" />
      </form>

      <section v-else class="panel receipt" role="status" data-testid="progress-receipt">
        <div class="panel-head">
          <p class="panel-title">进度已记录</p>
          <span class="badge badge--success">已保存</span>
        </div>
        <div class="panel-body">
          <dl class="meta-list">
            <dt>进度消息</dt>
            <dd data-testid="progress-receipt-message">{{ receipt.progress_message || '（空）' }}</dd>
            <dt>百分比</dt>
            <dd class="tabular" data-testid="progress-receipt-percent">{{ receipt.progress_percent ?? '未设置' }}</dd>
          </dl>
        </div>
      </section>
    </div>

    <template #footer>
      <button type="button" class="btn" data-testid="progress-cancel" @click="onClose">
        {{ receipt ? '关闭' : '取消' }}
      </button>
      <button
        v-if="!receipt"
        type="button"
        class="btn btn--primary"
        :disabled="action.busy.value"
        data-testid="progress-submit"
        @click="submit"
      >
        {{ action.busy.value ? '提交中…' : '保存进度' }}
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
