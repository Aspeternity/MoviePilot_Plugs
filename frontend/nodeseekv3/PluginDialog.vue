<script setup>
import { onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'

const props = defineProps({
  title: { type: String, required: true },
  divider: Boolean,
  bodyClass: { type: String, default: '' },
})
const emit = defineEmits(['close'])
const body = ref(null)
const card = shallowRef(null)
let restoreBody

onMounted(() => {
  const hostBody = body.value?.parentElement
  const hostCard = hostBody?.parentElement
  if (!hostBody?.matches('.v-card-text') || !hostCard?.matches('.v-card')) return

  // 联邦外壳用 pa-0 让组件自行排版。用回宿主正文的原生字体和内边距。
  const compact = hostBody.classList.contains('pa-0')
  const addBodyClass = props.bodyClass && !hostBody.classList.contains(props.bodyClass)
  hostBody.classList.remove('pa-0')
  if (addBodyClass) hostBody.classList.add(props.bodyClass)
  card.value = hostCard
  restoreBody = () => {
    if (compact) hostBody.classList.add('pa-0')
    if (addBodyClass) hostBody.classList.remove(props.bodyClass)
  }
})
onBeforeUnmount(() => restoreBody?.())
</script>

<template>
  <div ref="body" :class="card ? undefined : ['v-card-text', bodyClass]">
    <!-- 让标题与操作按钮成为宿主弹窗的直接子元素，正文独立滚动。 -->
    <Teleport :to="card || 'body'" :disabled="!card">
      <VCardItem :title="title" class="order-first" />
      <VDialogCloseBtn @click="emit('close')" />
      <VDivider v-if="divider" class="order-first" />
    </Teleport>
    <slot />
    <Teleport :to="card || 'body'" :disabled="!card">
      <slot name="actions" />
    </Teleport>
  </div>
</template>
