<script setup lang="ts">
/**
 * 设置浮窗（右上角半透明按钮打开）：两页标签 —— 环境变量 与 LLM 设置。
 *
 * 数据来自后端 `/api/web/v1/settings`（T43）：
 * - 环境变量按分组列出，显示当前值与来源（本机覆盖 / 环境或 .env / 未设置）；密文字段只回显“已设置”。
 * - LLM 设置包含 base_url / api_key / model / temperature / max_tokens / system_prompt，并可「测试连接」。
 *
 * 保存语义：留空的密文字段表示保持原值；环境变量值为空 + 勾选「清除」时删除本机覆盖。
 */
import { computed, ref, watch } from 'vue'

import { ApiError, newIdempotencyKey } from '@/api/client'
import {
  getSettings,
  testLlmConnection,
  updateSettings,
  type LlmProbeDTO,
  type SettingsDTO,
  type SettingsEnvEntryDTO,
  type SettingsUpdatePayload,
} from '@/api/settings'
import { describeApiError } from '@/components/task/task-format'
import AppIcon from './AppIcon.vue'
import ModalShell from './ModalShell.vue'
import { showToast } from './feedback'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

type Tab = 'env' | 'llm'

const tab = ref<Tab>('llm')
const settings = ref<SettingsDTO | null>(null)
const loading = ref(false)
const saving = ref(false)
const error = ref<ApiError | null>(null)
const notice = ref('')
const probing = ref(false)
const probe = ref<LlmProbeDTO | null>(null)
const probeError = ref<ApiError | null>(null)

/** 表单草稿：环境变量（按 key）+ LLM 字段。 */
const envDraft = ref<Record<string, string>>({})
const envClear = ref<Record<string, boolean>>({})
const envNewKey = ref('')
const envNewValue = ref('')
const llmDraft = ref({
  base_url: '',
  api_key: '',
  model: '',
  temperature: 0.7,
  max_tokens: 2048,
  system_prompt: '',
})

const groups = computed(() => {
  const order = settings.value?.env_groups ?? []
  const byGroup = new Map<string, SettingsEnvEntryDTO[]>()
  for (const entry of settings.value?.env ?? []) {
    const list = byGroup.get(entry.group) ?? []
    list.push(entry)
    byGroup.set(entry.group, list)
  }
  const groupsOut: { name: string; entries: SettingsEnvEntryDTO[] }[] = []
  for (const name of order) {
    const entries = byGroup.get(name)
    if (entries && entries.length) groupsOut.push({ name, entries })
  }
  for (const [name, entries] of byGroup) {
    if (!order.includes(name)) groupsOut.push({ name, entries })
  }
  return groupsOut
})

const dirtyEnvCount = computed(
  () =>
    Object.entries(envDraft.value).filter(([key, value]) => {
      const entry = settings.value?.env.find((item) => item.key === key)
      if (!entry) return true
      const original = entry.secret ? '' : (entry.value ?? '')
      return value !== original || Boolean(envClear.value[key])
    }).length +
    (envNewKey.value.trim() ? 1 : 0),
)

function fillDrafts(data: SettingsDTO): void {
  const draft: Record<string, string> = {}
  for (const entry of data.env) {
    // 密文字段不回显，草稿留空表示“保持原值”。
    draft[entry.key] = entry.secret ? '' : (entry.value ?? '')
  }
  envDraft.value = draft
  envClear.value = {}
  envNewKey.value = ''
  envNewValue.value = ''
  llmDraft.value = {
    base_url: data.llm.base_url,
    api_key: '',
    model: data.llm.model,
    temperature: data.llm.temperature ?? 0.7,
    max_tokens: data.llm.max_tokens ?? 2048,
    system_prompt: data.llm.system_prompt ?? '',
  }
}

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  notice.value = ''
  probe.value = null
  probeError.value = null
  try {
    const data = await getSettings()
    settings.value = data
    fillDrafts(data)
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught : null
  } finally {
    loading.value = false
  }
}

watch(
  () => props.open,
  (open) => {
    if (open) void load()
  },
  { immediate: true },
)

function buildPayload(): SettingsUpdatePayload {
  const env: Record<string, string | null> = {}
  for (const entry of settings.value?.env ?? []) {
    const value = envDraft.value[entry.key] ?? ''
    if (envClear.value[entry.key]) {
      env[entry.key] = null
      continue
    }
    if (entry.secret) {
      // 留空 = 保持原值，只有填了新值才提交。
      if (value.trim()) env[entry.key] = value.trim()
      continue
    }
    const original = entry.value ?? ''
    if (value !== original) env[entry.key] = value
  }
  const newKey = envNewKey.value.trim()
  if (newKey) env[newKey] = envNewValue.value

  const llm: SettingsUpdatePayload['llm'] = {
    base_url: llmDraft.value.base_url,
    model: llmDraft.value.model,
    temperature: Number(llmDraft.value.temperature),
    max_tokens: Number(llmDraft.value.max_tokens),
    system_prompt: llmDraft.value.system_prompt,
  }
  if (llmDraft.value.api_key.trim()) llm.api_key = llmDraft.value.api_key.trim()
  return { env, llm }
}

async function save(): Promise<void> {
  if (saving.value) return
  saving.value = true
  error.value = null
  notice.value = ''
  try {
    const payload = buildPayload()
    const updated = await updateSettings(payload, newIdempotencyKey())
    settings.value = updated
    fillDrafts(updated)
    const touchedEnv = Object.keys(payload.env ?? {}).length > 0
    notice.value = touchedEnv
      ? '已保存。环境变量改动在服务重启后生效。'
      : '已保存。LLM 配置已写入本机设置文件。'
    showToast('设置已保存', '🛠️')
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught : null
  } finally {
    saving.value = false
  }
}

async function runProbe(): Promise<void> {
  if (probing.value) return
  probing.value = true
  probe.value = null
  probeError.value = null
  try {
    probe.value = await testLlmConnection()
  } catch (caught) {
    probeError.value = caught instanceof ApiError ? caught : null
  } finally {
    probing.value = false
  }
}

function sourceLabel(entry: SettingsEnvEntryDTO): string {
  if (entry.source === 'override') return '本机覆盖'
  if (entry.source === 'environment') return '环境变量'
  return '未设置'
}

function groupLabel(entry: SettingsEnvEntryDTO): string {
  return entry.secret && entry.has_value ? '已设置（留空保持）' : entry.description
}
</script>

<template>
  <ModalShell :open="open" variant="project" labelled-by="settings-title" @close="emit('close')">
    <div class="settings-head">
      <div>
        <span class="modal-overline">SETTINGS · LOCAL CONSOLE</span>
        <h2 id="settings-title">设置</h2>
        <p class="settings-sub">环境变量与 LLM 连接配置只作用于本机服务。</p>
      </div>
      <button class="sheet-close on-plain" type="button" aria-label="关闭" @click="emit('close')">
        <AppIcon name="x" />
      </button>
    </div>

    <div class="settings-tabs" role="tablist" aria-label="设置分类">
      <button
        type="button"
        class="tab"
        role="tab"
        :aria-selected="tab === 'llm'"
        :data-testid="'settings-tab-llm'"
        @click="tab = 'llm'"
      >
        🤖 LLM 设置
      </button>
      <button
        type="button"
        class="tab"
        role="tab"
        :aria-selected="tab === 'env'"
        :data-testid="'settings-tab-env'"
        @click="tab = 'env'"
      >
        🧩 环境变量
        <span v-if="tab === 'env' && dirtyEnvCount" class="chip-count">{{ dirtyEnvCount }}</span>
      </button>
    </div>

    <div class="sheet-scroll">
      <div v-if="loading" class="board-state" data-testid="settings-loading">
        <span class="state-face" aria-hidden="true">🛠️</span>
        <strong>正在读取本机设置…</strong>
      </div>

      <div v-else-if="error && !settings" class="board-state is-error" data-testid="settings-error">
        <span class="state-face" aria-hidden="true">☁️</span>
        <strong>设置没读出来</strong>
        <span>{{ error.displayMessage }}</span>
        <button class="btn btn-soft" type="button" @click="load()">重新加载</button>
      </div>

      <div v-else-if="settings" class="settings-body">
        <div v-if="error" class="notice notice--error" data-testid="settings-save-error">
          <strong>{{ describeApiError(error).message }}</strong>
          <span>{{ describeApiError(error).hint }}</span>
          <span v-if="error.request_id" class="mono">request_id: {{ error.request_id }}</span>
        </div>
        <div v-if="notice" class="notice notice--success" data-testid="settings-notice">{{ notice }}</div>

        <!-- LLM 设置 -->
        <template v-if="tab === 'llm'">
          <section class="settings-card">
            <div class="settings-card-head">
              <strong>连接配置</strong>
              <span class="field-hint">保存后供后续调用 LLM 的功能使用</span>
            </div>
            <div class="form-row">
              <div class="field">
                <label for="llm-base-url">Base URL</label>
                <input
                  id="llm-base-url"
                  v-model="llmDraft.base_url"
                  class="input"
                  type="text"
                  placeholder="https://api.openai.com/v1"
                  data-testid="llm-base-url"
                />
              </div>
              <div class="field">
                <label for="llm-model">模型</label>
                <input
                  id="llm-model"
                  v-model="llmDraft.model"
                  class="input"
                  type="text"
                  placeholder="gpt-4o-mini"
                  data-testid="llm-model"
                />
              </div>
            </div>
            <div class="field">
              <label for="llm-api-key">
                API Key
                <span class="field-hint">
                  {{ settings.llm.api_key_set ? '已设置 · 留空表示不修改' : '还没有设置' }}
                </span>
              </label>
              <input
                id="llm-api-key"
                v-model="llmDraft.api_key"
                class="input"
                type="password"
                autocomplete="new-password"
                :placeholder="settings.llm.api_key_set ? '••••••••（留空保持原值）' : 'sk-…'"
                data-testid="llm-api-key"
              />
            </div>
            <div class="form-row">
              <div class="field">
                <label for="llm-temperature">Temperature <span class="field-hint">0 ~ 2</span></label>
                <input
                  id="llm-temperature"
                  v-model.number="llmDraft.temperature"
                  class="input"
                  type="number"
                  min="0"
                  max="2"
                  step="0.1"
                />
              </div>
              <div class="field">
                <label for="llm-max-tokens">Max tokens</label>
                <input
                  id="llm-max-tokens"
                  v-model.number="llmDraft.max_tokens"
                  class="input"
                  type="number"
                  min="1"
                  max="200000"
                  step="1"
                />
              </div>
            </div>
            <div class="field">
              <label for="llm-system-prompt">System prompt <span class="field-hint">可选</span></label>
              <textarea
                id="llm-system-prompt"
                v-model="llmDraft.system_prompt"
                class="textarea textarea-sm"
                maxlength="8000"
                placeholder="给模型的默认系统提示词。"
              />
            </div>
            <div class="settings-actions">
              <button class="btn btn-soft" type="button" :disabled="probing" data-testid="llm-test" @click="runProbe">
                <AppIcon name="link" /> {{ probing ? '测试中…' : '测试连接' }}
              </button>
              <span class="field-hint">
                用已保存的配置探测 {{ settings.llm.base_url || '（未设置 base_url）' }}/models
              </span>
            </div>
            <div v-if="probe" class="notice" :class="probe.ok ? 'notice-success' : 'notice-warning'" data-testid="llm-probe-result">
              <strong>{{ probe.message }}</strong>
              <span v-if="probe.latency_ms !== null">耗时 {{ probe.latency_ms }}ms · HTTP {{ probe.status_code ?? '—' }}</span>
              <span v-if="probe.models.length" class="mono">{{ probe.models.slice(0, 6).join(' / ') }}</span>
            </div>
            <div v-if="probeError" class="notice notice--error">
              <strong>{{ describeApiError(probeError).message }}</strong>
              <span>{{ describeApiError(probeError).hint }}</span>
            </div>
          </section>
        </template>

        <!-- 环境变量 -->
        <template v-else>
          <div class="notice notice--info">
            环境变量由后端在服务重启时读取；这里保存的值会写入
            <span class="mono">{{ settings.settings_file }}</span>，重启后生效（真实进程环境优先级最高）。
          </div>

          <section v-for="group in groups" :key="group.name" class="settings-card">
            <div class="settings-card-head">
              <strong>{{ group.name }}</strong>
              <span class="field-hint">{{ group.entries.length }} 项</span>
            </div>
            <div v-for="entry in group.entries" :key="entry.key" class="settings-env-row">
              <div class="settings-env-meta">
                <code class="mono">{{ entry.key }}</code>
                <small>{{ groupLabel(entry) }}</small>
              </div>
              <div class="settings-env-value">
                <input
                  class="input"
                  :type="entry.secret ? 'password' : 'text'"
                  :placeholder="entry.secret && entry.has_value ? '••••••••（留空保持）' : '未设置'"
                  :value="envDraft[entry.key] ?? ''"
                  :disabled="Boolean(envClear[entry.key])"
                  :autocomplete="'off'"
                  :data-testid="`env-input-${entry.key}`"
                  @input="envDraft[entry.key] = ($event.target as HTMLInputElement).value"
                />
                <span class="settings-source" :class="`is-${entry.source}`">{{ sourceLabel(entry) }}</span>
                <label class="settings-clear" :title="`清除本机覆盖（回到 .env / 进程环境）`">
                  <input
                    type="checkbox"
                    :checked="Boolean(envClear[entry.key])"
                    :data-testid="`env-clear-${entry.key}`"
                    @change="envClear[entry.key] = ($event.target as HTMLInputElement).checked"
                  />
                  清除
                </label>
              </div>
            </div>
          </section>

          <section class="settings-card">
            <div class="settings-card-head">
              <strong>新增自定义变量</strong>
              <span class="field-hint">大写字母、数字、下划线</span>
            </div>
            <div class="form-row">
              <div class="field">
                <label for="env-new-key">变量名</label>
                <input id="env-new-key" v-model="envNewKey" class="input mono" type="text" placeholder="MY_FLAG" data-testid="env-new-key" />
              </div>
              <div class="field">
                <label for="env-new-value">变量值</label>
                <input id="env-new-value" v-model="envNewValue" class="input" type="text" placeholder="on" data-testid="env-new-value" />
              </div>
            </div>
          </section>
        </template>
      </div>
    </div>

    <div class="settings-footer">
      <span class="field-hint">
        {{ settings?.updated_at ? `上次保存：${settings.updated_at}` : '还没有保存过设置' }}
      </span>
      <div class="footer-actions">
        <button class="btn btn-ghost" type="button" @click="emit('close')">关闭</button>
        <button class="btn btn-soft" type="button" :disabled="loading" @click="load()">
          <AppIcon name="refresh" /> 重新读取
        </button>
        <button class="btn btn-primary" type="button" :disabled="saving || loading" data-testid="settings-save" @click="save">
          {{ saving ? '保存中…' : '保存设置' }}
        </button>
      </div>
    </div>
  </ModalShell>
</template>
