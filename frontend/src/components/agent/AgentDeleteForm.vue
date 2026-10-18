<script setup lang="ts">
/**
 * Agent 删除确认弹窗（T30）：单条与批量共用。
 *
 * 服务端门禁（`agent_service._change_agent_availability`，与禁用同一套校验）：
 * - 删除**必须填原因**，否则 400 `INVALID_ARGUMENT`；
 * - 目标仍持有有效执行时必须明确选择 `WAIT`（等待到期）或 `NOW`（立即回收，且必须有原因）；
 * - 目标是某进行中项目的最后一名主管时返回 409/403，页面原样展示服务端原因。
 *
 * 处置单选只在「已知目标持有未结束执行」或「未取到目标执行明细（批量 / 读取失败）」时出现，
 * 与 AgentDisableForm 的语义与措辞保持一致；受影响任务逐条列出。
 *
 * 版式：目标 → 影响面摘要 → 受影响任务表 → 处置单选 → 删除后果提示 → 原因 → 错误；
 * 取消与提交在弹窗页脚。
 */
import { computed, ref, watch } from 'vue'
import type { ApiError } from '@/api/client'
import type { AgentExecutionBriefDTO } from '@/api/agents'
import BaseDialog from '@/components/BaseDialog.vue'
import ErrorState from '@/components/ErrorState.vue'
import { executionWaitingReclaim, formatAgentTime } from './agent-format'

const props = withDefaults(
  defineProps<{
    open: boolean
    /** 单条 / 批量：决定标题与提交后的处理（批量走逐项结果）。 */
    mode?: 'single' | 'bulk'
    /** 待删目标（单条 1 个；批量按选中顺序）。 */
    targets?: { id: string; name: string }[]
    /**
     * 目标当前的未结束执行：已知时用于影响面与是否需要处置；
     * `null` = 未取到（批量操作或详情读取失败），此时也要求明确处置。
     */
    executions?: AgentExecutionBriefDTO[] | null
    /** 目标已占容量槽合计。 */
    reservedCount?: number
    busy?: boolean
    error?: ApiError | null
  }>(),
  { mode: 'single', targets: () => [], executions: null, reservedCount: 0, busy: false, error: null },
)

const emit = defineEmits<{
  close: []
  submit: [payload: { reason: string; reclaim: 'WAIT' | 'NOW' | null }]
}>()

const disposition = ref<'WAIT' | 'NOW'>('WAIT')
const reason = ref('')
const localError = ref('')

const isBulk = computed(() => props.mode === 'bulk' || props.targets.length > 1)
const singleName = computed(() => props.targets[0]?.name ?? '')
const title = computed(() =>
  isBulk.value ? `批量删除 Agent（${props.targets.length} 项）` : `删除 Agent：${singleName.value}`,
)
const description = computed(() =>
  isBulk.value
    ? '逐项执行：一项失败不影响其他项，结果会逐项列出。'
    : '删除是软删除：可随时恢复，历史全部保留。',
)

const executionsKnown = computed(() => props.executions !== null)
const executions = computed<AgentExecutionBriefDTO[]>(() => props.executions ?? [])
const waitingReclaimCount = computed(
  () => executions.value.filter((item) => executionWaitingReclaim(item)).length,
)
/** 已知持有执行、或未取到明细时都必须让操作人明确处置（服务端不接受缺省处置）。 */
const showDisposition = computed(() => !executionsKnown.value || executions.value.length > 0)

function resetForm(): void {
  disposition.value = 'WAIT'
  reason.value = ''
  localError.value = ''
}

watch(
  () => props.open,
  (open) => {
    if (open) resetForm()
  },
)

function submit(): void {
  localError.value = ''
  const trimmed = reason.value.trim()
  if (!trimmed) {
    localError.value = '删除必须填写原因（服务端会写入审计）。'
    return
  }
  emit('submit', { reason: trimmed, reclaim: showDisposition.value ? disposition.value : null })
}
</script>

<template>
  <BaseDialog :open="open" :title="title" :description="description" @close="emit('close')">
    <div class="stack agent-delete" data-testid="agent-delete-form">
      <ul v-if="isBulk" class="chip-list" data-testid="agent-delete-targets">
        <li v-for="target in targets" :key="target.id" class="chip" :data-testid="`agent-delete-target-${target.id}`">
          {{ target.name }}
        </li>
      </ul>

      <p class="notice" data-testid="agent-delete-impact-summary">
        <template v-if="executionsKnown">
          当前未结束执行 {{ executions.length }} 个（其中等待回收 {{ waitingReclaimCount }} 个）·
          已占容量槽 {{ reservedCount }} 个
        </template>
        <template v-else>
          未取到目标当前的执行明细 · 已占容量槽合计 {{ reservedCount }} 个
        </template>
      </p>

      <template v-if="executionsKnown && executions.length > 0">
        <div class="stack stack--tight">
          <p class="field-label">受影响的执行与任务：</p>
          <div class="table-wrap">
            <table class="table" data-testid="agent-delete-impact">
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
                  :data-testid="`agent-delete-impact-${item.task_id}`"
                >
                  <td class="mono">{{ item.display_number ?? item.task_id }}</td>
                  <td>
                    <span
                      v-if="executionWaitingReclaim(item)"
                      class="chip chip--warn"
                      data-testid="agent-delete-waiting"
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
        </div>
      </template>

      <p v-if="showDisposition" class="notice notice--warning" data-testid="agent-delete-disposition-note">
        <template v-if="executionsKnown">
          目标仍持有未结束执行，必须明确选择处置方式（服务端不接受缺省处置）。
        </template>
        <template v-else>
          未读取到目标当前的执行明细；若任一目标仍持有有效执行，必须明确选择处置方式（服务端不接受缺省处置）。
        </template>
      </p>

      <fieldset v-if="showDisposition" class="disposition">
        <legend class="field-label">执行处置</legend>
        <label class="checkbox-line disposition__option">
          <input
            v-model="disposition"
            type="radio"
            value="WAIT"
            data-testid="agent-delete-disposition-wait"
          />
          <span>WAIT：保留等待到期（槽位继续占用，到期由系统扫描回收）</span>
        </label>
        <label class="checkbox-line disposition__option">
          <input
            v-model="disposition"
            type="radio"
            value="NOW"
            data-testid="agent-delete-disposition-now"
          />
          <span>NOW：立即回收（任务退回待办，必须填写原因）</span>
        </label>
      </fieldset>

      <p class="notice notice--info" data-testid="agent-delete-effect">
        删除后从列表、候选与统计中消失，Key 立即失效；执行、提交与审核历史保留，可恢复。
      </p>

      <label class="field">
        <span class="field-label">删除原因<span class="field-required" aria-hidden="true">*</span></span>
        <textarea v-model="reason" class="textarea" rows="3" data-testid="agent-delete-reason" />
      </label>

      <p v-if="localError" class="notice notice--error" role="alert" data-testid="agent-delete-local-error">
        {{ localError }}
      </p>
      <ErrorState v-if="error" :error="error" :show-retry="false" />
    </div>

    <template #footer>
      <button type="button" class="btn" data-testid="agent-delete-cancel" @click="emit('close')">取消</button>
      <button
        type="button"
        class="btn btn--danger"
        data-testid="agent-delete-submit"
        :disabled="busy"
        @click="submit"
      >
        {{
          busy
            ? '删除中…'
            : isBulk
              ? `确认删除（${targets.length} 项）`
              : showDisposition && disposition === 'NOW'
                ? '删除并立即回收'
                : '确认删除'
        }}
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

.chip-list {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  list-style: none;
  margin: 0;
  padding: 0;
}
</style>
