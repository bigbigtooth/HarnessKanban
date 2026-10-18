<script setup lang="ts">
/**
 * 结果提交表单（T32）：
 * 1) 先以 multipart 上传执行产物（purpose=EXECUTION_ARTIFACT + execution_id）并完成绑定；
 * 2) 再用 execution_id / expected_version / summary / content / self_check / artifacts 正式提交。
 *
 * 网络失败保留全部输入，并按**同一个幂等键**重试（每次上传各自一个键，成功后才换键）。
 * 提交必须在固定截止时刻之前；页面不提供续期，也不自行缩短/延长时限。
 */
import { computed, onMounted, ref } from 'vue'
import BaseDialog from '@/components/BaseDialog.vue'
import { ApiError } from '@/api/client'
import {
  submitTask,
  uploadTaskAttachment,
  type SubmitReceiptDTO,
  type TaskDetailDTO,
  type TaskListItemDTO,
} from '@/api/tasks'
import { newIdempotencyKey } from '@/api/client'
import { formatDateTimeWithZone, formatRemaining, formatFileSize } from './task-format'
import { useTaskAction } from './useTaskAction'
import TaskErrorNotice from './TaskErrorNotice.vue'

const props = defineProps<{
  task: TaskListItemDTO
  detail: TaskDetailDTO | null
  nowMs: number
}>()

const emit = defineEmits<{
  close: []
  submitted: [receipt: SubmitReceiptDTO]
  conflict: [taskId: string]
}>()

const action = useTaskAction()

interface SelfCheckRow {
  criterion: string
  passed: boolean
  evidence: string
}

interface UploadedArtifact {
  fileId: string
  name: string
  size: number
}

interface PendingUpload {
  name: string
  file: File
  /** 该文件的幂等键：上传失败重试必须复用同一个键。 */
  key: string
  error: ApiError | null
  busy: boolean
}

const summary = ref('')
const content = ref('')
const selfCheck = ref<SelfCheckRow[]>([])
const links = ref<Array<{ title: string; url: string }>>([])
const artifacts = ref<UploadedArtifact[]>([])
const pending = ref<PendingUpload[]>([])
const localError = ref('')
const receipt = ref<SubmitReceiptDTO | null>(null)

const execution = computed(() => props.detail?.current_execution ?? null)
const deadline = computed(() => execution.value?.execution_deadline_at ?? null)
const timeoutSeconds = computed(
  () => execution.value?.execution_timeout_seconds ?? props.task.execution_timeout_seconds ?? null,
)

onMounted(() => {
  const criteria = (props.detail?.acceptance_criteria ?? props.task.acceptance_criteria ?? '')
    .split('\n')
    .map((line) => line.replace(/^[-*\s]+/, '').trim())
    .filter((line) => line.length > 0)
  selfCheck.value = criteria.map((criterion) => ({ criterion, passed: false, evidence: '' }))
})

function addLink(): void {
  links.value.push({ title: '', url: '' })
}

function removeLink(index: number): void {
  links.value.splice(index, 1)
}

function addCustomCriterion(): void {
  selfCheck.value.push({ criterion: '', passed: false, evidence: '' })
}

/** 上传产物：同一文件的失败重试复用原幂等键。 */
async function onFilesSelected(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  const executionId = execution.value?.execution_id
  if (!executionId) {
    localError.value = '缺少当前执行 ID，无法上传产物（任务可能已结束或被回收）。'
    return
  }
  for (const file of files) {
    const entry: PendingUpload = { name: file.name, file, key: newIdempotencyKey(), error: null, busy: true }
    pending.value.push(entry)
    await uploadOne(entry, executionId)
  }
}

async function uploadOne(entry: PendingUpload, executionId: string): Promise<void> {
  entry.busy = true
  entry.error = null
  try {
    const attachment = await uploadTaskAttachment(
      props.task.id,
      { file: entry.file, purpose: 'EXECUTION_ARTIFACT', executionId },
      entry.key,
    )
    artifacts.value.push({ fileId: attachment.file_id, name: attachment.original_name, size: attachment.size })
    // 注意：pending 是响应式数组，读到的元素是代理对象，必须按 key 移除而不是按引用比较。
    pending.value = pending.value.filter((item) => item.key !== entry.key)
  } catch (thrown) {
    entry.error = thrown instanceof ApiError ? thrown : new ApiError({
      code: 'NETWORK_ERROR',
      message: thrown instanceof Error ? thrown.message : '上传失败',
      httpStatus: 0,
      kind: 'network',
    })
    entry.busy = false
  }
}

/** 上传失败重试：复用该文件原来的幂等键。 */
async function retryUpload(entry: PendingUpload): Promise<void> {
  const executionId = execution.value?.execution_id
  if (!executionId) return
  await uploadOne(entry, executionId)
}

function removeArtifact(fileId: string): void {
  artifacts.value = artifacts.value.filter((item) => item.fileId !== fileId)
}

async function submit(): Promise<void> {
  localError.value = ''
  if (!execution.value) {
    localError.value = '当前没有可提交的执行（可能已被回收或提交过）。'
    return
  }
  if (pending.value.some((item) => item.busy)) {
    localError.value = '仍有产物正在上传，请等待上传结束后再提交。'
    return
  }
  if (pending.value.length > 0) {
    localError.value = '存在上传失败的产物：请重试上传（沿用原幂等键）或移除后再提交。'
    return
  }
  if (!summary.value.trim()) {
    localError.value = '结果摘要 summary 为必填项。'
    return
  }
  if (summary.value.trim().length > 200) {
    localError.value = '结果摘要不能超过 200 字。'
    return
  }
  const invalidLink = links.value.find((link) => (link.title.trim() || link.url.trim()) && (!link.title.trim() || !link.url.trim()))
  if (invalidLink) {
    localError.value = '外链必须同时填写标题与 http(s) 地址。'
    return
  }
  const linksPayload = links.value
    .filter((link) => link.title.trim() && link.url.trim())
    .map((link) => ({ title: link.title.trim(), url: link.url.trim() }))
  const selfCheckPayload = selfCheck.value
    .filter((row) => row.criterion.trim())
    .map((row) => ({ criterion: row.criterion.trim(), passed: row.passed, evidence: row.evidence }))

  try {
    // 关键：expected_version 取提交时刻的最新已知版本（详情优先）；失败重试复用同一幂等键。
    const result = await action.run((key) =>
      submitTask(
        props.task.id,
        {
          executionId: execution.value!.execution_id,
          expectedVersion: props.detail?.version ?? props.task.version,
          summary: summary.value.trim(),
          content: content.value,
          selfCheck: selfCheckPayload,
          artifacts: artifacts.value.map((item) => item.fileId),
          links: linksPayload,
        },
        key,
      ),
    )
    if (!result) return
    receipt.value = result
    emit('submitted', result)
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
  <BaseDialog :open="true" :title="`提交结果 ${task.display_number}`" @close="onClose">
    <div class="form-body">
      <section class="panel">
        <div class="panel-head">
          <h3 class="panel-title">提交约束（服务端判定）</h3>
        </div>
        <div class="panel-body panel-body--tight">
          <dl class="meta-list" data-testid="submit-head">
            <dt>结果摘要上限</dt>
            <dd>200 字（服务端校验）</dd>
            <dt>当前执行 ID</dt>
            <dd data-testid="submit-execution-id"><code class="mono">{{ execution?.execution_id ?? '无（不可提交）' }}</code></dd>
            <dt>固定超时</dt>
            <dd class="tabular" data-testid="submit-timeout">
              {{ timeoutSeconds == null ? '—' : `${timeoutSeconds} 秒` }}
            </dd>
            <dt>固定截止时刻</dt>
            <dd class="tabular" data-testid="submit-deadline">
              {{ deadline ? formatDateTimeWithZone(deadline) : '—' }}
              <span class="submit-remaining" data-testid="submit-remaining">
                （{{ formatRemaining(deadline, nowMs) }}，由服务端数据库时间判定；不支持续期）
              </span>
            </dd>
          </dl>
        </div>
      </section>

      <form v-if="!receipt" class="form-grid" @submit.prevent="submit">
        <label class="field">
          <span class="field-label">结果摘要 summary（必填）<span class="field-required" aria-hidden="true">*</span></span>
          <input v-model="summary" class="input" type="text" maxlength="200" data-testid="submit-summary" />
        </label>

        <label class="field">
          <span class="field-label">交付内容 content（Markdown）</span>
          <textarea v-model="content" class="textarea" rows="5" data-testid="submit-content" />
        </label>

        <fieldset class="field fieldset">
          <legend class="field-label">产物文件（先上传绑定，再随正式提交引用）</legend>
          <input class="input file-input" type="file" multiple data-testid="submit-artifacts" @change="onFilesSelected" />
          <ul v-if="artifacts.length" class="record-list" data-testid="submit-artifact-list">
            <li v-for="item in artifacts" :key="item.fileId" class="record-row">
              <code class="mono" data-testid="submit-artifact-id">{{ item.fileId }}</code>
              <span class="record-name">{{ item.name }}（{{ formatFileSize(item.size) }}）</span>
              <button type="button" class="btn btn--sm btn--danger" @click="removeArtifact(item.fileId)">移除</button>
            </li>
          </ul>
          <ul v-if="pending.length" class="record-list" data-testid="submit-upload-list">
            <li
              v-for="(item, index) in pending"
              :key="item.key"
              class="record-row"
              :data-testid="`submit-upload-${index}`"
            >
              <span class="record-name">{{ item.name }}：</span>
              <span v-if="item.busy" class="muted">上传中…</span>
              <span v-else-if="item.error" class="upload-error" data-testid="submit-upload-error">
                上传失败：{{ item.error.message }}（{{ item.error.code }}）
              </span>
              <button v-if="item.error" type="button" class="btn btn--sm" @click="retryUpload(item)">
                重试（同一幂等键）
              </button>
            </li>
          </ul>
        </fieldset>

        <fieldset class="field fieldset">
          <legend class="field-label">验收自检 self_check</legend>
          <ul class="record-list" data-testid="submit-selfcheck">
            <li v-for="(row, index) in selfCheck" :key="index" class="selfcheck-row">
              <input
                v-model="row.criterion"
                class="input"
                type="text"
                :data-testid="`selfcheck-criterion-${index}`"
                placeholder="验收标准"
              />
              <label class="checkbox-line">
                <input v-model="row.passed" type="checkbox" :data-testid="`selfcheck-passed-${index}`" />
                已满足
              </label>
              <input
                v-model="row.evidence"
                class="input"
                type="text"
                :data-testid="`selfcheck-evidence-${index}`"
                placeholder="证据说明"
              />
            </li>
          </ul>
          <button type="button" class="btn btn--sm" data-testid="submit-add-criterion" @click="addCustomCriterion">
            添加自检项
          </button>
        </fieldset>

        <fieldset class="field fieldset">
          <legend class="field-label">外部链接（仅文本引用，不抓取）</legend>
          <ul class="record-list">
            <li v-for="(link, index) in links" :key="index" class="link-row">
              <input v-model="link.title" class="input" type="text" :data-testid="`submit-link-title-${index}`" placeholder="标题" />
              <input v-model="link.url" class="input" type="url" :data-testid="`submit-link-url-${index}`" placeholder="https://" />
              <button type="button" class="btn btn--sm btn--ghost" @click="removeLink(index)">删除</button>
            </li>
          </ul>
          <button type="button" class="btn btn--sm" data-testid="submit-add-link" @click="addLink">添加外链</button>
        </fieldset>

        <p v-if="localError" class="notice notice--error" role="alert" data-testid="submit-local-error">
          {{ localError }}
        </p>
        <TaskErrorNotice :error="action.error.value" />
      </form>

      <section v-else class="panel receipt" role="status" data-testid="submit-receipt">
        <div class="panel-head">
          <p class="panel-title">提交成功：任务已进入待审核</p>
        </div>
        <div class="panel-body">
          <dl class="meta-list">
            <dt>提交 ID</dt>
            <dd data-testid="submit-receipt-id"><code class="mono">{{ receipt.submission_id }}</code></dd>
            <dt>任务状态</dt>
            <dd data-testid="submit-receipt-status">{{ receipt.status }}</dd>
          </dl>
        </div>
      </section>
    </div>

    <template #footer>
      <button type="button" class="btn" data-testid="submit-cancel" @click="onClose">
        {{ receipt ? '关闭' : '取消' }}
      </button>
      <button
        v-if="!receipt"
        type="button"
        class="btn btn--primary"
        :disabled="action.busy.value"
        data-testid="submit-submit"
        @click="submit"
      >
        {{ action.busy.value ? '提交中…' : action.error.value ? '按原幂等键重试提交' : '正式提交' }}
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

.fieldset {
  border: 0;
  margin: 0;
  padding: 0;
  min-width: 0;
}

.file-input {
  padding: var(--space-1) var(--space-2);
  font-size: var(--text-xs);
}

.record-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.record-row,
.link-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-muted);
  font-size: var(--text-xs);
  min-width: 0;
}

.record-row button {
  margin-left: auto;
}

.record-name {
  color: var(--color-text-secondary);
  word-break: break-word;
}

.upload-error {
  color: var(--color-danger);
}

.selfcheck-row {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) max-content minmax(0, 1fr);
  gap: var(--space-2);
  align-items: center;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-muted);
}

@media (max-width: 720px) {
  .selfcheck-row {
    grid-template-columns: minmax(0, 1fr);
  }
}

.link-row .input {
  flex: 1 1 12rem;
}

.submit-remaining {
  color: var(--color-text-muted);
}

.receipt {
  border-color: var(--color-success-border);
}
</style>
