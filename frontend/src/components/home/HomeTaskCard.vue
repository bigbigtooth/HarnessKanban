<script setup lang="ts">
/**
 * 看板任务卡片：沿用 demo 的 `.task-card` 结构，内容换成真实任务字段。
 *
 * 卡片左侧是标签（任务 labels 的第一项，没有标签时用状态名），右上角是难度徽标；
 * 底部一行是「项目 · 截止 · 附件/执行进度」加上负责人头像。点击整张卡片打开任务详情浮窗。
 */
import { computed, ref } from 'vue'

import { DIFFICULTY_TIMEOUT_SECONDS, type Difficulty } from '@/api/types'
import type { TaskListItemDTO } from '@/api/tasks'
import AppIcon from './AppIcon.vue'
import { projectCoverView, projectById } from '@/stores/home'
import { claimModeLabel } from '@/components/task/task-format'
import { dueMeta, tagPalette } from './home-format'
import { TASK_STATUS_LABEL } from './home-format'
import type { AvatarView } from '@/stores/home'

const props = defineProps<{
  task: TaskListItemDTO
  index: number
  /** 负责人头像（指派/实际执行者），无则显示“待领取”。 */
  owner?: { name: string; avatar: AvatarView } | null
}>()

const emit = defineEmits<{
  open: [task: TaskListItemDTO]
  dragstart: [task: TaskListItemDTO]
  dragend: [task: TaskListItemDTO]
}>()

/** 拖动中：卡片自己降透明度，并在拖动结束后的极短时间内抑制“点击打开详情”。 */
const dragging = ref(false)

const isDone = computed(() => props.task.status === 'DONE')
const isArchived = computed(() => props.task.is_archived)
const difficulty = computed(() => props.task.difficulty as Difficulty)
const tag = computed(() => props.task.labels?.[0] || TASK_STATUS_LABEL[props.task.status])
const palette = computed(() => tagPalette(tag.value))
const due = computed(() => dueMeta(props.task.due_at, isDone.value))
const project = computed(() => projectById.value.get(props.task.project_id) ?? null)
const projectCover = computed(() => (project.value ? projectCoverView(project.value.id) : null))
const claimLabel = computed(() => claimModeLabel(props.task.claim_mode))
const timeoutLabel = computed(() => {
  const seconds = DIFFICULTY_TIMEOUT_SECONDS[difficulty.value]
  if (seconds >= 3600) return `${Math.round(seconds / 3600)}h 时限`
  return `${Math.round(seconds / 60)}m 时限`
})

function onDragStart(event: DragEvent): void {
  dragging.value = true
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move'
    // Firefox 需要写入数据才会开始拖拽
    event.dataTransfer.setData('text/plain', props.task.id)
  }
  emit('dragstart', props.task)
}

function onDragEnd(): void {
  emit('dragend', props.task)
  // dragend 与随后的 click 几乎同时触发：稍等再恢复，避免拖完就弹出详情浮窗。
  window.setTimeout(() => {
    dragging.value = false
  }, 120)
}

function onActivate(): void {
  if (dragging.value) return
  emit('open', props.task)
}

const ariaLabel = computed(() => {
  const pieces = [props.task.title, tag.value, TASK_STATUS_LABEL[props.task.status]]
  if (props.owner?.name) pieces.push(`负责人 ${props.owner.name}`)
  pieces.push('点击查看任务详情，可拖动换列')
  return pieces.join('，')
})
</script>

<template>
  <article
    class="task-card"
    :class="{ 'is-done': isDone, dragging }"
    draggable="true"
    tabindex="0"
    role="button"
    :data-task-id="task.id"
    :data-project-id="task.project_id"
    :data-status="task.status"
    :data-display-number="`TASK-${String(task.number).padStart(4, '0')}`"
    :data-testid="`task-card-${task.id}`"
    :style="{ '--i': index }"
    :aria-label="ariaLabel"
    @click="onActivate"
    @keydown.enter.prevent="onActivate"
    @keydown.space.prevent="onActivate"
    @dragstart="onDragStart"
    @dragend="onDragEnd"
  >
    <div class="card-top">
      <span class="tag" :class="`tag-${palette}`">
        <span class="tag-emoji" aria-hidden="true">🏷️</span>{{ tag }}
      </span>
      <span class="priority" :class="difficulty >= 4 ? 'priority-high' : 'priority-low'">
        {{ difficulty >= 4 ? '⚡' : '☁️' }} L{{ difficulty }}
      </span>
    </div>
    <h3>
      <span class="task-number">{{ `TASK-${String(task.number).padStart(4, '0')}` }}</span>
      {{ task.title }}
    </h3>
    <p v-if="task.description" class="task-desc">{{ task.description }}</p>
    <div class="card-foot">
      <div class="card-meta">
        <span
          v-if="project"
          class="meta-item meta-project"
          :title="`所属项目：${project.title}`"
          :style="{ '--dot': projectCover?.dot }"
        >
          <span aria-hidden="true">{{ project.title }}</span>
        </span>
        <span v-if="due" class="meta-item meta-due" :class="due.className" :title="due.title">
          <AppIcon name="calendar" />{{ due.label }}
        </span>
        <span class="meta-item" :title="`领取模式：${claimLabel}`">
          <AppIcon name="bolt" />{{ claimLabel }}
        </span>
        <span v-if="task.designated_agent_id && !task.assignee_agent_id" class="meta-item" title="指定执行者，等待领取">
          <AppIcon name="users" />指定
        </span>
        <span v-if="isArchived" class="meta-item" title="已归档">📦 归档</span>
        <span v-else-if="task.status === 'PREPARING'" class="meta-item" :title="timeoutLabel">🌾 待发布</span>
      </div>
      <span v-if="owner" class="card-owner" :title="owner.name">
        <span class="avatar avatar-sm" :style="{ '--a': owner.avatar.color }">
          <span aria-hidden="true">{{ owner.avatar.emoji }}</span>
          <span class="sr-only">{{ owner.name }}</span>
        </span>
      </span>
      <span v-else-if="isDone" class="done-badge" title="已完成" aria-label="已完成">
        <AppIcon name="check" />
      </span>
      <span v-else class="card-owner card-owner--empty" title="还没有执行者">👤</span>
    </div>
  </article>
</template>
