<script setup>
import { onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import ticketCss from './Ticket.css?inline'

defineProps({ section: { type: String, required: true } })
const emit = defineEmits(['close'])
const body = ref(null)
const card = shallowRef(null)

onMounted(() => {
  const hostBody = body.value?.parentElement
  const hostCard = hostBody?.parentElement
  if (!hostBody?.matches('.v-card-text') || !hostCard?.matches('.v-card')) return
  hostCard.classList.add('ns-ticket-dialog')
  card.value = hostCard
})
onBeforeUnmount(() => card.value?.classList.remove('ns-ticket-dialog'))
</script>

<template>
  <!-- 样式随联邦模块加载，避免单独的样式请求丢失宿主 API 鉴权。 -->
  <Teleport to="head"><component :is="'style'" v-text="ticketCss" /></Teleport>
  <div ref="body" :class="{ 'ns-ticket-dialog': !card }">
    <Teleport :to="card || 'body'" :disabled="!card">
      <header class="ns-mast order-first">
        <VIcon icon="mdi-calendar-check-outline" aria-hidden="true" />
        <strong>NodeSeek</strong><span aria-hidden="true">/</span><span>{{ section }}</span>
      </header>
      <VDialogCloseBtn @click="emit('close')" />
    </Teleport>
    <slot />
    <Teleport :to="card || 'body'" :disabled="!card"><slot name="actions" /></Teleport>
  </div>
</template>
