#!/usr/bin/env node

import { createServer } from 'node:http'
import { writeFile } from 'node:fs/promises'

const cdpBase = process.env.CDP_URL ?? 'http://localhost:9222'
const apiBase = process.env.API_URL ?? 'http://localhost:8006/api/map'
const outPath = process.env.OUT ?? '/tmp/legacy-leaflet-comparison.png'
const highlightId = process.env.HIGHLIGHT_ID ?? ''
const center = [37.598289, 55.600926]
const bbox = [37.591, 55.596, 37.606, 55.606]

async function post(path, body) {
  const response = await fetch(`${apiBase}/objects/${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  })
  if (!response.ok) throw new Error(`${path} returned ${response.status}: ${await response.text()}`)
  return response.json()
}

const manifest = await post('preload', { mapId: 3, bbox, zoom: 15, objectTypes: ['cable_line', 'node', 'coupler'], layerIds: [] })
const features = []
for (const batch of manifest.batches) {
  const result = await post('batch', { queryId: manifest.queryId, batchIndex: batch.index })
  features.push(...result.featureCollection.features)
}

const html = `<!doctype html><html><head><meta charset="utf-8"><title>Legacy Leaflet style comparison</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@6.12.0/dist/maplibre-gl.css">
<style>*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;font:13px system-ui;background:#f4f6f6}.head{height:40px;display:flex;justify-content:space-between;align-items:center;padding:0 12px;background:white;border-bottom:1px solid #d8dee2}.maps{height:calc(100% - 40px);display:grid;grid-template-columns:1fr 1fr;gap:2px}.cell{position:relative;min-width:0;background:white}.label{position:absolute;z-index:1000;top:8px;left:50%;transform:translateX(-50%);background:#fffdf0eb;padding:5px 9px;border-radius:5px;box-shadow:0 1px 4px #3334;white-space:nowrap}#ml,#leaf{height:100%;width:100%}.leaflet-container{background:#ddd}.fallback-icon{background:none;border:none;color:#008000;line-height:1}</style></head><body>
<div class="head"><b>Published map 3 · equivalent ground scale: MapLibre 15 / Leaflet 16</b><span>${features.length} control objects · same Esri imagery, center and data${highlightId ? ` · highlighted ${highlightId}` : ''}</span></div>
<div class="maps"><div class="cell"><span class="label">MapLibre · application style rules</span><div id="ml"></div></div><div class="cell"><span class="label">Leaflet · reconstructed Map.js / MapHtml.php rules</span><div id="leaf"></div></div></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script type="module">
import * as ml from 'https://unpkg.com/maplibre-gl@6.12.0/dist/maplibre-gl.mjs'
const raw=${JSON.stringify(features)}, center=${JSON.stringify(center)}
window.__errors=[];window.addEventListener('error',e=>window.__errors.push(e.message))
const tile='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'
const map=new ml.Map({container:'ml',center,zoom:15,style:{version:8,sources:{esri:{type:'raster',tiles:[tile],tileSize:256}},layers:[{id:'esri',type:'raster',source:'esri'}]}});window.__ml=map;map.on('error',e=>window.__errors.push(e.error?.message||String(e.error)))
const prepare=f=>{const s=f.properties.style||{},shape=s.shape==='image'?'imageFallback':s.shape;let geometry=f.geometry;if(shape==='triangle'&&geometry.type==='Point'){const[x,y]=geometry.coordinates,h=.00005,d=.0001,r=n=>Math.round(n*1e7)/1e7,top=[x,r(y+h)],left=[r(x-d),r(y-h)],right=[r(x+d),r(y-h)];geometry={type:'Polygon',coordinates:[[top,left,right,top]]}}return{...f,geometry,properties:{...f.properties,__mapId:String(f.id),__mapShape:shape,__mapColor:s.color,__mapWidth:s.width,__mapDashArray:s.dashArray,__mapStrokeColor:s.strokeColor,__mapStrokeWidth:s.strokeWidth,__mapZIndex:s.zIndex}}}
const prepared=raw.map(prepare),lines=prepared.filter(f=>f.properties.objectType==='cable_line'),points=prepared.filter(f=>f.properties.objectType!=='cable_line')
const width=['coalesce',['get','__mapWidth'],3]
const dash=['case',['==',['get','__mapDashArray'],'20,15'],['case',['<=',width,1],['literal',[20,15]],['<=',width,2],['literal',[10,7.5]],['<=',width,3],['literal',[20/3,5]],['<=',width,4],['literal',[5,3.75]],['<=',width,5],['literal',[4,3]],['literal',[20/6,15/6]]],['==',['get','__mapDashArray'],'1,10'],['case',['<=',width,1],['literal',[1,10]],['<=',width,2],['literal',[.5,5]],['<=',width,3],['literal',[1/3,10/3]],['<=',width,4],['literal',[.25,2.5]],['<=',width,5],['literal',[.2,2]],['literal',[1/6,10/6]]],['literal',[1,1e6]]]
const radius=['interpolate',['linear'],['zoom'],13,1,14,2,15,4,16,7,17,14,18,25,19,28,22,28]
const imageSize=['interpolate',['linear'],['zoom'],13,1/24,14,6/24,15,10/24,16,17/24,17,30/24,18,50/24,22,50/24]
map.on('load',()=>{const c=document.createElement('canvas');c.width=48;c.height=48;const x=c.getContext('2d');x.fillStyle='#fff';x.beginPath();x.moveTo(24,4);x.lineTo(45,43);x.lineTo(3,43);x.closePath();x.fill();map.addImage('triangle-fallback',x.getImageData(0,0,48,48),{sdf:true,pixelRatio:2});map.addSource('lines',{type:'geojson',data:{type:'FeatureCollection',features:lines},promoteId:'__mapId'});map.addSource('points',{type:'geojson',data:{type:'FeatureCollection',features:points},promoteId:'__mapId'});map.addLayer({id:'cables',type:'line',source:'lines',layout:{'line-cap':'round','line-join':'round'},paint:{'line-color':['case',['boolean',['feature-state','selected'],false],'#FFFF59',['coalesce',['get','__mapColor'],'#0877d1']],'line-width':width,'line-dasharray':dash,'line-opacity':1}});const visible=['in',['get','objectType'],['literal',['node','coupler']]];map.addLayer({id:'circles',type:'circle',source:'points',filter:['all',visible,['==',['get','__mapShape'],'circle']],layout:{'circle-sort-key':['coalesce',['get','__mapZIndex'],0]},paint:{'circle-radius':radius,'circle-color':['coalesce',['get','__mapColor'],'#008000'],'circle-stroke-color':['coalesce',['get','__mapStrokeColor'],'#111827'],'circle-stroke-width':['coalesce',['get','__mapStrokeWidth'],1]}});const triangles=['all',visible,['==',['get','__mapShape'],'triangle']];map.addLayer({id:'triangles-fill',type:'fill',source:'points',filter:triangles,layout:{'fill-sort-key':['coalesce',['get','__mapZIndex'],0]},paint:{'fill-color':['coalesce',['get','__mapColor'],'#008000'],'fill-outline-color':['coalesce',['get','__mapStrokeColor'],'#111827']}});map.addLayer({id:'triangles-outline',type:'line',source:'points',filter:triangles,layout:{'line-cap':'round','line-join':'round','line-sort-key':['coalesce',['get','__mapZIndex'],0]},paint:{'line-color':['coalesce',['get','__mapStrokeColor'],'#111827'],'line-width':['coalesce',['get','__mapStrokeWidth'],1]}});map.addLayer({id:'image-fallbacks',type:'symbol',source:'points',filter:['all',visible,['==',['get','__mapShape'],'imageFallback']],layout:{'icon-image':'triangle-fallback','icon-size':imageSize,'icon-allow-overlap':true}})})
const leaf=L.map('leaf',{zoomControl:false,attributionControl:false}).setView([center[1],center[0]],16);window.__leaf=leaf;L.tileLayer(tile,{tileSize:256,maxZoom:19}).addTo(leaf)
const legacyZoom=leaf.getZoom(),nodeRadius=z=>z<15?1:z===15?2:z===16?4:z===17?7:z===18?14:z===19?25:28
const legacyGeometry=raw.map(f=>{if(f.properties.style?.shape==='triangle'&&f.geometry.type==='Point'){const[x,y]=f.geometry.coordinates,h=.00005,d=.0001,r=n=>Math.round(n*1e7)/1e7,top=[x,r(y+h)],left=[r(x-d),r(y-h)],right=[r(x+d),r(y-h)];return{...f,geometry:{type:'Polygon',coordinates:[[top,left,right,top]]}}}return f})
const legacyStyle=f=>{const s=f.properties.style||{};if(f.geometry.type==='LineString')return{color:s.color||'#3388ff',weight:s.width||3,dashArray:s.dashArray||null,opacity:1,lineCap:'round',lineJoin:'round'};return{color:s.strokeColor||'#000',weight:s.strokeWidth??1,dashArray:s.dashArray||null,opacity:1,fillColor:s.color||'#008000',fillOpacity:1}}
const legacy=L.geoJSON(legacyGeometry,{style:legacyStyle,pointToLayer:(f,ll)=>{const s=f.properties.style||{};if(s.shape==='circle')return L.circleMarker(ll,{...legacyStyle(f),radius:nodeRadius(legacyZoom)});const size=legacyZoom===16?10:6;return L.marker(ll,{icon:L.divIcon({className:'fallback-icon',html:'▲',iconSize:[size,size],iconAnchor:[size/2,size/2]})})}}).addTo(leaf);window.__legacyFeatures=legacy
window.__legacyFeatures=legacy;window.__setHighlight=id=>{map.setFeatureState({source:'lines',id},{selected:true});map.getSource('lines').updateData({update:[{id,addOrUpdateProperties:[{key:'__mapWidth',value:6}]}]});legacy.eachLayer(layer=>{if(String(layer.feature?.id)===id)layer.setStyle({color:'#FFFF59',weight:6})})};window.__ready=false;map.once('idle',()=>{window.__ready=true;${highlightId ? `window.__setHighlight(${JSON.stringify(highlightId)})` : ''}});setTimeout(()=>{map.resize();leaf.invalidateSize()},400)
</script></body></html>`

const server = createServer((_request, response) => { response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); response.end(html) })
await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
const address = server.address()
if (!address || typeof address === 'string') throw new Error('No local comparison server address')
const target = await fetch(`${cdpBase}/json/new?${encodeURIComponent(`http://localhost:${address.port}/`)}`, { method: 'PUT' }).then(r => r.json())
const socket = new WebSocket(target.webSocketDebuggerUrl)
await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }) })
let nextId = 0
const pending = new Map()
const browserErrors = []
socket.addEventListener('message', event => {
  const message = JSON.parse(event.data)
  if (message.method === 'Runtime.exceptionThrown') browserErrors.push(message.params.exceptionDetails.exception?.description ?? message.params.exceptionDetails.text)
  if (message.method === 'Log.entryAdded' && ['error', 'warning'].includes(message.params.entry.level)) browserErrors.push(`${message.params.entry.level}: ${message.params.entry.text}`)
  if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id) }
})
const send = (method, params = {}) => new Promise(resolve => { const id = ++nextId; pending.set(id, resolve); socket.send(JSON.stringify({ id, method, params })) })
try {
  await send('Page.enable'); await send('Runtime.enable'); await send('Log.enable')
  await send('Emulation.setDeviceMetricsOverride', { width: 1200, height: 800, deviceScaleFactor: 1, mobile: false })
  await send('Page.navigate', { url: `http://localhost:${address.port}/` })
  const evaluate = async expression => (await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })).result.result.value
  let state
  for (let attempt = 0; attempt < 100; attempt++) {
    state = await evaluate(`({ ready: window.__ready, errors: window.__errors, leaflet: Boolean(window.__leaf), canvasCount: document.querySelectorAll('canvas').length, total: ${features.length} })`)
    if (state.ready && state.leaflet) break
    await new Promise(resolve => setTimeout(resolve, 150))
  }
  await new Promise(resolve => setTimeout(resolve, 500))
  const png = await send('Page.captureScreenshot', { format: 'png' })
  await writeFile(outPath, Buffer.from(png.result.data, 'base64'))
  const result = await evaluate(`({ ready: window.__ready, errors: window.__errors, maplibreZoom: window.__ml?.getZoom(), leafletZoom: window.__leaf?.getZoom(), maplibreFeatures: window.__ml?.queryRenderedFeatures().length, leafletLayers: window.__legacyFeatures?.getLayers().length })`)
  if (!state.ready || browserErrors.length || (result.errors?.length ?? 0)) throw new Error(`Comparison page failed: ${JSON.stringify({ state, result, browserErrors })}`)
  console.log(JSON.stringify({ snapshot: manifest.datasetVersion, objectCount: features.length, types: features.reduce((counts, feature) => { const type = feature.properties.objectType; counts[type] = (counts[type] ?? 0) + 1; return counts }, {}), highlightId: highlightId || null, result, screenshot: outPath, note: 'Leaflet page reconstructs legacy style and triangle geometry from Map.js/MapHtml.php; missing legacy image files are shown as a marked triangle fallback.' }, null, 2))
} finally {
  try { await send('Page.close') } catch {}
  socket.close()
  server.close()
}
