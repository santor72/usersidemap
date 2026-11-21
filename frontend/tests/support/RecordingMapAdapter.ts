import type { Change, Feature, ObjectType, Viewport } from '../../modules/map-data/runtime/core/types'
import type { MapAdapter, ToolMode } from '../../modules/cartography/runtime/core/contracts'

export class RecordingMapAdapter implements MapAdapter {
  readonly tiles: Array<{ descriptor?: import('../../modules/map-data/runtime/core/tile-client').TileDescriptor; layerIds: number[]; format?: 'vector' | 'raster' }> = []
  selectedFeature?: Feature
  setCableTiles(descriptor?: import('../../modules/map-data/runtime/core/tile-client').TileDescriptor, layerIds: number[] = [], format?: 'vector' | 'raster') { this.tiles.push({ descriptor, layerIds: [...layerIds], format }) }
  setSelectedFeature(feature?: Feature) { this.selectedFeature = feature }
  private tileErrorListeners = new Set<(error: { status?: number; code?: string; tileVersion?: string; message: string }) => void>()
  onCableError(listener: (error: { status?: number; code?: string; tileVersion?: string; message: string }) => void) { this.tileErrorListeners.add(listener); return () => this.tileErrorListeners.delete(listener) }
  fireTileError(error: { status?: number; code?: string; tileVersion?: string; message: string }) { this.tileErrorListeners.forEach(fn => fn(error)) }
  readonly changes: Change[] = []
  readonly visibility: ObjectType[][] = []
  mode: ToolMode = 'none'
  selected?: string
  measure: [number, number][] = []
  destroyed = false
  private viewportListeners = new Set<(viewport: Viewport, moving: boolean) => void>()
  private selectListeners = new Set<(feature?: Feature) => void>()
  private pointListeners = new Set<(point: [number, number]) => void>()
  private errorListeners = new Set<(message: string) => void>()
  onViewport(listener: (viewport: Viewport, moving: boolean) => void) { this.viewportListeners.add(listener); return () => this.viewportListeners.delete(listener) }
  onSelect(listener: (feature?: Feature) => void) { this.selectListeners.add(listener); return () => this.selectListeners.delete(listener) }
  onMeasurePoint(listener: (point: [number, number]) => void) { this.pointListeners.add(listener); return () => this.pointListeners.delete(listener) }
  onBasemapError(listener: (message: string) => void) { this.errorListeners.add(listener); return () => this.errorListeners.delete(listener) }
  ready() { return Promise.resolve() }
  getViewport(): Viewport { return { bbox: [-1, -1, 1, 1], zoom: 15 } }
  apply(change: Change) { this.changes.push(change) }
  setVisibility(types: ObjectType[]) { this.visibility.push([...types]) }
  setSelected(id?: string) { this.selected = id }
  setMode(mode: ToolMode) { this.mode = mode }
  setMeasure(points: [number, number][]) { this.measure = [...points] }
  zoomIn() {}
  zoomOut() {}
  resize() {}
  fireViewport(viewport: Viewport, moving: boolean) { this.viewportListeners.forEach(listener => listener(viewport, moving)) }
  fireSelect(feature?: Feature) { this.selectListeners.forEach(listener => listener(feature)) }
  fireMeasure(point: [number, number]) { this.pointListeners.forEach(listener => listener(point)) }
  fireBasemapError(message: string) { this.errorListeners.forEach(listener => listener(message)) }
  destroy() { this.destroyed = true; this.viewportListeners.clear(); this.selectListeners.clear(); this.pointListeners.clear(); this.errorListeners.clear() }
}
