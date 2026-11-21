import type { Change, Feature, ObjectType, Viewport } from '#map-data'
import type { MapAdapter, ToolMode } from '../core/contracts'

/**
 * Copy this skeleton when adding a rendering engine. Keep its constructor and
 * private state engine-specific; implement the stable MapAdapter boundary.
 */
export abstract class MapAdapterTemplate implements MapAdapter {
  abstract onViewport(listener: (viewport: Viewport, moving: boolean) => void): () => void
  abstract onSelect(listener: (feature?: Feature) => void): () => void
  abstract onMeasurePoint(listener: (point: [number, number]) => void): () => void
  abstract onBasemapError(listener: (message: string) => void): () => void
  abstract ready(): Promise<void>
  abstract getViewport(): Viewport
  abstract apply(change: Change): void
  abstract setVisibility(types: ObjectType[]): void
  abstract setSelected(id?: string): void
  abstract setMode(mode: ToolMode): void
  abstract setMeasure(points: [number, number][], finished: boolean): void
  abstract zoomIn(): void
  abstract zoomOut(): void
  abstract resize(): void
  abstract destroy(): void
}
