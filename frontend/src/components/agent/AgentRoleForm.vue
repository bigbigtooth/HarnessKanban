<script setup lang="ts">
/**
 * Agent 角色变更表单（T29 / D08）。
 *
 * 角色属于 Agent（全局），不属于项目成员关系；变更走 `POST /agents/{id}/role`，
 * 服务端会在同一事务内执行“最后主管保护”检查，被拒绝时页面回显服务端原因。
 *
 * 版式：当前角色/版本元数据 → 目标角色选择 → 错误；取消与提交在弹窗页脚。
 */
import { ref, watch } from 'vue'
import type { ApiError } from '@/api/client'
import { AgentRole } from '@/api/types'
import type { AgentDetailDTO } from '@/api/agents'
import BaseDialog from '@/components/BaseDialog.vue'
import ErrorState from '@/components/ErrorState.vue'
import { agentRoleLabel } from './agent-format'

const props = withDefaults(
  defineProps<{
    open: boolean
    agent: AgentDetailDTO | null
    busy?: boolean
    error?: ApiError | null
  }>(),
  { busy: false, error: null },
)

const emit = defineEmits<{ close: []; submit: [role: AgentRole] }>()

const role = ref<AgentRole>(AgentRole.WORKER)
const localError = ref('')

watch(
  () => [props.open, props.agent?.role] as const,
  ([open, currentRole]) => {
    if (open) {
      role.value = (currentRole as AgentRole | undefined) ?? AgentRole.WORKER
      localError.value = ''
    }
  },
  { immediate: true },
)

function submit(): void {
  localError.value = ''
  if (!props.agent) {
    localError.value = '缺少 Agent 详情，无法变更角色。'
    return
  }
  if (role.value === props.agent.role) {
    localError.value = `角色未变化（当前已是${agentRoleLabel(props.agent.role)}）。`
    return
  }
  emit('submit', role.value)
}
</script>

<template>
  <BaseDialog
    :open="open"
    :title="`变更角色：${agent?.name ?? ''}`"
    description="角色是 Agent 的全局属性；主管会被要求在每个进行中项目保留至少一名已启用主管。"
    @close="emit('close')"
  >
    <div class="stack agent-role" data-testid="agent-role-form">
      <dl class="meta-list">
        <dt>当前角色</dt>
        <dd data-testid="agent-role-current">{{ agent ? agentRoleLabel(agent.role) : '—' }}</dd>
        <dt>当前版本</dt>
        <dd class="tabular" data-testid="agent-role-version">{{ agent?.version ?? '—' }}</dd>
      </dl>

      <label class="field">
        <span class="field-label">目标角色</span>
        <select v-model="role" class="select" data-testid="agent-role-select">
          <option :value="AgentRole.SUPERVISOR">主管（可发布/审核）</option>
          <option :value="AgentRole.WORKER">员工</option>
        </select>
      </label>

      <p v-if="localError" class="notice notice--error" role="alert" data-testid="agent-role-local-error">
        {{ localError }}
      </p>
      <ErrorState v-if="error" :error="error" :show-retry="false" />
    </div>

    <template #footer>
      <button type="button" class="btn" data-testid="agent-role-cancel" @click="emit('close')">取消</button>
      <button type="button" class="btn btn--primary" data-testid="agent-role-submit" :disabled="busy" @click="submit">
        {{ busy ? '提交中…' : '提交角色变更' }}
      </button>
    </template>
  </BaseDialog>
</template>

<style scoped>
.agent-role {
  min-width: 0;
}
</style>
