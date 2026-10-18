<script setup lang="ts">
/**
 * 领取安排表单（T31）：OPEN/ASSIGNED 与指定 Agent。
 * 仅准备中/待办可用；指定动作不创建执行、不占容量、不开始计时（服务端保证）。
 */
import { computed, onMounted, ref } from 'vue'
import BaseDialog from '@/components/BaseDialog.vue'
import { assignTask, type TaskListItemDTO, type TaskMutationDTO } from '@/api/tasks'
import { ClaimMode } from '@/api/types'
import { assignableAgents, ensureAgentDirectory } from './boardState'
import { useTaskAction } from './useTaskAction'
import TaskErrorNotice from './TaskErrorNotice.vue'

const props = defineProps<{ task: TaskListItemDTO; projectId: string | null }>()

const emit = defineEmits<{
  close: []
  saved: [task: TaskMutationDTO]
  conflict: [taskId: string]
}>()

const action = useTaskAction()
const claimMode = ref<ClaimMode>(props.task.claim_mode)
const designatedAgentId = ref<string>(props.task.designated_agent_id ?? '')
const localError = ref('')

const candidates = computed(() => assignableAgents(props.projectId))

onMounted(() => {
  void ensureAgentDirectory()
})

async function submit(): Promise<void> {
  localError.value = ''
  if (claimMode.value === ClaimMode.ASSIGNED && !designatedAgentId.value) {
    localError.value = '指定领取必须选择一名指定的 Agent。'
    return
  }
  if (
    claimMode.value === props.task.claim_mode &&
    designatedAgentId.value === (props.task.designated_agent_id ?? '')
  ) {
    localError.value = '领取安排没有变化。'
    return
  }
  try {
    const result = await action.run((key) =>
      assignTask(
        props.task.id,
        props.task.version,
        claimMode.value,
        claimMode.value === ClaimMode.ASSIGNED ? designatedAgentId.value : null,
        key,
      ),
    )
    if (!result) return
    emit('saved', result)
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
  <BaseDialog :open="true" :title="`修改领取安排 ${task.display_number}`" @close="onClose">
    <div class="form-body">
      <p class="notice notice--info" data-testid="assign-note">
        指定只是“谁可以领”，不会创建执行记录、不占用容量、也不开始计时；指定动作完成后任务仍是待办。
      </p>

      <form class="form-grid" @submit.prevent="submit">
        <label class="field">
          <span class="field-label">领取模式</span>
          <select v-model="claimMode" class="select" data-testid="assign-claim-mode">
            <option :value="ClaimMode.OPEN">开放抢单（授权 Agent 竞争）</option>
            <option :value="ClaimMode.ASSIGNED">指定领取（仅指定 Agent）</option>
          </select>
        </label>

        <label v-if="claimMode === ClaimMode.ASSIGNED" class="field">
          <span class="field-label">指定 Agent<span class="field-required" aria-hidden="true">*</span></span>
          <select v-model="designatedAgentId" class="select" data-testid="assign-designated">
            <option value="">请选择</option>
            <option v-for="agent in candidates" :key="agent.id" :value="agent.id">
              {{ agent.name }}（{{ agent.role === 'SUPERVISOR' ? '主管' : '员工' }}）
            </option>
          </select>
        </label>

        <p v-if="localError" class="notice notice--error" role="alert" data-testid="assign-local-error">
          {{ localError }}
        </p>
        <TaskErrorNotice :error="action.error.value" />
      </form>
    </div>

    <template #footer>
      <button type="button" class="btn" data-testid="assign-cancel" @click="onClose">取消</button>
      <button
        type="button"
        class="btn btn--primary"
        :disabled="action.busy.value"
        data-testid="assign-submit"
        @click="submit"
      >
        {{ action.busy.value ? '提交中…' : '保存领取安排' }}
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
