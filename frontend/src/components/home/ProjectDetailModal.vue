<script setup lang="ts">
/**
 * 项目详情浮窗（demo 的 `.project-sheet` 外观）：封面横幅 + 项目背景 + 相关文档 + 团队成员 +
 * 进度/状态侧栏 + 生命周期与删除操作。
 *
 * 保留服务端原有的业务约束：成员移除要求显式 WAIT/NOW 处置、归档需要原因、删除是软删除。
 */
import { computed, ref, watch } from 'vue'

import { listAgentsPage, type AgentSummaryDTO } from '@/api/agents'
import { ApiError } from '@/api/client'
import {
  activateProject,
  addMember,
  archiveProject,
  completeProject,
  deleteProject,
  getProject,
  listMembers,
  listProjectDocuments,
  removeMember,
  restoreProject,
  type DocumentItemDTO,
  type ProjectDetailDTO,
  type ProjectMemberDTO,
  type ProjectSummaryDTO,
} from '@/api/projects'
import ProjectDeleteForm from '@/components/project/ProjectDeleteForm.vue'
import ProjectDocumentsPanel from '@/components/project/ProjectDocumentsPanel.vue'
import ProjectLifecyclePanel from '@/components/project/ProjectLifecyclePanel.vue'
import ProjectMembersPanel from '@/components/project/ProjectMembersPanel.vue'
import { isProjectDeleted, isProjectReadOnly, projectDeletedReason, type ProjectAction } from '@/components/project/project-format'
import { useProjectAction } from '@/components/project/useProjectAction'
import AppIcon from './AppIcon.vue'
import ModalShell from './ModalShell.vue'
import { projectStatsById, refreshHomeSilently } from '@/stores/home'
import { projectSkin } from '@/stores/projectSkins'
import { projectProgressPercent, projectStatusMeta } from './home-format'
import { showToast } from './feedback'

const props = defineProps<{ project: ProjectSummaryDTO | null }>()
const emit = defineEmits<{
  close: []
  edit: [project: ProjectDetailDTO]
  viewTasks: [projectId: string]
}>()

const detail = ref<ProjectDetailDTO | null>(null)
const members = ref<ProjectMemberDTO[]>([])
const documents = ref<DocumentItemDTO[]>([])
const agents = ref<AgentSummaryDTO[]>([])
const memberAgentSearch = ref('')
const removalToken = ref(0)
const loading = ref(false)
const error = ref<ApiError | null>(null)
const membersLoading = ref(false)
const membersError = ref<ApiError | null>(null)
const documentsLoading = ref(false)
const documentsError = ref<ApiError | null>(null)
const showDelete = ref(false)
let token = 0

const lifecycleAction = useProjectAction()
const memberAddAction = useProjectAction()
const memberRemoveAction = useProjectAction()
const deleteAction = useProjectAction()

const projectId = computed(() => props.project?.id ?? null)
const skin = computed(() => (props.project ? projectSkin(props.project.id) : null))
const statusMeta = computed(() => (detail.value ? projectStatusMeta(detail.value.status) : null))
const readOnly = computed(() => (detail.value ? isProjectReadOnly(detail.value) || isProjectDeleted(detail.value) : false))
const stats = computed(() => (props.project ? projectStatsById.value.get(props.project.id) ?? null : null))
const percent = computed(() => projectProgressPercent(stats.value?.completion_rate ?? null))

async function load(projectIdToLoad: string): Promise<void> {
  const current = ++token
  loading.value = true
  error.value = null
  detail.value = null
  members.value = []
  documents.value = []
  try {
    const [project, memberPage, documentPage] = await Promise.all([
      getProject(projectIdToLoad),
      listMembers(projectIdToLoad, { pageSize: 100 }),
      listProjectDocuments(projectIdToLoad, { pageSize: 50 }),
    ])
    if (current !== token) return
    detail.value = project
    members.value = memberPage.items
    documents.value = documentPage.items
  } catch (caught) {
    if (current !== token) return
    error.value = caught instanceof ApiError ? caught : null
  } finally {
    if (current === token) loading.value = false
  }
  if (current === token) void loadAgents(memberAgentSearch.value)
}

async function loadMembers(silent = true): Promise<void> {
  const id = projectId.value
  if (!id) return
  if (!silent) membersLoading.value = true
  try {
    const page = await listMembers(id, { pageSize: 100 })
    if (projectId.value !== id) return
    members.value = page.items
    membersError.value = null
  } catch (caught) {
    if (projectId.value !== id) return
    membersError.value = caught instanceof ApiError ? caught : null
  } finally {
    membersLoading.value = false
  }
}

async function loadDocuments(silent = true): Promise<void> {
  const id = projectId.value
  if (!id) return
  if (!silent) documentsLoading.value = true
  try {
    const page = await listProjectDocuments(id, { pageSize: 50 })
    if (projectId.value !== id) return
    documents.value = page.items
    documentsError.value = null
  } catch (caught) {
    if (projectId.value !== id) return
    documentsError.value = caught instanceof ApiError ? caught : null
  } finally {
    documentsLoading.value = false
  }
}

async function loadAgents(keyword: string): Promise<void> {
  try {
    const page = await listAgentsPage({ name: keyword || null, pageSize: 100 })
    agents.value = page.items
  } catch {
    agents.value = []
  }
}

watch(
  () => props.project,
  (project) => {
    if (project) void load(project.id)
    else {
      token += 1
      detail.value = null
    }
  },
  { immediate: true },
)

async function runLifecycle(payload: { action: ProjectAction; reason?: string }): Promise<void> {
  const project = detail.value
  if (!project) return
  try {
    const result = await lifecycleAction.run((key) => {
      switch (payload.action) {
        case 'activate':
          return activateProject(project.id, project.version, key)
        case 'complete':
          return completeProject(project.id, project.version, key)
        case 'archive':
          return archiveProject(project.id, project.version, payload.reason ?? '', key)
        case 'restore':
          return restoreProject(project.id, project.version, key)
      }
    })
    if (!result) return
    showToast(`项目「${result.title}」现在：${projectStatusMeta(result.status).label}`, '🪐')
    await load(project.id)
    void refreshHomeSilently()
  } catch {
    // 服务端拒绝（如存在进行中任务）由 lifecycleAction.error 原样呈现。
  }
}

async function submitAddMember(agentId: string): Promise<void> {
  const id = projectId.value
  if (!id) return
  try {
    const result = await memberAddAction.run((key) => addMember(id, agentId, key))
    if (!result) return
    showToast(`已授权 Agent「${result.agent_name}」加入项目`, '🤝')
    await loadMembers(false)
    void refreshHomeSilently()
  } catch {
    // 重复授权 / 已禁用 Agent 由 memberAddAction.error 呈现。
  }
}

async function submitRemoveMember(payload: {
  agentId: string
  disposition: 'WAIT' | 'NOW' | null
  reason: string
}): Promise<void> {
  const id = projectId.value
  if (!id) return
  try {
    const result = await memberRemoveAction.run((key) =>
      removeMember(
        id,
        payload.agentId,
        { ...(payload.disposition ? { disposition: payload.disposition } : {}), reason: payload.reason },
        key,
      ),
    )
    if (!result) return
    removalToken.value += 1
    showToast(
      result.reclaimed_execution_ids.length > 0
        ? `已移除成员，立即回收 ${result.reclaimed_execution_ids.length} 个执行`
        : '已移除成员授权',
      '🧹',
    )
    await loadMembers(false)
    void refreshHomeSilently()
  } catch {
    // 最后主管保护由 memberRemoveAction.error 呈现，确认框保持打开。
  }
}

function onDocumentsChanged(payload: { message: string }): void {
  showToast(payload.message, '📎')
  void loadDocuments(false)
}

async function submitDelete(reason: string): Promise<void> {
  const project = detail.value
  if (!project) return
  try {
    const result = await deleteAction.run((key) => deleteProject(project.id, reason, project.version, key))
    if (!result) return
    showDelete.value = false
    showToast(`项目「${result.title}」已删除（可在「已删除」筛选里恢复）`, '🗑️')
    void refreshHomeSilently()
    emit('close')
  } catch {
    // 409（存在进行中/待审核任务）由弹窗原样展示。
  }
}
</script>

<template>
  <ModalShell :open="Boolean(project)" variant="project" labelled-by="project-modal-title" @close="emit('close')">
    <template v-if="project && skin">
      <div class="project-banner" :style="{ '--c1': skin.palette.c1, '--c2': skin.palette.c2 }">
        <button class="sheet-close" type="button" aria-label="关闭项目详情" @click="emit('close')">
          <AppIcon name="x" />
        </button>
        <span class="banner-orbit" aria-hidden="true" />
        <span class="banner-emoji" aria-hidden="true">{{ skin.cover.emoji }}</span>
        <div class="banner-copy">
          <span class="banner-overline">PROJECT POCKET</span>
          <span v-if="statusMeta" class="status-pill" :class="statusMeta.className">{{ statusMeta.label }}</span>
          <h2 id="project-modal-title">{{ project.title }}</h2>
          <p>{{ project.summary || '还没有写一句话简介，点「编辑项目」补上一句吧。' }}</p>
        </div>
      </div>

      <div class="sheet-scroll">
        <div v-if="loading" class="board-state" data-testid="project-detail-loading">
          <span class="state-face" aria-hidden="true">🪐</span>
          <strong>正在打开这颗星球…</strong>
        </div>
        <div v-else-if="error" class="board-state is-error" data-testid="project-detail-error">
          <span class="state-face" aria-hidden="true">☁️</span>
          <strong>项目没拉下来</strong>
          <span>{{ error.displayMessage }}</span>
          <button class="btn btn-soft" type="button" @click="load(project.id)">重新加载</button>
        </div>
        <div v-else-if="detail" class="project-sheet-body">
          <div class="project-main">
            <section>
              <div class="block-title">项目背景 <span class="block-num">BACKGROUND</span></div>
              <div v-if="detail.background" class="background-text">{{ detail.background }}</div>
              <div v-else class="background-text is-empty">
                还没有写下项目背景。点「编辑项目」，把这件事的来龙去脉轻轻放进来。
              </div>
            </section>

            <section class="docs-section">
              <div class="docs-head">
                <div class="block-title">
                  相关文档 <span class="doc-count">{{ documents.length }} 份</span>
                </div>
              </div>
              <ProjectDocumentsPanel
                :project-id="detail.id"
                :documents="documents"
                :loading="documentsLoading"
                :error="documentsError"
                :read-only="readOnly"
                @changed="onDocumentsChanged"
                @refresh="loadDocuments()"
              />
            </section>

            <section class="docs-section">
              <div class="docs-head">
                <div class="block-title">
                  团队成员 <span class="doc-count">{{ members.length }} 位</span>
                </div>
              </div>
              <ProjectMembersPanel
                :project-id="detail.id"
                :members="members"
                :agents="agents"
                :loading="membersLoading"
                :error="membersError"
                :read-only="readOnly"
                :busy="memberAddAction.busy.value || memberRemoveAction.busy.value"
                :agent-search="memberAgentSearch"
                :removal-error="memberRemoveAction.error.value"
                :removal-token="removalToken"
                @add="submitAddMember"
                @search="(keyword: string) => { memberAgentSearch = keyword; loadAgents(keyword) }"
                @remove="submitRemoveMember"
                @refresh="loadMembers()"
              />
            </section>

            <section>
              <div class="block-title">项目操作 <span class="block-num">ACTIONS</span></div>
              <ProjectLifecyclePanel
                :project="detail"
                :busy="lifecycleAction.busy.value"
                :error="lifecycleAction.error.value"
                @action="runLifecycle"
              />
              <div v-if="isProjectDeleted(detail)" class="notice notice--info">
                {{ projectDeletedReason(detail) || '该项目已删除（只读）：可在「已删除」筛选里恢复。' }}
              </div>
              <div class="project-danger-row">
                <button class="btn btn-danger" type="button" data-testid="project-delete" @click="showDelete = true">
                  <AppIcon name="trash" /> 删除项目
                </button>
              </div>
            </section>
          </div>

          <aside class="project-side">
            <div class="side-card">
              <div class="side-label">PROGRESS · 项目进度</div>
              <div class="side-progress">
                <strong>{{ percent }}%</strong>
                <span>
                  {{ stats ? `已完成 ${stats.completion?.done ?? 0} / ${stats.task_total} 个任务` : '还没有任务' }}
                </span>
              </div>
              <div class="side-bar">
                <span :style="{ width: `${percent}%` }" />
              </div>
            </div>
            <div class="side-card">
              <div class="side-label">STATUS · 状态与版本</div>
              <div class="side-date">
                <strong>{{ statusMeta?.label ?? project.status }}</strong>
                <span class="muted">版本 v{{ detail.version }} · 背景 v{{ detail.content_version }}</span>
              </div>
            </div>
            <div class="side-card">
              <div class="side-label">TASKS · 任务</div>
              <div class="side-progress">
                <strong>{{ stats?.task_total ?? 0 }} 个</strong>
                <span>
                  待验收 {{ stats?.pending_review ?? 0 }} · 逾期 {{ stats?.overdue_task_total ?? 0 }}
                </span>
              </div>
              <button class="btn btn-soft btn-block" type="button" data-testid="project-view-tasks" @click="emit('viewTasks', detail.id)">
                <AppIcon name="checklist" /> 只看这个项目的任务
              </button>
            </div>
            <div class="side-card">
              <div class="side-label">CREATED · 创建时间</div>
              <div class="side-date">
                <strong>{{ (detail.created_at ?? '').slice(0, 10) || '未知' }}</strong>
                <span class="muted">最近更新 {{ (detail.updated_at ?? '').slice(0, 10) || '—' }}</span>
              </div>
            </div>
          </aside>
        </div>
      </div>

      <div class="project-footer">
        <button class="btn btn-ghost" type="button" @click="emit('close')">关闭</button>
        <div class="footer-actions">
          <button
            v-if="detail"
            class="btn btn-primary"
            type="button"
            data-testid="project-edit"
            @click="emit('edit', detail)"
          >
            <AppIcon name="edit" /> 编辑项目
          </button>
        </div>
      </div>
    </template>
  </ModalShell>

  <ProjectDeleteForm
    :open="showDelete"
    :targets="detail ? [detail] : []"
    :busy="deleteAction.busy.value"
    :error="deleteAction.error.value"
    @close="showDelete = false"
    @confirm="submitDelete"
  />
</template>
