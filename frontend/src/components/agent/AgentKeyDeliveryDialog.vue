<script setup lang="ts">
/**
 * Agent Key 一次性交付框（T29 / D16 / CONTRACT §8）。
 *
 * 安全约束（硬性）：
 * - 明文 Key 只在**本组件的内存状态**里存在，用于本次展示；绝不写入
 *   localStorage / sessionStorage / URL / 监控事件，也绝不通过列表或详情接口重新获取；
 * - 关闭即清除：`open` 变 false 时立即把内部 Key 置空并通知父组件清除；
 * - 服务端重放（`key_delivery_unavailable=true`）时**不显示任何 Key**，
 *   只显示“已无法再次交付，请重新重置”。
 *
 * 版式：「只展示一次」以警示徽标 + 警示条突出；Key 用等宽大字展示，复制按钮为主按钮。
 */
import { ref, watch } from 'vue'
import BaseDialog from '@/components/BaseDialog.vue'

const props = withDefaults(
  defineProps<{
    open: boolean
    agentName: string
    /** 一次性明文 Key；为 null 时表示本次没有可交付的 Key。 */
    agentKey?: string | null
    /** 服务端重放：原 Key 无法再次交付。 */
    keyDeliveryUnavailable?: boolean
    mode?: 'create' | 'rotate' | 'enable'
  }>(),
  { agentKey: null, keyDeliveryUnavailable: false, mode: 'create' },
)

const emit = defineEmits<{ close: [] }>()

// 内部副本：只用于渲染。关闭时立刻清空，父组件的副本也由 close 事件清除。
const internalKey = ref<string | null>(null)
const internalUnavailable = ref(false)
const copied = ref(false)

const MODE_TITLE: Record<'create' | 'rotate' | 'enable', string> = {
  create: 'Agent 创建成功：一次性交付 Key',
  rotate: 'Key 重置成功：一次性交付 Key',
  enable: 'Agent 启用：一次性交付 Key',
}

function clearInternal(): void {
  internalKey.value = null
  internalUnavailable.value = false
  copied.value = false
}

watch(
  () => props.open,
  (open) => {
    if (open) {
      internalKey.value = props.agentKey ?? null
      internalUnavailable.value = props.keyDeliveryUnavailable === true
      copied.value = false
    } else {
      clearInternal()
    }
  },
  { immediate: true },
)

// 交付内容在弹窗打开期间被父组件清空时，同步清空内部副本。
watch(
  () => [props.agentKey, props.keyDeliveryUnavailable] as const,
  ([key, unavailable]) => {
    if (!props.open) return
    internalKey.value = key ?? null
    internalUnavailable.value = unavailable === true
  },
)

async function copyKey(): Promise<void> {
  const key = internalKey.value
  if (!key) return
  const clipboard = globalThis.navigator?.clipboard
  if (clipboard && typeof clipboard.writeText === 'function') {
    try {
      await clipboard.writeText(key)
      copied.value = true
      return
    } catch {
      copied.value = false
    }
  }
  // 剪贴板不可用时仍保持 Key 可见、可手动选择复制；不写任何持久存储。
  copied.value = false
}

function onClose(): void {
  clearInternal()
  emit('close')
}
</script>

<template>
  <BaseDialog
    :open="open"
    :title="MODE_TITLE[mode]"
    description="明文 Key 仅在此处显示一次。关闭后无法再次查看，请立即交给 Agent 本人保存。"
    @close="onClose"
  >
    <div class="stack key-delivery" data-testid="agent-key-dialog">
      <p class="key-agent" data-testid="agent-key-agent">Agent：{{ agentName }}</p>

      <template v-if="internalKey">
        <div class="key-heading">
          <label class="field-label" for="agent-key-value">一次性明文 Key</label>
          <span class="badge badge--warning">只展示一次</span>
        </div>
        <input
          id="agent-key-value"
          class="input mono key-value"
          data-testid="agent-key-value"
          type="text"
          readonly
          :value="internalKey"
          @focus="($event.target as HTMLInputElement).select()"
        />
        <div class="row row--wrap">
          <button type="button" class="btn btn--primary" data-testid="agent-key-copy" @click="copyKey">
            复制 Key
          </button>
          <span v-if="copied" class="chip chip--success text-xs" data-testid="agent-key-copied">已复制到剪贴板</span>
        </div>
        <p class="notice notice--warning">
          本 Key 不会再次显示，也不会写入浏览器存储；关闭本框后如已丢失，只能重新重置 Key。
        </p>
      </template>

      <template v-else-if="internalUnavailable">
        <p class="notice notice--error" role="alert" data-testid="agent-key-unavailable">
          已无法再次交付，请重新重置
        </p>
        <p class="notice notice--warning">
          本次请求是幂等重放（key_delivery_unavailable=true）：服务端只保存 Key 摘要，原文不可找回。
          请关闭本框后在详情中执行“重置 Key”，用新交付的 Key 替换。
        </p>
      </template>

      <template v-else>
        <p class="muted" data-testid="agent-key-empty">本次操作未产生新的 Key 交付。</p>
      </template>
    </div>
    <template #footer>
      <button type="button" class="btn btn--primary" data-testid="agent-key-close" @click="onClose">我已保存，关闭</button>
    </template>
  </BaseDialog>
</template>

<style scoped>
.key-agent {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.key-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.key-value {
  background: var(--color-surface-sunken);
  font-size: var(--text-md);
  letter-spacing: 0.02em;
}
</style>
