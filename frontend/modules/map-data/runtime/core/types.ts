export type ObjectType = 'cable_line' | 'node' | 'coupler' | (string & {})
export type Position = [number, number]
export interface Viewport { bbox: [number, number, number, number]; zoom: number }
export interface Feature<P = Record<string, unknown>> { type: 'Feature'; id: string | number; geometry: Geometry; properties: P }
export interface FeatureCollection { type: 'FeatureCollection'; features: Feature[] }
export interface LayerInfo { source_id: number; title: string; default_visible: boolean; object_count: number; style?: Record<string, unknown> }
export interface Catalog { datasetVersion: string; objectTypes: Array<{ object_type: ObjectType; count: number; min_zoom: number }>; layers: LayerInfo[] }
export interface Preload { queryId: string; datasetVersion: string; totalObjects: number; totalVertices: number; typeCounts: Record<string, number>; batches: Array<{ index: number; objectCount: number; vertexCount: number }> }
export interface TilePin { format: 'vector' | 'raster'; tileVersion: string }
export interface ObjectDetails { sourceKind: string; sourceId: number; title: string | null; properties: Record<string, unknown>; length_m: number | null; cableEndpoints?: Array<string | null>; links: unknown[]; layers: unknown[]; datasetVersion: string }
export type Change = { kind: 'add' | 'update'; feature: Feature; generation: number } | { kind: 'remove'; id: string; generation: number } | { kind: 'reset'; generation: number }
export interface LoadState { phase: 'idle' | 'loading' | 'partial' | 'ready' | 'error'; loadedBatches: number; totalBatches: number; loadedObjects: number; totalObjects: number; error?: string; apiError?: ApiError; datasetVersion?: string }
export interface ApiError { code: string; message: string; status: number }
import type { Geometry } from 'geojson'
