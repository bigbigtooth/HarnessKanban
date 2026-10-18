<script setup lang="ts">
/**
 * 项目文档面板（T30 / D09 / T11/T12）。
 *
 * - 上传：multipart；同名时服务端 409 `DOCUMENT_CONFLICT`，界面必须让用户**显式选择**
 *   “上传为新版本”（同一文档 version+1）或“新建同名文档”（allow_duplicate_name）；
 *   绝不做静默覆盖，也绝不覆盖历史版本；
 * - 列表显示名称 / 大小 / 版本 / 上传者 / 时间；下载走服务端受控链接；
 * - 移出参考列表：DELETE 文档（历史版本与文件仍保留，不会删除）；界面不承诺在线编辑；
 * - 归档项目只读。
 *
 * 视觉：文档用 `.table`（名称/版本/大小/上传者/上传时间/操作），上传入口放在 `.panel-foot`
 * 并保持整块可见；“移出参考列表”是危险动作，统一 `.btn--danger` 且保留确认流程。
 * 每个文档一个 `<tbody>` 以承载行级 `data-testid`，因此下方 scoped 样式补回行分隔线。
 */
import { computed, ref } from 'vue'
import { ApiError } from '@/api/client'
import type { DocumentItemDTO, DocumentVersionDTO, ProjectContextVersionDTO } from '@/api/projects'
import {
  archiveDocument,
  fileDownloadUrl,
  uploadDocumentVersion,
  uploadProjectDocument,
} from '@/api/projects'
import BaseDialog from '@/components/BaseDialog.vue'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import { useProjectAction } from './useProjectAction'
import { documentVersionSummary, formatBytes, uploaderLabel } from './project-format'

const props = withDefaults(
  defineProps<{
    projectId: string
    documents: DocumentItemDTO[]
    loading?: boolean
    error?: ApiError | null
    readOnly?: boolean
  }>(),
  { loading: false, error: null, readOnly: false },
)

const emit = defineEmits<{
  /** 文档变化：父组件刷新列表并回显新的上下文版本。 */
  changed: [payload: { message: string; contextVersion?: ProjectContextVersionDTO | null }]
  refresh: []
}>()

const uploadAction = useProjectAction()
const versionAction = useProjectAction()
const archiveAction = useProjectAction()

const newFile = ref<File | null>(null)
const newName = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const notice = ref('')
const localError = ref('')

// 同名冲突：等待用户显式选择
interface ConflictState {
  file: File
  name: string
  existing: DocumentItemDTO | null
}
const conflict = ref<ConflictState | null>(null)

// 移出参考列表的确认
const archiveTarget = ref<DocumentItemDTO | null>(null)

const previewVersions = ref<Record<string, DocumentVersionDTO[]>>({})

const sortedDocuments = computed(() =>
  [...props.documents].sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN')),
)

function versionOf(document: DocumentItemDTO): DocumentVersionDTO | null {
  return document.current_version ?? null
}

function pickFile(event: Event): void {
  const input = event.target as HTMLInputElement
  newFile.value = input.files && input.files.length > 0 ? input.files[0]! : null
  if (newFile.value && !newName.value) newName.value = newFile.value.name
}

function resetUploadForm(): void {
  newFile.value = null
  newName.value = ''
  if (fileInput.value) fileInput.value.value = ''
}

async function upload(): Promise<void> {
  localError.value = ''
  notice.value = ''
  if (props.readOnly) {
    localError.value = '项目已归档：文档只读，请先恢复项目。'
    return
  }
  const file = newFile.value
  if (!file) {
    localError.value = '请先选择要上传的文件。'
    return
  }
  const name = newName.value.trim() || file.name
  try {
    const result = await uploadAction.run((key) =>
      uploadProjectDocument(props.projectId, { file, name }, key),
    )
    if (!result) return
    resetUploadForm()
    notice.value = `已上传文档「${result.name}」（v${result.current_version?.number ?? 1}）。`
    emit('changed', { message: notice.value, contextVersion: result.current_context_version ?? null })
  } catch (thrown) {
    if (thrown instanceof ApiError && thrown.code === 'DOCUMENT_CONFLICT') {
      const existing =
        sortedDocuments.value.find((doc) => doc.name.toLowerCase() === name.toLowerCase()) ?? null
      conflict.value = { file, name, existing }
      return
    }
    localError.value = thrown instanceof Error ? thrown.message : '上传失败'
  }
}

/** 用户显式选择“上传为新版本”：同一文档 version+1。 */
async function chooseNewVersion(): Promise<void> {
  const state = conflict.value
  if (!state?.existing) return
  // 这是一次新的逻辑动作（负载与首次上传不同），必须换新幂等键。
  versionAction.reset()
  try {
    const result = await versionAction.run((key) =>
      uploadDocumentVersion(state.existing!.id, state.file, key),
    )
    if (!result) return
    conflict.value = null
    resetUploadForm()
    notice.value = `文档「${result.name}」已更新到 v${result.current_version?.number ?? '?'}（历史版本保留）。`
    emit('changed', { message: notice.value, contextVersion: result.current_context_version ?? null })
  } catch {
    // 错误由 versionAction.error 呈现，冲突框保持打开以便重试。
  }
}

/** 用户显式选择“新建同名文档”：服务端放行同名（allow_duplicate_name=true）。 */
async function chooseNewDocument(): Promise<void> {
  const state = conflict.value
  if (!state) return
  uploadAction.reset()
  try {
    const result = await uploadAction.run((key) =>
      uploadProjectDocument(
        props.projectId,
        { file: state.file, name: state.name, allowDuplicateName: true },
        key,
      ),
    )
    if (!result) return
    conflict.value = null
    resetUploadForm()
    notice.value = `已新建同名文档「${result.name}」（v${result.current_version?.number ?? 1}）。`
    emit('changed', { message: notice.value, contextVersion: result.current_context_version ?? null })
  } catch {
    // 错误由 uploadAction.error 呈现。
  }
}

function cancelConflict(): void {
  conflict.value = null
}

async function uploadNewVersion(document: DocumentItemDTO, event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files && input.files.length > 0 ? input.files[0]! : null
  input.value = ''
  if (!file) return
  localError.value = ''
  versionAction.reset()
  try {
    const result = await versionAction.run((key) => uploadDocumentVersion(document.id, file, key))
    if (!result) return
    notice.value = `文档「${result.name}」已更新到 v${result.current_version?.number ?? '?'}（历史版本保留）。`
    emit('changed', { message: notice.value, contextVersion: result.current_context_version ?? null })
  } catch {
    // 错误由 versionAction.error 呈现。
  }
}

function openArchive(document: DocumentItemDTO): void {
  archiveTarget.value = document
}

async function confirmArchive(): Promise<void> {
  const target = archiveTarget.value
  if (!target) return
  archiveAction.reset()
  try {
    const result = await archiveAction.run((key) => archiveDocument(target.id, key))
    if (!result) return
    archiveTarget.value = null
    notice.value = `文档「${result.name}」已移出参考列表（历史版本与文件仍保留）。`
    emit('changed', { message: notice.value, contextVersion: result.current_context_version ?? null })
  } catch {
    // 错误由 archiveAction.error 呈现。
  }
}

function toggleVersions(document: DocumentItemDTO): void {
  if (previewVersions.value[document.id]) {
    const next = { ...previewVersions.value }
    delete next[document.id]
    previewVersions.value = next
    return
  }
  previewVersions.value = {
    ...previewVersions.value,
    [document.id]: document.current_version ? [document.current_version] : [],
  }
}
</script>

<template>
  <section class="panel" data-testid="project-documents">
    <header class="panel-head">
      <h4 class="panel-title">项目文档（{{ documents.length }}）</h4>
      <button type="button" class="btn btn--sm" data-testid="project-documents-refresh" @click="emit('refresh')">
        刷新文档
      </button>
    </header>

    <div class="panel-body stack">
      <p class="notice notice--info" data-testid="project-documents-hint">
        文档为受控版本文件：上传后只能下载查看，本系统不提供在线编辑；每次上传都会生成新的上下文版本。
      </p>

      <p v-if="readOnly" class="notice notice--warning" data-testid="project-documents-readonly">
        项目已归档：文档只读（不能上传、换版或移出），恢复项目后才能修改。
      </p>

      <p v-if="notice" class="notice notice--success" role="status" data-testid="project-documents-notice">
        {{ notice }}
      </p>

      <LoadingState v-if="loading" label="加载文档…" />
      <ErrorState v-else-if="error" :error="error" @retry="emit('refresh')" />
      <EmptyState v-else-if="documents.length === 0" title="暂无文档" description="可上传项目背景参考文件。" />
      <div v-else class="table-wrap">
        <table class="table doc-table" data-testid="project-document-list">
          <thead>
            <tr>
              <th scope="col">名称</th>
              <th scope="col">版本</th>
              <th scope="col">大小</th>
              <th scope="col">上传者</th>
              <th scope="col">上传时间</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody
            v-for="document in sortedDocuments"
            :key="document.id"
            :data-testid="`project-document-${document.id}`"
          >
            <tr :data-testid="`project-document-meta-${document.id}`">
              <td class="strong" :data-testid="`project-document-name-${document.id}`">
                {{ document.name }}
              </td>
              <td class="table__num">v{{ versionOf(document)?.number ?? '—' }}</td>
              <td class="table__num">{{ formatBytes(versionOf(document)?.size) }}</td>
              <td>{{ uploaderLabel(versionOf(document)) }}</td>
              <td class="muted text-xs">
                {{ versionOf(document)?.created_at ?? document.created_at ?? '—' }}
              </td>
              <td>
                <div class="row row--wrap doc-actions">
                  <a
                    v-if="versionOf(document)?.file_id || document.file_id"
                    class="btn btn--sm"
                    :href="fileDownloadUrl(versionOf(document)?.file_id ?? document.file_id!)"
                    :data-testid="`project-document-download-${document.id}`"
                  >
                    下载当前版本
                  </a>
                  <button
                    type="button"
                    class="btn btn--sm"
                    :data-testid="`project-document-versions-${document.id}`"
                    @click="toggleVersions(document)"
                  >
                    版本信息
                  </button>
                  <label v-if="!readOnly" class="btn btn--sm upload-version">
                    <span>上传新版本</span>
                    <input
                      class="sr-only"
                      type="file"
                      :data-testid="`project-document-new-version-${document.id}`"
                      @change="uploadNewVersion(document, $event)"
                    />
                  </label>
                  <button
                    v-if="!readOnly"
                    type="button"
                    class="btn btn--sm btn--danger"
                    :data-testid="`project-document-archive-${document.id}`"
                    @click="openArchive(document)"
                  >
                    移出参考列表
                  </button>
                </div>
              </td>
            </tr>
            <tr v-if="previewVersions[document.id]">
              <td colspan="6">
                <ul
                  class="version-list"
                  :data-testid="`project-document-versions-list-${document.id}`"
                >
                  <li v-for="version in previewVersions[document.id]" :key="version.id">
                    {{ documentVersionSummary(version) }}
                  </li>
                </ul>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p
        v-if="localError"
        class="notice notice--error"
        role="alert"
        data-testid="project-documents-local-error"
      >
        {{ localError }}
      </p>
      <ErrorState v-if="uploadAction.error.value" :error="uploadAction.error.value" :show-retry="false" />
      <ErrorState v-if="versionAction.error.value" :error="versionAction.error.value" :show-retry="false" />
      <ErrorState v-if="archiveAction.error.value" :error="archiveAction.error.value" :show-retry="false" />
    </div>

    <div v-if="!readOnly" class="panel-foot upload-block">
      <label class="field upload-block__field">
        <span class="field-label">选择文件</span>
        <input
          ref="fileInput"
          class="input"
          type="file"
          data-testid="project-document-file"
          @change="pickFile"
        />
      </label>
      <label class="field upload-block__field">
        <span class="field-label">文档名称（默认取文件名）</span>
        <input
          v-model="newName"
          class="input"
          type="text"
          data-testid="project-document-name"
          autocomplete="off"
        />
      </label>
      <button
        type="button"
        class="btn btn--primary"
        data-testid="project-document-upload"
        :disabled="uploadAction.busy.value"
        @click="upload"
      >
        {{ uploadAction.busy.value ? '上传中…' : '上传新文档' }}
      </button>
    </div>

    <!-- 同名冲突：显式选择新版本还是新文档 -->
    <BaseDialog
      :open="conflict !== null"
      title="已存在同名文档"
      description="服务端拒绝静默覆盖：请明确选择把这次上传作为“新版本”还是“新建同名文档”。"
      @close="cancelConflict"
    >
      <div class="stack" data-testid="project-document-conflict">
        <p>
          文档名：<strong data-testid="project-document-conflict-name">{{ conflict?.name }}</strong>
        </p>
        <p class="field-hint">
          {{
            conflict?.existing
              ? '选择“上传为新版本”会在同一文档下生成 v(n+1)，历史版本保留；选择“新建同名文档”会创建一份独立的同名文档。'
              : '当前参考列表中已无同名文档，只能新建同名文档。'
          }}
        </p>
      </div>
      <template #footer>
        <button type="button" class="btn" data-testid="project-document-conflict-cancel" @click="cancelConflict">
          取消
        </button>
        <button
          type="button"
          class="btn"
          data-testid="project-document-conflict-new-document"
          :disabled="uploadAction.busy.value"
          @click="chooseNewDocument"
        >
          新建同名文档
        </button>
        <button
          v-if="conflict?.existing"
          type="button"
          class="btn btn--primary"
          data-testid="project-document-conflict-new-version"
          :disabled="versionAction.busy.value"
          @click="chooseNewVersion"
        >
          上传为新版本
        </button>
      </template>
    </BaseDialog>

    <!-- 移出参考列表确认（服务端不接收原因字段，因此界面不假装收集原因） -->
    <BaseDialog
      :open="archiveTarget !== null"
      :title="`移出参考列表：${archiveTarget?.name ?? ''}`"
      description="移出后该文档不再出现在当前参考列表与后续上下文版本中；历史版本与文件仍保留，可继续追溯下载。"
      @close="archiveTarget = null"
    >
      <p data-testid="project-document-archive-confirm-text">
        确认把「{{ archiveTarget?.name }}」移出当前参考列表？该操作会推进项目的内容版本，但不会删除任何历史版本。
      </p>
      <template #footer>
        <button type="button" class="btn" data-testid="project-document-archive-cancel" @click="archiveTarget = null">
          取消
        </button>
        <button
          type="button"
          class="btn btn--danger"
          data-testid="project-document-archive-confirm"
          :disabled="archiveAction.busy.value"
          @click="confirmArchive"
        >
          确认移出
        </button>
      </template>
    </BaseDialog>
  </section>
</template>

<style scoped>
/* 每个文档一个 <tbody>（行级 testid 需要），补回共享表格样式里被去掉的行分隔线。 */
.table.doc-table tbody tr td {
  border-bottom: 1px solid var(--color-border);
}

.table.doc-table tbody:last-of-type tr:last-child td {
  border-bottom: none;
}

/* 上传入口：与只读列表分区，始终可见、可直接操作。 */
.upload-block {
  align-items: flex-end;
  justify-content: flex-start;
  gap: var(--space-3);
  background: var(--color-surface-muted);
}

.upload-block__field {
  flex: 1 1 240px;
}

.upload-version {
  cursor: pointer;
}

.doc-actions {
  justify-content: flex-end;
}

.version-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
  padding: 0;
  list-style: none;
  color: var(--color-text-muted);
  font-size: var(--text-xs);
}
</style>
