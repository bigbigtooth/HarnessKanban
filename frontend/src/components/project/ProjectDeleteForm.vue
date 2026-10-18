<script setup lang="ts">
/**
 * 项目删除确认弹窗（单条与批量共用）。
 *
 * 删除语义来自服务端：软删除（冻结为只读 + 删除标记），原因必填；
 * 存在进行中/待审核任务时服务端 409 `INVALID_TRANSITION`，这里原样展示原因，
 * 保持弹窗打开以便修正后重试，绝不把拒绝渲染成成功。
 */
import { ref, watch } from 'vue'
import BaseDialog from '@/components/BaseDialog.vue'
import type { ApiError } from '@/api/client'
import type { ProjectSummaryDTO } from '@/api/projects'
import {
  PROJECT_DELETE_HINT,
  deleteErrorHint,
  isProjectDeleted,
  projectStatusLabel,
} from './project-format'

const props = withDefaults(
  defineProps<{
    open: boolean
    /** 待删除项目（单条时长度为 1；列表项版本即 `expected_version`）。 */
    targets: ProjectSummaryDTO[]
    busy?: boolean
    error?: ApiError | null
  }>(),
  { busy: false, error: null },
)

const emit = defineEmits<{ close: []; confirm: [reason: string] }>()

const reason = ref('')
const localError = ref('')

function reset(): void {
  reason.value = ''
  localError.value = ''
}

watch(
  () => props.open,
  (open) => {
    if (open) reset()
  },
  { immediate: true },
)

function submit(): void {
  localError.value = ''
  if (!reason.value.trim()) {
    localError.value = '删除必须填写原因。'
    return
  }
  emit('confirm', reason.value.trim())
}

function onClose(): void {
  if (props.busy) return
  emit('close')
}

function rowVersionText(project: ProjectSummaryDTO): string {
  return `v${project.version}`
}
</script>

<template>
  <BaseDialog
    :open="open"
    :title="targets.length > 1 ? `批量删除 ${targets.length} 个项目` : '删除项目'"
    description="删除后项目进入「已删除」，可在筛选「已删除」中查看并恢复。"
    :close-on-backdrop="!busy"
    @close="onClose"
  >
    <div class="stack stack--loose">
      <p class="notice notice--info" data-testid="project-delete-hint">{{ PROJECT_DELETE_HINT }}</p>

      <section class="section">
        <div class="section-head">
          <h3 class="section-title">待删除项目</h3>
          <span class="badge badge--neutral tabular" data-testid="project-delete-count">
            {{ targets.length }}
          </span>
        </div>
        <ul class="target-list" data-testid="project-delete-targets">
          <li
            v-for="project in targets"
            :key="project.id"
            class="target"
            :data-testid="`project-delete-target-${project.id}`"
          >
            <span class="strong">{{ project.title }}</span>
            <span class="badge badge--neutral" :data-status="project.status">
              {{ projectStatusLabel(project.status) }}
            </span>
            <span class="badge badge--mono tabular">{{ rowVersionText(project) }}</span>
            <span v-if="isProjectDeleted(project)" class="chip chip--warn">已删除（重复提交将按幂等处理）</span>
          </li>
        </ul>
      </section>

      <label class="field">
        <span class="field-label">
          删除原因<span class="field-required" aria-hidden="true">*</span>
        </span>
        <textarea
          v-model="reason"
          class="textarea"
          rows="3"
          :disabled="busy"
          placeholder="例如：需求取消，项目下线"
          data-testid="project-delete-reason"
        />
        <span class="field-hint">原因写入活动记录，删除本身只改标记，不删除任何历史。</span>
      </label>

      <p v-if="localError" class="notice notice--error" role="alert" data-testid="project-delete-local-error">
        {{ localError }}
      </p>

      <p v-if="error" class="notice notice--error" role="alert" data-testid="project-delete-error">
        服务端拒绝（{{ error.code }}）：{{ error.message }}
        <template v-if="deleteErrorHint(error.code)"> {{ deleteErrorHint(error.code) }}</template>
      </p>
    </div>

    <template #footer>
      <button type="button" class="btn" :disabled="busy" data-testid="project-delete-cancel" @click="onClose">
        取消
      </button>
      <button
        type="button"
        class="btn btn--danger"
        :disabled="busy"
        data-testid="project-delete-confirm"
        @click="submit"
      >
        {{ busy ? '提交中…' : '确认删除' }}
      </button>
    </template>
  </BaseDialog>
</template>

<style scoped>
.target-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.target {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-muted);
  min-width: 0;
}
</style>
