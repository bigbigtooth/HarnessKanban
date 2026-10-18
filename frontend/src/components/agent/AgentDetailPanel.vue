<script setup lang="ts">
/**
 * Agent 详情面板（T29 / D16）。
 *
 * 展示规则：
 * - 显示状态：禁用优先于在线状态；其余取自服务端 `online_state`（前端不重算在线）；
 * - **有效执行数与已占容量槽分开显示**：有效执行 = 未结束且未过期；
 *   占槽 = 服务端权威的 reserved_execution_count（含到期未回收）；
 * - 到期未回收的执行显示“等待回收”，其槽位仍占用；
 * - 软删除（`deleted_at`）的 Agent 显示「已删除」徽标、删除时间与原因，并提供恢复入口；
 * - 详情不展示任何 Key 摘要/指纹，也绝不缓存明文 Key。
 *
 * 版式：面板头（身份 + 状态徽标 + 关闭）→ 容量/执行指标 → 字段元数据 →
 * 授权项目 → 当前执行 → 执行历史 → 管理动作（页脚按钮组）。
 */
import { computed } from 'vue'
import type { ApiError } from '@/api/client'
import type { AgentDetailDTO } from '@/api/agents'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import {
  agentDeletedReasonText,
  agentDisplayState,
  agentInitial,
  agentRoleLabel,
  executionHistoryText,
  executionWaitingReclaim,
  formatAgentTime,
  isAgentDeleted,
  ONLINE_STATE_BADGE_CLASS,
  ONLINE_STATE_LABEL,
  slotView,
} from './agent-format'

const props = withDefaults(
  defineProps<{
    agent: AgentDetailDTO | null
    loading?: boolean
    error?: ApiError | null
    busy?: boolean
  }>(),
  { loading: false, error: null, busy: false },
)

const emit = defineEmits<{
  refresh: []
  rotateKey: []
  enable: []
  disable: []
  restore: []
  changeRole: []
  close: []
}>()

const state = computed(() => (props.agent ? agentDisplayState(props.agent) : 'NEVER_CONNECTED'))
const stateLabel = computed(() => ONLINE_STATE_LABEL[state.value])
const stateBadgeClass = computed(() => ONLINE_STATE_BADGE_CLASS[state.value])
const slots = computed(() => (props.agent ? slotView(props.agent) : null))
const capabilities = computed(() => props.agent?.capabilities ?? [])
const projects = computed(() => props.agent?.projects ?? [])
const currentExecutions = computed(() => props.agent?.current_executions ?? [])
const history = computed(() => props.agent?.execution_summary ?? null)
const initial = computed(() => agentInitial(props.agent?.name))
const isDeleted = computed(() => isAgentDeleted(props.agent))
const deletedReason = computed(() => (props.agent ? agentDeletedReasonText(props.agent) : ''))
</script>

<template>
  <section class="panel agent-detail" data-testid="agent-detail">
    <LoadingState v-if="loading && !agent" label="加载 Agent 详情…" />
    <ErrorState v-else-if="error && !agent" :error="error" @retry="emit('refresh')" />

    <template v-else-if="agent">
      <header class="panel-head agent-detail__head">
        <div class="row row--top agent-detail__identity">
          <span class="avatar avatar--lg" aria-hidden="true">{{ initial }}</span>
          <div class="stack stack--tight">
            <h3 class="detail-name" data-testid="agent-detail-name">{{ agent.name }}</h3>
            <div class="row row--wrap agent-detail__badges">
              <span class="badge badge--primary" data-testid="agent-detail-role">
                {{ agentRoleLabel(agent.role) }}
              </span>
              <span
                class="badge"
                :class="stateBadgeClass"
                :data-state="state"
                data-testid="agent-detail-state"
              >
                {{ stateLabel }}
              </span>
              <span
                class="badge"
                :class="agent.enabled ? 'badge--success' : 'badge--neutral'"
                data-testid="agent-detail-enabled"
              >
                {{ agent.enabled ? '已启用' : '已禁用（显示优先于在线状态）' }}
              </span>
              <span v-if="isDeleted" class="badge badge--danger" data-testid="agent-detail-deleted">
                已删除
              </span>
            </div>
          </div>
        </div>
        <button
          type="button"
          class="btn btn--ghost btn--sm"
          data-testid="agent-detail-close"
          @click="emit('close')"
        >
          关闭详情
        </button>
      </header>

      <div class="panel-body agent-detail__body">
        <ErrorState v-if="error" :error="error" @retry="emit('refresh')" />

        <p v-if="isDeleted" class="notice notice--warning" data-testid="agent-detail-deleted-notice">
          该 Agent 已删除：Key 已失效，不再出现在列表、候选与统计中；执行、提交与审核历史保留，可恢复。
        </p>

        <ul class="stat-grid agent-detail__metrics">
          <li class="stat">
            <span class="stat__label">有效执行（工作中）</span>
            <span class="stat__value tabular" data-testid="agent-detail-effective">{{ slots?.effective ?? 0 }}</span>
            <span class="stat__hint">未结束且未过期的执行</span>
          </li>
          <li class="stat">
            <span class="stat__label">已占容量槽</span>
            <span class="stat__value tabular" data-testid="agent-detail-reserved">
              {{ slots?.reserved ?? 0 }} / {{ agent.max_concurrency }}
            </span>
            <span class="stat__hint">服务端权威占槽，含到期未回收</span>
          </li>
          <li class="stat">
            <span class="stat__label">可用容量</span>
            <span class="stat__value tabular" data-testid="agent-detail-available">{{ slots?.available ?? 0 }}</span>
            <span class="stat__hint">= 最大并发 − 占槽</span>
          </li>
          <li class="stat">
            <span class="stat__label">最大并发</span>
            <span class="stat__value tabular" data-testid="agent-detail-max-concurrency">
              {{ agent.max_concurrency }}
            </span>
            <span class="stat__hint">服务端配置上限</span>
          </li>
        </ul>

        <p
          v-if="(slots?.waitingReclaim ?? 0) > 0"
          class="notice notice--warning"
          data-testid="agent-detail-waiting-reclaim"
        >
          等待回收：{{ slots?.waitingReclaim }} 个执行已过固定截止但未回收，仍占用容量槽（不计入有效执行）。
        </p>

        <dl class="meta-list">
          <dt>描述</dt>
          <dd data-testid="agent-detail-description">{{ agent.description || '（未填写）' }}</dd>
          <dt>能力</dt>
          <dd data-testid="agent-detail-capabilities">
            {{ capabilities.length > 0 ? capabilities.join('、') : '（未声明）' }}
          </dd>
          <dt>Key 版本</dt>
          <dd class="tabular" data-testid="agent-detail-key-version">
            {{ agent.key_version }}（明文 Key 不可再次查看）
          </dd>
          <dt>最近心跳</dt>
          <dd class="tabular" :title="agent.last_heartbeat_at ?? ''" data-testid="agent-detail-heartbeat">
            {{ formatAgentTime(agent.last_heartbeat_at) || '从未连接' }}
          </dd>
          <template v-if="isDeleted">
            <dt>删除时间</dt>
            <dd class="tabular" :title="agent.deleted_at ?? ''" data-testid="agent-detail-deleted-at">
              {{ formatAgentTime(agent.deleted_at) }}
            </dd>
            <dt>删除原因</dt>
            <dd data-testid="agent-detail-deleted-reason">{{ deletedReason }}</dd>
          </template>
        </dl>

        <section class="section">
          <div class="section-head">
            <h4 class="section-title">授权项目（{{ projects.length }}）</h4>
          </div>
          <p v-if="projects.length === 0" class="muted text-sm" data-testid="agent-detail-projects-empty">
            该 Agent 尚未被授权任何项目。
          </p>
          <ul v-else class="chip-list" data-testid="agent-detail-projects">
            <li
              v-for="project in projects"
              :key="project.id"
              class="chip"
              :data-testid="`agent-detail-project-${project.id}`"
            >
              {{ project.title }}
              <span class="muted">（{{ project.status }}）</span>
            </li>
          </ul>
        </section>

        <section class="section">
          <div class="section-head">
            <h4 class="section-title">当前未结束执行（{{ currentExecutions.length }}）</h4>
          </div>
          <p v-if="currentExecutions.length === 0" class="muted text-sm" data-testid="agent-detail-executions-empty">
            没有未结束的执行。
          </p>
          <ul v-else class="execution-list" data-testid="agent-detail-executions">
            <li
              v-for="item in currentExecutions"
              :key="item.execution_id"
              class="execution-item"
              :data-testid="`agent-detail-execution-${item.execution_id}`"
            >
              <div class="row row--wrap">
                <span class="mono strong">{{ item.display_number ?? item.task_id }}</span>
                <span class="badge badge--difficulty" :data-difficulty="item.difficulty">
                  难度 L{{ item.difficulty }}
                </span>
                <span
                  v-if="executionWaitingReclaim(item)"
                  class="chip chip--warn"
                  data-testid="agent-execution-waiting-reclaim"
                >等待回收</span>
                <span v-else class="chip chip--success">执行中</span>
              </div>
              <dl class="meta-list text-xs">
                <dt>领取于</dt>
                <dd class="tabular" :title="item.claimed_at">{{ formatAgentTime(item.claimed_at) }}</dd>
                <dt>执行截止</dt>
                <dd class="tabular" :title="item.execution_deadline_at">
                  {{ formatAgentTime(item.execution_deadline_at) }}
                </dd>
              </dl>
            </li>
          </ul>
        </section>

        <section class="section">
          <div class="section-head">
            <h4 class="section-title">执行历史</h4>
          </div>
          <p v-if="history" class="muted text-sm" data-testid="agent-detail-history">
            {{ executionHistoryText(history) }}；未结束 {{ history.open_count }}。
          </p>
        </section>
      </div>

      <footer class="agent-detail__foot">
        <h4 class="section-title">管理动作</h4>
        <div class="btn-group">
          <button
            type="button"
            class="btn"
            data-testid="agent-rotate-key"
            :disabled="busy || isDeleted"
            :title="isDeleted ? '已删除的 Agent 需先恢复' : undefined"
            @click="emit('rotateKey')"
          >
            重置 Key（旧 Key 立即失效）
          </button>
          <button
            v-if="isDeleted"
            type="button"
            class="btn btn--primary"
            data-testid="agent-detail-restore"
            :disabled="busy"
            @click="emit('restore')"
          >
            恢复（启用 + 清空删除标记）
          </button>
          <button
            v-else-if="!agent.enabled"
            type="button"
            class="btn btn--primary"
            data-testid="agent-enable"
            :disabled="busy"
            @click="emit('enable')"
          >
            启用
          </button>
          <button
            v-else
            type="button"
            class="btn btn--danger"
            data-testid="agent-disable"
            :disabled="busy"
            @click="emit('disable')"
          >
            禁用
          </button>
          <button
            type="button"
            class="btn"
            data-testid="agent-change-role"
            :disabled="busy || isDeleted"
            :title="isDeleted ? '已删除的 Agent 需先恢复' : undefined"
            @click="emit('changeRole')"
          >
            变更角色
          </button>
          <button
            type="button"
            class="btn btn--ghost"
            data-testid="agent-refresh"
            :disabled="busy"
            @click="emit('refresh')"
          >
            刷新
          </button>
        </div>
      </footer>
    </template>
  </section>
</template>

<style scoped>
.agent-detail__head {
  align-items: flex-start;
}

.agent-detail__identity {
  min-width: 0;
}

.detail-name {
  margin: 0;
  font-size: var(--text-lg);
}

.agent-detail__body {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

/* 指标块：`li` 复用 `.stat` 外观（列表语义，去掉默认标记） */
.agent-detail__metrics {
  list-style: none;
  margin: 0;
  padding: 0;
}

.chip-list {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  list-style: none;
  margin: 0;
  padding: 0;
}

.execution-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  list-style: none;
  margin: 0;
  padding: 0;
}

.execution-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-muted);
}

.agent-detail__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  flex-wrap: wrap;
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--color-border);
  background: var(--color-surface-muted);
  border-bottom-left-radius: var(--radius-lg);
  border-bottom-right-radius: var(--radius-lg);
}
</style>
