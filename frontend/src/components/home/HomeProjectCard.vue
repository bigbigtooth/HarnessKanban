<script setup lang="ts">
/**
 * 项目卡片：沿用 demo 的 `.project-card`（封面渐变 + 状态胶囊 + emoji + 进度条 + 头像堆）。
 *
 * 真实数据映射：进度取 /dashboard 的完成度，头像堆取该项目任务上的执行者，
 * 资料数取项目文档数（由父组件传入，父组件用一次 /dashboard + 每项目一次文档数缓存得到）。
 */
import { computed } from 'vue'

import type { ProjectSummaryDTO } from '@/api/projects'
import AppIcon from './AppIcon.vue'
import { agentAvatar, projectStatsById } from '@/stores/home'
import { projectSkin } from '@/stores/projectSkins'
import { projectProgressPercent, projectStatusMeta } from './home-format'

const props = defineProps<{
  project: ProjectSummaryDTO
  index: number
  /** 该项目任务上的执行者（最多取前 3 个显示头像）。 */
  workerIds: string[]
  /** 工人名字（用于 tooltip）。 */
  workerName: (agentId: string) => string
}>()

const emit = defineEmits<{ open: [project: ProjectSummaryDTO] }>()

const deleted = computed(() => Boolean(props.project.deleted_at))
const statusMeta = computed(() =>
  deleted.value
    ? { label: '已删除', className: 'status-paused', emoji: '🗑️' }
    : projectStatusMeta(props.project.status),
)
const skin = computed(() => projectSkin(props.project.id))
const stats = computed(() => projectStatsById.value.get(props.project.id) ?? null)
const percent = computed(() => projectProgressPercent(stats.value?.completion_rate ?? null))
const taskTotal = computed(() => stats.value?.task_total ?? 0)
const pendingReview = computed(() => stats.value?.pending_review ?? 0)
const overdue = computed(() => stats.value?.overdue_task_total ?? 0)
const workers = computed(() => props.workerIds.slice(0, 3))

const ariaLabel = computed(
  () =>
    `项目：${props.project.title}，${statusMeta.value.label}，完成度 ${percent.value}%，${taskTotal.value} 个任务，点击查看项目详情`,
)
</script>

<template>
  <article
    class="project-card"
    :class="{ 'is-deleted': deleted }"
    tabindex="0"
    role="button"
    :data-project-id="project.id"
    :data-testid="`project-card-${project.id}`"
    :style="{ '--i': index }"
    :aria-label="ariaLabel"
    @click="emit('open', project)"
    @keydown.enter.prevent="emit('open', project)"
    @keydown.space.prevent="emit('open', project)"
  >
    <div class="project-cover" :style="{ '--c1': skin.palette.c1, '--c2': skin.palette.c2 }">
      <span class="status-pill" :class="statusMeta.className">
        {{ statusMeta.emoji }} {{ statusMeta.label }}
      </span>
      <span class="cover-dots" aria-hidden="true" />
      <span class="cover-emoji" aria-hidden="true">{{ skin.cover.emoji }}</span>
    </div>
    <div class="project-body">
      <div class="project-title-row">
        <h3>{{ project.title }}</h3>
        <span class="project-open" aria-hidden="true"><AppIcon name="arrow" /></span>
      </div>
      <p class="project-tagline">
        {{ project.summary || '还没有写一句话简介，点开项目补上一句。' }}
      </p>
      <div class="project-progress" :aria-label="`项目完成度 ${percent}%`">
        <div class="project-bar">
          <span
            :style="{
              width: `${percent}%`,
              '--bar-a': skin.palette.barA,
              '--bar-b': skin.palette.barB,
            }"
          />
        </div>
        <b>{{ percent }}%</b>
      </div>
      <div class="project-foot">
        <span class="project-meta-group">
          <span v-if="workers.length === 0" class="project-meta">还没有执行者</span>
          <span v-else class="avatar-stack">
            <span
              v-for="agentId in workers"
              :key="agentId"
              class="avatar avatar-sm"
              :style="{ '--a': agentAvatar({ id: agentId, name: workerName(agentId) }).color }"
              :title="workerName(agentId)"
            >
              <span aria-hidden="true">{{ agentAvatar({ id: agentId, name: workerName(agentId) }).emoji }}</span>
              <span class="sr-only">{{ workerName(agentId) }}</span>
            </span>
          </span>
        </span>
        <span class="project-meta-group">
          <span class="project-meta" :title="`共 ${taskTotal} 个任务`">
            <AppIcon name="checklist" />{{ taskTotal }} 个
          </span>
          <span v-if="pendingReview" class="project-meta" :title="`${pendingReview} 个待验收`">
            <AppIcon name="eye" />{{ pendingReview }}
          </span>
          <span v-if="overdue" class="project-meta due-warm" :title="`${overdue} 个逾期`">
            <AppIcon name="clock" />{{ overdue }}
          </span>
        </span>
      </div>
    </div>
  </article>
</template>
