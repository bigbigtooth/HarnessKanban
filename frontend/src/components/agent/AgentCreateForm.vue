<script setup lang="ts">
/**
 * Agent 创建表单（T29 / D16）。
 *
 * - 只收集创建所需字段（名称 / 角色 / 容量 / 描述 / 能力）；不发送 enabled、key_version 等只读字段；
 * - 容量留空表示由服务端按配置默认值创建，前端不编造数字；
 * - 失败时保留已填写内容并回显服务端原因（父组件传入 error）。
 *
 * 版式：共享 `.form-grid` / `.field` / `.input`，取消与提交收在弹窗页脚；
 * 表单本体保持 `<form>`，回车仍可提交。
 */
import { reactive, ref } from 'vue'
import type { ApiError } from '@/api/client'
import { AGENT_ROLES, AgentRole } from '@/api/types'
import type { CreateAgentInput } from '@/api/agents'
import BaseDialog from '@/components/BaseDialog.vue'
import ErrorState from '@/components/ErrorState.vue'

const props = withDefaults(
  defineProps<{
    open: boolean
    busy?: boolean
    error?: ApiError | null
  }>(),
  { busy: false, error: null },
)

const emit = defineEmits<{ close: []; submit: [input: CreateAgentInput] }>()

const form = reactive({
  name: '',
  role: AgentRole.WORKER as AgentRole,
  maxConcurrency: '' as string,
  description: '',
  capabilities: '' as string,
})

const localError = ref('')

function parseCapabilities(raw: string): string[] {
  return raw
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0)
}

function submit(): void {
  localError.value = ''
  const name = form.name.trim()
  if (!name) {
    localError.value = '名称必填。'
    return
  }
  if (form.maxConcurrency.trim()) {
    const parsed = Number(form.maxConcurrency)
    if (!Number.isInteger(parsed) || parsed < 1) {
      localError.value = '最大并发必须是正整数（留空则由服务端使用配置默认值）。'
      return
    }
  }
  const input: CreateAgentInput = { name, role: form.role }
  if (form.maxConcurrency.trim()) input.max_concurrency = Number(form.maxConcurrency)
  if (form.description.trim()) input.description = form.description.trim()
  const capabilities = parseCapabilities(form.capabilities)
  if (capabilities.length > 0) input.capabilities = capabilities
  emit('submit', input)
}

function close(): void {
  emit('close')
}
</script>

<template>
  <BaseDialog :open="open" title="创建 Agent" description="创建成功后服务端只交付一次明文 Key。" @close="close">
    <form class="form-grid agent-create" data-testid="agent-create-form" @submit.prevent="submit">
      <label class="field">
        <span class="field-label">名称（必填，全局唯一）</span>
        <input v-model="form.name" class="input" type="text" data-testid="agent-create-name" autocomplete="off" />
      </label>

      <div class="form-grid form-grid--2">
        <label class="field">
          <span class="field-label">角色</span>
          <select v-model="form.role" class="select" data-testid="agent-create-role">
            <option v-for="role in AGENT_ROLES" :key="role" :value="role">
              {{ role === AgentRole.SUPERVISOR ? '主管（可领取/审核）' : '员工' }}
            </option>
          </select>
        </label>

        <label class="field">
          <span class="field-label">最大并发（正整数；留空则用服务端默认值）</span>
          <input
            v-model="form.maxConcurrency"
            class="input"
            type="text"
            inputmode="numeric"
            data-testid="agent-create-max-concurrency"
            autocomplete="off"
          />
        </label>
      </div>

      <label class="field">
        <span class="field-label">描述（可选）</span>
        <textarea v-model="form.description" class="textarea" rows="2" data-testid="agent-create-description" />
      </label>

      <label class="field">
        <span class="field-label">能力（可选，一行一个或用逗号分隔）</span>
        <textarea v-model="form.capabilities" class="textarea" rows="2" data-testid="agent-create-capabilities" />
      </label>

      <p v-if="localError" class="notice notice--error" role="alert" data-testid="agent-create-local-error">
        {{ localError }}
      </p>
      <ErrorState v-if="error" :error="error" :show-retry="false" />
    </form>

    <template #footer>
      <button type="button" class="btn" data-testid="agent-create-cancel" @click="close">取消</button>
      <button type="button" class="btn btn--primary" data-testid="agent-create-submit" :disabled="busy" @click="submit">
        {{ busy ? '创建中…' : '创建并交付 Key' }}
      </button>
    </template>
  </BaseDialog>
</template>

<style scoped>
.agent-create {
  min-width: 0;
}
</style>
