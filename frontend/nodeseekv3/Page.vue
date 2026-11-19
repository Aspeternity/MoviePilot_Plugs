<script setup>
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
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

const ticket = computed(() => page.value[0]?.ticket)
const history = () => renderNodes(page.value.slice(1))
const legacyPage = () => renderNodes(page.value)
const statusIcons = {
  success: 'mdi-check-circle-outline', info: 'mdi-clock-outline',
  warning: 'mdi-alert-circle-outline', error: 'mdi-close-circle-outline',
}
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
  <PluginDialog view="data" :loading="loading" @close="emit('close')">
    <div v-if="loadFailed">
      <VAlert type="error" title="数据加载失败">
        <div>签到数据加载失败，请重试。</div>
        <VBtn class="mt-3" color="error" variant="tonal" @click="load">重试</VBtn>
      </VAlert>
    </div>
    <div v-else-if="ticket" class="ns-overview" :class="{ 'ns-has-gear': show_switch, 'ns-app-mode': appMode }">
      <section class="ns-stats" aria-label="签到概览">
        <div class="ns-pass">
          <div class="ns-face">
            <div class="ns-label"><VIcon :icon="statusIcons[ticket.status_color]" />今日签到</div>
            <strong class="ns-lead">{{ ticket.status }}</strong>
            <span class="ns-date">{{ ticket.date }}</span>
          </div>
          <div class="ns-stub">
            <div class="ns-label"><VIcon icon="mdi-food-drumstick-outline" />签到奖励</div>
            <div v-if="ticket.gain != null" class="ns-reward"><strong>{{ ticket.gain }}</strong><span>个鸡腿</span></div>
            <strong v-else class="ns-unknown-reward">{{ ticket.reward }}</strong>
          </div>
        </div>
        <div class="ns-timing">
          <div v-for="item in [
            { title: '最近签到', value: ticket.latest, detail: ticket.latest_status, icon: 'mdi-history' },
            { title: '下次执行', value: ticket.next, detail: ticket.next_detail, icon: 'mdi-calendar-clock-outline' },
          ]" :key="item.title" :title="item.detail || undefined">
            <span class="ns-time-icon"><VIcon :icon="item.icon" /></span>
            <div><div class="ns-label">{{ item.title }}</div><strong>{{ item.value }}</strong>
              <small v-if="item.detail && !['签到成功', '站点已签到'].includes(item.detail)">{{ item.detail }}</small>
            </div>
          </div>
        </div>
      </section>
      <section class="ns-activity" aria-label="签到历史">
        <div class="ns-section-title"><h3>签到历史</h3><span>近 {{ ticket.retention_days }} 天 · 北京时间</span></div>
        <component :is="history" />
      </section>
    </div>
    <component :is="legacyPage" v-else />
    <template #actions>
      <VFab
        v-if="show_switch"
        icon
        location="bottom"
        size="x-large"
        fixed
        app
        appear
        class="ns-gear" :class="{ 'mb-10': appMode }"
        @click="emit('switch')"
      >
        <VIcon icon="mdi-cog" /><span class="d-sr-only">插件配置</span>
      </VFab>
    </template>
  </PluginDialog>
</template>
