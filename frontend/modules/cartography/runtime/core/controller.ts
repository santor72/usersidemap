import type { CableMode, TileCatalog, TileDescriptor, TilePin, Catalog, Change, Feature, LoadState, ObjectDetails, ObjectType, Viewport } from '#map-data'
import type { MapAdapter, MapControllerOptions, ToolMode } from './contracts'
import { MapDataClient, MapTileClient, TileRefreshLoop } from '#map-data'

export class MapController {
  tileCatalog?: TileCatalog
  cableMode: CableMode = 'none'
  tileError?: string
  coldStart = false
  catalog?: Catalog
  selected?: Feature
  details?: Partial<ObjectDetails> & { error?: string }
  waitingForConfirmation = false
  mode: ToolMode = 'select'
  measurePoints: [number, number][] = []
  measureFinished = false
  basemapError?: string
  readonly enabledTypes: ObjectType[]
  readonly tools: ToolMode[]
  private enabled: ObjectType[]
  private layerIds: number[]
  private viewport?: Viewport
  private moving = false
  private pointMinZoom: number
  private started = false
  private timer?: ReturnType<typeof setTimeout>
  private selectedToken = 0
  private destroyed = false
  private detailsAbort?: AbortController
  private refreshFlight?: Promise<void>
  private modeOperation = 0
  private readonly tileLoop: TileRefreshLoop
  private tileOperation = 0
  private tileAbort?: AbortController
  private unsubs: Array<() => void> = []
  private onChange?: () => void

  constructor(readonly options: MapControllerOptions, private readonly data: MapDataClient, private readonly adapter: MapAdapter, onChange?: () => void, private readonly tiles = new MapTileClient()) {
    this.pointMinZoom = options.pointSymbols?.iconMinZoom ?? 15
    this.tileLoop = new TileRefreshLoop(() => this.refreshTileCatalog(), error => { if (!this.destroyed) { this.tileError = String(error); this.onChange?.() } })
    if (options.cableTiles && !['none', 'vector', 'raster'].includes(options.cableTiles)) throw new Error('CABLE_TILES должен быть none, vector или raster')
    this.enabledTypes = [...options.objectTypes]; this.tools = [...options.tools]; this.onChange = onChange
    this.enabled = options.initialState?.objectTypes?.filter(x => this.enabledTypes.includes(x)) ?? [...this.enabledTypes]
    this.layerIds = [...(options.initialState?.thematicLayerIds ?? [])]
    this.mode = options.initialState?.mode && this.tools.includes(options.initialState.mode) ? options.initialState.mode : 'select'
    this.adapter.setMode(this.mode)
    this.unsubs.push(this.data.subscribe(change => this.onDataChange(change)))
    this.unsubs.push(this.adapter.onViewport((viewport, moving) => this.onViewport(viewport, moving)))
    this.unsubs.push(this.adapter.onSelect(feature => { void this.select(feature) }))
    this.unsubs.push(this.adapter.onMeasurePoint(point => { if (this.mode === 'measure') { this.measurePoints.push(point); this.measureFinished = false; this.adapter.setMeasure(this.measurePoints, false); this.onChange?.() } }))
    if (this.adapter.onCableError) this.unsubs.push(this.adapter.onCableError(error => this.onCableError(error)))
    this.unsubs.push(this.adapter.onBasemapError(message => { this.basemapError = message; this.onChange?.() }))
  }
  get tileDescriptor(): TileDescriptor | undefined { return this.cableMode === 'none' ? undefined : this.tileCatalog?.formats[this.cableMode].active ?? undefined }
  get pin(): TilePin | undefined { const item = this.tileDescriptor; return item && this.cableMode !== 'none' ? { format: this.cableMode, tileVersion: item.tileVersion } : undefined }
  get tilesEnabled(): boolean { return Boolean(this.options.cableTiles && this.options.cableTiles !== 'none') }
  get lagging(): boolean { return Boolean(this.tileDescriptor?.tilesBehindActive && this.tileCatalog?.tiles_lag === 'banner') }
  get loadState(): LoadState { return this.data.getState() }
  get objects(): Feature[] { return this.data.getFeatures() }
  get activeTypes(): ObjectType[] { return [...this.enabled] }
  get activeLayerIds(): number[] { return [...this.layerIds] }
  async start() {
    this.catalog = await this.data.catalog(this.options.mapId).catch(() => undefined)
    if (this.destroyed) return
    if (this.tilesEnabled) {
      if (!this.adapter.setCableTiles) throw new Error('Адаптер карты не поддерживает тайлы кабелей')
      this.tileCatalog = await this.tiles.catalog(this.options.mapId)
      if (this.destroyed) return
      const requested = this.options.cableTiles as 'vector' | 'raster'
      if (this.tileCatalog.formats[requested].active) {
        this.cableMode = requested
        if ((this.options.initialState?.objectTypes ?? this.enabledTypes).includes('cable_line') && !this.enabled.includes('cable_line')) this.enabled.push('cable_line')
      }
      else this.coldStart = true
      this.mergeTileLayers()
    }
    if (this.catalog && this.cableMode === 'none') this.enabled = this.enabled.filter(type => this.catalog?.objectTypes.some(item => item.object_type === type))
    this.adapter.setVisibility(this.enabled)
    await this.adapter.ready()
    if (this.destroyed) return
    this.applyCableTiles()
    this.viewport = this.adapter.getViewport()
    this.started = true
    this.configureTileRefresh()
    await this.loadViewport()
    this.onChange?.()
  }
  private onViewport(viewport: Viewport, moving: boolean) {
    this.viewport = viewport
    if (!this.started) { this.moving = moving; return }
    if (moving) {
      if (!this.moving) {
        this.moving = true
        clearTimeout(this.timer)
        this.data.cancel()
        if (this.selected && this.cableMode === 'none') { ++this.selectedToken; this.details = undefined; this.waitingForConfirmation = true; this.adapter.setSelected(undefined); this.onChange?.() }
      }
      return
    }
    this.moving = false
    clearTimeout(this.timer)
    this.timer = setTimeout(() => { if (!this.destroyed && this.viewport) void this.loadViewport(true).then(() => this.onChange?.()) }, 180)
  }
  private onDataChange(change: Change) {
    if (this.destroyed) return
    this.adapter.apply(change)
    const tiledCableSelected = this.cableMode !== 'none' && this.selected?.properties.objectType === 'cable_line'
    if (!tiledCableSelected && (change.kind === 'reset' || (change.kind === 'remove' && this.selected && String(this.selected.id) === change.id))) void this.select(undefined)
    else if ((change.kind === 'add' || change.kind === 'update') && this.selected && String(this.selected.id) === String(change.feature.id) && !this.details && !this.moving) void this.select(change.feature)
    this.onChange?.()
  }
  setObjectType(type: ObjectType, enabled: boolean) {
    if (!this.enabledTypes.includes(type)) return
    if (enabled && type === 'cable_line' && this.tileDescriptor) { /* Available in release even after a newer sync. */ }
    else if (enabled && this.catalog && !this.catalog.objectTypes.some(item => item.object_type === type)) return
    this.enabled = enabled ? [...new Set([...this.enabled, type])] : this.enabled.filter(x => x !== type)
    this.adapter.setVisibility(this.enabled)
    if (!enabled && this.selected?.properties.objectType === type) void this.select(undefined)
    if (this.viewport) void this.loadViewport().then(() => this.onChange?.())
  }
  setThematicLayers(ids: number[]) {
    const all = this.catalog?.layers.map(layer => layer.source_id) ?? []
    this.layerIds = all.length && all.length === ids.length && all.every(id => ids.includes(id)) ? [] : [...ids]
    this.applyCableTiles()
    void this.select(undefined)
    if (this.viewport) void this.loadViewport().then(() => this.onChange?.())
  }
  setMode(mode: ToolMode) {
    if (!this.tools.includes(mode)) return
    if (mode !== this.mode) { this.measurePoints = []; this.measureFinished = false; this.adapter.setMeasure([], false) }
    this.mode = mode; this.adapter.setMode(mode); this.onChange?.()
  }
  setPointSymbols(settings: import('./contracts').PointSymbolSettings) {
    this.adapter.setPointSymbols?.(settings)
    const changed = this.pointMinZoom !== settings.iconMinZoom
    this.pointMinZoom = settings.iconMinZoom
    if (changed && this.started && !this.moving) {
      clearTimeout(this.timer)
      this.timer = setTimeout(() => { if (!this.destroyed) void this.loadViewport().then(() => this.onChange?.()) }, 180)
    }
  }
  zoomIn() { this.adapter.zoomIn() }
  zoomOut() { this.adapter.zoomOut() }
  refresh() { void this.refreshCurrent() }
  private async refreshCurrent() {
    if (this.tilesEnabled) {
      try { await this.refreshTileCatalog() } catch (error) { this.tileError = String(error); this.onChange?.(); return }
    }
    const nextCatalog = await this.data.catalog(this.options.mapId).catch(() => undefined)
    if (nextCatalog) {
      this.catalog = nextCatalog
      this.mergeTileLayers()
      this.enabled = this.enabled.filter(type => this.catalog?.objectTypes.some(item => item.object_type === type))
      this.layerIds = this.layerIds.filter(id => this.catalog?.layers.some(layer => layer.source_id === id))
      this.adapter.setVisibility(this.enabled)
    }
    if (this.cableMode !== 'none') this.adapter.retryCableTiles?.()
    if (this.viewport) await this.loadViewport()
    this.onChange?.()
  }
  private mergeTileLayers() {
    if (!this.catalog || !this.tileCatalog || this.cableMode === 'none') return
    const old = this.catalog.layers
    this.catalog.layers = this.tileCatalog.layers.map(layer => ({ source_id: layer.id, title: layer.title,
      default_visible: layer.defaultVisible, object_count: old.find(item => item.source_id === layer.id)?.object_count ?? 0 }))
    // The live source catalog can be newer (or empty) after sync/rollback.
    // Its absence/counts must not disable providers in the retained release.
    if (this.tileDescriptor) for (const type of this.enabledTypes) if (!this.catalog.objectTypes.some(item => item.object_type === type)) this.catalog.objectTypes.push({ object_type: type, count: 0, min_zoom: 0 })
  }
  private applyCableTiles() { this.adapter.setCableTiles?.(this.tileDescriptor, this.layerIds, this.cableMode === 'none' ? undefined : this.cableMode) }
  private configureTileRefresh() { if (this.tilesEnabled) this.tileLoop.configure(this.tileDescriptor?.templateExpiresAt, this.tileCatalog?.catalogPollSeconds ?? 0) }
  refreshOnFocus() { if (!this.destroyed && this.tilesEnabled && this.started) void this.tileLoop.trigger('focus') }
  private onCableError(error: { status?: number; code?: string; tileVersion?: string; message: string }) {
    if (this.destroyed || this.cableMode === 'none' || (error.tileVersion && error.tileVersion !== this.tileDescriptor?.tileVersion)) return
    this.tileError = error.message; this.onChange?.()
    if (error.code === 'TILE_SIGNATURE_INVALID') this.tileLoop.pause()
    else if (error.status === 410) void this.tileLoop.trigger('retired')
    else if (error.code === 'TILE_SIGNATURE_EXPIRED') void this.tileLoop.trigger('expired')
  }
  private async loadViewport(preserve = false) {
    if (!this.viewport || this.destroyed) return
    const descriptor = this.tileDescriptor
    await this.data.load(this.options.mapId, this.viewport, this.enabled, this.layerIds,
      { preserve, pin: this.pin, datasetVersion: descriptor?.datasetVersion, pointMinZoom: this.pointMinZoom })
    if (this.data.getState().apiError?.status === 410 && this.cableMode !== 'none') void this.tileLoop.trigger('retired')
  }
  async setCableMode(mode: CableMode) {
    if (mode === this.cableMode || (mode !== 'none' && !this.tilesEnabled)) return
    if (mode !== 'none' && !this.tileCatalog?.formats[mode].active) { this.tileError = `Нет опубликованного ${mode} выпуска; выполните bake и publish`; this.onChange?.(); return }
    const operation = ++this.modeOperation
    clearTimeout(this.timer)
    this.data.cancel()
    void this.select(undefined)
    this.cableMode = mode
    if (mode !== 'none') this.coldStart = false
    this.catalog = await this.data.catalog(this.options.mapId).catch(() => this.catalog)
    if (this.destroyed || operation !== this.modeOperation) return
    this.tileError = undefined
    this.mergeTileLayers()
    this.applyCableTiles()
    this.configureTileRefresh()
    await this.loadViewport()
    this.onChange?.()
  }
  refreshTileCatalog(): Promise<void> {
    if (this.destroyed) return Promise.resolve()
    if (this.refreshFlight) return this.refreshFlight
    this.refreshFlight = this.fetchTileCatalog().finally(() => { this.refreshFlight = undefined })
    return this.refreshFlight
  }
  private async fetchTileCatalog() {
    const operation = ++this.tileOperation
    this.tileAbort?.abort()
    const abort = new AbortController()
    this.tileAbort = abort
    const timeout = setTimeout(() => abort.abort(), 10000)
    let next: TileCatalog
    try { next = await this.tiles.catalog(this.options.mapId, abort.signal) } finally { clearTimeout(timeout) }
    if (this.destroyed || operation !== this.tileOperation) return
    const previous = this.tileDescriptor
    const current = this.cableMode === 'none' ? undefined : next.formats[this.cableMode].active
    if (previous && !current) throw new Error('Активный выпуск тайлов отсутствует; карта остаётся в выбранном режиме')
    if (current?.templateExpiresAt && Date.parse(current.templateExpiresAt) <= Date.now()) throw new Error('Catalog вернул истёкшую подпись тайлов')
    this.tileCatalog = next
    if (previous?.tileVersion !== current?.tileVersion) {
      clearTimeout(this.timer); this.data.cancel(); void this.select(undefined)
      this.mergeTileLayers(); this.applyCableTiles(); await this.loadViewport()
    } else this.applyCableTiles()
    this.configureTileRefresh()
    this.tileError = undefined
    this.onChange?.()
  }
  finishMeasure() { this.measureFinished = true; this.adapter.setMeasure(this.measurePoints, true); this.onChange?.() }
  clearMeasure() { this.measurePoints = []; this.measureFinished = false; this.adapter.setMeasure([], false); this.onChange?.() }
  measureLengthKm(): number { let meters = 0; for (let i = 1; i < this.measurePoints.length; i++) { const a = this.measurePoints[i - 1], b = this.measurePoints[i]; if (a && b) meters += haversine(a, b) }; return meters / 1000 }
  private async select(feature?: Feature) {
    // A rendered hit may contain only a tile fragment of a multi-part cable.
    // Keep the complete logical object from the data client for selection/details.
    feature = feature ? this.data.getFeature(String(feature.id)) ?? feature : undefined
    this.detailsAbort?.abort()
    const detailsAbort = new AbortController(); this.detailsAbort = detailsAbort
    const token = ++this.selectedToken; this.selected = feature; this.details = undefined; this.waitingForConfirmation = Boolean(feature && this.cableMode === 'none' && !this.data.isConfirmed(feature)); this.adapter.setSelected(feature ? String(feature.id) : undefined); this.adapter.setSelectedFeature?.(undefined); this.onChange?.()
    if (feature && this.pin && this.tileDescriptor) {
      const pin = this.pin, dataset = this.tileDescriptor.datasetVersion
      try {
        const item = await this.tiles.details(this.options.mapId, pin, feature, detailsAbort.signal)
        if (this.destroyed || token !== this.selectedToken) return
        if (item.details.datasetVersion !== dataset) throw new Error('Dataset details не соответствует выпуску тайлов')
        this.selected = item.feature; this.details = item.details
        this.adapter.setSelectedFeature?.(item.feature)
      } catch (error) { if (token !== this.selectedToken || this.destroyed) return; this.details = { error: String(error) }; if ((error as { api?: { status?: number } }).api?.status === 410) void this.tileLoop.trigger('retired') }
      this.onChange?.(); return
    }
    if (feature && !this.waitingForConfirmation) { try { const details = await this.data.details(feature); if (token === this.selectedToken) this.details = details } catch (error) { if (token !== this.selectedToken) return; if ((error as Error & { api?: { code?: string } }).api?.code === 'OBJECT_NOT_FOUND') { await this.select(undefined); return }; if (String(error).includes('Объект не подтвержден текущим запросом')) this.waitingForConfirmation = true; else this.details = { error: String(error) } } }
    this.onChange?.()
  }
  clearSelection() { void this.select(undefined) }
  destroy() { this.destroyed = true; this.tileLoop.destroy(); ++this.modeOperation; ++this.tileOperation; this.tileAbort?.abort(); this.detailsAbort?.abort(); clearTimeout(this.timer); this.data.destroy(); this.unsubs.forEach(fn => fn()); this.adapter.destroy() }
}
function haversine(a: [number, number], b: [number, number]) { const rad = Math.PI / 180, dLat = (b[1] - a[1]) * rad, dLon = (b[0] - a[0]) * rad; const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[1] * rad) * Math.cos(b[1] * rad) * Math.sin(dLon / 2) ** 2; return 6371008.8 * 2 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h)) }
