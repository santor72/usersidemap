#!/usr/bin/env node
import assert from 'node:assert/strict'
import { createHmac } from 'node:crypto'
import { mkdir, writeFile } from 'node:fs/promises'

const base = process.env.CDP_URL ?? 'http://localhost:9225'
const url = process.env.APP_URL ?? 'http://localhost:3019/?mapDiagnostics=1'
const output = process.env.OUTPUT_DIR ?? 'reports/tiles/frontend-raster-refresh'
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
await mkdir(output,{recursive:true})
const reports=[]
for(const dpr of [1,2]) {
  const target=await fetch(`${base}/json/new?about:blank`,{method:'PUT'}).then(r=>r.json())
  const page=await Cdp.connect(target.webSocketDebuggerUrl)
  const requests=()=>page.events.filter(e=>e.method==='Network.requestWillBeSent').map(e=>e.params.request)
  const preloads=()=>requests().filter(r=>r.url.endsWith('/objects/preload'))
  try {
    await page.send('Page.enable');await page.send('Runtime.enable');await page.send('Network.enable')
    // Disable Nuxt development sockets: HMR reconnect reloads a frozen document.
    await page.send('Page.addScriptToEvaluateOnNewDocument',{source: `window.WebSocket=class extends EventTarget { static CONNECTING=0; static OPEN=1; static CLOSING=2; static CLOSED=3; constructor(url){super();this.url=url;this.readyState=0} send(){} close(){this.readyState=3} }`})
    await page.send('Emulation.setDeviceMetricsOverride',{width:1400,height:1000,deviceScaleFactor:dpr,mobile:false})
    await page.send('Page.navigate',{url});await page.send('Page.bringToFront')
    await page.until("Boolean(window.__usersideMapLibreDiagnostic?.getLayer('cable_line-tiles')) && document.querySelector('.status-label')?.textContent.includes('Область загружена')")
    await page.until("window.__usersideMapLibreDiagnostic.isSourceLoaded('cables-tiles')")
    await page.evaluate("(()=>{const s=document.querySelector('#cable-mode');s.value='raster';s.dispatchEvent(new Event('change',{bubbles:true}))})()")
    await page.until("Boolean(window.__usersideMapLibreDiagnostic.getLayer('cable-raster')) && document.querySelector('.status-label')?.textContent.includes('Область загружена')")
    await page.until("window.__usersideMapLibreDiagnostic.isSourceLoaded('cables-tiles')")
    const raster=await page.evaluate("(()=>{const m=window.__usersideMapLibreDiagnostic,s=m.getStyle();return{source:s.sources['cables-tiles'],layers:s.layers.map(l=>l.id),points:s.sources['objects-points'].data.features.map(f=>f.id)}})()")
    assert.equal(raster.source.type,'raster');assert.equal(raster.source.tileSize,256);assert.equal(raster.source.maxzoom,15)
    assert.equal(raster.source.tiles[0].includes('@2x.webp'),dpr===2)
    assert(raster.layers.indexOf('cable-raster')<raster.layers.indexOf('network-circles'));assert.deepEqual(raster.points,['node:101'])
    const rasterPin=JSON.parse(preloads().at(-1).postData).pin
    assert.equal(rasterPin.format,'raster')
    await page.evaluate("(()=>{const m=window.__usersideMapLibreDiagnostic,p=m.project([37.0025,55]);m.fire('click',{point:p,lngLat:m.unproject(p)});})()")
    assert(!await page.evaluate("Boolean(document.querySelector('.feature-card'))"))
    await writeFile(`${output}/raster-dpr${dpr}.png`,Buffer.from((await page.send('Page.captureScreenshot',{format:'png'})).data,'base64'))
    await page.evaluate("(()=>{const s=document.querySelector('#cable-mode');s.value='vector';s.dispatchEvent(new Event('change',{bubbles:true}))})()")
    await page.until("Boolean(window.__usersideMapLibreDiagnostic.getLayer('cable_line-tiles')) && document.querySelector('.status-label')?.textContent.includes('Область загружена')")
    await page.until("window.__usersideMapLibreDiagnostic.isSourceLoaded('cables-tiles')")
    const vectorPin=JSON.parse(preloads().at(-1).postData).pin
    assert.notEqual(vectorPin.tileVersion,rasterPin.tileVersion)
    assert.deepEqual(await page.evaluate("window.__usersideMapLibreDiagnostic.getStyle().sources['objects-points'].data.features.map(f=>f.id)"),['node:100'])
    await page.evaluate("(()=>{const m=window.__usersideMapLibreDiagnostic,p=m.project([37.0025,55]);m.fire('click',{point:p,lngLat:m.unproject(p)});window.__signedBefore=m.getStyle().sources['cables-tiles'].tiles[0]})()")
    await page.until("Boolean(document.querySelector('.feature-card dl'))")
    const loadCount=preloads().length
    await page.until("window.__usersideMapLibreDiagnostic.getStyle().sources['cables-tiles'].tiles[0] !== window.__signedBefore")
    assert.equal(preloads().length,loadCount)
    assert.equal(await page.evaluate("window.__usersideMapLibreDiagnostic.getStyle().sources['selected-cable'].data.features[0].id"),'cable_group:5')
    // Freeze JS beyond HMAC TTL, then restore focus: real expiry, polling still zero.
    await page.send('Page.setWebLifecycleState',{state:'frozen'})
    await new Promise(resolve=>setTimeout(resolve,11000))
    await page.send('Page.setWebLifecycleState',{state:'active'});await page.send('Page.bringToFront')
    await page.evaluate("window.dispatchEvent(new Event('focus'))")
    await page.until("(()=>{const t=window.__usersideMapLibreDiagnostic?.getStyle()?.sources?.['cables-tiles']?.tiles?.[0];return t && Number(new URL(t).searchParams.get('exp'))*1000>Date.now()})()")
    assert.equal(preloads().length,loadCount)
    await page.evaluate("window.__tileStatuses=[];void window.__usersideMapLibreDiagnostic.on('error',e=>{window.__tileStatuses.push({status:e.error.status,message:e.error.message?.split('http')[0]})})")
    // Hold the preventive timer so this gate must recover via actual XYZ410.
    await page.evaluate("clearTimeout(window.__usersideMapControllerDiagnostic.tileLoop.timer)")
    const catalogBefore=requests().filter(r=>r.url.endsWith('/tiles/catalog')).length
    // Actual retirement: publish two immutable successors; force uncached old XYZ.
    await page.evaluate("window.__retiredBefore=window.__usersideMapLibreDiagnostic.getStyle().sources['cables-tiles'].tiles[0]")
    await page.evaluate("fetch('http://localhost:8009/fixture/advance-vector',{method:'POST'}).then(r=>{if(!r.ok)throw Error('fixture publication failed');return r.json()})")
    const retired=await page.evaluate('window.__retiredBefore')
    const prefix=new URL(retired).pathname.split('/').slice(0,6).join('/')
    const exp=Math.floor(Date.now()/1000)+60
    const sig=createHmac('sha256','disposable-frontend-hmac-secret-32-characters').update([prefix,String(exp)].join(String.fromCharCode(10))).digest('hex')
    const freshRetired=retired.split('?')[0]+`?exp=${exp}&sig=${sig}&audit=retire`
    await page.evaluate(`(()=>{const m=window.__usersideMapLibreDiagnostic,s=m.getStyle(),layers=s.layers.filter(l=>l.source==='cables-tiles');for(const l of layers)m.removeLayer(l.id);m.removeSource('cables-tiles');m.addSource('cables-tiles',{...s.sources['cables-tiles'],tiles:[${JSON.stringify(freshRetired)}]});for(const l of layers)m.addLayer(l,'network-circles');m.jumpTo({center:[37.04,55]})})()`)
    await page.until("window.__usersideMapLibreDiagnostic.getStyle().sources['cables-tiles'].tiles[0].split('/')[7] !== window.__retiredBefore.split('/')[7]")
    await page.until("document.querySelector('.status-label')?.textContent.includes('Область загружена')")
    assert(page.events.some(e=>e.method==='Network.responseReceived'&&e.params.response.status===410&&e.params.response.url.endsWith('/objects/preload')) || await page.evaluate("window.__tileStatuses.some(e=>e.status===410||e.message?.includes('(410)'))"))
    const catalogDelta=requests().filter(r=>r.url.endsWith('/tiles/catalog')).length-catalogBefore
    assert.equal(catalogDelta,1)
    assert(!await page.evaluate("Boolean(document.querySelector('.feature-card'))"))
    assert.equal(await page.evaluate("document.querySelector('#cable-mode').value"),'vector')
    const errors=page.events.filter(e=>e.method==='Runtime.exceptionThrown').map(e=>e.params.exceptionDetails.text)
    assert.deepEqual(errors,[])
    reports.push({dpr,retina:dpr===2,nativeMaxzoom:raster.source.maxzoom,tileSize:raster.source.tileSize,rasterPoints:raster.points,vectorPoints:['node:100'],rasterNoHit:true,renewalPreservedSelection:true,sleepRecovery:true,retireRefresh:true,catalogDelta,uncaughtErrors:errors})
  } catch(error) { console.error(await page.evaluate("JSON.stringify({text:document.body.innerText,statuses:window.__tileStatuses,map:!!window.__usersideMapLibreDiagnostic,style:!!window.__usersideMapLibreDiagnostic?.getStyle(),canvas:window.__usersideMapLibreDiagnostic?.getCanvas()?.isConnected})")); throw error } finally {await page.send('Page.close').catch(()=>{});page.ws.close()}
}
await writeFile(`${output}/audit.json`,JSON.stringify({accepted:true,reports},null,2)+'\n')
console.log(JSON.stringify({accepted:true,reports},null,2))
