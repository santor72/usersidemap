import type { ApiError, Catalog, Change, Feature, FeatureCollection, LoadState, ObjectDetails, ObjectType, Preload, TilePin, Viewport } from './types'
import type { Geometry } from 'geojson'

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))
const idOf = (f: Feature) => String(f.id ?? `${f.properties.sourceKind}:${f.properties.sourceId}`)

export class MapDataClient {
  private generation = 0
  private abort?: AbortController
  private queryId?: string
  private version?: string
  private lastLoad?: { mapId: number; viewport: Viewport; types: ObjectType[]; layers: number[]; pin?: TilePin; pointMinZoom?: number }
  private readonly objects = new Map<string, Feature>()
  private readonly objectVertices = new Map<string, number>()
  private cachedVertices = 0
  private readonly confirmed = new Set<string>()
  private readonly listeners = new Set<(change: Change) => void>()
  private state: LoadState = { phase: 'idle', loadedBatches: 0, totalBatches: 0, loadedObjects: 0, totalObjects: 0 }

  constructor(private readonly baseUrl = '/api/map', private readonly concurrency = 4, private readonly maxRetries = 2, private readonly maxCachedFeatures = 50000, private readonly maxCachedVertices = 2_000_000) {}

  subscribe(listener: (change: Change) => void): () => void { this.listeners.add(listener); return () => this.listeners.delete(listener) }
  private emit(change: Change) { for (const listener of this.listeners) listener(change) }
  getState(): LoadState { return { ...this.state } }
  getFeatures(): Feature[] { return [...this.objects.values()] }
  getFeature(id: string): Feature | undefined { return this.objects.get(id) }
  isConfirmed(feature: Feature): boolean { return this.confirmed.has(idOf(feature)) }
  private setState(state: LoadState) { this.state = state }

  async request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } })
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      const detail = body.error ?? { code: 'HTTP_ERROR', message: response.statusText }
      const err = new Error(detail.message) as Error & { api: ApiError }
      err.api = { code: detail.code, message: detail.message, status: response.status }
      throw err
    }
    return response.json() as Promise<T>
  }
  async catalog(mapId: number): Promise<Catalog> { return this.request(`/layers?mapId=${encodeURIComponent(mapId)}`) }

  async load(mapId: number, viewport: Viewport, types: ObjectType[], layers: number[], options: { preserve?: boolean; pin?: TilePin; datasetVersion?: string; pointMinZoom?: number } = {}): Promise<void> {
    if (options.pin) types = types.filter(type => type !== 'cable_line')
    const generation = ++this.generation
    this.abort?.abort()
    const controller = new AbortController()
    this.abort = controller
    this.confirmed.clear()
    const normalized = normalizeViewport(viewport)
    const previousLoad = this.lastLoad
    const sameContext = previousLoad?.mapId === mapId
      && previousLoad.types.join() === types.join()
      && previousLoad.layers.join() === layers.join()
      && previousLoad.pin?.format === options.pin?.format
      && previousLoad.pin?.tileVersion === options.pin?.tileVersion
      && previousLoad.pointMinZoom === options.pointMinZoom
    this.lastLoad = { mapId, viewport: normalized, types: [...types], layers: [...layers], pin: options.pin ? { ...options.pin } : undefined, pointMinZoom: options.pointMinZoom }
    if (options.preserve && sameContext && this.version) {
      for (const [id, feature] of this.objects) if (!intersects(feature.geometry, normalized.bbox)) { this.dropFeature(id); this.emit({ kind: 'remove', id, generation }) }
    } else {
      this.clearFeatures(); this.version = undefined; this.queryId = undefined
      this.emit({ kind: 'reset', generation })
    }
    this.setState({ phase: 'loading', loadedBatches: 0, totalBatches: 0, loadedObjects: 0, totalObjects: 0 })
    try {
      let preload: Preload | undefined
      for (let attempt = 0; ; attempt++) {
        try {
          preload = await this.request<Preload>('/objects/preload', { method: 'POST', body: JSON.stringify({ mapId, bbox: normalized.bbox, zoom: normalized.zoom, objectTypes: types, layerIds: layers, ...(options.pin ? { pin: options.pin } : {}), ...(options.pointMinZoom !== undefined ? { pointMinZoom: options.pointMinZoom } : {}) }), signal: controller.signal })
          break
        } catch (error) {
          const status = (error as Error & { api?: ApiError }).api?.status
          if (controller.signal.aborted || attempt >= this.maxRetries || (status && status < 500 && status !== 429)) throw error
          await sleep(200 * 2 ** attempt)
        }
      }
      if (!preload) return
      if (generation !== this.generation) return
      if (options.datasetVersion && preload.datasetVersion !== options.datasetVersion) throw new Error('Dataset preload не соответствует выпуску тайлов')
      this.queryId = preload.queryId
      const versionChanged = this.version !== undefined && this.version !== preload.datasetVersion
      if (versionChanged) { this.clearFeatures(); this.emit({ kind: 'reset', generation }) }
      this.version = preload.datasetVersion
      this.setState({ phase: preload.batches.length ? 'loading' : 'ready', loadedBatches: 0, totalBatches: preload.batches.length, loadedObjects: 0, totalObjects: preload.totalObjects, datasetVersion: preload.datasetVersion })
      const seen = new Set<string>()
      let next = 0, failed: unknown
      const worker = async () => {
        while (next < preload.batches.length && !failed && !controller.signal.aborted) {
          const index = next++
          let attempt = 0
          for (;;) {
            try {
              const result = await this.request<{ queryId: string; datasetVersion: string; batchIndex: number; featureCollection: FeatureCollection }>('/objects/batch', { method: 'POST', body: JSON.stringify({ queryId: preload.queryId, batchIndex: index }), signal: controller.signal })
              if (generation !== this.generation) return
              if (result.queryId !== preload.queryId || result.datasetVersion !== preload.datasetVersion || result.batchIndex !== index) throw new Error('Ответ batch не соответствует текущему запросу')
              for (const feature of result.featureCollection.features) {
                const id = idOf(feature), existing = this.objects.get(id), previous = Boolean(existing)
                // Published snapshots are immutable: preserve the canonical object/reference.
                const canonical = existing ?? feature
                const oldVertices = this.objectVertices.get(id) ?? 0
                const featureVertices = existing ? oldVertices : countVertices(feature.geometry)
                if ((!previous && this.objects.size >= this.maxCachedFeatures) || this.cachedVertices - oldVertices + featureVertices > this.maxCachedVertices) { failed = new Error('Превышен лимит объектов или вершин в памяти'); break }
                this.objects.set(id, canonical); this.objectVertices.set(id, featureVertices); this.cachedVertices += featureVertices - oldVertices; seen.add(id); this.confirmed.add(id)
                this.emit({ kind: previous ? 'update' : 'add', feature: canonical, generation })
              }
              if (failed) break
              const old = this.state
              this.setState({ ...old, phase: 'partial', loadedBatches: old.loadedBatches + 1, loadedObjects: old.loadedObjects + result.featureCollection.features.length })
              break
            } catch (error) {
              if (controller.signal.aborted || generation !== this.generation) return
              const status = (error as Error & { api?: ApiError }).api?.status
              if (attempt++ < this.maxRetries && (!status || status >= 500 || status === 429)) { await sleep(200 * 2 ** (attempt - 1)); continue }
              failed = error; break
            }
          }
        }
      }
      await Promise.all(Array.from({ length: Math.min(this.concurrency, preload.batches.length) }, worker))
      if (generation !== this.generation || controller.signal.aborted) return
      if (failed) { this.setState({ ...this.state, phase: 'error', error: String(failed), apiError: (failed as { api?: ApiError }).api }); return }
      for (const [id] of this.objects) if (!seen.has(id)) { this.dropFeature(id); this.emit({ kind: 'remove', id, generation }) }
      if (this.objects.size > this.maxCachedFeatures) {
        this.clearFeatures(); this.emit({ kind: 'reset', generation }); this.setState({ ...this.state, phase: 'error', error: 'Лимит памяти превышен; переместите карту для новой загрузки' }); return
      }
      this.setState({ ...this.state, phase: 'ready' })
    } catch (error) {
      if (generation === this.generation && !controller.signal.aborted) this.setState({ ...this.state, phase: 'error', error: String(error), apiError: (error as { api?: ApiError }).api })
    }
  }

  cancel() { ++this.generation; this.abort?.abort(); this.abort = undefined; this.confirmed.clear() }
  private dropFeature(id: string) { this.objects.delete(id); this.cachedVertices -= this.objectVertices.get(id) ?? 0; this.objectVertices.delete(id); this.confirmed.delete(id) }
  private clearFeatures() { this.objects.clear(); this.objectVertices.clear(); this.cachedVertices = 0; this.confirmed.clear() }
  async details(feature: Feature): Promise<ObjectDetails> {
    if (!this.queryId || !this.version || !this.confirmed.has(idOf(feature))) throw new Error('Объект не подтвержден текущим запросом')
    try { return await this.fetchDetails(feature, this.queryId) }
    catch (error) {
      const code = (error as Error & { api?: ApiError }).api?.code
      if (code !== 'QUERY_EXPIRED' && code !== 'QUERY_NOT_FOUND') throw error
      const previous = this.lastLoad
      if (!previous) throw error
      await this.load(previous.mapId, previous.viewport, previous.types, previous.layers, { preserve: true, pin: previous.pin })
      if (!this.queryId || !this.confirmed.has(idOf(feature))) throw new Error('Объект отсутствует в обновленной выборке')
      return this.fetchDetails(feature, this.queryId)
    }
  }
  private async fetchDetails(feature: Feature, queryId: string): Promise<ObjectDetails> {
    const version = this.version
    const result = await this.request<ObjectDetails & { queryId: string }>(`/objects/${encodeURIComponent(String(feature.properties.sourceKind))}/${encodeURIComponent(String(feature.properties.sourceId))}?queryId=${encodeURIComponent(queryId)}`)
    if (this.queryId !== queryId || this.version !== version || result.queryId !== queryId || result.datasetVersion !== version) throw new Error('Ответ деталей устарел относительно текущего снимка')
    return result
  }
  destroy() { this.cancel(); this.listeners.clear(); this.clearFeatures() }
}

export function normalizeViewport(input: Viewport): Viewport {
  const [w0, s0, e0, n0] = input.bbox
  const south = Math.max(-90, Math.min(90, Math.min(s0, n0)))
  const north = Math.max(-90, Math.min(90, Math.max(s0, n0)))
  const wrap = (x: number) => ((x + 180) % 360 + 360) % 360 - 180
  let west = wrap(w0), east = wrap(e0)
  if (Math.abs(e0 - w0) >= 360) { west = -180; east = 180 }
  return { bbox: [west, south, east, north], zoom: Math.max(0, Math.min(24, Math.floor(input.zoom))) }
}

function intersects(geometry: Geometry, bbox: Viewport['bbox']): boolean {
  const longitudes: number[] = [], latitudes: number[] = []
  const visit = (value: unknown): void => {
    if (!Array.isArray(value)) return
    if (value.length >= 2 && typeof value[0] === 'number' && typeof value[1] === 'number') { longitudes.push(value[0]); latitudes.push(value[1]); return }
    for (const child of value) visit(child)
  }
  if (geometry.type === 'GeometryCollection') for (const item of geometry.geometries) visitGeometry(item, visit)
  else visitGeometry(geometry, visit)
  if (!longitudes.length || !latitudes.length) return false
  const minX = Math.min(...longitudes), maxX = Math.max(...longitudes), minY = Math.min(...latitudes), maxY = Math.max(...latitudes)
  const [west, south, east, north] = bbox
  if (maxY < south || minY > north) return false
  return west <= east ? maxX >= west && minX <= east : (maxX >= west && minX <= 180) || (maxX >= -180 && minX <= east)
}

function visitGeometry(geometry: Geometry, visit: (coordinates: unknown) => void) {
  if (geometry.type === 'GeometryCollection') { for (const child of geometry.geometries) visitGeometry(child, visit); return }
  if ('coordinates' in geometry) visit(geometry.coordinates)
}

function countVertices(geometry: Geometry): number {
  let count = 0
  const visit = (value: unknown): void => {
    if (!Array.isArray(value)) return
    if (value.length >= 2 && typeof value[0] === 'number' && typeof value[1] === 'number') { count++; return }
    for (const child of value) visit(child)
  }
  visitGeometry(geometry, visit)
  return count
}
