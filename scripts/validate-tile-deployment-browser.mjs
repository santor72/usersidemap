#!/usr/bin/env node
import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'

const base = process.env.CDP_URL ?? 'http://localhost:9225'
const url = process.env.APP_URL ?? 'http://localhost:3000/'
const output = 'reports/tiles/deployment-browser'
class Cdp {
  constructor(ws) {
    this.ws=ws;this.id=0;this.pending=new Map();this.events=[]
    ws.addEventListener('message',event=>{
      const m=JSON.parse(event.data);if(m.method)this.events.push(m)
      if(m.method==='Target.attachedToTarget') {
        this.send('Network.enable',{},m.params.sessionId)
          .then(()=>this.send('Runtime.runIfWaitingForDebugger',{},m.params.sessionId))
          .catch(()=>{})
      }
      const p=this.pending.get(m.id);if(!p)return;this.pending.delete(m.id)
      if(m.error)p.reject(new Error(m.error.message));else p.resolve(m.result)
    })
    ws.addEventListener('close',event=>{
      for(const p of this.pending.values())p.reject(new Error(`CDP connection closed (${event.code})`))
      this.pending.clear()
    })
  }
  static async connect(url) {
    const ws=new WebSocket(url)
    await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true})})
    return new Cdp(ws)
  }
  send(method,params={},sessionId) {return new Promise((resolve,reject)=>{const id=++this.id;this.pending.set(id,{resolve,reject});this.ws.send(JSON.stringify({id,method,params,sessionId}))})}
  async evaluate(expression) {
    const r=await this.send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true})
    if(r.exceptionDetails)throw new Error('Browser evaluation failed')
    return r.result.value
  }
  async until(check) {
    const end=Date.now()+60000
    while(!await check()){if(Date.now()>end)throw new Error('Browser gate timed out');await new Promise(resolve=>setTimeout(resolve,200))}
  }
}
await mkdir(output,{recursive:true})
const reports=[]
for(const dpr of [1,2]) {
  const target=await fetch(`${base}/json/new?about:blank`,{method:'PUT'}).then(r=>r.json())
  const page=await Cdp.connect(target.webSocketDebuggerUrl)
  const requests=()=>page.events.filter(e=>e.method==='Network.requestWillBeSent').map(e=>e.params.request)
  const preloads=()=>requests().filter(r=>r.url.endsWith('/objects/preload'))
  const tiles=(mode)=>page.events.filter(e=>e.method==='Network.responseReceived'&&e.params.response.url.includes(`/tiles/maps/3/${mode}/`))
  try {
    await page.send('Page.enable');await page.send('Runtime.enable');await page.send('Network.enable')
    await page.send('Target.setAutoAttach',{autoAttach:true,waitForDebuggerOnStart:true,flatten:true})
    await page.send('Network.setCacheDisabled',{cacheDisabled:true})
    await page.send('Emulation.setDeviceMetricsOverride',{width:1400,height:1000,deviceScaleFactor:dpr,mobile:false})
    await page.send('Page.navigate',{url});await page.send('Page.bringToFront')
    await page.until(()=>page.evaluate("document.querySelector('#cable-mode')?.value==='vector' && document.querySelector('.status-label')?.textContent.includes('Область загружена')"))
    assert.equal(await page.evaluate('Boolean(window.__usersideMapLibreDiagnostic || window.__usersideMapControllerDiagnostic)'),false)
    // Pan through the public UI to a known snapshot cable endpoint before overzoom.
    const bbox=JSON.parse(preloads().at(-1).postData).bbox
    const rect=await page.evaluate("(()=>{const r=document.querySelector('.maplibregl-canvas').getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height}})()")
    const merc=lat=>Math.log(Math.tan(Math.PI/4+lat*Math.PI/360))
    const target=[37.606768,55.792535]
    const dx=rect.width/2-(target[0]-bbox[0])/(bbox[2]-bbox[0])*rect.width
    const dy=rect.height/2-(merc(bbox[3])-merc(target[1]))/(merc(bbox[3])-merc(bbox[1]))*rect.height
    const cx=rect.x+rect.width/2,cy=rect.y+rect.height/2,count=preloads().length
    await page.send('Input.dispatchMouseEvent',{type:'mousePressed',x:cx,y:cy,button:'left',clickCount:1})
    for(let i=1;i<=10;i++) {
      await page.send('Input.dispatchMouseEvent',{type:'mouseMoved',x:cx+dx*i/10,y:cy+dy*i/10,buttons:1})
      await new Promise(resolve=>setTimeout(resolve,30))
    }
    await page.send('Input.dispatchMouseEvent',{type:'mouseReleased',x:cx+dx,y:cy+dy,button:'left',clickCount:1})
    await page.until(()=>Promise.resolve(preloads().length>count))
    while(JSON.parse(preloads().at(-1).postData).zoom<18) {
      const count=preloads().length
      await page.evaluate("document.querySelector('button[aria-label=\"Приблизить\"]').click()")
      await page.until(()=>Promise.resolve(preloads().length>count))
    }
    const modes=[]
    for(const mode of ['vector','raster','none']) {
      const count=preloads().length
      if(mode!=='vector') {
        await page.evaluate(`(()=>{const s=document.querySelector('#cable-mode');s.value='${mode}';s.dispatchEvent(new Event('change',{bubbles:true}))})()`)
        await page.until(()=>Promise.resolve(preloads().length>count))
      }
      await page.until(()=>page.evaluate("document.querySelector('.status-label')?.textContent.includes('Область загружена')"))
      if(mode!=='none')await page.until(()=>Promise.resolve(tiles(mode).some(e=>e.params.response.status===200)))
      await new Promise(resolve=>setTimeout(resolve,1800))
      const payload=JSON.parse(preloads().at(-1).postData)
      if(mode==='none'){assert(!payload.pin);assert(payload.objectTypes.includes('cable_line'))}
      else {assert.equal(payload.pin.format,mode);assert(!payload.objectTypes.includes('cable_line'))}
      const responses=tiles(mode).map(e=>e.params.response)
      for(const r of responses)assert.equal(r.status,200)
      for(const r of responses) {
        const parsed=new URL(r.url),parts=parsed.pathname.split('/')
        assert(Number(parts.at(-3))<=(mode==='vector'?14:15))
        if(mode==='raster')assert.equal(parsed.pathname.includes('@2x.webp'),dpr===2)
      }
      assert.equal(await page.evaluate("Boolean(document.querySelector('.error-text'))"),false)
      await writeFile(`${output}/${mode}-dpr${dpr}.jpg`,Buffer.from((await page.send('Page.captureScreenshot',{format:'jpeg',quality:90})).data,'base64'))
      console.log(`DPR${dpr} ${mode}: accepted at zoom ${payload.zoom}`)
      modes.push({mode,displayZoom:payload.zoom,tileResponses:responses.length,pinned:Boolean(payload.pin),requestedObjectTypes:payload.objectTypes})
    }
    const exceptions=page.events.filter(e=>e.method==='Runtime.exceptionThrown').length
    assert.equal(exceptions,0)
    assert(!requests().some(r=>r.headers.Authorization||r.headers.authorization))
    reports.push({dpr,modes,exceptions,productionDiagnosticsAbsent:true,browserBearerAbsent:true})
  } finally {page.ws.close();await fetch(`${base}/json/close/${target.id}`)}
}
await writeFile(`${output}/audit.json`,JSON.stringify({accepted:true,reports},null,2)+'\n')
console.log(JSON.stringify({accepted:true,dprs:reports.map(r=>r.dpr),modes:['none','vector','raster'],report:`${output}/audit.json`}))
