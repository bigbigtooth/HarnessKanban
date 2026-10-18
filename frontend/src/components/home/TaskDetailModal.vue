<script setup lang="ts">
/**
 * 任务详情浮窗：点看板卡片后弹出，展示任务要求、验收标准、执行与提交/审核历史、附件与活动时间线，
 * 底部按服务端的可用动作（`availableActions`）给出操作入口。
 *
 * 详情通过 `GET /tasks/{id}` 拉取，时间线用 `GET /tasks/{id}/activity`，附件用
 * `GET /tasks/{id}/attachments`；三者并行，离开浮窗即丢弃过期响应。
 */
import { computed, ref, watch } from 'vue'

import {
  getTask,
  listTaskActivity,
  listTaskAttachments,
  type ActivityEventDTO,
  type AttachmentDTO,
  type TaskDetailDTO,
  type TaskListItemDTO,
} from '@/api/tasks'
import { ApiError } from '@/api/client'
import { fileDownloadUrl } from '@/api/projects'
import {
  ACTION_LABELS,
  activityActionLabel,
  availableActions,
  claimModeLabel,
  difficultyLabel,
  formatDateTime,
  formatRemaining,
  formatTimeoutSeconds,
  isAwaitingReclaim,
  reviewDecisionLabel,
  statusLabel,
  type TaskActionKind,
} from '@/components/task/task-format'
import AppIcon from './AppIcon.vue'
import ModalShell from './ModalShell.vue'
import { agentName, projectById } from '@/stores/home'
import { dueMeta, projectStatusMeta, tagPalette } from './home-format'

const props = defineProps<{ task: TaskListItemDTO | null; nowMs?: number }>()
const emit = defineEmits<{
  close: []
  action: [kind: TaskActionKind]
  edit: []
}>()

const detail = ref<TaskDetailDTO | null>(null)
const activity = ref<ActivityEventDTO[]>([])
const attachments = ref<AttachmentDTO[]>([])
const loading = ref(false)
const error = ref<ApiError | null>(null)
let token = 0

const project = computed(() => (props.task ? projectById.value.get(props.task.project_id) ?? null : null))
const due = computed(() => (props.task ? dueMeta(props.task.due_at, props.task.status === 'DONE') : null))
const execution = computed(() => detail.value?.current_execution ?? null)
const submission = computed(() => detail.value?.current_submission ?? null)
// 「编辑」有独立按钮，这里不再重复列一次。
const actions_ = computed<TaskActionKind[]>(() =>
  props.task ? availableActions(props.task).filter((kind) => kind !== 'edit') : [],
)
const palette = computed(() => tagPalette(props.task?.labels?.[0]))
// 详情接口的字段最权威；列表接口的 description/acceptance_criteria 是可选的。
const requirementText = computed(() => detail.value?.description ?? props.task?.description ?? '')
const acceptanceText = computed(() => detail.value?.acceptance_criteria ?? props.task?.acceptance_criteria ?? '')

const remaining = computed(() => {
  if (!execution.value) return ''
  return formatRemaining(execution.value.execution_deadline_at, props.nowMs ?? Date.now())
})

const awaitingReclaim = computed(() =>
  props.task ? isAwaitingReclaim(props.task, props.nowMs ?? Date.now()) : false,
)

async function load(task: TaskListItemDTO): Promise<void> {
  const current = ++token
  loading.value = true
  error.value = null
  detail.value = null
  activity.value = []
  attachments.value = []
  try {
    const [taskDetail, events, files] = await Promise.all([
      getTask(task.id),
      listTaskActivity(task.id, { pageSize: 50 }),
      listTaskAttachments(task.id, { pageSize: 50 }),
    ])
    if (current !== token) return
    detail.value = taskDetail
    activity.value = events.items
    attachments.value = files.items
  } catch (caught) {
    if (current !== token) return
    error.value = caught instanceof ApiError ? caught : null
  } finally {
    if (current === token) loading.value = false
  }
}

watch(
  () => props.task,
  (task) => {
    if (task) void load(task)
    else {
      token += 1
      detail.value = null
    }
  },
  { immediate: true },
)

/** 当前执行 + 历史执行（去重，按序号倒序）。 */
const executionHistory = computed(() => {
  const history = detail.value?.execution_history ?? []
  const current = execution.value
  const items = current ? [current, ...history.filter((item) => item.execution_id !== current.execution_id)] : history
  return [...items].sort((a, b) => b.sequence - a.sequence)
})

const submissionHistory = computed(() => {
  const history = detail.value?.submission_history ?? []
  const current = submission.value
  const items = current
    ? [current, ...history.filter((item) => item.submission_id !== current.submission_id)]
    : history
  return items
})
</script>

<template>
  <ModalShell :open="Boolean(task)" labelled-by="task-detail-title" @close="emit('close')">
    <template v-if="task">
      <div class="task-form-head">
        <div>
          <span class="modal-overline">TASK DETAILS · {{ task.display_number }}</span>
          <h2 id="task-detail-title">{{ task.title }}</h2>
          <div class="task-detail-badges">
            <span class="status-pill" :class="`status-${task.status.toLowerCase()}`">
              {{ statusLabel(task.status) }}
            </span>
            <span class="tag" :class="`tag-${palette}`">
              <span class="tag-emoji" aria-hidden="true">🏷️</span>{{ task.labels?.[0] || '未分类' }}
            </span>
            <span class="priority" :class="task.difficulty >= 4 ? 'priority-high' : 'priority-low'">
              {{ task.difficulty >= 4 ? '⚡' : '☁️' }} {{ difficultyLabel(task.difficulty) }}
            </span>
            <span class="meta-item"><AppIcon name="bolt" />{{ claimModeLabel(task.claim_mode) }}</span>
            <span v-if="task.is_archived" class="meta-item">📦 已归档</span>
            <span v-if="awaitingReclaim" class="chip chip-danger">等待回收</span>
          </div>
        </div>
        <button class="sheet-close on-plain" type="button" aria-label="关闭" @click="emit('close')">
          <AppIcon name="x" />
        </button>
      </div>

      <div class="sheet-scroll">
        <div v-if="loading" class="board-state" data-testid="task-detail-loading">
          <span class="state-face" aria-hidden="true">🫧</span>
          <strong>正在打开这张卡片…</strong>
        </div>
        <div v-else-if="error" class="board-state is-error" data-testid="task-detail-error">
          <span class="state-face" aria-hidden="true">☁️</span>
          <strong>详情没拉下来</strong>
          <span>{{ error.displayMessage }}</span>
          <button class="btn btn-soft" type="button" @click="load(task)">重新加载</button>
        </div>
        <div v-else class="task-detail-body">
          <div class="task-detail-main">
            <section>
              <div class="block-title">任务要求 <span class="block-num">REQUIREMENTS</span></div>
              <div v-if="requirementText" class="background-text">{{ requirementText }}</div>
              <div v-else class="background-text is-empty">还没有写任务要求。</div>
            </section>
            <section>
              <div class="block-title">验收标准 <span class="block-num">ACCEPTANCE</span></div>
              <div v-if="acceptanceText" class="background-text">{{ acceptanceText }}</div>
              <div v-else class="background-text is-empty">还没有写验收标准。</div>
            </section>

            <section v-if="execution">
              <div class="block-title">
                当前执行 <span class="block-num">EXECUTION</span>
              </div>
              <div class="background-text">
                <div class="detail-kv">
                  <span>执行者</span><strong>{{ agentName(execution.agent_id) || execution.agent_id }}</strong>
                  <span>执行序号</span><strong>#{{ execution.sequence }}</strong>
                  <span>固定超时</span><strong>{{ formatTimeoutSeconds(execution.execution_timeout_seconds) }}</strong>
                  <span>截止时刻</span><strong>{{ formatDateTime(execution.execution_deadline_at) }}（{{ remaining }}）</strong>
                  <span>进度</span>
                  <strong>
                    {{
                      execution.progress_percent === null || execution.progress_percent === undefined
                        ? execution.progress_message || '未上报'
                        : `${execution.progress_percent}% · ${execution.progress_message || '已上报'}`
                    }}
                  </strong>
                </div>
              </div>
            </section>

            <section v-if="submission">
              <div class="block-title">最近提交 <span class="block-num">SUBMISSION</span></div>
              <div class="background-text">
                <p class="detail-summary">{{ submission.summary }}</p>
                <div v-if="submission.content" class="detail-content">{{ submission.content }}</div>
                <ul v-if="submission.self_check?.length" class="detail-checklist">
                  <li v-for="(item, index) in submission.self_check" :key="index">
                    {{ item.passed ? '✅' : '❌' }} {{ item.criterion }}
                    <small v-if="item.evidence">（{{ item.evidence }}）</small>
                  </li>
                </ul>
                <p class="muted">提交时间：{{ formatDateTime(submission.submitted_at) }}</p>
              </div>
            </section>

            <section v-if="detail?.review_history?.length">
              <div class="block-title">审核记录 <span class="block-num">REVIEWS</span></div>
              <ul class="detail-timeline">
                <li v-for="review in detail.review_history" :key="review.review_id">
                  <span class="tone-badge" :class="review.decision === 'APPROVE' ? 'is-ok' : 'is-warn'">
                    {{ reviewDecisionLabel(review.decision) }}
                  </span>
                  <div>
                    <strong>{{ review.comment || '（无备注）' }}</strong>
                    <small>{{ formatDateTime(review.created_at) }} · {{ review.actor_type }}</small>
                  </div>
                </li>
              </ul>
            </section>

            <section>
              <div class="block-title">
                附件 <span class="doc-count">{{ attachments.length }} 份</span>
              </div>
              <div v-if="attachments.length === 0" class="doc-empty">还没有附件。任务参考与执行产物都会出现在这里。</div>
              <div v-else class="doc-list">
                <div v-for="file in attachments" :key="file.id" class="doc-row">
                  <span class="doc-icon" :class="`kind-${file.media_type.includes('zip') ? 'zip' : file.media_type.includes('image') ? 'img' : 'doc'}`">📄</span>
                  <div class="doc-info">
                    <strong>{{ file.original_name }}</strong>
                    <small>{{ file.purpose }} · {{ Math.round(file.size / 1024) }} KB · {{ formatDateTime(file.created_at) }}</small>
                  </div>
                  <a class="doc-action" :href="fileDownloadUrl(file.file_id)" :download="file.original_name" title="下载">
                    <AppIcon name="download" />
                  </a>
                </div>
              </div>
            </section>

            <section>
              <div class="block-title">
                活动时间线 <span class="doc-count">{{ activity.length }} 条</span>
              </div>
              <ul v-if="activity.length" class="detail-timeline">
                <li v-for="event in activity" :key="event.id">
                  <span class="tone-badge">{{ event.actor_type }}</span>
                  <div>
                    <strong>{{ activityActionLabel(event.action) }}</strong>
                    <small>{{ formatDateTime(event.created_at) }}<span v-if="event.reason"> · {{ event.reason }}</span></small>
                  </div>
                </li>
              </ul>
              <div v-else class="doc-empty">还没有活动记录。</div>
            </section>
          </div>

          <aside class="task-detail-side">
            <div class="side-card">
              <div class="side-label">PROJECT · 所属项目</div>
              <div class="side-date">
                <strong>{{ project?.title ?? task.project_id }}</strong>
                <span v-if="project" class="status-pill" :class="projectStatusMeta(project.status).className">
                  {{ projectStatusMeta(project.status).label }}
                </span>
              </div>
            </div>
            <div class="side-card">
              <div class="side-label">ASSIGNEE · 执行者</div>
              <div class="side-date">
                <strong>{{ task.assignee_agent_id ? agentName(task.assignee_agent_id) : '还没有执行者' }}</strong>
                <span v-if="task.designated_agent_id" class="muted">
                  指定：{{ agentName(task.designated_agent_id) }}
                </span>
              </div>
            </div>
            <div class="side-card">
              <div class="side-label">DEADLINE · 计划截止</div>
              <div class="side-date">
                <strong>{{ due ? due.title : '未设置' }}</strong>
                <span v-if="due" class="due-bubble" :class="due.className">{{ due.label }}</span>
              </div>
            </div>
            <div class="side-card">
              <div class="side-label">HISTORY · 执行与提交</div>
              <div class="side-progress">
                <strong>{{ executionHistory.length }} 次执行</strong>
                <span>{{ submissionHistory.length }} 次提交 · 版本 v{{ task.version }}</span>
              </div>
            </div>
          </aside>
        </div>
      </div>

      <div class="task-form-footer">
        <div class="footer-actions">
          <button class="btn btn-ghost" type="button" @click="emit('close')">关闭</button>
          <button class="btn btn-soft" type="button" data-testid="task-detail-edit" @click="emit('edit')">
            <AppIcon name="edit" /> 编辑要求
          </button>
          <button
            v-for="kind in actions_"
            :key="kind"
            class="btn"
            :class="kind === 'archive' || kind === 'reclaim' ? 'btn-danger' : 'btn-primary'"
            type="button"
            :data-testid="`task-action-${kind}`"
            @click="emit('action', kind)"
          >
            {{ ACTION_LABELS[kind] }}
          </button>
        </div>
      </div>
    </template>
  </ModalShell>
</template>
