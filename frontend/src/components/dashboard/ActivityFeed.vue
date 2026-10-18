<script setup lang="ts">
/**
 * 活动列表（T33 / 产品 9.4）：只渲染服务端返回的事件，不改写、不合并、不推断。
 * 服务端按 `-created_at, -id` 返回最新在前；前端保持该顺序展示。
 * 时间沿用 `formatDateTimeWithZone`（Asia/Shanghai 并标注时区），不改变展示语义。
 */
import type { ActivityEventDTO } from '@/api/dashboard'
import { RouterLink } from 'vue-router'

import { activityActionLabel, formatDateTimeWithZone } from '@/components/task/task-format'

defineProps<{ events: ActivityEventDTO[] }>()

/** UUID 太长，列表里只显示前 8 位（完整值放在 title 里）。 */
function shortId(value: string): string {
  return value.slice(0, 8)
}

function actorLabel(event: ActivityEventDTO): string {
  if (event.actor_type === 'web_operator') return '本地管理员（Web 代操作）'
  if (event.actor_type === 'agent') return `Agent ${event.actor_agent_id ?? ''}`.trim()
  return 'system（系统）'
}

function summaryText(value: Record<string, unknown> | null | undefined): string {
  if (!value) return ''
  const text = JSON.stringify(value)
  if (!text || text === '{}') return ''
  return text.length > 200 ? `${text.slice(0, 200)}…` : text
}

/** 动作 → 语义色：只影响时间线节点与动作徽标的配色，文案与顺序仍取服务端值。 */
const ACTION_TONE: Readonly<Record<string, string>> = {
  'task.approved': 'success',
  'task.rejected': 'danger',
  timed_out: 'danger',
  reclaimed: 'warning',
  released: 'warning',
  'task.reopened': 'warning',
  claimed: 'primary',
  published: 'primary',
  'task.submitted': 'info',
}

function actionTone(action: string): string {
  return ACTION_TONE[action] ?? 'neutral'
}
</script>

<template>
  <ul v-if="events.length > 0" class="activity-list" data-testid="activity-list">
    <li
      v-for="event in events"
      :key="event.id"
      class="activity-item"
      :data-tone="actionTone(event.action)"
      :data-testid="`activity-item-${event.id}`"
    >
      <div class="row row--wrap">
        <span class="activity-item__time mono" :data-testid="`activity-time-${event.id}`">{{
          formatDateTimeWithZone(event.created_at)
        }}</span>
        <span
          class="badge"
          :class="`badge--${actionTone(event.action)}`"
          :data-testid="`activity-action-${event.id}`"
          >{{ activityActionLabel(event.action) }}</span
        >
        <span class="activity-item__actor" :data-testid="`activity-actor-${event.id}`">{{ actorLabel(event) }}</span>
        <span v-if="event.project_id" class="badge badge--mono" :title="`项目 ${event.project_id}`">
          项目 {{ shortId(event.project_id) }}
        </span>
        <RouterLink
          v-if="event.task_id"
          class="badge badge--mono badge--link"
          :to="{ path: '/', query: { task: event.task_id } }"
          :title="`任务 ${event.task_id}（点击查看详情）`"
          :data-testid="`activity-task-${event.id}`"
        >
          任务 {{ shortId(event.task_id) }} ↗
        </RouterLink>
      </div>

      <p
        v-if="event.reason"
        class="notice notice--warning activity-item__reason"
        :data-testid="`activity-reason-${event.id}`"
      >
        原因：{{ event.reason }}
      </p>

      <p class="activity-item__detail">
        <template v-if="summaryText(event.before)">变更前：{{ summaryText(event.before) }} </template>
        <template v-if="summaryText(event.after)">变更后：{{ summaryText(event.after) }} </template>
        <template v-if="event.request_id">请求号：{{ event.request_id }}</template>
      </p>
    </li>
  </ul>
  <p v-else class="muted" data-testid="activity-empty">当前筛选条件下没有活动记录。</p>
</template>

<style scoped>
.activity-list {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  list-style: none;
}

.activity-item {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) 0 var(--space-3) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.activity-item:first-child {
  padding-top: 0;
}

.activity-item:last-child {
  border-bottom: none;
  padding-bottom: 0;
}

.activity-item::before {
  content: '';
  position: absolute;
  left: 0;
  top: var(--space-4);
  width: 0.625rem;
  height: 0.625rem;
  border: 2px solid var(--tone, var(--color-border-strong));
  border-radius: var(--radius-pill);
  background: var(--color-surface);
}

.activity-item:first-child::before {
  top: 0.2rem;
}

.activity-item[data-tone='success'] {
  --tone: var(--color-success);
}

.activity-item[data-tone='danger'] {
  --tone: var(--color-danger);
}

.activity-item[data-tone='warning'] {
  --tone: var(--color-warning);
}

.activity-item[data-tone='primary'] {
  --tone: var(--color-primary);
}

.activity-item[data-tone='info'] {
  --tone: var(--color-info);
}

.activity-item[data-tone='neutral'] {
  --tone: var(--color-border-strong);
}

.activity-item__time {
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
}

.activity-item__actor {
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
}

.activity-item__reason {
  font-size: var(--text-xs);
}

.activity-item__detail {
  margin: 0;
  color: var(--color-text-muted);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  word-break: break-all;
}
</style>
