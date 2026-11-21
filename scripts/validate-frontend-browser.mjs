#!/usr/bin/env node

const cdpBase = process.env.CDP_URL ?? 'http://localhost:9222'
const appUrl = process.env.APP_URL ?? 'http://localhost:3000/'
const timeoutMs = Number(process.env.TIMEOUT_MS ?? 60000)

class Page {
  constructor(ws) {
    this.ws = ws
    this.nextId = 0
    this.pending = new Map()
    this.events = []
    ws.addEventListener('message', event => {
      const message = JSON.parse(event.data)
      if (message.method) this.events.push(message)
      if (!message.id || !this.pending.has(message.id)) return
      const { resolve, reject } = this.pending.get(message.id)
      this.pending.delete(message.id)
      if (message.error) reject(new Error(message.error.message))
      else resolve(message)
    })
  }

  static async create(preloadScript, blockedUrls = []) {
    const target = await fetch(`${cdpBase}/json/new?${encodeURIComponent(appUrl)}`, { method: 'PUT' }).then(checkJson)
    const ws = new WebSocket(target.webSocketDebuggerUrl)
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, { once: true })
      ws.addEventListener('error', reject, { once: true })
    })
    const page = new Page(ws)
    await page.send('Page.enable')
    await page.send('Runtime.enable')
    await page.send('Network.enable')
    await page.send('Page.addScriptToEvaluateOnNewDocument', { source: preloadScript })
    if (blockedUrls.length) {
      await page.send('Network.enable')
      await page.send('Network.setBlockedURLs', { urls: blockedUrls })
    }
    await page.send('Page.navigate', { url: appUrl })
    return page
  }

  send(method, params = {}) {
    const id = ++this.nextId
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.ws.send(JSON.stringify({ id, method, params }))
    })
  }

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
    if (result.result.exceptionDetails) throw new Error(result.result.exceptionDetails.text)
    return result.result.result.value
  }

  async close() {
    try { await this.send('Page.close') } finally { this.ws.close() }
  }
}

async function checkJson(response) {
  if (!response.ok) throw new Error(`CDP request failed (${response.status})`)
  return response.json()
}

function failBatchScript(fullWorld, versioned = false) {
  return `(() => {
    const realFetch = window.fetch.bind(window)
    window.__test = { fail: true, failIndex: -1, attempts: 0, preloads: 0, version: 'test-version-a' }
    window.fetch = async (input, init = {}) => {
      const url = typeof input === 'string' ? input : input.url
      if (${fullWorld} && url.includes('/objects/preload') && init.body) {
        const body = JSON.parse(init.body); body.bbox = [-180, -85, 180, 85]; body.zoom = 15
        init = { ...init, body: JSON.stringify(body) }
      }
      const response = await realFetch(input, init)
      if (!response.ok) return response
      if (url.includes('/objects/batch')) {
        const body = JSON.parse(init.body || '{}')
        if (window.__test.fail && (${versioned ? 'false' : `!${fullWorld} || body.batchIndex === window.__test.failIndex || window.__test.failIndex === -1`})) {
          window.__test.attempts++
          return new Response(JSON.stringify({ error: { code: 'INJECTED_TEST', message: 'Injected batch failure' } }), { status: 503, headers: { 'Content-Type': 'application/json' } })
        }
      }
      if (${versioned} && (url.includes('/objects/preload') || url.includes('/objects/batch'))) {
        const body = await response.clone().json()
        body.datasetVersion = window.__test.version
        if (url.includes('/objects/preload')) window.__test.preloads++
        return new Response(JSON.stringify(body), { status: response.status, statusText: response.statusText, headers: response.headers })
      }
      return response
    }
  })()`
}

function catalogAndBasemapFailureScript() {
  return `(() => {
    const realFetch = window.fetch.bind(window)
    window.fetch = async (input, init = {}) => {
      const url = typeof input === 'string' ? input : input.url
      const response = await realFetch(input, init)
      if (!response.ok || !url.includes('/layers?')) return response
      const catalog = await response.clone().json()
      catalog.objectTypes = catalog.objectTypes.filter(item => item.object_type !== 'coupler')
      return new Response(JSON.stringify(catalog), { status: response.status, statusText: response.statusText, headers: response.headers })
    }
  })()`
}

async function waitFor(page, predicate, description) {
  const end = Date.now() + timeoutMs
  while (Date.now() < end) {
    const value = await page.evaluate(`(${predicate})()`)
    if (value) return value
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  const state = await page.evaluate(`({ status: document.querySelector('.load-status')?.innerText, test: window.__test })`)
  throw new Error(`Timed out waiting for ${description}: ${JSON.stringify(state)}`)
}

const currentLoad = `() => {
  const status = document.querySelector('.load-status')?.innerText || ''
  const match = status.match(/Пачки\\s+(\\d+)\\/(\\d+)\\s+·\\s+(\\d+)\\/(\\d+)\\s+объектов/)
  return { ready: status.includes('Область загружена'), failed: !!document.querySelector('.retry-button'), status, progress: match && match.slice(1).map(Number) }
}`

async function runErrorRetry() {
  const page = await Page.create(failBatchScript(false))
  try {
    const failed = await waitFor(page, `() => document.querySelector('.retry-button') && window.__test.attempts >= 1 && (${currentLoad})().failed`, 'persistent batch error UI')
    const initial = await page.evaluate(`(${currentLoad})()`)
    await page.evaluate(`(() => { window.__test.fail = false; document.querySelector('.retry-button').click() })()`)
    const recovered = await waitFor(page, `() => { const s = (${currentLoad})(); return s.ready && !s.failed && s.progress ? s : null }`, 'manual retry recovery')
    if (!recovered.progress || recovered.progress[0] !== recovered.progress[1] || recovered.progress[2] !== recovered.progress[3]) throw new Error(`Retry ended without a complete batch set: ${JSON.stringify(recovered)}`)
    return { scenario: 'error-retry', attempts: failed ? await page.evaluate('window.__test.attempts') : 0, error: initial.status, recovered: recovered.status }
  } finally { await page.close() }
}

async function runPartialRecovery() {
  const page = await Page.create(failBatchScript(true))
  try {
    await page.evaluate('window.__test.failIndex = 2')
    const failed = await waitFor(page, `() => { const s = (${currentLoad})(); return s.failed && s.progress ? s : null }`, 'partial full-world batch failure')
    const [loadedBatches, totalBatches, loadedObjects, totalObjects] = failed.progress
    if (loadedBatches <= 0 || loadedBatches >= totalBatches || loadedObjects <= 0 || loadedObjects >= totalObjects) throw new Error(`Expected a non-empty partial result: ${failed.status}`)
    const attempts = await page.evaluate('window.__test.attempts')
    await page.evaluate(`(() => { window.__test.fail = false; document.querySelector('.retry-button').click() })()`)
    const recovered = await waitFor(page, `() => { const s = (${currentLoad})(); return s.ready && !s.failed && s.progress && s.progress[0] === s.progress[1] ? s : null }`, 'full-world recovery')
    if (recovered.progress[2] !== recovered.progress[3]) throw new Error(`Recovered with an incomplete feature count: ${recovered.status}`)
    return { scenario: 'partial-recovery', failed: failed.status, failedAttempts: attempts, recovered: recovered.status }
  } finally { await page.close() }
}

async function runVersionChange() {
  const page = await Page.create(failBatchScript(true, true))
  try {
    const first = await waitFor(page, `() => { const s = (${currentLoad})(); return s.ready && s.progress && s.progress[0] === s.progress[1] && window.__test.preloads === 1 ? s : null }`, 'first full-world dataset version')
    await page.evaluate(`(() => { window.__test.version = 'test-version-b'; document.querySelector('button[aria-label="Приблизить"]').click() })()`)
    const second = await waitFor(page, `() => { const s = (${currentLoad})(); return s.ready && s.progress && s.progress[0] === s.progress[1] && window.__test.preloads === 2 ? s : null }`, 'single second preload for public zoom command')
    if (first.progress[1] !== second.progress[1] || second.progress[2] !== second.progress[3]) throw new Error(`Second version is incomplete: ${second.status}`)
    return { scenario: 'version-change-contract', first: first.status, second: second.status, preloads: await page.evaluate('window.__test.preloads'), note: 'datasetVersion is injected consistently in preload and batch responses; this does not publish a new backend snapshot' }
  } finally { await page.close() }
}

async function runCatalogAndBasemapFailures() {
  const page = await Page.create(catalogAndBasemapFailureScript(), ['*arcgisonline.com*'])
  try {
    const result = await waitFor(page, `() => {
      const s = (${currentLoad})()
      const unavailable = [...document.querySelectorAll('.check-row')].find(row => row.innerText.includes('Муфты'))?.innerText.includes('Недоступно')
      const basemapError = document.querySelector('.basemap-warning')?.innerText.includes('подложка недоступна')
      return s.ready && unavailable && basemapError ? { status: s.status, unavailable, basemapError } : null
    }`, 'independent unsupported type and basemap error states')
    return { scenario: 'catalog-and-basemap-failures', ...result }
  } finally { await page.close() }
}

async function runProductionDiagnosticsAbsent() {
  const page = await Page.create('')
  try {
    const result = await waitFor(page, `() => {
      const ready = (${currentLoad})().ready
      const diagnostics = typeof window.__usersideMapLibreDiagnostic !== 'undefined'
      const lifecycleControl = Boolean(document.querySelector('.lifecycle-test'))
      const thematicFilter = document.body.innerText.includes('Тематические слои')
      return ready && !diagnostics && !lifecycleControl && !thematicFilter ? { ready, diagnostics, lifecycleControl, thematicFilter } : null
    }`, 'production bundle omits dev diagnostics')
    const preload = page.events.find(event => event.method === 'Network.requestWillBeSent' && event.params.request.url.includes('/objects/preload'))
    const requestBody = JSON.parse(preload?.params.request.postData ?? '{}')
    if (!Array.isArray(requestBody.layerIds) || requestBody.layerIds.length !== 0) throw new Error(`Expected preload to retain empty layerIds: ${JSON.stringify(requestBody)}`)
    return { scenario: 'production-ui-and-loader', ...result, layerIds: requestBody.layerIds }
  } finally { await page.close() }
}

try {
  const results = [await runErrorRetry(), await runPartialRecovery(), await runVersionChange(), await runCatalogAndBasemapFailures(), await runProductionDiagnosticsAbsent()]
  console.log(JSON.stringify({ appUrl, cdpBase, results }, null, 2))
} catch (error) {
  console.error(error instanceof Error ? error.stack : error)
  process.exitCode = 1
}
