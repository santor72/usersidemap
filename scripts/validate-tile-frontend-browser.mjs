#!/usr/bin/env node
import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'

const base = process.env.CDP_URL ?? 'http://localhost:9225'
const url = process.env.APP_URL ?? 'http://localhost:3019/?mapDiagnostics=1'
const output = process.env.OUTPUT_DIR ?? 'reports/tiles/frontend-vector'
class Cdp {
  constructor(ws) {
    this.ws = ws; this.sequence = 0; this.pending = new Map(); this.events = []
    ws.addEventListener('message', event => {
      const message = JSON.parse(event.data)
      if (message.method) this.events.push(message)
      const pending = this.pending.get(message.id)
      if (!pending) return
      this.pending.delete(message.id)
      if (message.error) pending.reject(new Error(message.error.message))
      else pending.resolve(message.result)
    })
  }
  static async connect(url) {
    const ws = new WebSocket(url)
    await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, { once: true }); ws.addEventListener('error', reject, { once: true }) })
    return new Cdp(ws)
  }
  send(method, params = {}) { return new Promise((resolve, reject) => { const id = ++this.sequence; this.pending.set(id, { resolve, reject }); this.ws.send(JSON.stringify({ id, method, params })) }) }
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails))
    return result.result.value
  }
  async until(expression) {
    const start = Date.now()
    while (!(await this.evaluate(expression))) {
      if (Date.now() - start > 45000) throw new Error(`Timeout: ${expression}`)
      await new Promise(resolve => setTimeout(resolve, 150))
    }
  }
}
const target = await fetch(`${base}/json/new?about:blank`, { method: 'PUT' }).then(r => r.json())
const page = await Cdp.connect(target.webSocketDebuggerUrl)
const requests = () => page.events.filter(e => e.method === 'Network.requestWillBeSent').map(e => e.params.request)
try {
  await mkdir(output, { recursive: true })
  await page.send('Page.enable'); await page.send('Runtime.enable'); await page.send('Network.enable')
  await page.send('Emulation.setDeviceMetricsOverride', { width: 1400, height: 1000, deviceScaleFactor: 1, mobile: false })
  await page.send('Page.navigate', { url })
  await page.until("Boolean(window.__usersideMapLibreDiagnostic?.getLayer('cable_line-tiles')) && document.querySelector('.status-label')?.textContent.includes('Область загружена')")
  await page.until("window.__usersideMapLibreDiagnostic.isSourceLoaded('cables-tiles') && window.__usersideMapLibreDiagnostic.queryRenderedFeatures({layers:['cable_line-tiles','cable_line-dashed-tiles']}).length > 0")
  const initial = await page.evaluate(`(() => {
    const m = window.__usersideMapLibreDiagnostic, style = m.getStyle(), layers = style.layers.map(x => x.id)
    return { mode: document.querySelector('#cable-mode').value, source: style.sources['cables-tiles'], layers,
      cableIds: [...new Set(m.queryRenderedFeatures({ layers: ['cable_line-tiles','cable_line-dashed-tiles'] }).map(f => f.properties.objectId))].sort(),
      points: style.sources['objects-points'].data.features.map(f => f.id), lag: Boolean(document.querySelector('.tiles-lag-banner')), maxZoom: m.getMaxZoom() }
  })()`)
  assert.equal(initial.mode, 'vector'); assert.equal(initial.source.maxzoom, 14); assert.equal(initial.source.promoteId, 'objectId'); assert.equal(initial.maxZoom, 18)
  assert.deepEqual(initial.points, ['node:100']); assert(initial.lag)
  assert(initial.layers.indexOf('cable_line-dashed-tiles') < initial.layers.indexOf('network-circles'))
  const preloadRequests = requests().filter(r => r.url.endsWith('/objects/preload')).map(r => JSON.parse(r.postData))
  assert(preloadRequests.length); assert(preloadRequests.every(r => r.pin?.format === 'vector' && !r.objectTypes.includes('cable_line')))
  assert.match(initial.source.tiles[0], /:8009\/tiles\/maps\/3\/vector\//)
  const themeIds = {}
  for (const id of [1,11]) {
    await page.evaluate(`(() => { const s=document.querySelector('#theme-layer'); s.value='${id}'; s.dispatchEvent(new Event('change',{bubbles:true})); })()`)
    await new Promise(resolve => setTimeout(resolve,350))
    themeIds[id] = await page.evaluate(`window.__usersideMapLibreDiagnostic.queryRenderedFeatures({layers:['cable_line-tiles','cable_line-dashed-tiles']}).map(f=>({id:f.properties.objectId,layerIds:f.properties.layerIds}))`)
    assert(themeIds[id].length); assert(themeIds[id].every(f => f.layerIds.includes(`,${id},`)))
  }
  await page.evaluate("(() => { const s=document.querySelector('#theme-layer'); s.value=''; s.dispatchEvent(new Event('change',{bubbles:true})); })()")
  await new Promise(resolve => setTimeout(resolve,350))
  const hit = await page.evaluate(`(() => { const m=window.__usersideMapLibreDiagnostic; const f=m.queryRenderedFeatures({layers:['cable_line-tiles','cable_line-dashed-tiles']}).find(f=>f.properties.sourceKind==='cable_group'); const coords=f.geometry.type==='MultiLineString'?f.geometry.coordinates[0]:f.geometry.coordinates; const p=m.project(coords[Math.floor(coords.length/2)]); const rect=m.getCanvas().getBoundingClientRect(); return { x:rect.left+p.x,y:rect.top+p.y,objectId:f.properties.objectId }; })()`)
  await page.send('Input.dispatchMouseEvent', { type:'mousePressed',button:'left',clickCount:1,x:hit.x,y:hit.y })
  await page.send('Input.dispatchMouseEvent', { type:'mouseReleased',button:'left',clickCount:1,x:hit.x,y:hit.y })
  await page.until("Boolean(document.querySelector('.feature-card dl'))")
  const selection = await page.evaluate("window.__usersideMapLibreDiagnostic.getStyle().sources['selected-cable'].data.features[0]")
  assert.equal(selection.id, hit.objectId); assert.equal(selection.geometry.type, 'MultiLineString'); assert.equal(selection.geometry.coordinates.length, 2)
  assert(requests().some(r => /\/tiles\/[^/]+\/objects\/cable_group\/5$/.test(r.url)))
  await writeFile(`${output}/vector-selected.png`, Buffer.from((await page.send('Page.captureScreenshot',{format:'png'})).data,'base64'))
  await page.evaluate("Array.from(document.querySelectorAll('.button-pair button')).find(b=>b.textContent==='Измерение').click()")
  await page.send('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,x:600,y:650})
  await page.send('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,x:600,y:650})
  const measure = await page.evaluate("window.__usersideMapLibreDiagnostic.getStyle().sources['measure-points'].data.features[0].geometry.coordinates")
  const expected = await page.evaluate("(() => {const m=window.__usersideMapLibreDiagnostic,r=m.getCanvas().getBoundingClientRect(),p=m.unproject([600-r.left,650-r.top]);return[p.lng,p.lat]})()")
  assert(Math.abs(measure[0]-expected[0])<1e-8); assert(Math.abs(measure[1]-expected[1])<1e-8)
  await page.evaluate("(() => {const s=document.querySelector('#cable-mode');s.value='none';s.dispatchEvent(new Event('change',{bubbles:true}));})()")
  await page.until("!window.__usersideMapLibreDiagnostic.getSource('cables-tiles') && document.querySelector('.status-label')?.textContent.includes('Область загружена')")
  const nonePreload = requests().filter(r => r.url.endsWith('/objects/preload')).at(-1)
  assert(!JSON.parse(nonePreload.postData).pin); assert(!await page.evaluate("Boolean(document.querySelector('.feature-card'))"))
  const errors=page.events.filter(e=>e.method==='Runtime.exceptionThrown').map(e=>e.params.exceptionDetails.text)
  assert.deepEqual(errors, [])
  // Never persist request headers or signed templates in reports.
  const report={ accepted:true,initial:{ ...initial,source:{type:initial.source.type,maxzoom:initial.source.maxzoom,promoteId:initial.source.promoteId}},themeIds,selection:{id:selection.id,geometryType:selection.geometry.type,pieces:selection.geometry.coordinates.length},measure,nonePin:false,uncaughtErrors:errors }
  await writeFile(`${output}/audit.json`,JSON.stringify(report,null,2)+'\n')
  console.log(JSON.stringify(report,null,2))
} finally {
  await page.send('Page.close').catch(()=>{});page.ws.close()
}
