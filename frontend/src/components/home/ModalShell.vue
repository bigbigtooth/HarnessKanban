<script setup lang="ts">
/**
 * 浮窗外壳：沿用 demo 的 `.modal-layer` / `.sheet` 结构（背景虚化、弹入动画、移动端底部抽屉）。
 *
 * 统一的交互约定：点击遮罩关闭（可关）、Esc 关闭、打开时锁住页面滚动、关闭后焦点回到打开它的元素。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    open: boolean
    /** 标题元素 id，用于 aria-labelledby。 */
    labelledBy?: string
    /** `task` = 窄表单；`project` 与默认都是宽浮窗。 */
    variant?: 'default' | 'task' | 'project'
    closeOnBackdrop?: boolean
  }>(),
  { labelledBy: undefined, variant: 'default', closeOnBackdrop: true },
)

const emit = defineEmits<{ close: [] }>()

const sheet = ref<HTMLElement | null>(null)
let previouslyFocused: HTMLElement | null = null

function close(): void {
  emit('close')
}

function onBackdropClick(): void {
  if (props.closeOnBackdrop) close()
}

function onKeydown(event: KeyboardEvent): void {
  if (!props.open) return
  if (event.key === 'Escape') {
    event.stopPropagation()
    close()
    return
  }
  if (event.key !== 'Tab' || !sheet.value) return
  // 焦点陷阱：Tab 在浮窗内部循环。
  const focusable = sheet.value.querySelectorAll<HTMLElement>(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )
  if (focusable.length === 0) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  const active = document.activeElement as HTMLElement | null
  if (event.shiftKey && (active === first || !sheet.value.contains(active))) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  }
}

watch(
  () => props.open,
  async (open) => {
    if (open) {
      previouslyFocused = document.activeElement as HTMLElement | null
      document.body.style.overflow = 'hidden'
      document.addEventListener('keydown', onKeydown, true)
      await nextTick()
      const target = sheet.value?.querySelector<HTMLElement>(
        'input, select, textarea, button:not(.sheet-close)',
      )
      target?.focus()
    } else {
      document.body.style.overflow = ''
      document.removeEventListener('keydown', onKeydown, true)
      previouslyFocused?.focus?.()
      previouslyFocused = null
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  document.body.style.overflow = ''
  document.removeEventListener('keydown', onKeydown, true)
})

/** 窄浮窗用于任务表单，其余（详情/设置）用宽浮窗。 */
const sheetClass = computed(() => (props.variant === 'task' ? 'sheet task-sheet' : 'sheet project-sheet'))
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="modal-layer">
      <div class="modal-backdrop" data-close-modal @click="onBackdropClick" />
      <div
        ref="sheet"
        :class="sheetClass"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="labelledBy"
      >
        <slot />
      </div>
    </div>
  </Teleport>
</template>
