#!/usr/bin/env node

import { writeFile } from 'node:fs/promises'
import { execFileSync } from 'node:child_process'

const cdpBase = process.env.CDP_URL ?? 'http://localhost:9223'
const appUrl = process.env.APP_URL ?? 'http://localhost:3002/?mapDiagnostics=1'
const timeoutMs = Number(process.env.TIMEOUT_MS ?? 180000)
const profileMarker = process.env.PROFILE_MARKER ?? 'userside-map-render-profile'
const screenshotPath = process.env.SCREENSHOT_PATH ?? '/tmp/full-dataset-maplibre.png'
const forceFullWorld = process.env.FULL_WORLD_PRELOAD !== 'false'

class Cdp {
  constructor(ws) {
    this.ws = ws
    this.id = 0
    this.pending = new Map()
    ws.addEventListener('message', event => {
      const message = JSON.parse(event.data)
      if (!message.id || !this.pending.has(message.id)) return
      const pending = this.pending.get(message.id)
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
  send(method, params = {}) {
    const id = ++this.id
    return new Promise((resolve, reject) => { this.pending.set(id, { resolve, reject }); this.ws.send(JSON.stringify({ id, method, params })) })
  }
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text)
    return result.result.value
  }
  close() { this.ws.close() }
}

const browserInfo = await fetch(`${cdpBase}/json/version`).then(r => r.json())
const browser = await Cdp.connect(browserInfo.webSocketDebuggerUrl)
const target = await fetch(`${cdpBase}/json/new?${encodeURIComponent(appUrl)}`, { method: 'PUT' }).then(r => r.json())
const page = await Cdp.connect(target.webSocketDebuggerUrl)
const hook = `(() => {
  const audit = window.__renderAudit = { batches: [], idles: [], sourceEvents: 0, mapAttached: false, uiReadyAt: null, gl: {draws: 0, uploads: 0, uploadBytes: 0, drawTimes: [], uploadTimes: []}, manifest: null, release: null }
  const now = () => performance.now()
  let diagnosticMap
  const attachMapDiagnostics = () => {
    diagnosticMap = window.__usersideMapLibreDiagnostic
    if (!diagnosticMap || audit.mapAttached) return
    audit.mapAttached = true
    diagnosticMap.on('sourcedata', event => { if (String(event.sourceId||'').startsWith('objects-')) { audit.sourceEvents++; audit.lastSourceEventAt=now() } })
    diagnosticMap.on('idle', () => {
      const layerIds = ['cable_line','network-circles','network-triangles','network-triangle-outlines','network-image-fallbacks'].filter(id => diagnosticMap.getLayer(id))
      let features = []
      try { if (layerIds.length) features = diagnosticMap.queryRenderedFeatures(undefined,{layers:layerIds}) } catch {}
      const ids = [...new Set(features.map(feature=>String(feature.properties?.__mapId ?? feature.id ?? '')).filter(Boolean))]
      const byType = {}
      for (const feature of features) { const type=String(feature.properties?.objectType??'unknown'); byType[type]=(byType[type]??0)+1 }
      audit.idles.push({time:now(),sourceEventCount:audit.sourceEvents,renderedUniqueIds:ids.length,renderedFeatures:features.length,byType,sourceLoaded:['objects-cable_line','objects-points'].filter(id=>diagnosticMap.getSource(id)).every(id=>diagnosticMap.isSourceLoaded(id)),tilesLoaded:diagnosticMap.areTilesLoaded()})
    })
  }
  const attachTimer=setInterval(()=>{attachMapDiagnostics();if(audit.mapAttached)clearInterval(attachTimer)},20)
  attachMapDiagnostics()
  const unique = new Set()
  for (const Context of [window.WebGLRenderingContext, window.WebGL2RenderingContext]) {
    if (!Context) continue
    const proto = Context.prototype
    for (const name of ['drawArrays','drawElements','drawArraysInstanced','drawElementsInstanced','bufferData','bufferSubData']) {
      const original = proto[name]
      if (!original || unique.has(original)) continue
      unique.add(original)
      proto[name] = function(...args) {
        if (this.canvas?.classList?.contains('maplibregl-canvas')) {
          const t = now()
          if (name.startsWith('draw')) { audit.gl.draws++; audit.gl.drawTimes.push(t) }
          else {
            audit.gl.uploads++
            const value = name === 'bufferSubData' ? args[2] : args[1]
            const bytes = typeof value === 'number' ? value : (value?.byteLength ?? 0)
            audit.gl.uploadBytes += bytes
            audit.gl.uploadTimes.push(t)
          }
        }
        return original.apply(this,args)
      }
    }
  }
  const realFetch = window.fetch.bind(window)
  window.fetch = async (input, init = {}) => {
    const url = typeof input === 'string' ? input : input.url
    if (url.includes('/objects/preload') && init.body) {
      const body = JSON.parse(init.body)
      ${forceFullWorld ? 'body.bbox = [-180,-85,180,85]; body.zoom = 15' : ''}
      init = {...init, body: JSON.stringify(body)}
    }
    const response = await realFetch(input,init)
    const t = now()
    if (url.includes('/objects/preload')) {
      const json = await response.clone().json()
      audit.manifest = {time:t,version:json.datasetVersion,totalObjects:json.totalObjects,totalVertices:json.totalVertices,batches:json.batches.length,request:JSON.parse(init.body)}
      await new Promise(resolve => { audit.release = resolve })
    } else if (url.includes('/objects/batch')) {
      try {
        const json=await response.clone().json()
        audit.batches.push({time:t,index:json.batchIndex,version:json.datasetVersion,count:json.featureCollection?.features?.length})
      } catch {}
    }
    return response
  }
})()`

try {
  await page.send('Page.enable'); await page.send('Runtime.enable')
  await page.send('Page.addScriptToEvaluateOnNewDocument', { source: hook })
  await page.send('Page.navigate', { url: appUrl })
  const waitUntil = async predicate => {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) { const value = await page.evaluate(`(${predicate})()`); if (value) return value; await new Promise(resolve => setTimeout(resolve,100)) }
    throw new Error(`Timed out: ${JSON.stringify(await page.evaluate(`({status:document.querySelector('.load-status')?.innerText,audit:window.__renderAudit})`))}`)
  }
  const before = await waitUntil('() => window.__renderAudit?.manifest && window.__renderAudit?.release && window.__renderAudit?.mapAttached && document.querySelector(".maplibregl-canvas") ? {manifest:window.__renderAudit.manifest} : null')
  await new Promise(resolve => setTimeout(resolve, 1200))
  const baseline = await browser.send('SystemInfo.getProcessInfo')
  const baselineJsHeap = await page.evaluate('performance.memory?.usedJSHeapSize ?? null')
  const processRss = () => execFileSync('ps', ['-axo','pid=,rss=,command='], { encoding:'utf8' }).split('\n').flatMap(line => {
    const match = line.trim().match(/^(\d+)\s+(\d+)\s+(.+)$/)
    if (!match || !match[3].includes(profileMarker)) return []
    const command = match[3]
    const type = command.includes('--type=renderer') ? 'renderer' : command.includes('--type=gpu-process') ? 'GPU' : command.includes('--type=') ? 'utility' : 'browser'
    return [{ pid:Number(match[1]), type, rssKB:Number(match[2]) }]
  })
  const baselineRss = processRss()
  const baselineSourceEvents = await page.evaluate('window.__renderAudit.baselineSourceEvents=window.__renderAudit.sourceEvents')
  const startTime = await page.evaluate('performance.now()')
  await page.evaluate('window.__renderAudit.release()')
  const finished = await waitUntil(`() => { const s=document.querySelector('.load-status')?.innerText||''; const m=s.match(/Пачки\\s+(\\d+)\\/(\\d+)\\s+·\\s+(\\d+)\\/(\\d+)\\s+объектов/); return s.includes('Область загружена') && m && +m[1]===+m[2] && +m[3]===+m[4] ? {status:s,audit:window.__renderAudit,heap:performance.memory?.usedJSHeapSize??null} : null }`)
  const uiReadyAt = await page.evaluate('window.__renderAudit.uiReadyAt=performance.now()')
  const settled = await waitUntil('() => { const a=window.__renderAudit; const lastBatch=Math.max(...a.batches.map(x=>x.time)); return a.idles.find(item=>item.time>=lastBatch&&item.sourceEventCount===a.sourceEvents&&item.sourceEventCount>a.baselineSourceEvents&&item.sourceLoaded&&item.tilesLoaded) ?? null }')
  await new Promise(resolve => setTimeout(resolve, 1000))
  const final = await page.evaluate('({audit:window.__renderAudit,heap:performance.memory?.usedJSHeapSize??null,canvas:[...document.querySelectorAll("canvas")].map(c=>({className:c.className,width:c.width,height:c.height}))})')
  const sampledAt = await page.evaluate('performance.now()')
  const after = await browser.send('SystemInfo.getProcessInfo')
  const afterRss = processRss()
  const screenshot = await page.send('Page.captureScreenshot', { format:'png', fromSurface:true })
  await writeFile(screenshotPath, Buffer.from(screenshot.data,'base64'))
  const responseBatchTimes = final.audit.batches.map(x=>x.time).sort((a,b)=>a-b)
  const firstBatch = responseBatchTimes[0], lastBatch = responseBatchTimes.at(-1)
  const firstAcceptedBatch = firstBatch
  const firstUploadAfterFirstBatch = final.audit.gl.uploadTimes.find(t=>t>=firstAcceptedBatch) ?? null
  const firstDrawAfterFirstBatch = final.audit.gl.drawTimes.find(t=>t>=firstAcceptedBatch) ?? null
  const lastUploadAfterLastBatch = final.audit.gl.uploadTimes.filter(t=>t>=lastBatch).at(-1) ?? null
  const firstDrawAfterLastBatch = final.audit.gl.drawTimes.find(t=>t>=lastBatch) ?? null
  const firstBatchIdle = final.audit.idles.find(item=>item.time>=firstBatch&&item.renderedUniqueIds>0) ?? null
  const fullViewportIdle = settled
  const processRows = info => info.processInfo.map(x=>({type:x.type,id:x.id,memoryFootprint:x.memoryFootprint}))
  const rssByType = rows => Object.fromEntries(['browser','renderer','GPU','utility'].map(type=>[type,rows.filter(x=>x.type===type).reduce((sum,x)=>sum+x.rssKB,0)]))
  const rssBefore = rssByType(baselineRss), rssAfter = rssByType(afterRss)
  const afterByPid = new Map(afterRss.map(row=>[row.pid,row]))
  const pidDeltas = baselineRss.flatMap(row=>afterByPid.has(row.pid)?[{pid:row.pid,type:row.type,deltaKB:afterByPid.get(row.pid).rssKB-row.rssKB}]:[])
  console.log(JSON.stringify({
    browser: browserInfo.Browser, tab: target.id, viewport: await page.evaluate('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})'), forceFullWorld,
    manifest: before.manifest, status: finished.status, receivedBatches:final.audit.batches.length,
    httpMs:{firstBatch:firstBatch-startTime,lastBatch:lastBatch-startTime},
    rendering:{firstBatchDisplayedIdle:firstBatchIdle,fullViewportIdle,firstDisplayAfterFirstBatchMs:firstBatchIdle===null?null:firstBatchIdle.time-firstBatch,viewportSettledAfterLastBatchMs:fullViewportIdle.time-lastBatch,sourceEvents:final.audit.sourceEvents,baselineSourceEvents,idleEvents:final.audit.idles.length},
    webgl:{draws:final.audit.gl.draws,uploads:final.audit.gl.uploads,uploadBytes:final.audit.gl.uploadBytes,firstUploadAfterFirstBatchMs:firstUploadAfterFirstBatch===null?null:firstUploadAfterFirstBatch-firstAcceptedBatch,firstDrawAfterFirstBatchMs:firstDrawAfterFirstBatch===null?null:firstDrawAfterFirstBatch-firstAcceptedBatch,firstDrawAfterLastBatchMs:firstDrawAfterLastBatch===null?null:firstDrawAfterLastBatch-lastBatch,lastUploadAfterLastBatchMs:lastUploadAfterLastBatch===null?null:lastUploadAfterLastBatch-lastBatch,lastDrawAfterLastBatchMs:final.audit.gl.drawTimes.at(-1)-lastBatch,quietPeriodAfterLastDrawMs:sampledAt-final.audit.gl.drawTimes.at(-1)},
    jsHeapBytes:{baseline:baselineJsHeap,after:final.heap},
    processMemoryRssKB:{baseline:rssBefore,after:rssAfter,delta:Object.fromEntries(Object.keys(rssBefore).map(type=>[type,rssAfter[type]-rssBefore[type]])),pidDeltas,processes:{baseline:baselineRss,after:afterRss},cdpProcessInfo:{baseline:processRows(baseline),after:processRows(after)}}, canvas:final.canvas,
    screenshot:screenshotPath,
    limitation:'MapLibre idle plus queryRenderedFeatures confirms settlement and visible IDs for the current viewport, not that all geographic features fit in one frame. Process RSS includes Chrome renderer/GPU allocations and is an aggregate proxy, not precise per-map WebGL/native memory.'
  },null,2))
} finally {
  page.close(); browser.close()
  await fetch(`${cdpBase}/json/close/${target.id}`).catch(()=>{})
}
