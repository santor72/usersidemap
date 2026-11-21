import { afterEach, expect, it, vi } from 'vitest'
import { createServer, type Server } from 'node:http'
import { createApp, toNodeListener } from 'h3'

const servers: Server[]=[]
async function listen(server: Server) {
  servers.push(server)
  await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve))
  const address=server.address()
  if(!address || typeof address==='string') throw Error('No test port')
  return `http://127.0.0.1:${address.port}`
}
afterEach(async()=>{
  await Promise.all(servers.splice(0).map(server=>new Promise<void>((resolve,reject)=>server.close(error=>error?reject(error):resolve()))))
  vi.unstubAllGlobals()
})
it('forwards a session without an empty Bearer header, and overrides credentials only for a service token',async()=>{
  const upstream=await listen(createServer((req,res)=>{
    const authorized=req.headers.authorization ? req.headers.authorization==='Bearer service-fixture' : req.headers.cookie==='map_tiles_session=cookie-fixture'
    res.statusCode=authorized?200:401
    res.setHeader('Content-Type','application/json')
    res.end(JSON.stringify({authorized,cookie: req.headers.cookie ?? null,authorizationPresent:req.headers.authorization!==undefined}))
  }))
  const config={mapApiInternalUrl:upstream,mapTilesToken:'',public:{mapId:3}}
  vi.stubGlobal('defineEventHandler',(handler:unknown)=>handler)
  vi.stubGlobal('useRuntimeConfig',()=>config)
  const metadata=(await import('../server/api/maps/[...path]')).default
  const objects=(await import('../server/api/map/[...path]')).default
  const app=createApp().use(async event=>{
    const isMetadata=event.path.startsWith('/api/maps/')
    event.context.params={path:event.path.slice(isMetadata?10:9)}
    return (isMetadata?metadata:objects)(event)
  })
  const origin=await listen(createServer(toNodeListener(app)))
  for(const path of ['/api/maps/3/tiles/catalog','/api/map/layers']) {
    const response=await fetch(origin+path,{headers:{Cookie:'map_tiles_session=cookie-fixture'}})
    expect(response.status).toBe(200)
    expect(await response.json()).toMatchObject({authorized:true,authorizationPresent:false})
  }
  config.mapTilesToken='service-fixture'
  const response=await fetch(origin+'/api/maps/3/tiles/catalog',{headers:{Authorization:'Bearer invalid-client',Cookie:'map_tiles_session=cookie-fixture'}})
  expect(response.status).toBe(200)
  expect(await response.json()).toMatchObject({authorized:true,authorizationPresent:true,cookie:''})
  const denied=await fetch(origin+'/api/maps/4/tiles/catalog')
  expect(denied.status).toBe(404);expect(denied.headers.get('Cache-Control')).toBe('no-store')
})
