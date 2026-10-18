<script setup lang="ts">
/**
 * 原因类动作表单（T31/T32）：
 * - `withdraw`（待办 → 准备中，原因必填，有当前执行时服务端会拒绝）；
 * - `reopen`（已完成 → 待办，原因必填，需项目进行中，历史保留）；
 * - `archive`（独立标记，不改五态，原因必填）；
 * - `restore`（清除归档标记，无原因字段）。
 */
import { computed, ref } from 'vue'
import BaseDialog from '@/components/BaseDialog.vue'
import {
  archiveTask,
  reopenTask,
  restoreTask,
  withdrawTask,
  type ReopenReceiptDTO,
  type TaskMutationDTO,
  type TaskListItemDTO,
} from '@/api/tasks'
import { useTaskAction } from './useTaskAction'
import TaskErrorNotice from './TaskErrorNotice.vue'

type ReasonMode = 'withdraw' | 'reopen' | 'archive' | 'restore'

const props = defineProps<{ mode: ReasonMode; task: TaskListItemDTO }>()

const emit = defineEmits<{
  close: []
  saved: [task: TaskMutationDTO | null, mode: ReasonMode]
  reopened: [receipt: ReopenReceiptDTO]
  conflict: [taskId: string]
}>()

const action = useTaskAction()
const reason = ref('')
const localError = ref('')
const done = ref(false)

const config = computed(() => {
  switch (props.mode) {
    case 'withdraw':
      return {
        title: `撤回发布 ${props.task.display_number}`,
        note: '撤回后任务回到「准备中」；若存在当前执行，服务端会拒绝，必须先回收执行。',
        requireReason: true,
        confirm: '确认撤回',
        doneText: '已撤回：任务回到准备中',
        noteTone: 'notice--warning',
        submitTone: 'btn--danger',
      }
    case 'reopen':
      return {
        title: `重新打开 ${props.task.display_number}`,
        note: '重新打开后任务回到「待办」，本轮完成与审核历史保留，新一轮必须重新领取。项目必须处于进行中。',
        requireReason: true,
        confirm: '确认重新打开',
        doneText: '已重新打开：任务回到待办',
        noteTone: 'notice--info',
        submitTone: 'btn--primary',
      }
    case 'archive':
      return {
        title: `归档 ${props.task.display_number}`,
        note: '归档只是隐藏标记，不改变五态状态；有有效执行或处于待审核时服务端会拒绝。',
        requireReason: true,
        confirm: '确认归档',
        doneText: '已归档（状态未改变）',
        noteTone: 'notice--warning',
        submitTone: 'btn--danger',
      }
    default:
      return {
        title: `恢复归档 ${props.task.display_number}`,
        note: '恢复只清除归档标记；项目必须允许写操作。',
        requireReason: false,
        confirm: '确认恢复',
        doneText: '已恢复归档标记',
        noteTone: 'notice--info',
        submitTone: 'btn--primary',
      }
  }
})

async function submit(): Promise<void> {
  localError.value = ''
  if (config.value.requireReason && !reason.value.trim()) {
    localError.value = '必须填写原因。'
    return
  }
  try {
    if (props.mode === 'withdraw') {
      const result = await action.run((key) =>
        withdrawTask(props.task.id, props.task.version, reason.value.trim(), key),
      )
      if (!result) return
      done.value = true
      emit('saved', result, 'withdraw')
      return
    }
    if (props.mode === 'reopen') {
      const result = await action.run((key) =>
        reopenTask(props.task.id, props.task.version, reason.value.trim(), key),
      )
      if (!result) return
      done.value = true
      emit('reopened', result)
      return
    }
    if (props.mode === 'archive') {
      const result = await action.run((key) =>
        archiveTask(props.task.id, props.task.version, reason.value.trim(), key),
      )
      if (!result) return
      done.value = true
      emit('saved', result, 'archive')
      return
    }
    const result = await action.run((key) => restoreTask(props.task.id, props.task.version, key))
    if (!result) return
    done.value = true
    emit('saved', result, 'restore')
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
  <BaseDialog :open="true" :title="config.title" @close="onClose">
    <div class="form-body">
      <p class="notice" :class="config.noteTone" data-testid="reason-note">{{ config.note }}</p>

      <form v-if="!done" class="form-grid" @submit.prevent="submit">
        <label v-if="config.requireReason" class="field">
          <span class="field-label">原因（必填）<span class="field-required" aria-hidden="true">*</span></span>
          <textarea v-model="reason" class="textarea" rows="3" data-testid="reason-input" />
        </label>

        <p v-if="localError" class="notice notice--error" role="alert" data-testid="reason-local-error">
          {{ localError }}
        </p>
        <TaskErrorNotice :error="action.error.value" />
      </form>

      <p v-else class="notice notice--success" role="status" data-testid="reason-done">{{ config.doneText }}</p>
    </div>

    <template #footer>
      <button type="button" class="btn" data-testid="reason-cancel" @click="onClose">
        {{ done ? '关闭' : '取消' }}
      </button>
      <button
        v-if="!done"
        type="button"
        class="btn"
        :class="config.submitTone"
        :disabled="action.busy.value"
        data-testid="reason-submit"
        @click="submit"
      >
        {{ action.busy.value ? '提交中…' : config.confirm }}
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
</style>
