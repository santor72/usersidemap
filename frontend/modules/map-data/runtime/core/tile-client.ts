import type { Geometry } from 'geojson'
import type { ApiError, Feature, ObjectDetails, TilePin } from './types'

export type CableMode = 'none' | 'vector' | 'raster'
export interface TileDescriptor {
  tileVersion: string
  datasetVersion: string
  minzoom: number
  maxzoom: number
  displayMaxzoom: number
  tileSize: number
  template: string
  template2x: string | null
  templateExpiresAt: string | null
  datasetAge: number
  tilesBehindActive: boolean
  capabilities: { layerFilter: boolean; hitTest: boolean; dash: boolean }
}
export interface TileCatalog {
  mapId: number
  activeDatasetVersion: string | null
  tiles_lag: 'banner'
  catalogPollSeconds: number
  layers: Array<{ id: number; title: string; defaultVisible: boolean }>
  formats: Record<'vector' | 'raster', { active: TileDescriptor | null; prev: TileDescriptor | null }>
}
export interface TileObject {
  tileVersion: string
  datasetVersion: string
  objectId: string
  source_kind: string
  source_id: number
  object_type: string
  title: string | null
  geometry: Geometry
  style: Record<string, unknown>
  properties: Record<string, unknown>
  layer_ids: number[]
  links: unknown[]
  length_m: number | null
  cableEndpoints?: ObjectDetails['cableEndpoints']
  renderAttributes: Record<string, unknown>
}

async function failed(response: Response): Promise<never> {
  const body = await response.json().catch(() => ({}))
  const detail = body.error ?? { code: 'HTTP_ERROR', message: response.statusText }
  const error = new Error(detail.message) as Error & { api: ApiError }
  error.api = { ...detail, status: response.status }
  throw error
}

/** Metadata/details travel through Nuxt. XYZ templates remain direct origin URLs. */
export class MapTileClient {
  private catalogs = new Map<number, { etag: string | null; value: TileCatalog }>()
  constructor(private readonly baseUrl = '/api/maps') {}
  async catalog(mapId: number, signal?: AbortSignal): Promise<TileCatalog> {
    const cached = this.catalogs.get(mapId)
    const response = await fetch(`${this.baseUrl}/${mapId}/tiles/catalog`, {
      signal, cache: 'no-cache', credentials: 'same-origin',
      headers: cached?.etag ? { 'If-None-Match': cached.etag } : {}
    })
    if (response.status === 304 && cached) return cached.value
    if (!response.ok) return failed(response)
    const value = await response.json() as TileCatalog
    if (value.mapId !== mapId) throw new Error('Каталог тайлов другой карты')
    this.catalogs.set(mapId, { etag: response.headers.get('ETag'), value })
    return value
  }
  async details(mapId: number, pin: TilePin, feature: Feature, signal?: AbortSignal): Promise<{ feature: Feature; details: ObjectDetails }> {
    const kind = encodeURIComponent(String(feature.properties.sourceKind))
    const id = encodeURIComponent(String(feature.properties.sourceId))
    const response = await fetch(`${this.baseUrl}/${mapId}/tiles/${encodeURIComponent(pin.tileVersion)}/objects/${kind}/${id}`, { signal, credentials: 'same-origin', cache: 'no-cache' })
    if (!response.ok) return failed(response)
    const item = await response.json() as TileObject
    if (item.tileVersion !== pin.tileVersion || item.objectId !== `${feature.properties.sourceKind}:${feature.properties.sourceId}`) throw new Error('Ответ details не соответствует выбранному объекту')
    return {
      feature: { type: 'Feature', id: item.objectId, geometry: item.geometry, properties: {
        ...item.renderAttributes, sourceKind: item.source_kind, sourceId: item.source_id,
        objectType: item.object_type, title: item.title, style: item.style,
        properties: item.properties, layerIds: item.layer_ids
      } },
      details: { sourceKind: item.source_kind, sourceId: item.source_id, title: item.title,
        properties: item.properties, links: item.links, layers: item.layer_ids,
        length_m: item.length_m, cableEndpoints: item.cableEndpoints, datasetVersion: item.datasetVersion }
    }
  }
}
