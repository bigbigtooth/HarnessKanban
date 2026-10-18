<script setup lang="ts">
/**
 * 应用外壳：demo 风格的顶栏（品牌 + 任务/项目胶囊标签 + 右上角动作区）加内容区。
 *
 * 首页只有两个核心模块（任务 / 项目）；全局看板、Agent 管理、活动保留在「更多」里，
 * 右上角还有一个半透明的设置按钮（环境变量 + LLM 设置）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import AppIcon from '@/components/home/AppIcon.vue'
import SettingsDialog from '@/components/home/SettingsDialog.vue'
import { burstConfetti } from '@/components/home/feedback'
import ConfettiHost from '@/components/home/ConfettiHost.vue'
import ToastHost from '@/components/home/ToastHost.vue'
import LoginScreen from '@/components/auth/LoginScreen.vue'
import { authReady, authState, initAuth, logout } from '@/stores/auth'
import { agentAvatar, refreshShellCounts, shellCounts } from '@/stores/home'
import { theme, toggleTheme } from '@/stores/theme'

const route = useRoute()
const router = useRouter()
const settingsOpen = ref(false)
const moreOpen = ref(false)
const moreWrap = ref<HTMLElement | null>(null)
let countTimer: ReturnType<typeof setInterval> | null = null

/** 点击菜单外部或按 Esc 关闭「更多」菜单。 */
function onDocumentPointerDown(event: MouseEvent): void {
  if (!moreOpen.value) return
  if (moreWrap.value && !moreWrap.value.contains(event.target as Node)) moreOpen.value = false
}

function onDocumentKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') moreOpen.value = false
}

/** 顶部标签：任务（/）与项目（/projects）。 */
const activeTab = computed(() => (route.name === 'home-projects' ? 'projects' : 'tasks'))

/** 小分队的展示头像（固定一只，人数取在线 Agent 数）。 */
const teamPreview = computed(() => agentAvatar({ id: 'harvesstask-team', name: '小分队' }))
const teamCount = computed(() => shellCounts.onlineAgents)

function switchTab(tab: 'tasks' | 'projects'): void {
  if (tab === 'projects') void router.push({ name: 'home-projects' })
  else void router.push({ name: 'home-tasks' })
}

function goHome(): void {
  void router.push({ name: 'home-tasks' })
}

function toggleMore(): void {
  moreOpen.value = !moreOpen.value
}

function closeMore(): void {
  moreOpen.value = false
}

function onThemeToggle(): void {
  const next = toggleTheme()
  if (next === 'dark') burstConfetti()
}

/** 登录通过后才开始取顶栏计数（未登录时不发任何业务请求）。 */
function startShellData(): void {
  void refreshShellCounts()
  if (countTimer !== null) return
  // 二级页面没有首页的轮询，这里每 30s 刷新一次计数（3 个 pageSize=1 的查询，成本很低）。
  countTimer = setInterval(() => {
    if (route.name === 'home-tasks' || route.name === 'home-projects') return
    void refreshShellCounts()
  }, 30_000)
}

async function onLogout(): Promise<void> {
  moreOpen.value = false
  await logout()
}

watch(authReady, (ready) => {
  if (ready) startShellData()
})

onMounted(async () => {
  document.addEventListener('pointerdown', onDocumentPointerDown)
  document.addEventListener('keydown', onDocumentKeydown)
  // 先确认服务端是否要求访问密钥：要求且未通过时只渲染登录页。
  await initAuth()
  if (authReady.value) startShellData()
})

onBeforeUnmount(() => {
  if (countTimer !== null) clearInterval(countTimer)
  document.removeEventListener('pointerdown', onDocumentPointerDown)
  document.removeEventListener('keydown', onDocumentKeydown)
})
</script>

<template>
  <div v-if="authState.checking" class="auth-boot" data-testid="auth-checking">
    <div class="ambient" aria-hidden="true">
      <span class="blob blob-a" />
      <span class="blob blob-b" />
      <span class="blob blob-c" />
      <span class="ambient-ring" />
    </div>
    <div class="auth-boot-card">
      <span class="state-face" aria-hidden="true">🫧</span>
      <strong>正在确认访问权限…</strong>
    </div>
  </div>

  <LoginScreen v-else-if="!authReady" />

  <template v-else>
  <div class="ambient" aria-hidden="true">
    <span class="blob blob-a" />
    <span class="blob blob-b" />
    <span class="blob blob-c" />
    <span class="ambient-ring" />
    <span class="doodle doodle-1">☁︎</span>
    <span class="doodle doodle-2">✦</span>
    <span class="doodle doodle-3">✿</span>
    <span class="doodle doodle-4">☁️</span>
    <span class="doodle doodle-5">✦</span>
  </div>

  <header class="topbar">
    <a class="brand" href="#" aria-label="Harness看板首页" @click.prevent="goHome">
      <span class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 40 40" fill="none">
          <defs>
            <linearGradient id="brandGradient" x1="5" y1="4" x2="36" y2="38" gradientUnits="userSpaceOnUse">
              <stop stop-color="#d4f9e9" />
              <stop offset="1" stop-color="#c6e7ff" />
            </linearGradient>
          </defs>
          <rect x="2.2" y="2.2" width="35.6" height="35.6" rx="12.6" fill="url(#brandGradient)" stroke="white" stroke-width="1.6" />
          <ellipse cx="14.2" cy="18.1" rx="1.8" ry="2.25" fill="#2c4845" />
          <ellipse cx="25.8" cy="18.1" rx="1.8" ry="2.25" fill="#2c4845" />
          <circle cx="10.6" cy="23.1" r="2.1" fill="#ffb7c7" />
          <circle cx="29.4" cy="23.1" r="2.1" fill="#ffb7c7" />
          <path d="M14.1 24.1c2.8 3.15 9 3.15 11.8 0" stroke="#2c4845" stroke-width="1.9" stroke-linecap="round" />
          <path d="M20 2.2c.2 2.7 1.5 4 4.2 4.4" stroke="#7ec9ad" stroke-width="1.7" stroke-linecap="round" />
        </svg>
      </span>
      <span class="brand-copy">
        <span class="brand-name">Harness<em>看板</em></span>
        <span class="brand-sub">HarnessKanban</span>
      </span>
    </a>

    <nav
      class="tab-switch"
      :class="{ 'is-projects': activeTab === 'projects' }"
      role="tablist"
      aria-label="工作区视图"
    >
      <span class="tab-glider" aria-hidden="true" />
      <button
        class="tab"
        role="tab"
        type="button"
        :aria-selected="activeTab === 'tasks'"
        :tabindex="activeTab === 'tasks' ? 0 : -1"
        data-tab="tasks"
        data-testid="tab-tasks"
        @click="switchTab('tasks')"
      >
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <rect x="3" y="3" width="14" height="14" rx="4" />
          <path d="M7 10h6M7 13h3.5" />
          <path d="M7 7h4" />
        </svg>
        <span>任务</span>
        <span class="tab-count" data-testid="task-tab-count">{{ shellCounts.tasks }}</span>
      </button>
      <button
        class="tab"
        role="tab"
        type="button"
        :aria-selected="activeTab === 'projects'"
        :tabindex="activeTab === 'projects' ? 0 : -1"
        data-tab="projects"
        data-testid="tab-projects"
        @click="switchTab('projects')"
      >
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M3.2 6.7c0-1.1.9-2 2-2h2.4l1.5 1.7h5.7c1.1 0 2 .9 2 2v5.9c0 1.1-.9 2-2 2H5.2c-1.1 0-2-.9-2-2V6.7Z" />
          <path d="M3.5 9.2h13" />
        </svg>
        <span>项目</span>
        <span class="tab-count" data-testid="project-tab-count">{{ shellCounts.projects }}</span>
      </button>
    </nav>

    <div class="top-actions">
      <RouterLink class="team-pill" to="/agents" aria-label="小分队成员" data-testid="team-pill">
        <span class="avatar avatar-sm team-avatar" :style="{ '--a': teamPreview.color }" aria-hidden="true">
          {{ teamPreview.emoji }}
        </span>
        <span class="team-label">{{ teamCount }} 位在线</span>
      </RouterLink>

      <div ref="moreWrap" class="more-wrap">
        <button
          class="icon-btn"
          type="button"
          aria-label="更多页面"
          title="更多页面"
          aria-haspopup="menu"
          :aria-expanded="moreOpen"
          data-testid="more-menu"
          @click="toggleMore"
        >
          <AppIcon name="dots" />
        </button>
        <div v-if="moreOpen" class="more-menu" role="menu">
          <RouterLink class="more-item" to="/dashboard" role="menuitem" data-testid="more-dashboard" @click="closeMore">
            <AppIcon name="grid" /> 全局看板
          </RouterLink>
          <RouterLink class="more-item" to="/agents" role="menuitem" data-testid="more-agents" @click="closeMore">
            <AppIcon name="robot" /> Agent 管理
          </RouterLink>
          <RouterLink class="more-item" to="/activity" role="menuitem" data-testid="more-activity" @click="closeMore">
            <AppIcon name="pulse" /> 活动日志
          </RouterLink>
          <button v-if="authState.required" class="more-item" type="button" role="menuitem" data-testid="more-logout" @click="onLogout">
            <AppIcon name="key" /> 退出登录
          </button>
        </div>
      </div>

      <button
        class="icon-btn theme-btn"
        type="button"
        :aria-label="theme === 'light' ? '切换到深色主题' : '切换到浅色主题'"
        :title="theme === 'light' ? '深色主题' : '浅色主题'"
        data-testid="theme-toggle"
        @click="onThemeToggle"
      >
        <AppIcon :name="theme === 'light' ? 'moon' : 'sun'" />
      </button>

      <button
        class="icon-btn settings-btn"
        type="button"
        aria-label="设置"
        title="设置（环境变量 / LLM）"
        data-testid="open-settings"
        @click="settingsOpen = true"
      >
        <AppIcon name="sliders" />
      </button>
    </div>
  </header>

  <RouterView />

  <SettingsDialog :open="settingsOpen" @close="settingsOpen = false" />
  </template>

  <ToastHost />
  <ConfettiHost />
</template>

<style scoped>
.more-wrap {
  position: relative;
}
</style>
