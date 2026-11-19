import assert from 'node:assert/strict'
import test from 'node:test'
import { readMoviePilotResponse } from './MoviePilotResponse.js'

test('reads wrapped MoviePilot page and form responses', () => {
  for (const data of [{ page: [] }, { conf: [], model: {} }]) {
    assert.equal(readMoviePilotResponse({ success: true, message: '', data }), data)
  }
})

test('preserves direct plugin replies and host error messages', () => {
  const replies = [
    { page: [] },
    { success: true, status: 'authenticated', message: '已验证' },
    { success: false, status: 'cookie_missing', message: '请同步 CookieCloud' },
    { success: false, data: null, message: '请重新登录 MoviePilot' },
  ]
  for (const reply of replies) {
    assert.equal(readMoviePilotResponse(reply), reply)
  }
})
