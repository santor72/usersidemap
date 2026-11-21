<script setup lang="ts">
import { nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import type { MapAdapter, MapControllerOptions } from '../core/contracts'
import { MapController } from '../core/controller'

const props = defineProps<{ options: MapControllerOptions }>()
const emit = defineEmits<{ ready: [controller: MapController]; change: []; error: [message: string] }>()
const element = ref<HTMLElement>()
type DiagnosticWindow = Window & { __usersideMapControllerDiagnostic?: MapController }
const clearDiagnostic = () => { const diagnostic = window as DiagnosticWindow; if (diagnostic.__usersideMapControllerDiagnostic === controller) delete diagnostic.__usersideMapControllerDiagnostic }
let unmounted = false
let controller: MapController | undefined
const onFocus = () => controller?.refreshOnFocus()
const onVisibility = () => { if (document.visibilityState === 'visible') onFocus() }
let resizeObserver: ResizeObserver | undefined
onMounted(async () => {
  let adapter: MapAdapter | undefined
  try {
    await nextTick()
    const container = element.value ?? document.querySelector<HTMLElement>('.map-canvas') ?? undefined
    if (!container) throw new Error('Контейнер карты не создан')
    const data = useNuxtApp().$createMapDataClient()
    const config = useRuntimeConfig().public
    adapter = await useNuxtApp().$createMapAdapter(container, { center: [props.options.initialView.longitude, props.options.initialView.latitude], zoom: props.options.initialView.zoom, tiles: props.options.basemap?.tiles ?? String(config.mapTilesUrl), attribution: props.options.basemap?.attribution ?? String(config.mapAttribution), objectTypes: props.options.objectTypes, pointSymbols: props.options.pointSymbols })
    controller = new MapController({ ...props.options, cableTiles: props.options.cableTiles ?? String(config.cableTiles) as MapControllerOptions['cableTiles'] }, data, adapter, () => emit('change'))
    if (import.meta.dev && new URLSearchParams(window.location.search).get('mapDiagnostics') === '1') (window as DiagnosticWindow).__usersideMapControllerDiagnostic = controller
    resizeObserver = new ResizeObserver(() => adapter?.resize())
    resizeObserver.observe(container)
    emit('ready', controller)
    await controller.start()
    if (unmounted) return
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibility)
  } catch (error) {
    resizeObserver?.disconnect(); clearDiagnostic()
    if (controller) controller.destroy()
    else adapter?.destroy()
    controller = undefined
    const message = error instanceof Error ? error.message : String(error)
    console.error('[cartography] Не удалось запустить карту', error)
    emit('error', message)
  }
})
onBeforeUnmount(() => { clearDiagnostic(); unmounted = true; window.removeEventListener('focus', onFocus); document.removeEventListener('visibilitychange', onVisibility); resizeObserver?.disconnect(); controller?.destroy(); controller = undefined })
watch(() => props.options.initialView, () => {})
</script>
<template><div ref="element" class="map-canvas"><slot /></div></template>
