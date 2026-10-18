<script setup lang="ts">
/**
 * 代领表单（T32）：先明确确认，再等待唯一一条执行回执。
 *
 * 回执必须展示：execution_id、固定超时秒数、固定截止时刻。
 * 倒计时/截止只是服务端固定值，页面不提供续期，也不自行判定超时。
 */
import { computed, onMounted, ref, watch } from 'vue'
import BaseDialog from '@/components/BaseDialog.vue'
import { claimTask, type ClaimReceiptDTO, type TaskDetailDTO, type TaskListItemDTO } from '@/api/tasks'
import { assignableAgents, ensureAgentDirectory, agentById } from './boardState'
import { difficultyLabel, formatDateTimeWithZone, formatTimeoutSeconds } from './task-format'
import { useTaskAction } from './useTaskAction'
import TaskErrorNotice from './TaskErrorNotice.vue'

const props = defineProps<{ task: TaskListItemDTO; projectId: string | null; detail?: TaskDetailDTO | null }>()

const emit = defineEmits<{
  close: []
  claimed: [receipt: ClaimReceiptDTO]
  conflict: [taskId: string]
}>()

const action = useTaskAction()
const confirmed = ref(false)
const selectedAgentId = ref<string>(props.task.designated_agent_id ?? '')
const localError = ref<string>('')
const receipt = ref<ClaimReceiptDTO | null>(null)

const candidates = computed(() => assignableAgents(props.projectId))
const selectedAgent = computed(() => agentById(selectedAgentId.value))
const isAssignedMode = computed(() => props.task.claim_mode === 'ASSIGNED')

onMounted(async () => {
  await ensureAgentDirectory()
})

// 目录按页异步加载：候选到位后补齐默认代领 Agent（用户在加载中提交会看到“请选择 Agent”）。
watch(candidates, (list) => {
  if (!selectedAgentId.value || !list.some((item) => item.id === selectedAgentId.value)) {
    selectedAgentId.value = list[0]?.id ?? ''
  }
})

async function confirmClaim(): Promise<void> {
  localError.value = ''
  if (!selectedAgentId.value) {
    localError.value = '请选择要代为领取的 Agent。'
    return
  }
  if (!confirmed.value) {
    localError.value = '请先勾选确认：领取会立即开始固定执行计时。'
    return
  }
  try {
    const result = await action.run((key) =>
      claimTask(props.task.id, selectedAgentId.value, props.detail?.version ?? props.task.version, key),
    )
    if (!result) return
    if (!result.execution_id) {
      localError.value = '服务端未返回执行回执，请刷新后确认任务状态。'
      return
    }
    receipt.value = result
    confirmed.value = false
    emit('claimed', result)
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
  <BaseDialog :open="true" :title="`领取任务 ${task.display_number}`" @close="onClose">
    <div class="form-body">
      <section class="panel">
        <div class="panel-body panel-body--tight">
          <dl class="meta-list" data-testid="claim-summary">
            <dt>标题</dt>
            <dd data-testid="claim-title">{{ task.title }}</dd>
            <dt>难度</dt>
            <dd data-testid="claim-difficulty">{{ difficultyLabel(task.difficulty) }}</dd>
            <dt>领取模式</dt>
            <dd data-testid="claim-mode">{{ isAssignedMode ? '指定领取（仅指定 Agent 可领）' : '开放抢单' }}</dd>
          </dl>
        </div>
      </section>

      <p class="notice notice--warning" data-testid="claim-warning">
        领取会立即按难度生成固定执行时限并释放/占用一次容量；执行截止时刻由服务端固定，<strong>不支持续期</strong>，
        页面倒计时仅作展示。
      </p>

      <form v-if="!receipt" class="form-grid" @submit.prevent="confirmClaim">
        <label class="field">
          <span class="field-label">代为领取的 Agent<span class="field-required" aria-hidden="true">*</span></span>
          <select v-model="selectedAgentId" class="select" data-testid="claim-agent" :disabled="action.busy.value">
            <option value="">请选择</option>
            <option v-for="agent in candidates" :key="agent.id" :value="agent.id">
              {{ agent.name }}（{{ agent.role === 'SUPERVISOR' ? '主管' : '员工' }}，容量
              {{ agent.max_concurrency - agent.reserved_execution_count }}）
            </option>
          </select>
        </label>

        <p v-if="selectedAgent" class="agent-state" data-testid="claim-agent-state">
          所选 Agent：{{ selectedAgent.name }}；状态
          {{ selectedAgent.enabled ? '启用' : '已禁用' }}；在线状态
          {{ selectedAgent.online_state ?? '未知' }}（离线也可代领，但执行时限照常计时）。
        </p>

        <label class="checkbox-line">
          <input v-model="confirmed" type="checkbox" data-testid="claim-confirm-check" />
          我确认以该 Agent 身份领取该任务
        </label>

        <p v-if="localError" class="notice notice--error" role="alert" data-testid="claim-local-error">
          {{ localError }}
        </p>
        <TaskErrorNotice :error="action.error.value" />
      </form>

      <section v-else class="panel receipt" data-testid="claim-receipt" role="status">
        <div class="panel-head">
          <p class="panel-title">领取成功（唯一一条执行回执）</p>
          <span class="badge badge--success">已领取</span>
        </div>
        <div class="panel-body">
          <dl class="meta-list">
            <dt>执行 ID</dt>
            <dd data-testid="claim-execution-id"><code class="mono">{{ receipt.execution_id }}</code></dd>
            <dt>固定超时</dt>
            <dd class="tabular" data-testid="claim-execution-timeout">
              {{ formatTimeoutSeconds(receipt.execution_timeout_seconds) }}
            </dd>
            <dt>固定截止时刻</dt>
            <dd class="tabular" data-testid="claim-execution-deadline">
              {{ formatDateTimeWithZone(receipt.execution_deadline_at) }}
            </dd>
            <dt>执行者</dt>
            <dd data-testid="claim-receipt-agent"><code class="mono">{{ receipt.assignee_agent_id }}</code></dd>
          </dl>
          <p class="form-note">该截止时刻不会因配置、进度或心跳变化；超时判定由服务端完成。</p>
        </div>
      </section>
    </div>

    <template #footer>
      <button type="button" class="btn" data-testid="claim-cancel" @click="onClose">
        {{ receipt ? '关闭' : '取消' }}
      </button>
      <button
        v-if="!receipt"
        type="button"
        class="btn btn--primary"
        :disabled="action.busy.value"
        data-testid="claim-submit"
        @click="confirmClaim"
      >
        {{ action.busy.value ? '领取中…' : '确认领取' }}
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

.agent-state {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-muted);
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
}

.receipt {
  border-color: var(--color-success-border);
}

.form-note {
  margin: var(--space-3) 0 0;
  color: var(--color-text-muted);
  font-size: var(--text-xs);
}
</style>
