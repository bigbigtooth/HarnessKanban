<script setup lang="ts">
/**
 * 待处理异常与项目分布（T33 / D17）。
 *
 * 异常列表来自 `dashboard.exceptions`（计数为真实总数，列表受服务端上限约束），
 * 每条异常都提供跳转到对应项目看板的入口，便于现场定位任务；
 * 项目分布只展示同一份 payload 的项目行，完成率为空时显示“无任务”（产品 §9.2），
 * 绝不显示成 0% 或 100%。
 */
import { RouterLink } from 'vue-router'
import type { DashboardDTO } from '@/api/dashboard'
import { statusLabel } from '@/components/task/task-format'
import { projectStatusLabel } from '@/components/project/project-format'
import { exceptionKindLabel, exceptionReasonLabels } from './dashboard-format'

defineProps<{ dashboard: DashboardDTO }>()

function boardLink(projectId: string): string {
  return `/projects/${projectId}/board`
}
</script>

<template>
  <div class="stack stack--loose">
    <section class="panel" data-testid="dashboard-exceptions">
      <div class="panel-head">
        <h3 class="panel-title">待处理异常</h3>
        <span class="badge badge--warning" data-testid="dashboard-exception-counts">
          需回收 {{ dashboard.exception_counts.OVERDUE_RECLAIM_REQUIRED }} · 指定者不可用
          {{ dashboard.exception_counts.UNAVAILABLE_DESIGNATED_AGENT }} · 合计
          {{ dashboard.exception_counts.total }}
        </span>
      </div>

      <div class="panel-body stack">
        <p class="section-hint" data-testid="dashboard-exception-returned">
          列表返回 {{ dashboard.exception_counts.returned }} / 上限
          {{ dashboard.exception_counts.limit }} 条（计数为范围内真实总数）。
        </p>

        <ul v-if="dashboard.exceptions.length > 0" class="exception-list" data-testid="dashboard-exception-list">
          <li
            v-for="item in dashboard.exceptions"
            :key="item.task_id"
            class="exception-item"
            :data-testid="`dashboard-exception-${item.task_id}`"
          >
            <div class="row row--between row--wrap">
              <div class="row row--wrap">
                <span class="mono exception-item__number">{{ item.display_number }}</span>
                <span class="strong">{{ item.title }}</span>
              </div>
              <div class="row row--wrap">
                <span class="badge badge--warning" :data-testid="`dashboard-exception-kind-${item.task_id}`">
                  {{ exceptionKindLabel(item) }}
                </span>
                <span class="badge badge--status" :data-status="item.status">{{ statusLabel(item.status) }}</span>
                <RouterLink
                  class="btn btn--sm"
                  :to="boardLink(item.project_id)"
                  :data-testid="`dashboard-exception-link-${item.task_id}`"
                >
                  前往项目看板定位
                </RouterLink>
              </div>
            </div>
            <p class="exception-item__reasons" :data-testid="`dashboard-exception-reasons-${item.task_id}`">
              {{ exceptionReasonLabels(item).join('；') || '待关注' }}
              <template v-if="item.execution_deadline_at"> · 执行截止 {{ item.execution_deadline_at }}</template>
              <template v-if="item.designated_agent_name"> · 指定执行者 {{ item.designated_agent_name }}</template>
            </p>
          </li>
        </ul>
        <p v-else class="muted text-sm" data-testid="dashboard-exceptions-empty">当前范围内没有待处理异常。</p>
      </div>
    </section>

    <section class="panel" data-testid="dashboard-project-status">
      <div class="panel-head">
        <h3 class="panel-title">项目分布</h3>
        <span class="section-hint">完成率＝已完成 ÷ 可见任务；空项目显示“无任务”</span>
      </div>

      <div class="panel-body">
        <div v-if="dashboard.projects.length > 0" class="table-wrap">
          <table class="table" data-testid="dashboard-projects">
            <thead>
              <tr>
                <th scope="col">项目</th>
                <th scope="col">状态</th>
                <th scope="col" class="table__num">任务总数</th>
                <th scope="col" class="table__num">准备中</th>
                <th scope="col" class="table__num">待办</th>
                <th scope="col" class="table__num">进行中</th>
                <th scope="col" class="table__num">待审核</th>
                <th scope="col" class="table__num">已完成</th>
                <th scope="col" class="table__num">完成率</th>
                <th scope="col">看板</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="project in dashboard.projects"
                :key="project.project_id"
                :data-testid="`dashboard-project-row-${project.project_id}`"
              >
                <td>
                  <span class="truncate">{{ project.title }}</span>
                </td>
                <td><span class="badge badge--neutral">{{ projectStatusLabel(project.status) }}</span></td>
                <td class="table__num" :data-testid="`dashboard-project-total-${project.project_id}`">
                  {{ project.task_total }}
                </td>
                <td class="table__num">{{ project.task_status_counts.PREPARING ?? 0 }}</td>
                <td class="table__num">{{ project.task_status_counts.TODO ?? 0 }}</td>
                <td class="table__num">{{ project.task_status_counts.IN_PROGRESS ?? 0 }}</td>
                <td class="table__num">{{ project.task_status_counts.IN_REVIEW ?? 0 }}</td>
                <td class="table__num">{{ project.task_status_counts.DONE ?? 0 }}</td>
                <td class="table__num">
                  {{ project.completion_rate == null ? '无任务' : `${(project.completion_rate * 100).toFixed(1)}%` }}
                </td>
                <td>
                  <RouterLink
                    class="btn btn--sm"
                    :to="boardLink(project.project_id)"
                    :data-testid="`dashboard-project-board-${project.project_id}`"
                  >
                    打开看板
                  </RouterLink>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else class="muted text-sm" data-testid="dashboard-projects-empty">当前范围内没有项目。</p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.exception-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.exception-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--color-warning-border);
  border-left-width: 3px;
  border-radius: var(--radius-md);
  background: var(--color-warning-soft);
}

.exception-item__number {
  color: var(--color-warning);
  font-weight: var(--weight-semibold);
}

.exception-item__reasons {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
}
</style>
