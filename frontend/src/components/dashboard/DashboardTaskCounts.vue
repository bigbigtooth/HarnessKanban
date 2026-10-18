<script setup lang="ts">
/**
 * 五态任务计数与完成率（T33 / D17）。
 *
 * 数字全部来自 `GET /dashboard` 的 `task_status_counts` / `task_total` / `completion`；
 * 历史执行、提交与审核不进入任务计数（服务端保证，前端不重算）。
 * 空范围由 `completionRateText` 明确显示“无任务（不计算完成率）”，绝不显示成 100%。
 */
import { computed } from 'vue'
import { TASK_STATUS_COLUMNS } from '@/api/types'
import type { DashboardDTO } from '@/api/dashboard'
import { statusLabel } from '@/components/task/task-format'
import { completionRateText } from './dashboard-format'

const props = defineProps<{ dashboard: DashboardDTO }>()

const counts = computed(() => props.dashboard.task_status_counts ?? ({} as DashboardDTO['task_status_counts']))
const statuses = computed(() =>
  TASK_STATUS_COLUMNS.map((status) => ({ status, count: counts.value[status] ?? 0 })),
)
</script>

<template>
  <section class="panel" data-testid="dashboard-task-counts">
    <div class="panel-head">
      <h3 class="panel-title">任务分布（五态）</h3>
      <span class="badge badge--primary" data-testid="dashboard-task-total">
        任务总数 {{ dashboard.task_total }}
      </span>
    </div>

    <div class="panel-body">
      <ul class="task-counts">
        <li
          v-for="item in statuses"
          :key="item.status"
          class="stat task-counts__item"
          :data-status="item.status"
          :data-testid="`dashboard-status-${item.status}`"
        >
          <span class="stat__label">{{ statusLabel(item.status) }}</span>
          <span class="stat__value" :data-testid="`dashboard-count-${item.status}`">{{ item.count }}</span>
        </li>
      </ul>

      <dl class="meta-list task-counts__meta">
        <dt>完成率</dt>
        <dd class="strong tabular" data-testid="dashboard-completion-rate">
          {{ completionRateText(dashboard.completion) }}
        </dd>
        <dt>已归档任务（默认口径外）</dt>
        <dd class="tabular" data-testid="dashboard-archived-task-total">{{ dashboard.archived_task_total }}</dd>
        <dt>已过计划截止（未完成）</dt>
        <dd class="tabular" data-testid="dashboard-overdue-task-total">{{ dashboard.overdue_task_total }}</dd>
      </dl>
    </div>
  </section>
</template>

<style scoped>
.task-counts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(76px, 1fr));
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.task-counts__item {
  gap: 0;
  padding: var(--space-2) var(--space-3);
  border-left: 3px solid var(--status-color, var(--color-border-strong));
}

.task-counts__item .stat__label {
  color: var(--status-color, var(--color-text-muted));
}

.task-counts__item .stat__value {
  font-size: var(--text-xl);
}

.task-counts__meta {
  margin-top: var(--space-3);
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
}
</style>
