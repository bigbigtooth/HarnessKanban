<script setup lang="ts">
/**
 * 登录页：服务端配置了 WEBUI_ACCESS_KEY 时，未登录只能看到这一页。
 *
 * 外观沿用 demo 的薄荷玻璃卡（深色主题自动跟随），只做一件事：输入访问密钥。
 * 密钥由部署方配置在 `backend/.env` 或 `backend/secret/webui_access_key`。
 */
import { nextTick, onMounted, ref } from 'vue'

import AppIcon from '@/components/home/AppIcon.vue'
import { authState, initAuth, login } from '@/stores/auth'
import { theme, toggleTheme } from '@/stores/theme'

const key = ref('')
const busy = ref(false)
const input = ref<HTMLInputElement | null>(null)

async function submit(): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    const ok = await login(key.value)
    if (ok) key.value = ''
  } finally {
    busy.value = false
    await nextTick()
    input.value?.focus()
  }
}

async function retry(): Promise<void> {
  busy.value = true
  try {
    await initAuth()
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  input.value?.focus()
})
</script>

<template>
  <div class="auth-gate">
    <div class="ambient" aria-hidden="true">
      <span class="blob blob-a" />
      <span class="blob blob-b" />
      <span class="blob blob-c" />
      <span class="ambient-ring" />
      <span class="doodle doodle-1">☁︎</span>
      <span class="doodle doodle-2">✦</span>
      <span class="doodle doodle-3">✿</span>
    </div>

    <div class="auth-card" role="form" aria-labelledby="auth-title">
      <div class="auth-brand">
        <span class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 40 40" fill="none">
            <defs>
              <linearGradient id="authBrandGradient" x1="5" y1="4" x2="36" y2="38" gradientUnits="userSpaceOnUse">
                <stop stop-color="#d4f9e9" />
                <stop offset="1" stop-color="#c6e7ff" />
              </linearGradient>
            </defs>
            <rect x="2.2" y="2.2" width="35.6" height="35.6" rx="12.6" fill="url(#authBrandGradient)" stroke="white" stroke-width="1.6" />
            <ellipse cx="14.2" cy="18.1" rx="1.8" ry="2.25" fill="#2c4845" />
            <ellipse cx="25.8" cy="18.1" rx="1.8" ry="2.25" fill="#2c4845" />
            <circle cx="10.6" cy="23.1" r="2.1" fill="#ffb7c7" />
            <circle cx="29.4" cy="23.1" r="2.1" fill="#ffb7c7" />
            <path d="M14.1 24.1c2.8 3.15 9 3.15 11.8 0" stroke="#2c4845" stroke-width="1.9" stroke-linecap="round" />
            <path d="M20 2.2c.2 2.7 1.5 4 4.2 4.4" stroke="#7ec9ad" stroke-width="1.7" stroke-linecap="round" />
          </svg>
        </span>
        <span class="brand-copy">
          <span class="brand-name">Harness<em>Kanban</em></span>
          <span class="brand-sub">Harness看板</span>
        </span>
      </div>

      <span class="modal-overline">ACCESS KEY REQUIRED</span>
      <h1 id="auth-title">请输入访问密钥</h1>
      <p class="auth-hint">
        这台 Harness看板 已开启访问保护。密钥由管理员配置在服务端
        <code>WEBUI_ACCESS_KEY</code>（或 <code>secret/webui_access_key</code>），登录后 30 天内免再次输入。
      </p>

      <form class="auth-form" novalidate @submit.prevent="submit">
        <label class="sr-only" for="webui-key">访问密钥</label>
        <input
          id="webui-key"
          ref="input"
          v-model="key"
          class="input auth-input"
          type="password"
          autocomplete="current-password"
          placeholder="粘贴访问密钥"
          data-testid="auth-key"
          :disabled="busy"
        />
        <button class="btn btn-primary auth-submit" type="submit" :disabled="busy" data-testid="auth-submit">
          <AppIcon name="key" />
          {{ busy ? '验证中…' : '登录' }}
        </button>
      </form>

      <p v-if="authState.error" class="notice notice--error auth-error" role="alert" data-testid="auth-error">
        {{ authState.error }}
      </p>

      <div class="auth-foot">
        <button class="btn btn-ghost btn-sm" type="button" :disabled="busy" data-testid="auth-retry" @click="retry">
          <AppIcon name="refresh" /> 重试连接
        </button>
        <button class="btn btn-ghost btn-sm" type="button" data-testid="auth-theme" @click="toggleTheme">
          <AppIcon :name="theme === 'light' ? 'moon' : 'sun'" />
          {{ theme === 'light' ? '深色' : '浅色' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.auth-gate {
  position: fixed;
  inset: 0;
  z-index: 5;
  display: grid;
  place-items: center;
  padding: 24px;
  overflow: auto;
}

.auth-card {
  position: relative;
  z-index: 1;
  width: min(440px, 100%);
  display: grid;
  gap: 12px;
  padding: 28px 28px 22px;
  border-radius: 26px;
  border: 1px solid var(--glass-strong);
  background: var(--glass-strong);
  box-shadow: var(--shadow-lg);
  backdrop-filter: blur(24px) saturate(150%);
  -webkit-backdrop-filter: blur(24px) saturate(150%);
  animation: sheet-pop 0.42s var(--ease-spring) both;
}

.auth-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}

.auth-brand .brand-mark {
  width: 44px;
  height: 44px;
}

.auth-card h1 {
  font-family: var(--display);
  font-size: 25px;
  font-weight: 850;
  letter-spacing: -0.02em;
  margin: 2px 0 0;
}

.auth-hint {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--muted);
}

.auth-hint code {
  padding: 1px 5px;
  border-radius: 5px;
  background: var(--mint-soft);
  color: var(--mint-ink);
  font-size: 11.5px;
}

.auth-form {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 9px;
  margin-top: 4px;
}

.auth-input {
  height: 46px;
  font-size: 14px;
  letter-spacing: 0.04em;
}

.auth-submit {
  height: 46px;
  padding: 0 20px;
  border-radius: 15px;
}

.auth-error {
  margin: 0;
}

.auth-foot {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  padding-top: 4px;
}

@media (max-width: 430px) {
  .auth-form {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
