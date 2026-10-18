<script setup lang="ts">
/**
 * 新建 / 编辑任务浮窗（demo 的 `.task-sheet` 外观）。
 *
 * 新建：`POST /tasks`（落在所属项目的「准备中」列，除非在某一列点「加一张卡片」时直接发布）。
 * 编辑：`PATCH /tasks/{id}`，只提交变更字段 + `expected_version`；版本冲突会把服务端提示原样显示。
 */
import { computed, ref, watch } from 'vue'

import { ApiError, newIdempotencyKey } from '@/api/client'
import { createTask, editTask, publishTask, type TaskDetailDTO, type TaskListItemDTO } from '@/api/tasks'
import { DIFFICULTY_LABEL, type Difficulty } from '@/api/types'
import { describeApiError } from '@/components/task/task-format'
import AppIcon from './AppIcon.vue'
import ModalShell from './ModalShell.vue'
import { activeAgents, agentAvatar, projectList } from '@/stores/home'
import { showToast } from './feedback'

const props = defineProps<{
  open: boolean
  mode: 'create' | 'edit'
  /** 新建时预选的项目（空串表示未选，默认取第一个项目）。 */
  projectId?: string
  /** 在某一列点「加一张卡片」时传入，创建后直接发布到该状态（TODO 允许）。 */
  initialStatus?: string
  /** 编辑模式下的任务详情。 */
  task?: TaskDetailDTO | TaskListItemDTO | null
}>()

const emit = defineEmits<{ close: []; saved: [taskId: string | null] }>()

const title = ref('')
const description = ref('')
const acceptance = ref('')
const difficulty = ref<Difficulty>(3)
const claimMode = ref<'OPEN' | 'ASSIGNED'>('OPEN')
const designatedAgentId = ref('')
const dueAt = ref('')
const labelsText = ref('')
const projectId = ref('')
const busy = ref(false)
const error = ref<ApiError | null>(null)
let key = newIdempotencyKey()

const isEdit = computed(() => props.mode === 'edit')
// 指定执行者必须是项目成员（服务端同样校验）；没有项目归属信息时保守地全部列出。
const agentOptions = computed(() =>
  activeAgents.value.filter(
    (agent) => !agent.project_ids || agent.project_ids.includes(projectId.value),
  ),
)
const difficultyOptions = computed(() =>
  ([1, 2, 3, 4, 5] as Difficulty[]).map((level) => ({ value: level, label: DIFFICULTY_LABEL[level] })),
)

function reset(): void {
  const task = props.task
  key = newIdempotencyKey()
  error.value = null
  busy.value = false
  if (task) {
    title.value = task.title
    description.value = task.description ?? ''
    acceptance.value = task.acceptance_criteria ?? ''
    difficulty.value = task.difficulty
    claimMode.value = task.claim_mode
    designatedAgentId.value = task.designated_agent_id ?? ''
    dueAt.value = (task.due_at ?? '').slice(0, 10)
    labelsText.value = (task.labels ?? []).join(', ')
    projectId.value = task.project_id
    return
  }
  title.value = ''
  description.value = ''
  acceptance.value = ''
  difficulty.value = 3
  claimMode.value = 'OPEN'
  designatedAgentId.value = ''
  dueAt.value = ''
  labelsText.value = ''
  projectId.value = props.projectId || projectList.items.find((p) => !p.deleted_at)?.id || ''
}

watch(() => props.open, (open) => { if (open) reset() }, { immediate: true })

function parseLabels(): string[] {
  return labelsText.value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 10)
}

async function submit(): Promise<void> {
  if (busy.value) return
  const trimmed = title.value.trim()
  if (!trimmed) {
    error.value = new ApiError({ code: 'INVALID_ARGUMENT', message: '请先写一个任务标题', httpStatus: 400 })
    return
  }
  if (!projectId.value) {
    error.value = new ApiError({ code: 'INVALID_ARGUMENT', message: '请选择所属项目', httpStatus: 400 })
    return
  }
  // 直接发布到「待办」及之后的状态时，服务端要求任务要求与验收标准都已填写。
  const publishesOnCreate = !isEdit.value && props.initialStatus !== 'PREPARING'
  if (publishesOnCreate && (!description.value.trim() || !acceptance.value.trim())) {
    error.value = new ApiError({
      code: 'INVALID_ARGUMENT',
      message: '要直接发布，需要先填写任务要求与验收标准',
      httpStatus: 400,
      details: { fields: 'description, acceptance_criteria' },
    })
    return
  }
  busy.value = true
  error.value = null
  try {
    if (isEdit.value && props.task) {
      await editTask(
        props.task.id,
        props.task.version,
        {
          title: trimmed,
          description: description.value,
          acceptanceCriteria: acceptance.value,
          difficulty: difficulty.value,
          dueAt: dueAt.value ? new Date(`${dueAt.value}T00:00:00Z`).toISOString() : null,
          labels: parseLabels(),
        },
        key,
      )
      showToast(`已保存 ${props.task.display_number ?? ''} 的要求`, '🎀')
      emit('saved', props.task.id)
    } else {
      const created = await createTask(
        {
          projectId: projectId.value,
          title: trimmed,
          description: description.value,
          acceptanceCriteria: acceptance.value,
          difficulty: difficulty.value,
          claimMode: claimMode.value,
          designatedAgentId: claimMode.value === 'ASSIGNED' ? designatedAgentId.value || undefined : undefined,
          dueAt: dueAt.value ? new Date(`${dueAt.value}T00:00:00Z`).toISOString() : undefined,
          labels: parseLabels(),
        },
        key,
      )
      // 在「待办 / 进行中…」列点加号时，直接发布到该列（准备中列保持草稿）。
      if (props.initialStatus && props.initialStatus !== 'PREPARING') {
        try {
          await publishTask(created.id, created.version, newIdempotencyKey())
          showToast(`任务 ${created.display_number} 已发布`, '🌱')
        } catch (publishError) {
          showToast(
            `任务已创建但未发布：${publishError instanceof ApiError ? publishError.displayMessage : '发布失败'}`,
            '☁️',
          )
        }
      } else {
        showToast(`已在准备中创建 ${created.display_number}`, '🌱')
      }
      emit('saved', created.id)
    }
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught : null
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <ModalShell :open="open" variant="task" labelled-by="task-form-title" @close="emit('close')">
    <form class="task-form" novalidate @submit.prevent="submit">
      <div class="task-form-head">
        <div>
          <span class="modal-overline">{{ isEdit ? 'TASK DETAILS' : 'A NEW TINY TASK' }}</span>
          <h2 id="task-form-title">{{ isEdit ? '编辑这张小卡' : '种一张新卡片' }}</h2>
        </div>
        <button class="sheet-close on-plain" type="button" aria-label="关闭" @click="emit('close')">
          <AppIcon name="x" />
        </button>
      </div>

      <div class="task-form-body">
        <div v-if="error" class="notice notice--error" data-testid="task-form-error">
          <strong>{{ describeApiError(error).message }}</strong>
          <span>{{ describeApiError(error).hint }}</span>
          <span v-if="error.request_id" class="mono">request_id: {{ error.request_id }}</span>
        </div>

        <div class="field">
          <label for="task-title">任务标题 <span class="field-hint">清楚一点，未来的你会感谢你</span></label>
          <input id="task-title" v-model="title" class="input" type="text" maxlength="200" placeholder="比如：把草莓奶油配色搬进首页" autocomplete="off" data-testid="task-form-title" />
        </div>

        <div class="field">
          <label for="task-project">所属项目</label>
          <select id="task-project" v-model="projectId" class="select" data-testid="task-form-project">
            <option value="" disabled>选择项目</option>
            <option v-for="project in projectList.items.filter((item) => !item.deleted_at)" :key="project.id" :value="project.id">
              {{ project.title }}
            </option>
          </select>
        </div>

        <div class="field">
          <label for="task-desc">任务要求 <span class="field-hint">可选</span></label>
          <textarea id="task-desc" v-model="description" class="textarea" maxlength="4000" placeholder="写清楚背景、要做的事和交付物。" data-testid="task-form-description" />
        </div>

        <div class="field">
          <label for="task-acceptance">验收标准 <span class="field-hint">可选</span></label>
          <textarea id="task-acceptance" v-model="acceptance" class="textarea textarea-sm" maxlength="4000" placeholder="满足什么条件算完成。" data-testid="task-form-acceptance" />
        </div>

        <div class="form-row">
          <div class="field">
            <label for="task-difficulty">难度</label>
            <select id="task-difficulty" v-model.number="difficulty" class="select" data-testid="task-form-difficulty">
              <option v-for="option in difficultyOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
            </select>
          </div>
          <div class="field">
            <label for="task-due">计划截止 <span class="field-hint">可选</span></label>
            <input id="task-due" v-model="dueAt" class="input" type="date" data-testid="task-form-due" />
          </div>
        </div>

        <div class="form-row">
          <div class="field">
            <label for="task-claim">领取模式</label>
            <select id="task-claim" v-model="claimMode" class="select" data-testid="task-form-claim-mode">
              <option value="OPEN">开放领取</option>
              <option value="ASSIGNED">指定执行者</option>
            </select>
          </div>
          <div v-if="claimMode === 'ASSIGNED'" class="field">
            <label for="task-agent">指定 Agent</label>
            <select id="task-agent" v-model="designatedAgentId" class="select" data-testid="task-form-agent">
              <option value="">选择 Agent</option>
              <option v-for="agent in agentOptions" :key="agent.id" :value="agent.id">
                {{ agentAvatar(agent).emoji }} {{ agent.name }}
              </option>
            </select>
          </div>
        </div>

        <div class="field">
          <label for="task-labels">标签 <span class="field-hint">逗号分隔，最多 10 个</span></label>
          <input id="task-labels" v-model="labelsText" class="input" type="text" placeholder="前端, 设计" data-testid="task-form-labels" />
        </div>

        <div class="modal-task-note">
          <span aria-hidden="true">🌼</span>
          <span>
            {{
              isEdit
                ? '保存只提交改动字段；服务端版本不一致会提示冲突，内容会为你保留。'
                : initialStatus && initialStatus !== 'PREPARING'
                  ? '任务要求与验收标准都填写后会直接发布到对应列，否则先落在「准备中」。'
                  : '新任务先落在「准备中」，发布后 Agent 才能领取。'
            }}
          </span>
        </div>
      </div>

      <div class="task-form-footer">
        <div class="footer-actions">
          <button class="btn btn-ghost" type="button" @click="emit('close')">取消</button>
          <button class="btn btn-primary" type="submit" :disabled="busy" data-testid="task-form-submit">
            {{ busy ? '保存中…' : isEdit ? '保存修改' : '创建任务' }}
          </button>
        </div>
      </div>
    </form>
  </ModalShell>
</template>
