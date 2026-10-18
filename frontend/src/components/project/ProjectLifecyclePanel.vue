<script setup lang="ts">
/**
 * 项目生命周期面板（T30 / D08）。
 *
 * 只提供动作端点对应的按钮（启用/结项/归档/恢复），绝不 PATCH `status`；
 * 服务端拒绝（最后主管、未完成任务、进行中/待审核阻止归档、非法转换）原样回显。
 * 归档原因必填，由本面板内的确认框收集。
 *
 * 视觉：状态徽标与版本徽标放在 `.panel-head`；动作收在 `.btn-group`；
 * 归档是危险动作，统一用 `.btn--danger`；服务端拒绝走 ErrorState（原话 + 错误码）。
 * 注意：`.local-error` 保留在面板体内（不在弹窗里），失败时面板与弹窗同时可见。
 */
import { computed, ref } from 'vue'
import type { ApiError } from '@/api/client'
import type { ProjectDetailDTO } from '@/api/projects'
import BaseDialog from '@/components/BaseDialog.vue'
import ErrorState from '@/components/ErrorState.vue'
import {
  lifecycleActions,
  projectStatusBadgeClass,
  projectStatusLabel,
  type ProjectAction,
} from './project-format'

const props = withDefaults(
  defineProps<{
    project: ProjectDetailDTO | null
    busy?: boolean
    error?: ApiError | null
  }>(),
  { busy: false, error: null },
)

const emit = defineEmits<{
  action: [payload: { action: ProjectAction; reason?: string }]
}>()

const options = computed(() => (props.project ? lifecycleActions(props.project) : []))
const showArchive = ref(false)
const archiveReason = ref('')
const localError = ref('')

function run(action: ProjectAction, needsReason: boolean): void {
  localError.value = ''
  if (needsReason) {
    archiveReason.value = ''
    showArchive.value = true
    return
  }
  emit('action', { action })
}

function confirmArchive(): void {
  const reason = archiveReason.value.trim()
  if (!reason) {
    localError.value = '归档必须填写原因。'
    return
  }
  showArchive.value = false
  emit('action', { action: 'archive', reason })
}
</script>

<template>
  <section class="panel" data-testid="project-lifecycle">
    <header class="panel-head">
      <h4 class="panel-title">项目生命周期</h4>
      <div class="row row--wrap">
        <span
          class="badge"
          :class="projectStatusBadgeClass(project?.status)"
          :data-status="project?.status ?? ''"
          data-testid="project-status"
        >
          {{ projectStatusLabel(project?.status) }}
        </span>
        <span class="badge badge--neutral badge--mono" data-testid="project-version">
          版本 v{{ project?.version ?? '—' }}
        </span>
        <span
          v-if="project?.archived_from_status"
          class="badge badge--warning"
          data-testid="project-archived-from"
        >
          归档前状态：{{ projectStatusLabel(project.archived_from_status) }}
        </span>
      </div>
    </header>

    <div class="panel-body stack">
      <p class="notice notice--info" data-testid="project-lifecycle-hint">
        生命周期由动作端点驱动：草稿需至少一名已启用主管才能启用；结项要求所有未归档任务已完成；
        存在进行中或待审核任务时服务端拒绝归档。
      </p>

      <div class="btn-group">
        <button
          v-for="option in options"
          :key="option.action"
          type="button"
          class="btn"
          :class="option.action === 'archive' ? 'btn--danger' : 'btn--primary'"
          :data-testid="`project-action-${option.action}`"
          :disabled="busy"
          @click="run(option.action, option.needsReason)"
        >
          {{ option.label }}
        </button>
      </div>

      <p
        v-if="localError"
        class="notice notice--error"
        role="alert"
        data-testid="project-lifecycle-local-error"
      >
        {{ localError }}
      </p>
      <ErrorState v-if="error" :error="error" :show-retry="false" />
    </div>

    <BaseDialog
      :open="showArchive"
      title="归档项目"
      description="归档后项目只读；任务、成员与文档都不会被删除，可随时恢复。"
      @close="showArchive = false"
    >
      <label class="field">
        <span class="field-label">归档原因（必填）</span>
        <textarea
          v-model="archiveReason"
          class="textarea"
          rows="3"
          data-testid="project-archive-reason"
        />
      </label>
      <template #footer>
        <button type="button" class="btn" data-testid="project-archive-cancel" @click="showArchive = false">
          取消
        </button>
        <button
          type="button"
          class="btn btn--danger"
          data-testid="project-archive-confirm"
          :disabled="busy"
          @click="confirmArchive"
        >
          确认归档
        </button>
      </template>
    </BaseDialog>
  </section>
</template>
