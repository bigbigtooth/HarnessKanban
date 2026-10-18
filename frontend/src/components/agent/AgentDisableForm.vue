<script setup lang="ts">
/**
 * Agent 禁用表单（T29 / D16）。
 *
 * 服务端门禁：Agent 仍持有未结束执行（含已到期未回收）时，必须明确选择
 * `WAIT`（等固定截止）或 `NOW`（立即回收，且必须填原因），否则 400；
 * 因此表单在这两种处置上都要求明确选择，并列出受影响任务。
 * 禁用需要原因（服务端记录到审计）。
 *
 * 版式：影响面摘要 → 受影响任务表 → 处置单选组 → 原因 → 错误；
 * 取消与提交在弹窗页脚。
 */
import { computed, ref } from 'vue'
import type { ApiError } from '@/api/client'
import type { AgentExecutionBriefDTO } from '@/api/agents'
import BaseDialog from '@/components/BaseDialog.vue'
import ErrorState from '@/components/ErrorState.vue'
import { executionWaitingReclaim, formatAgentTime } from './agent-format'
const props = withDefaults(
  defineProps<{
    open: boolean
    agentName: string
    /** 该 Agent 当前未结束的执行（含已到期未回收）。 */
    executions?: AgentExecutionBriefDTO[]
    reservedCount?: number
    busy?: boolean
    error?: ApiError | null
  }>(),
  { executions: () => [], reservedCount: 0, busy: false, error: null },
)

const emit = defineEmits<{
  close: []
  submit: [payload: { disposition: 'WAIT' | 'NOW'; reason: string }]
}>()

const disposition = ref<'WAIT' | 'NOW'>('WAIT')
const reason = ref('')
const localError = ref('')

const holdsExecutions = computed(() => props.executions.length > 0)
const waitingReclaimCount = computed(
  () => props.executions.filter((item) => executionWaitingReclaim(item)).length,
)

function submit(): void {
  localError.value = ''
  const trimmed = reason.value.trim()
  if (!trimmed) {
    localError.value = '禁用必须填写原因（服务端会写入审计）。'
    return
  }
  if (holdsExecutions.value && disposition.value === 'NOW' && !trimmed) {
    localError.value = '立即回收必须填写原因。'
    return
  }
  emit('submit', { disposition: disposition.value, reason: trimmed })
}
</script>

<template>
  <BaseDialog
    :open="open"
    :title="`禁用 Agent：${agentName}`"
    description="禁用后该 Agent 的全部项目授权仍保留，但无法再领取或执行任务。"
    @close="emit('close')"
  >
    <div class="stack agent-disable" data-testid="agent-disable-form">
      <p class="notice" data-testid="agent-disable-impact-summary">
        当前未结束执行 {{ executions.length }} 个（其中等待回收 {{ waitingReclaimCount }} 个）·
        已占容量槽 {{ reservedCount }} 个
      </p>

      <template v-if="holdsExecutions">
        <div class="stack stack--tight">
          <p class="field-label">受影响的执行与任务：</p>
          <div class="table-wrap">
            <table class="table" data-testid="agent-disable-impact">
              <thead>
                <tr>
                  <th scope="col">任务</th>
                  <th scope="col">状态</th>
                  <th scope="col">难度</th>
                  <th scope="col">执行截止</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="item in executions"
                  :key="item.execution_id"
                  :data-testid="`agent-disable-impact-${item.task_id}`"
                >
                  <td class="mono">{{ item.display_number ?? item.task_id }}</td>
                  <td>
                    <span
                      v-if="executionWaitingReclaim(item)"
                      class="chip chip--warn"
                      data-testid="agent-disable-waiting"
                    >等待回收</span>
                    <span v-else class="chip chip--success">执行中</span>
                  </td>
                  <td>
                    <span class="badge badge--difficulty" :data-difficulty="item.difficulty">
                      L{{ item.difficulty }}
                    </span>
                  </td>
                  <td class="tabular text-xs" :title="item.execution_deadline_at">
                    {{ formatAgentTime(item.execution_deadline_at) }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="notice notice--warning" data-testid="agent-disable-impact-note">
            该 Agent 仍持有未结束执行，必须明确选择处置方式（服务端不接受缺省处置）。
          </p>
        </div>

        <fieldset class="disposition">
          <legend class="field-label">执行处置</legend>
          <label class="checkbox-line disposition__option">
            <input v-model="disposition" type="radio" value="WAIT" data-testid="agent-disable-disposition-wait" />
            <span>WAIT：保留执行到固定截止（槽位继续占用，到期由系统扫描回收）</span>
          </label>
          <label class="checkbox-line disposition__option">
            <input v-model="disposition" type="radio" value="NOW" data-testid="agent-disable-disposition-now" />
            <span>NOW：立即回收（任务退回待办，必须填写原因）</span>
          </label>
        </fieldset>
      </template>

      <template v-else>
        <p class="notice" data-testid="agent-disable-impact-none">
          该 Agent 当前没有未结束执行，禁用不会回收任何任务。
        </p>
      </template>

      <label class="field">
        <span class="field-label">禁用原因（必填）</span>
        <textarea v-model="reason" class="textarea" rows="3" data-testid="agent-disable-reason" />
      </label>

      <p v-if="localError" class="notice notice--error" role="alert" data-testid="agent-disable-local-error">
        {{ localError }}
      </p>
      <ErrorState v-if="error" :error="error" :show-retry="false" />
    </div>

    <template #footer>
      <button type="button" class="btn" data-testid="agent-disable-cancel" @click="emit('close')">取消</button>
      <button
        type="button"
        class="btn btn--primary"
        data-testid="agent-disable-submit"
        :disabled="busy"
        @click="submit"
      >
        {{ busy ? '提交中…' : holdsExecutions && disposition === 'NOW' ? '禁用并立即回收' : '禁用' }}
      </button>
    </template>
  </BaseDialog>
</template>

<style scoped>
.disposition {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-muted);
}

.disposition__option {
  align-items: flex-start;
  color: var(--color-text);
}

.disposition__option input[type='radio'] {
  margin: var(--space-1) 0 0;
  accent-color: var(--color-primary);
  flex: none;
}
</style>
