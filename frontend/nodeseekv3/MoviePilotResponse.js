export function readMoviePilotResponse(payload) {
  // Plugin clients preserve the host envelope and pass plugin-specific replies through.
  if (payload?.success === true && typeof payload.message === 'string' && Object.hasOwn(payload, 'data')) {
    return payload.data
  }
  return payload
}
