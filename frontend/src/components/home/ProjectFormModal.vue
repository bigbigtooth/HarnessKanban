<script setup lang="ts">
/**
 * 新建 / 编辑项目浮窗（demo 的 `.project-sheet` 表单外观）。
 *
 * 新建：`POST /projects`（服务端固定落成 DRAFT 草稿，需要「启用」或后续生命周期动作推进）。
 * 编辑：标题/简介用 `PATCH /projects/{id}`，项目背景单独用一次 `PATCH`（服务端要求 background 单独提交）。
 * 封面配色与 emoji 是本机外观偏好，保存在 localStorage（见 stores/projectSkins.ts）。
 */
import { computed, ref, watch } from 'vue'

import { ApiError, newIdempotencyKey } from '@/api/client'
import {
  createProject,
  updateProjectBackground,
  updateProjectMetadata,
  type ProjectDetailDTO,
  type ProjectSummaryDTO,
} from '@/api/projects'
import { describeApiError } from '@/components/task/task-format'
import AppIcon from './AppIcon.vue'
import ModalShell from './ModalShell.vue'
import { currentCoverChoices, emojiChoices } from '@/stores/home'
import { projectSkin, setProjectSkin } from '@/stores/projectSkins'
import { showToast } from './feedback'

const props = defineProps<{
  open: boolean
  mode: 'create' | 'edit'
  project?: ProjectDetailDTO | ProjectSummaryDTO | null
}>()

const emit = defineEmits<{ close: []; saved: [projectId: string] }>()

const title = ref('')
const summary = ref('')
const background = ref('')
const coverIndex = ref(0)
const emoji = ref<string>(emojiChoices[0])
const busy = ref(false)
const error = ref<ApiError | null>(null)
let key = newIdempotencyKey()

const isEdit = computed(() => props.mode === 'edit')

function reset(): void {
  key = newIdempotencyKey()
  busy.value = false
  error.value = null
  if (props.project) {
    title.value = props.project.title
    summary.value = props.project.summary ?? ''
    background.value = ('background' in props.project ? props.project.background : '') ?? ''
    const skin = projectSkin(props.project.id)
    coverIndex.value = skin.cover.cover
    emoji.value = skin.cover.emoji
    return
  }
  title.value = ''
  summary.value = ''
  background.value = ''
  coverIndex.value = Math.floor(Math.random() * currentCoverChoices().length)
  emoji.value = emojiChoices[Math.floor(Math.random() * emojiChoices.length)]
}

watch(
  () => [props.open, props.project] as const,
  () => {
    if (props.open) reset()
  },
  { immediate: true },
)

const coverChoices = computed(() => currentCoverChoices())
const previewPalette = computed(() => coverChoices.value[coverIndex.value] ?? coverChoices.value[0])

function pickCover(index: number): void {
  coverIndex.value = index
  if (props.project) setProjectSkin(props.project.id, { cover: index })
}

function pickEmoji(value: string): void {
  emoji.value = value
  if (props.project) setProjectSkin(props.project.id, { emoji: value })
}

async function submit(): Promise<void> {
  if (busy.value) return
  const name = title.value.trim()
  if (!name) {
    error.value = new ApiError({ code: 'INVALID_ARGUMENT', message: '请先给项目起个名字', httpStatus: 400 })
    return
  }
  busy.value = true
  error.value = null
  try {
    if (isEdit.value && props.project) {
      const project = props.project
      const previousBackground = 'background' in project ? (project.background ?? '') : ''
      await updateProjectMetadata(project.id, project.version, { title: name, summary: summary.value }, key)
      if (background.value !== previousBackground) {
        // 背景是独立请求：即使元数据保存成功，背景失败也要如实报告。
        await updateProjectBackground(project.id, background.value, project.version + 1, newIdempotencyKey())
      }
      showToast(`项目「${name}」已保存`, '🪐')
      emit('saved', project.id)
    } else {
      const created = await createProject({ title: name, summary: summary.value, background: background.value }, key)
      setProjectSkin(created.id, { cover: coverIndex.value, emoji: emoji.value })
      showToast(`新星球「${created.title}」已创建（草稿）`, '🌱')
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
  <ModalShell :open="open" variant="project" labelled-by="project-form-title" @close="emit('close')">
    <form class="project-form" novalidate @submit.prevent="submit">
      <div class="form-head">
        <div>
          <span class="modal-overline">{{ isEdit ? 'EDIT LITTLE PLANET' : 'NEW LITTLE PLANET' }}</span>
          <h2 id="project-form-title">{{ isEdit ? '编辑项目' : '种一颗新星球' }}</h2>
        </div>
        <button class="sheet-close on-plain" type="button" aria-label="关闭" @click="emit('close')">
          <AppIcon name="x" />
        </button>
      </div>

      <div class="sheet-scroll">
        <div class="form-body">
          <div class="form-main">
            <div v-if="error" class="notice notice--error" data-testid="project-form-error">
              <strong>{{ describeApiError(error).message }}</strong>
              <span>{{ describeApiError(error).hint }}</span>
            </div>

            <div class="field">
              <label for="project-name">项目名称</label>
              <input
                id="project-name"
                v-model="title"
                class="input"
                type="text"
                maxlength="60"
                placeholder="比如：草莓奶油配色计划"
                autocomplete="off"
                data-testid="project-form-title"
              />
            </div>

            <div class="field">
              <label for="project-tagline">一句话简介 <span class="field-hint">可选</span></label>
              <input
                id="project-tagline"
                v-model="summary"
                class="input"
                type="text"
                maxlength="120"
                placeholder="用一句话说清这个项目要做什么"
                data-testid="project-form-summary"
              />
            </div>

            <div class="field">
              <label for="project-background">项目背景 <span class="field-hint">会冻结进任务的领取快照</span></label>
              <textarea
                id="project-background"
                v-model="background"
                class="textarea"
                maxlength="4000"
                placeholder="把这件事的来龙去脉、约定与参考资料放进来。"
                data-testid="project-form-background"
              />
              <span class="field-hint">{{ background.length }} / 4000</span>
            </div>

            <div class="form-note">
              <span aria-hidden="true">🪐</span>
              <span>
                {{ isEdit ? '保存后版本号会 +1；背景单独计一次内容版本。' : '新项目会先落成「草稿」，在项目详情里点「启用」后 Agent 才能领取任务。' }}
              </span>
            </div>
          </div>

          <div class="form-side">
            <div class="cover-preview" :style="{ '--c1': previewPalette.c1, '--c2': previewPalette.c2 }">
              <span class="cover-emoji" aria-hidden="true">{{ emoji }}</span>
              <strong>{{ title || '新星球' }}</strong>
            </div>

            <fieldset class="form-fieldset">
              <legend>封面配色 <span class="field-hint">本机可见</span></legend>
              <div class="cover-picker">
                <button
                  v-for="(cover, index) in coverChoices"
                  :key="index"
                  type="button"
                  class="cover-swatch"
                  :class="{ 'is-active': coverIndex === index }"
                  :style="{ '--c1': cover.c1, '--c2': cover.c2 }"
                  :aria-pressed="coverIndex === index"
                  :aria-label="`配色 ${index + 1}`"
                  @click="pickCover(index)"
                >
                  <span v-if="coverIndex === index" aria-hidden="true">✓</span>
                </button>
              </div>
            </fieldset>

            <fieldset class="form-fieldset">
              <legend>封面 emoji</legend>
              <div class="emoji-picker">
                <button
                  v-for="option in emojiChoices"
                  :key="option"
                  type="button"
                  class="emoji-option"
                  :class="{ 'is-active': emoji === option }"
                  :aria-pressed="emoji === option"
                  @click="pickEmoji(option)"
                >
                  {{ option }}
                </button>
              </div>
            </fieldset>
          </div>
        </div>
      </div>

      <div class="form-footer">
        <button class="btn btn-ghost" type="button" @click="emit('close')">取消</button>
        <button class="btn btn-primary" type="submit" :disabled="busy" data-testid="project-form-submit">
          {{ busy ? '保存中…' : isEdit ? '保存修改' : '创建项目' }}
        </button>
      </div>
    </form>
  </ModalShell>
</template>
