<script>
// 只记住上次数据页的尺寸，重开时仍从宿主读取全部数据。
let previousOverviewSize
</script>

<script setup>
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import ticketCss from './Ticket.css?inline'

const props = defineProps({
  view: { type: String, required: true },
  loading: { type: Boolean, required: true },
})
const emit = defineEmits(['close'])
const body = ref(null)
const content = ref(null)
const card = shallowRef(null)
const visible = ref(false)
const reserve = ref(false)
const reservedHeight = ref('')

watch(() => props.loading, (loading, _, onCleanup) => {
  if (!loading) {
    visible.value = true
    return
  }
  function showPlaceholder() {
    if (props.view === 'data' && previousOverviewSize && previousOverviewSize.width === content.value?.clientWidth) {
      reservedHeight.value = previousOverviewSize.height + 'px'
    }
    reserve.value = true
    visible.value = true
  }
  if (visible.value) {
    showPlaceholder()
    return
  }
  const timer = setTimeout(showPlaceholder, 180)
  onCleanup(() => clearTimeout(timer))
}, { immediate: true })

onMounted(() => {
  const hostBody = body.value?.parentElement
  const hostCard = hostBody?.parentElement
  if (!hostBody?.matches('.v-card-text') || !hostCard?.matches('.v-card')) return
  hostCard.classList.add('ns-ticket-dialog')
  card.value = hostCard
})
onBeforeUnmount(() => {
  const overview = content.value?.querySelector('.ns-overview')
  if (overview && !props.loading && overview.offsetHeight && overview.clientWidth) {
    previousOverviewSize = { width: overview.clientWidth, height: overview.offsetHeight }
  }
  card.value?.classList.remove('ns-ticket-dialog')
})
</script>

<template>
  <!-- 样式随联邦模块加载，避免单独的样式请求丢失宿主 API 鉴权。 -->
  <Teleport to="head"><component :is="'style'" v-text="ticketCss" /></Teleport>
  <div ref="body" :class="{ 'ns-ticket-dialog': !card, 'ns-awaiting-open': !visible }">
    <Teleport :to="card || 'body'" :disabled="!card">
      <header class="ns-mast order-first">
        <VIcon icon="mdi-calendar-check-outline" aria-hidden="true" />
        <strong>NodeSeek</strong><span aria-hidden="true">/</span><span>{{ view === 'config' ? '插件配置' : '论坛签到' }}</span>
      </header>
      <VDialogCloseBtn @click="emit('close')" />
    </Teleport>
    <div ref="content" :class="{ 'ns-reserved-data': reserve && view === 'data', 'ns-reserved-config': reserve && view === 'config' }"
         :style="{ '--ns-reserved-height': reservedHeight || undefined }" :aria-busy="loading">
      <slot v-if="!loading" />
      <div v-else-if="visible" class="ns-placeholder" role="status">
        <span class="d-sr-only">{{ view === 'config' ? '正在加载配置' : '正在加载签到数据' }}</span>
        <div v-if="view === 'config'" class="ns-settings" aria-hidden="true">
          <div class="ns-nav"><span v-for="n in 3" :key="n" class="ns-skeleton-line" /></div>
          <div class="ns-edit"><span v-for="n in 4" :key="n" class="ns-skeleton-row" /></div>
        </div>
        <div v-else class="ns-overview" aria-hidden="true">
          <div class="ns-stats"><div class="ns-skeleton-ticket" /><div class="ns-timing"><span v-for="n in 2" :key="n" class="ns-skeleton-line" /></div></div>
          <div class="ns-activity"><span class="ns-skeleton-line" /><span v-for="n in 10" :key="n" class="ns-skeleton-row" /></div>
        </div>
      </div>
    </div>
    <Teleport :to="card || 'body'" :disabled="!card"><slot name="actions" /></Teleport>
  </div>
</template>
