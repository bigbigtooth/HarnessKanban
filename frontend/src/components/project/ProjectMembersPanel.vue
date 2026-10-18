<script setup lang="ts">
/**
 * 项目成员面板（T30 / D08）。
 *
 * - 列表展示授权成员（角色取自 Agent，成员关系只表达授权）；
 * - 新增成员走 `PUT /projects/{id}/members/{agent_id}`，不能授权已禁用 Agent（服务端 409/403 原样回显）；
 * - 移除成员前先读取该 Agent 在当前项目的未结束执行，列出受影响任务，
 *   并要求在 WAIT（等固定截止）与 NOW（立即回收，原因必填）之间做显式选择；
 * - 归档项目只读：不提供新增/移除入口。
 *
 * 视觉：成员用 `.table` + 角色徽标；授权、最后一名主管保护、撤权处置等提示统一走 `.notice`；
 * 移除授权是危险动作，用 `.btn--danger`（只读时仍渲染但禁用，保证键盘/自动化路径一致）。
 */
import { computed, ref, watch } from 'vue'
import type { ApiError } from '@/api/client'
import type { AgentExecutionBriefDTO, AgentSummaryDTO } from '@/api/agents'
import { getAgent } from '@/api/agents'
import type { ProjectMemberDTO } from '@/api/projects'
import type { AgentRole } from '@/api/types'
import BaseDialog from '@/components/BaseDialog.vue'
import { onlineStateLabel } from '@/components/agent/agent-format'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import { agentRoleLabel, executionWaitingReclaim } from '@/components/agent/agent-format'
import { ERROR_HINTS } from './project-format'

const props = withDefaults(
  defineProps<{
    projectId: string
    members: ProjectMemberDTO[]
    agents: AgentSummaryDTO[]
    loading?: boolean
    error?: ApiError | null
    readOnly?: boolean
    busy?: boolean
    /** 当前 Agent 搜索关键词（服务端按名称过滤）。 */
    agentSearch?: string
    /** 移除动作的失败信息（来自父组件的幂等 runner）。 */
    removalError?: ApiError | null
    /** 每次成功移除后父组件递增：面板据此关闭确认框。 */
    removalToken?: number
  }>(),
  {
    loading: false,
    error: null,
    readOnly: false,
    busy: false,
    agentSearch: '',
    removalError: null,
    removalToken: 0,
  },
)

const emit = defineEmits<{
  add: [agentId: string]
  /** 按名称搜索可授权 Agent（服务端过滤，避免只看到前 100 个）。 */
  search: [keyword: string]
  /** 移除成员：disposition 仅在受影响执行存在时必填。 */
  remove: [payload: { agentId: string; disposition: 'WAIT' | 'NOW' | null; reason: string }]
  refresh: []
}>()

/** 主管 / 员工的角色徽标：主管用品牌色，员工用中性色，文字始终由 `agentRoleLabel` 表达。 */
function roleBadgeClass(role: AgentRole | string): string {
  return role === 'SUPERVISOR' ? 'badge--primary' : 'badge--neutral'
}

/** 最后一名主管保护：直接复用服务端拒绝时展示的同一句提示文案。 */
const lastSupervisorNotice = ERROR_HINTS.LAST_SUPERVISOR_REQUIRED

const selectedAgentId = ref('')
const addError = ref('')
const searchDraft = ref(props.agentSearch)

watch(
  () => props.agentSearch,
  (value) => {
    searchDraft.value = value
  },
)

function submitSearch(): void {
  emit('search', searchDraft.value.trim())
}

const memberIds = computed(() => new Set(props.members.map((member) => member.agent_id)))
const addableAgents = computed(() => props.agents.filter((agent) => !memberIds.value.has(agent.id)))

// ------------------------------------------------------------------ 移除流程
const removeTarget = ref<ProjectMemberDTO | null>(null)
const removeImpact = ref<AgentExecutionBriefDTO[]>([])
const removeImpactLoading = ref(false)
const removeDisposition = ref<'WAIT' | 'NOW'>('WAIT')
const removeReason = ref('')
const removeLocalError = ref('')

async function openRemove(member: ProjectMemberDTO): Promise<void> {
  removeTarget.value = member
  removeImpact.value = []
  removeDisposition.value = 'WAIT'
  removeReason.value = ''
  removeLocalError.value = ''
  removeImpactLoading.value = true
  try {
    // 真实读取该 Agent 的当前执行，并按项目过滤：撤权影响面以服务端事实为准。
    const detail = await getAgent(member.agent_id)
    removeImpact.value = detail.current_executions.filter(
      (brief) => brief.project_id === props.projectId,
    )
  } catch {
    removeImpact.value = []
  } finally {
    removeImpactLoading.value = false
  }
}

function closeRemove(): void {
  removeTarget.value = null
  removeImpact.value = []
  removeLocalError.value = ''
}

function confirmRemove(): void {
  if (!removeTarget.value) return
  removeLocalError.value = ''
  const holdsExecutions = removeImpact.value.length > 0
  if (holdsExecutions && removeDisposition.value === 'NOW' && !removeReason.value.trim()) {
    removeLocalError.value = '立即回收必须填写原因。'
    return
  }
  emit('remove', {
    agentId: removeTarget.value.agent_id,
    disposition: holdsExecutions ? removeDisposition.value : null,
    reason: removeReason.value.trim(),
  })
}

function addMember(): void {
  addError.value = ''
  if (!selectedAgentId.value) {
    addError.value = '请先选择要授权的 Agent。'
    return
  }
  emit('add', selectedAgentId.value)
  selectedAgentId.value = ''
}

// 父组件成功移除后递增 token：关闭确认框（失败时保持打开以便按原键重试）。
watch(
  () => props.removalToken,
  (token, previous) => {
    if (token !== previous && token > 0) closeRemove()
  },
)
</script>

<template>
  <section class="panel" data-testid="project-members">
    <header class="panel-head">
      <h4 class="panel-title">项目成员（{{ members.length }}）</h4>
      <button type="button" class="btn btn--sm" data-testid="project-members-refresh" @click="emit('refresh')">
        刷新成员
      </button>
    </header>

    <div class="panel-body stack">
      <p v-if="readOnly" class="notice notice--warning" data-testid="project-members-readonly">
        项目已归档：成员授权只读，恢复项目后才能调整。
      </p>
      <p v-else class="notice notice--info">{{ lastSupervisorNotice }}</p>

      <LoadingState v-if="loading" label="加载成员…" />
      <ErrorState v-else-if="error" :error="error" @retry="emit('refresh')" />
      <EmptyState
        v-else-if="members.length === 0"
        title="暂无成员"
        description="项目需要至少一名已启用主管才能启用。"
      />
      <div v-else class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th scope="col">成员</th>
              <th scope="col">角色</th>
              <th scope="col">启用状态</th>
              <th scope="col">在线状态</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody data-testid="project-member-list">
            <tr v-for="member in members" :key="member.id" :data-testid="`project-member-${member.agent_id}`">
              <td>
                <div class="row">
                  <span class="avatar" aria-hidden="true">{{ member.agent_name.slice(0, 1) }}</span>
                  <span class="strong truncate">{{ member.agent_name }}</span>
                </div>
              </td>
              <td>
                <span class="badge" :class="roleBadgeClass(member.role)">
                  {{ agentRoleLabel(member.role) }}
                </span>
              </td>
              <td>
                <span class="badge" :class="member.enabled ? 'badge--success' : 'badge--neutral'">
                  {{ member.enabled ? '已启用' : '已禁用' }}
                </span>
              </td>
              <td>
                <span class="badge badge--neutral">
                  {{ onlineStateLabel(member.online_state) }}
                </span>
              </td>
              <td>
                <button
                  type="button"
                  class="btn btn--sm btn--danger"
                  :data-testid="`project-member-remove-${member.agent_id}`"
                  :disabled="readOnly || busy"
                  @click="openRemove(member)"
                >
                  移除授权
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p
        v-if="addError"
        class="notice notice--error"
        role="alert"
        data-testid="project-member-add-error"
      >
        {{ addError }}
      </p>
    </div>

    <div v-if="!readOnly" class="panel-foot members-add">
      <label class="field members-add__field">
        <span class="field-label">按名称搜索 Agent</span>
        <input
          v-model="searchDraft"
          class="input"
          type="text"
          data-testid="project-member-agent-search"
          placeholder="输入名称关键字"
          @keyup.enter="submitSearch"
        />
      </label>
      <button type="button" class="btn" data-testid="project-member-agent-search-apply" @click="submitSearch">
        查询 Agent
      </button>
      <label class="field members-add__field">
        <span class="field-label">新增授权 Agent</span>
        <select v-model="selectedAgentId" class="select" data-testid="project-member-add-select" :disabled="busy">
          <option value="">请选择…</option>
          <option v-for="agent in addableAgents" :key="agent.id" :value="agent.id">
            {{ agent.name }}（{{ agentRoleLabel(agent.role) }}{{ agent.enabled ? '' : '，已禁用' }}）
          </option>
        </select>
      </label>
      <button type="button" class="btn btn--primary" data-testid="project-member-add" :disabled="busy" @click="addMember">
        授权加入项目
      </button>
    </div>

    <BaseDialog
      :open="removeTarget !== null"
      :title="`移除授权：${removeTarget?.agent_name ?? ''}`"
      description="撤权立即生效；该成员在本项目的未结束执行按所选处置处理。"
      @close="closeRemove"
    >
      <div class="stack" data-testid="project-member-remove-form">
        <LoadingState v-if="removeImpactLoading" label="正在读取该成员的未结束执行…" />

        <template v-else-if="removeImpact.length > 0">
          <p class="strong">
            受影响任务 {{ removeImpact.length }} 个（该成员在本项目仍有未结束执行）：
          </p>
          <ul class="impact-list" data-testid="project-member-remove-impact">
            <li v-for="brief in removeImpact" :key="brief.execution_id">
              <span class="mono">{{ brief.display_number ?? brief.task_id }}</span>
              <span v-if="executionWaitingReclaim(brief)" class="badge badge--warning">等待回收</span>
              <span v-else class="badge badge--info">执行中</span>
              <span class="muted text-sm">（L{{ brief.difficulty }}，截止 {{ brief.execution_deadline_at }}）</span>
            </li>
          </ul>
          <fieldset class="disposition">
            <legend class="field-label">执行处置</legend>
            <label class="checkbox-line">
              <input v-model="removeDisposition" type="radio" value="WAIT" data-testid="project-member-disposition-wait" />
              WAIT：授权立即撤销，执行保留到固定截止
            </label>
            <label class="checkbox-line">
              <input v-model="removeDisposition" type="radio" value="NOW" data-testid="project-member-disposition-now" />
              NOW：立即回收（任务退回待办，必须填写原因）
            </label>
          </fieldset>
        </template>

        <p v-else class="notice notice--info" data-testid="project-member-remove-impact-none">
          该成员在本项目没有未结束执行，移除授权不会回收任务。
        </p>

        <label class="field">
          <span class="field-label">原因（立即回收时必填）</span>
          <textarea
            v-model="removeReason"
            class="textarea"
            rows="2"
            data-testid="project-member-remove-reason"
          />
        </label>

        <p
          v-if="removeLocalError"
          class="notice notice--error"
          role="alert"
          data-testid="project-member-remove-local-error"
        >
          {{ removeLocalError }}
        </p>
        <ErrorState v-if="removalError" :error="removalError" :show-retry="false" />
      </div>

      <template #footer>
        <button type="button" class="btn" data-testid="project-member-remove-cancel" @click="closeRemove">
          取消
        </button>
        <button
          type="button"
          class="btn btn--danger"
          data-testid="project-member-remove-confirm"
          :disabled="busy"
          @click="confirmRemove"
        >
          {{ busy ? '提交中…' : '确认移除授权' }}
        </button>
      </template>
    </BaseDialog>
  </section>
</template>

<style scoped>
/* 授权表单：与表格同一面板，用分隔线区分“只读列表 / 写操作”两块。 */
.members-add {
  align-items: flex-end;
  justify-content: flex-start;
  gap: var(--space-3);
  background: var(--color-surface-muted);
}

.members-add__field {
  flex: 1 1 220px;
}

.impact-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
  padding: 0;
  list-style: none;
}

.impact-list li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

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

.disposition legend {
  padding: 0 var(--space-1);
}
</style>
