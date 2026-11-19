<script setup>
import { onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { readMoviePilotResponse } from './MoviePilotResponse'
import PluginDialog from './PluginDialog.vue'
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
const appMode = ref(false)
const controller = new AbortController()

function updateAppMode() {
  const preference = localStorage.getItem('ui-mode')
  const { userAgent, userAgentData, maxTouchPoints } = navigator
  const mobile = userAgentData?.mobile === true ||
    /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini|CriOS/i.test(userAgent) ||
    (/Macintosh/i.test(userAgent) && maxTouchPoints > 1)
  appMode.value = document.documentElement.dataset.nativeChrome === 'active' || preference === 'app' ||
    (preference !== 'desktop' && mobile && window.innerWidth < 1280)
}

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
onMounted(() => {
  updateAppMode()
  window.addEventListener('resize', updateAppMode)
  load()
})
onUnmounted(() => {
  window.removeEventListener('resize', updateAppMode)
  controller.abort()
})
</script>

<template>
  <PluginDialog title="NodeSeek 论坛签到" body-class="min-h-40" @close="emit('close')">
    <div v-if="loading"><VProgressLinear indeterminate aria-label="正在加载签到数据" /></div>
    <div v-else-if="loadFailed">
      <VAlert type="error" title="数据加载失败">
        <div>签到数据加载失败，请重试。</div>
        <VBtn class="mt-3" color="error" variant="tonal" @click="load">重试</VBtn>
      </VAlert>
    </div>
    <div v-else><Layout /></div>
    <template #actions>
      <VFab
        v-if="show_switch"
        icon="mdi-cog"
        location="bottom"
        size="x-large"
        fixed
        app
        appear
        :class="{ 'mb-10': appMode }"
        aria-label="插件配置"
        @click="emit('switch')"
      />
    </template>
  </PluginDialog>
</template>
