import { afterEach, describe, expect, it, vi } from 'vitest'
import { MapDataClient, normalizeViewport } from '../modules/map-data/runtime/core/client'
import type { Feature } from '../modules/map-data/runtime/core/types'
import { MapController } from '../modules/cartography/runtime/core/controller'
import type { ToolMode } from '../modules/cartography/runtime/core/contracts'
import { RecordingMapAdapter } from './support/RecordingMapAdapter'
import { prepareRenderFeature } from '../modules/cartography/runtime/adapters/maplibre/feature-style'
import { legacyImageSizeAtMapLibreZoom, legacyNodeRadiusAtMapLibreZoom } from '../modules/cartography/runtime/adapters/maplibre/legacy-point-sizes'

const viewport = { bbox: [-1, -1, 1, 1] as [number, number, number, number], zoom: 15 }
const feature = (id: number): Feature => ({ type: 'Feature', id: `node:${id}`, geometry: { type: 'Point', coordinates: [0, 0] }, properties: { sourceKind: 'node', sourceId: id, objectType: 'node' } })
const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })
const preload = (queryId: string, batches: number[]) => ({ queryId, datasetVersion: 'snapshot-a', totalObjects: batches.reduce((a, b) => a + b, 0), totalVertices: 0, typeCounts: { node: batches.reduce((a, b) => a + b, 0) }, batches: batches.map((objectCount, index) => ({ index, objectCount, vertexCount: objectCount })) })

afterEach(() => vi.unstubAllGlobals())

describe('MapLibre feature styles', () => {
  it('matches legacy point sizes at the same geographic scale', () => {
    expect(legacyNodeRadiusAtMapLibreZoom(14)).toBe(2) // Leaflet zoom 15
    expect(legacyNodeRadiusAtMapLibreZoom(15)).toBe(4) // Leaflet zoom 16
    expect(legacyImageSizeAtMapLibreZoom(14)).toBe(6) // Leaflet zoom 15
    expect(legacyImageSizeAtMapLibreZoom(15)).toBe(10) // Leaflet zoom 16
  })

  it('preserves backend paint fields, z-order and a fallback for unshipped icons', () => {
    const source = { ...feature(5), properties: { ...feature(5).properties, style: { shape: 'image', color: '#123456', width: 4, dashArray: '20,15', strokeColor: '#000000', strokeWidth: 2, zIndex: 7 } } }
    const rendered = prepareRenderFeature(source)
    expect(rendered.properties).toMatchObject({ __mapId: 'node:5', __mapColor: '#123456', __mapWidth: 4, __mapDashArray: '20,15', __mapShape: 'imageFallback', __mapStrokeColor: '#000000', __mapStrokeWidth: 2, __mapZIndex: 7, __mapImageFallback: true })
    expect(rendered.properties.style).toEqual(source.properties.style)
  })

  it('reconstructs legacy triangle node polygons at geographic size', () => {
    const source: Feature = { ...feature(6), geometry: { type: 'Point', coordinates: [37.6, 55.7] }, properties: { ...feature(6).properties, style: { shape: 'triangle', color: '#008000', strokeColor: '#000000', strokeWidth: 1 } } }
    const rendered = prepareRenderFeature(source)
    expect(rendered.geometry).toEqual({ type: 'Polygon', coordinates: [[[37.6, 55.70005], [37.5999, 55.69995], [37.6001, 55.69995], [37.6, 55.70005]]] })
    expect(rendered.properties).toMatchObject({ __mapShape: 'triangle', __mapImageFallback: false })
  })
})

describe('MapDataClient', () => {
  it('reloads at the current map zoom when the point icon threshold changes', async () => {
    const requests: Array<{ zoom: number; pointMinZoom: number }> = []
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      if (String(input).includes('/layers?')) return json({ datasetVersion: 'snapshot-a', objectTypes: [{ object_type: 'node', count: 1, min_zoom: 15 }], layers: [] })
      requests.push(JSON.parse(String(init?.body)))
      return json(preload('points', []))
    }))
    const adapter = new RecordingMapAdapter()
    adapter.getViewport = () => ({ ...viewport, zoom: 12 })
    const controller = new MapController({ mapId: 3, initialView: { longitude: 0, latitude: 0, zoom: 12 }, objectTypes: ['node'], tools: ['select'] }, new MapDataClient(), adapter)
    await controller.start()
    expect(requests.at(-1)).toMatchObject({ zoom: 12, pointMinZoom: 15 })
    controller.setPointSymbols({ baseSize: 24, labelMinZoom: 16, iconMinZoom: 10 })
    await vi.waitFor(() => expect(requests.at(-1)).toMatchObject({ zoom: 12, pointMinZoom: 10 }))
    controller.setPointSymbols({ baseSize: 24, labelMinZoom: 16, iconMinZoom: 18 })
    await vi.waitFor(() => expect(requests.at(-1)).toMatchObject({ zoom: 12, pointMinZoom: 18 }))
    controller.destroy()
  })

  it('limits concurrent batch requests and reports each completed batch once', async () => {
    let active = 0, peak = 0
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/objects/preload')) return json(preload('query', [1, 1, 1]))
      active++; peak = Math.max(active, peak)
      await new Promise(resolve => setTimeout(resolve, 10))
      active--
      const index = Number(JSON.parse(String(init?.body)).batchIndex)
      return json({ queryId: 'query', datasetVersion: 'snapshot-a', batchIndex: index, objectCount: 1, featureCollection: { type: 'FeatureCollection', features: [feature(index + 1)] } })
    }))
    const client = new MapDataClient('/api/map', 2, 0)
    await client.load(3, viewport, ['node'], [])
    expect(peak).toBe(2)
    expect(client.getState()).toMatchObject({ phase: 'ready', loadedBatches: 3, loadedObjects: 3, totalObjects: 3 })
    expect(client.getFeatures()).toHaveLength(3)
    client.destroy()
  })

  it('ignores a delayed preload from an invalidated generation', async () => {
    let preloads = 0, batches = 0
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      if (url.endsWith('/objects/preload')) {
        const call = ++preloads
        if (call === 1) await new Promise(resolve => setTimeout(resolve, 30))
        return json(call === 1 ? preload('old', [1]) : preload('new', []))
      }
      batches++
      return json({})
    }))
    const client = new MapDataClient()
    const old = client.load(3, viewport, ['node'], [])
    await client.load(3, viewport, ['node'], [])
    await old
    expect(client.getState().phase).toBe('ready')
    expect(batches).toBe(0)
    expect(client.getFeatures()).toHaveLength(0)
    client.destroy()
  })

  it('normalizes fractional zoom and longitude across the antimeridian', () => {
    expect(normalizeViewport({ bbox: [190, -95, 200, 95], zoom: 15.9 })).toEqual({ bbox: [-170, -90, -160, 90], zoom: 15 })
    expect(normalizeViewport({ bbox: [-400, -10, 400, 10], zoom: 30 })).toEqual({ bbox: [-180, -10, 180, 10], zoom: 24 })
  })

  it('does not queue a second initial load for a viewport event before start completes', async () => {
    let preloads = 0
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      if (url.includes('/layers?')) return json({ datasetVersion: 'snapshot-a', objectTypes: [{ object_type: 'node', count: 1, min_zoom: 15 }], layers: [] })
      if (url.endsWith('/objects/preload')) return json(preload(`initial-${++preloads}`, []))
      return json({})
    }))
    const adapter = new RecordingMapAdapter()
    const controller = new MapController({ mapId: 3, initialView: { longitude: 0, latitude: 0, zoom: 15 }, objectTypes: ['node'], tools: ['select'] }, new MapDataClient(), adapter)
    const start = controller.start()
    adapter.fireViewport(viewport, false)
    await start
    await new Promise(resolve => setTimeout(resolve, 220))
    expect(preloads).toBe(1)
    controller.destroy()
  })

  it('connects a controller to independent non-WebGL adapters', async () => {
    const requestedTypes: string[][] = []
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => String(input).includes('/layers?')
      ? json({ datasetVersion: 'snapshot-a', objectTypes: [{ object_type: 'node', count: 1, min_zoom: 15 }, { object_type: 'coupler', count: 1, min_zoom: 15 }], layers: [{ source_id: 1, title: 'A', default_visible: true, object_count: 0 }, { source_id: 2, title: 'B', default_visible: false, object_count: 0 }] })
      : (requestedTypes.push(JSON.parse(String(init?.body)).objectTypes), json(preload('empty', [])))))
    const firstAdapter = new RecordingMapAdapter(), secondAdapter = new RecordingMapAdapter()
    const options = { mapId: 3, initialView: { longitude: 0, latitude: 0, zoom: 15 }, objectTypes: ['node', 'coupler'], tools: ['select', 'measure'] as ToolMode[] }
    const first = new MapController(options, new MapDataClient(), firstAdapter)
    const second = new MapController(options, new MapDataClient(), secondAdapter)
    await Promise.all([first.start(), second.start()])
    expect(first.activeLayerIds).toEqual([])
    first.setThematicLayers([1])
    expect(first.activeLayerIds).toEqual([1])
    first.setThematicLayers([1, 2])
    expect(first.activeLayerIds).toEqual([])
    first.setObjectType('node', false)
    first.setObjectType('coupler', false)
    expect(first.activeTypes).toEqual([])
    expect(requestedTypes.at(-1)).toEqual([])
    expect(second.activeTypes).toEqual(['node', 'coupler'])
    first.setMode('measure')
    firstAdapter.fireMeasure([0, 0]); firstAdapter.fireMeasure([0, 1])
    expect(first.measureLengthKm()).toBeGreaterThan(110)
    first.finishMeasure()
    first.setMode('select')
    expect(first.measurePoints).toHaveLength(0)
    expect(firstAdapter.measure).toHaveLength(0)
    expect(firstAdapter.changes.some(change => change.kind === 'reset')).toBe(true)
    first.destroy(); second.destroy()
    expect(firstAdapter.destroyed).toBe(true)
    expect(secondAdapter.destroyed).toBe(true)
  })

  it('recovers an expired details lease once and confirms the object in the replacement query', async () => {
    let queryCount = 0, detailsCount = 0
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/objects/preload')) return json(preload(`query-${++queryCount}`, [1]))
      if (url.endsWith('/objects/batch')) {
        const body = JSON.parse(String(init?.body))
        return json({ queryId: body.queryId, datasetVersion: 'snapshot-a', batchIndex: 0, objectCount: 1, featureCollection: { type: 'FeatureCollection', features: [feature(41)] } })
      }
      detailsCount++
      if (detailsCount === 1) return new Response(JSON.stringify({ error: { code: 'QUERY_EXPIRED', message: 'expired' } }), { status: 410 })
      return json({ queryId: 'query-2', datasetVersion: 'snapshot-a', sourceKind: 'node', sourceId: 41, title: 'Node', properties: {}, links: [], layers: [], length_m: null })
    }))
    const client = new MapDataClient('/api/map', 2, 0)
    await client.load(3, viewport, ['node'], [])
    const details = await client.details(feature(41))
    expect(queryCount).toBe(2)
    expect(detailsCount).toBe(2)
    expect(details.title).toBe('Node')
    client.destroy()
  })

  it('keeps a candidate when an overlapping refresh fails instead of declaring a complete result', async () => {
    let query = 0, fail = false
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/objects/preload')) return json(preload(`query-${++query}`, [1]))
      if (url.endsWith('/objects/batch')) {
        if (fail) return new Response(JSON.stringify({ error: { code: 'DATABASE_UNAVAILABLE', message: 'unavailable' } }), { status: 503 })
        const body = JSON.parse(String(init?.body))
        return json({ queryId: body.queryId, datasetVersion: 'snapshot-a', batchIndex: 0, objectCount: 1, featureCollection: { type: 'FeatureCollection', features: [feature(9)] } })
      }
      return json({})
    }))
    const client = new MapDataClient('/api/map', 1, 0)
    const changes: string[] = []
    client.subscribe(change => changes.push(change.kind))
    await client.load(3, viewport, ['node'], [])
    fail = true
    await client.load(3, viewport, ['node'], [], { preserve: true })
    expect(client.getState().phase).toBe('error')
    expect(client.getFeatures()).toHaveLength(1)
    expect(changes).not.toContain('remove')
    client.destroy()
  })

  it('retries a transient batch once without double-counting progress', async () => {
    let tries = 0
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/objects/preload')) return json(preload('retry-query', [1]))
      if (url.endsWith('/objects/batch')) {
        tries++
        if (tries === 1) return new Response(JSON.stringify({ error: { code: 'DATABASE_UNAVAILABLE', message: 'temporary' } }), { status: 503 })
        const body = JSON.parse(String(init?.body))
        return json({ queryId: body.queryId, datasetVersion: 'snapshot-a', batchIndex: 0, objectCount: 1, featureCollection: { type: 'FeatureCollection', features: [feature(21)] } })
      }
      return json({})
    }))
    const client = new MapDataClient('/api/map', 1, 1)
    await client.load(3, viewport, ['node'], [])
    expect(tries).toBe(2)
    expect(client.getState()).toMatchObject({ phase: 'ready', loadedBatches: 1, loadedObjects: 1 })
    client.destroy()
  })

  it('stops accepting an oversized cache with an explicit error state', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/objects/preload')) return json(preload('large-query', [2]))
      const body = JSON.parse(String(init?.body))
      return json({ queryId: body.queryId, datasetVersion: 'snapshot-a', batchIndex: 0, objectCount: 2, featureCollection: { type: 'FeatureCollection', features: [feature(31), feature(32)] } })
    }))
    const client = new MapDataClient('/api/map', 1, 0, 100, 1)
    await client.load(3, viewport, ['node'], [])
    expect(client.getState().phase).toBe('error')
    expect(client.getState().error).toContain('вершин')
    expect(client.getFeatures()).toHaveLength(1)
    client.destroy()
  })

  it('clears candidates after a successful empty viewport refresh', async () => {
    let calls = 0
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/objects/preload')) return json(++calls === 1 ? preload('first', [1]) : preload('empty', []))
      const body = JSON.parse(String(init?.body))
      return json({ queryId: body.queryId, datasetVersion: 'snapshot-a', batchIndex: 0, objectCount: 1, featureCollection: { type: 'FeatureCollection', features: [feature(71)] } })
    }))
    const client = new MapDataClient('/api/map', 1, 0)
    const changes: string[] = []
    client.subscribe(change => changes.push(change.kind === 'remove' ? `remove:${change.id}` : change.kind))
    await client.load(3, viewport, ['node'], [])
    expect(client.getFeatures()).toHaveLength(1)
    await client.load(3, viewport, ['node'], [], { preserve: true })
    expect(client.getFeatures()).toHaveLength(0)
    expect(changes).toContain('remove:node:71')
    expect(client.getState().phase).toBe('ready')
    client.destroy()
  })

  it('retains old candidates across API zoom changes until the new selection completes', async () => {
    let preloads = 0, releaseBatch!: () => void, enteredBatch!: () => void
    const batchEntered = new Promise<void>(resolve => { enteredBatch = resolve })
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/objects/preload')) return json(preload(++preloads === 1 ? 'zoom-15' : 'zoom-16', [1]))
      const body = JSON.parse(String(init?.body))
      if (body.queryId === 'zoom-16') {
        enteredBatch()
        await new Promise<void>(resolve => { releaseBatch = resolve })
      }
      return json({ queryId: body.queryId, datasetVersion: 'snapshot-a', batchIndex: 0, objectCount: 1, featureCollection: { type: 'FeatureCollection', features: [feature(body.queryId === 'zoom-15' ? 74 : 75)] } })
    }))
    const client = new MapDataClient('/api/map', 1, 0)
    const changes: string[] = []
    client.subscribe(change => changes.push(change.kind))
    await client.load(3, viewport, ['node'], [])
    expect(client.getFeatures().map(item => item.id)).toEqual(['node:74'])
    const next = client.load(3, { ...viewport, zoom: 16 }, ['node'], [], { preserve: true })
    await batchEntered
    expect(client.getFeatures().map(item => item.id)).toEqual(['node:74'])
    expect(changes.filter(kind => kind === 'reset')).toHaveLength(1)
    releaseBatch()
    await next
    expect(client.getFeatures().map(item => item.id)).toEqual(['node:75'])
    client.destroy()
  })

  it('clears the previous map objects when switching mapId', async () => {
    let query = 0
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/objects/preload')) return json(preload(`map-${++query}`, [1]))
      const body = JSON.parse(String(init?.body))
      return json({ queryId: body.queryId, datasetVersion: 'snapshot-a', batchIndex: 0, objectCount: 1, featureCollection: { type: 'FeatureCollection', features: [feature(body.queryId === 'map-1' ? 81 : 82)] } })
    }))
    const client = new MapDataClient('/api/map', 1, 0)
    const changes: string[] = []
    client.subscribe(change => changes.push(change.kind))
    await client.load(3, viewport, ['node'], [])
    expect(client.getFeatures().map(item => item.id)).toEqual(['node:81'])
    await client.load(4, viewport, ['node'], [])
    expect(changes).toContain('reset')
    expect(client.getFeatures().map(item => item.id)).toEqual(['node:82'])
    expect(client.getState()).toMatchObject({ phase: 'ready', datasetVersion: 'snapshot-a', loadedObjects: 1 })
    client.destroy()
  })

  it('replaces the previous dataset before accepting the first batch of a new version', async () => {
    let preloads = 0, releaseBatch!: () => void, enteredBatch!: () => void
    const batchEntered = new Promise<void>(resolve => { enteredBatch = resolve })
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/objects/preload')) {
        const call = ++preloads
        return json({ ...preload(call === 1 ? 'old' : 'new', [1]), datasetVersion: call === 1 ? 'snapshot-a' : 'snapshot-b' })
      }
      const body = JSON.parse(String(init?.body))
      if (body.queryId === 'new') {
        enteredBatch()
        await new Promise<void>(resolve => { releaseBatch = resolve })
      }
      return json({ queryId: body.queryId, datasetVersion: body.queryId === 'old' ? 'snapshot-a' : 'snapshot-b', batchIndex: 0, objectCount: 1, featureCollection: { type: 'FeatureCollection', features: [feature(body.queryId === 'old' ? 72 : 73)] } })
    }))
    const client = new MapDataClient('/api/map', 1, 0)
    await client.load(3, viewport, ['node'], [])
    expect(client.getFeatures().map(item => item.id)).toEqual(['node:72'])
    const next = client.load(3, viewport, ['node'], [])
    await batchEntered
    expect(client.getFeatures()).toHaveLength(0)
    releaseBatch()
    await next
    expect(client.getFeatures().map(item => item.id)).toEqual(['node:73'])
    expect(client.getState().datasetVersion).toBe('snapshot-b')
    client.destroy()
  })

  it('reloads selected details after the object is confirmed in a moved viewport', async () => {
    let query = 0
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/layers?')) return json({ datasetVersion: 'snapshot-a', objectTypes: [{ object_type: 'node', count: 1, min_zoom: 15 }], layers: [] })
      if (url.endsWith('/objects/preload')) return json(preload(`move-${++query}`, [1]))
      if (url.endsWith('/objects/batch')) {
        const body = JSON.parse(String(init?.body))
        return json({ queryId: body.queryId, datasetVersion: 'snapshot-a', batchIndex: 0, objectCount: 1, featureCollection: { type: 'FeatureCollection', features: [feature(51)] } })
      }
      const detailQuery = new URL(url, 'http://localhost').searchParams.get('queryId')
      return json({ queryId: detailQuery, datasetVersion: 'snapshot-a', sourceKind: 'node', sourceId: 51, title: detailQuery, properties: {}, links: [], layers: [], length_m: null })
    }))
    const adapter = new RecordingMapAdapter()
    const controller = new MapController({ mapId: 3, initialView: { longitude: 0, latitude: 0, zoom: 15 }, objectTypes: ['node'], tools: ['select', 'measure'] as ToolMode[] }, new MapDataClient(), adapter)
    await controller.start()
    adapter.fireSelect(feature(51))
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(controller.details?.title).toBe('move-1')
    adapter.fireViewport(viewport, true)
    expect(controller.waitingForConfirmation).toBe(true)
    adapter.fireViewport(viewport, false)
    await new Promise(resolve => setTimeout(resolve, 230))
    expect(controller.details?.title).toBe('move-2')
    expect(controller.waitingForConfirmation).toBe(false)
    controller.destroy()
  })

  it('does not let a slow previous selection replace the current details', async () => {
    const releaseDetails = new Map<number, (response: Response) => void>()
    vi.stubGlobal('fetch', vi.fn((input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/layers?')) return Promise.resolve(json({ datasetVersion: 'snapshot-a', objectTypes: [{ object_type: 'node', count: 2, min_zoom: 15 }], layers: [] }))
      if (url.endsWith('/objects/preload')) return Promise.resolve(json(preload('selection', [2])))
      if (url.endsWith('/objects/batch')) {
        const body = JSON.parse(String(init?.body))
        return Promise.resolve(json({ queryId: body.queryId, datasetVersion: 'snapshot-a', batchIndex: 0, objectCount: 2, featureCollection: { type: 'FeatureCollection', features: [feature(61), feature(62)] } }))
      }
      const id = Number(new URL(url, 'http://localhost').pathname.split('/').at(-1))
      return new Promise<Response>(resolve => releaseDetails.set(id, resolve))
    }))
    const adapter = new RecordingMapAdapter()
    const controller = new MapController({ mapId: 3, initialView: { longitude: 0, latitude: 0, zoom: 15 }, objectTypes: ['node'], tools: ['select', 'measure'] as ToolMode[] }, new MapDataClient(), adapter)
    await controller.start()
    adapter.fireSelect(feature(61))
    adapter.fireSelect(feature(62))
    releaseDetails.get(62)?.(json({ queryId: 'selection', datasetVersion: 'snapshot-a', sourceKind: 'node', sourceId: 62, title: 'current', properties: {}, links: [], layers: [], length_m: null }))
    await new Promise(resolve => setTimeout(resolve, 0))
    releaseDetails.get(61)?.(json({ queryId: 'selection', datasetVersion: 'snapshot-a', sourceKind: 'node', sourceId: 61, title: 'stale', properties: {}, links: [], layers: [], length_m: null }))
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(controller.selected?.id).toBe('node:62')
    expect(controller.details?.title).toBe('current')
    controller.destroy()
  })
})

describe('complete cable groups', () => {
  const group: Feature = {
    type: 'Feature', id: 'cable_group:50',
    geometry: { type: 'MultiLineString', coordinates: [[[-0.5, 0], [0.5, 0]], [[10, 10], [11, 10]]] },
    properties: { sourceKind: 'cable_group', sourceId: 50, objectType: 'cable_line', style: { color: '#000080', width: 5 } }
  }
  const stubApi = () => vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
    const url = String(input)
    if (url.includes('/layers?')) return json({ datasetVersion: 'snapshot-a', objectTypes: [{ object_type: 'cable_line', count: 1, min_zoom: 1 }], layers: [] })
    if (url.endsWith('/objects/preload')) return json(preload('group-query', [1]))
    if (url.endsWith('/objects/batch')) return json({ queryId: 'group-query', datasetVersion: 'snapshot-a', batchIndex: 0, featureCollection: { type: 'FeatureCollection', features: [group] } })
    return json({ queryId: 'group-query', sourceKind: 'cable_group', sourceId: 50, properties: { memberIds: [10, 11] }, links: [], layers: [], datasetVersion: 'snapshot-a', title: 'route', length_m: 10 })
  }))

  it('retains offscreen pieces and selects the whole group from a rendered fragment', async () => {
    stubApi()
    const client = new MapDataClient('/api/map', 1, 0)
    const adapter = new RecordingMapAdapter()
    const controller = new MapController({ mapId: 3, objectTypes: ['cable_line'], tools: ['select'], initialView: { longitude: 0, latitude: 0, zoom: 15 }, basemap: { tiles: 'tiles', attribution: '' } }, client, adapter)
    await controller.start()
    expect(client.getFeatures()).toEqual([group])
    expect(prepareRenderFeature(group).geometry).toEqual(group.geometry)
    const fragment: Feature = { ...group, geometry: { type: 'LineString', coordinates: [[-0.5, 0], [0.5, 0]] } }
    adapter.fireSelect(fragment)
    await vi.waitFor(() => expect(controller.details).toBeDefined())
    expect(controller.selected?.geometry).toEqual(group.geometry)
    expect(adapter.selected).toBe('cable_group:50')
    expect(controller.details).toMatchObject({ properties: { memberIds: [10, 11] } })
    controller.destroy()
  })

  it('counts vertices across all pieces for the memory limit', async () => {
    stubApi()
    const client = new MapDataClient('/api/map', 1, 0, 10, 3)
    await client.load(3, viewport, ['cable_line'], [])
    expect(client.getState().phase).toBe('error')
    expect(client.getFeatures()).toHaveLength(0)
    client.destroy()
  })
})


describe('stable viewport refresh', () => {
  it('confirms the same immutable cable without changing its canonical reference', async () => {
    let sequence = 0
    const cable: Feature = { ...feature(50), id: 'cable_group:50',
      geometry: { type: 'MultiLineString', coordinates: [[[-0.5,0],[0.5,0]], [[10,10],[11,10]]] },
      properties: { sourceKind: 'cable_group', sourceId: 50, objectType: 'cable_line' } }
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      if (String(input).endsWith('/objects/preload')) return json(preload(`refresh-${++sequence}`, [1]))
      const body = JSON.parse(String(init?.body))
      return json({ queryId: body.queryId, datasetVersion: 'snapshot-a', batchIndex: 0, featureCollection: { type: 'FeatureCollection', features: [cable] } })
    }))
    const client = new MapDataClient('/api/map',1,0)
    await client.load(3,viewport,['cable_line'],[])
    const before = client.getFeature('cable_group:50')!
    const changes: string[] = []
    client.subscribe(change => changes.push(change.kind))
    await client.load(3,{ ...viewport, zoom: 16 },['cable_line'],[],{ preserve: true })
    expect(client.getFeature('cable_group:50')).toBe(before)
    expect(client.isConfirmed(before)).toBe(true)
    expect(changes).toEqual(['update'])
    expect(before.geometry).toEqual(cable.geometry)
    client.destroy()
  })
})
