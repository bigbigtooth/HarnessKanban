<script setup lang="ts">
/**
 * 任务编制表单（T31）：`create` 只创建准备中任务，绝不自动发布；`edit` 走 PATCH 白名单。
 *
 * 服务端规则必须原样呈现：非 PREPARING 状态提交实质字段（要求/验收/难度/标题）
 * 会被 409 INVALID_TRANSITION 拒绝，页面显示该错误而不是静默丢弃。
 */
import { computed, onMounted, ref } from 'vue'
import BaseDialog from '@/components/BaseDialog.vue'
import {
  assignTask,
  createTask,
  editTask,
  uploadStagingFile,
  type TaskDetailDTO,
  type TaskMutationDTO,
} from '@/api/tasks'
import { newIdempotencyKey } from '@/api/client'
import { ClaimMode, DIFFICULTIES, type Difficulty } from '@/api/types'
import { assignableAgents, ensureAgentDirectory } from './boardState'
import { difficultyLabel } from './task-format'
import { useTaskAction } from './useTaskAction'
import TaskErrorNotice from './TaskErrorNotice.vue'

const props = defineProps<{
  mode: 'create' | 'edit'
  projectId: string | null
  task?: TaskDetailDTO | null
}>()

const emit = defineEmits<{
  close: []
  saved: [task: TaskMutationDTO | null, mode: 'create' | 'edit']
  /** 409/404：调用方需要回读服务端状态（编辑模式）。 */
  conflict: [taskId: string]
}>()

const action = useTaskAction()

const title = ref(props.task?.title ?? '')
const description = ref(props.task?.description ?? '')
const acceptance = ref(props.task?.acceptance_criteria ?? '')
const difficulty = ref<Difficulty>((props.task?.difficulty ?? 3) as Difficulty)
const claimMode = ref<ClaimMode>((props.task?.claim_mode ?? ClaimMode.OPEN) as ClaimMode)
const designatedAgentId = ref<string>(props.task?.designated_agent_id ?? '')
const dueAt = ref<string>(toLocalInputValue(props.task?.due_at ?? null))
const localError = ref<string>('')

// 新建任务的暂存附件：提交时先逐个 POST /files，再把 file_id 交给 createTask，
// 服务端会把每个文件的 OSS 签名链接写进任务提示词（description）。
const ATTACHMENT_ACCEPT = '.md,.txt,.pdf,.docx,.xlsx,.png,.jpg,.jpeg,.zip'
const MAX_ATTACHMENTS = 20
const selectedFiles = ref<File[]>([])
const uploading = ref(false)
const uploadProgress = ref('')

function addFiles(fileList: FileList | null): void {
  if (!fileList || fileList.length === 0) return
  const incoming = Array.from(fileList)
  const merged = [...selectedFiles.value]
  for (const file of incoming) {
    if (merged.length >= MAX_ATTACHMENTS) {
      localError.value = `参考附件最多 ${MAX_ATTACHMENTS} 个。`
      break
    }
    if (!merged.some((existing) => existing.name === file.name && existing.size === file.size)) {
      merged.push(file)
    }
  }
  selectedFiles.value = merged
}

function removeFile(index: number): void {
  selectedFiles.value = selectedFiles.value.filter((_, position) => position !== index)
}

async function uploadAttachments(projectId: string): Promise<string[]> {
  const fileIds: string[] = []
  for (let index = 0; index < selectedFiles.value.length; index += 1) {
    const file = selectedFiles.value[index]
    uploadProgress.value = `上传附件中（${index + 1}/${selectedFiles.value.length}）：${file.name}`
    const staged = await uploadStagingFile(projectId, file, newIdempotencyKey())
    fileIds.push(staged.file_id)
  }
  return fileIds
}

const isEdit = computed(() => props.mode === 'edit')
const candidates = computed(() => assignableAgents(props.projectId))
const dialogTitle = computed(() => (isEdit.value ? `编辑任务要求（${props.task?.display_number ?? ''}）` : '新建任务（准备中）'))

function toLocalInputValue(iso: string | null | undefined): string {
  if (!iso) return ''
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return ''
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}T${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`
}

function dueAtIso(): string | null {
  const raw = dueAt.value.trim()
  if (!raw) return null
  const parsed = new Date(raw)
  if (Number.isNaN(parsed.getTime())) return null
  return parsed.toISOString()
}

onMounted(() => {
  void ensureAgentDirectory()
})

async function submit(): Promise<void> {
  localError.value = ''
  if (!isEdit.value && !props.projectId) {
    localError.value = '缺少项目 ID，无法创建任务。'
    return
  }
  if (!title.value.trim()) {
    localError.value = '标题为必填项。'
    return
  }
  if (claimMode.value === ClaimMode.ASSIGNED && !designatedAgentId.value) {
    localError.value = '指定领取必须选择一名指定的 Agent。'
    return
  }

  try {
    if (!isEdit.value) {
      let attachmentFileIds: string[] = []
      uploading.value = true
      try {
        if (selectedFiles.value.length > 0) {
          attachmentFileIds = await uploadAttachments(props.projectId as string)
        }
      } catch (error) {
        uploading.value = false
        uploadProgress.value = ''
        localError.value =
          error instanceof Error && error.message
            ? `附件上传失败：${error.message}`
            : '附件上传失败，请重试。'
        return
      }
      try {
        const created = await action.run((key) =>
          createTask(
            {
              projectId: props.projectId as string,
              title: title.value.trim(),
              description: description.value,
              acceptanceCriteria: acceptance.value,
              difficulty: difficulty.value,
              claimMode: claimMode.value,
              designatedAgentId: claimMode.value === ClaimMode.ASSIGNED ? designatedAgentId.value : null,
              dueAt: dueAtIso(),
              attachmentFileIds,
            },
            key,
          ),
        )
        if (created) emit('saved', created, 'create')
      } finally {
        uploading.value = false
        uploadProgress.value = ''
      }
      return
    }

    const task = props.task
    if (!task) {
      localError.value = '缺少任务数据。'
      return
    }
    const patch: {
      title?: string
      description?: string
      acceptanceCriteria?: string
      difficulty?: Difficulty
      dueAt?: string | null
    } = {}
    if (title.value.trim() !== task.title) patch.title = title.value.trim()
    if (description.value !== (task.description ?? '')) patch.description = description.value
    if (acceptance.value !== (task.acceptance_criteria ?? '')) patch.acceptanceCriteria = acceptance.value
    if (difficulty.value !== task.difficulty) patch.difficulty = difficulty.value
    if (dueAt.value !== toLocalInputValue(task.due_at)) patch.dueAt = dueAtIso()
    if (Object.keys(patch).length === 0) {
      localError.value = '没有需要提交的改动。'
      return
    }
    const updated = await action.run((key) => editTask(task.id, task.version, patch, key))
    if (updated) emit('saved', updated, 'edit')
  } catch {
    // 失败保留输入；冲突（409）时通知调用方回读服务端状态。
    if (action.error.value?.isConflict) emit('conflict', props.task?.id ?? "")
  }
}

/** 编制阶段同时提供“修改领取安排”，避免为了改指定人先创建再改。 */
async function saveAssignment(): Promise<void> {
  const task = props.task
  if (!task) return
  localError.value = ''
  if (claimMode.value === ClaimMode.ASSIGNED && !designatedAgentId.value) {
    localError.value = '指定领取必须选择一名指定的 Agent。'
    return
  }
  if (claimMode.value === task.claim_mode && designatedAgentId.value === (task.designated_agent_id ?? '')) {
    return
  }
  try {
    const updated = await action.run((key) =>
      assignTask(
        task.id,
        task.version,
        claimMode.value,
        claimMode.value === ClaimMode.ASSIGNED ? designatedAgentId.value : null,
        key,
      ),
    )
    if (updated) emit('saved', updated, 'edit')
  } catch {
    // 保留选择，回显错误。
  }
}

function onClose(): void {
  action.reset()
  emit('close')
}
</script>

<template>
  <BaseDialog :open="true" :title="dialogTitle" @close="onClose">
    <div class="form-body">
      <p class="notice notice--info" data-testid="authoring-note">
        {{ isEdit
          ? '编制规则：准备中可修改全部要求；待办只能改标题/截止，实质要求需先撤回；已领取或已完成的要求快照不可覆盖（服务端会拒绝）。'
          : '新建任务固定落到「准备中」，不会自动发布；发布后才会进入待办并参与领取。' }}
      </p>

      <form class="form-grid" data-testid="authoring-form" @submit.prevent="submit">
        <label class="field">
          <span class="field-label">标题<span class="field-required" aria-hidden="true">*</span></span>
          <input
            v-model="title"
            class="input"
            type="text"
            maxlength="200"
            required
            data-testid="authoring-title"
          />
        </label>

        <label class="field">
          <span class="field-label">任务要求（description）</span>
          <textarea v-model="description" class="textarea" rows="4" data-testid="authoring-description" />
        </label>

        <label class="field">
          <span class="field-label">验收标准（acceptance_criteria）</span>
          <textarea v-model="acceptance" class="textarea" rows="3" data-testid="authoring-acceptance" />
        </label>

        <div class="form-grid form-grid--2">
          <label class="field">
            <span class="field-label">难度</span>
            <select v-model.number="difficulty" class="select" data-testid="authoring-difficulty">
              <option v-for="level in DIFFICULTIES" :key="level" :value="level">{{ difficultyLabel(level) }}</option>
            </select>
          </label>

          <label class="field">
            <span class="field-label">领取模式</span>
            <select v-model="claimMode" class="select" data-testid="authoring-claim-mode">
              <option :value="ClaimMode.OPEN">开放抢单</option>
              <option :value="ClaimMode.ASSIGNED">指定领取</option>
            </select>
          </label>

          <label v-if="claimMode === ClaimMode.ASSIGNED" class="field">
            <span class="field-label">指定 Agent<span class="field-required" aria-hidden="true">*</span></span>
            <select v-model="designatedAgentId" class="select" data-testid="authoring-designated">
              <option value="">请选择</option>
              <option v-for="agent in candidates" :key="agent.id" :value="agent.id">
                {{ agent.name }}（{{ agent.role === 'SUPERVISOR' ? '主管' : '员工' }}）
              </option>
            </select>
          </label>
        </div>

        <label class="field">
          <span class="field-label">计划截止 due_at（本地时间，存储为 UTC）</span>
          <input v-model="dueAt" class="input" type="datetime-local" data-testid="authoring-due-at" />
        </label>

        <div v-if="!isEdit" class="field">
          <span class="field-label">参考附件（上传到 OSS，下载链接会写入任务提示词）</span>
          <input
            class="input"
            type="file"
            multiple
            :accept="ATTACHMENT_ACCEPT"
            data-testid="authoring-file-input"
            @change="addFiles(($event.target as HTMLInputElement).files)"
          />
          <ul v-if="selectedFiles.length > 0" class="attachment-list" data-testid="authoring-file-list">
            <li
              v-for="(file, index) in selectedFiles"
              :key="`${file.name}-${file.size}`"
              class="attachment-list__item"
              :data-testid="`authoring-file-chip-${index}`"
            >
              <span class="attachment-list__name">{{ file.name }}</span>
              <span class="attachment-list__size">{{ (file.size / 1024).toFixed(1) }} KB</span>
              <button
                type="button"
                class="btn btn--ghost attachment-list__remove"
                :data-testid="`authoring-file-remove-${index}`"
                :disabled="uploading"
                @click="removeFile(index)"
              >
                移除
              </button>
            </li>
          </ul>
          <p v-if="uploadProgress" class="notice notice--info" data-testid="authoring-upload-progress">
            {{ uploadProgress }}
          </p>
        </div>

        <p v-if="localError" class="notice notice--error" role="alert" data-testid="authoring-local-error">
          {{ localError }}
        </p>
        <TaskErrorNotice :error="action.error.value" />
      </form>
    </div>

    <template #footer>
      <button
        v-if="isEdit && task"
        type="button"
        class="btn form-footer-secondary"
        :disabled="action.busy.value"
        data-testid="authoring-save-assignment"
        @click="saveAssignment"
      >
        只保存领取安排
      </button>
      <button type="button" class="btn" data-testid="authoring-cancel" @click="onClose">取消</button>
      <button
        type="button"
        class="btn btn--primary"
        :disabled="action.busy.value || uploading"
        data-testid="authoring-submit"
        @click="submit"
      >
        {{
          uploading
            ? '上传附件中…'
            : action.busy.value
              ? '提交中…'
              : isEdit
                ? '保存修改'
                : '创建（准备中）'
        }}
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

.form-footer-secondary {
  margin-right: auto;
}

.attachment-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2, 4px);
}

.attachment-list__item {
  display: flex;
  align-items: center;
  gap: var(--space-2, 4px);
  padding: 2px 0;
}

.attachment-list__name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attachment-list__size {
  color: var(--text-muted, #888);
  font-size: 12px;
}

.attachment-list__remove {
  padding: 0 8px;
  font-size: 12px;
}
</style>
