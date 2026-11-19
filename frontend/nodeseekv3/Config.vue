<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, shallowRef } from 'vue'
import { readMoviePilotResponse } from './MoviePilotResponse'
import PluginDialog from './PluginDialog.vue'
import { renderNodes } from './RenderNodes'

const props = defineProps({
  api: { type: [Object, Function], required: true },
  pluginId: { type: String, required: true },
  initialConfig: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['save', 'switch', 'close'])
const model = reactive({ ...props.initialConfig })
const layout = shallowRef(null)
const root = ref(null)
const loading = ref(true)
const loadFailed = ref(false)
const checking = ref('')
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
  if (currentScope.value !== previousScope) {
    verification.value = null
    touchedLogin.value = true
  }
}

function timeText(key) {
  return [model[`${key}_hour`] ?? '', model[`${key}_minute`] ?? ''].join(':')
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
    proxyLabel: model.use_proxy ? '使用系统代理' : '直连',
    verifying: checking.value === 'verify',
    checkingLogin: Boolean(checking.value),
    busy: Boolean(checking.value),
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
  await nextTick()
  root.value?.querySelector('[data-login-feedback]')?.focus()
}

const Layout = () => renderNodes(layout.value ? [layout.value] : [], {
  model,
  bindings: bindings.value,
  actions: { verifyLogin: verify },
  update,
  submit: save,
})

onMounted(load)
onUnmounted(() => controller.abort())
</script>

<template>
  <PluginDialog title="NodeSeek 论坛签到 - 插件配置" divider @close="emit('close')">
    <div v-if="loading">
      <VProgressLinear indeterminate aria-label="正在加载配置" />
    </div>
    <div v-else-if="loadFailed">
      <VAlert type="error" title="配置加载失败">
        <div>插件配置加载失败，请稍后重试</div>
        <VBtn class="mt-3" color="error" variant="tonal" @click="load">重试</VBtn>
      </VAlert>
    </div>
    <fieldset v-else ref="root" :disabled="Boolean(checking)" class="pa-0 ma-0" style="border: 0; min-inline-size: 0">
      <Layout />
    </fieldset>
    <template #actions>
      <VCardActions class="app-dialog-actions">
        <VBtn color="info" variant="tonal" prepend-icon="mdi-database-eye-outline" @click="emit('switch')">查看数据</VBtn>
        <VSpacer />
        <VBtn v-if="layout" color="primary" variant="flat" prepend-icon="mdi-content-save" class="px-5"
              :disabled="Boolean(checking)" :loading="checking === 'save'" @click="save">保存</VBtn>
      </VCardActions>
    </template>
  </PluginDialog>
</template>
