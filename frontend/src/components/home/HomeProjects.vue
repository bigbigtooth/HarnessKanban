<script setup lang="ts">
/**
 * 项目模块：状态筛选胶囊（含数量）+ 搜索 + 卡片网格 + 新建项目卡片。
 *
 * 与 demo 一致：点卡片打开项目详情浮窗，「新建项目」打开表单浮窗。
 */
import { computed, onMounted, ref } from 'vue'

import type { ProjectSummaryDTO } from '@/api/projects'
import AppIcon from './AppIcon.vue'
import HomeProjectCard from './HomeProjectCard.vue'
import { agentName, loadProjects, projectList, projectStatsById, projectTaskIndex } from '@/stores/home'

const emit = defineEmits<{
  openProject: [project: ProjectSummaryDTO]
  createProject: []
}>()

type ProjectChip = { value: string; label: string }

const CHIPS: ProjectChip[] = [
  { value: 'all', label: '全部' },
  { value: 'DRAFT', label: '草稿' },
  { value: 'ACTIVE', label: '进行中' },
  { value: 'COMPLETED', label: '已结项' },
  { value: 'ARCHIVED', label: '已归档' },
  { value: 'deleted', label: '已删除' },
]

const chip = ref('all')
const query = ref('')

function matchesChip(project: ProjectSummaryDTO): boolean {
  if (chip.value === 'all') return !project.deleted_at
  if (chip.value === 'deleted') return Boolean(project.deleted_at)
  return !project.deleted_at && project.status === chip.value
}

function chipCount(value: string): number {
  return projectList.items.filter((project) => {
    if (value === 'all') return !project.deleted_at
    if (value === 'deleted') return Boolean(project.deleted_at)
    return !project.deleted_at && project.status === value
  }).length
}

const visibleProjects = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  return projectList.items.filter((project) => {
    if (!matchesChip(project)) return false
    if (!keyword) return true
    const haystack = [project.title, project.summary ?? ''].join(' ').toLowerCase()
    return haystack.includes(keyword)
  })
})

/** 项目下正在干活的 Agent（来自 store 的独立任务索引，不受任务模块筛选影响）。 */
const workersByProject = computed(() => projectTaskIndex.workers)

const totalDocsHint = computed(() => {
  const projects = projectList.items.filter((project) => !project.deleted_at)
  // 任务总数取 /dashboard 的项目行（精确到全部任务，不随任务模块筛选变化）。
  const tasks = projects.reduce(
    (sum, project) => sum + (projectStatsById.value.get(project.id)?.task_total ?? 0),
    0,
  )
  return `${projects.length} 个项目 · ${tasks} 个任务`
})

const filtered = computed(() => query.value.trim().length > 0 || chip.value !== 'all')

function clearFilters(): void {
  chip.value = 'all'
  query.value = ''
}

onMounted(() => {
  if (projectList.status === 'idle') void loadProjects()
})
</script>

<template>
  <section class="view view-projects" data-testid="view-home-projects">
    <div class="page-wrap">
      <div class="view-head project-head">
        <div class="view-head-copy">
          <div class="eyebrow">
            <span class="live-dot" aria-hidden="true" />
            <span>PROJECT POCKET</span>
            <span class="eyebrow-sep" aria-hidden="true" />
            <span class="eyebrow-long">把想法养成小星球</span>
          </div>
          <h1>每个项目，都是一颗<span class="highlight">小星球</span><span class="title-sticker" aria-hidden="true">🪐</span></h1>
          <p>背景、文档和小伙伴，都收在同一个软软的口袋里。</p>
        </div>
        <div class="view-head-side project-head-side">
          <div class="mini-count" data-testid="project-summary">
            <span class="mini-spark" aria-hidden="true">📎</span>
            <span>{{ totalDocsHint }}</span>
          </div>
          <button class="btn btn-primary btn-lg" type="button" data-testid="new-project" @click="emit('createProject')">
            <AppIcon name="plus" />
            新建项目
          </button>
        </div>
      </div>

      <div class="project-toolbar">
        <div class="filter-chips" role="group" aria-label="按项目状态筛选">
          <button
            v-for="item in CHIPS"
            :key="item.value"
            type="button"
            class="chip"
            :data-project-filter="item.value"
            :aria-pressed="chip === item.value"
            @click="chip = item.value"
          >
            {{ item.label }} <span class="chip-count">{{ chipCount(item.value) }}</span>
          </button>
        </div>
        <label class="search wide">
          <AppIcon name="search" />
          <span class="sr-only">搜索项目</span>
          <input
            v-model="query"
            type="search"
            placeholder="搜索项目 / 简介"
            autocomplete="off"
            data-testid="project-search"
          />
        </label>
      </div>

      <div v-if="projectList.status === 'loading' && projectList.items.length === 0" class="board-state" data-testid="projects-loading">
        <span class="state-face" aria-hidden="true">🪐</span>
        <strong>正在整理小星球…</strong>
        <span>马上就好。</span>
      </div>
      <div v-else-if="projectList.status === 'error' && projectList.items.length === 0" class="board-state is-error" data-testid="projects-error">
        <span class="state-face" aria-hidden="true">☁️</span>
        <strong>项目没拉下来</strong>
        <span>{{ projectList.error?.displayMessage ?? '请稍后重试' }}</span>
        <button class="btn btn-soft" type="button" @click="loadProjects()">
          重新加载
        </button>
      </div>
      <div v-else class="project-grid" aria-label="项目列表" data-testid="project-grid">
        <HomeProjectCard
          v-for="(project, index) in visibleProjects"
          :key="project.id"
          :project="project"
          :index="index"
          :worker-ids="workersByProject.get(project.id) ?? []"
          :worker-name="agentName"
          @open="emit('openProject', $event)"
        />
        <button class="new-project-tile" type="button" data-testid="new-project-tile" aria-label="新建项目" @click="emit('createProject')">
          <span class="tile-plus" aria-hidden="true"><AppIcon name="plus" /></span>
          <span>新建一个项目</span>
          <small>把新的小星球装进口袋</small>
        </button>
        <div v-if="visibleProjects.length === 0" class="empty-projects">
          <div class="empty-emoji" aria-hidden="true">{{ filtered ? '☁️' : '🌱' }}</div>
          <h3>{{ filtered ? '没有找到这颗小星球' : '还没有项目，先种一颗吧' }}</h3>
          <p>
            {{
              filtered
                ? '换个关键词或状态看看，灵感可能藏在下一颗星球里。'
                : '项目背景、相关文档和小伙伴，都可以收进同一个口袋。'
            }}
          </p>
          <button v-if="filtered" class="btn btn-soft" type="button" @click="clearFilters">清空筛选</button>
          <button v-else class="btn btn-primary" type="button" @click="emit('createProject')">
            <AppIcon name="plus" /> 新建第一个项目
          </button>
        </div>
      </div>
    </div>
  </section>
</template>
