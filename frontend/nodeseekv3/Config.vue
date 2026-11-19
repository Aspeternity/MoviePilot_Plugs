<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, shallowRef } from 'vue'
import { readMoviePilotResponse } from './MoviePilotResponse'
import PluginDialog from './PluginDialog.vue'

const props = defineProps({
  api: { type: [Object, Function], required: true },
  pluginId: { type: String, required: true },
  initialConfig: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['save', 'switch', 'close'])
const model = reactive({ ...props.initialConfig })
const layout = shallowRef(null)
const root = ref(null)
const section = ref('basic')
const sections = [
  { key: 'basic', label: '签到设置', icon: 'mdi-tune-variant' },
  { key: 'plan', label: '签到计划', icon: 'mdi-clock-outline' },
  { key: 'login', label: '登录与网络', icon: 'mdi-fingerprint' },
]
const switches = [
  { key: 'enabled', label: '启用定时签到' }, { key: 'onlyonce', label: '保存后立即签到一次' },
  { key: 'random', label: '随机奖励', hint: '关闭时使用固定奖励' }, { key: 'notify', label: '发送签到通知' },
]
const loading = ref(true)
const loadFailed = ref(false)
const checking = ref('')
const uaNotice = ref('')
const verification = shallowRef(null)
const initialNotice = ref('')
const savedLogin = ref(null)
const controller = new AbortController()

function loginOptions(config) {
  const cloud = config.cookiecloud === true
  return {
    cookiecloud: cloud,
    use_proxy: config.use_proxy === true,
    user_agent: String(config.user_agent || '').trim(),
    ...(cloud ? {} : { cookie: String(config.cookie || '').trim() }),
  }
}

function loginScope(config) {
  return JSON.stringify(loginOptions(config))
}

const currentScope = computed(() => loginScope(model))
const currentVerification = computed(() => verification.value?.scope === currentScope.value ? verification.value : null)
const touchedLogin = ref(false)

function update(field, value) {
  if (field === 'cron') {
    const previous = String(model.cron ?? '').trim().split(/\s+/)
    const next = String(value ?? '').trim().split(/\s+/)
    if (next.length === 5 && (previous.length !== 5 || previous[4].toLowerCase() !== next[4].toLowerCase())) {
      model.cron_weekday_basis = 'sunday_zero'
    }
  }
  const previousScope = currentScope.value
  model[field] = value
  if (field === 'user_agent') uaNotice.value = ''
  if (currentScope.value !== previousScope) {
    verification.value = null
    touchedLogin.value = true
  }
}

function fillBrowserUA() {
  const ua = navigator.userAgent?.trim()
  if (!ua || ua.length > 1024) {
    uaNotice.value = ua ? '当前浏览器 UA 超过 1024 个字符，请手动填写。' : '未读取到当前浏览器 UA，请手动填写。'
    return
  }
  update('user_agent', ua)
  uaNotice.value = '已填入当前浏览器 UA。'
}

function timeText(key) {
  const hour = model[key + '_hour']
  const minute = model[key + '_minute']
  return hour && minute ? hour + ':' + minute : ''
}

const bindings = computed(() => {
  const start = timeText('window_start')
  const end = timeText('window_end')
  const clock = /^(?:[01]\d|2[0-3]):[0-5]\d$/
  const complete = clock.test(start) && clock.test(end)
  const valid = complete && start < end
  const cloud = model.cookiecloud === true
  const result = currentVerification.value
  const verified = result ? result.success : savedLogin.value === currentScope.value
  return {
    cronMode: model.schedule_mode === 'cron',
    windowMode: model.schedule_mode === 'window',
    legacyCronDays: model.cron_weekday_basis !== 'sunday_zero',
    windowError: valid ? '' : complete ? '结束时间须晚于开始时间，且在同一天。' : '请选择完整的开始和结束时间。',
    windowSummary: valid ? `每天 ${start}–${end} 随机签到一次 · 北京时间` : '',
    useCloud: cloud,
    useManual: !cloud,
    needsLogin: !verified,
    loginMessage: checking.value ? '' : result?.message || (!touchedLogin.value && initialNotice.value) || '',
    loginAlertType: result?.success ? 'success' : result?.type || 'warning',
  }
})

async function load() {
  loading.value = true
  loadFailed.value = false
  try {
    const id = encodeURIComponent(props.pluginId)
    const body = await props.api.get(`plugin/form/${id}`, { feedback: 'silent', signal: controller.signal })
    const response = readMoviePilotResponse(body)
    const form = response?.conf?.[0]
    if (form?.component !== 'VForm') throw new Error('Invalid NodeSeek configuration layout')
    Object.assign(model, response.model, { enabled: form.enabled === true, onlyonce: false })
    if (form.enabled === true) savedLogin.value = currentScope.value
    initialNotice.value = form.login_notice || ''
    layout.value = form
  } catch {
    if (!controller.signal.aborted) loadFailed.value = true
  } finally {
    loading.value = false
  }
}

async function checkLogin(snapshot, force) {
  const scope = loginScope(snapshot)
  const options = loginOptions(snapshot)
  let result
  try {
    const id = encodeURIComponent(props.pluginId)
    const body = await props.api.post(`plugin/${id}/config_login_check`, { ...options, force }, {
      feedback: 'silent',
      timeout: 60000,
      signal: controller.signal,
    })
    result = readMoviePilotResponse(body)
    if (typeof result?.success !== 'boolean' || typeof result.message !== 'string') {
      throw new Error('Invalid NodeSeek verification response')
    }
  } catch {
    result = { success: false, message: '未收到登录验证结果。请检查 MoviePilot 连接后重试。' }
  }
  const warning = ['signed_out', 'unknown', 'cookie_missing', 'manual_missing', 'manual_invalid', 'proxy_missing']
  verification.value = { ...result, scope, type: warning.includes(result.status) ? 'warning' : 'error' }
  return result.success && !controller.signal.aborted
}

async function verify() {
  if (checking.value) return
  checking.value = 'verify'
  try {
    await checkLogin({ ...model }, true)
  } finally {
    checking.value = ''
  }
}

async function save() {
  if (!layout.value || checking.value || controller.signal.aborted) return
  const snapshot = { ...model }
  if (!snapshot.enabled && !snapshot.onlyonce) {
    emit('save', snapshot)
    return
  }

  checking.value = 'save'
  let ok
  try {
    ok = await checkLogin(snapshot, currentVerification.value?.success === false)
  } finally {
    checking.value = ''
  }
  if (controller.signal.aborted || loginScope(snapshot) !== currentScope.value) return
  if (ok) {
    emit('save', snapshot)
    return
  }
  section.value = 'login'
  await nextTick()
  root.value?.querySelector('[data-login-feedback]')?.focus()
}

function updateTime(key, value) {
  const [hour, minute] = value.split(':')
  model[key + '_hour'] = hour || null
  model[key + '_minute'] = minute || null
}

onMounted(load)
onUnmounted(() => controller.abort())
</script>

<template>
  <PluginDialog view="config" :loading="loading" @close="emit('close')">
    <div v-if="loadFailed">
      <VAlert type="error" title="配置加载失败">
        <div>插件配置加载失败，请稍后重试</div>
        <VBtn class="mt-3" color="error" variant="tonal" @click="load">重试</VBtn>
      </VAlert>
    </div>
    <form v-else ref="root" novalidate @submit.prevent="save">
      <div class="ns-settings">
        <nav class="ns-nav" aria-label="配置分组">
          <button v-for="item in sections" :key="item.key" type="button" :disabled="Boolean(checking)"
                  :aria-current="section === item.key ? 'page' : undefined" @click="section = item.key">
            <VIcon :icon="item.icon" />{{ item.label }}
          </button>
        </nav>
        <fieldset class="ns-edit" :disabled="Boolean(checking)">
          <h3>{{ sections.find(item => item.key === section).label }}</h3>
          <template v-if="section === 'basic'">
            <label v-for="item in switches" :key="item.key" class="ns-setting">
              <span>{{ item.label }}<small v-if="item.hint">{{ item.hint }}</small></span>
              <input v-model="model[item.key]" type="checkbox" class="ns-switch" />
            </label>
            <p v-if="bindings.needsLogin && (model.enabled || model.onlyonce)" class="ns-hint">保存时会验证当前登录方式。</p>
          </template>
          <template v-else-if="section === 'plan'">
            <div class="ns-choice" role="group" aria-label="计划模式">
              <button type="button" :aria-pressed="bindings.windowMode" @click="update('schedule_mode', 'window')">随机时段</button>
              <button type="button" :aria-pressed="bindings.cronMode" @click="update('schedule_mode', 'cron')">Cron 定时</button>
            </div>
            <template v-if="bindings.windowMode">
              <div class="ns-period">
                <label v-for="(label, key) in { window_start: '开始时间', window_end: '结束时间' }" :key="key" class="ns-field">
                  {{ label }}<input type="time" :value="timeText(key)"
                                   @input="updateTime(key, $event.target.value)" />
                </label>
              </div>
              <p class="ns-hint" :class="{ 'ns-error': bindings.windowError }">{{ bindings.windowError || bindings.windowSummary }}</p>
            </template>
            <template v-else>
              <VCronField :model-value="model.cron" label="Cron 表达式" class="ns-cron"
                          hint="北京时间 · 星期可填写 mon（周一）或 sun（周日）。" persistent-hint
                          @update:model-value="update('cron', $event)" />
              <p v-if="bindings.legacyCronDays && layout.legacy_weekdays" class="ns-hint">
                旧规则当前按{{ layout.legacy_weekdays }}执行。修改星期时，0 表示周日。
              </p>
            </template>
          </template>
          <template v-else>
            <div class="ns-choice" role="group" aria-label="登录方式">
              <button type="button" :aria-pressed="bindings.useCloud" @click="update('cookiecloud', true)"><VIcon icon="mdi-cloud-outline" />CookieCloud</button>
              <button type="button" :aria-pressed="bindings.useManual" @click="update('cookiecloud', false)"><VIcon icon="mdi-key-outline" />手动 Cookie</button>
            </div>
            <p v-if="bindings.useCloud" class="ns-hint">使用 MoviePilot 中配置的 CookieCloud。</p>
            <label v-else class="ns-field">NodeSeek Cookie
              <input type="password" autocomplete="off" :value="model.cookie" @input="update('cookie', $event.target.value)" />
            </label>
            <button type="button" class="ns-secondary" :disabled="Boolean(checking)" @click="verify">
              {{ checking === 'verify' ? '正在验证…' : '验证登录' }}
            </button>
            <div data-login-feedback tabindex="-1" role="status" aria-live="polite" class="ns-feedback">
              <p v-if="checking" class="ns-hint">正在验证登录…</p>
              <p v-else-if="bindings.loginMessage" class="ns-hint" :class="'ns-' + bindings.loginAlertType">{{ bindings.loginMessage }}</p>
            </div>
            <details class="ns-network">
              <summary>代理与 User-Agent</summary>
              <label class="ns-setting"><span>使用 MoviePilot 系统代理</span>
                <input type="checkbox" class="ns-switch" :checked="model.use_proxy" @change="update('use_proxy', $event.target.checked)" />
              </label>
              <div class="ns-ua-field">
                <label class="ns-field">浏览器 User-Agent
                  <input type="text" maxlength="1024" placeholder="使用宿主默认值" :value="model.user_agent"
                         @input="update('user_agent', $event.target.value)" />
                </label>
                <button type="button" class="ns-secondary" @click="fillBrowserUA">填入当前浏览器 UA</button>
              </div>
              <p role="status" aria-live="polite" class="ns-ua-notice ns-hint">{{ uaNotice }}</p>
            </details>
          </template>
        </fieldset>
      </div>
    </form>
    <template #actions>
      <footer class="ns-edit-foot">
        <button type="button" class="ns-secondary" :disabled="Boolean(checking)" @click="emit('switch')">查看数据</button>
        <button v-if="layout" type="button" class="ns-save" :disabled="Boolean(checking)" @click="save">
          {{ checking === 'save' ? '正在验证…' : '保存' }}
        </button>
      </footer>
    </template>
  </PluginDialog>
</template>
