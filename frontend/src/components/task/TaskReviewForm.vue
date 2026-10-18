<script setup lang="ts">
/**
 * 审核表单（T32）：Web 审核必须显式选择一名有效主管（不能是提交者本人），
 * 并明确展示「审核人身份未验证」（CONTRACT.md §4：本地 Web 代操作固定 false）。
 *
 * APPROVE → 已完成；REJECT → 退回待办并默认独占指定原执行者（需要重新领取）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import BaseDialog from '@/components/BaseDialog.vue'
import {
  reviewTask,
  type ReviewReceiptDTO,
  type TaskDetailDTO,
  type TaskListItemDTO,
} from '@/api/tasks'
import { ReviewDecision } from '@/api/types'
import { agentDirectory, ensureAgentDirectory, supervisorCandidates } from './boardState'
import { reviewDecisionLabel } from './task-format'
import { useTaskAction } from './useTaskAction'
import TaskErrorNotice from './TaskErrorNotice.vue'

const props = defineProps<{
  task: TaskListItemDTO
  detail: TaskDetailDTO | null
  /** 由「拖到待办」进入时预设为驳回。 */
  preferredDecision?: ReviewDecision | null
}>()

const emit = defineEmits<{
  close: []
  reviewed: [receipt: ReviewReceiptDTO]
  conflict: [taskId: string]
}>()

const action = useTaskAction()
const submission = computed(() => props.detail?.current_submission ?? null)
const submitterAgentId = computed<string | null>(() => {
  const raw = submission.value?.submitted_by_agent_id ?? null
  // AgentAPI 提交时为提交者 UUID；Web 代提交时服务端可能返回空值。
  if (!raw || raw === 'None' || raw === 'null') return null
  return raw
})
const candidates = computed(() => supervisorCandidates(props.task.project_id, submitterAgentId.value))
const reviewerAgentId = ref('')
const decision = ref<ReviewDecision>((props.preferredDecision ?? ReviewDecision.APPROVE) as ReviewDecision)
const comment = ref('')
const localError = ref('')
const receipt = ref<ReviewReceiptDTO | null>(null)

onMounted(async () => {
  await ensureAgentDirectory()
})

// Agent 目录是异步（按页）加载的：候选一变就把默认评审人补齐，
// 否则用户在加载完成前点提交会一直看到“必须选择一名有效主管”。
watch(
  candidates,
  (list) => {
    if (!list.some((item) => item.id === reviewerAgentId.value)) {
      reviewerAgentId.value = list[0]?.id ?? ''
    }
  },
  { immediate: true },
)

const selectedReviewer = computed(() => candidates.value.find((item) => item.id === reviewerAgentId.value) ?? null)

async function submit(): Promise<void> {
  localError.value = ''
  const current = submission.value
  if (!current) {
    localError.value = '当前没有可审核的提交（可能已被其他主管处理）。'
    return
  }
  if (!reviewerAgentId.value) {
    localError.value = '必须选择一名有效主管作为审核人。'
    return
  }
  const reviewer = selectedReviewer.value
  if (!reviewer) {
    localError.value = '所选主管不可用（已禁用或不属于该项目）。'
    return
  }
  if (reviewer.id === submitterAgentId.value) {
    localError.value = '审核人不能是提交者本人（服务端也会拒绝）。'
    return
  }
  if (decision.value === ReviewDecision.REJECT && !comment.value.trim()) {
    localError.value = '驳回必须填写原因与修改要求。'
    return
  }
  try {
    const result = await action.run((key) =>
      reviewTask(
        props.task.id,
        {
          submissionId: current.submission_id,
          reviewerAgentId: reviewerAgentId.value,
          decision: decision.value,
          comment: comment.value,
          expectedVersion: props.detail?.version ?? props.task.version,
        },
        key,
      ),
    )
    if (!result) return
    receipt.value = result
    emit('reviewed', result)
  } catch {
    // 失败保留输入；冲突（409）时通知调用方回读服务端状态。
    if (action.error.value?.isConflict) emit('conflict', props.task.id)
  }
}

function onClose(): void {
  action.reset()
  emit('close')
}
</script>

<template>
  <BaseDialog :open="true" :title="`审核提交 ${task.display_number}`" @close="onClose">
    <div class="form-body">
      <div class="notice notice--warning identity" data-testid="review-identity">
        <p class="identity-flag" data-testid="review-unverified">
          审核人身份未验证：本地 Web 操作只能由管理员选定主管，服务端记录
          <code class="mono">reviewer_identity_verified=false</code>。
        </p>
        <p class="identity-submitter" data-testid="review-submitter">
          本次提交者：<code class="mono">{{ submitterAgentId ?? 'Web 代提交（无 Agent 提交者）' }}</code>
          <span v-if="submitterAgentId">（该 Agent 不会出现在审核人候选中）</span>
        </p>
      </div>

      <section v-if="submission" class="panel" data-testid="review-frozen-submission">
        <div class="panel-head">
          <h3 class="panel-title">被冻结的提交内容</h3>
          <span class="badge badge--info">只读快照</span>
        </div>
        <div class="panel-body">
          <dl class="meta-list">
            <dt>提交 ID</dt>
            <dd data-testid="review-submission-id"><code class="mono">{{ submission.submission_id }}</code></dd>
            <dt>执行 ID</dt>
            <dd data-testid="review-submission-execution"><code class="mono">{{ submission.execution_id }}</code></dd>
            <dt>结果摘要</dt>
            <dd data-testid="review-submission-summary">{{ submission.summary }}</dd>
            <dt>交付内容</dt>
            <dd class="prewrap" data-testid="review-submission-content">{{ submission.content || '（空）' }}</dd>
            <dt>自检</dt>
            <dd>
              <ul v-if="submission.self_check?.length" class="selfcheck" data-testid="review-submission-selfcheck">
                <li v-for="(item, index) in submission.self_check" :key="index">
                  <span class="chip" :class="item.passed ? 'chip--success' : 'chip--warn'">
                    {{ item.passed ? '已满足' : '未满足' }}
                  </span>
                  {{ item.criterion }}
                  <template v-if="item.evidence">（证据：{{ item.evidence }}）</template>
                </li>
              </ul>
              <span v-else>未填写</span>
            </dd>
            <dt>产物</dt>
            <dd data-testid="review-submission-artifacts">
              <template v-if="submission.artifact_refs?.length">
                <code v-for="fileId in submission.artifact_refs" :key="fileId" class="mono artifact-ref">
                  {{ fileId }}
                </code>
              </template>
              <span v-else>无</span>
            </dd>
            <dt>外链</dt>
            <dd data-testid="review-submission-links">
              <template v-if="submission.links?.length">
                <span v-for="link in submission.links" :key="link.url">{{ link.title }}（{{ link.url }}）</span>
              </template>
              <span v-else>无</span>
            </dd>
            <dt>要求版本</dt>
            <dd data-testid="review-requirements-version">
              {{ submission.requirements_version ?? '—' }}（上下文版本
              {{ submission.context_version_id ?? '—' }}）
            </dd>
          </dl>
        </div>
      </section>

      <p v-else class="notice notice--error" data-testid="review-no-submission">
        当前任务没有可审核的提交，请刷新后确认服务端状态。
      </p>

      <form v-if="!receipt" class="form-grid" @submit.prevent="submit">
        <label class="field">
          <span class="field-label">
            审核主管（必须不是提交者）<span class="field-required" aria-hidden="true">*</span>
          </span>
          <select v-model="reviewerAgentId" class="select" data-testid="review-reviewer" :disabled="action.busy.value">
            <option value="">请选择</option>
            <option v-for="agent in candidates" :key="agent.id" :value="agent.id">
              {{ agent.name }}（主管）
            </option>
          </select>
        </label>

        <p v-if="candidates.length === 0 && agentDirectory.status === 'loading'" class="notice notice--info" data-testid="review-candidates-loading">
          正在加载 Agent 目录，稍后会自动选中一名有资格的主管。
        </p>
        <p v-if="candidates.length === 0 && agentDirectory.status === 'error'" class="notice notice--error" data-testid="review-directory-error">
          读取 Agent 目录失败：{{ agentDirectory.error?.displayMessage ?? '请稍后重试' }}。
        </p>
        <p v-if="candidates.length === 0 && agentDirectory.status !== 'loading' && agentDirectory.status !== 'error'" class="notice notice--error" data-testid="review-no-candidate">
          没有可用的主管候选（提交者本人不能审核；请确认项目里还有启用状态的主管）。
        </p>

        <fieldset class="field fieldset">
          <legend class="field-label">审核决定</legend>
          <div class="decision-list">
            <label class="decision-option">
              <input
                v-model="decision"
                type="radio"
                :value="ReviewDecision.APPROVE"
                data-testid="review-decision-approve"
              />
              <span>通过（→ 已完成）</span>
            </label>
            <label class="decision-option">
              <input
                v-model="decision"
                type="radio"
                :value="ReviewDecision.REJECT"
                data-testid="review-decision-reject"
              />
              <span>驳回（→ 待办，默认独占指定原执行者，需重新领取）</span>
            </label>
          </div>
        </fieldset>

        <label class="field">
          <span class="field-label">
            审核意见 comment
            {{ decision === ReviewDecision.REJECT ? '（驳回必填：未满足哪些验收标准）' : '（可选）' }}
          </span>
          <textarea v-model="comment" class="textarea" rows="3" data-testid="review-comment" />
        </label>

        <p v-if="localError" class="notice notice--error" role="alert" data-testid="review-local-error">
          {{ localError }}
        </p>
        <TaskErrorNotice :error="action.error.value" />
      </form>

      <section v-else class="panel receipt" role="status" data-testid="review-receipt">
        <div class="panel-head">
          <p class="panel-title">审核已记录：{{ reviewDecisionLabel(receipt.review?.decision ?? decision) }}</p>
        </div>
        <div class="panel-body">
          <dl class="meta-list">
            <dt>任务状态</dt>
            <dd data-testid="review-receipt-status">{{ receipt.status }}</dd>
            <dt v-if="receipt.rework_assignment">返工安排</dt>
            <dd v-if="receipt.rework_assignment" data-testid="review-rework">
              {{ receipt.rework_assignment }}（下一轮需要重新领取，历史提交与审核保留可读）
            </dd>
          </dl>
        </div>
      </section>
    </div>

    <template #footer>
      <button type="button" class="btn" data-testid="review-cancel" @click="onClose">
        {{ receipt ? '关闭' : '取消' }}
      </button>
      <button
        v-if="!receipt"
        type="button"
        class="btn"
        :class="decision === ReviewDecision.REJECT ? 'btn--danger' : 'btn--primary'"
        :disabled="action.busy.value"
        data-testid="review-submit"
        @click="submit"
      >
        {{ action.busy.value ? '提交中…' : decision === ReviewDecision.REJECT ? '确认驳回' : '确认通过' }}
      </button>
    </template>
  </BaseDialog>
</template>

<style scoped>
.form-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.identity {
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-1);
}

.identity-flag,
.identity-submitter {
  margin: 0;
}

.fieldset {
  border: 0;
  margin: 0;
  padding: 0;
  min-width: 0;
}

.decision-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.decision-option {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: border-color var(--transition-fast), background var(--transition-fast);
}

.decision-option:hover {
  border-color: var(--color-border-strong);
}

.decision-option:has(input:checked) {
  border-color: var(--color-primary-border);
  background: var(--color-primary-soft);
  color: var(--color-primary-active);
  font-weight: var(--weight-medium);
}

.decision-option input[type='radio'] {
  margin-top: 0.15rem;
  accent-color: var(--color-primary);
}

.prewrap {
  white-space: pre-wrap;
}

.artifact-ref {
  display: block;
  word-break: break-all;
}

.receipt {
  border-color: var(--color-success-border);
}
</style>
