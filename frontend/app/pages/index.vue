<script setup lang="ts">
import type { MapController } from '../../modules/cartography/runtime/core/controller'
import type { MapControllerOptions } from '../../modules/cartography/runtime/core/contracts'
import type { ObjectType } from '#map-data'
import { nextTick, onMounted, watch } from 'vue'
import { mapObjectTypes } from '../map-types'

import networkIcon from 'lucide-static/icons/network.svg?url'
import couplerIcon from 'lucide-static/icons/link-2.svg?url'
import { normalizePointSymbols } from '../../modules/cartography/runtime/adapters/maplibre/point-symbols'

const config = useRuntimeConfig().public
const pointSymbols = reactive(normalizePointSymbols({ baseSize: Number(config.mapIconBaseSize), iconMinZoom: Number(config.mapIconMinZoom), labelMinZoom: Number(config.mapLabelMinZoom) }))
onMounted(() => {
  try {
    const saved = localStorage.getItem('userside-map-point-symbols')
    if (saved) Object.assign(pointSymbols, normalizePointSymbols({ ...pointSymbols, ...JSON.parse(saved) }))
  } catch { /* Storage may be unavailable; use runtime defaults. */ }
})
watch(pointSymbols, () => {
  controller.value?.setPointSymbols(pointSymbols)
  try { localStorage.setItem('userside-map-point-symbols', JSON.stringify(pointSymbols)) } catch { /* Keep settings for this session. */ }
})
const onMapReady = (value: MapController) => { controller.value = value; value.setPointSymbols(pointSymbols) }
const revision = ref(0)
const controller = shallowRef<MapController>()
const mapStartError = ref('')
const mapMounted = ref(true)
const lifecycleTestEnabled = import.meta.dev && useRequestURL().searchParams.get('mapLifecycle') === '1'
const mapOptions: MapControllerOptions = {
  cableTiles: String(config.cableTiles) as MapControllerOptions['cableTiles'],
  mapId: Number(config.mapId),
  pointSymbols,
  initialView: { longitude: Number(config.mapLongitude), latitude: Number(config.mapLatitude), zoom: Number(config.mapZoom) },
  objectTypes: mapObjectTypes.map(type => type.id),
  tools: ['select', 'measure'],
  initialState: { objectTypes: ['cable_line', 'node', 'coupler'], mode: 'select' },
  basemap: { tiles: String(config.mapTilesUrl), attribution: String(config.mapAttribution) }
}
const typeEnabled = (type: ObjectType) => Boolean(controller.value?.activeTypes.includes(type))
const state = computed(() => { revision.value; return controller.value?.loadState })
const catalog = computed(() => { revision.value; return controller.value?.catalog })
const tileView = computed(() => { revision.value; return { mode: controller.value?.cableMode ?? 'none', enabled: controller.value?.tilesEnabled, active: controller.value?.tileCatalog?.formats.vector.active, raster: controller.value?.tileCatalog?.formats.raster.active, lag: controller.value?.lagging, error: controller.value?.tileError, coldStart: controller.value?.coldStart } })
const changeCableMode = (event: Event) => controller.value?.setCableMode((event.target as HTMLSelectElement).value as 'none' | 'vector' | 'raster')
const changeTheme = (event: Event) => { const value = (event.target as HTMLSelectElement).value; controller.value?.setThematicLayers(value ? [Number(value)] : []) }
const details = computed(() => { revision.value; return controller.value?.details })
const selected = computed(() => { revision.value; return controller.value?.selected })
const isCable = computed(() => selected.value?.properties.objectType === 'cable_line')
const isTitleOnly = computed(() => {
  const type = selected.value?.properties.objectType
  return type === 'node' || type === 'coupler'
})
const lengthFormat = new Intl.NumberFormat('ru-RU', { style: 'unit', unit: 'meter', maximumFractionDigits: 2 })
const cableLength = computed(() => {
  const recordedLength = details.value?.properties?.linkLengthM
  const length = typeof recordedLength === 'number' && recordedLength > 0 ? recordedLength : details.value?.length_m
  return length == null ? 'Не указана' : lengthFormat.format(length)
})
const cableEndpoints = computed(() => {
  const names = details.value?.cableEndpoints
  return names?.some(Boolean) ? names.map(name => name ?? 'Название не указано').join(' · ') : 'Не указаны'
})
const countFor = (type: string) => catalog.value?.objectTypes.find(x => x.object_type === type)?.count ?? 0
const enableType = (type: ObjectType, event: Event) => controller.value?.setObjectType(type, (event.target as HTMLInputElement).checked)
const pretty = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2)
const restartMapForDiagnostics = async () => {
  mapStartError.value = ''
  controller.value = undefined
  mapMounted.value = false
  await nextTick()
  await new Promise(resolve => setTimeout(resolve, 100))
  mapMounted.value = true
}
</script>
<template>
  <main class="shell">
    <aside class="panel">
      <div class="brand"><span class="brand-mark">◎</span><div><strong>Network map</strong><small>Userside · published snapshot</small></div></div>
      <section class="section">
        <div class="section-title"><h2>Объекты</h2><span class="muted">карта 3</span></div>
        <label v-for="{ id: type, title } in mapObjectTypes" :key="type" class="check-row">
          <input type="checkbox" :checked="typeEnabled(type)" :disabled="Boolean(catalog && !catalog.objectTypes.some(item => item.object_type === type))" :aria-label="`${title}${catalog && !catalog.objectTypes.some(item => item.object_type === type) ? ', недоступен в каталоге карты' : ''}`" @change="enableType(type, $event)">
          <img v-if="type === 'node' || type === 'coupler'" class="object-icon" :class="type" :src="type === 'node' ? networkIcon : couplerIcon" alt=""><span v-else class="swatch" :class="type"></span><span>{{ title }}</span><span class="row-count">{{ tileView.mode === 'none' ? countFor(type).toLocaleString('ru-RU') : '—' }}</span>
          <span v-if="catalog && !catalog.objectTypes.some(item => item.object_type === type)" class="muted unavailable-type">Недоступно</span>
        </label>
      </section>
      <section v-if="tileView.enabled" class="section cable-settings">
        <div class="section-title"><h2>Кабели</h2></div>
        <label for="cable-mode">Режим отображения</label>
        <select id="cable-mode" :value="tileView.mode" @change="changeCableMode">
          <option value="none">Объекты (none)</option><option value="vector" :disabled="!tileView.active">Vector тайлы</option><option value="raster" :disabled="!tileView.raster">Raster тайлы</option>
        </select>
        <small v-if="tileView.mode === 'raster'" class="muted">Raster: кабели без тематических фильтров и выбора; пунктир отображается сплошной линией. Фильтр ниже действует только на точки.</small>
        <small v-if="tileView.coldStart" class="muted">Нет готового выпуска выбранного формата. Используется объектный режим.</small>
        <div v-if="tileView.error" class="error-text">{{ tileView.error }} <button @click="controller?.refresh()">Повторить</button></div>
      </section>
      <section v-if="catalog?.layers.length" class="section cable-settings">
        <label for="theme-layer">Тематический слой</label>
        <select id="theme-layer" :value="controller?.activeLayerIds[0] ?? ''" @change="changeTheme">
          <option value="">Все слои</option><option v-for="layer in catalog.layers" :key="layer.source_id" :value="layer.source_id">{{ layer.title }} ({{ layer.source_id }})</option>
        </select>
      </section>
      <section class="section symbol-settings">
        <div class="section-title"><h2>Значки и подписи</h2></div>
        <label for="icon-size">Базовый размер <span>{{ pointSymbols.baseSize }} px</span></label>
        <input id="icon-size" v-model.number="pointSymbols.baseSize" type="range" min="12" max="48" step="1">
        <label for="icon-zoom">Иконки узлов и муфт с zoom <span>{{ pointSymbols.iconMinZoom }}</span></label>
        <input id="icon-zoom" v-model.number="pointSymbols.iconMinZoom" type="range" min="0" max="22" step="1">
        <label for="label-zoom">Подписи с zoom <span>{{ pointSymbols.labelMinZoom }}</span></label>
        <input id="label-zoom" v-model.number="pointSymbols.labelMinZoom" type="range" min="10" max="20" step="1">
        <small class="muted">Базовый размер на zoom 16. При отдалении значки уменьшаются, при приближении увеличиваются.</small>
      </section>
      <section class="section tools">
        <div class="section-title"><h2>Инструменты</h2><div class="zoom-buttons"><button aria-label="Приблизить" @click="controller?.zoomIn()">+</button><button aria-label="Отдалить" @click="controller?.zoomOut()">−</button></div></div>
        <div class="button-pair"><button :class="{ active: controller?.mode === 'select' }" @click="controller?.setMode('select')">Выбор</button><button :class="{ active: controller?.mode === 'measure' }" @click="controller?.setMode('measure')">Измерение</button></div>
        <div v-if="controller?.mode === 'measure'" class="measure-result"><span>{{ controller.measureLengthKm().toFixed(3) }} км · {{ controller.measurePoints.length }} точек</span><div><button @click="controller?.finishMeasure()">Завершить</button><button @click="controller?.clearMeasure()">Сброс</button></div></div>
      </section>
      <section class="load-status">
        <div class="section-title"><h2>Загрузка</h2><span class="status-dot" :class="state?.phase"></span></div>
        <div v-if="!state || state.phase === 'idle'">Ожидание карты</div>
        <template v-else>
          <div class="status-label">{{ state.phase === 'loading' ? 'Загружаем объекты' : state.phase === 'partial' ? 'Показываем результат' : state.phase === 'ready' ? 'Область загружена' : 'Ошибка загрузки' }}</div>
          <div v-if="state.totalBatches" class="progress-track"><span :style="{ width: `${Math.round(state.loadedBatches / state.totalBatches * 100)}%` }"></span></div>
          <div v-if="state.totalBatches" class="muted">Пачки {{ state.loadedBatches }}/{{ state.totalBatches }} · {{ state.loadedObjects }}/{{ state.totalObjects }} объектов</div>
          <div v-if="state.error" class="error-text">{{ state.error }}</div>
          <button v-if="state.phase === 'error'" class="retry-button" @click="controller?.refresh()">Повторить загрузку</button>
        </template>
      </section>
      <button v-if="lifecycleTestEnabled" class="lifecycle-test" @click="restartMapForDiagnostics">Пересоздать карту (проверка)</button>
      <footer>Данные из опубликованной версии FastAPI</footer>
    </aside>
    <section class="map-frame">
      <ClientOnly fallback-tag="div"><MapCanvas v-if="mapMounted" :options="mapOptions" @ready="onMapReady" @change="revision++" @error="mapStartError = $event" />
        <template #fallback><div class="map-placeholder">Инициализация карты…</div></template>
      </ClientOnly>
      <div class="map-hint">Перемещайте карту, чтобы загрузить объекты выбранной области</div>
      <div v-if="tileView.lag" class="tiles-lag-banner">Кабельные тайлы отстают от последней синхронизации. Точки соответствуют выпуску кабелей.</div>
      <div v-if="mapStartError" class="basemap-warning">Не удалось запустить карту: {{ mapStartError }} <button @click="restartMapForDiagnostics">Повторить</button></div>
      <div v-if="controller?.basemapError" class="basemap-warning">Подложка карты недоступна. Загрузка объектов продолжает работать.</div>
      <article v-if="selected" class="feature-card">
        <button class="close-card" aria-label="Закрыть" @click="controller?.clearSelection()">×</button>
        <h2 v-if="isTitleOnly">{{ selected.properties.title }}</h2>
        <template v-else>
          <h2 v-if="isCable">Кабель</h2>
          <template v-else>
            <small>{{ mapObjectTypes.find(type => type.id === selected?.properties.objectType)?.title ?? selected.properties.objectType }}</small>
            <h2>{{ selected.properties.title || `Объект ${selected.id}` }}</h2>
          </template>
          <div v-if="controller?.waitingForConfirmation" class="muted">Ожидаем подтверждения объекта в новой области…</div>
          <div v-else-if="!details" class="muted">Загружаем детали…</div>
          <div v-else-if="details.error" class="error-text">{{ details.error }}</div>
          <dl v-else-if="isCable">
            <dt>Длина</dt><dd>{{ cableLength }}</dd>
            <dt>Между</dt><dd>{{ cableEndpoints }}</dd>
          </dl>
          <template v-else>
            <dl><template v-for="(value, key) in details" :key="key"><dt v-if="!['properties', 'links', 'layers', 'sourceKind', 'sourceId', 'queryId', 'datasetVersion'].includes(String(key))">{{ key }}</dt><dd v-if="!['properties', 'links', 'layers', 'sourceKind', 'sourceId', 'queryId', 'datasetVersion'].includes(String(key))">{{ pretty(value) }}</dd></template></dl>
            <div v-if="details.properties && Object.keys(details.properties).length"><h3>Свойства</h3><dl><template v-for="(value, key) in details.properties" :key="key"><dt>{{ key }}</dt><dd>{{ pretty(value) }}</dd></template></dl></div>
            <div v-if="details.links?.length"><h3>Связи</h3><pre>{{ pretty(details.links) }}</pre></div>
          </template>
        </template>
      </article>
    </section>
  </main>
</template>
