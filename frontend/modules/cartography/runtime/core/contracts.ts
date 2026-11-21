import type { CableMode, TileDescriptor } from '#map-data'
import type { Change, Feature, FeatureCollection, ObjectType, Viewport } from '#map-data'

export type ToolMode = 'select' | 'measure' | 'none'
export interface PointSymbolSettings {
  /** Icon width in CSS pixels at zoom 16. */
  baseSize: number
  iconMinZoom: number
  labelMinZoom: number
}
export interface MapAdapterOptions {
  center: [number, number]
  zoom: number
  tiles: string
  attribution: string
  objectTypes: ObjectType[]
  pointSymbols?: PointSymbolSettings
}
export type MapAdapterFactory = (container: HTMLElement, options: MapAdapterOptions) => Promise<MapAdapter>
export interface MapAdapter {
  onViewport(listener: (viewport: Viewport, moving: boolean) => void): () => void
  onSelect(listener: (feature?: Feature) => void): () => void
  onMeasurePoint(listener: (point: [number, number]) => void): () => void
  onBasemapError(listener: (message: string) => void): () => void
  ready(): Promise<void>
  getViewport(): Viewport
  apply(change: Change): void
  setVisibility(types: ObjectType[]): void
  setPointSymbols?(settings: PointSymbolSettings): void
  setSelected(id?: string): void
  setSelectedFeature?(feature?: Feature): void
  setCableTiles?(descriptor?: TileDescriptor, layerIds?: number[], format?: Exclude<CableMode, 'none'>): void
  retryCableTiles?(): void
  onCableError?(listener: (error: { status?: number; code?: string; tileVersion?: string; message: string }) => void): () => void
  setMode(mode: ToolMode): void
  setMeasure(points: [number, number][], finished: boolean): void
  zoomIn(): void
  zoomOut(): void
  resize(): void
  destroy(): void
}
export interface MapControllerOptions {
  cableTiles?: CableMode
  mapId: number
  initialView: { longitude: number; latitude: number; zoom: number }
  objectTypes: ObjectType[]
  pointSymbols?: PointSymbolSettings
  tools: ToolMode[]
  initialState?: { objectTypes?: ObjectType[]; thematicLayerIds?: number[]; mode?: ToolMode }
  basemap?: { tiles: string; attribution: string }
}
export type { Feature, FeatureCollection, Viewport, ObjectType }
