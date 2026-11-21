#!/usr/bin/env node
import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'

const base = process.env.CDP_URL ?? 'http://localhost:9225'
const url = process.env.APP_URL ?? 'http://localhost:8766/'
const output = process.env.OUTPUT_DIR ?? '/tmp/cable-tiles-browser'

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
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, { once: true }); ws.addEventListener('error', reject, { once: true })
    })
    return new Cdp(ws)
  }
  send(method, params={}) {
    return new Promise((resolve,reject) => {
      const id = ++this.sequence; this.pending.set(id,{resolve,reject}); this.ws.send(JSON.stringify({id,method,params}))
    })
  }
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true})
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails))
    return result.result.value
  }
}

await mkdir(output,{recursive:true})
const results=[]
for (const dpr of [1,2]) {
  await fetch(new URL('/audit-clear',url))
  const target = await fetch(`${base}/json/new?about:blank`,{method:'PUT'}).then(r=>r.json())
  const page = await Cdp.connect(target.webSocketDebuggerUrl)
  try {
    await page.send('Page.enable'); await page.send('Runtime.enable'); await page.send('Network.enable')
    await page.send('Emulation.setDeviceMetricsOverride',{width:1200,height:570,deviceScaleFactor:dpr,mobile:false})
    await page.send('Page.navigate',{url})
    const started=Date.now()
    while (!await page.evaluate('Boolean(window.prototype?.ready)')) {
      if (Date.now()-started>45000) throw new Error('Prototype initialization timeout')
      await new Promise(resolve=>setTimeout(resolve,100))
    }
    const native=await page.evaluate('window.prototype.snapshot()')
    assert.deepEqual(native.errors,[])
    assert(native.views.vector.ids.includes('cable:1'),'short cable at native vector')
    assert.deepEqual(native.views.none.ids,native.views.vector.ids,'native rendered ID parity')
    for (const view of Object.values(native.views)) assert.equal(view.topAtNode,'nodes','points above cables')
    const screenshot=async name=> {
      const result=await page.send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false})
      await writeFile(`${output}/${name}-dpr${dpr}.png`,Buffer.from(result.data,'base64'))
    }
    await screenshot('native')
    const lower=await page.evaluate('window.prototype.view(13)')
    assert(!lower.views.vector.ids.includes('cable:1'))
    assert.deepEqual(lower.views.none.ids,lower.views.vector.ids,'z13 rendered ID parity')
    const filtered=await page.evaluate('window.prototype.theme([1])')
    assert(!filtered.views.vector.ids.includes('cable:2'),'layer 11 must not match layer 1')
    assert.deepEqual(filtered.views.none.ids,filtered.views.vector.ids,'thematic rendered ID parity')
    await page.evaluate('window.prototype.theme([])')
    const overzoom=await page.evaluate('window.prototype.view(18,[37.000505,54.9995])')
    assert(overzoom.views.vector.ids.includes('cable:1'),'short cable survives overzoom')
    assert.deepEqual(overzoom.views.none.ids,overzoom.views.vector.ids,'overzoom rendered ID parity')
    await screenshot('overzoom')
    // MapLibre 6 fetches vector tiles inside workers; origin audit sees those
    // requests as well as raster requests, including their canonical XYZ.
    const requests=await fetch(new URL('/audit.json',url)).then(r=>r.json())
    const tiles=requests.map(path=>({url:new URL(path,url).href}))
    assert(tiles.some(tile=>tile.url.includes('/vector/')))
    assert(tiles.some(tile=>tile.url.includes('/raster/')))
    for (const tile of tiles) {
      const [,format,z]=tile.url.match(/\/(vector|raster)\/(\d+)\//)
      assert(Number(z)<=native.views[format].source.maxzoom,'only native files requested under overzoom')
      const response=await fetch(tile.url)
      assert.equal(response.status,200)
      if(format==='raster') assert.equal(tile.url.includes('@2x.webp'),dpr===2)
      else assert.equal(response.headers.get('content-encoding'),'gzip')
    }
    results.push({dpr,native,lower,filtered,overzoom,tileRequests:tiles.map(tile=>tile.url)})
  } finally {
    page.ws.close();await fetch(`${base}/json/close/${target.id}`)
  }
}
await writeFile(`${output}/report.json`,JSON.stringify(results,null,2)+'\n')
console.log(JSON.stringify({passed:true,dprs:[1,2],output}))
