<script setup>
import { onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { readMoviePilotResponse } from './MoviePilotResponse'
import { renderNodes } from './RenderNodes'

const props = defineProps({
  api: { type: [Object, Function], required: true },
  pluginId: { type: String, required: true },
  show_switch: { type: Boolean, default: true },
})
const emit = defineEmits(['switch', 'close'])
const page = shallowRef([])
const loading = ref(true)
const loadFailed = ref(false)
const controller = new AbortController()

async function load() {
  loading.value = true
  loadFailed.value = false
  try {
    const id = encodeURIComponent(props.pluginId)
    const body = await props.api.get(`plugin/page/${id}`, { feedback: 'silent', signal: controller.signal })
    const response = readMoviePilotResponse(body)
    if (!Array.isArray(response?.page)) throw new Error('Invalid NodeSeek page layout')
    page.value = response.page
  } catch {
    if (!controller.signal.aborted) loadFailed.value = true
  } finally {
    loading.value = false
  }
}

const Layout = () => renderNodes(page.value)
onMounted(load)
onUnmounted(() => controller.abort())
</script>

<template>
  <div>
    <VCardTitle class="d-flex align-center justify-space-between">
      NodeSeek 论坛签到
      <VBtn icon="mdi-close" variant="text" aria-label="关闭" @click="emit('close')" />
    </VCardTitle>
    <VDivider />
    <VCardText v-if="loading"><VProgressLinear indeterminate aria-label="正在加载签到数据" /></VCardText>
    <VCardText v-else-if="loadFailed">
      <VAlert type="error" variant="tonal">签到数据加载失败，请重试。</VAlert>
      <VBtn class="mt-3" variant="tonal" @click="load">重试</VBtn>
    </VCardText>
    <VCardText v-else><Layout /></VCardText>
    <VCardActions v-if="show_switch" class="app-dialog-actions">
      <VBtn color="info" variant="tonal" prepend-icon="mdi-cog-outline" @click="emit('switch')">插件配置</VBtn>
    </VCardActions>
  </div>
</template>
