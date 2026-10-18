<script setup lang="ts">
/**
 * 执行与容量、Agent 状态（T33 / D17）。
 *
 * “工作中”与“已占容量槽”并列展示：到期未回收的执行不计入工作中，但仍然占槽，
 * 因此单独列出“等待回收”。数字全部来自 `execution_counts` / `agent_counts` / `agents`。
 * 线上状态由服务端判定（禁用优先于在线），前端只做展示，不按本地时钟重算。
 */
import type { DashboardDTO } from '@/api/dashboard'
import { ONLINE_STATE_LABEL, agentDisplayState, agentRoleLabel } from '@/components/agent/agent-format'

defineProps<{ dashboard: DashboardDTO }>()

type AgentRow = DashboardDTO['agents'][number]

function stateOf(agent: AgentRow) {
  return agentDisplayState({
    enabled: agent.enabled,
    online_state: agent.online_state as never,
  })
}

function stateLabel(agent: AgentRow): string {
  return ONLINE_STATE_LABEL[stateOf(agent)]
}

/** 头像取名字首字（“主管-张三”取“张”）；仅装饰，状态与名称文字仍然完整可见。 */
function agentInitial(name: string): string {
  const tail = name.trim().split('-').pop() ?? ''
  return (tail.trim() || name.trim()).slice(0, 1) || '?'
}
</script>

<template>
  <div class="stack stack--loose">
    <section class="panel" data-testid="dashboard-workload">
      <div class="panel-head">
        <h3 class="panel-title">执行与容量</h3>
        <span class="section-hint">有效执行与已占容量槽分开统计，等待回收单独可见</span>
      </div>

      <div class="panel-body stack">
        <div class="stat-grid">
          <div class="stat stat--compact">
            <span class="stat__label">工作中（有效执行）</span>
            <span class="stat__value" data-testid="dashboard-effective-executions">
              {{ dashboard.execution_counts.effective_executions }}
            </span>
          </div>
          <div class="stat stat--compact">
            <span class="stat__label">已占容量槽</span>
            <span class="stat__value" data-testid="dashboard-reserved-slots">
              {{ dashboard.execution_counts.reserved_slots }}
            </span>
          </div>
          <div
            class="stat stat--compact"
            :class="{ 'stat--warning': dashboard.execution_counts.waiting_reclaim_executions > 0 }"
          >
            <span class="stat__label">等待回收</span>
            <span class="stat__value" data-testid="dashboard-waiting-reclaim">
              {{ dashboard.execution_counts.waiting_reclaim_executions }}
            </span>
          </div>
          <div class="stat stat--compact">
            <span class="stat__label">未结束执行（含等待回收）</span>
            <span class="stat__value" data-testid="dashboard-open-executions">
              {{ dashboard.execution_counts.open_executions }}
            </span>
          </div>
          <div class="stat stat--compact">
            <span class="stat__label">可用容量</span>
            <span class="stat__value" data-testid="dashboard-capacity-available">
              {{ dashboard.execution_counts.capacity_available }} / {{ dashboard.execution_counts.capacity_total }}
            </span>
          </div>
        </div>

        <p
          class="notice"
          :class="{ 'notice--warning': dashboard.execution_counts.waiting_reclaim_executions > 0 }"
          data-testid="dashboard-workload-hint"
        >
          等待回收的执行已过固定截止、仍未结束：它们不再计入“工作中”，但<strong>仍占用容量槽</strong>，需要主管或系统回收。
        </p>
      </div>
    </section>

    <section class="panel" data-testid="dashboard-agent-status">
      <div class="panel-head">
        <h3 class="panel-title">Agent 状态</h3>
        <span class="section-hint">在线状态由服务端判定，已禁用优先于在线状态</span>
      </div>

      <div class="panel-body stack">
        <dl class="stat-grid" data-testid="dashboard-agent-counts">
          <div class="stat stat--compact">
            <dt class="stat__label">Agent 总数</dt>
            <dd class="stat__value" data-testid="dashboard-agent-total">{{ dashboard.agent_counts.total }}</dd>
          </div>
          <div class="stat stat--compact">
            <dt class="stat__label">在线</dt>
            <dd class="stat__value" data-testid="dashboard-agent-online">{{ dashboard.agent_counts.online }}</dd>
          </div>
          <div class="stat stat--compact">
            <dt class="stat__label">工作中</dt>
            <dd class="stat__value" data-testid="dashboard-agent-working">{{ dashboard.agent_counts.working }}</dd>
          </div>
          <div class="stat stat--compact">
            <dt class="stat__label">离线</dt>
            <dd class="stat__value" data-testid="dashboard-agent-offline">{{ dashboard.agent_counts.offline }}</dd>
          </div>
          <div class="stat stat--compact">
            <dt class="stat__label">未连接</dt>
            <dd class="stat__value" data-testid="dashboard-agent-never">{{ dashboard.agent_counts.never_connected }}</dd>
          </div>
          <div class="stat stat--compact">
            <dt class="stat__label">已禁用</dt>
            <dd class="stat__value" data-testid="dashboard-agent-disabled">{{ dashboard.agent_counts.disabled }}</dd>
          </div>
        </dl>

        <div v-if="dashboard.agents.length > 0" class="table-wrap">
          <table class="table" data-testid="dashboard-agents">
            <thead>
              <tr>
                <th scope="col">Agent</th>
                <th scope="col">角色</th>
                <th scope="col">状态</th>
                <th scope="col" class="table__num">有效执行</th>
                <th scope="col" class="table__num">占槽/上限</th>
                <th scope="col">等待回收</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="agent in dashboard.agents"
                :key="agent.agent_id"
                :data-testid="`dashboard-agent-${agent.agent_id}`"
              >
                <td>
                  <span class="row">
                    <span class="avatar avatar--muted" aria-hidden="true">{{ agentInitial(agent.name) }}</span>
                    <span class="truncate">{{ agent.name }}</span>
                  </span>
                </td>
                <td>{{ agentRoleLabel(agent.role) }}</td>
                <td :data-state="stateOf(agent)">
                  <span class="agent-state" :data-state="stateOf(agent)">{{ stateLabel(agent) }}</span>
                </td>
                <td class="table__num" :data-testid="`dashboard-agent-effective-${agent.agent_id}`">
                  {{ agent.effective_executions }}
                </td>
                <td class="table__num" :data-testid="`dashboard-agent-slots-${agent.agent_id}`">
                  {{ agent.reserved_slots }} / {{ agent.max_concurrency }}
                </td>
                <td>
                  <span
                    v-if="agent.waiting_reclaim_executions > 0"
                    class="badge badge--warning"
                    :data-testid="`dashboard-agent-waiting-${agent.agent_id}`"
                  >
                    等待回收 {{ agent.waiting_reclaim_executions }}
                  </span>
                  <span v-else class="muted">—</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else class="muted" data-testid="dashboard-agents-empty">当前范围内没有 Agent。</p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.stat--compact {
  padding: var(--space-2) var(--space-3);
}

.stat--compact .stat__value {
  font-size: var(--text-xl);
}

.stat--warning {
  background: var(--color-warning-soft);
  border-color: var(--color-warning-border);
}

.stat--warning .stat__label,
.stat--warning .stat__value {
  color: var(--color-warning);
}

.agent-state {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-weight: var(--weight-medium);
}

.agent-state::before {
  content: '';
  width: 0.5rem;
  height: 0.5rem;
  border-radius: var(--radius-pill);
  background: var(--color-border-strong);
  flex: none;
}

.agent-state[data-state='ONLINE_WORKING']::before {
  background: var(--color-success);
}

.agent-state[data-state='ONLINE_IDLE']::before {
  background: var(--color-primary);
}

.agent-state[data-state='OFFLINE']::before {
  background: var(--color-warning);
}

.agent-state[data-state='DISABLED']::before {
  background: var(--color-danger);
}

.agent-state[data-state='NEVER_CONNECTED']::before {
  background: var(--color-border-strong);
}
</style>
