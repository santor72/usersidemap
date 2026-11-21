import { afterEach, describe, expect, it, vi } from 'vitest'
import { MapDataClient } from '../modules/map-data/runtime/core/client'
import { MapTileClient, type TileCatalog, type TileDescriptor } from '../modules/map-data/runtime/core/tile-client'
import { MapController } from '../modules/cartography/runtime/core/controller'
import { RecordingMapAdapter } from './support/RecordingMapAdapter'
import type { Feature } from '../modules/map-data/runtime/core/types'

const json = (value: unknown, etag?: string) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json', ...(etag ? { ETag: etag } : {}) } })
const descriptor: TileDescriptor = { tileVersion: 'release-a', datasetVersion: 'snapshot-old', minzoom: 1, maxzoom: 14, displayMaxzoom: 18, tileSize: 512,
  template: 'https://tiles.test/1/vector/release-a/{z}/{x}/{y}.pbf', template2x: null, templateExpiresAt: null,
  tilesBehindActive: true, datasetAge: 100, capabilities: { layerFilter: true, hitTest: true, dash: true } }
const tileCatalog = (active: TileDescriptor | null = descriptor): TileCatalog => ({ mapId: 3, activeDatasetVersion: 'snapshot-new', tiles_lag: 'banner', catalogPollSeconds: 0,
  layers: [{ id: 1, title: 'One', defaultVisible: true }, { id: 11, title: 'Eleven', defaultVisible: true }],
  formats: { vector: { active, prev: null }, raster: { active: null, prev: null } } })
const options = { mapId: 3, cableTiles: 'vector' as const, initialView: { longitude: 0, latitude: 0, zoom: 15 }, objectTypes: ['node', 'cable_line'], tools: ['select', 'measure'] as Array<'select' | 'measure'> }
const cable: Feature = { type: 'Feature', id: 'cable_group:5', geometry: { type: 'LineString', coordinates: [[0, 0], [0.5, 0]] }, properties: { sourceKind: 'cable_group', sourceId: 5, objectType: 'cable_line' } }
const fullGeometry = { type: 'MultiLineString', coordinates: [[[0, 0], [0.5, 0]], [[10, 10], [11, 10]]] }
const details = { tileVersion: 'release-a', datasetVersion: 'snapshot-old', objectId: cable.id, source_kind: 'cable_group', source_id: 5, object_type: 'cable_line', title: 'Full route', geometry: fullGeometry, style: {}, properties: {}, layer_ids: [1], links: [], length_m: 1, cableEndpoints: ['Start node', 'End building'], renderAttributes: {} }
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

function stubApi(catalog = tileCatalog()) {
  const preloads: Array<Record<string, unknown>> = []
  const fetch = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/tiles/catalog')) return json(catalog)
    if (url.includes('/layers?')) return json({ datasetVersion: 'snapshot-new', objectTypes: [{ object_type: 'node', count: 1, min_zoom: 1 }, { object_type: 'cable_line', count: 5, min_zoom: 1 }], layers: [] })
    if (url.endsWith('/objects/preload')) {
      const body = JSON.parse(String(init?.body)); preloads.push(body)
      return json({ queryId: 'query', datasetVersion: body.pin ? 'snapshot-old' : 'snapshot-new', totalObjects: 0, totalVertices: 0, typeCounts: {}, batches: [] })
    }
    return json(details)
  })
  vi.stubGlobal('fetch', fetch)
  return { preloads, fetch }
}

describe('tile metadata and snapshot controller', () => {
  it('uses ETag 304 and accepts new signatures for the same release', async () => {
    let call = 0
    vi.stubGlobal('fetch', vi.fn(async (_: unknown, init?: RequestInit) => {
      call++
      if (call === 2) { expect(init?.headers).toEqual({ 'If-None-Match': '"a"' }); return new Response(null, { status: 304 }) }
      return json(tileCatalog(call === 1 ? descriptor : { ...descriptor, template: descriptor.template + '?exp=2&sig=new' }), call === 1 ? '"a"' : '"b"')
    }))
    const client = new MapTileClient()
    const first = await client.catalog(3)
    expect(await client.catalog(3)).toBe(first)
    expect((await client.catalog(3)).formats.vector.active?.template).toContain('?exp=2')
  })
  it('pins only point preload to the vector release and switches none without pin', async () => {
    const { preloads } = stubApi()
    const adapter = new RecordingMapAdapter()
    const controller = new MapController(options, new MapDataClient(), adapter)
    await controller.start()
    expect(controller.cableMode).toBe('vector')
    expect(controller.lagging).toBe(true)
    expect(preloads[0]).toMatchObject({ objectTypes: ['node'], pin: { format: 'vector', tileVersion: 'release-a' } })
    expect(adapter.tiles.at(-1)?.descriptor?.maxzoom).toBe(14)
    controller.setThematicLayers([1]); expect(adapter.tiles.at(-1)?.layerIds).toEqual([1])
    await controller.setCableMode('none')
    expect(preloads.at(-1)).toMatchObject({ objectTypes: ['node', 'cable_line'] })
    expect(preloads.at(-1)).not.toHaveProperty('pin')
    expect(adapter.tiles.at(-1)?.descriptor).toBeUndefined()
    controller.destroy()
  })
  it('uses none on cold start but refuses a runtime loss of active', async () => {
    const { preloads, fetch } = stubApi(tileCatalog(null))
    const controller = new MapController(options, new MapDataClient(), new RecordingMapAdapter())
    await controller.start()
    expect(controller.coldStart).toBe(true)
    expect(preloads[0]).not.toHaveProperty('pin')
    fetch.mockImplementationOnce(async () => json(tileCatalog()))
    await controller.refreshTileCatalog(); await controller.setCableMode('vector')
    fetch.mockImplementationOnce(async () => json(tileCatalog(null)))
    await expect(controller.refreshTileCatalog()).rejects.toThrow('Активный выпуск')
    expect(controller.cableMode).toBe('vector')
    controller.destroy()
  })
  it('fetches full cable geometry without queryId and clears it on mode switch', async () => {
    const { fetch } = stubApi()
    const adapter = new RecordingMapAdapter(), client = new MapDataClient()
    const controller = new MapController(options, client, adapter)
    await controller.start()
    adapter.fireSelect(cable)
    await vi.waitFor(() => expect(controller.details?.title).toBe('Full route'))
    expect(controller.details?.cableEndpoints).toEqual(['Start node', 'End building'])
    expect(controller.selected?.geometry).toEqual(fullGeometry)
    expect(adapter.selectedFeature?.geometry).toEqual(fullGeometry)
    expect(client.getFeatures()).toHaveLength(0)
    const request = fetch.mock.calls.find(([url]) => String(url).includes('/release-a/objects/'))
    expect(String(request?.[0])).not.toContain('queryId')
    await controller.setCableMode('none')
    expect(controller.selected).toBeUndefined(); expect(adapter.selectedFeature).toBeUndefined()
    controller.destroy()
  })
  it('rejects a wrong pinned dataset before loading any batch', async () => {
    const { fetch } = stubApi()
    fetch.mockImplementationOnce(async () => json({ queryId: 'wrong', datasetVersion: 'snapshot-new', batches: [{ index: 0 }], totalObjects: 1 }))
    const data = new MapDataClient()
    await data.load(3, { bbox: [-1, -1, 1, 1], zoom: 15 }, ['cable_line', 'node'], [], { pin: { format: 'vector', tileVersion: 'release-a' }, datasetVersion: 'snapshot-old' })
    expect(data.getState().phase).toBe('error')
    expect(fetch).toHaveBeenCalledTimes(1); expect(data.getFeatures()).toHaveLength(0)
    data.destroy()
  })
  it('keeps measurement coordinates independent of selected cables', async () => {
    stubApi()
    const adapter = new RecordingMapAdapter(), controller = new MapController(options, new MapDataClient(), adapter)
    await controller.start()
    controller.setMode('measure'); adapter.fireMeasure([0.1, 0.2]); adapter.fireMeasure([0.2, 0.3])
    expect(controller.measurePoints).toEqual([[0.1, 0.2], [0.2, 0.3]])
    expect(controller.measureLengthKm()).toBeGreaterThan(10)
    controller.destroy()
  })
  it('keeps points enabled when the live catalog is empty after sync', async () => {
    const { fetch, preloads } = stubApi()
    fetch.mockImplementationOnce(async () => json({ datasetVersion: 'snapshot-new', objectTypes: [], layers: [] }))
    const controller = new MapController(options, new MapDataClient(), new RecordingMapAdapter())
    await controller.start()
    expect(controller.activeTypes).toEqual(['node', 'cable_line'])
    expect(preloads[0]).toMatchObject({ objectTypes: ['node'], pin: { tileVersion: 'release-a' } })
    expect(controller.catalog?.objectTypes.map(item => item.object_type)).toEqual(['node', 'cable_line'])
    controller.destroy()
  })
  it('discards details arriving after mode change even if fetch ignores abort', async () => {
    const { fetch } = stubApi()
    const adapter = new RecordingMapAdapter(), controller = new MapController(options, new MapDataClient(), adapter)
    await controller.start()
    let finish!: (response: Response) => void
    fetch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    adapter.fireSelect(cable)
    await controller.setCableMode('none')
    finish(json(details))
    await new Promise(resolve => setTimeout(resolve,0))
    expect(controller.selected).toBeUndefined(); expect(controller.details).toBeUndefined(); expect(adapter.selectedFeature).toBeUndefined()
    controller.destroy()
  })
  it('selects raster on startup, pins its dataset and sends theme filters only to points', async () => {
    const catalog=tileCatalog()
    catalog.formats.raster.active={...descriptor,tileVersion:'raster-a',datasetVersion:'raster-snapshot',tileSize:256,maxzoom:15,template:'https://tiles.test/raster/{z}/{x}/{y}.webp',template2x:'https://tiles.test/raster/{z}/{x}/{y}@2x.webp',capabilities:{layerFilter:false,hitTest:false,dash:false}}
    const {fetch}=stubApi(catalog)
    const original=fetch.getMockImplementation()!
    const bodies: Array<Record<string,unknown>>=[]
    fetch.mockImplementation(async(input,init)=>{
      if(String(input).endsWith('/objects/preload')) {
        const body=JSON.parse(String(init?.body));bodies.push(body)
        return json({queryId:'raster-query',datasetVersion:body.pin?.format==='raster'?'raster-snapshot':'snapshot-old',totalObjects:0,batches:[]})
      }
      return original(input,init)
    })
    const adapter=new RecordingMapAdapter(),controller=new MapController({...options,cableTiles:'raster'},new MapDataClient(),adapter)
    await controller.start()
    expect(controller.cableMode).toBe('raster');expect(adapter.tiles.at(-1)?.format).toBe('raster')
    expect(bodies[0]).toMatchObject({objectTypes:['node'],pin:{format:'raster',tileVersion:'raster-a'}})
    controller.setThematicLayers([11])
    await vi.waitFor(()=>expect(bodies.at(-1)).toMatchObject({layerIds:[11]}))
    await controller.setCableMode('vector')
    expect(bodies.at(-1)).toMatchObject({pin:{format:'vector',tileVersion:'release-a'}})
    expect(controller.loadState.datasetVersion).toBe('snapshot-old')
    controller.destroy()
  })
  it('preserves selection and point state when only a signature changes', async () => {
    const {fetch,preloads}=stubApi()
    const adapter=new RecordingMapAdapter(),controller=new MapController(options,new MapDataClient(),adapter)
    await controller.start();adapter.fireSelect(cable)
    await vi.waitFor(()=>expect(controller.details?.title).toBe('Full route'))
    const before=adapter.selectedFeature,loads=preloads.length
    fetch.mockImplementationOnce(async()=>json(tileCatalog({...descriptor,template:descriptor.template+'?new-signature'})))
    await controller.refreshTileCatalog()
    expect(adapter.selectedFeature).toBe(before);expect(preloads).toHaveLength(loads)
    expect(controller.selected?.id).toBe('cable_group:5')
    controller.destroy()
  })
  it('deduplicates 410 and pauses invalid-signature retries without falling back', async () => {
    const {fetch}=stubApi()
    const adapter=new RecordingMapAdapter(),controller=new MapController(options,new MapDataClient(),adapter)
    await controller.start()
    let finish!: (response:Response)=>void
    fetch.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}))
    const calls=fetch.mock.calls.length
    for(let i=0;i<100;i++)adapter.fireTileError({status:410,tileVersion:'release-a',message:'retired'})
    expect(fetch.mock.calls.length).toBe(calls+1)
    finish(json(tileCatalog()))
    await vi.waitFor(()=>expect(controller.tileError).toBeUndefined())
    adapter.fireTileError({status:403,code:'TILE_SIGNATURE_INVALID',tileVersion:'release-a',message:'invalid'})
    await new Promise(resolve=>setTimeout(resolve,10))
    expect(fetch.mock.calls.length).toBe(calls+1);expect(controller.cableMode).toBe('vector');expect(controller.tileError).toBe('invalid')
    controller.destroy()
  })
})
