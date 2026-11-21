import * as maplibregl from 'maplibre-gl'
import type { ExpressionSpecification, GeoJSONSource, GeoJSONSourceDiff, VectorTileSource, RasterTileSource, Map as MlMap } from 'maplibre-gl'
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'
import type { Change, Feature, FeatureCollection, ObjectType, TileDescriptor, Viewport } from '#map-data'
import type { MapAdapter, MapAdapterOptions, PointSymbolSettings, ToolMode } from '../../core/contracts'
import { prepareRenderFeature } from './feature-style'
import { cableLayerIds, cableLayers, cableTileLayers } from './cable-style'
import { legacyImageSizeExpression, legacyNodeRadiusExpression } from './legacy-point-sizes'

import { pointIcons, normalizePointSymbols, pointSizeExpression, preparePointSymbol, rasterizeIcon } from './point-symbols'
import latinFont from '@fontsource/noto-sans/files/noto-sans-latin-400-normal.woff2?url'
import cyrillicFont from '@fontsource/noto-sans/files/noto-sans-cyrillic-400-normal.woff2?url'

type DiagnosticWindow = Window & { __usersideMapLibreDiagnostic?: MlMap }

maplibregl.setWorkerUrl(maplibreWorkerUrl)

const empty = (): FeatureCollection => ({ type: 'FeatureCollection', features: [] })
const sourceId = (type: string) => type === 'cable_line' ? 'objects-cable_line' : 'objects-points'
const pointTypes = (types: ObjectType[]) => types.filter(type => type !== 'cable_line')
// Extra object types retain the legacy backend-driven geometry and sizes.
// Nodes and couplers use screen-sized symbols from the point icon registry.

export class MapLibreAdapter implements MapAdapter {
  private pointSymbols: PointSymbolSettings
  private readonly types: ObjectType[]
  private readonly map: MlMap
  private listenersViewport = new Set<(viewport: Viewport, moving: boolean) => void>()
  private listenersSelect = new Set<(feature?: Feature) => void>()
  private listenersMeasure = new Set<(point: [number, number]) => void>()
  private listenersBasemapError = new Set<(message: string) => void>()
  private destroyed = false
  private mode: ToolMode = 'select'
  private selectedId?: string
  private cableFormat: 'vector' | 'raster' = 'vector'
  private cableTemplate?: string
  private listenersCableError = new Set<(error: { status?: number; code?: string; tileVersion?: string; message: string }) => void>()
  private cableTiles?: TileDescriptor
  private cableTileThemes: number[] = []
  private visible = new Set<ObjectType>()
  private readonly featureTypes = new Map<string, string>()
  private readonly featuresByType = new Map<ObjectType, Map<string, Feature>>()
  private measuring: [number, number][] = []
  private measureFinished = false
  private moving = false
  private generation = 0
  private readonly refreshTypesPending = new Set<ObjectType>()
  private readonly sourceChanges = new Map<ObjectType, { reset: boolean; upsert: Map<string, Feature>; remove: Set<string> }>()
  private refreshScheduled = false
  private readonly readyPromise: Promise<void>
  private rejectReady!: (error: unknown) => void
  private markReady!: () => void
  private onMoveStart = () => { this.moving = true; this.listenersViewport.forEach(fn => fn(this.viewport(), true)) }
  private onMove = () => this.listenersViewport.forEach(fn => fn(this.viewport(), true))
  private onMoveEnd = () => { this.moving = false; this.listenersViewport.forEach(fn => fn(this.viewport(), false)) }
  private onClick = (event: maplibregl.MapMouseEvent) => {
    if (this.mode === 'measure') { if (this.measureFinished) return; const point: [number, number] = [event.lngLat.lng, event.lngLat.lat]; this.measuring.push(point); this.drawMeasure(); this.listenersMeasure.forEach(listener => listener(point)); return }
    if (this.mode !== 'select') return
    const layers: string[] = this.types.includes('cable_line') ? [...cableLayerIds, ...cableLayerIds.map(id => `${id}-tiles`)] : []
    if (pointTypes(this.types).length) layers.push('network-icons', 'network-icon-backgrounds', 'network-labels', 'network-circles', 'network-triangles', 'network-triangle-outlines', 'network-image-fallbacks')
    const candidates = this.map.queryRenderedFeatures(event.point).filter(feature => layers.includes(feature.layer.id))
    const rendered = candidates.find(feature => this.visible.has((feature.source === 'cables-tiles' ? 'cable_line' : String(feature.properties?.objectType)) as ObjectType))
    const selected = rendered ? { type: 'Feature' as const, id: rendered.properties?.objectId ?? rendered.id ?? '', geometry: rendered.geometry,
      properties: { ...rendered.properties, ...(rendered.source === 'cables-tiles' ? { objectType: 'cable_line' } : {}) } } : undefined
    this.listenersSelect.forEach(listener => listener(selected as Feature | undefined))
  }
  private onError = (event: maplibregl.ErrorEvent & { sourceId?: string }) => {
    const error = event.error as Error & { status?: number; url?: string; body?: Blob }
    const match = error.url?.match(/\/tiles\/maps\/\d+\/(vector|raster)\/([a-f0-9-]{36})\//)
    if (event.sourceId === 'basemap') this.listenersBasemapError.forEach(listener => listener('Подложка карты недоступна'))
    if (event.sourceId !== 'cables-tiles' && !match) return
    const version = match?.[2] ?? this.cableTiles?.tileVersion
    // Never expose signed request URLs in cards/log messages.
    void (async () => {
      let code: string | undefined
      if (error.body instanceof Blob) { try { code = JSON.parse(await error.body.text()).error?.code } catch { /* HTTP fallback below. */ } }
      if (!code && error.status === 403 && error.url) {
        const exp = Number(new URL(error.url).searchParams.get('exp'))
        code = exp && Date.now() >= exp * 1000 ? 'TILE_SIGNATURE_EXPIRED' : 'TILE_SIGNATURE_INVALID'
      }
      if (this.destroyed) return
      this.listenersCableError.forEach(fn => fn({ status: error.status, code, tileVersion: version, message: `Ошибка тайлов: ${code ?? error.status ?? 'NETWORK_ERROR'}` }))
    })()
  }

  constructor(container: HTMLElement, options: MapAdapterOptions) {
    this.pointSymbols = normalizePointSymbols(options.pointSymbols)
    this.types = [...options.objectTypes]
    for (const type of this.types) this.featuresByType.set(type, new Map())
    this.visible = new Set(options.objectTypes)
    this.readyPromise = new Promise((resolve, reject) => { this.markReady = resolve; this.rejectReady = reject })
    this.map = new maplibregl.Map({
      container,
      center: options.center,
      zoom: options.zoom,
      style: { version: 8, 'font-faces': { 'Noto Sans Regular': [{ url: latinFont, 'unicode-range': ['U+0000-00FF', 'U+2000-206F'] }, { url: cyrillicFont, 'unicode-range': ['U+0400-052F'] }] }, sources: { basemap: { type: 'raster', tiles: [options.tiles], tileSize: 256, attribution: options.attribution } }, layers: [{ id: 'basemap-2gis', type: 'raster', source: 'basemap' }] }
    })
    if (import.meta.dev && typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('mapDiagnostics') === '1') {
      (window as DiagnosticWindow).__usersideMapLibreDiagnostic = this.map
    }
    this.map.on('load', async () => { try { await this.addObjectLayers(); this.markReady(); this.setVisibility([...this.visible]) } catch (error) { this.rejectReady(error) } })
    this.map.on('movestart', this.onMoveStart); this.map.on('move', this.onMove); this.map.on('moveend', this.onMoveEnd); this.map.on('click', this.onClick)
    this.map.on('error', this.onError)
  }
  private async addObjectLayers() {
    for (const [type, svg] of Object.entries(pointIcons)) {
      const pixels = await rasterizeIcon(svg)
      if (this.destroyed) return
      this.map.addImage(`network-icon-${type}`, pixels, { pixelRatio: 2 })
    }
    const triangle = document.createElement('canvas')
    triangle.width = 48; triangle.height = 48
    const context = triangle.getContext('2d')!
    context.fillStyle = '#fff'; context.beginPath(); context.moveTo(24, 4); context.lineTo(45, 43); context.lineTo(3, 43); context.closePath(); context.fill()
    this.map.addImage('network-triangle', context.getImageData(0, 0, 48, 48), { sdf: true, pixelRatio: 2 })
    if (this.types.includes('cable_line')) {
      const sid = sourceId('cable_line')
      this.map.addSource(sid, { type: 'geojson', data: empty(), promoteId: '__mapId', tolerance: 0 })
      for (const layer of cableLayers(sid)) this.map.addLayer(layer)
    }
    if (pointTypes(this.types).length) {
      const sid = sourceId('node')
      this.map.addSource(sid, { type: 'geojson', data: empty(), promoteId: '__mapId' })
      const visibleTypes: ExpressionSpecification = ['in', ['get', 'objectType'], ['literal', [...this.visible]]]
      const circleFilter: ExpressionSpecification = ['all', visibleTypes, ['!=', ['get', '__mapShape'], 'triangle'], ['!=', ['get', '__mapShape'], 'imageFallback'], ['!=', ['get', '__mapShape'], 'symbol']]
      this.map.addLayer({ id: 'network-circles', type: 'circle', source: sid, filter: circleFilter, layout: { 'circle-sort-key': ['coalesce', ['get', '__mapZIndex'], 0] }, paint: { 'circle-radius': legacyNodeRadiusExpression, 'circle-color': ['coalesce', ['get', '__mapColor'], ['case', ['==', ['get', 'objectType'], 'node'], '#008000', '#ea7b00']], 'circle-stroke-color': ['case', ['boolean', ['feature-state', 'selected'], false], '#FFFF59', ['coalesce', ['get', '__mapStrokeColor'], '#111827']], 'circle-stroke-width': ['case', ['boolean', ['feature-state', 'selected'], false], 6, ['coalesce', ['get', '__mapStrokeWidth'], 1]], 'circle-opacity': 1 } })
      const triangleFilter: ExpressionSpecification = ['all', visibleTypes, ['==', ['get', '__mapShape'], 'triangle']]
      this.map.addLayer({ id: 'network-triangles', type: 'fill', source: sid, filter: triangleFilter, layout: { 'fill-sort-key': ['coalesce', ['get', '__mapZIndex'], 0] }, paint: { 'fill-color': ['coalesce', ['get', '__mapColor'], '#008000'], 'fill-opacity': 1, 'fill-outline-color': ['coalesce', ['get', '__mapStrokeColor'], '#111827'] } })
      this.map.addLayer({ id: 'network-triangle-outlines', type: 'line', source: sid, filter: triangleFilter, layout: { 'line-cap': 'round', 'line-join': 'round', 'line-sort-key': ['coalesce', ['get', '__mapZIndex'], 0] }, paint: { 'line-color': ['case', ['boolean', ['feature-state', 'selected'], false], '#FFFF59', ['coalesce', ['get', '__mapStrokeColor'], '#111827']], 'line-width': ['case', ['boolean', ['feature-state', 'selected'], false], 6, ['coalesce', ['get', '__mapStrokeWidth'], 1]], 'line-opacity': 1 } })
      this.map.addLayer({ id: 'network-image-fallbacks', type: 'symbol', source: sid, filter: ['all', visibleTypes, ['==', ['get', '__mapShape'], 'imageFallback']], layout: { 'icon-image': 'network-triangle', 'icon-size': legacyImageSizeExpression, 'icon-allow-overlap': true, 'symbol-sort-key': ['coalesce', ['get', '__mapZIndex'], 0] }, paint: { 'icon-color': ['coalesce', ['get', '__mapColor'], '#008000'], 'icon-halo-color': ['case', ['boolean', ['feature-state', 'selected'], false], '#FFFF59', ['coalesce', ['get', '__mapStrokeColor'], '#111827']], 'icon-halo-width': ['case', ['boolean', ['feature-state', 'selected'], false], 6, ['coalesce', ['get', '__mapStrokeWidth'], 1]] } })
    }
    if (pointTypes(this.types).length) {
      const sid = sourceId('node')
      const filter: ExpressionSpecification = ['all', ['in', ['get', 'objectType'], ['literal', [...this.visible]]], ['==', ['get', '__mapShape'], 'symbol']]
      this.map.addLayer({ id: 'network-icon-backgrounds', type: 'circle', source: sid, filter, minzoom: this.pointSymbols.iconMinZoom, paint: {
        'circle-radius': pointSizeExpression(this.pointSymbols.baseSize, 2),
        'circle-color': ['coalesce', ['get', '__mapColor'], '#e2e8f0'],
        'circle-stroke-color': ['case', ['boolean', ['feature-state', 'selected'], false], '#FFFF59', '#ffffff'],
        'circle-stroke-width': ['case', ['boolean', ['feature-state', 'selected'], false], 4, 2]
      } })
      this.map.addLayer({ id: 'network-icons', type: 'symbol', source: sid, filter, minzoom: this.pointSymbols.iconMinZoom, layout: {
        'icon-image': ['get', '__mapIcon'], 'icon-size': pointSizeExpression(this.pointSymbols.baseSize, 32),
        'icon-allow-overlap': true, 'icon-ignore-placement': true
      } })
      this.map.addLayer({ id: 'network-labels', type: 'symbol', source: sid, filter, minzoom: Math.max(this.pointSymbols.iconMinZoom, this.pointSymbols.labelMinZoom), layout: {
        'text-field': ['coalesce', ['get', 'title'], ''], 'text-font': ['Noto Sans Regular'], 'text-size': 12,
        'text-anchor': 'top', 'text-offset': [0, this.pointSymbols.baseSize * 1.25 / 24 + 0.4],
        'text-padding': 4, 'text-max-width': 16, 'text-allow-overlap': false
      }, paint: { 'text-color': '#111827', 'text-halo-color': '#ffffff', 'text-halo-width': 2 } })
    }
    this.map.addSource('selected-cable', { type: 'geojson', data: empty(), tolerance: 0 })
    this.map.addLayer({ id: 'selected-cable', type: 'line', source: 'selected-cable', paint: { 'line-color': '#FFFF59', 'line-width': 6 } })
    this.map.addSource('measure-line', { type: 'geojson', data: empty() })
    this.map.addLayer({ id: 'measure-line', type: 'line', source: 'measure-line', paint: { 'line-color': '#facc15', 'line-width': 3, 'line-dasharray': [2, 2] } })
    this.map.addSource('measure-points', { type: 'geojson', data: empty() })
    this.map.addLayer({ id: 'measure-points', type: 'circle', source: 'measure-points', paint: { 'circle-radius': 5, 'circle-color': '#facc15', 'circle-stroke-width': 2, 'circle-stroke-color': '#111827' } })
    this.setVisibility([...this.visible])
  }
  private viewport(): Viewport { const b = this.map.getBounds(); return { bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], zoom: this.map.getZoom() } }
  onViewport(listener: (viewport: Viewport, moving: boolean) => void) { this.listenersViewport.add(listener); return () => this.listenersViewport.delete(listener) }
  onSelect(listener: (feature?: Feature) => void) { this.listenersSelect.add(listener); return () => this.listenersSelect.delete(listener) }
  onMeasurePoint(listener: (point: [number, number]) => void) { this.listenersMeasure.add(listener); return () => this.listenersMeasure.delete(listener) }
  onCableError(listener: (error: { status?: number; code?: string; tileVersion?: string; message: string }) => void) { this.listenersCableError.add(listener); return () => this.listenersCableError.delete(listener) }
  onBasemapError(listener: (message: string) => void) { this.listenersBasemapError.add(listener); return () => this.listenersBasemapError.delete(listener) }
  ready() { return this.readyPromise }
  getViewport(): Viewport { return this.viewport() }
  private prepareFeature(feature: Feature): Feature {
    const rendered = prepareRenderFeature(preparePointSymbol(feature))
    if (this.selectedId === String(feature.id) && feature.properties.objectType === 'cable_line') rendered.properties.__mapWidth = 6
    return rendered
  }
  apply(change: Change) {
    if (change.kind === 'reset') {
      this.generation = change.generation
      this.featureTypes.clear()
      this.featuresByType.forEach(features => features.clear())
      for (const type of this.types) this.scheduleRefresh(type, { reset: true })
      return
    }
    if (change.generation < this.generation) return
    this.generation = change.generation
    const feature = change.kind === 'remove' ? undefined : change.feature
    const id = change.kind === 'remove' ? change.id : String(feature!.id)
    const previousType = this.featureTypes.get(id) as ObjectType | undefined
    if (change.kind === 'remove') {
      if (previousType) this.featuresByType.get(previousType)?.delete(id)
      this.featureTypes.delete(id)
      if (previousType) this.scheduleRefresh(previousType, { remove: id })
      return
    }
    const type = String(feature!.properties.objectType) as ObjectType
    if (previousType && previousType !== type) {
      this.featuresByType.get(previousType)?.delete(id)
      this.scheduleRefresh(previousType, { remove: id })
    }
    if (previousType === type && this.featuresByType.get(type)?.get(id) === feature) return
    this.featuresByType.get(type)?.set(id, feature!)
    this.featureTypes.set(id, type)
    this.scheduleRefresh(type, { upsert: feature! })
  }
  private scheduleRefresh(type: ObjectType, change: { reset?: boolean; upsert?: Feature; remove?: string } = {}) {
    const pending = this.sourceChanges.get(type) ?? { reset: false, upsert: new Map<string, Feature>(), remove: new Set<string>() }
    if (change.reset) { pending.reset = true; pending.upsert.clear(); pending.remove.clear() }
    if (change.remove) { pending.upsert.delete(change.remove); if (!pending.reset) pending.remove.add(change.remove) }
    if (change.upsert) { const id = String(change.upsert.id); pending.remove.delete(id); pending.upsert.set(id, change.upsert) }
    this.sourceChanges.set(type, pending)
    this.refreshTypesPending.add(type)
    void this.readyPromise.then(() => {
      if (this.refreshScheduled) return
      this.refreshScheduled = true
      requestAnimationFrame(() => {
        this.refreshScheduled = false
        const pending = [...this.refreshTypesPending]
        this.refreshTypesPending.clear()
        for (const item of pending) void this.refreshType(item).catch(error => console.error('[cartography] GeoJSON source update failed', error))
      })
    }).catch(error => console.error('[cartography] GeoJSON source readiness failed', error))
  }
  private async refreshType(type: ObjectType) {
    const source = this.map.getSource(sourceId(type)) as GeoJSONSource | undefined
    if (!source) return
    const groupedTypes = type === 'cable_line' ? [type] : pointTypes(this.types)
    const grouped = groupedTypes.flatMap(item => {
      const pending = this.sourceChanges.get(item)
      if (!pending) return []
      this.sourceChanges.delete(item)
      return [pending]
    })
    if (!grouped.length) return
    const reset = grouped.some(pending => pending.reset)
    if (reset) {
      const typesToReset = type === 'cable_line' ? [type] : pointTypes(this.types)
      const features = typesToReset.flatMap(item => [...(this.featuresByType.get(item)?.values() ?? [])]).map(feature => this.prepareFeature(feature))
      await source.setData({ type: 'FeatureCollection', features })
      return
    }
    const additions = grouped.flatMap(pending => [...pending.upsert.values()]).map(feature => this.prepareFeature(feature))
    const diff: GeoJSONSourceDiff = {}
    const removals = new Set(grouped.flatMap(pending => [...pending.remove]))
    if (removals.size) diff.remove = [...removals]
    if (additions.length) diff.add = additions
    if (diff.remove?.length || diff.add?.length) await source.updateData(diff)
  }
  setVisibility(typesToShow: ObjectType[]) {
    this.visible = new Set(typesToShow)
    if (!this.map.getStyle()) return
    for (const layer of cableLayerIds) if (this.map.getLayer(layer)) this.map.setLayoutProperty(layer, 'visibility', this.visible.has('cable_line') && !this.cableTiles ? 'visible' : 'none')
    if (this.map.getLayer('cable-raster')) this.map.setLayoutProperty('cable-raster', 'visibility', this.visible.has('cable_line') ? 'visible' : 'none')
    for (const layer of cableLayerIds.map(id => `${id}-tiles`)) if (this.map.getLayer(layer)) this.map.setLayoutProperty(layer, 'visibility', this.visible.has('cable_line') ? 'visible' : 'none')
    const visibleTypes: ExpressionSpecification = ['in', ['get', 'objectType'], ['literal', [...this.visible]]]
    const circleFilter: ExpressionSpecification = ['all', visibleTypes, ['!=', ['get', '__mapShape'], 'triangle'], ['!=', ['get', '__mapShape'], 'imageFallback'], ['!=', ['get', '__mapShape'], 'symbol']]
    const triangleFilter: ExpressionSpecification = ['all', visibleTypes, ['==', ['get', '__mapShape'], 'triangle']]
    const imageFilter: ExpressionSpecification = ['all', visibleTypes, ['==', ['get', '__mapShape'], 'imageFallback']]
    const symbolFilter: ExpressionSpecification = ['all', visibleTypes, ['==', ['get', '__mapShape'], 'symbol']]
    for (const layer of ['network-icons', 'network-icon-backgrounds', 'network-labels']) if (this.map.getLayer(layer)) this.map.setFilter(layer, symbolFilter)
    if (this.map.getLayer('network-circles')) this.map.setFilter('network-circles', circleFilter)
    if (this.map.getLayer('network-triangles')) this.map.setFilter('network-triangles', triangleFilter)
    if (this.map.getLayer('network-triangle-outlines')) this.map.setFilter('network-triangle-outlines', triangleFilter)
    if (this.map.getLayer('network-image-fallbacks')) this.map.setFilter('network-image-fallbacks', imageFilter)
  }
  setCableTiles(descriptor?: TileDescriptor, layerIds: number[] = [], format: 'vector' | 'raster' = 'vector') {
    const previous = this.cableTiles, previousFormat = this.cableFormat
    const template = descriptor && format === 'raster' && window.devicePixelRatio >= 1.5 ? descriptor.template2x ?? descriptor.template : descriptor?.template
    this.cableTiles = descriptor
    this.cableFormat = format
    this.cableTileThemes = [...layerIds]
    if (!this.map.getLayer('selected-cable')) return
    const ids = [...cableLayerIds.map(id => `${id}-tiles`), 'cable-raster']
    if (!descriptor || previous?.tileVersion !== descriptor.tileVersion || previousFormat !== format || this.cableTemplate !== template || !this.map.getSource('cables-tiles')) {
      for (const id of ids) if (this.map.getLayer(id)) this.map.removeLayer(id)
      if (this.map.getSource('cables-tiles')) this.map.removeSource('cables-tiles')
      if (descriptor) {
        const before = ['network-circles', 'network-triangles', 'network-image-fallbacks', 'network-icon-backgrounds', 'selected-cable'].find(id => this.map.getLayer(id))
        if (format === 'vector') {
          this.map.addSource('cables-tiles', { type: 'vector', tiles: [template!], minzoom: descriptor.minzoom, maxzoom: descriptor.maxzoom, promoteId: 'objectId' })
          for (const layer of cableTileLayers('cables-tiles', layerIds)) this.map.addLayer(layer, before)
        } else {
          this.map.addSource('cables-tiles', { type: 'raster', tiles: [template!], tileSize: descriptor.tileSize, minzoom: descriptor.minzoom, maxzoom: descriptor.maxzoom })
          this.map.addLayer({ id: 'cable-raster', type: 'raster', source: 'cables-tiles', paint: { 'raster-opacity': 1, 'raster-fade-duration': 0 } }, before)
        }
      }
    } else if (this.cableTemplate !== template) {
      (this.map.getSource('cables-tiles') as VectorTileSource | RasterTileSource).setTiles([template!])
    }
    this.cableTemplate = template
    if (descriptor && format === 'vector') for (const layer of cableTileLayers('cables-tiles', this.cableTileThemes)) if (this.map.getLayer(layer.id)) this.map.setFilter(layer.id, layer.filter!)
    this.map.setMaxZoom(descriptor?.displayMaxzoom ?? 24)
    this.setVisibility([...this.visible])
  }
  retryCableTiles() { const source = this.map.getSource('cables-tiles') as VectorTileSource | RasterTileSource | undefined; if (source && this.cableTemplate) source.setTiles([this.cableTemplate]) }
  setSelectedFeature(feature?: Feature) {
    const source = this.map.getSource('selected-cable') as GeoJSONSource | undefined
    if (!source) return
    source.setData({ type: 'FeatureCollection', features: feature?.properties.objectType === 'cable_line' ? [feature] : [] })
  }
  setPointSymbols(settings: PointSymbolSettings) {
    this.pointSymbols = normalizePointSymbols(settings)
    if (!this.map.getLayer('network-icons')) return
    this.map.setLayoutProperty('network-icons', 'icon-size', pointSizeExpression(this.pointSymbols.baseSize, 32))
    this.map.setPaintProperty('network-icon-backgrounds', 'circle-radius', pointSizeExpression(this.pointSymbols.baseSize, 2))
    this.map.setLayerZoomRange('network-icons', this.pointSymbols.iconMinZoom, 24)
    this.map.setLayerZoomRange('network-icon-backgrounds', this.pointSymbols.iconMinZoom, 24)
    this.map.setLayerZoomRange('network-labels', Math.max(this.pointSymbols.iconMinZoom, this.pointSymbols.labelMinZoom), 24)
    this.map.setLayoutProperty('network-labels', 'text-offset', [0, this.pointSymbols.baseSize * 1.25 / 24 + 0.4])
  }
  setSelected(id?: string) {
    const previous = this.selectedId
    this.selectedId = id
    for (const [featureId, type] of this.featureTypes) if (this.map.getSource(sourceId(type))) this.map.setFeatureState({ source: sourceId(type), id: featureId }, { selected: featureId === id })
    for (const featureId of new Set([previous, id].filter((value): value is string => Boolean(value)))) {
      if (this.featureTypes.get(featureId) !== 'cable_line') continue
      const feature = this.featuresByType.get('cable_line')?.get(featureId)
      const source = this.map.getSource(sourceId('cable_line')) as GeoJSONSource | undefined
      if (!feature || !source) continue
      const style = feature.properties.style && typeof feature.properties.style === 'object' ? feature.properties.style as Record<string, unknown> : {}
      const width = featureId === id ? 6 : Number(style.width ?? 3)
      void source.updateData({ update: [{ id: featureId, addOrUpdateProperties: [{ key: '__mapWidth', value: width }] }] }).catch(error => console.error('[cartography] Selected cable update failed', error))
    }
  }
  setMode(mode: ToolMode) { this.mode = mode; if (mode !== 'measure') { this.measuring = []; this.measureFinished = false; this.drawMeasure() }; this.map.getCanvas().style.cursor = mode === 'measure' ? 'crosshair' : '' }
  setMeasure(points: [number, number][], finished: boolean) { this.measuring = [...points]; this.measureFinished = finished; this.drawMeasure() }
  private drawMeasure() {
    if (!this.map.getSource('measure-line')) return
    const line = this.measuring.length > 1 ? [{ type: 'Feature' as const, id: 'measurement', properties: {}, geometry: { type: 'LineString' as const, coordinates: this.measuring } }] : []
    const pts = this.measuring.map((coordinates, id) => ({ type: 'Feature' as const, id, properties: {}, geometry: { type: 'Point' as const, coordinates } }))
    ;(this.map.getSource('measure-line') as GeoJSONSource).setData({ type: 'FeatureCollection', features: line })
    ;(this.map.getSource('measure-points') as GeoJSONSource).setData({ type: 'FeatureCollection', features: pts })
  }
  zoomIn() { this.map.zoomIn() }
  zoomOut() { this.map.zoomOut() }
  resize() { this.map.resize() }
  destroy() { this.destroyed = true; if (import.meta.dev && typeof window !== 'undefined' && (window as DiagnosticWindow).__usersideMapLibreDiagnostic === this.map) delete (window as DiagnosticWindow).__usersideMapLibreDiagnostic; this.map.off('movestart', this.onMoveStart); this.map.off('move', this.onMove); this.map.off('moveend', this.onMoveEnd); this.map.off('click', this.onClick); this.map.off('error', this.onError); this.listenersViewport.clear(); this.listenersSelect.clear(); this.listenersMeasure.clear(); this.listenersBasemapError.clear(); this.listenersCableError.clear(); this.map.remove() }
}
